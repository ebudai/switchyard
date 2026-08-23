#!/usr/bin/env python3
"""Regression test: provisioned projects can seed and use a five-stage workflow."""

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

from scripts.ticket_board.project_provision import build_plan, render_workflow_sql


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
EXPECTED_STAGES = [
    {"name": "draft", "display_label": "Draft", "rank": 0, "owner_roles": []},
    {"name": "analysis", "display_label": "Triage", "rank": 1, "owner_roles": ["director"]},
    {
        "name": "in_progress",
        "display_label": "Implementation",
        "rank": 2,
        "owner_roles": ["builder"],
    },
    {"name": "audit", "display_label": "Audit", "rank": 3, "owner_roles": ["audit"]},
    {"name": "director_review", "display_label": "Final Sign-Off", "rank": 4, "owner_roles": ["director"]},
]


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


def service_call(conn: str, caller_role: str, sql: str) -> str:
    return psql(
        conn,
        f"""
SELECT set_config('ticket_board.caller_role', '{caller_role}', false);
{sql}
""",
    ).splitlines()[-1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-project-workflow.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "project_workflow_test"
        admin_conn = conninfo(socket_dir, port, dbname)
        service_conn = conninfo(socket_dir, port, dbname, "ticket_board_service")

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(admin_conn, render_workflow_sql(build_plan(project="otto", owner_user="otto-agent", implementer_roles=["builder"])))
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            stages = json.loads(
                psql(
                    admin_conn,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'name', name,
    'display_label', display_label,
    'rank', rank,
    'owner_roles', owner_roles
) ORDER BY rank)::text
FROM ticket_board.workflow_stages;
""",
                )
            )
            assert stages == EXPECTED_STAGES, stages
            assert psql(admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions;") == "7"
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('builder');") == "t"
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('ops');") == "f"
            reachable = json.loads(
                psql(
                    admin_conn,
                    """
WITH RECURSIVE walk(stage) AS (
    VALUES ('draft'::text)
    UNION
    SELECT wt.to_stage
    FROM ticket_board.workflow_transitions wt
    JOIN walk ON walk.stage = wt.from_stage
)
SELECT jsonb_agg(ws.name ORDER BY ws.rank)::text
FROM ticket_board.workflow_stages ws
JOIN walk ON walk.stage = ws.name;
""",
                )
            )
            assert reachable == [stage["name"] for stage in EXPECTED_STAGES], reachable

            ticket_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow ticket', 'Body', 'draft');
""",
            )
            assert ticket_id == "OTTO-1", ticket_id
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "draft"

            service_call(service_conn, "director", f"SELECT ticket_board.release_draft('{ticket_id}');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "analysis:director"

            service_call(service_conn, "director", f"SELECT ticket_board.route('{ticket_id}', 'in_progress', 'builder');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "in_progress:builder"

            service_call(service_conn, "builder", f"SELECT ticket_board.submit_to_audit('{ticket_id}', 'abcdef1');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "audit:audit"

            service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{ticket_id}', 'Audit verified.');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "director_review:director"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_project_workflow_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
