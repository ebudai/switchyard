#!/usr/bin/env python3
"""Regression tests for notification_trace retention and lookup indexing."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"


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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-trace-retention.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_trace_retention_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, "CREATE ROLE ticket_board_service;")
            psql(conninfo, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))

            indexdef = psql(
                conninfo,
                """
SELECT indexdef
FROM pg_indexes
WHERE schemaname = 'ticket_board'
  AND tablename = 'notification_trace'
  AND indexname = 'notification_trace_send_lookup_idx';
""",
            )
            assert "ticket_id" in indexdef
            assert "target_role" in indexdef
            assert "ticket_state_at_event" in indexdef
            assert "WHERE ((kind = 'transition'::text) AND (event = 'send'::text))" in indexdef

            source = json.dumps(
                {
                    "id": "PGU-31801",
                    "title": "Trace retention",
                    "body": "",
                    "state": "in_progress",
                    "assignee": "ops",
                    "comments": [],
                    "created": "2026-07-12T00:00:00+00:00",
                    "updated": "2026-07-12T00:00:00+00:00",
                },
                sort_keys=True,
            ).replace("'", "''")
            psql(
                conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-31801', 'Trace retention', '', 'in_progress', 'ops', 'Ready.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00', '{source}'::jsonb
);
DELETE FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-31801';
INSERT INTO ticket_board.notification_trace (ts, ticket_id, target_role, kind, event)
VALUES
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'claim'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'listener_claim'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'requeue'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'gate_defer'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'enqueue'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'send'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'ack'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'drop'),
    (clock_timestamp() - interval '2 hours', 'PGU-31801', 'ops', 'transition', 'send_failed'),
    (clock_timestamp(), 'PGU-31801', 'ops', 'transition', 'gate_defer');
""",
            )

            deleted = psql(conninfo, "SELECT ticket_board.prune_notification_trace(clock_timestamp(), interval '1 hour');")
            assert deleted == "4", deleted
            remaining = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(event ORDER BY event)::text
FROM ticket_board.notification_trace
WHERE ticket_id = 'PGU-31801';
""",
                )
            )
            assert remaining == ["ack", "drop", "enqueue", "gate_defer", "send", "send_failed"], remaining

            explain_active_work = psql(
                conninfo,
                """
SET enable_seqscan = off;
EXPLAIN (COSTS OFF)
SELECT max(trace.ts) AS last_sent_at
FROM ticket_board.notification_trace trace
WHERE trace.ticket_id = 'PGU-31801'
  AND trace.target_role = 'ops'
  AND trace.kind = 'transition'
  AND trace.event = 'send'
  AND trace.ticket_state_at_event = 'in_progress';
""",
            )
            assert "notification_trace_send_lookup_idx" in explain_active_work, explain_active_work
            assert "Index Only Scan" in explain_active_work, explain_active_work

            explain_signature = psql(
                conninfo,
                """
SET enable_seqscan = off;
EXPLAIN (COSTS OFF)
SELECT max(nt.id)::text
FROM ticket_board.notification_trace nt
WHERE nt.ticket_id = 'PGU-31801'
  AND nt.kind = 'transition'
  AND nt.event = 'send';
""",
            )
            assert "notification_trace_send_lookup_idx" in explain_signature, explain_signature
            assert "Index Only Scan" in explain_signature, explain_signature
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("ticket_board_notification_trace_retention_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
