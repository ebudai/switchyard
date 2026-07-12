#!/usr/bin/env python3
"""Regression test: TicketBoardApp writes through the PostgreSQL single-writer function API."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    raise SystemExit(
        "ticket_board_postgres_backend_test: psycopg3 is required for the Postgres backend; "
        "install Arch/CachyOS package python-psycopg, or run the test from a venv with psycopg installed"
    )

from scripts.ticket_board.app import TicketBoardApp


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "eric", "ops", "app", "audit", "inspector", "perf", "research", "main"]
SERVICE_ROLE = "ticket_board_service"


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


def psql_error(conn: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        raise AssertionError(f"SQL unexpectedly succeeded: {sql}")
    return proc.stderr + proc.stdout


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


def insert_ticket(
    conn: str,
    ticket_id: str,
    *,
    title: str,
    state: str,
    assignee: str,
    implementation: str = "Implemented.",
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


def make_app(root: Path, socket_dir: Path, port: int, dbname: str, role: str) -> TicketBoardApp:
    return TicketBoardApp(
        root / "frames",
        root / "assets",
        database_url=conninfo(socket_dir, port, dbname, role),
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-postgres-backend.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        dbname = "pgu_backend_test"
        admin_conn = conninfo(socket_dir, port, dbname)

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            create_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            service_app = make_app(root, socket_dir, port, dbname, SERVICE_ROLE)
            service_conn = conninfo(socket_dir, port, dbname, SERVICE_ROLE)

            before_signature = service_app.store_signature()
            created = service_app.create_ticket(
                title="Postgres create",
                body="Created through TicketBoardApp.",
                screenshot=None,
                assignee="unassigned",
                needs_eric_signoff=False,
            )
            assert created["id"] == "PGU-1", created
            assert service_app.verify_created_ticket_persisted(created, before_signature) != before_signature
            assert not (root / f"json-unused-{SERVICE_ROLE}").exists(), "postgres backend should not create a JSON store directory"
            assert "permission denied" in psql_error(
                service_conn,
                "UPDATE ticket_board.tickets SET title = title WHERE id = 'PGU-1';",
            )

            filed = service_app.create_ticket_record(
                title="Filed from implementation",
                body="Linked to source.",
                screenshot=None,
                screenshots=None,
                assignee="unassigned",
                state="analysis",
                blocked_by=[],
                implementation="",
                audit_prompt="",
                audit_signoff=False,
                needs_eric_signoff=False,
                eric_signoff=False,
                comments=[],
                parent_id="PGU-1",
            )
            assert filed["parent_id"] == "PGU-1", filed
            assert "source_ticket_id ticket not found" in psql_error(
                service_conn,
                "SELECT ticket_board.file_bug('missing', 'body', 'PGU-99999');",
            )

            blocked = service_app.create_ticket(
                title="Blocked child",
                body="Needs PGU-1 first.",
                screenshot=None,
                assignee="unassigned",
                needs_eric_signoff=False,
                blocked_by=["PGU-1"],
                blocked_reason="Waiting for PGU-1.",
            )
            assert blocked["blocked_by"] == ["PGU-1"], blocked
            assert blocked["blocked_reason"] == "Waiting for PGU-1.", blocked

            service_app.update_ticket(blocked["id"], {"comment": {"who": "director", "text": "Tracking note."}})
            cancelled = service_app.update_ticket(
                blocked["id"],
                {
                    "state": "cancelled",
                    "comment": {"who": "director", "text": "Covered by PGU-1."},
                },
            )
            assert cancelled["state"] == "cancelled", cancelled
            assert cancelled["comments"][-1]["text"] == "Covered by PGU-1.", cancelled

            insert_ticket(admin_conn, "PGU-100", title="Ops ready", state="ready", assignee="ops")
            in_progress = service_app.update_ticket("PGU-100", {"state": "in_progress"})
            assert in_progress["state"] == "in_progress", in_progress
            submitted = service_app.update_ticket("PGU-100", {"state": "audit", "commit_hash": "abcdef1"})
            assert submitted["state"] == "audit", submitted
            assert submitted["commit_hash"] == "abcdef1", submitted
            edited = service_app.update_ticket("PGU-100", {"implementation": "Edited through function API."})
            assert edited["implementation"] == "Edited through function API.", edited

            audit_ready = service_app.update_ticket("PGU-100", {"audit_signoff": True})
            assert audit_ready["state"] == "director_review", audit_ready
            done = service_app.update_ticket("PGU-100", {"state": "done", "commit_hash": "abcdef1"})
            assert done["state"] == "done", done

            insert_ticket(admin_conn, "PGU-200", title="Audit kickback", state="audit", assignee="audit")
            kicked = service_app.update_ticket(
                "PGU-200",
                {"state": "analysis", "comment": {"who": "audit", "text": "Needs another pass."}},
            )
            assert kicked["state"] == "analysis", kicked
            assert kicked["comments"][-1]["text"] == "Needs another pass.", kicked

            insert_ticket(
                admin_conn,
                "PGU-300",
                title="Eric review",
                state="eric_review",
                assignee="director",
                audit_signoff=True,
                needs_eric_signoff=True,
            )
            eric_signed = service_app.update_ticket("PGU-300", {"eric_signoff": True})
            assert eric_signed["state"] == "director_review", eric_signed
            assert eric_signed["eric_signoff"] is True, eric_signed

            insert_ticket(
                admin_conn,
                "PGU-301",
                title="Eric reopen",
                state="eric_review",
                assignee="director",
                audit_signoff=True,
                needs_eric_signoff=True,
            )
            eric_reopened = service_app.update_ticket(
                "PGU-301",
                {"state": "analysis", "comment": {"who": "eric", "text": "Needs design revision."}},
            )
            assert eric_reopened["state"] == "ready", eric_reopened
            assert eric_reopened["comments"][-1]["text"] == "Needs design revision.", eric_reopened

            deferred = service_app.update_ticket("PGU-1", {"state": "backlog"})
            assert deferred["state"] == "backlog", deferred

            insert_ticket(admin_conn, "PGU-401", title="Old analysis ping", state="analysis", assignee="ops", implementation="")
            insert_ticket(admin_conn, "PGU-402", title="Active analysis ping", state="analysis", assignee="ops", implementation="")
            insert_ticket(admin_conn, "PGU-403", title="Old audit ping", state="audit", assignee="audit")
            insert_ticket(admin_conn, "PGU-404", title="Active audit ping", state="audit", assignee="audit")
            insert_ticket(
                admin_conn,
                "PGU-405",
                title="Old director review ping",
                state="director_review",
                assignee="director",
                audit_signoff=True,
                commit_hash="abcdef2",
            )
            insert_ticket(
                admin_conn,
                "PGU-406",
                title="Active director review ping",
                state="director_review",
                assignee="director",
                audit_signoff=True,
                commit_hash="abcdef3",
            )
            psql(
                admin_conn,
                """
UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp(),
    last_nudged_at = NULL
WHERE ticket_id IN ('PGU-401', 'PGU-403', 'PGU-405');

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp() + interval '1 hour',
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-402';

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp(),
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-404';

INSERT INTO ticket_board.ticket_notification_queue (
    ticket_id, kind, target_role, message, payload, dedupe_key, updated_at
) VALUES (
    'PGU-404',
    'transition',
    'audit',
    'PGU-404 -- Active audit ping ready for audit',
    jsonb_build_object('id', 'PGU-404', 'state', 'audit', 'target_role', 'audit'),
    'active-work-test:PGU-404:audit',
    clock_timestamp() + interval '2 hours'
);

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp() + interval '3 hours',
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-406';
""",
            )

            tickets, errors = service_app.list_tickets()
            assert errors == [], errors
            assert {"PGU-1", "PGU-2", "PGU-3", "PGU-100", "PGU-200", "PGU-300", "PGU-301"}.issubset(
                {ticket["id"] for ticket in tickets}
            ), tickets
            tickets_by_id = {ticket["id"]: ticket for ticket in tickets}
            assert tickets_by_id["PGU-402"]["active_work_highlight"] is True, tickets_by_id["PGU-402"]
            assert tickets_by_id["PGU-402"]["active_work_owner_role"] == "director", tickets_by_id["PGU-402"]
            assert tickets_by_id["PGU-402"]["active_work_notified_at"], tickets_by_id["PGU-402"]
            assert tickets_by_id["PGU-401"]["active_work_highlight"] is False, tickets_by_id["PGU-401"]
            assert tickets_by_id["PGU-404"]["active_work_highlight"] is True, tickets_by_id["PGU-404"]
            assert tickets_by_id["PGU-404"]["active_work_owner_role"] == "audit", tickets_by_id["PGU-404"]
            assert tickets_by_id["PGU-403"]["active_work_highlight"] is False, tickets_by_id["PGU-403"]
            assert tickets_by_id["PGU-406"]["active_work_highlight"] is True, tickets_by_id["PGU-406"]
            assert tickets_by_id["PGU-406"]["active_work_owner_role"] == "director", tickets_by_id["PGU-406"]
            assert tickets_by_id["PGU-405"]["active_work_highlight"] is False, tickets_by_id["PGU-405"]
            assert tickets_by_id["PGU-1"]["active_work_highlight"] is False, tickets_by_id["PGU-1"]
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_postgres_backend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
