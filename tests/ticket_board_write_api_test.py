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
from pathlib import Path
from urllib.error import HTTPError

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
from scripts.ticket_board.server import CALLER_ROLE_HEADER, TicketBoardServer


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "eric", "ops", "app", "audit", "perf", "research", "main"]
SERVICE_ROLE = "ticket_board_service"
FRONTEND_SCRIPT_PATHS = [
    ROOT / "scripts" / "ticket_board" / "frontend_script_app.py",
    ROOT / "scripts" / "ticket_board" / "frontend_script_core.py",
    ROOT / "scripts" / "ticket_board" / "frontend_script_detail.py",
]


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
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
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
        "needs_eric_signoff": needs_eric_signoff,
        "eric_signoff": eric_signoff,
        "manually_controlled": False,
        "commit_hash": commit_hash,
        "commit_exempt": False,
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
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation,
    audit_signoff, needs_eric_signoff, eric_signoff, commit_hash,
    created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{implementation}',
    {str(audit_signoff).lower()}, {str(needs_eric_signoff).lower()},
    {str(eric_signoff).lower()}, '{commit_hash}',
    '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
    '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
""",
    )


def post_json(base_url: str, path: str, payload: dict[str, object], *, caller: str | None = None, expect: int = 200) -> dict[str, object] | str:
    headers = {"Content-Type": "application/json"}
    if caller is not None:
        headers[CALLER_ROLE_HEADER] = caller
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


def get_ticket(base_url: str, ticket_id: str) -> dict[str, object]:
    with urllib.request.urlopen(base_url + "/api/board", timeout=5) as response:
        board = json.loads(response.read().decode("utf-8"))
    for ticket in board["tickets"]:
        if ticket["id"] == ticket_id:
            return ticket
    raise AssertionError(f"{ticket_id} not found in board")


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


def seed_fixtures(seed_ticket: object, commit_hash: str) -> None:
    seed_ticket("PGU-100", title="Route", state="analysis")
    seed_ticket("PGU-101", title="Start submit", state="ready", assignee="ops", implementation="Ready.")
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
    seed_ticket("PGU-116", title="Backlog cancel", state="backlog", assignee="ops")
    seed_ticket("PGU-109", title="Manual", state="analysis")
    seed_ticket("PGU-110", title="Blocker", state="analysis")
    seed_ticket("PGU-111", title="Blocked target", state="analysis")
    seed_ticket("PGU-112", title="Comment", state="analysis")
    seed_ticket("PGU-113", title="Merge source", state="analysis")
    seed_ticket("PGU-114", title="Merge target", state="analysis")
    seed_ticket(
        "PGU-115",
        title="Director kickback to ready",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )


def exercise_write_api(base_url: str, commit_hash: str) -> None:
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

    filed_payload = post_json(
        base_url,
        "/api/tickets/actions/file_bug",
        {"title": "API file bug", "body": "Linked bug.", "source_ticket_id": source_id},
        caller="ops",
        expect=201,
    )
    filed = filed_payload["ticket"]  # type: ignore[index]
    assert filed["parent_id"] == source_id, filed  # type: ignore[index]

    routed = post_json(base_url, "/api/tickets/PGU-100/actions/route", {"state": "backlog", "assignee": "ops"}, caller="director")
    assert routed["ticket"]["state"] == "backlog", routed  # type: ignore[index]
    assert routed["ticket"]["assignee"] == "ops", routed  # type: ignore[index]
    director_ready = post_json(
        base_url,
        "/api/tickets/PGU-115/actions/route",
        {"state": "ready", "assignee": "ops"},
        caller="director",
    )
    assert director_ready["ticket"]["state"] == "ready", director_ready  # type: ignore[index]
    assert director_ready["ticket"]["assignee"] == "ops", director_ready  # type: ignore[index]

    wrong_assignee = post_json(base_url, "/api/tickets/PGU-101/actions/start_work", {}, caller="app", expect=403)
    assert "app cannot call start_work for ticket assigned to ops" in str(wrong_assignee), wrong_assignee
    started = post_json(base_url, "/api/tickets/PGU-101/actions/start_work", {}, caller="ops")
    assert started["ticket"]["state"] == "in_progress", started  # type: ignore[index]
    submitted = post_json(base_url, "/api/tickets/PGU-101/actions/submit_to_audit", {"commit_hash": commit_hash}, caller="ops")
    assert submitted["ticket"]["state"] == "audit", submitted  # type: ignore[index]
    assert submitted["ticket"]["commit_hash"] == commit_hash, submitted  # type: ignore[index]

    audit_signed = post_json(base_url, "/api/tickets/PGU-102/actions/audit_sign_off", {}, caller="audit")
    assert audit_signed["ticket"]["state"] == "director_review", audit_signed  # type: ignore[index]
    audit_kicked = post_json(
        base_url,
        "/api/tickets/PGU-103/actions/audit_kick_back",
        {"reason": "Needs another pass."},
        caller="audit",
    )
    assert audit_kicked["ticket"]["state"] == "analysis", audit_kicked  # type: ignore[index]
    assert audit_kicked["ticket"]["comments"][-1]["who"] == "audit", audit_kicked  # type: ignore[index]

    eric_signed = post_json(base_url, "/api/tickets/PGU-104/actions/eric_sign_off", {}, caller="eric")
    assert eric_signed["ticket"]["state"] == "director_review", eric_signed  # type: ignore[index]
    eric_reopened = post_json(
        base_url,
        "/api/tickets/PGU-105/actions/eric_reopen",
        {"reason": "Needs design revision."},
        caller="eric",
    )
    assert eric_reopened["ticket"]["state"] in {"analysis", "ready"}, eric_reopened  # type: ignore[index]
    assert eric_reopened["ticket"]["comments"][-1]["who"] == "eric", eric_reopened  # type: ignore[index]

    done = post_json(base_url, "/api/tickets/PGU-106/actions/mark_done", {"commit_hash": commit_hash}, caller="director")
    assert done["ticket"]["state"] == "done", done  # type: ignore[index]
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
    assert get_ticket(base_url, "PGU-112")["comments"][-1]["text"] == "Pane note."
    edited = post_json(base_url, "/api/tickets/PGU-112/actions/edit_fields", {"title": "Edited through action"}, caller="app")
    assert edited["ticket"]["title"] == "Edited through action", edited  # type: ignore[index]
    invalid_edit = post_json(base_url, "/api/tickets/PGU-112/actions/edit_fields", {"state": "done"}, caller="app", expect=400)
    assert "edit_fields cannot update: state" in str(invalid_edit), invalid_edit
    merge_forbidden = post_json(base_url, "/api/tickets/PGU-113/actions/merge", {"target_id": "PGU-114"}, caller="app", expect=403)
    assert "app cannot call merge" in str(merge_forbidden), merge_forbidden
    merged = post_json(base_url, "/api/tickets/PGU-113/actions/merge", {"target_id": "PGU-114"}, caller="director")
    assert merged["target"]["id"] == "PGU-114", merged  # type: ignore[index]
    merged_source = get_ticket(base_url, "PGU-113")
    assert merged_source["state"] == "done", merged_source
    assert merged_source["commit_exempt"] is True, merged_source


def exercise_json_backend(commit_hash: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-write-api-json.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        seed_fixtures(lambda ticket_id, **kwargs: seed_json_ticket(store, ticket_id, **kwargs), commit_hash)
        app = TicketBoardApp(store, frames, assets, store_backend="json", allow_json_store=True)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise_write_api(f"http://127.0.0.1:{server.server_port}", commit_hash)
        finally:
            server.shutdown()
            server.server_close()
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
            create_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            seed_fixtures(lambda ticket_id, **kwargs: seed_postgres_ticket(admin_conn, ticket_id, **kwargs), commit_hash)
            app = TicketBoardApp(
                root / "json-unused",
                frames,
                assets,
                store_backend="postgres",
                database_url=conninfo(socket_dir, port, dbname, SERVICE_ROLE),
            )
            server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                exercise_write_api(f"http://127.0.0.1:{server.server_port}", commit_hash)
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
    exercise_json_backend(commit_hash)
    exercise_postgres_backend(commit_hash)
    print("ticket_board_write_api_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
