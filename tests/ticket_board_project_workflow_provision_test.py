#!/usr/bin/env python3
"""Regression test: provisioned projects can seed and use a closeable workflow."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.project_provision import build_plan, render_database_sql, render_workflow_sql


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
MIGRATION_RUNNER = ROOT / "scripts" / "ticket-board-migrate"
EXPECTED_STAGES = [
    {"name": "draft", "display_label": "Draft", "rank": 0, "owner_roles": ["designer"], "is_terminal": False},
    {"name": "analysis", "display_label": "Triage", "rank": 1, "owner_roles": ["director"], "is_terminal": False},
    {
        "name": "in_progress",
        "display_label": "Implementation",
        "rank": 2,
        "owner_roles": ["main", "app"],
        "is_terminal": False,
    },
    {"name": "audit", "display_label": "Audit", "rank": 3, "owner_roles": ["audit"], "is_terminal": False},
    {
        "name": "director_review",
        "display_label": "Final Sign-Off",
        "rank": 4,
        "owner_roles": ["director"],
        "is_terminal": False,
    },
    {"name": "done", "display_label": "Done", "rank": 9, "owner_roles": [], "is_terminal": True},
    {"name": "cancelled", "display_label": "Cancelled", "rank": 10, "owner_roles": [], "is_terminal": True},
]
EXPECTED_AUDITLESS_STAGES = [
    {"name": "draft", "display_label": "Draft", "rank": 0, "owner_roles": [], "is_terminal": False},
    {"name": "analysis", "display_label": "Triage", "rank": 1, "owner_roles": ["director"], "is_terminal": False},
    {
        "name": "in_progress",
        "display_label": "Implementation",
        "rank": 2,
        "owner_roles": ["app"],
        "is_terminal": False,
    },
    {
        "name": "director_review",
        "display_label": "Final Sign-Off",
        "rank": 3,
        "owner_roles": ["director"],
        "is_terminal": False,
    },
    {"name": "done", "display_label": "Done", "rank": 9, "owner_roles": [], "is_terminal": True},
    {"name": "cancelled", "display_label": "Cancelled", "rank": 10, "owner_roles": [], "is_terminal": True},
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


def run_migrations(conn: str, *, migrations_dir: Path | None = None) -> None:
    env = {**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": conn}
    if migrations_dir is not None:
        env["TICKET_BOARD_MIGRATIONS_DIR"] = str(migrations_dir)
    proc = subprocess.run(
        [str(MIGRATION_RUNNER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)


def bootstrap_database_roles(conn: str) -> None:
    psql(
        conn,
        """
DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'ticket_board_service',
        'ticket_board_listener',
        'director',
        'user',
        'ops',
        'app',
        'audit',
        'inspector',
        'perf',
        'research',
        'main'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', role_name);
        END IF;
    END LOOP;
END;
$$;
""",
    )


def psql_call(conn: str, caller_role: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=f"""
SELECT set_config('ticket_board.caller_role', '{caller_role}', false);
{sql}
""",
        text=True,
        capture_output=True,
        check=False,
    )


def service_call(conn: str, caller_role: str, sql: str) -> str:
    proc = psql_call(conn, caller_role, sql)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip().splitlines()[-1]


def service_call_without_shadow_warning(conn: str, caller_role: str, sql: str) -> str:
    proc = psql_call(conn, caller_role, sql)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    assert "shadow mismatch" not in proc.stderr, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def service_call_fails(conn: str, caller_role: str, sql: str) -> str:
    proc = psql_call(conn, caller_role, sql)
    if proc.returncode == 0:
        raise AssertionError(f"expected service call to fail, got stdout:\n{proc.stdout}")
    return proc.stderr


def guarded_migrations_dir(root: Path) -> Path:
    migrations_dir = root / "migrations"
    migrations_dir.mkdir(parents=True)
    source_migrations = ROOT / "scripts" / "ticket_board" / "migrations"
    for migration in source_migrations.glob("*.sql"):
        shutil.copy2(migration, migrations_dir / migration.name)
    (root / "schema.sql").write_text(SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (migrations_dir / "270_ready_to_in_progress.sql").write_text(
        """
DO $$
BEGIN
    RAISE EXCEPTION 'legacy 270 migration should be backfilled, not executed';
END;
$$;
""".lstrip(),
        encoding="utf-8",
    )
    (migrations_dir / "pgu999_provision_probe.sql").write_text(
        """
CREATE TABLE ticket_board.provision_probe (
    id integer PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO ticket_board.provision_probe (id, value) VALUES (1, 'migration-ran');
""".lstrip(),
        encoding="utf-8",
    )
    return migrations_dir


def apply_generated_provisioning_database_sequence(
    *,
    root: Path,
    socket_dir: Path,
    port: int,
    project: str,
    database: str,
    precreate_empty_database: bool,
) -> None:
    postgres_conn = conninfo(socket_dir, port, "postgres")
    plan = build_plan(project=project, owner_user=f"{project}-agent", database=database)
    if precreate_empty_database:
        run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", database])

    psql(postgres_conn, render_database_sql(plan))
    admin_conn = conninfo(socket_dir, port, database)
    migrations_dir = guarded_migrations_dir(root / database)
    psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
    run_migrations(admin_conn, migrations_dir=migrations_dir)
    psql(admin_conn, render_workflow_sql(plan))
    psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

    assert psql(admin_conn, "SELECT EXISTS (SELECT 1 FROM ticket_board.schema_migrations WHERE name = 'schema.sql');") == "t"
    assert psql(admin_conn, "SELECT EXISTS (SELECT 1 FROM ticket_board.schema_migrations WHERE name = '270_ready_to_in_progress.sql');") == "t"
    assert psql(admin_conn, "SELECT value FROM ticket_board.provision_probe WHERE id = 1;") == "migration-ran"
    assert int(
        psql(
            admin_conn,
            """
SELECT count(*)
FROM information_schema.tables
WHERE table_schema = 'ticket_board';
""",
        )
    ) > 0
    service_conn = conninfo(socket_dir, port, database, "ticket_board_service")
    ticket_id = service_call(
        service_conn,
        "director",
        f"""
SELECT set_config('ticket_board.ticket_prefix', '{project.upper()}', false);
SELECT ticket_board.create_ticket('Provisioning sequence probe', 'Body', 'analysis');
""",
    )
    assert ticket_id == f"{project.upper()}-1", ticket_id


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
            apply_generated_provisioning_database_sequence(
                root=root,
                socket_dir=socket_dir,
                port=port,
                project="fresh",
                database="project_workflow_fresh",
                precreate_empty_database=False,
            )
            apply_generated_provisioning_database_sequence(
                root=root,
                socket_dir=socket_dir,
                port=port,
                project="retry",
                database="project_workflow_empty_retry",
                precreate_empty_database=True,
            )
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            bootstrap_database_roles(admin_conn)
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            run_migrations(admin_conn)
            psql(admin_conn, render_workflow_sql(build_plan(project="otto", owner_user="otto-agent")))
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            stages = json.loads(
                psql(
                    admin_conn,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'name', name,
    'display_label', display_label,
    'rank', rank,
    'owner_roles', owner_roles,
    'is_terminal', is_terminal
) ORDER BY rank)::text
FROM ticket_board.workflow_stages;
""",
                )
            )
            assert stages == EXPECTED_STAGES, stages
            assert psql(admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions;") == "22"
            assert psql(admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions WHERE from_stage IN ('done', 'cancelled');") == "3"
            assert psql(
                admin_conn,
                """
SELECT count(*)
FROM ticket_board.workflow_stages a
CROSS JOIN ticket_board.workflow_stages b
WHERE a.name <> b.name
  AND ticket_board.workflow_transition_allowed_hardcoded(a.name, b.name)
      IS DISTINCT FROM ticket_board.workflow_transition_allowed_config(a.name, b.name);
""",
            ) == "0"
            aggregate_rbac_expectations = [
                ("user_reopen", "director", "main", "f"),
                ("mark_done", "audit", "main", "f"),
                ("cancel", "app", "main", "f"),
                ("audit_kick_back", "director", "main", "f"),
                ("user_reopen", "user", "main", "t"),
                ("audit_kick_back", "audit", "main", "t"),
                ("audit_kick_back", "main", "main", "f"),
            ]
            for action_name, actor, assignee, expected in aggregate_rbac_expectations:
                assert (
                    psql(
                        admin_conn,
                        (
                            "SELECT ticket_board.workflow_transition_rbac_allowed_config("
                            f"'{action_name}', '{actor}', '{assignee}');"
                        ),
                    )
                    == expected
                ), (action_name, actor, assignee, expected)
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('main');") == "t"
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('app');") == "t"
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('ops');") == "f"
            assert psql(admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('designer');") == "f"
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
            terminal_from_director_review = json.loads(
                psql(
                    admin_conn,
                    """
WITH RECURSIVE walk(stage) AS (
    VALUES ('director_review'::text)
    UNION
    SELECT wt.to_stage
    FROM ticket_board.workflow_transitions wt
    JOIN walk ON walk.stage = wt.from_stage
)
SELECT jsonb_agg(ws.name ORDER BY ws.rank)::text
FROM ticket_board.workflow_stages ws
JOIN walk ON walk.stage = ws.name
WHERE ws.is_terminal;
""",
                )
            )
            assert terminal_from_director_review == ["done", "cancelled"], terminal_from_director_review

            ticket_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow ticket', 'Body', 'draft');
""",
            )
            assert ticket_id == "OTTO-1", ticket_id
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "draft:designer"

            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{ticket_id}');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "analysis:director"

            service_call(service_conn, "director", f"SELECT ticket_board.route('{ticket_id}', 'in_progress', 'main');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "in_progress:main"

            service_call(service_conn, "main", f"SELECT ticket_board.submit_to_audit('{ticket_id}', 'abcdef1');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "audit:audit"

            service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{ticket_id}', 'Audit verified.');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "director_review:director"

            service_call(service_conn, "director", f"SELECT ticket_board.mark_done('{ticket_id}', 'abcdef1');")
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "done"

            service_call_without_shadow_warning(service_conn, "director", f"SELECT ticket_board.route('{ticket_id}', 'analysis', 'director');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "analysis:director"

            user_reopen_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow user reopen', 'Body', 'draft');
""",
            )
            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{user_reopen_id}');")
            service_call(service_conn, "director", f"SELECT ticket_board.route('{user_reopen_id}', 'in_progress', 'main');")
            service_call(service_conn, "main", f"SELECT ticket_board.submit_to_audit('{user_reopen_id}', 'abcdef2');")
            service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{user_reopen_id}', 'Audit verified.');")
            service_call(service_conn, "director", f"SELECT ticket_board.mark_done('{user_reopen_id}', 'abcdef2');")
            service_call_without_shadow_warning(service_conn, "user", f"SELECT ticket_board.user_reopen('{user_reopen_id}', 'Needs another pass.');")
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{user_reopen_id}';") == "analysis"

            audit_kickback_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow audit kickback', 'Body', 'draft');
""",
            )
            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{audit_kickback_id}');")
            service_call(service_conn, "director", f"SELECT ticket_board.route('{audit_kickback_id}', 'in_progress', 'main');")
            service_call(service_conn, "main", f"SELECT ticket_board.submit_to_audit('{audit_kickback_id}', 'abcdef3');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{audit_kickback_id}';") == "audit:audit"
            service_call_without_shadow_warning(service_conn, "audit", f"SELECT ticket_board.audit_kick_back('{audit_kickback_id}', 'Needs another pass.', 'main');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{audit_kickback_id}';") == "in_progress:main"

            director_kickback_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow director kickback', 'Body', 'draft');
""",
            )
            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{director_kickback_id}');")
            service_call(service_conn, "director", f"SELECT ticket_board.route('{director_kickback_id}', 'in_progress', 'app');")
            service_call(service_conn, "app", f"SELECT ticket_board.submit_to_audit('{director_kickback_id}', 'abcdef4');")
            service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{director_kickback_id}', 'Audit verified.');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{director_kickback_id}';") == "director_review:director"
            service_call_without_shadow_warning(service_conn, "director", f"SELECT ticket_board.route('{director_kickback_id}', 'in_progress', 'app');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{director_kickback_id}';") == "in_progress:app"
            service_call_without_shadow_warning(service_conn, "director", f"SELECT ticket_board.route('{director_kickback_id}', 'analysis', 'director');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{director_kickback_id}';") == "analysis:director"

            cancel_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned workflow cancellation', 'Body', 'draft');
""",
            )
            assert cancel_id == "OTTO-5", cancel_id
            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{cancel_id}');")
            service_call(service_conn, "director", f"SELECT ticket_board.route('{cancel_id}', 'in_progress', 'app');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "in_progress:app"
            cancel_error = service_call_fails(service_conn, "app", f"SELECT ticket_board.cancel('{cancel_id}', 'implementer cannot cancel');")
            assert "role app cannot call cancel" in cancel_error, cancel_error
            service_call(service_conn, "director", f"SELECT ticket_board.cancel('{cancel_id}', 'director cancelled.');")
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "cancelled"
            service_call_without_shadow_warning(service_conn, "director", f"SELECT ticket_board.route('{cancel_id}', 'analysis', 'director');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "analysis:director"

            noaudit_dbname = "project_workflow_noaudit"
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", noaudit_dbname])
            noaudit_admin_conn = conninfo(socket_dir, port, noaudit_dbname)
            noaudit_service_conn = conninfo(socket_dir, port, noaudit_dbname, "ticket_board_service")
            psql(noaudit_admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            run_migrations(noaudit_admin_conn)
            noaudit_plan = build_plan(
                project="noaudit",
                owner_user="noaudit-agent",
                database=noaudit_dbname,
                include_designer=False,
                include_audit=False,
                implementer_roles=("app",),
            )
            psql(noaudit_admin_conn, render_workflow_sql(noaudit_plan))
            psql(noaudit_admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            noaudit_stages = json.loads(
                psql(
                    noaudit_admin_conn,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'name', name,
    'display_label', display_label,
    'rank', rank,
    'owner_roles', owner_roles,
    'is_terminal', is_terminal
) ORDER BY rank)::text
FROM ticket_board.workflow_stages;
""",
                )
            )
            assert noaudit_stages == EXPECTED_AUDITLESS_STAGES, noaudit_stages
            assert psql(noaudit_admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions;") == "16"
            assert psql(noaudit_admin_conn, "SELECT count(*) FROM ticket_board.workflow_stages WHERE name = 'audit';") == "0"
            assert psql(noaudit_admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions WHERE 'audit' = ANY(allowed_roles);") == "0"
            assert psql(noaudit_admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('app');") == "t"
            assert psql(noaudit_admin_conn, "SELECT ticket_board.ticket_is_implementer_assignee('audit');") == "f"

            noaudit_ticket_id = service_call(
                noaudit_service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'NOAUDIT', false);
SELECT ticket_board.create_ticket('Audit-less workflow ticket', 'Body', 'analysis');
""",
            )
            assert noaudit_ticket_id == "NOAUDIT-1", noaudit_ticket_id
            assert (
                psql(noaudit_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';")
                == "analysis:unassigned"
            )

            service_call(noaudit_service_conn, "director", f"SELECT ticket_board.route('{noaudit_ticket_id}', 'in_progress', 'app');")
            assert (
                psql(noaudit_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';")
                == "in_progress:app"
            )

            service_call(noaudit_service_conn, "app", f"SELECT ticket_board.submit_to_audit('{noaudit_ticket_id}', '123abcd');")
            assert (
                psql(
                    noaudit_admin_conn,
                    f"SELECT state || ':' || assignee || ':' || audit_signoff::text FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';",
                )
                == "director_review:director:true"
            )

            service_call(noaudit_service_conn, "director", f"SELECT ticket_board.mark_done('{noaudit_ticket_id}', '123abcd');")
            assert psql(noaudit_admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';") == "done"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_project_workflow_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
