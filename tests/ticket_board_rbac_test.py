#!/usr/bin/env python3
"""Regression test: ticket-board PostgreSQL RBAC roles and grants."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
EXPECTED_ROLES = ["director", "ops", "app", "audit", "perf", "research", "main", "agent", "ticket_board_service"]
WORKER_ROLES = ["ops", "app", "audit", "perf", "research", "main", "ticket_board_service"]
ROLE_SQL_ARRAY = "ARRAY['director','ops','app','audit','perf','research','main','agent','ticket_board_service']"


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
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo], input_text=sql).stdout.strip()


def psql_error(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        raise AssertionError(f"SQL unexpectedly succeeded: {sql}")
    return proc.stderr + proc.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-rbac.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_rbac_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(conninfo, RBAC_PATH.read_text(encoding="utf-8"))
            psql(conninfo, RBAC_PATH.read_text(encoding="utf-8"))

            role_rows = json.loads(
                psql(
                    conninfo,
                    f"""
SELECT jsonb_object_agg(rolname, jsonb_build_object('can_login', rolcanlogin, 'password_is_null', rolpassword IS NULL))::text
FROM pg_authid
WHERE rolname = ANY({ROLE_SQL_ARRAY});
""",
                )
            )
            assert sorted(role_rows) == sorted(EXPECTED_ROLES), role_rows
            for role in EXPECTED_ROLES:
                assert role_rows[role] == {"can_login": True, "password_is_null": True}, (role, role_rows[role])

            schema_usage = json.loads(
                psql(
                    conninfo,
                    f"""
SELECT jsonb_object_agg(role_name, has_schema_privilege(role_name, 'ticket_board', 'USAGE'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
                )
            )
            assert all(schema_usage.values()), schema_usage

            select_privileges = json.loads(
                psql(
                    conninfo,
                    f"""
SELECT jsonb_object_agg(role_name, has_table_privilege(role_name, 'ticket_board.tickets', 'SELECT'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
                )
            )
            assert all(select_privileges.values()), select_privileges

            manual_update = json.loads(
                psql(
                    conninfo,
                    f"""
SELECT jsonb_object_agg(role_name, has_column_privilege(role_name, 'ticket_board.tickets', 'manually_controlled', 'UPDATE'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
                )
            )
            assert manual_update["director"] is True, manual_update
            for role in WORKER_ROLES:
                assert manual_update[role] is False, (role, manual_update)
            manual_update_grantees = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(grantee ORDER BY grantee)::text
FROM information_schema.column_privileges
WHERE table_schema = 'ticket_board'
  AND table_name = 'tickets'
  AND column_name = 'manually_controlled'
  AND privilege_type = 'UPDATE';
""",
                )
            )
            assert manual_update_grantees == ["director"], manual_update_grantees

            worker_columns = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
  'app_state', has_column_privilege('app', 'ticket_board.tickets', 'state', 'UPDATE'),
  'app_audit_signoff', has_column_privilege('app', 'ticket_board.tickets', 'audit_signoff', 'UPDATE'),
  'audit_audit_signoff', has_column_privilege('audit', 'ticket_board.tickets', 'audit_signoff', 'UPDATE'),
  'audit_assignee', has_column_privilege('audit', 'ticket_board.tickets', 'assignee', 'UPDATE'),
  'audit_needs_eric_signoff', has_column_privilege('audit', 'ticket_board.tickets', 'needs_eric_signoff', 'UPDATE'),
  'audit_blocked_reason', has_column_privilege('audit', 'ticket_board.tickets', 'blocked_reason', 'UPDATE'),
  'service_state', has_column_privilege('ticket_board_service', 'ticket_board.tickets', 'state', 'UPDATE'),
  'service_manual', has_column_privilege('ticket_board_service', 'ticket_board.tickets', 'manually_controlled', 'UPDATE'),
  'agent_state', has_column_privilege('agent', 'ticket_board.tickets', 'state', 'UPDATE'),
  'app_comment_insert', has_table_privilege('app', 'ticket_board.ticket_comments', 'INSERT')
)::text;
""",
                )
            )
            assert worker_columns == {
                "app_state": True,
                "app_audit_signoff": False,
                "audit_audit_signoff": True,
                "audit_assignee": True,
                "audit_needs_eric_signoff": True,
                "audit_blocked_reason": True,
                "service_state": True,
                "service_manual": False,
                "agent_state": True,
                "app_comment_insert": True,
            }, worker_columns

            psql(
                conninfo,
                """
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, created_text, updated_text, source_json
) VALUES (
    'PGU-1', 'RBAC fixture', '', 'audit', 'audit',
    '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00',
    '{"id":"PGU-1","title":"RBAC fixture","body":"","state":"audit","assignee":"audit","comments":[],"created":"2026-07-10T00:00:00+00:00","updated":"2026-07-10T00:00:00+00:00"}'::jsonb
);
""",
            )
            psql(
                conninfo,
                """
SET ROLE audit;
UPDATE ticket_board.tickets
SET assignee = 'ops',
    needs_eric_signoff = true,
    blocked_reason = 'Waiting on director merge.'
WHERE id = 'PGU-1';
RESET ROLE;
""",
            )
            audit_column_update = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'assignee', assignee,
    'needs_eric_signoff', needs_eric_signoff,
    'blocked_reason', blocked_reason
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-1';
""",
                )
            )
            assert audit_column_update == {
                "assignee": "ops",
                "needs_eric_signoff": True,
                "blocked_reason": "Waiting on director merge.",
            }, audit_column_update
            psql(
                conninfo,
                """
SET ROLE audit;
UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-1';
RESET ROLE;
""",
            )
            audit_update = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'assignee', assignee,
    'needs_eric_signoff', needs_eric_signoff,
    'blocked_reason', blocked_reason,
    'state', state
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-1';
""",
                )
            )
            assert audit_update["state"] == "analysis", audit_update
            assert audit_update["assignee"] in {"ops", "unassigned"}, audit_update
            assert audit_update["needs_eric_signoff"] is True, audit_update
            assert audit_update["blocked_reason"] == "Waiting on director merge.", audit_update
            assert "permission denied" in psql_error(
                conninfo,
                """
SET ROLE audit;
UPDATE ticket_board.tickets SET manually_controlled = true WHERE id = 'PGU-1';
""",
            )
            assert "permission denied" in psql_error(
                conninfo,
                """
SET ROLE app;
UPDATE ticket_board.tickets SET audit_signoff = true WHERE id = 'PGU-1';
""",
            )
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_rbac_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
