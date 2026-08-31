#!/usr/bin/env python3
"""Regression: missed pg_notify transitions are replayed by listener reconcile."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import TicketBoardNotifyListener


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


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-07-11T00:00:00+00:00",
        "updated": "2026-07-11T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def create_pane_roles(conninfo: str) -> None:
    psql(
        conninfo,
        """
CREATE ROLE director LOGIN;
CREATE ROLE "user" LOGIN;
CREATE ROLE ops LOGIN;
CREATE ROLE app LOGIN;
CREATE ROLE audit LOGIN;
CREATE ROLE inspector LOGIN;
CREATE ROLE perf LOGIN;
CREATE ROLE research LOGIN;
CREATE ROLE main LOGIN;
""",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-reconcile.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_reconcile_test"
        admin_conninfo = f"host={socket_dir} port={port} dbname={dbname} user=postgres"
        listener_conninfo = f"host={socket_dir} port={port} dbname={dbname} user=ticket_board_listener"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            create_pane_roles(admin_conninfo)
            psql(admin_conninfo, RBAC_PATH.read_text(encoding="utf-8"))
            psql(
                admin_conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-206', 'Locked head', '', 'backlog', 'ops', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-206", "Locked head", "backlog", "ops")}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-206';
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-207', 'Durable reconcile', '', 'backlog', 'app', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-207", "Durable reconcile", "backlog", "app")}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-207';
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.ticket_notification_state
SET last_activity_at = clock_timestamp() - interval '1 hour',
    entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = clock_timestamp() - interval '40 minutes',
    nudge_count = 3
WHERE ticket_id IN ('PGU-206', 'PGU-207');
""",
            )
            nudged = psql(
                listener_conninfo,
                """
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object(
        'ops', (clock_timestamp() - interval '1 hour')::text,
        'app', (clock_timestamp() - interval '1 hour')::text
    ),
    clock_timestamp(),
    interval '45 seconds',
    interval '30 minutes',
    2
);
""",
            )
            assert nudged == "2", nudged
            psql(
                admin_conninfo,
                """
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = CASE ticket_id
    WHEN 'PGU-206' THEN clock_timestamp() - interval '10 seconds'
    WHEN 'PGU-207' THEN clock_timestamp()
    ELSE next_attempt_at
END
WHERE ticket_id IN ('PGU-206', 'PGU-207');
""",
            )
            before = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-207';",
            )
            assert before == "1", before

            sent: list[tuple[str, str]] = []
            with psycopg.connect(admin_conninfo, autocommit=False) as lock_conn:
                lock_conn.execute(
                    """
SELECT id
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-206'
FOR UPDATE
"""
                )
                listener = TicketBoardNotifyListener(
                    conninfo=listener_conninfo,
                    sender=lambda target, message: sent.append((target, message)),
                    activity_gate=lambda _target: False,
                    poll_seconds=0,
                )
                delivered = listener.listen_once(max_notifications=1)
                lock_conn.rollback()

            assert delivered == 1
            assert sent == [
                (
                    "pgu-director:0.0",
                    "PRIORITY PGU-207 -- Durable reconcile appears stuck for app; check/reassign",
                )
            ]
            after = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-207';",
            )
            assert after == "0", after

            psql(
                admin_conninfo,
                f"""
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-226', 'Stale cancelled nudge', '', 'in_progress', 'ops', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-226", "Stale cancelled nudge", "in_progress", "ops")}'::jsonb
);
SELECT ticket_board.enqueue_notification(
    'PGU-226',
    'nudge',
    'ops',
    'New ticket for you: PGU-226 -- Stale cancelled nudge',
    jsonb_build_object(
        'kind', 'nudge',
        'id', 'PGU-226',
        'title', 'Stale cancelled nudge',
        'state', 'in_progress',
        'assignee', 'ops',
        'target_role', 'ops',
        'message', 'New ticket for you: PGU-226 -- Stale cancelled nudge'
    ),
    'test-stale-cancelled'
);
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-226',
    0,
    'director',
    '2026-07-11T00:10:00+00:00',
    'Cancelled before stale nudge delivery.',
    '{{"who":"director","ts":"2026-07-11T00:10:00+00:00","text":"Cancelled before stale nudge delivery."}}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-226';
COMMIT;
DELETE FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-226' AND kind = 'transition';
""",
            )
            sent.clear()
            cancelled_drop = TicketBoardNotifyListener(
                conninfo=listener_conninfo,
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
            ).listen_once(max_notifications=1)
            assert cancelled_drop == 0, cancelled_drop
            assert sent == []
            cancelled_queue = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-226';",
            )
            assert cancelled_queue == "0", cancelled_queue

            psql(
                admin_conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-227', 'Stale picked-up nudge', '', 'in_progress', 'research', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-227", "Stale picked-up nudge", "in_progress", "research")}'::jsonb
);
SELECT ticket_board.enqueue_notification(
    'PGU-227',
    'nudge',
    'ops',
    'New ticket for you: PGU-227 -- Stale picked-up nudge',
    jsonb_build_object(
        'kind', 'nudge',
        'id', 'PGU-227',
        'title', 'Stale picked-up nudge',
        'state', 'in_progress',
        'assignee', 'ops',
        'target_role', 'ops',
        'message', 'New ticket for you: PGU-227 -- Stale picked-up nudge'
    ),
    'test-stale-picked-up'
);
UPDATE ticket_board.tickets SET state = 'audit', commit_hash = 'abcdef1' WHERE id = 'PGU-227';
DELETE FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-227' AND kind = 'transition';
""",
            )
            sent.clear()
            picked_up_drop = TicketBoardNotifyListener(
                conninfo=listener_conninfo,
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
            ).listen_once(max_notifications=1)
            assert picked_up_drop == 0, picked_up_drop
            assert sent == []
            picked_up_queue = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-227';",
            )
            assert picked_up_queue == "0", picked_up_queue

            psql(
                admin_conninfo,
                f"""
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-228', 'Still stuck escalation', '', 'audit', 'audit', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-228", "Still stuck escalation", "audit", "audit")}'::jsonb
);
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id <> 'PGU-228';
UPDATE ticket_board.ticket_notification_state
SET last_activity_at = clock_timestamp() - interval '1 hour',
    entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = clock_timestamp() - interval '10 minutes',
    nudge_count = 3
WHERE ticket_id = 'PGU-228';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            escalation_row = json.loads(
                psql(
                    listener_conninfo,
                    """
SELECT jsonb_build_object(
    'kind', kind,
    'target_role', target_role,
    'message', message,
    'payload_kind', payload->>'kind'
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-228'
  AND kind = 'escalation';
""",
                )
            )
            assert escalation_row == {
                "kind": "escalation",
                "target_role": "director",
                "message": "PRIORITY PGU-228 -- Still stuck escalation appears stuck for audit; check/reassign",
                "payload_kind": "escalation",
            }, escalation_row
            psql(
                admin_conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-228'
  AND kind = 'transition';
""",
            )

            sent.clear()
            escalation_delivered = TicketBoardNotifyListener(
                conninfo=listener_conninfo,
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
            ).listen_once(max_notifications=1)
            assert escalation_delivered == 1, escalation_delivered
            assert sent == [
                (
                    "pgu-director:0.0",
                    "PRIORITY PGU-228 -- Still stuck escalation appears stuck for audit; check/reassign",
                )
            ]
            escalation_queue = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-228';",
            )
            assert escalation_queue == "0", escalation_queue
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_notify_reconcile_postgres_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
