#!/usr/bin/env python3
"""Regression tests for the guarded collation maintenance helper."""

from __future__ import annotations

import subprocess
import socket
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ticket-board-refresh-collation-version"


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


def psql(socket_dir: Path, port: int, database: str, sql: str) -> str:
    return run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", "-d", database, "-tA"],
        input_text=sql,
    ).stdout.strip()


def test_plan_only_prints_locking_repair_sql_without_connecting() -> None:
    result = subprocess.run(
        [
            str(SCRIPT),
            "--plan-only",
            "--database",
            "postgres",
            "--database",
            "pgu",
            "--expected-ticket-count",
            "574",
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert 'REINDEX DATABASE "postgres";' in result.stdout
    assert 'ALTER DATABASE "postgres" REFRESH COLLATION VERSION;' in result.stdout
    assert 'REINDEX DATABASE "pgu";' in result.stdout
    assert 'ALTER DATABASE "pgu" REFRESH COLLATION VERSION;' in result.stdout
    assert result.stderr == ""


def test_default_mode_is_dry_run() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "EXECUTE=0" in source
    assert "dry run only; pass --execute during a maintenance window" in source
    assert "REINDEX DATABASE" in source
    assert "ticket count changed during repair" in source


def test_execute_handles_database_without_ticket_board_schema() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-collation.") as tmp:
        root = Path(tmp)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", "maintenance_no_schema"])
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", "board_with_schema"])
            psql(
                socket_dir,
                port,
                "board_with_schema",
                """
CREATE SCHEMA ticket_board;
CREATE TABLE ticket_board.tickets (id text PRIMARY KEY);
INSERT INTO ticket_board.tickets (id) VALUES ('TEST-1'), ('TEST-2');
""",
            )

            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--host",
                    str(socket_dir),
                    "--port",
                    str(port),
                    "--user",
                    "postgres",
                    "--database",
                    "maintenance_no_schema",
                    "--database",
                    "board_with_schema",
                    "--execute",
                    "--expected-ticket-count",
                    "2",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            assert "relation \"ticket_board.tickets\" does not exist" not in result.stderr
            assert "board_with_schema ticket count preserved at 2" in result.stderr
            assert psql(socket_dir, port, "board_with_schema", "SELECT count(*) FROM ticket_board.tickets;") == "2"
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def main() -> int:
    test_plan_only_prints_locking_repair_sql_without_connecting()
    test_default_mode_is_dry_run()
    test_execute_handles_database_without_ticket_board_schema()
    print("ticket_board_collation_maintenance_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
