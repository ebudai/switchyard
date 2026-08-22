#!/usr/bin/env python3
"""Regression test: API blocked_by contains only unresolved blockers."""

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
        "ticket_board_unresolved_blockers_api_test: psycopg3 is required; "
        "install python-psycopg or run from a venv with psycopg installed"
    )

from scripts.ticket_board.app import TicketBoardApp


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "user", "ops", "app", "audit", "inspector", "perf", "research", "main"]
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


def insert_ticket(conn: str, ticket_id: str, title: str) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation,
    created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', 'analysis', 'unassigned', '',
    '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
    '{ticket_source(ticket_id, title, "analysis", "unassigned")}'::jsonb
);
""",
    )


def sql_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def create_roles(conn: str) -> None:
    psql(conn, "\n".join(f"CREATE ROLE {sql_ident(role)} LOGIN;" for role in PANE_ROLES))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-unresolved-blockers-api.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        dbname = "pgu_unresolved_blockers_test"
        admin_conn = conninfo(socket_dir, port, dbname)

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            create_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            insert_ticket(admin_conn, "PGU-1", "Resolved blocker")
            insert_ticket(admin_conn, "PGU-2", "Unresolved blocker")
            insert_ticket(admin_conn, "PGU-3", "Blocked ticket")
            psql(
                admin_conn,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
VALUES ('PGU-3', 'PGU-1', 0, false);
""",
            )

            app = TicketBoardApp(
                frames,
                assets,
                database_url=conninfo(socket_dir, port, dbname, SERVICE_ROLE),
            )
            unresolved = app.get_ticket("PGU-3")
            assert unresolved["blocked_by"] == ["PGU-1"], unresolved
            assert unresolved["blockers"] == [{"id": "PGU-1", "resolved": False}], unresolved

            psql(admin_conn, "UPDATE ticket_board.ticket_blockers SET resolved = true WHERE ticket_id = 'PGU-3';")
            resolved_only = app.get_ticket("PGU-3")
            assert resolved_only["blocked_by"] == [], resolved_only
            assert resolved_only["blockers"] == [{"id": "PGU-1", "resolved": True}], resolved_only

            psql(
                admin_conn,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
VALUES ('PGU-3', 'PGU-2', 1, false);
""",
            )
            mixed = app.get_ticket("PGU-3")
            assert mixed["blocked_by"] == ["PGU-2"], mixed
            assert mixed["blockers"] == [
                {"id": "PGU-1", "resolved": True},
                {"id": "PGU-2", "resolved": False},
            ], mixed

            source = json.loads(psql(admin_conn, "SELECT ticket_board.build_ticket_source_json('PGU-3')::text;"))
            assert source["blocked_by"] == ["PGU-2"], source
            assert source["blockers"] == mixed["blockers"], source
        finally:
            run(["pg_ctl", "-D", str(data_dir), "-w", "stop"], capture=False)

    print("ticket_board_unresolved_blockers_api_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
