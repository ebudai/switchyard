#!/usr/bin/env python3
"""Regression test: ticket-board HTTP write operations cover pane workflows."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    raise SystemExit(
        "ticket_board_write_api_test: psycopg3 is required for postgres write-API coverage; "
        "install Arch/CachyOS package python-psycopg, or run from a venv with psycopg installed"
    )

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import CALLER_ROLE_HEADER, WRITE_TOKEN_HEADER, TicketBoardServer


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "eric", "ops", "app", "audit", "inspector", "perf", "research", "main"]
SERVICE_ROLE = "ticket_board_service"
FRONTEND_SCRIPT_PATHS = [
    ROOT / "scripts" / "ticket_board" / "frontend_script_app.py",
    ROOT / "scripts" / "ticket_board" / "frontend_script_core.py",
    ROOT / "scripts" / "ticket_board" / "frontend_script_detail.py",
]
DEFAULT_TOKEN = object()
TEST_WRITE_TOKEN: str | None = None


class QuietNotifier:
    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return


def run(args: list[str], *, input_text: str | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    if capture:
        return subprocess.run(args, input=input_text, text=True, capture_output=True, check=True)
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def conninfo(socket_dir: Path, port: int, dbname: str, user: str = "postgres") -> str:
    return f"host={socket_dir} port={port} dbname={dbname} user={user}"


def psql(conn: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def main_commit() -> str:
    return run(["git", "--git-dir=/data/git/pgu.git", "rev-parse", "refs/heads/main"]).stdout.strip()


def create_roles(conn: str) -> None:
    psql(conn, "\n".join(f"CREATE ROLE {role} LOGIN;" for role in PANE_ROLES))


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-07-10T00:00:00+00:00",
        "updated": "2026-07-10T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def seed_json_ticket(
    store: Path,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    audit_signoff: bool = False,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    regression: bool = False,
    parked: bool = False,
) -> None:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "blocked_by": [],
        "parent_id": "",
        "blocked_reason": "",
        "implementation": implementation,
        "audit_prompt": "",
        "audit_signoff": audit_signoff,
        "needs_inspection": needs_inspection,
        "inspector_signoff": inspector_signoff,
        "needs_eric_signoff": needs_eric_signoff,
        "eric_signoff": eric_signoff,
        "manually_controlled": False,
        "parked": parked,
        "commit_hash": commit_hash,
        "commit_exempt": commit_exempt,
        "regression": regression,
        "created": "2026-07-10T00:00:00+00:00",
        "updated": "2026-07-10T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }
    (store / f"{ticket_id}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def seed_postgres_ticket(
    conn: str,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    audit_signoff: bool = False,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    regression: bool = False,
    parked: bool = False,
) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff, commit_hash,
    commit_exempt, regression, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{implementation}',
    {str(audit_signoff).lower()}, {str(needs_inspection).lower()}, {str(inspector_signoff).lower()}, {str(needs_eric_signoff).lower()},
    {str(eric_signoff).lower()}, '{commit_hash}', {str(commit_exempt).lower()}, {str(regression).lower()}, {str(parked).lower()},
    '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
    '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
""",
    )


def post_json(
    base_url: str,
    path: str,
    payload: dict[str, object],
    *,
    caller: str | None = None,
    token: str | None | object = DEFAULT_TOKEN,
    expect: int = 200,
) -> dict[str, object] | str:
    headers = {"Content-Type": "application/json"}
    if caller is not None:
        headers[CALLER_ROLE_HEADER] = caller
    effective_token = TEST_WRITE_TOKEN if token is DEFAULT_TOKEN else token
    if effective_token:
        headers[WRITE_TOKEN_HEADER] = effective_token
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == expect, (path, response.status, body)
            return json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == expect, (path, exc.code, body)
        return body


def upload_png(base_url: str) -> dict[str, object]:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (220, 90, 40)).save(buffer, format="PNG")
    headers = {"Content-Type": "image/png"}
    if TEST_WRITE_TOKEN:
        headers[WRITE_TOKEN_HEADER] = TEST_WRITE_TOKEN
    request = urllib.request.Request(
        base_url + "/api/upload",
        data=buffer.getvalue(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")
        assert response.status == 201, (response.status, body)
        return json.loads(body)


def get_ticket(base_url: str, ticket_id: str) -> dict[str, object]:
    with urllib.request.urlopen(base_url + "/api/board", timeout=5) as response:
        board = json.loads(response.read().decode("utf-8"))
    for ticket in board["tickets"]:
        if ticket["id"] == ticket_id:
            return ticket
    raise AssertionError(f"{ticket_id} not found in board")


def assert_served_html_contains_write_token(base_url: str) -> None:
    with urllib.request.urlopen(base_url + "/", timeout=5) as response:
        html = response.read().decode("utf-8")
    assert "window.PGU_TICKET_BOARD_WRITE_TOKEN" in html
    assert TEST_WRITE_TOKEN and TEST_WRITE_TOKEN in html


def call_arguments(source: str, open_paren: int) -> list[str]:
    args: list[str] = []
    start = open_paren + 1
    depth = 1
    string_quote = ""
    escape = False
    index = start
    while index < len(source):
        char = source[index]
        if string_quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == string_quote:
                string_quote = ""
        elif char in {"'", '"', "`"}:
            string_quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                arg = source[start:index].strip()
                if arg:
                    args.append(arg)
                return args
        elif char == "," and depth == 1:
            args.append(source[start:index].strip())
            start = index + 1
        index += 1
    raise AssertionError("unterminated updateTicket call")


def frontend_update_ticket_calls() -> list[tuple[Path, int, list[str]]]:
    calls: list[tuple[Path, int, list[str]]] = []
    needle = "updateTicket("
    for path in FRONTEND_SCRIPT_PATHS:
        source = path.read_text(encoding="utf-8")
        offset = 0
        while True:
            index = source.find(needle, offset)
            if index == -1:
                break
            offset = index + len(needle)
            if "function " in source[max(0, index - 32) : index]:
                continue
            line = source.count("\n", 0, index) + 1
            calls.append((path, line, call_arguments(source, index + len("updateTicket"))))
    return calls


def assert_frontend_update_calls_send_caller_role() -> None:
    missing = [
        f"{path.relative_to(ROOT)}:{line} args={len(args)}"
        for path, line, args in frontend_update_ticket_calls()
        if len(args) < 3
    ]
    assert not missing, "frontend updateTicket calls missing caller role: " + ", ".join(missing)


def assert_frontend_writes_send_auth_token() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in FRONTEND_SCRIPT_PATHS)
    assert "X-PGU-Write-Token" in source
    assert "window.PGU_TICKET_BOARD_WRITE_TOKEN" in source


def seed_fixtures(seed_ticket: object, commit_hash: str) -> None:
    seed_ticket("PGU-100", title="Route", state="analysis")
    seed_ticket("PGU-101", title="Start submit", state="analysis", assignee="ops", implementation="Ready.")
    seed_ticket("PGU-102", title="Audit signoff", state="audit", assignee="audit", implementation="Done.")
    seed_ticket("PGU-103", title="Audit kickback", state="audit", assignee="audit", implementation="Done.")
    seed_ticket(
        "PGU-104",
        title="Eric signoff",
        state="eric_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        needs_eric_signoff=True,
    )
    seed_ticket(
        "PGU-105",
        title="Eric reopen",
        state="eric_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        needs_eric_signoff=True,
    )
    seed_ticket(
        "PGU-106",
        title="Done",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        commit_hash=commit_hash,
    )
    seed_ticket("PGU-107", title="Defer", state="analysis")
    seed_ticket("PGU-108", title="Cancel", state="analysis")
    seed_ticket("PGU-116", title="Backlog cancel", state="backlog", assignee="ops", parked=True)
    seed_ticket(
        "PGU-117",
        title="Needs inspection submit",
        state="analysis",
        assignee="ops",
        implementation="Render output attached.",
        needs_inspection=True,
    )
    seed_ticket(
        "PGU-118",
        title="Inspection audit gate",
        state="inspection",
        assignee="ops",
        implementation="Rendered.",
        needs_inspection=True,
    )
    seed_ticket(
        "PGU-119",
        title="Inspection kickback",
        state="inspection",
        assignee="ops",
        implementation="Rendered.",
        needs_inspection=True,
        inspector_signoff=True,
    )
    seed_ticket(
        "PGU-130",
        title="Submit to inspection",
        state="analysis",
        assignee="unassigned",
        implementation="Rendered.",
        needs_inspection=True,
    )
    seed_ticket(
        "PGU-131",
        title="Submit to inspection not applicable",
        state="analysis",
        assignee="unassigned",
        implementation="Non-visual change.",
        needs_inspection=False,
    )
    seed_ticket(
        "PGU-132",
        title="Submit to inspection wrong state",
        state="audit",
        assignee="ops",
        implementation="Already in audit.",
        commit_hash=commit_hash,
    )
    seed_ticket(
        "PGU-120",
        title="Commit exempt submit",
        state="in_progress",
        assignee="research",
        implementation="No repo changes.",
    )
    seed_ticket(
        "PGU-121",
        title="Non-exempt submit",
        state="in_progress",
        assignee="perf",
        implementation="Needs commit.",
    )
    seed_ticket(
        "PGU-122",
        title="Commit exempt done",
        state="director_review",
        assignee="director",
        implementation="No repo changes.",
        audit_signoff=True,
    )
    seed_ticket(
        "PGU-123",
        title="Non-exempt done",
        state="director_review",
        assignee="director",
        implementation="Needs commit.",
        audit_signoff=True,
    )
    seed_ticket(
        "PGU-124",
        title="Eric review to inspection",
        state="eric_review",
        assignee="director",
        implementation="Rendered.",
        audit_signoff=True,
        inspector_signoff=True,
        needs_eric_signoff=True,
        needs_inspection=True,
        commit_hash=commit_hash,
    )
    seed_ticket(
        "PGU-125",
        title="Eric review inspection guard",
        state="eric_review",
        assignee="director",
        implementation="Rendered.",
        audit_signoff=True,
        needs_eric_signoff=True,
        needs_inspection=False,
    )
    seed_ticket("PGU-126", title="Invalid implementation assignee", state="analysis")
    seed_ticket("PGU-127", title="Inspector utility task", state="backlog", assignee="inspector", parked=True)
    seed_ticket("PGU-129", title="Force move override", state="analysis", assignee="unassigned")
    seed_ticket("PGU-109", title="Manual", state="analysis")
    seed_ticket("PGU-110", title="Blocker", state="analysis")
    seed_ticket("PGU-111", title="Blocked target", state="analysis")
    seed_ticket("PGU-112", title="Comment", state="analysis")
    seed_ticket("PGU-113", title="Merge source", state="analysis")
    seed_ticket("PGU-114", title="Merge target", state="analysis")
    seed_ticket(
        "PGU-115",
        title="Director kickback to implementation",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )


def exercise_write_api(base_url: str, commit_hash: str, *, frames: Path, assets: Path) -> None:
    assert_served_html_contains_write_token(base_url)
    unauth_spoof = post_json(
        base_url,
        "/api/tickets/PGU-100/actions/route",
        {"state": "backlog", "assignee": "ops"},
        caller="director",
        token=None,
        expect=403,
    )
    assert "missing or invalid X-PGU-Write-Token" in str(unauth_spoof), unauth_spoof
    wrong_token = post_json(
        base_url,
        "/api/tickets/PGU-100/actions/route",
        {"state": "backlog", "assignee": "ops"},
        caller="director",
        token="wrong-token",
        expect=403,
    )
    assert "missing or invalid X-PGU-Write-Token" in str(wrong_token), wrong_token
    missing = post_json(base_url, "/api/tickets/actions/create_ticket", {"title": "No caller", "body": ""}, expect=400)
    assert "missing X-PGU-Caller-Role" in str(missing), missing
    forbidden = post_json(
        base_url,
        "/api/tickets/PGU-100/actions/route",
        {"state": "backlog", "assignee": "ops"},
        caller="ops",
        expect=403,
    )
    assert "ops cannot call route" in str(forbidden), forbidden
    eric_director_only = post_json(
        base_url,
        "/api/tickets/PGU-108/actions/cancel",
        {"reason": "Eric should not cancel."},
        caller="eric",
        expect=403,
    )
    assert "eric cannot call cancel" in str(eric_director_only), eric_director_only
    legacy_create = post_json(
        base_url,
        "/api/tickets",
        {"title": "Bare create should be gone", "body": ""},
        caller="director",
        expect=404,
    )
    assert "Not found" in str(legacy_create), legacy_create
    legacy_missing = post_json(
        base_url,
        "/api/tickets/PGU-108",
        {"state": "cancelled", "comment": {"who": "director", "text": "No caller."}},
        expect=404,
    )
    assert "Not found" in str(legacy_missing), legacy_missing
    legacy_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-106",
        {"state": "done", "commit_hash": commit_hash},
        caller="ops",
        expect=404,
    )
    assert "Not found" in str(legacy_forbidden), legacy_forbidden
    legacy_eric_director_only = post_json(
        base_url,
        "/api/tickets/PGU-109",
        {"manually_controlled": True},
        caller="eric",
        expect=404,
    )
    assert "Not found" in str(legacy_eric_director_only), legacy_eric_director_only
    legacy_merge = post_json(
        base_url,
        "/api/tickets/PGU-113/merge",
        {"target_id": "PGU-114", "actor": "director"},
        caller="director",
        expect=404,
    )
    assert "Not found" in str(legacy_merge), legacy_merge

    created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API create", "body": "Created through operation API."},
        caller="director",
        expect=201,
    )
    created = created_payload["ticket"]  # type: ignore[index]
    source_id = str(created["id"])  # type: ignore[index]
    assert created["state"] == "analysis", created  # type: ignore[index]
    assert created["assignee"] == "unassigned", created  # type: ignore[index]

    backlog_created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API backlog create", "body": "Deferred work.", "initial_state": "backlog", "assignee": "ops"},
        caller="director",
        expect=201,
    )
    backlog_created = backlog_created_payload["ticket"]  # type: ignore[index]
    assert backlog_created["state"] == "backlog", backlog_created  # type: ignore[index]
    assert backlog_created["assignee"] == "unassigned", backlog_created  # type: ignore[index]

    disallowed_create_state = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Bad create state", "body": "No.", "initial_state": "audit"},
        caller="director",
        expect=400,
    )
    assert "invalid create state" in str(disallowed_create_state), disallowed_create_state

    uploaded = upload_png(base_url)
    uploaded_image = uploaded["image"]  # type: ignore[index]
    uploaded_path = Path(str(uploaded_image["path"]))  # type: ignore[index]
    pasted_create_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {
            "title": "API pasted image create",
            "body": "Created with a pasted image upload.",
            "screenshots": [str(uploaded_path)],
        },
        caller="director",
        expect=201,
    )
    pasted_create = pasted_create_payload["ticket"]  # type: ignore[index]
    pasted_path = Path(pasted_create["screenshots"][0])  # type: ignore[index]
    assert pasted_path == uploaded_path.resolve(), pasted_create
    assert assets.resolve() in pasted_path.parents, pasted_create
    assert pasted_create["screenshot"] == str(pasted_path), pasted_create  # type: ignore[index]
    assert pasted_create["screenshots_info"][0]["available"] is True, pasted_create  # type: ignore[index]
    assert pasted_path.is_file(), pasted_path
    persisted_pasted_create = get_ticket(base_url, str(pasted_create["id"]))  # type: ignore[index]
    assert persisted_pasted_create["screenshots"] == [str(pasted_path)], persisted_pasted_create

    filed_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "API file bug", "body": "Linked bug.", "source_ticket_id": source_id},
        caller="ops",
        expect=201,
    )
    filed = filed_payload["ticket"]  # type: ignore[index]
    assert filed["parent_id"] == source_id, filed  # type: ignore[index]
    audit_filed_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Audit filed bug", "body": "Found during review.", "source_ticket_id": source_id},
        caller="audit",
        expect=201,
    )
    audit_filed = audit_filed_payload["ticket"]  # type: ignore[index]
    assert audit_filed["parent_id"] == source_id, audit_filed  # type: ignore[index]
    assert audit_filed["state"] == "analysis", audit_filed  # type: ignore[index]
    assert audit_filed["assignee"] == "unassigned", audit_filed  # type: ignore[index]
    main_filed_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Main filed bug", "body": "Found in implementation.", "source_ticket_id": source_id},
        caller="main",
        expect=201,
    )
    main_filed = main_filed_payload["ticket"]  # type: ignore[index]
    assert main_filed["parent_id"] == source_id, main_filed  # type: ignore[index]
    assert main_filed["state"] == "analysis", main_filed  # type: ignore[index]
    assert main_filed["assignee"] == "unassigned", main_filed  # type: ignore[index]

    routed = post_json(base_url, "/api/tickets/PGU-100/actions/route", {"state": "backlog", "assignee": "ops"}, caller="director")
    assert routed["ticket"]["state"] == "backlog", routed  # type: ignore[index]
    assert routed["ticket"]["assignee"] == "ops", routed  # type: ignore[index]
    parked_routed = post_json(base_url, "/api/tickets/PGU-100/actions/defer", {}, caller="director")
    assert parked_routed["ticket"]["state"] == "backlog", parked_routed  # type: ignore[index]
    director_implementation = post_json(
        base_url,
        "/api/tickets/PGU-115/actions/route",
        {"state": "in_progress", "assignee": "main"},
        caller="director",
    )
    assert director_implementation["ticket"]["state"] == "in_progress", director_implementation  # type: ignore[index]
    assert director_implementation["ticket"]["assignee"] == "main", director_implementation  # type: ignore[index]
    invalid_implementation_assignee = post_json(
        base_url,
        "/api/tickets/PGU-126/actions/route",
        {"state": "in_progress", "assignee": "director"},
        caller="director",
        expect=400,
    )
    assert "in_progress tickets require an implementer assignee" in str(invalid_implementation_assignee), invalid_implementation_assignee
    normal_done_blocked = post_json(
        base_url,
        "/api/tickets/PGU-129/actions/route",
        {"state": "done", "assignee": "director"},
        caller="director",
        expect=400,
    )
    assert "illegal state transition: analysis -> done" in str(normal_done_blocked), normal_done_blocked
    force_move_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-129/actions/override_move",
        {"state": "done", "assignee": "director"},
        caller="ops",
        expect=403,
    )
    assert "ops cannot call override_move" in str(force_move_forbidden), force_move_forbidden
    force_moved = post_json(
        base_url,
        "/api/tickets/PGU-129/actions/override_move",
        {"state": "done", "assignee": "director", "no_notify": True},
        caller="director",
    )
    assert force_moved["ticket"]["state"] == "done", force_moved  # type: ignore[index]
    assert force_moved["ticket"]["assignee"] == "director", force_moved  # type: ignore[index]
    assert force_moved["ticket"]["comments"][-1]["text"] == (
        "Director override: analysis/unassigned -> done/director (notification suppressed)"
    ), force_moved  # type: ignore[index]

    wrong_assignee = post_json(base_url, "/api/tickets/PGU-101/actions/start_work", {}, caller="app", expect=403)
    assert "app cannot call start_work for ticket assigned to ops" in str(wrong_assignee), wrong_assignee
    utility_wrong_assignee = post_json(
        base_url,
        "/api/tickets/PGU-127/actions/start_task",
        {"text": "Wrong pane start."},
        caller="audit",
        expect=403,
    )
    assert "audit cannot call start_task for ticket assigned to inspector" in str(utility_wrong_assignee), utility_wrong_assignee
    utility_started = post_json(
        base_url,
        "/api/tickets/PGU-127/actions/start_task",
        {"text": "Starting utility task."},
        caller="inspector",
    )
    assert utility_started["ticket"]["state"] == "analysis", utility_started  # type: ignore[index]
    assert utility_started["ticket"]["comments"][-1]["text"] == "Starting utility task.", utility_started  # type: ignore[index]
    empty_utility_complete = post_json(
        base_url,
        "/api/tickets/PGU-127/actions/complete_task",
        {"text": "   "},
        caller="inspector",
        expect=400,
    )
    assert "completion_note must be a non-empty string" in str(empty_utility_complete), empty_utility_complete
    utility_completed = post_json(
        base_url,
        "/api/tickets/PGU-127/actions/complete_task",
        {"text": "Utility task complete."},
        caller="inspector",
    )
    assert utility_completed["ticket"]["state"] == "done", utility_completed  # type: ignore[index]
    assert utility_completed["ticket"]["commit_exempt"] is True, utility_completed  # type: ignore[index]
    assert utility_completed["ticket"]["comments"][-1]["text"] == "Utility task complete.", utility_completed  # type: ignore[index]
    started = post_json(base_url, "/api/tickets/PGU-101/actions/start_work", {}, caller="ops")
    assert started["ticket"]["state"] == "in_progress", started  # type: ignore[index]
    in_progress_utility_complete = post_json(
        base_url,
        "/api/tickets/PGU-101/actions/complete_task",
        {"text": "Trying to skip audit."},
        caller="ops",
        expect=400,
    )
    assert "complete_task requires a backlog or analysis ticket" in str(in_progress_utility_complete), in_progress_utility_complete
    submitted = post_json(base_url, "/api/tickets/PGU-101/actions/submit_to_audit", {"commit_hash": commit_hash}, caller="ops")
    assert submitted["ticket"]["state"] == "audit", submitted  # type: ignore[index]
    assert submitted["ticket"]["commit_hash"] == commit_hash, submitted  # type: ignore[index]
    signed_off_submitted = post_json(
        base_url,
        "/api/tickets/PGU-101/actions/audit_sign_off",
        {"text": "Audit verified for serial-focus release."},
        caller="audit",
    )
    assert signed_off_submitted["ticket"]["state"] == "director_review", signed_off_submitted  # type: ignore[index]
    completed_submitted = post_json(
        base_url,
        "/api/tickets/PGU-101/actions/mark_done",
        {"commit_hash": commit_hash},
        caller="director",
    )
    assert completed_submitted["ticket"]["state"] == "done", completed_submitted  # type: ignore[index]
    non_exempt_empty_submit = post_json(
        base_url,
        "/api/tickets/PGU-121/actions/submit_to_audit",
        {},
        caller="perf",
        expect=400,
    )
    assert "commit_hash must be a 7-40 character hex commit" in str(non_exempt_empty_submit), non_exempt_empty_submit
    wrong_assignee_exempt_request = post_json(
        base_url,
        "/api/tickets/PGU-121/actions/request_commit_exempt",
        {"reason": "No repository change was needed."},
        caller="app",
        expect=403,
    )
    assert "app cannot call request_commit_exempt for ticket assigned to perf" in str(wrong_assignee_exempt_request), wrong_assignee_exempt_request
    empty_exempt_request = post_json(
        base_url,
        "/api/tickets/PGU-121/actions/request_commit_exempt",
        {"reason": "   "},
        caller="perf",
        expect=400,
    )
    assert "reason must be a non-empty string" in str(empty_exempt_request), empty_exempt_request
    exempt_requested = post_json(
        base_url,
        "/api/tickets/PGU-121/actions/request_commit_exempt",
        {"reason": "No repository change was needed."},
        caller="perf",
    )
    assert exempt_requested["ticket"]["state"] == "analysis", exempt_requested  # type: ignore[index]
    assert exempt_requested["ticket"]["assignee"] == "unassigned", exempt_requested  # type: ignore[index]
    assert exempt_requested["ticket"]["commit_exempt"] is False, exempt_requested  # type: ignore[index]
    assert exempt_requested["ticket"]["comments"][-1]["who"] == "perf", exempt_requested  # type: ignore[index]
    assert "Commit exemption requested: No repository change was needed." == exempt_requested["ticket"]["comments"][-1]["text"], exempt_requested  # type: ignore[index]
    submit_exempt_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-120/actions/edit_fields",
        {"commit_exempt": True},
        caller="ops",
        expect=403,
    )
    assert "commit_exempt can only be edited by director" in str(submit_exempt_forbidden), submit_exempt_forbidden
    submit_exempt_set = post_json(
        base_url,
        "/api/tickets/PGU-120/actions/edit_fields",
        {"commit_exempt": True},
        caller="director",
    )
    assert submit_exempt_set["ticket"]["commit_exempt"] is True, submit_exempt_set  # type: ignore[index]
    exempt_submitted = post_json(base_url, "/api/tickets/PGU-120/actions/submit_to_audit", {}, caller="research")
    assert exempt_submitted["ticket"]["state"] == "audit", exempt_submitted  # type: ignore[index]
    assert exempt_submitted["ticket"]["commit_hash"] == "", exempt_submitted  # type: ignore[index]
    assert exempt_submitted["ticket"]["commit_exempt"] is True, exempt_submitted  # type: ignore[index]

    eric_inspection_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Eric cannot require inspection", "body": "", "needs_inspection": True},
        caller="eric",
        expect=403,
    )
    assert "needs_inspection can only be set by director" in str(eric_inspection_create), eric_inspection_create
    eric_commit_exempt_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Eric cannot exempt commits", "body": "", "commit_exempt": True},
        caller="eric",
        expect=403,
    )
    assert "commit_exempt can only be set by director" in str(eric_commit_exempt_create), eric_commit_exempt_create
    file_bug_commit_exempt = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Ops cannot exempt filed bug", "body": "", "source_ticket_id": "PGU-100", "commit_exempt": True},
        caller="ops",
        expect=403,
    )
    assert "commit_exempt can only be set by director" in str(file_bug_commit_exempt), file_bug_commit_exempt
    director_inspection_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Director requires inspection", "body": "", "needs_inspection": True},
        caller="director",
        expect=201,
    )
    assert director_inspection_create["ticket"]["needs_inspection"] is True, director_inspection_create  # type: ignore[index]
    regression_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Director marks regression", "body": "", "regression": True},
        caller="director",
        expect=201,
    )
    assert regression_create["ticket"]["regression"] is True, regression_create  # type: ignore[index]
    assert get_ticket(base_url, str(regression_create["ticket"]["id"]))["regression"] is True
    regression_file_bug = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Regression bug child", "body": "", "source_ticket_id": "PGU-100", "regression": True},
        caller="ops",
        expect=201,
    )
    assert regression_file_bug["ticket"]["regression"] is True, regression_file_bug  # type: ignore[index]

    inspector_field_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"needs_inspection": True},
        caller="app",
        expect=403,
    )
    assert "needs_inspection can only be edited by director" in str(inspector_field_forbidden), inspector_field_forbidden
    inspector_field_director = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"needs_inspection": True},
        caller="director",
    )
    assert inspector_field_director["ticket"]["needs_inspection"] is True, inspector_field_director  # type: ignore[index]
    regression_field_set = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"regression": True},
        caller="app",
    )
    assert regression_field_set["ticket"]["regression"] is True, regression_field_set  # type: ignore[index]
    regression_field_cleared = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"regression": False},
        caller="app",
    )
    assert regression_field_cleared["ticket"]["regression"] is False, regression_field_cleared  # type: ignore[index]
    assert get_ticket(base_url, "PGU-112")["regression"] is False

    eric_review_to_inspection = post_json(
        base_url,
        "/api/tickets/PGU-124/actions/route",
        {"state": "inspection", "assignee": "inspector"},
        caller="director",
    )
    assert eric_review_to_inspection["ticket"]["state"] == "inspection", eric_review_to_inspection  # type: ignore[index]
    assert eric_review_to_inspection["ticket"]["assignee"] == "inspector", eric_review_to_inspection  # type: ignore[index]
    assert eric_review_to_inspection["ticket"]["needs_inspection"] is True, eric_review_to_inspection  # type: ignore[index]
    assert eric_review_to_inspection["ticket"]["audit_signoff"] is False, eric_review_to_inspection  # type: ignore[index]
    assert eric_review_to_inspection["ticket"]["inspector_signoff"] is False, eric_review_to_inspection  # type: ignore[index]
    assert eric_review_to_inspection["ticket"]["commit_hash"] == commit_hash, eric_review_to_inspection  # type: ignore[index]
    eric_review_to_inspection_guard = post_json(
        base_url,
        "/api/tickets/PGU-125/actions/route",
        {"state": "inspection", "assignee": "inspector"},
        caller="director",
        expect=400,
    )
    assert "needs_inspection must be true" in str(eric_review_to_inspection_guard), eric_review_to_inspection_guard

    inspect_started = post_json(base_url, "/api/tickets/PGU-117/actions/start_work", {}, caller="ops")
    assert inspect_started["ticket"]["state"] == "in_progress", inspect_started  # type: ignore[index]
    inspect_submitted = post_json(
        base_url,
        "/api/tickets/PGU-117/actions/submit_to_audit",
        {"commit_hash": commit_hash},
        caller="ops",
    )
    assert inspect_submitted["ticket"]["state"] == "inspection", inspect_submitted  # type: ignore[index]
    assert inspect_submitted["ticket"]["inspector_signoff"] is False, inspect_submitted  # type: ignore[index]

    self_inspect_started = post_json(
        base_url,
        "/api/tickets/PGU-130/actions/route",
        {"state": "in_progress", "assignee": "app"},
        caller="director",
    )
    assert self_inspect_started["ticket"]["state"] == "in_progress", self_inspect_started  # type: ignore[index]
    self_inspect_submitted = post_json(base_url, "/api/tickets/PGU-130/actions/submit_to_inspection", {}, caller="app")
    assert self_inspect_submitted["ticket"]["state"] == "inspection", self_inspect_submitted  # type: ignore[index]
    assert self_inspect_submitted["ticket"]["assignee"] == "inspector", self_inspect_submitted  # type: ignore[index]
    assert self_inspect_submitted["ticket"]["commit_hash"] == "", self_inspect_submitted  # type: ignore[index]
    assert self_inspect_submitted["ticket"]["inspector_signoff"] is False, self_inspect_submitted  # type: ignore[index]
    audit_cannot_submit_inspection = post_json(
        base_url,
        "/api/tickets/PGU-130/actions/submit_to_inspection",
        {},
        caller="audit",
        expect=403,
    )
    assert "audit cannot call submit_to_inspection" in str(audit_cannot_submit_inspection), audit_cannot_submit_inspection
    inspect_pingback = post_json(
        base_url,
        "/api/tickets/PGU-130/actions/inspector_kick_back",
        {"recommendations": "Sharper crop needed."},
        caller="inspector",
    )
    assert inspect_pingback["ticket"]["state"] == "in_progress", inspect_pingback  # type: ignore[index]
    assert inspect_pingback["ticket"]["assignee"] == "app", inspect_pingback  # type: ignore[index]
    inspect_round_two = post_json(base_url, "/api/tickets/PGU-130/actions/submit_to_inspection", {}, caller="app")
    assert inspect_round_two["ticket"]["state"] == "inspection", inspect_round_two  # type: ignore[index]
    assert inspect_round_two["ticket"]["assignee"] == "inspector", inspect_round_two  # type: ignore[index]
    post_json(
        base_url,
        "/api/tickets/PGU-130/actions/force_move",
        {"state": "done", "assignee": "director", "suppress_notification": True},
        caller="director",
    )
    not_applicable_started = post_json(
        base_url,
        "/api/tickets/PGU-131/actions/route",
        {"state": "in_progress", "assignee": "app"},
        caller="director",
    )
    assert not_applicable_started["ticket"]["state"] == "in_progress", not_applicable_started  # type: ignore[index]
    not_applicable_inspection = post_json(
        base_url,
        "/api/tickets/PGU-131/actions/submit_to_inspection",
        {},
        caller="app",
        expect=400,
    )
    assert "submit_to_inspection requires needs_inspection=true" in str(not_applicable_inspection), not_applicable_inspection
    wrong_state_inspection = post_json(
        base_url,
        "/api/tickets/PGU-132/actions/submit_to_inspection",
        {},
        caller="ops",
        expect=400,
    )
    assert "submit_to_inspection requires an in_progress ticket" in str(wrong_state_inspection), wrong_state_inspection

    inspector_cannot_audit = post_json(base_url, "/api/tickets/PGU-102/actions/audit_sign_off", {}, caller="inspector", expect=403)
    assert "inspector cannot call audit_sign_off" in str(inspector_cannot_audit), inspector_cannot_audit
    audit_cannot_inspect = post_json(base_url, "/api/tickets/PGU-117/actions/inspector_sign_off", {}, caller="audit", expect=403)
    assert "audit cannot call inspector_sign_off" in str(audit_cannot_inspect), audit_cannot_inspect
    main_cannot_inspect_kick = post_json(
        base_url,
        "/api/tickets/PGU-117/actions/inspector_kick_back",
        {"recommendations": "No."},
        caller="main",
        expect=403,
    )
    assert "main cannot call inspector_kick_back" in str(main_cannot_inspect_kick), main_cannot_inspect_kick

    inspection_skip = post_json(
        base_url,
        "/api/tickets/PGU-118/actions/route",
        {"state": "audit", "assignee": "ops"},
        caller="director",
        expect=400,
    )
    assert "inspector_signoff must be true" in str(inspection_skip), inspection_skip
    inspection_signed = post_json(base_url, "/api/tickets/PGU-117/actions/inspector_sign_off", {}, caller="inspector")
    assert inspection_signed["ticket"]["state"] == "audit", inspection_signed  # type: ignore[index]
    assert inspection_signed["ticket"]["inspector_signoff"] is True, inspection_signed  # type: ignore[index]
    inspection_audit_signed = post_json(
        base_url,
        "/api/tickets/PGU-117/actions/audit_sign_off",
        {"text": "Inspection path verified."},
        caller="audit",
    )
    assert inspection_audit_signed["ticket"]["state"] == "director_review", inspection_audit_signed  # type: ignore[index]
    inspection_done = post_json(
        base_url,
        "/api/tickets/PGU-117/actions/mark_done",
        {"commit_hash": commit_hash},
        caller="director",
    )
    assert inspection_done["ticket"]["state"] == "done", inspection_done  # type: ignore[index]

    empty_inspector_kick = post_json(
        base_url,
        "/api/tickets/PGU-119/actions/inspector_kick_back",
        {"recommendations": ""},
        caller="inspector",
        expect=400,
    )
    assert "non-empty" in str(empty_inspector_kick), empty_inspector_kick
    inspector_kicked = post_json(
        base_url,
        "/api/tickets/PGU-119/actions/inspector_kick_back",
        {"recommendations": "Frame has visible banding."},
        caller="inspector",
    )
    assert inspector_kicked["ticket"]["state"] == "in_progress", inspector_kicked  # type: ignore[index]
    assert inspector_kicked["ticket"]["inspector_signoff"] is False, inspector_kicked  # type: ignore[index]
    assert inspector_kicked["ticket"]["comments"][-1]["who"] == "inspector", inspector_kicked  # type: ignore[index]
    assert "Frame has visible banding." in inspector_kicked["ticket"]["comments"][-1]["text"], inspector_kicked  # type: ignore[index]
    inspector_restarted = post_json(base_url, "/api/tickets/PGU-119/actions/start_work", {}, caller="ops")
    assert inspector_restarted["ticket"]["state"] == "in_progress", inspector_restarted  # type: ignore[index]
    inspector_resubmitted = post_json(
        base_url,
        "/api/tickets/PGU-119/actions/submit_to_audit",
        {"commit_hash": commit_hash},
        caller="ops",
    )
    assert inspector_resubmitted["ticket"]["state"] == "inspection", inspector_resubmitted  # type: ignore[index]
    assert inspector_resubmitted["ticket"]["inspector_signoff"] is False, inspector_resubmitted  # type: ignore[index]
    inspector_resubmitted_signed = post_json(base_url, "/api/tickets/PGU-119/actions/inspector_sign_off", {}, caller="inspector")
    assert inspector_resubmitted_signed["ticket"]["state"] == "audit", inspector_resubmitted_signed  # type: ignore[index]
    inspector_resubmitted_audit = post_json(
        base_url,
        "/api/tickets/PGU-119/actions/audit_sign_off",
        {"text": "Inspector kickback path verified."},
        caller="audit",
    )
    assert inspector_resubmitted_audit["ticket"]["state"] == "director_review", inspector_resubmitted_audit  # type: ignore[index]
    inspector_resubmitted_done = post_json(
        base_url,
        "/api/tickets/PGU-119/actions/mark_done",
        {"commit_hash": commit_hash},
        caller="director",
    )
    assert inspector_resubmitted_done["ticket"]["state"] == "done", inspector_resubmitted_done  # type: ignore[index]

    audit_signoff_missing_comment = post_json(
        base_url,
        "/api/tickets/PGU-102/actions/audit_sign_off",
        {},
        caller="audit",
        expect=400,
    )
    assert "audit_sign_off requires a non-empty comment" in str(audit_signoff_missing_comment), audit_signoff_missing_comment
    audit_signoff_empty_comment = post_json(
        base_url,
        "/api/tickets/PGU-102/actions/audit_sign_off",
        {"text": "   "},
        caller="audit",
        expect=400,
    )
    assert "audit_sign_off requires a non-empty comment" in str(audit_signoff_empty_comment), audit_signoff_empty_comment
    audit_signed = post_json(base_url, "/api/tickets/PGU-102/actions/audit_sign_off", {"text": "Audit verified."}, caller="audit")
    assert audit_signed["ticket"]["state"] == "director_review", audit_signed  # type: ignore[index]
    assert audit_signed["ticket"]["audit_signoff"] is True, audit_signed  # type: ignore[index]
    assert audit_signed["ticket"]["comments"][-1]["text"] == "Audit verified.", audit_signed  # type: ignore[index]
    audit_kicked = post_json(
        base_url,
        "/api/tickets/PGU-103/actions/audit_kick_back",
        {"reason": "Needs another pass."},
        caller="audit",
    )
    assert audit_kicked["ticket"]["state"] == "in_progress", audit_kicked  # type: ignore[index]
    assert audit_kicked["ticket"]["assignee"] == "ops", audit_kicked  # type: ignore[index]
    assert audit_kicked["ticket"]["comments"][-1]["who"] == "audit", audit_kicked  # type: ignore[index]

    eric_signed = post_json(base_url, "/api/tickets/PGU-104/actions/eric_sign_off", {"text": "Eric approves."}, caller="eric")
    assert eric_signed["ticket"]["state"] == "director_review", eric_signed  # type: ignore[index]
    assert eric_signed["ticket"]["comments"][-1]["text"] == "Eric approves.", eric_signed  # type: ignore[index]
    eric_reopened = post_json(
        base_url,
        "/api/tickets/PGU-105/actions/eric_reopen",
        {"reason": "Needs design revision."},
        caller="eric",
    )
    assert eric_reopened["ticket"]["state"] in {"analysis", "in_progress"}, eric_reopened  # type: ignore[index]
    assert eric_reopened["ticket"]["comments"][-1]["who"] == "eric", eric_reopened  # type: ignore[index]

    done = post_json(base_url, "/api/tickets/PGU-106/actions/mark_done", {"commit_hash": commit_hash}, caller="director")
    assert done["ticket"]["state"] == "done", done  # type: ignore[index]
    non_exempt_empty_done = post_json(
        base_url,
        "/api/tickets/PGU-123/actions/mark_done",
        {},
        caller="director",
        expect=400,
    )
    assert "commit_hash must be a 7-40 character hex commit" in str(non_exempt_empty_done), non_exempt_empty_done
    done_exempt_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-122/actions/edit_fields",
        {"commit_exempt": True},
        caller="ops",
        expect=403,
    )
    assert "commit_exempt can only be edited by director" in str(done_exempt_forbidden), done_exempt_forbidden
    done_exempt_set = post_json(
        base_url,
        "/api/tickets/PGU-122/actions/edit_fields",
        {"commit_exempt": True},
        caller="director",
    )
    assert done_exempt_set["ticket"]["commit_exempt"] is True, done_exempt_set  # type: ignore[index]
    exempt_done = post_json(base_url, "/api/tickets/PGU-122/actions/mark_done", {}, caller="director")
    assert exempt_done["ticket"]["state"] == "done", exempt_done  # type: ignore[index]
    assert exempt_done["ticket"]["commit_hash"] == "", exempt_done  # type: ignore[index]
    assert exempt_done["ticket"]["commit_exempt"] is True, exempt_done  # type: ignore[index]
    deferred = post_json(base_url, "/api/tickets/PGU-107/actions/defer", {}, caller="director")
    assert deferred["ticket"]["state"] == "backlog", deferred  # type: ignore[index]
    cancelled = post_json(base_url, "/api/tickets/PGU-108/actions/cancel", {"reason": "No longer needed."}, caller="director")
    assert cancelled["ticket"]["state"] == "cancelled", cancelled  # type: ignore[index]
    assert cancelled["ticket"]["comments"][-1]["who"] == "director", cancelled  # type: ignore[index]
    backlog_cancelled = post_json(
        base_url,
        "/api/tickets/PGU-116/actions/cancel",
        {"reason": "No longer needed from backlog."},
        caller="director",
    )
    assert backlog_cancelled["ticket"]["state"] == "cancelled", backlog_cancelled  # type: ignore[index]
    assert backlog_cancelled["ticket"]["comments"][-1]["who"] == "director", backlog_cancelled  # type: ignore[index]

    manual = post_json(
        base_url,
        "/api/tickets/PGU-109/actions/set_manually_controlled",
        {"manually_controlled": True},
        caller="director",
    )
    assert manual["ticket"]["manually_controlled"] is True, manual  # type: ignore[index]
    blockers = post_json(
        base_url,
        "/api/tickets/PGU-111/actions/set_blockers",
        {"blocked_by": ["PGU-110"], "blocked_reason": "Waiting on blocker."},
        caller="director",
    )
    assert blockers["ticket"]["blocked_by"] == ["PGU-110"], blockers  # type: ignore[index]
    assert blockers["ticket"]["blocked_reason"] == "Waiting on blocker.", blockers  # type: ignore[index]
    terminal_blocker = post_json(
        base_url,
        "/api/tickets/PGU-111/actions/set_blockers",
        {"blocked_by": ["PGU-106"], "blocked_reason": "Waiting on a done ticket."},
        caller="director",
        expect=400,
    )
    assert "terminal tickets cannot block other tickets: PGU-106 is done" in str(terminal_blocker), terminal_blocker
    blocked_reason_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-111/actions/edit_fields",
        {"blocked_reason": ""},
        caller="main",
        expect=400,
    )
    assert "edit_fields cannot update: blocked_reason" in str(blocked_reason_forbidden), blocked_reason_forbidden
    assert get_ticket(base_url, "PGU-111")["blocked_reason"] == "Waiting on blocker."
    comment = post_json(base_url, "/api/tickets/PGU-112/actions/add_comment", {"text": "Pane note."}, caller="app")
    assert comment["ticket"]["comments"][-1]["who"] == "app", comment  # type: ignore[index]
    assert comment["ticket"]["comments"][-1]["urgent"] is False, comment  # type: ignore[index]
    assert get_ticket(base_url, "PGU-112")["comments"][-1]["text"] == "Pane note."
    urgent_comment = post_json(base_url, "/api/tickets/PGU-112/actions/add_comment", {"text": "Urgent pane note.", "urgent": True}, caller="app")
    assert urgent_comment["ticket"]["comments"][-1]["urgent"] is True, urgent_comment  # type: ignore[index]
    edited = post_json(base_url, "/api/tickets/PGU-112/actions/edit_fields", {"title": "Edited through action"}, caller="app")
    assert edited["ticket"]["title"] == "Edited through action", edited  # type: ignore[index]
    invalid_edit = post_json(base_url, "/api/tickets/PGU-112/actions/edit_fields", {"state": "done"}, caller="app", expect=400)
    assert "edit_fields cannot update: state" in str(invalid_edit), invalid_edit
    frame_attachment = frames / "api-frame-ref.png"
    Image.new("RGB", (2, 2), (40, 80, 120)).save(frame_attachment)
    attachment_edit = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"screenshots": [str(frame_attachment)]},
        caller="app",
    )
    stored_path = Path(attachment_edit["ticket"]["screenshots"][0])  # type: ignore[index]
    assert stored_path != frame_attachment.resolve(), attachment_edit
    assert assets.resolve() in stored_path.parents, attachment_edit
    assert attachment_edit["ticket"]["screenshots_info"][0]["available"] is True, attachment_edit  # type: ignore[index]
    frame_attachment.unlink()
    persisted_attachment = get_ticket(base_url, "PGU-112")
    assert persisted_attachment["screenshots"] == [str(stored_path)], persisted_attachment
    assert persisted_attachment["screenshots_info"][0]["available"] is True, persisted_attachment
    merge_forbidden = post_json(base_url, "/api/tickets/PGU-113/actions/merge", {"target_id": "PGU-114"}, caller="app", expect=403)
    assert "app cannot call merge" in str(merge_forbidden), merge_forbidden
    merged = post_json(base_url, "/api/tickets/PGU-113/actions/merge", {"target_id": "PGU-114"}, caller="director")
    assert merged["target"]["id"] == "PGU-114", merged  # type: ignore[index]
    merged_source = get_ticket(base_url, "PGU-113")
    assert merged_source["state"] == "done", merged_source
    assert merged_source["commit_exempt"] is True, merged_source
    assert "regression" in merged_source, merged_source


def exercise_postgres_backend(commit_hash: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-write-api-postgres.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        dbname = "pgu_write_api_test"
        admin_conn = conninfo(socket_dir, port, dbname)
        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            assert psql(
                admin_conn,
                """
SELECT column_default
FROM information_schema.columns
WHERE table_schema = 'ticket_board'
  AND table_name = 'tickets'
  AND column_name = 'regression';
""",
            ) == "false"
            create_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            seed_fixtures(lambda ticket_id, **kwargs: seed_postgres_ticket(admin_conn, ticket_id, **kwargs), commit_hash)
            app = TicketBoardApp(
                frames,
                assets,
                database_url=conninfo(socket_dir, port, dbname, SERVICE_ROLE),
            )
            server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
            global TEST_WRITE_TOKEN
            TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                exercise_write_api(f"http://127.0.0.1:{server.server_port}", commit_hash, frames=frames, assets=assets)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def main() -> int:
    commit_hash = main_commit()
    assert_frontend_update_calls_send_caller_role()
    assert_frontend_writes_send_auth_token()
    exercise_postgres_backend(commit_hash)
    print("ticket_board_write_api_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
