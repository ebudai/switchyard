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
from scripts.ticket_board.server import (
    CALLER_ROLE_HEADER,
    LEGACY_CALLER_ROLE_HEADER,
    LEGACY_WRITE_TOKEN_HEADER,
    WRITE_TOKEN_HEADER,
    CallerRegistry,
    TicketBoardEventHub,
    TicketBoardServer,
    TicketBoardUnixServer,
)
from scripts.ticket_board.write_client import TicketBoardWriteClient, TicketBoardWriteError


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "user", "ops", "app", "audit", "inspector", "perf", "research", "main"]
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


def sql_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def create_roles(conn: str) -> None:
    psql(conn, "\n".join(f"CREATE ROLE {sql_ident(role)} LOGIN;" for role in PANE_ROLES))


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
    needs_audit: bool = True,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_user_signoff: bool = False,
    user_signoff: bool = False,
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
        "needs_audit": needs_audit,
        "needs_inspection": needs_inspection,
        "inspector_signoff": inspector_signoff,
        "needs_user_signoff": needs_user_signoff,
        "user_signoff": user_signoff,
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
    needs_audit: bool = True,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_user_signoff: bool = False,
    user_signoff: bool = False,
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
    audit_signoff, needs_audit, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, regression, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{implementation}',
    {str(audit_signoff).lower()}, {str(needs_audit).lower()}, {str(needs_inspection).lower()}, {str(inspector_signoff).lower()}, {str(needs_user_signoff).lower()},
    {str(user_signoff).lower()}, '{commit_hash}', {str(commit_exempt).lower()}, {str(regression).lower()}, {str(parked).lower()},
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


def upload_png(base_url: str, query: str = "", *, expect: int = 201) -> dict[str, object] | str:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (220, 90, 40)).save(buffer, format="PNG")
    headers = {"Content-Type": "image/png"}
    if TEST_WRITE_TOKEN:
        headers[WRITE_TOKEN_HEADER] = TEST_WRITE_TOKEN
    suffix = f"?{query}" if query else ""
    request = urllib.request.Request(
        base_url + "/api/upload" + suffix,
        data=buffer.getvalue(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode("utf-8")
            assert response.status == expect, (response.status, body)
            return json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == expect, (exc.code, body)
        return body


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
    assert "X-Ticket-Board-Write-Token" in source
    assert "X-Ticket-Board-Caller-Role" in source
    assert "window.PGU_TICKET_BOARD_WRITE_TOKEN" in source


def seed_fixtures(seed_ticket: object, commit_hash: str) -> None:
    seed_ticket("PGU-100", title="Route", state="analysis")
    seed_ticket("PGU-101", title="Start submit", state="analysis", assignee="ops", implementation="Ready.")
    seed_ticket("PGU-102", title="Audit signoff", state="audit", assignee="audit", implementation="Done.")
    seed_ticket("PGU-103", title="Audit kickback", state="audit", assignee="audit", implementation="Done.")
    seed_ticket(
        "PGU-104",
        title="User signoff",
        state="user_review",
        assignee="user",
        implementation="Done.",
        audit_signoff=True,
        needs_user_signoff=True,
    )
    seed_ticket(
        "PGU-105",
        title="User reopen",
        state="user_review",
        assignee="user",
        implementation="Done.",
        audit_signoff=True,
        needs_user_signoff=True,
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
        "PGU-133",
        title="Inspection kickback with comment",
        state="inspection",
        assignee="ops",
        implementation="Rendered.",
        needs_inspection=True,
        inspector_signoff=True,
    )
    seed_ticket("PGU-134", title="Audit kickback empty", state="audit", assignee="audit", implementation="Done.")
    seed_ticket(
        "PGU-135",
        title="User reopen empty",
        state="user_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        needs_user_signoff=True,
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
        title="User review to inspection",
        state="user_review",
        assignee="director",
        implementation="Rendered.",
        audit_signoff=True,
        inspector_signoff=True,
        needs_user_signoff=True,
        needs_inspection=True,
        commit_hash=commit_hash,
    )
    seed_ticket(
        "PGU-125",
        title="User review inspection guard",
        state="user_review",
        assignee="director",
        implementation="Rendered.",
        audit_signoff=True,
        needs_user_signoff=True,
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
    seed_ticket("PGU-140", title="Draft release", state="draft", assignee="unassigned")
    seed_ticket(
        "PGU-115",
        title="Director kickback to implementation",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )
    seed_ticket(
        "PGU-136",
        title="Director route coerces stage owner",
        state="audit",
        assignee="audit",
        implementation="Done.",
        audit_signoff=True,
        needs_user_signoff=True,
        commit_hash=commit_hash,
    )
    seed_ticket(
        "PGU-141",
        title="Route to user assignee",
        state="analysis",
        assignee="app",
        implementation="Ready for UAT.",
        needs_user_signoff=True,
    )
    seed_ticket(
        "PGU-142",
        title="Force move to user assignee",
        state="analysis",
        assignee="unassigned",
        implementation="Ready for override.",
        needs_user_signoff=True,
    )
    seed_ticket(
        "PGU-143",
        title="User information request",
        state="analysis",
        assignee="director",
        implementation="Need external decision.",
    )
    seed_ticket(
        "PGU-144",
        title="Do not bypass UAT pipeline",
        state="analysis",
        assignee="director",
        implementation="Unfinished work.",
        needs_user_signoff=True,
    )


def exercise_write_api(base_url: str, commit_hash: str, *, frames: Path, assets: Path, admin_conn: str) -> None:
    assert_served_html_contains_write_token(base_url)
    unauth_spoof = post_json(
        base_url,
        "/api/tickets/PGU-100/actions/route",
        {"state": "backlog", "assignee": "ops"},
        caller="director",
        token=None,
        expect=403,
    )
    assert "missing or invalid X-Ticket-Board-Write-Token" in str(unauth_spoof), unauth_spoof
    wrong_token = post_json(
        base_url,
        "/api/tickets/PGU-100/actions/route",
        {"state": "backlog", "assignee": "ops"},
        caller="director",
        token="wrong-token",
        expect=403,
    )
    assert "missing or invalid X-Ticket-Board-Write-Token" in str(wrong_token), wrong_token
    missing = post_json(base_url, "/api/tickets/actions/create_ticket", {"title": "No caller", "body": ""}, expect=400)
    assert "missing X-Ticket-Board-Caller-Role" in str(missing), missing
    legacy_headers = {"Content-Type": "application/json", LEGACY_CALLER_ROLE_HEADER: "director", LEGACY_WRITE_TOKEN_HEADER: TEST_WRITE_TOKEN}
    legacy_request = urllib.request.Request(
        base_url + "/api/tickets/actions/create_ticket",
        data=json.dumps({"title": "Legacy headers accepted", "body": ""}).encode("utf-8"),
        headers=legacy_headers,
        method="POST",
    )
    with urllib.request.urlopen(legacy_request, timeout=5) as response:
        assert response.status == 201
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
        {"reason": "User should not cancel."},
        caller="user",
        expect=403,
    )
    assert "user cannot call cancel" in str(eric_director_only), eric_director_only
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
        caller="user",
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
    assert created["needs_audit"] is True, created  # type: ignore[index]

    parented_director_create_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {
            "title": "Director parented analysis create",
            "body": "Director-created child work.",
            "state": "analysis",
            "parent_id": source_id,
            "assignee": "ops",
        },
        caller="director",
        expect=201,
    )
    parented_director_create = parented_director_create_payload["ticket"]  # type: ignore[index]
    assert parented_director_create["state"] == "analysis", parented_director_create  # type: ignore[index]
    assert parented_director_create["assignee"] == "ops", parented_director_create  # type: ignore[index]
    assert parented_director_create["parent_id"] == source_id, parented_director_create  # type: ignore[index]
    assert parented_director_create["comments"] == [], parented_director_create  # type: ignore[index]

    user_default_audit_created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "User default-audit create", "body": "User keeps audit enabled.", "needs_audit": True},
        caller="user",
        expect=201,
    )
    user_default_audit_created = user_default_audit_created_payload["ticket"]  # type: ignore[index]
    assert user_default_audit_created["needs_audit"] is True, user_default_audit_created  # type: ignore[index]

    no_audit_created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API no-audit create", "body": "Director partitions this away from audit.", "needs_audit": False},
        caller="director",
        expect=201,
    )
    no_audit_created = no_audit_created_payload["ticket"]  # type: ignore[index]
    assert no_audit_created["needs_audit"] is False, no_audit_created  # type: ignore[index]
    assert psql(
        admin_conn,
        f"SELECT (source_json->>'needs_audit') FROM ticket_board.tickets WHERE id = '{no_audit_created['id']}';",
    ) == "false"

    no_audit_create_forbidden = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Implementer cannot skip audit", "body": "", "needs_audit": False},
        caller="user",
        expect=403,
    )
    assert "needs_audit can only be set to false by director" in str(no_audit_create_forbidden), no_audit_create_forbidden

    backlog_created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API backlog create", "body": "Deferred work.", "initial_state": "backlog", "assignee": "research"},
        caller="director",
        expect=201,
    )
    backlog_created = backlog_created_payload["ticket"]  # type: ignore[index]
    assert backlog_created["state"] == "backlog", backlog_created  # type: ignore[index]
    assert backlog_created["assignee"] == "research", backlog_created  # type: ignore[index]

    draft_assignee_rejected = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API draft create", "body": "Staged work.", "initial_state": "draft", "assignee": "ops"},
        caller="director",
        expect=400,
    )
    assert "draft tickets cannot be created with an assignee" in str(draft_assignee_rejected), draft_assignee_rejected

    draft_created_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API draft create", "body": "Staged work.", "initial_state": "draft"},
        caller="director",
        expect=201,
    )
    draft_created = draft_created_payload["ticket"]  # type: ignore[index]
    draft_id = str(draft_created["id"])  # type: ignore[index]
    assert draft_created["state"] == "draft", draft_created  # type: ignore[index]
    assert draft_created["assignee"] == "unassigned", draft_created  # type: ignore[index]
    assert psql(admin_conn, f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{draft_id}';") == "0"

    draft_route_rejected = post_json(
        base_url,
        "/api/tickets/PGU-140/actions/route",
        {"state": "analysis", "assignee": "unassigned"},
        caller="director",
        expect=400,
    )
    assert "draft tickets must be released with release_draft" in str(draft_route_rejected), draft_route_rejected
    draft_release_forbidden = post_json(
        base_url,
        f"/api/tickets/{draft_id}/actions/release_draft",
        {},
        caller="ops",
        expect=403,
    )
    assert "ops cannot call release_draft" in str(draft_release_forbidden), draft_release_forbidden
    draft_released = post_json(base_url, f"/api/tickets/{draft_id}/actions/release_draft", {}, caller="user")
    assert draft_released["ticket"]["state"] == "analysis", draft_released  # type: ignore[index]
    assert draft_released["ticket"]["assignee"] == "director", draft_released  # type: ignore[index]

    draft_cancel_payload = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "API draft cancel", "body": "Discard.", "initial_state": "draft"},
        caller="director",
        expect=201,
    )
    draft_cancel_id = str(draft_cancel_payload["ticket"]["id"])  # type: ignore[index]
    draft_cancelled = post_json(
        base_url,
        f"/api/tickets/{draft_cancel_id}/actions/cancel",
        {"reason": "Abandon draft."},
        caller="director",
    )
    assert draft_cancelled["ticket"]["state"] == "cancelled", draft_cancelled  # type: ignore[index]

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
    target_uploaded = upload_png(base_url, "set=target")
    assert str(target_uploaded["image"]["name"]).startswith("target__upload_"), target_uploaded  # type: ignore[index]
    attempt_uploaded = upload_png(base_url, "set=attempt&attempt=7&label=UAT%20Rework")
    assert str(attempt_uploaded["image"]["name"]).startswith("attempt-007-uat-rework__upload_"), attempt_uploaded  # type: ignore[index]
    named_upload = upload_png(base_url, "filename=Spiral%20Arm%20Draft.PNG")
    assert str(named_upload["image"]["name"]) == "spiral-arm-draft.png", named_upload  # type: ignore[index]
    named_upload_collision = upload_png(base_url, "filename=Spiral%20Arm%20Draft.PNG")
    assert str(named_upload_collision["image"]["name"]) == "spiral-arm-draft-2.png", named_upload_collision  # type: ignore[index]
    feedback_without_number = upload_png(base_url, "set=feedback", expect=400)
    assert "feedback upload set requires a feedback number" in str(feedback_without_number), feedback_without_number
    feedback_uploaded = upload_png(base_url, "set=feedback&attempt=2&filename=User%20Markup.PNG")
    assert str(feedback_uploaded["image"]["name"]) == "feedback-002__user-markup.png", feedback_uploaded  # type: ignore[index]
    labeled_feedback_uploaded = upload_png(base_url, "set=feedback&attempt=3&label=User%20Markup&filename=zoom%20notes.PNG")
    assert str(labeled_feedback_uploaded["image"]["name"]) == "feedback-003-user-markup__zoom-notes.png", labeled_feedback_uploaded  # type: ignore[index]
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

    director_file_bug_forbidden = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Director should use create_ticket", "body": "No direct file_bug.", "source_ticket_id": source_id},
        caller="director",
        expect=403,
    )
    assert "director cannot call file_bug" in str(director_file_bug_forbidden), director_file_bug_forbidden

    filed_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "API file bug", "body": "Linked bug.", "source_ticket_id": source_id},
        caller="ops",
        expect=201,
    )
    filed = filed_payload["ticket"]  # type: ignore[index]
    assert filed["parent_id"] == source_id, filed  # type: ignore[index]
    assert filed["needs_audit"] is True, filed  # type: ignore[index]
    no_audit_file_bug_forbidden = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "Ops cannot skip audit on filed bug", "body": "", "source_ticket_id": source_id, "needs_audit": False},
        caller="ops",
        expect=403,
    )
    assert "needs_audit can only be set to false by director" in str(no_audit_file_bug_forbidden), no_audit_file_bug_forbidden
    blocked_file_bug_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {
            "title": "API blocked file bug",
            "body": "Linked blocked bug.",
            "source_ticket_id": source_id,
            "assignee": "ops",
            "blocked_by": [source_id],
            "blocked_reason": "Waiting on source fix.",
        },
        caller="ops",
        expect=201,
    )
    blocked_file_bug = blocked_file_bug_payload["ticket"]  # type: ignore[index]
    assert blocked_file_bug["parent_id"] == source_id, blocked_file_bug  # type: ignore[index]
    assert blocked_file_bug["state"] == "analysis", blocked_file_bug  # type: ignore[index]
    assert blocked_file_bug["assignee"] == "ops", blocked_file_bug  # type: ignore[index]
    assert blocked_file_bug["blocked_by"] == [source_id], blocked_file_bug  # type: ignore[index]
    assert blocked_file_bug["blocked_reason"] == "Waiting on source fix.", blocked_file_bug  # type: ignore[index]
    assert blocked_file_bug["comments"][0]["who"] == "ops", blocked_file_bug  # type: ignore[index]
    assert psql(
        admin_conn,
        f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{blocked_file_bug['id']}';",
    ) == "0"
    assigned_bug_roles = ("ops", "main", "app", "perf", "research", "audit")
    for role in assigned_bug_roles:
        assigned_file_bug_payload = post_json(
            base_url,
            "/api/tickets/actions/file_bug",
            {
                "title": f"{role} assigned file bug",
                "body": f"{role} found a bug.",
                "source_ticket_id": source_id,
                "assignee": role,
            },
            caller=role,
            expect=201,
        )
        assigned_file_bug = assigned_file_bug_payload["ticket"]  # type: ignore[index]
        assert assigned_file_bug["parent_id"] == source_id, assigned_file_bug  # type: ignore[index]
        assert assigned_file_bug["state"] == "analysis", assigned_file_bug  # type: ignore[index]
        assert assigned_file_bug["assignee"] == role, assigned_file_bug  # type: ignore[index]
        assert assigned_file_bug["comments"][0]["who"] == role, assigned_file_bug  # type: ignore[index]
        assert assigned_file_bug["comments"][0]["text"] == f"Filed bug against {source_id}.", assigned_file_bug  # type: ignore[index]
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
    assert "in_progress tickets require an implementation-stage owner assignee" in str(invalid_implementation_assignee), invalid_implementation_assignee
    incoherent_audit_route = post_json(
        base_url,
        "/api/tickets/PGU-136/actions/route",
        {"state": "dat", "assignee": "audit"},
        caller="director",
    )
    assert incoherent_audit_route["ticket"]["state"] == "dat", incoherent_audit_route  # type: ignore[index]
    assert incoherent_audit_route["ticket"]["assignee"] == "director", incoherent_audit_route  # type: ignore[index]

    uat_started = post_json(base_url, "/api/tickets/PGU-141/actions/start_work", {}, caller="app")
    assert uat_started["ticket"]["state"] == "in_progress", uat_started  # type: ignore[index]
    assert uat_started["ticket"]["assignee"] == "app", uat_started  # type: ignore[index]
    uat_submitted = post_json(
        base_url,
        "/api/tickets/PGU-141/actions/submit_to_audit",
        {"commit_hash": commit_hash},
        caller="app",
    )
    assert uat_submitted["ticket"]["state"] == "audit", uat_submitted  # type: ignore[index]
    assert psql(admin_conn, "SELECT coalesce(ticket_board.ticket_current_reserved_ticket('app'), '<none>');") == "PGU-141"
    uat_audit = post_json(
        base_url,
        "/api/tickets/PGU-141/actions/audit_sign_off",
        {"text": "Audit verified for UAT."},
        caller="audit",
    )
    assert uat_audit["ticket"]["state"] == "dat", uat_audit  # type: ignore[index]
    assert uat_audit["ticket"]["assignee"] == "director", uat_audit  # type: ignore[index]
    uat_routed = post_json(
        base_url,
        "/api/tickets/PGU-141/actions/route",
        {"state": "user_review", "assignee": "user"},
        caller="director",
    )
    assert uat_routed["ticket"]["state"] == "user_review", uat_routed  # type: ignore[index]
    assert uat_routed["ticket"]["assignee"] == "user", uat_routed  # type: ignore[index]
    uat_reservation = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'app_reserved_ticket', coalesce(ticket_board.ticket_current_reserved_ticket('app'), '<none>'),
    'reserved_from_ticket', coalesce(ticket_board.ticket_reserved_implementer(id, state, assignee), '<none>')
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-141';
""",
        )
    )
    assert uat_reservation == {
        "app_reserved_ticket": "<none>",
        "reserved_from_ticket": "<none>",
    }, uat_reservation

    user_info_request = post_json(
        base_url,
        "/api/tickets/PGU-143/actions/route",
        {"state": "user_review", "assignee": "user"},
        caller="director",
    )
    assert user_info_request["ticket"]["state"] == "user_review", user_info_request  # type: ignore[index]
    assert user_info_request["ticket"]["assignee"] == "user", user_info_request  # type: ignore[index]
    assert user_info_request["ticket"]["needs_user_signoff"] is False, user_info_request  # type: ignore[index]
    assert user_info_request["ticket"]["user_signoff"] is False, user_info_request  # type: ignore[index]
    user_info_signoff = post_json(
        base_url,
        "/api/tickets/PGU-143/actions/user_sign_off",
        {"text": "Looks approved."},
        caller="user",
        expect=400,
    )
    assert "user_sign_off requires needs_user_signoff=true" in str(user_info_signoff), user_info_signoff
    user_info_after_signoff = get_ticket(base_url, "PGU-143")
    assert user_info_after_signoff["state"] == "user_review", user_info_after_signoff
    assert user_info_after_signoff["user_signoff"] is False, user_info_after_signoff
    assert user_info_after_signoff["comments"] == [], user_info_after_signoff
    uat_bypass = post_json(
        base_url,
        "/api/tickets/PGU-144/actions/route",
        {"state": "user_review", "assignee": "user"},
        caller="director",
        expect=400,
    )
    assert "analysis -> user_review is only for user information requests with needs_user_signoff=false" in str(uat_bypass), uat_bypass

    force_moved_user = post_json(
        base_url,
        "/api/tickets/PGU-142/actions/force_move",
        {"state": "user_review", "assignee": "user", "suppress_notification": True},
        caller="director",
    )
    assert force_moved_user["ticket"]["state"] == "user_review", force_moved_user  # type: ignore[index]
    assert force_moved_user["ticket"]["assignee"] == "user", force_moved_user  # type: ignore[index]

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
    assert submitted["ticket"]["assignee"] == "audit", submitted  # type: ignore[index]
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
    assert exempt_requested["ticket"]["assignee"] == "director", exempt_requested  # type: ignore[index]
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
    assert exempt_submitted["ticket"]["assignee"] == "audit", exempt_submitted  # type: ignore[index]
    assert exempt_submitted["ticket"]["commit_hash"] == "", exempt_submitted  # type: ignore[index]
    assert exempt_submitted["ticket"]["commit_exempt"] is True, exempt_submitted  # type: ignore[index]

    eric_inspection_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "User cannot require inspection", "body": "", "needs_inspection": True},
        caller="user",
        expect=403,
    )
    assert "needs_inspection can only be set by director" in str(eric_inspection_create), eric_inspection_create
    eric_commit_exempt_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "User cannot exempt commits", "body": "", "commit_exempt": True},
        caller="user",
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
    director_uat_create = post_json(
        base_url,
        "/api/tickets/actions/create_ticket",
        {"title": "Director requires UAT", "body": "", "needs_user_signoff": True},
        caller="director",
        expect=201,
    )
    assert director_uat_create["ticket"]["needs_user_signoff"] is True, director_uat_create  # type: ignore[index]
    assert get_ticket(base_url, str(director_uat_create["ticket"]["id"]))["needs_user_signoff"] is True
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
    audit_field_forbidden = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"needs_audit": False},
        caller="app",
        expect=403,
    )
    assert "needs_audit can only be set to false by director" in str(audit_field_forbidden), audit_field_forbidden
    audit_field_director = post_json(
        base_url,
        "/api/tickets/PGU-112/actions/edit_fields",
        {"needs_audit": False},
        caller="director",
    )
    assert audit_field_director["ticket"]["needs_audit"] is False, audit_field_director  # type: ignore[index]
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

    user_review_to_inspection = post_json(
        base_url,
        "/api/tickets/PGU-124/actions/route",
        {"state": "inspection", "assignee": "inspector"},
        caller="director",
    )
    assert user_review_to_inspection["ticket"]["state"] == "inspection", user_review_to_inspection  # type: ignore[index]
    assert user_review_to_inspection["ticket"]["assignee"] == "inspector", user_review_to_inspection  # type: ignore[index]
    assert user_review_to_inspection["ticket"]["needs_inspection"] is True, user_review_to_inspection  # type: ignore[index]
    assert user_review_to_inspection["ticket"]["audit_signoff"] is False, user_review_to_inspection  # type: ignore[index]
    assert user_review_to_inspection["ticket"]["inspector_signoff"] is False, user_review_to_inspection  # type: ignore[index]
    assert user_review_to_inspection["ticket"]["commit_hash"] == commit_hash, user_review_to_inspection  # type: ignore[index]
    user_review_to_inspection_guard = post_json(
        base_url,
        "/api/tickets/PGU-125/actions/route",
        {"state": "inspection", "assignee": "inspector"},
        caller="director",
        expect=400,
    )
    assert "needs_inspection must be true" in str(user_review_to_inspection_guard), user_review_to_inspection_guard

    inspect_started = post_json(base_url, "/api/tickets/PGU-117/actions/start_work", {}, caller="ops")
    assert inspect_started["ticket"]["state"] == "in_progress", inspect_started  # type: ignore[index]
    inspect_submitted = post_json(
        base_url,
        "/api/tickets/PGU-117/actions/submit_to_audit",
        {"commit_hash": commit_hash},
        caller="ops",
    )
    assert inspect_submitted["ticket"]["state"] == "inspection", inspect_submitted  # type: ignore[index]
    assert inspect_submitted["ticket"]["assignee"] == "inspector", inspect_submitted  # type: ignore[index]
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
    assert inspection_signed["ticket"]["assignee"] == "audit", inspection_signed  # type: ignore[index]
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
    )
    assert empty_inspector_kick["ticket"]["state"] == "in_progress", empty_inspector_kick  # type: ignore[index]
    assert empty_inspector_kick["ticket"]["inspector_signoff"] is False, empty_inspector_kick  # type: ignore[index]
    assert empty_inspector_kick["ticket"]["comments"] == [], empty_inspector_kick  # type: ignore[index]
    post_json(
        base_url,
        "/api/tickets/PGU-119/actions/force_move",
        {"state": "done", "assignee": "director", "suppress_notification": True},
        caller="director",
    )
    inspector_kicked = post_json(
        base_url,
        "/api/tickets/PGU-133/actions/inspector_kick_back",
        {"recommendations": "Frame has visible banding."},
        caller="inspector",
    )
    assert inspector_kicked["ticket"]["state"] == "in_progress", inspector_kicked  # type: ignore[index]
    assert inspector_kicked["ticket"]["assignee"] == "ops", inspector_kicked  # type: ignore[index]
    assert inspector_kicked["ticket"]["inspector_signoff"] is False, inspector_kicked  # type: ignore[index]
    assert inspector_kicked["ticket"]["comments"][-1]["who"] == "inspector", inspector_kicked  # type: ignore[index]
    assert "Frame has visible banding." in inspector_kicked["ticket"]["comments"][-1]["text"], inspector_kicked  # type: ignore[index]
    inspector_restarted = post_json(base_url, "/api/tickets/PGU-133/actions/start_work", {}, caller="ops")
    assert inspector_restarted["ticket"]["state"] == "in_progress", inspector_restarted  # type: ignore[index]
    inspector_resubmitted = post_json(
        base_url,
        "/api/tickets/PGU-133/actions/submit_to_audit",
        {"commit_hash": commit_hash},
        caller="ops",
    )
    assert inspector_resubmitted["ticket"]["state"] == "inspection", inspector_resubmitted  # type: ignore[index]
    assert inspector_resubmitted["ticket"]["assignee"] == "inspector", inspector_resubmitted  # type: ignore[index]
    assert inspector_resubmitted["ticket"]["inspector_signoff"] is False, inspector_resubmitted  # type: ignore[index]
    inspector_resubmitted_signed = post_json(base_url, "/api/tickets/PGU-133/actions/inspector_sign_off", {}, caller="inspector")
    assert inspector_resubmitted_signed["ticket"]["state"] == "audit", inspector_resubmitted_signed  # type: ignore[index]
    assert inspector_resubmitted_signed["ticket"]["assignee"] == "audit", inspector_resubmitted_signed  # type: ignore[index]
    inspector_resubmitted_audit = post_json(
        base_url,
        "/api/tickets/PGU-133/actions/audit_sign_off",
        {"text": "Inspector kickback path verified."},
        caller="audit",
    )
    assert inspector_resubmitted_audit["ticket"]["state"] == "director_review", inspector_resubmitted_audit  # type: ignore[index]
    inspector_resubmitted_done = post_json(
        base_url,
        "/api/tickets/PGU-133/actions/mark_done",
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
    assert audit_signed["ticket"]["assignee"] == "director", audit_signed  # type: ignore[index]
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
    post_json(
        base_url,
        "/api/tickets/PGU-103/actions/force_move",
        {"state": "done", "assignee": "director", "suppress_notification": True},
        caller="director",
    )
    empty_audit_kicked = post_json(
        base_url,
        "/api/tickets/PGU-134/actions/audit_kick_back",
        {},
        caller="audit",
    )
    assert empty_audit_kicked["ticket"]["state"] == "in_progress", empty_audit_kicked  # type: ignore[index]
    assert empty_audit_kicked["ticket"]["comments"] == [], empty_audit_kicked  # type: ignore[index]

    eric_signed = post_json(base_url, "/api/tickets/PGU-104/actions/user_sign_off", {"text": "User approves."}, caller="user")
    assert eric_signed["ticket"]["state"] == "director_review", eric_signed  # type: ignore[index]
    assert eric_signed["ticket"]["comments"][-1]["text"] == "User approves.", eric_signed  # type: ignore[index]
    user_reopened = post_json(
        base_url,
        "/api/tickets/PGU-105/actions/user_reopen",
        {"reason": "Needs design revision."},
        caller="user",
    )
    assert user_reopened["ticket"]["state"] in {"analysis", "in_progress"}, user_reopened  # type: ignore[index]
    assert user_reopened["ticket"]["comments"][-1]["who"] == "user", user_reopened  # type: ignore[index]
    empty_user_reopened = post_json(
        base_url,
        "/api/tickets/PGU-135/actions/user_reopen",
        {},
        caller="user",
    )
    assert empty_user_reopened["ticket"]["state"] == "analysis", empty_user_reopened  # type: ignore[index]
    assert empty_user_reopened["ticket"]["comments"] == [], empty_user_reopened  # type: ignore[index]

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

    psql(admin_conn, "ALTER TABLE ticket_board.tickets DISABLE TRIGGER USER;")
    seed_postgres_ticket(admin_conn, "PGU-128", title="Implementer hand-back", state="in_progress", assignee="ops", implementation="Blocked.")
    seed_postgres_ticket(admin_conn, "PGU-1281", title="Other implementer hand-back", state="in_progress", assignee="main", implementation="Blocked.")
    seed_postgres_ticket(admin_conn, "PGU-1282", title="Hand-back is not route", state="in_progress", assignee="ops", implementation="Blocked.")
    psql(admin_conn, "ALTER TABLE ticket_board.tickets ENABLE TRIGGER USER;")
    psql(admin_conn, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ('PGU-128', 'PGU-1281', 'PGU-1282');")
    handed_back = post_json(
        base_url,
        "/api/tickets/PGU-128/actions/implementer_kick_back",
        {"reason": "Cannot access the required external credentials."},
        caller="ops",
    )
    assert handed_back["ticket"]["state"] == "analysis", handed_back  # type: ignore[index]
    assert handed_back["ticket"]["assignee"] == "director", handed_back  # type: ignore[index]
    assert handed_back["ticket"]["comments"][-1]["who"] == "ops", handed_back  # type: ignore[index]
    assert handed_back["ticket"]["comments"][-1]["text"] == "Cannot access the required external credentials.", handed_back  # type: ignore[index]
    director_notice = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('target_role', target_role, 'kind', kind, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-128'
ORDER BY id
LIMIT 1;
""",
        )
    )
    assert director_notice == {"target_role": "director", "kind": "transition", "new_state": "analysis"}, director_notice
    empty_handback = post_json(
        base_url,
        "/api/tickets/PGU-1281/actions/implementer_kick_back",
        {"reason": "   "},
        caller="main",
        expect=400,
    )
    assert "implementer_kick_back requires a non-empty reason" in str(empty_handback), empty_handback
    wrong_handback = post_json(
        base_url,
        "/api/tickets/PGU-1281/actions/implementer_kick_back",
        {"reason": "Please clarify."},
        caller="ops",
        expect=403,
    )
    assert "ops cannot call implementer_kick_back for ticket assigned to main" in str(wrong_handback), wrong_handback
    route_disguised_as_handback = post_json(
        base_url,
        "/api/tickets/PGU-1282/actions/implementer_kick_back",
        {"reason": "Cannot proceed.", "state": "done", "assignee": "ops"},
        caller="ops",
    )
    assert route_disguised_as_handback["ticket"]["state"] == "analysis", route_disguised_as_handback  # type: ignore[index]
    assert route_disguised_as_handback["ticket"]["assignee"] == "director", route_disguised_as_handback  # type: ignore[index]
    psql(admin_conn, "DELETE FROM ticket_board.tickets WHERE id IN ('PGU-128', 'PGU-1281', 'PGU-1282');")

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


def exercise_real_write_client_file_bug_payload(app: TicketBoardApp, socket_path: Path) -> None:
    events = TicketBoardEventHub(app)
    server = TicketBoardUnixServer(
        socket_path,
        app,
        events=events,
        director_notifier=QuietNotifier(),
        caller_registry=CallerRegistry(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role="ops")
        filed = client.file_bug(
            title="Real write client file bug",
            body="Default needs_audit=true must not trip the director-only skip guard.",
            source_ticket_id="PGU-100",
        )["ticket"]
        assert filed["parent_id"] == "PGU-100", filed
        assert filed["needs_audit"] is True, filed
        forbidden = client.file_bug(
            title="Real write client forbidden no-audit file bug",
            body="Only director can turn audit off.",
            source_ticket_id="PGU-100",
            needs_audit=False,
        )
    except TicketBoardWriteError as exc:
        if "needs_audit can only be set to false by director" not in str(exc):
            raise
    else:
        raise AssertionError(f"needs_audit=false unexpectedly succeeded: {forbidden}")
    finally:
        server.shutdown()
        server.server_close()
        events.close()
        thread.join(timeout=2)


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
                exercise_write_api(
                    f"http://127.0.0.1:{server.server_port}",
                    commit_hash,
                    frames=frames,
                    assets=assets,
                    admin_conn=admin_conn,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            exercise_real_write_client_file_bug_payload(app, root / "ticket-board.sock")
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
