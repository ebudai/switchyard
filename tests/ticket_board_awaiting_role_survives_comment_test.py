#!/usr/bin/env python3
"""An escalation survives being acknowledged (PGU-909).

`await-role` is the only way a pane can say "the next action is someone else's"
when the blocker is not another ticket. Until PGU-909 the awaited role could
clear it by commenting, so replying "seen, still blocked" removed the ticket
from that role's own queue and let its nudges resume against the implementer.

Everything here runs against a real Postgres and the real schema. The bug was a
trigger, and the trigger it was actually in was NOT the one first suspected, so
reading the source is not sufficient evidence about which path fires.
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
            "title": "Escalation fixture",
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
    '{ticket_id}', 'Escalation fixture', '', '{state}', '{assignee}', '', '',
    false, true, false, false, false, false, '', false, false, false,
    '2026-09-04T00:00:00+00:00', '2026-09-04T00:00:00+00:00', '{source}'::jsonb
);
""",
    )


def awaiting(conninfo: str, ticket_id: str) -> str:
    return psql(
        conninfo,
        f"SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket_id}';",
    ).strip()


def suppressed(conninfo: str, ticket_id: str) -> bool:
    out = psql(
        conninfo,
        f"""
SELECT ticket_board.ticket_awaiting_role_is_active(ns.awaiting_role, ns.awaiting_since_at, clock_timestamp())
FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = '{ticket_id}';
""",
    ).strip()
    return out == "t"


def missing_prerequisite() -> str:
    for tool in ("initdb", "pg_ctl", "createdb", "psql"):
        if not shutil.which(tool):
            return f"missing {tool}; install PostgreSQL client and server binaries"
    return ""


def run_checks(svc: str, admin: str) -> None:
    # ACCEPTANCE 1 -- a comment by the awaited role does not clear the flag.
    insert_ticket(admin, "PGU-1", state="in_progress", assignee="research")
    as_role(svc, "research", "SELECT ticket_board.set_awaiting_role('PGU-1', 'director');")
    assert awaiting(admin, "PGU-1") == "director"
    assert suppressed(admin, "PGU-1") is True

    as_role(svc, "director", "SELECT ticket_board.append_ticket_comment('PGU-1', 'director', 'Seen, still blocked.');")
    assert awaiting(admin, "PGU-1") == "director", "an acknowledgement cleared the escalation"

    # ACCEPTANCE 4 -- the flag still governs suppression while it stands.
    assert suppressed(admin, "PGU-1") is True

    # A comment from anyone else must not clear it either, and never did.
    as_role(svc, "audit", "SELECT ticket_board.append_ticket_comment('PGU-1', 'audit', 'Passing by.');")
    assert awaiting(admin, "PGU-1") == "director"

    # ACCEPTANCE 3 -- the explicit clear still works.
    as_role(svc, "director", "SELECT ticket_board.clear_awaiting_role('PGU-1');")
    assert awaiting(admin, "PGU-1") == ""
    assert suppressed(admin, "PGU-1") is False

    # ACCEPTANCE 2a -- a state change still clears it.
    as_role(svc, "research", "SELECT ticket_board.set_awaiting_role('PGU-1', 'director');")
    assert awaiting(admin, "PGU-1") == "director"
    as_role(
        svc,
        "research",
        "SELECT ticket_board.submit_to_audit('PGU-1', "
        "'0123456789abcdef0123456789abcdef01234567');",
    )
    assert awaiting(admin, "PGU-1") == "", "a state change no longer clears the escalation"

    # ACCEPTANCE 2b -- a reassignment still clears it.
    # A different assignee, because the serial-focus rule would push a second
    # in_progress ticket for the same role straight to backlog.
    insert_ticket(admin, "PGU-2", state="in_progress", assignee="app")
    as_role(svc, "app", "SELECT ticket_board.set_awaiting_role('PGU-2', 'director');")
    assert awaiting(admin, "PGU-2") == "director"
    as_role(svc, "director", "SELECT ticket_board.route('PGU-2', 'in_progress', 'app');")
    assert awaiting(admin, "PGU-2") == "", "a reassignment no longer clears the escalation"

    # The regression in its exact original shape: the director's own reply must
    # not drop the ticket out of the director's queue.
    insert_ticket(admin, "PGU-3", state="audit", assignee="audit")
    as_role(svc, "audit", "SELECT ticket_board.set_awaiting_role('PGU-3', 'director');")
    before = awaiting(admin, "PGU-3")
    as_role(svc, "director", "SELECT ticket_board.append_ticket_comment('PGU-3', 'director', 'Verified, awaiting import.');")
    after = awaiting(admin, "PGU-3")
    assert before == after == "director", (before, after)


def main() -> int:
    reason = missing_prerequisite()
    if reason:
        print(f"ticket_board_awaiting_role_survives_comment_test: skipped - {reason}")
        return 0

    with tempfile.TemporaryDirectory(prefix="ticket-board-awaiting-comment.") as tmp:
        root = Path(tmp)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_awaiting_comment_test"
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

    print("ticket_board_awaiting_role_survives_comment_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
