#!/usr/bin/env python3
"""Regression: missed pg_notify transitions are replayed by listener reconcile."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

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
CREATE ROLE eric LOGIN;
CREATE ROLE ops LOGIN;
CREATE ROLE app LOGIN;
CREATE ROLE audit LOGIN;
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
    'PGU-207', 'Durable reconcile', '', 'analysis', 'ops', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    '{ticket_source("PGU-207", "Durable reconcile", "analysis", "ops")}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'ready' WHERE id = 'PGU-207';
""",
            )
            before = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.pending_transition_notifications() WHERE ticket_id = 'PGU-207';",
            )
            assert before == "1", before

            sent: list[tuple[str, str]] = []
            listener = TicketBoardNotifyListener(
                conninfo=listener_conninfo,
                sender=lambda target, message: sent.append((target, message)),
                poll_seconds=0,
            )
            delivered = listener.listen_once(max_notifications=1)

            assert delivered == 1
            assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-207 -- Durable reconcile")]
            after = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.pending_transition_notifications() WHERE ticket_id = 'PGU-207';",
            )
            assert after == "0", after
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_notify_reconcile_postgres_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
