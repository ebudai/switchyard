#!/usr/bin/env python3
"""Regression test: TicketBoardApp writes through the PostgreSQL single-writer function API."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

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
            assert created["state"] == "analysis", created
            assert created["assignee"] == "unassigned", created
            assert service_app.verify_created_ticket_persisted(created, before_signature) != before_signature
            assert not (root / f"json-unused-{SERVICE_ROLE}").exists(), "postgres backend should not create a JSON store directory"
            assert "permission denied" in psql_error(
                service_conn,
                "UPDATE ticket_board.tickets SET title = title WHERE id = 'PGU-1';",
            )
            created_with_implementation = service_app.create_ticket(
                title="Postgres create with implementation",
                body="Created with spec text up front.",
                screenshot=None,
                assignee="unassigned",
                needs_eric_signoff=False,
                implementation="Use the ticket body and this implementation note.",
            )
            assert created_with_implementation["implementation"] == "Use the ticket body and this implementation note.", created_with_implementation
            backlog_created = service_app.create_ticket(
                title="Postgres backlog create",
                body="Deferred future work.",
                screenshot=None,
                assignee="ops",
                needs_eric_signoff=False,
                state="backlog",
            )
            assert backlog_created["state"] == "backlog", backlog_created
            assert backlog_created["assignee"] == "unassigned", backlog_created
            backlog_state = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'parked', t.parked,
    'queue_count', (SELECT count(*) FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id = t.id),
    'trace_count', (SELECT count(*) FROM ticket_board.notification_trace nt WHERE nt.ticket_id = t.id)
)::text
FROM ticket_board.tickets t
WHERE t.id = '{backlog_created["id"]}';
""",
                )
            )
            assert backlog_state == {
                "state": "backlog",
                "assignee": "unassigned",
                "parked": True,
                "queue_count": 0,
                "trace_count": 0,
            }, backlog_state
            psql(
                admin_conn,
                f"""
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id = '{backlog_created["id"]}';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            backlog_nudge_state = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_build_object(
    'queue_count', (SELECT count(*) FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id = t.id),
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = '{backlog_created["id"]}';
""",
                )
            )
            assert backlog_nudge_state == {"queue_count": 0, "last_nudged": False, "nudge_count": 0}, backlog_nudge_state
            for disallowed_state in ("in_progress", "inspection", "audit", "director_review", "done", "cancelled"):
                try:
                    service_app.create_ticket(
                        title=f"Bad {disallowed_state}",
                        body="Must reject pipeline state create.",
                        screenshot=None,
                        assignee="ops",
                        needs_eric_signoff=False,
                        state=disallowed_state,
                    )
                except ValueError as exc:
                    assert "invalid create state" in str(exc), exc
                else:
                    raise AssertionError(f"create unexpectedly accepted {disallowed_state}")

            create_attachment = frames / "create-frame-ref.png"
            Image.new("RGB", (2, 2), (120, 40, 80)).save(create_attachment)
            created_with_attachment = service_app.create_ticket(
                title="Postgres create with attachment",
                body="Created with a pasted image path.",
                screenshot=str(create_attachment),
                assignee="unassigned",
                needs_eric_signoff=False,
            )
            created_attachment_path = Path(created_with_attachment["screenshots"][0])
            assert created_attachment_path != create_attachment.resolve(), created_with_attachment
            assert assets.resolve() in created_attachment_path.parents, created_with_attachment
            assert created_with_attachment["screenshot"] == str(created_attachment_path), created_with_attachment
            assert created_with_attachment["screenshots_info"][0]["available"] is True, created_with_attachment
            create_attachment.unlink()
            persisted_created_attachment = service_app.get_ticket(created_with_attachment["id"])
            assert persisted_created_attachment["screenshots"] == [str(created_attachment_path)], persisted_created_attachment
            assert persisted_created_attachment["screenshots_info"][0]["available"] is True, persisted_created_attachment
            assert created_attachment_path.is_file(), created_attachment_path

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
            assert blocked["blockers"] == [{"id": "PGU-1", "resolved": False}], blocked
            assert blocked["blocked_reason"] == "Waiting for PGU-1.", blocked
            blocked_create_state = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_build_object(
    'blocked_by', (SELECT jsonb_agg(blocker_ticket_id ORDER BY position) FROM ticket_board.ticket_blockers WHERE ticket_id = t.id),
    'blockers', (SELECT jsonb_agg(jsonb_build_object('id', blocker_ticket_id, 'resolved', resolved) ORDER BY position) FROM ticket_board.ticket_blockers WHERE ticket_id = t.id),
    'blocked_reason', t.blocked_reason,
    'queue_count', (SELECT count(*) FROM ticket_board.ticket_notification_queue q WHERE q.ticket_id = t.id),
    'trace_count', (SELECT count(*) FROM ticket_board.notification_trace nt WHERE nt.ticket_id = t.id)
)::text
FROM ticket_board.tickets t
WHERE t.id = '{blocked["id"]}';
""",
                )
            )
            assert blocked_create_state == {
                "blocked_by": ["PGU-1"],
                "blockers": [{"id": "PGU-1", "resolved": False}],
                "blocked_reason": "Waiting for PGU-1.",
                "queue_count": 0,
                "trace_count": 0,
            }, blocked_create_state
            assert "blocker ticket not found: PGU-99999" in psql_error(
                service_conn,
                """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.create_ticket('Missing blocker', 'Body', 'analysis', ARRAY['PGU-99999'], 'Missing dependency.');
""",
            )
            next_ticket_id = psql(admin_conn, "SELECT 'PGU-' || (max(ticket_number) + 1)::text FROM ticket_board.tickets;")
            assert "invalid blocker ticket id" in psql_error(
                service_conn,
                f"""
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.create_ticket('Self blocked', 'Body', 'analysis', ARRAY['{next_ticket_id}'], 'Self dependency.');
""",
            )
            cycle_ticket_id = psql(admin_conn, "SELECT 'PGU-' || (max(ticket_number) + 1)::text FROM ticket_board.tickets;")
            psql(
                admin_conn,
                f"INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position) VALUES ('PGU-1', '{cycle_ticket_id}', 0);",
            )
            assert "blocked_by cycle detected" in psql_error(
                service_conn,
                """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.create_ticket('Cycle blocked', 'Body', 'analysis', ARRAY['PGU-1'], 'Cycle dependency.');
""",
            )
            psql(admin_conn, f"DELETE FROM ticket_board.ticket_blockers WHERE ticket_id = 'PGU-1' AND blocker_ticket_id = '{cycle_ticket_id}';")

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

            insert_ticket(admin_conn, "PGU-100", title="Ops implementation", state="analysis", assignee="ops", implementation="")
            in_progress = service_app.update_ticket("PGU-100", {"state": "in_progress"})
            assert in_progress["state"] == "in_progress", in_progress
            assert in_progress["implementation"] == "", in_progress
            submitted = service_app.update_ticket("PGU-100", {"state": "audit", "commit_hash": "abcdef1"})
            assert submitted["state"] == "audit", submitted
            assert submitted["commit_hash"] == "abcdef1", submitted
            edited = service_app.update_ticket("PGU-100", {"implementation": "Edited through function API."})
            assert edited["implementation"] == "Edited through function API.", edited

            frame_attachment = frames / "frame-ref.png"
            Image.new("RGB", (2, 2), (80, 120, 160)).save(frame_attachment)
            attached = service_app.update_ticket("PGU-100", {"screenshots": [str(frame_attachment)]})
            stored_path = Path(attached["screenshots"][0])
            assert stored_path != frame_attachment.resolve(), attached
            assert assets.resolve() in stored_path.parents, attached
            assert attached["screenshots_info"][0]["available"] is True, attached
            frame_attachment.unlink()
            persisted_attachment = service_app.get_ticket("PGU-100")
            assert persisted_attachment["screenshots"] == [str(stored_path)], persisted_attachment
            assert persisted_attachment["screenshots_info"][0]["available"] is True, persisted_attachment
            assert stored_path.is_file(), stored_path

            audit_ready = service_app.update_ticket(
                "PGU-100",
                {"audit_signoff": True, "comment": {"who": "audit", "text": "Audit verified."}},
            )
            assert audit_ready["state"] == "director_review", audit_ready
            assert audit_ready["comments"][-1]["text"] == "Audit verified.", audit_ready
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
            eric_signed = service_app.update_ticket(
                "PGU-300",
                {"eric_signoff": True, "comment": {"who": "eric", "text": "Eric approves."}},
            )
            assert eric_signed["state"] == "director_review", eric_signed
            assert eric_signed["eric_signoff"] is True, eric_signed
            assert eric_signed["comments"][-1]["text"] == "Eric approves.", eric_signed

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
            assert eric_reopened["state"] == "in_progress", eric_reopened
            assert eric_reopened["comments"][-1]["text"] == "Needs design revision.", eric_reopened

            deferred = service_app.update_ticket("PGU-1", {"state": "backlog"})
            assert deferred["state"] == "backlog", deferred

            insert_ticket(admin_conn, "PGU-401", title="Old analysis ping", state="analysis", assignee="ops", implementation="")
            insert_ticket(admin_conn, "PGU-402", title="Active analysis ping", state="analysis", assignee="ops", implementation="")
            insert_ticket(admin_conn, "PGU-403", title="Old audit ping", state="audit", assignee="audit")
            insert_ticket(admin_conn, "PGU-404", title="Active audit ping", state="audit", assignee="audit")
            insert_ticket(admin_conn, "PGU-407", title="Old ops implementation ping", state="in_progress", assignee="ops")
            insert_ticket(admin_conn, "PGU-408", title="Active ops implementation ping", state="in_progress", assignee="ops")
            insert_ticket(admin_conn, "PGU-409", title="Old app implementation ping", state="in_progress", assignee="app")
            insert_ticket(admin_conn, "PGU-410", title="Active app implementation ping", state="in_progress", assignee="app")
            insert_ticket(admin_conn, "PGU-411", title="Unassigned implementation ping", state="in_progress", assignee="unassigned")
            insert_ticket(admin_conn, "PGU-412", title="Queued-only ops implementation ping", state="in_progress", assignee="ops")
            insert_ticket(admin_conn, "PGU-413", title="Nudged-only ops implementation ping", state="in_progress", assignee="ops")
            insert_ticket(admin_conn, "PGU-414", title="Active inspection ping", state="inspection", assignee="ops")
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
WHERE ticket_id IN ('PGU-401', 'PGU-405', 'PGU-407', 'PGU-409', 'PGU-411');

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp() + interval '1 hour',
    last_nudged_at = NULL
WHERE ticket_id IN ('PGU-402', 'PGU-408');

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = '2026-07-10T12:00:00+00:00'::timestamptz,
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-403';

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = '2026-07-10T10:00:00+00:00'::timestamptz,
    last_nudged_at = '2026-07-10T10:30:00+00:00'::timestamptz
WHERE ticket_id = 'PGU-404';

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp() + interval '4 hours',
    last_nudged_at = NULL
WHERE ticket_id IN ('PGU-412', 'PGU-414');

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = '2026-07-10T11:00:00+00:00'::timestamptz,
    last_nudged_at = clock_timestamp() + interval '5 hours'
WHERE ticket_id = 'PGU-413';

INSERT INTO ticket_board.ticket_notification_queue (
    ticket_id, kind, target_role, message, payload, dedupe_key, updated_at
) VALUES (
    'PGU-404',
    'transition',
    'audit',
    'PGU-404 -- Active audit ping ready for audit',
    jsonb_build_object('id', 'PGU-404', 'state', 'audit', 'target_role', 'audit'),
    'active-work-test:PGU-404:audit',
    '2026-07-10T13:00:00+00:00'::timestamptz
), (
    'PGU-410',
    'transition',
    'app',
    'PGU-410 -- Active app implementation ping',
    jsonb_build_object('id', 'PGU-410', 'state', 'in_progress', 'target_role', 'app'),
    'active-work-test:PGU-410:app',
    clock_timestamp() + interval '2 hours'
), (
    'PGU-412',
    'transition',
    'ops',
    'PGU-412 -- Queued-only ops implementation ping',
    jsonb_build_object('id', 'PGU-412', 'state', 'in_progress', 'target_role', 'ops'),
    'active-work-test:PGU-412:ops',
    clock_timestamp() + interval '6 hours'
), (
    'PGU-413',
    'nudge',
    'ops',
    'NUDGE PGU-413 -- Nudged-only ops implementation ping is still in progress',
    jsonb_build_object('kind', 'nudge', 'id', 'PGU-413', 'state', 'in_progress', 'target_role', 'ops'),
    'active-work-test:PGU-413:ops',
    clock_timestamp() + interval '7 hours'
);

UPDATE ticket_board.ticket_notification_state
SET last_transition_notified_at = clock_timestamp() + interval '3 hours',
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-406';

INSERT INTO ticket_board.notification_trace (
    ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event, pane_busy_determination, busy_reason
) VALUES
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-402', 'director', 'transition', 'send', 'analysis', 'ops', 'idle', 'idle'),
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-404', 'audit', 'transition', 'send', 'audit', 'audit', 'idle', 'idle'),
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-408', 'ops', 'transition', 'send', 'in_progress', 'ops', 'idle', 'idle'),
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-410', 'app', 'transition', 'send', 'in_progress', 'app', 'idle', 'idle'),
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-406', 'director', 'transition', 'send', 'director_review', 'director', 'idle', 'idle'),
    ('2026-07-10T13:00:00+00:00'::timestamptz, 'PGU-414', 'inspector', 'transition', 'send', 'inspection', 'ops', 'idle', 'idle');
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
            assert tickets_by_id["PGU-408"]["active_work_highlight"] is True, tickets_by_id["PGU-408"]
            assert tickets_by_id["PGU-408"]["active_work_owner_role"] == "ops", tickets_by_id["PGU-408"]
            assert tickets_by_id["PGU-408"]["active_work_notified_at"], tickets_by_id["PGU-408"]
            assert tickets_by_id["PGU-407"]["active_work_highlight"] is False, tickets_by_id["PGU-407"]
            assert tickets_by_id["PGU-410"]["active_work_highlight"] is True, tickets_by_id["PGU-410"]
            assert tickets_by_id["PGU-410"]["active_work_owner_role"] == "app", tickets_by_id["PGU-410"]
            assert tickets_by_id["PGU-409"]["active_work_highlight"] is False, tickets_by_id["PGU-409"]
            assert tickets_by_id["PGU-411"]["active_work_highlight"] is False, tickets_by_id["PGU-411"]
            assert tickets_by_id["PGU-411"]["active_work_owner_role"] == "", tickets_by_id["PGU-411"]
            assert tickets_by_id["PGU-412"]["active_work_highlight"] is False, tickets_by_id["PGU-412"]
            assert tickets_by_id["PGU-413"]["active_work_highlight"] is False, tickets_by_id["PGU-413"]
            assert tickets_by_id["PGU-406"]["active_work_highlight"] is True, tickets_by_id["PGU-406"]
            assert tickets_by_id["PGU-406"]["active_work_owner_role"] == "director", tickets_by_id["PGU-406"]
            assert tickets_by_id["PGU-414"]["active_work_highlight"] is True, tickets_by_id["PGU-414"]
            assert tickets_by_id["PGU-414"]["active_work_owner_role"] == "inspector", tickets_by_id["PGU-414"]
            assert tickets_by_id["PGU-405"]["active_work_highlight"] is False, tickets_by_id["PGU-405"]
            assert tickets_by_id["PGU-1"]["active_work_highlight"] is False, tickets_by_id["PGU-1"]

            insert_ticket(admin_conn, "PGU-415", title="Prompt refresh analysis ping", state="analysis", assignee="ops", implementation="")
            before_send_signature = service_app.store_signature()
            before_send = service_app.get_ticket("PGU-415")
            assert before_send["active_work_highlight"] is False, before_send
            psql(
                admin_conn,
                """
INSERT INTO ticket_board.notification_trace (
    ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event, pane_busy_determination, busy_reason
) VALUES (
    clock_timestamp(), 'PGU-415', 'director', 'transition', 'send', 'analysis', 'ops', 'idle', 'idle'
);
""",
            )
            after_send_signature = service_app.store_signature()
            assert after_send_signature != before_send_signature
            after_send = service_app.get_ticket("PGU-415")
            assert after_send["active_work_highlight"] is True, after_send
            assert after_send["active_work_owner_role"] == "director", after_send
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_postgres_backend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
