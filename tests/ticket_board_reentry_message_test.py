#!/usr/bin/env python3
"""A ticket returning to in_progress is not a new ticket (PGU-912).

"New ticket for you: PGU-N" fired on every entry to in_progress, including
re-entry after a hold or a queue demotion. The team carries a workaround in
its head -- the phrase is understood to mean the ticket already exists -- which
does not survive a handover to someone who has not learned it.

This is a WORDING fix and the tests are built to catch it becoming a
suppression fix: every case asserts a notification row EXISTS for the target
role, then asserts what it says. A test that only checked the string would pass
just as happily if the notification were never enqueued at all.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout


def as_role(conninfo: str, caller: str, sql: str) -> str:
    return psql(conninfo, f"SELECT set_config('ticket_board.caller_role', '{caller}', false);\n{sql}")


def insert_ticket(conninfo: str, ticket_id: str, *, state: str, assignee: str) -> None:
    source = json.dumps(
        {
            "id": ticket_id,
            "title": "Re-entry fixture",
            "body": "",
            "state": state,
            "assignee": assignee,
            "comments": [],
            "created": "2026-09-04T00:00:00+00:00",
            "updated": "2026-09-04T00:00:00+00:00",
        },
        sort_keys=True,
    ).replace("'", "''")
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_audit, needs_inspection, inspector_signoff,
    needs_user_signoff, user_signoff, commit_hash, commit_exempt,
    manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', 'Re-entry fixture', '', '{state}', '{assignee}', '', '',
    false, true, false, false, false, false, '', false, false, false,
    '2026-09-04T00:00:00+00:00', '2026-09-04T00:00:00+00:00', '{source}'::jsonb
);
""",
    )


def queued_messages(conninfo: str, ticket_id: str, role: str) -> list[str]:
    out = psql(
        conninfo,
        f"""
SELECT message FROM ticket_board.ticket_notification_queue
WHERE ticket_id = '{ticket_id}' AND target_role = '{role}' AND kind = 'transition'
ORDER BY id;
""",
    )
    return [line for line in out.splitlines() if line.strip()]


def clear_queue(conninfo: str, ticket_id: str) -> None:
    psql(conninfo, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';")


def record_delivery(conninfo: str, ticket_id: str, role: str, state: str) -> None:
    """What the listener writes once it actually sends a notification.

    The wording depends on a real delivery having happened, not merely on one
    having been queued. That is deliberate: if the first announcement never
    reached the role, calling the second one new is correct for them.
    """
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.notification_trace
    (ticket_id, target_role, kind, event, ticket_state_at_event)
VALUES ('{ticket_id}', '{role}', 'transition', 'send', '{state}');
""",
    )


def missing_prerequisite() -> str:
    for tool in ("initdb", "pg_ctl", "createdb", "psql"):
        if not shutil.which(tool):
            return f"missing {tool}; install PostgreSQL client and server binaries"
    return ""


def run_checks(svc: str, admin: str) -> None:
    # ACCEPTANCE 1 -- first entry to in_progress still reads as a new assignment.
    insert_ticket(admin, "PGU-1", state="analysis", assignee="director")
    clear_queue(admin, "PGU-1")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-1', 'in_progress', 'research');")
    first = queued_messages(admin, "PGU-1", "research")
    assert len(first) == 1, first
    assert first[0].startswith("New ticket for you: PGU-1"), first

    record_delivery(admin, "PGU-1", "research", "in_progress")
    clear_queue(admin, "PGU-1")

    # ACCEPTANCE 2 -- re-entry still ARRIVES, and does not claim to be new.
    as_role(svc, "director", "SELECT ticket_board.route('PGU-1', 'backlog', 'research');")
    clear_queue(admin, "PGU-1")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-1', 'in_progress', 'research');")
    again = queued_messages(admin, "PGU-1", "research")
    assert len(again) == 1, ("re-entry produced no notification -- this became a suppression fix", again)
    assert "New ticket for you" not in again[0], again
    assert again[0] == "PGU-1 -- Re-entry fixture is active again", again

    # ACCEPTANCE 3 -- to a role that has not seen it, re-entry IS new.
    record_delivery(admin, "PGU-1", "research", "in_progress")
    clear_queue(admin, "PGU-1")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-1', 'backlog', 'research');")
    clear_queue(admin, "PGU-1")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-1', 'in_progress', 'app');")
    reassigned = queued_messages(admin, "PGU-1", "app")
    assert len(reassigned) == 1, reassigned
    assert reassigned[0].startswith("New ticket for you: PGU-1"), reassigned

    # A queued-but-never-delivered first announcement does not make the second
    # one a repeat: the role never heard the first.
    insert_ticket(admin, "PGU-2", state="analysis", assignee="director")
    clear_queue(admin, "PGU-2")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-2', 'in_progress', 'research');")
    clear_queue(admin, "PGU-2")  # queued, never sent -- no trace row written
    as_role(svc, "director", "SELECT ticket_board.route('PGU-2', 'backlog', 'research');")
    clear_queue(admin, "PGU-2")
    as_role(svc, "director", "SELECT ticket_board.route('PGU-2', 'in_progress', 'research');")
    undelivered = queued_messages(admin, "PGU-2", "research")
    assert len(undelivered) == 1, undelivered
    assert undelivered[0].startswith("New ticket for you: PGU-2"), undelivered

    # ACCEPTANCE 4 -- other transition wordings are untouched.
    # ops, because research and app already hold in_progress tickets above and
    # the serial-focus rule would demote a second one to backlog.
    insert_ticket(admin, "PGU-3", state="in_progress", assignee="ops")
    clear_queue(admin, "PGU-3")
    as_role(svc, "ops", "SELECT ticket_board.submit_to_audit('PGU-3', '0123456789abcdef0123456789abcdef01234567');")
    audit_msgs = queued_messages(admin, "PGU-3", "audit")
    assert len(audit_msgs) == 1, audit_msgs
    assert audit_msgs[0].endswith("ready for audit"), audit_msgs

    kicked_before = queued_messages(admin, "PGU-3", "ops")
    clear_queue(admin, "PGU-3")
    as_role(svc, "audit", "SELECT ticket_board.audit_kick_back('PGU-3', 'Needs rework.');")
    kicked = queued_messages(admin, "PGU-3", "ops")
    assert len(kicked) == 1, (kicked_before, kicked)
    assert kicked[0].endswith("kicked back to you"), kicked


def main() -> int:
    reason = missing_prerequisite()
    if reason:
        print(f"ticket_board_reentry_message_test: skipped - {reason}")
        return 0

    with tempfile.TemporaryDirectory(prefix="ticket-board-reentry.") as tmp:
        root = Path(tmp)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_reentry_test"
        admin = f"host={socket_dir} port={port} dbname={dbname}"
        svc = f"{admin} user=ticket_board_service"

        subprocess.run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"], check=True, capture_output=True)
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname], check=True, capture_output=True)
            psql(admin, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(admin, "CREATE ROLE ticket_board_service LOGIN;")
            psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(
                admin,
                """
GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ticket_board TO ticket_board_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ticket_board TO ticket_board_service;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board TO ticket_board_service;
""",
            )
            run_checks(svc, admin)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_reentry_message_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
