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

from scripts.ticket_board.project_provision import (
    TENANT_WORKFLOW_EXCLUDED_ACTIONS,
    TENANT_WORKFLOW_EXCLUDED_STAGES,
    WorkflowStageSeed,
    WorkflowTransitionSeed,
    build_plan,
    project_workflow_stages,
    project_workflow_transitions,
    render_database_sql,
    render_vcs_close_role_sql,
    render_workflow_sql,
    schema_workflow_stages,
    schema_workflow_transitions,
)


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
MIGRATION_RUNNER = ROOT / "scripts" / "ticket-board-migrate"


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


def stage_dict(stage: WorkflowStageSeed) -> dict[str, object]:
    return {
        "name": stage.name,
        "display_label": stage.display_label,
        "rank": stage.rank,
        "owner_roles": list(stage.owner_roles),
        "entry_gate_field": stage.entry_gate_field,
        "gate_skip_to": stage.gate_skip_to,
        "is_terminal": stage.is_terminal,
    }


def transition_dict(transition: WorkflowTransitionSeed) -> dict[str, object]:
    return {
        "from_stage": transition.from_stage,
        "to_stage": transition.to_stage,
        "action_name": transition.action_name,
        "allowed_roles": list(transition.allowed_roles),
        "owner_scoped": transition.owner_scoped,
        "director_override": transition.director_override,
    }


def transition_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
    return (str(row["from_stage"]), str(row["to_stage"]), str(row["action_name"]))


def database_stages(conn: str) -> list[dict[str, object]]:
    return json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(jsonb_build_object(
    'name', name,
    'display_label', display_label,
    'rank', rank,
    'owner_roles', owner_roles,
    'entry_gate_field', entry_gate_field,
    'gate_skip_to', gate_skip_to,
    'is_terminal', is_terminal
) ORDER BY rank, name)::text
FROM ticket_board.workflow_stages;
""",
        )
    )


def database_transitions(conn: str) -> list[dict[str, object]]:
    return json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(jsonb_build_object(
    'from_stage', from_stage,
    'to_stage', to_stage,
    'action_name', action_name,
    'allowed_roles', allowed_roles,
    'owner_scoped', owner_scoped,
    'director_override', director_override
) ORDER BY from_stage, to_stage, action_name)::text
FROM ticket_board.workflow_transitions;
""",
        )
    )


def assert_project_workflow_matches_schema_projection(conn: str, plan) -> None:
    expected_stages = [stage_dict(stage) for stage in project_workflow_stages(plan)]
    assert database_stages(conn) == expected_stages

    expected_transitions = sorted(
        (transition_dict(transition) for transition in project_workflow_transitions(plan)),
        key=transition_sort_key,
    )
    actual_transitions = database_transitions(conn)
    assert actual_transitions == expected_transitions, {
        "missing": [row for row in expected_transitions if row not in actual_transitions],
        "extra": [row for row in actual_transitions if row not in expected_transitions],
    }


def assert_tenant_projection_policy(plan) -> None:
    stage_names = {stage.name for stage in project_workflow_stages(plan)}
    action_names = {transition.action_name for transition in project_workflow_transitions(plan)}
    assert stage_names.isdisjoint(TENANT_WORKFLOW_EXCLUDED_STAGES), stage_names
    assert action_names.isdisjoint(TENANT_WORKFLOW_EXCLUDED_ACTIONS), action_names


def assert_schema_additions_are_not_silent() -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    synthetic_schema = schema_sql.replace(
        "    ('audit', 'Audit', 5, ARRAY['audit']::text[], 'needs_audit', 'dat', 'audit_signoff', false),",
        "    ('qa', 'QA', 5, ARRAY['director']::text[], NULL, NULL, NULL, false),\n"
        "    ('audit', 'Audit', 5, ARRAY['audit']::text[], 'needs_audit', 'dat', 'audit_signoff', false),",
    ).replace(
        "    ('analysis', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),",
        "    ('analysis', 'qa', 'route', ARRAY['director']::text[], false, false),\n"
        "    ('analysis', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),",
    )
    plan = build_plan(project="otto", owner_user="otto-agent")
    assert "qa" in {stage.name for stage in project_workflow_stages(plan, schema_sql=synthetic_schema)}
    assert any(
        transition.from_stage == "analysis" and transition.to_stage == "qa" and transition.action_name == "route"
        for transition in project_workflow_transitions(plan, schema_sql=synthetic_schema)
    )
    rendered = render_workflow_sql(plan, schema_sql=synthetic_schema)
    assert "('qa', 'QA'," in rendered
    assert "('analysis', 'qa', 'route'" in rendered


def assert_missing_workflow_seed_inserts_fail_loudly() -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    missing_stages_schema = schema_sql.replace(
        "INSERT INTO ticket_board.workflow_stages",
        "INSERT INTO ticket_board.workflow_stages_missing",
        1,
    )
    missing_transitions_schema = schema_sql.replace(
        "INSERT INTO ticket_board.workflow_transitions",
        "INSERT INTO ticket_board.workflow_transitions_missing",
        1,
    )

    try:
        schema_workflow_stages(missing_stages_schema)
    except ValueError as exc:
        assert "could not find ticket_board.workflow_stages seed INSERT in schema.sql" in str(exc), exc
    else:
        raise AssertionError("missing workflow_stages seed INSERT did not fail loudly")

    try:
        schema_workflow_transitions(missing_transitions_schema)
    except ValueError as exc:
        assert "could not find ticket_board.workflow_transitions seed INSERT in schema.sql" in str(exc), exc
    else:
        raise AssertionError("missing workflow_transitions seed INSERT did not fail loudly")


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


def provisioned_transition(conn: str, action_name: str) -> dict[str, object]:
    row = psql(
        conn,
        f"""
SELECT jsonb_build_object(
    'from_stage', from_stage,
    'to_stage', to_stage,
    'action_name', action_name,
    'allowed_roles', allowed_roles,
    'owner_scoped', owner_scoped,
    'director_override', director_override
)::text
FROM ticket_board.workflow_transitions
WHERE action_name = '{action_name}';
""",
    )
    assert row, action_name
    return json.loads(row)


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
        assert_schema_additions_are_not_silent()
        assert_missing_workflow_seed_inserts_fail_loudly()
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
            plan = build_plan(project="otto", owner_user="otto-agent")
            psql(admin_conn, render_workflow_sql(plan))
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            assert_project_workflow_matches_schema_projection(admin_conn, plan)
            assert_tenant_projection_policy(plan)
            assert psql(admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions WHERE from_stage IN ('done', 'cancelled');") == "3"
            handback_transition = provisioned_transition(admin_conn, "implementer_kick_back")
            assert handback_transition == {
                "from_stage": "in_progress",
                "to_stage": "analysis",
                "action_name": "implementer_kick_back",
                "allowed_roles": ["app", "main"],
                "owner_scoped": True,
                "director_override": False,
            }, handback_transition
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
                ("audit_kick_back", "audit", "audit", "t"),
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
            assert reachable == [stage.name for stage in project_workflow_stages(plan)], reachable
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

            service_call(service_conn, "main", f"SELECT ticket_board.implementer_kick_back('{ticket_id}', 'Cannot proceed as specified.');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "analysis:director"
            assert (
                psql(admin_conn, f"SELECT who || ':' || text FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket_id}' ORDER BY position DESC LIMIT 1;")
                == "main:Cannot proceed as specified."
            )
            service_call(service_conn, "director", f"SELECT ticket_board.route('{ticket_id}', 'in_progress', 'main');")

            service_call(service_conn, "main", f"SELECT ticket_board.submit_to_audit('{ticket_id}', 'abcdef1');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "audit:audit"

            service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{ticket_id}', 'Audit verified.');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "director_review:director"

            service_call(service_conn, "director", f"SELECT ticket_board.mark_done('{ticket_id}', 'abcdef1');")
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{ticket_id}';") == "done"

            no_audit_ticket_id = service_call(
                service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket('Provisioned no-audit workflow ticket', 'Body', 'analysis', ARRAY[]::text[], '', false, false);
""",
            )
            service_call(service_conn, "director", f"SELECT ticket_board.route('{no_audit_ticket_id}', 'in_progress', 'app');")
            service_call(service_conn, "app", f"SELECT ticket_board.submit_to_audit('{no_audit_ticket_id}', 'abc7591');")
            assert (
                psql(
                    admin_conn,
                    f"SELECT state || ':' || assignee || ':' || audit_signoff::text || ':' || needs_audit::text FROM ticket_board.tickets WHERE id = '{no_audit_ticket_id}';",
                )
                == "director_review:director:false:false"
            )
            service_call(service_conn, "director", f"SELECT ticket_board.mark_done('{no_audit_ticket_id}', 'abc7591');")

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
            assert "role main cannot call audit_kick_back" in service_call_fails(
                service_conn,
                "main",
                f"SELECT ticket_board.audit_kick_back('{audit_kickback_id}', 'Wrong owner.', 'main');",
            )
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
            assert cancel_id == "OTTO-6", cancel_id
            service_call(service_conn, "designer", f"SELECT ticket_board.release_draft('{cancel_id}');")
            service_call(service_conn, "director", f"SELECT ticket_board.route('{cancel_id}', 'in_progress', 'app');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "in_progress:app"
            cancel_error = service_call_fails(service_conn, "app", f"SELECT ticket_board.cancel('{cancel_id}', 'implementer cannot cancel');")
            assert "role app cannot call cancel" in cancel_error, cancel_error
            service_call(service_conn, "director", f"SELECT ticket_board.cancel('{cancel_id}', 'director cancelled.');")
            assert psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "cancelled"
            service_call_without_shadow_warning(service_conn, "director", f"SELECT ticket_board.route('{cancel_id}', 'analysis', 'director');")
            assert psql(admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{cancel_id}';") == "analysis:director"

            provisioned_signoff_cases = [
                (False, False, "director_review", "director", False, False),
                (False, True, "dat", "director", False, False),
                (True, False, "audit", "audit", False, False),
                (True, True, "audit", "audit", False, False),
            ]
            for needs_audit, needs_user_signoff, expected_state, expected_assignee, expected_audit_signoff, expected_user_signoff in provisioned_signoff_cases:
                matrix_id = service_call(
                    service_conn,
                    "director",
                    f"""
SELECT set_config('ticket_board.ticket_prefix', 'OTTO', false);
SELECT ticket_board.create_ticket(
    'Provisioned audit UAT matrix',
    'Body',
    'analysis',
    ARRAY[]::text[],
    '',
    {'true' if needs_user_signoff else 'false'},
    {'true' if needs_audit else 'false'}
);
""",
                )
                service_call(service_conn, "director", f"SELECT ticket_board.route('{matrix_id}', 'in_progress', 'app');")
                service_call(service_conn, "app", f"SELECT ticket_board.submit_to_audit('{matrix_id}', 'a759{matrix_id.split('-')[-1].zfill(3)}');")
                matrix_status = json.loads(
                    psql(
                        admin_conn,
                        f"""
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee,
    'audit_signoff', audit_signoff,
    'needs_audit', needs_audit,
    'needs_user_signoff', needs_user_signoff,
    'user_signoff', user_signoff
)::text
FROM ticket_board.tickets
WHERE id = '{matrix_id}';
""",
                    )
                )
                assert matrix_status == {
                    "state": expected_state,
                    "assignee": expected_assignee,
                    "audit_signoff": expected_audit_signoff,
                    "needs_audit": needs_audit,
                    "needs_user_signoff": needs_user_signoff,
                    "user_signoff": expected_user_signoff,
                }, matrix_status
                if needs_audit:
                    service_call(service_conn, "audit", f"SELECT ticket_board.audit_sign_off('{matrix_id}', 'Audit verified.');")
                    after_audit_signoff = json.loads(
                        psql(
                            admin_conn,
                            f"""
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff)::text
FROM ticket_board.tickets
WHERE id = '{matrix_id}';
""",
                        )
                    )
                    assert after_audit_signoff == {
                        "state": "dat" if needs_user_signoff else "director_review",
                        "assignee": "director",
                        "audit_signoff": True,
                        "user_signoff": False,
                    }, after_audit_signoff
                if needs_user_signoff:
                    service_call(service_conn, "director", f"SELECT ticket_board.director_dat_sign_off('{matrix_id}', 'DAT accepted.');")
                    uat_status = json.loads(
                        psql(
                            admin_conn,
                            f"""
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff)::text
FROM ticket_board.tickets
WHERE id = '{matrix_id}';
""",
                        )
                    )
                    assert uat_status == {
                        "state": "user_review",
                        "assignee": "user",
                        "audit_signoff": needs_audit,
                        "user_signoff": False,
                    }, uat_status
                    service_call(service_conn, "user", f"SELECT ticket_board.user_sign_off('{matrix_id}', 'UAT accepted.');")
                    user_signed_status = json.loads(
                        psql(
                            admin_conn,
                            f"""
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff)::text
FROM ticket_board.tickets
WHERE id = '{matrix_id}';
""",
                        )
                    )
                    assert user_signed_status == {
                        "state": "director_review",
                        "assignee": "director",
                        "audit_signoff": needs_audit,
                        "user_signoff": True,
                    }, user_signed_status
                service_call(service_conn, "director", f"SELECT ticket_board.mark_done('{matrix_id}', 'a759{matrix_id.split('-')[-1].zfill(3)}');")

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

            assert_project_workflow_matches_schema_projection(noaudit_admin_conn, noaudit_plan)
            assert_tenant_projection_policy(noaudit_plan)
            assert psql(noaudit_admin_conn, "SELECT count(*) FROM ticket_board.workflow_stages WHERE name = 'audit';") == "0"
            assert psql(noaudit_admin_conn, "SELECT count(*) FROM ticket_board.workflow_transitions WHERE 'audit' = ANY(allowed_roles);") == "0"
            noaudit_handback_transition = provisioned_transition(noaudit_admin_conn, "implementer_kick_back")
            assert noaudit_handback_transition == {
                "from_stage": "in_progress",
                "to_stage": "analysis",
                "action_name": "implementer_kick_back",
                "allowed_roles": ["app"],
                "owner_scoped": True,
                "director_override": False,
            }, noaudit_handback_transition
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

            service_call(noaudit_service_conn, "app", f"SELECT ticket_board.implementer_kick_back('{noaudit_ticket_id}', 'Auditless hand-back.');")
            assert (
                psql(noaudit_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';")
                == "analysis:director"
            )
            assert (
                psql(
                    noaudit_admin_conn,
                    f"SELECT who || ':' || text FROM ticket_board.ticket_comments WHERE ticket_id = '{noaudit_ticket_id}' ORDER BY position DESC LIMIT 1;",
                )
                == "app:Auditless hand-back."
            )
            service_call(noaudit_service_conn, "director", f"SELECT ticket_board.route('{noaudit_ticket_id}', 'in_progress', 'app');")

            service_call(noaudit_service_conn, "app", f"SELECT ticket_board.submit_to_audit('{noaudit_ticket_id}', '123abcd');")
            assert (
                psql(
                    noaudit_admin_conn,
                    f"SELECT state || ':' || assignee || ':' || audit_signoff::text FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';",
                )
                == "director_review:director:false"
            )

            service_call(noaudit_service_conn, "director", f"SELECT ticket_board.mark_done('{noaudit_ticket_id}', '123abcd');")
            assert psql(noaudit_admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{noaudit_ticket_id}';") == "done"

            multi_audit_dbname = "project_workflow_multi_audit"
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", multi_audit_dbname])
            multi_audit_admin_conn = conninfo(socket_dir, port, multi_audit_dbname)
            multi_audit_service_conn = conninfo(socket_dir, port, multi_audit_dbname, "ticket_board_service")
            psql(multi_audit_admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            run_migrations(multi_audit_admin_conn)
            multi_audit_plan = build_plan(
                project="review",
                owner_user="review-agent",
                database=multi_audit_dbname,
                include_designer=False,
                implementer_roles=("main",),
                audit_roles=("audit_gemini", "audit_gpt"),
            )
            psql(multi_audit_admin_conn, render_workflow_sql(multi_audit_plan))
            psql(multi_audit_admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            assert_project_workflow_matches_schema_projection(multi_audit_admin_conn, multi_audit_plan)
            assert_tenant_projection_policy(multi_audit_plan)
            assert (
                psql(
                    multi_audit_admin_conn,
                    "SELECT owner_roles::text FROM ticket_board.workflow_stages WHERE name = 'audit';",
                )
                == "{audit_gemini,audit_gpt}"
            )
            assert (
                psql(
                    multi_audit_admin_conn,
                    """
SELECT allowed_roles::text
FROM ticket_board.workflow_transitions
WHERE from_stage = 'audit' AND to_stage = 'director_review' AND action_name = 'audit_sign_off';
""",
                )
                == "{audit_gemini,audit_gpt}"
            )

            multi_audit_ticket_id = service_call(
                multi_audit_service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'REV', false);
SELECT ticket_board.create_ticket('Partitioned audit workflow ticket', 'Body', 'analysis');
""",
            )
            service_call(multi_audit_service_conn, "director", f"SELECT ticket_board.route('{multi_audit_ticket_id}', 'in_progress', 'main');")
            psql(multi_audit_admin_conn, "DELETE FROM ticket_board.ticket_notification_queue;")
            service_call(multi_audit_service_conn, "main", f"SELECT ticket_board.submit_to_audit('{multi_audit_ticket_id}', 'abc8120');")
            multi_audit_state = psql(
                multi_audit_admin_conn,
                f"SELECT state || ':' || coalesce(assignee, '') FROM ticket_board.tickets WHERE id = '{multi_audit_ticket_id}';",
            )
            assert multi_audit_state == "audit:audit_gemini", multi_audit_state

            audit_assignment_queue = json.loads(
                psql(
                    multi_audit_admin_conn,
                    f"""
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'kind', kind,
    'new_state', payload->>'new_state',
    'assignee', payload->>'assignee'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = '{multi_audit_ticket_id}';
""",
                )
            )
            assert audit_assignment_queue == [
                {
                    "target_role": "audit_gemini",
                    "kind": "transition",
                    "new_state": "audit",
                    "assignee": "audit_gemini",
                }
            ], audit_assignment_queue
            assert (
                psql(
                    multi_audit_admin_conn,
                    f"SELECT ticket_board.transition_target_role('audit', assignee) FROM ticket_board.tickets WHERE id = '{multi_audit_ticket_id}';",
                )
                == "audit_gemini"
            )
            assert "role audit_gpt cannot call audit_sign_off" in service_call_fails(
                multi_audit_service_conn,
                "audit_gpt",
                f"SELECT ticket_board.audit_sign_off('{multi_audit_ticket_id}', 'Wrong partition.');",
            )
            service_call(multi_audit_service_conn, "audit_gemini", f"SELECT ticket_board.audit_sign_off('{multi_audit_ticket_id}', 'Audit verified.');")
            assert (
                psql(
                    multi_audit_admin_conn,
                    f"SELECT state || ':' || assignee || ':' || audit_signoff::text FROM ticket_board.tickets WHERE id = '{multi_audit_ticket_id}';",
                )
                == "director_review:director:true"
            )
            service_call(multi_audit_service_conn, "director", f"SELECT ticket_board.mark_done('{multi_audit_ticket_id}', 'abc8120');")

            second_multi_audit_ticket_id = service_call(
                multi_audit_service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'REV', false);
SELECT ticket_board.create_ticket('Second partitioned audit workflow ticket', 'Body', 'analysis');
""",
            )
            service_call(multi_audit_service_conn, "director", f"SELECT ticket_board.route('{second_multi_audit_ticket_id}', 'in_progress', 'main');")
            service_call(multi_audit_service_conn, "main", f"SELECT ticket_board.submit_to_audit('{second_multi_audit_ticket_id}', 'abc8121');")
            assert (
                psql(
                    multi_audit_admin_conn,
                    f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{second_multi_audit_ticket_id}';",
                )
                == "audit:audit_gpt"
            )
            service_call(multi_audit_service_conn, "audit_gpt", f"SELECT ticket_board.audit_sign_off('{second_multi_audit_ticket_id}', 'Audit verified.');")
            assert (
                psql(
                    multi_audit_admin_conn,
                    f"SELECT state || ':' || assignee || ':' || audit_signoff::text FROM ticket_board.tickets WHERE id = '{second_multi_audit_ticket_id}';",
                )
                == "director_review:director:true"
            )

            post_vcs_dbname = "project_workflow_post_vcs"
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", post_vcs_dbname])
            post_vcs_admin_conn = conninfo(socket_dir, port, post_vcs_dbname)
            post_vcs_service_conn = conninfo(socket_dir, port, post_vcs_dbname, "ticket_board_service")
            psql(post_vcs_admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            run_migrations(post_vcs_admin_conn)
            post_vcs_plan = build_plan(
                project="ship",
                owner_user="ship-agent",
                database=post_vcs_dbname,
                include_designer=False,
                include_audit=False,
                implementer_roles=("ops", "archivist"),
            )
            psql(post_vcs_admin_conn, render_workflow_sql(post_vcs_plan))
            psql(post_vcs_admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            post_vcs_ticket_id = service_call(
                post_vcs_service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'SHIP', false);
SELECT ticket_board.create_ticket('Post-provision VCS close ticket', 'Body', 'analysis');
""",
            )
            service_call(post_vcs_service_conn, "director", f"SELECT ticket_board.route('{post_vcs_ticket_id}', 'in_progress', 'ops');")
            service_call(post_vcs_service_conn, "ops", f"SELECT ticket_board.submit_to_audit('{post_vcs_ticket_id}', 'abc8210');")
            assert (
                psql(post_vcs_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{post_vcs_ticket_id}';")
                == "director_review:director"
            )
            post_vcs_plan = build_plan(
                project="ship",
                owner_user="ship-agent",
                database=post_vcs_dbname,
                include_designer=False,
                include_audit=False,
                implementer_roles=("ops", "archivist"),
                vcs_close_role="archivist",
            )
            psql(post_vcs_admin_conn, render_vcs_close_role_sql(post_vcs_plan))
            assert (
                psql(
                    post_vcs_admin_conn,
                    """
SELECT allowed_roles::text
FROM ticket_board.workflow_transitions
WHERE from_stage = 'vcs' AND to_stage = 'done' AND action_name = 'mark_done';
""",
                )
                == "{archivist}"
            )
            assert (
                psql(
                    post_vcs_admin_conn,
                    """
SELECT count(*)
FROM ticket_board.workflow_transitions
WHERE from_stage = 'director_review' AND to_stage = 'done' AND action_name = 'mark_done';
""",
                )
                == "0"
            )
            assert "role director cannot call mark_done" in service_call_fails(
                post_vcs_service_conn,
                "director",
                f"SELECT ticket_board.mark_done('{post_vcs_ticket_id}', 'abc8210');",
            )
            service_call(post_vcs_service_conn, "director", f"SELECT ticket_board.route('{post_vcs_ticket_id}', 'vcs', 'archivist');")
            service_call(post_vcs_service_conn, "archivist", f"SELECT ticket_board.mark_done('{post_vcs_ticket_id}', 'abc8210');")
            assert (
                psql(post_vcs_admin_conn, f"SELECT state || ':' || assignee || ':' || commit_hash FROM ticket_board.tickets WHERE id = '{post_vcs_ticket_id}';")
                == "done:archivist:abc8210"
            )

            vcs_dbname = "project_workflow_vcs"
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", vcs_dbname])
            vcs_admin_conn = conninfo(socket_dir, port, vcs_dbname)
            vcs_service_conn = conninfo(socket_dir, port, vcs_dbname, "ticket_board_service")
            psql(vcs_admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            run_migrations(vcs_admin_conn)
            vcs_plan = build_plan(
                project="ship",
                owner_user="ship-agent",
                database=vcs_dbname,
                include_designer=False,
                include_audit=False,
                implementer_roles=("ops",),
                vcs_close_role="ops",
            )
            psql(vcs_admin_conn, render_workflow_sql(vcs_plan))
            psql(vcs_admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            assert psql(
                vcs_admin_conn,
                "SELECT owner_roles::text || ':' || rank::text FROM ticket_board.workflow_stages WHERE name = 'vcs';",
            ) == "{ops}:4"
            assert psql(
                vcs_admin_conn,
                """
SELECT allowed_roles::text
FROM ticket_board.workflow_transitions
WHERE from_stage = 'vcs' AND to_stage = 'done' AND action_name = 'mark_done';
""",
            ) == "{ops}"
            assert psql(
                vcs_admin_conn,
                """
SELECT count(*)
FROM ticket_board.workflow_transitions
WHERE from_stage = 'director_review' AND to_stage = 'done' AND action_name = 'mark_done';
""",
            ) == "0"
            vcs_ticket_id = service_call(
                vcs_service_conn,
                "director",
                """
SELECT set_config('ticket_board.ticket_prefix', 'SHIP', false);
SELECT ticket_board.create_ticket('Provisioned VCS close ticket', 'Body', 'analysis');
""",
            )
            service_call(vcs_service_conn, "director", f"SELECT ticket_board.route('{vcs_ticket_id}', 'in_progress', 'ops');")
            service_call(vcs_service_conn, "ops", f"SELECT ticket_board.submit_to_audit('{vcs_ticket_id}', 'abc7690');")
            assert (
                psql(vcs_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{vcs_ticket_id}';")
                == "director_review:director"
            )
            assert "role director cannot call mark_done" in service_call_fails(
                vcs_service_conn,
                "director",
                f"SELECT ticket_board.mark_done('{vcs_ticket_id}', 'abc7690');",
            )
            service_call(vcs_service_conn, "director", f"SELECT ticket_board.route('{vcs_ticket_id}', 'vcs', 'ops');")
            assert (
                psql(vcs_admin_conn, f"SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = '{vcs_ticket_id}';")
                == "vcs:ops"
            )
            service_call(vcs_service_conn, "ops", f"SELECT ticket_board.mark_done('{vcs_ticket_id}', 'abc7690');")
            assert (
                psql(vcs_admin_conn, f"SELECT state || ':' || assignee || ':' || commit_hash FROM ticket_board.tickets WHERE id = '{vcs_ticket_id}';")
                == "done:ops:abc7690"
            )
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    print("ticket_board_project_workflow_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
