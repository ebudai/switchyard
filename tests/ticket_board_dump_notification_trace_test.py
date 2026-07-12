#!/usr/bin/env python3
"""Regression: notification trace dump handles SQL_ASCII text bytes."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
DUMP_PATH = ROOT / "scripts" / "ticket_board" / "dump_notification_trace.py"


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
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo], input_text=sql).stdout.strip()


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    return json.dumps(
        {
            "id": ticket_id,
            "title": title,
            "body": "",
            "state": state,
            "assignee": assignee,
            "comments": [],
            "created": "2026-07-12T00:00:00+00:00",
            "updated": "2026-07-12T00:00:00+00:00",
        },
        sort_keys=True,
    ).replace("'", "''")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-trace-dump.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_trace_dump_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname} user=postgres"

        run(
            [
                "initdb",
                "-D",
                str(data_dir),
                "-A",
                "trust",
                "--no-locale",
                "--encoding=SQL_ASCII",
                "--username=postgres",
            ]
        )
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            assert psql(conninfo, "SHOW client_encoding;") == "SQL_ASCII"
            psql(
                conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-970', 'Trace dump', '', 'in_progress', 'ops', 'Ready.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    '{ticket_source("PGU-970", "Trace dump", "in_progress", "ops")}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-970',
    42,
    'ops',
    'transition',
    'enqueue',
    'busy',
    'region changed',
    'abc123',
    '{{"nested": {{"message": "hello"}}, "items": ["one", "two"]}}'::jsonb
);
""",
            )
            json_proc = run(["python3", str(DUMP_PATH), "--database", conninfo, "--ticket-id", "PGU-970", "--json"])
            dumped = json.loads(json_proc.stdout)
            assert dumped[0]["ticket_id"] == "PGU-970", dumped
            assert dumped[0]["event"] == "enqueue", dumped
            assert dumped[0]["ticket_state_at_event"] == "in_progress", dumped
            assert dumped[0]["detail"]["nested"]["message"] == "hello", dumped

            text_proc = run(["python3", str(DUMP_PATH), "--database", conninfo, "--ticket-id", "PGU-970"])
            assert "ticket=PGU-970" in text_proc.stdout, text_proc.stdout
            assert "event=enqueue" in text_proc.stdout, text_proc.stdout
            assert "ticket_state=in_progress" in text_proc.stdout, text_proc.stdout
            assert "b'" not in text_proc.stdout, text_proc.stdout
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_dump_notification_trace_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
