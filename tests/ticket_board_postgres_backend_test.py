#!/usr/bin/env python3
"""Regression test: TicketBoardApp can read/write tickets through PostgreSQL."""

from __future__ import annotations

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


def psql(conninfo: str, sql: str) -> None:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)


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
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))

            app = TicketBoardApp(root / "json-unused", frames, assets, store_backend="postgres", database_url=conninfo)
            before_signature = app.store_signature()
            created = app.create_ticket(
                title="Postgres create",
                body="Created through TicketBoardApp.",
                screenshot=None,
                assignee="ops",
                needs_eric_signoff=False,
            )
            assert created["id"] == "PGU-1", created
            assert app.verify_created_ticket_persisted(created, before_signature) != before_signature
            assert not (root / "json-unused").exists(), "postgres backend should not create the JSON store directory"

            ready = app.update_ticket(
                "PGU-1",
                {
                    "implementation": "Use the database backend.",
                    "state": "ready",
                },
            )
            assert ready["state"] == "ready", ready
            in_progress = app.update_ticket("PGU-1", {"state": "in_progress"})
            assert in_progress["state"] == "in_progress", in_progress

            blocked = app.create_ticket(
                title="Blocked child",
                body="Needs PGU-1 first.",
                screenshot=None,
                assignee="ops",
                needs_eric_signoff=False,
                blocked_by=["PGU-1"],
                blocked_reason="Waiting for PGU-1.",
            )
            assert blocked["blocked_by"] == ["PGU-1"], blocked
            assert blocked["blocked_reason"] == "Waiting for PGU-1.", blocked

            cancelled = app.update_ticket(
                blocked["id"],
                {
                    "state": "cancelled",
                    "comment": {"who": "director", "text": "Covered by PGU-1."},
                },
            )
            assert cancelled["state"] == "cancelled", cancelled
            assert cancelled["comments"][-1]["text"] == "Covered by PGU-1.", cancelled

            audit = app.create_ticket_record(
                title="Audit auto-advance",
                body="",
                screenshot=None,
                screenshots=None,
                assignee="audit",
                state="audit",
                blocked_by=[],
                implementation="Implemented.",
                audit_prompt="Check the backend.",
                audit_signoff=False,
                needs_eric_signoff=False,
                eric_signoff=False,
                comments=[],
            )
            advanced = app.update_ticket(audit["id"], {"audit_signoff": True})
            assert advanced["state"] == "director_review", advanced
            assert advanced["assignee"] == "director", advanced

            tickets, errors = app.list_tickets()
            assert errors == [], errors
            assert {ticket["id"] for ticket in tickets} == {"PGU-1", "PGU-2", "PGU-3"}, tickets
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_postgres_backend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
