#!/usr/bin/env python3
"""Regression: active awaiting_role suppresses idle turn-end reminders."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"


def run(args: list[str], *, capture: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
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
    return proc.stdout.strip()


def ticket_source(ticket_id: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": ticket_id,
        "body": "",
        "state": state,
        "assignee": assignee,
        "implementation": "Ready.",
        "comments": [],
        "created": "2026-09-05T00:00:00+00:00",
        "updated": "2026-09-05T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-awaiting-idle-turn-end.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_awaiting_idle_turn_end_test"
        admin_conn = f"host={socket_dir} port={port} dbname={dbname} user=postgres"
        listener_conn = f"host={socket_dir} port={port} dbname={dbname} user=ticket_board_listener"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(admin_conn, "CREATE ROLE ticket_board_listener LOGIN; CREATE ROLE ticket_board_service;")
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            psql(
                admin_conn,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-9161', 'Awaiting-role suppresses turn-end nudge', '', 'in_progress', 'ops', 'Waiting on director.',
    '2026-09-05T00:00:00+00:00', '2026-09-05T00:00:00+00:00',
    '{ticket_source("PGU-9161", "in_progress", "ops")}'::jsonb
);
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '10 minutes',
    last_activity_at = clock_timestamp() - interval '10 minutes',
    awaiting_role = 'director',
    awaiting_since_at = clock_timestamp() - interval '1 minute'
WHERE ticket_id = 'PGU-9161';
DELETE FROM ticket_board.ticket_notification_queue;
""",
            )
            active_wait_precondition = json.loads(
                psql(
                    admin_conn,
                    """
SELECT jsonb_build_object(
    'state_rows', count(*)::int,
    'owner_role', max(ticket_board.transition_target_role(t.state, t.assignee)),
    'awaiting_role', max(ns.awaiting_role),
    'awaiting_active', bool_or(ticket_board.ticket_awaiting_role_is_active(
        ns.awaiting_role,
        ns.awaiting_since_at,
        clock_timestamp()
    ))
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-9161';
""",
                )
            )
            assert active_wait_precondition == {
                "state_rows": 1,
                "owner_role": "ops",
                "awaiting_role": "director",
                "awaiting_active": True,
            }, active_wait_precondition

            active_wait_returned = psql(
                listener_conn,
                """
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('ops', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
""",
            )
            assert active_wait_returned == "0", active_wait_returned
            active_wait_rows = json.loads(
                psql(
                    admin_conn,
                    """
SELECT jsonb_build_object(
    'queue_count', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue
        WHERE ticket_id = 'PGU-9161'
    ),
    'trace_count', (
        SELECT count(*)::int
        FROM ticket_board.notification_trace
        WHERE ticket_id = 'PGU-9161'
          AND kind IN ('idle_reminder', 'escalation')
    )
)::text;
""",
                )
            )
            assert active_wait_rows == {"queue_count": 0, "trace_count": 0}, active_wait_rows

            psql(
                admin_conn,
                """
UPDATE ticket_board.ticket_notification_state
SET awaiting_role = '',
    awaiting_since_at = NULL
WHERE ticket_id = 'PGU-9161';
DELETE FROM ticket_board.ticket_notification_queue;
""",
            )
            empty_wait_returned = psql(
                listener_conn,
                """
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('ops', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
""",
            )
            assert empty_wait_returned == "1", empty_wait_returned
            empty_wait_row = json.loads(
                psql(
                    admin_conn,
                    """
SELECT jsonb_build_object(
    'target_role', target_role,
    'kind', kind,
    'message', message
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-9161'
ORDER BY id DESC
LIMIT 1;
""",
                )
            )
            assert empty_wait_row == {
                "target_role": "ops",
                "kind": "idle_reminder",
                "message": "PGU-9161 is waiting in your in_progress queue. Advance it or hand it off. If you cannot move it forward, tell the director what is wrong.",
            }, empty_wait_row
        finally:
            run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], capture=False)

    print("ticket_board_awaiting_role_idle_turn_end_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
