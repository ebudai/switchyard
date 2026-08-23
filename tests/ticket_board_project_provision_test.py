#!/usr/bin/env python3
"""Tests for per-project ticket-board provisioning artifact rendering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.project_provision import (
    build_plan,
    render_board_unit,
    render_database_sql,
    render_listener_unit,
    render_operator_commands,
    render_polkit_rule,
    render_tmpfiles,
    render_workflow_sql,
)


def test_non_pgu_project_is_fully_parameterized() -> None:
    plan = build_plan(project="stellaris", owner_user="stellaris-agent", port=8871)

    assert plan.board_unit == "stellaris-ticket-board.service"
    assert plan.listener_unit == "stellaris-ticket-board-notify-listener.service"
    assert plan.polkit_name == "49-stellaris-ticket-board-deploy.rules"
    assert plan.runtime_directory == "stellaris-ticket-board"
    assert plan.socket_path == "/run/stellaris-ticket-board/ticket-board.sock"
    assert plan.port == 8871
    assert plan.database == "stellaris_ticket_board"
    assert plan.board_root == "/home/stellaris-agent/stellaris-ticketboard-live"
    assert plan.source_repo == str(ROOT)
    assert plan.asset_dir == "/home/stellaris-agent/.claude/stellaris-tickets-assets"
    assert plan.frame_dir == "/home/stellaris-agent/.claude/stellaris-ticket-frames"
    assert plan.service_role == "ticket_board_service"
    assert plan.listener_role == "ticket_board_listener"
    assert plan.workflow_seed == "default-five-stage"
    assert plan.implementer_roles == ("implementer",)
    assert plan.assignee_roles == ("unassigned", "implementer", "audit", "director")
    assert plan.caller_roles == ("director", "implementer", "audit", "user")

    combined = "\n".join(
        [
            json.dumps(plan.__dict__, sort_keys=True),
            render_board_unit(plan),
            render_listener_unit(plan),
            render_tmpfiles(plan),
            render_polkit_rule(plan),
            render_database_sql(plan),
            render_workflow_sql(plan),
            render_operator_commands(plan),
        ]
    )
    assert "127.0.0.1 --port 8871" in combined
    assert "PGDATABASE=stellaris_ticket_board" in combined
    assert "TICKET_BOARD_SOCKET=/run/stellaris-ticket-board/ticket-board.sock" in combined
    assert "TICKET_BOARD_IMPLEMENTER_ROLES=implementer" in combined
    assert "TICKET_BOARD_ASSIGNEES=unassigned,implementer,audit,director" in combined
    assert "TICKET_BOARD_CALLER_ROLES=director,implementer,audit,user" in combined
    assert "TICKET_BOARD_PANE_STATE_DIR=%t/stellaris-ticket-board/pane-state" in combined
    assert "PGU_TICKET_BOARD_SOCKET=" not in combined
    assert "PGU_TICKET_BOARD_PANE_STATE_DIR=" not in combined
    assert "ReadWritePaths=/home/stellaris-agent/.claude/stellaris-tickets-assets /home/stellaris-agent/.claude/stellaris-ticket-frames" in combined
    assert f"SOURCE_REPO='{ROOT}'" in combined
    assert "BOARD_ROOT='/home/stellaris-agent/stellaris-ticketboard-live'" in combined
    assert "TICKET_BOARD_PROJECT='stellaris'" in combined
    assert "TICKET_BOARD_SKIP_MIGRATIONS=1" in combined
    assert "sudo psql -X -v ON_ERROR_STOP=1 'postgresql:///stellaris_ticket_board?host=/var/run/postgresql&user=postgres' -f 'stellaris-workflow.sql'" in combined
    assert combined.index("scripts/ticket-board-migrate") < combined.index("-f 'stellaris-workflow.sql'")
    assert combined.index("-f 'stellaris-workflow.sql'") < combined.index("ticket_board/rbac.sql")
    assert "Seed the default five-stage workflow for stellaris" in combined
    assert "('analysis', 'Triage', 1, ARRAY['director']::text[]" in combined
    assert "('in_progress', 'Implementation', 2, ARRAY['implementer']::text[]" in combined
    assert "ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN ('unassigned', 'implementer', 'audit', 'director'))" in combined
    assert "('audit', 'director_review', 'audit_sign_off', ARRAY['audit']::text[]" in combined
    assert "('backlog'," not in render_workflow_sql(plan)
    assert "('inspection'," not in render_workflow_sql(plan)
    assert 'unit === "stellaris-ticket-board.service"' in combined
    assert 'unit === "stellaris-ticket-board-canary.service"' in combined
    assert 'subject.user !== "stellaris-agent"' in combined
    assert "Do NOT add org.freedesktop.systemd1.manage-unit-files here" in combined
    assert "reload-daemon cannot be scoped to a unit" in combined
    assert "setfacl -R -m u:boardsvc:rx '/home/stellaris-agent/stellaris-ticketboard-live'" in combined
    assert "ticket_has_unresolved_blockers" not in combined
    assert "/tmp/pgu-frames" not in combined
    assert "/home/agent/.claude/pgu-tickets-assets" not in combined
    assert "RuntimeDirectory=pgu-ticket-board" not in combined
    assert "/run/pgu-ticket-board/ticket-board.sock" not in combined


def test_pgu_project_render_matches_live_port_database_and_frame_dir() -> None:
    plan = build_plan(project="pgu", owner_user="agent")
    board_unit = render_board_unit(plan)
    tmpfiles = render_tmpfiles(plan)
    commands = render_operator_commands(plan)

    assert plan.port == 8770
    assert plan.database == "pgu"
    assert plan.board_unit == "pgu-ticket-board.service"
    assert plan.runtime_directory == "pgu-ticket-board"
    assert plan.socket_path == "/run/pgu-ticket-board/ticket-board.sock"
    assert plan.board_root == "/home/agent/pgu-ticketboard-live"
    assert plan.asset_dir == "/home/agent/.claude/pgu-tickets-assets"
    assert plan.frame_dir == "/tmp/pgu-frames"
    assert plan.workflow_seed == "pgu-full"
    assert plan.implementer_roles == ("main", "app", "ops", "perf", "research")
    assert "--port 8770" in board_unit
    assert "--frames /tmp/pgu-frames" in board_unit
    assert "--assets /home/agent/.claude/pgu-tickets-assets" in board_unit
    assert "ReadWritePaths=/home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames" in board_unit
    assert tmpfiles == "d /tmp/pgu-frames 1777 root root -\n"
    assert "sudo install -d -m 1777 -o root -g root '/tmp/pgu-frames'" in commands
    assert "-workflow.sql" not in commands
    assert "keeps the full workflow seeded by schema.sql" in render_workflow_sql(plan)
    assert "sudo install -d -m 0775 -o 'agent' -g 'agent' '/home/agent/.claude/pgu-tickets-assets' '/tmp/pgu-frames'" not in commands
    assert "sudo setfacl -R -m u:boardsvc:rwx '/home/agent/.claude/pgu-tickets-assets' '/tmp/pgu-frames'" not in commands


def test_database_sql_bootstraps_schema_compatible_roles_and_project_database() -> None:
    plan = build_plan(project="4xgame", owner_user="4xgame", port=8899)
    sql = render_database_sql(plan)

    assert "CREATE ROLE ticket_board_service" in sql
    assert "CREATE ROLE ticket_board_listener" in sql
    assert "CREATE DATABASE %I', '4xgame_ticket_board'" in sql
    assert 'COMMENT ON DATABASE "4xgame_ticket_board"' in sql


def test_custom_database_actor_roles_fail_loud_until_schema_supports_them() -> None:
    try:
        build_plan(
            project="stellaris",
            owner_user="stellaris-agent",
            service_role="stellaris_ticket_board_service",
        )
        raise AssertionError("expected custom DB role rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "custom database service/listener roles are not supported yet" in message
    assert "ticket_board_service" in message


def test_board_role_environment_drives_python_gate() -> None:
    script = """
from scripts.ticket_board import app
from scripts.ticket_board import server

assert app.ASSIGNEES == ('unassigned', 'builder', 'audit', 'director')
assert app.CALLER_ROLES == ('director', 'builder', 'audit', 'user')
assert server.IMPLEMENTER_ROLES == {'builder'}
assert server.OPERATION_ALLOWED_ROLES['start_work'] == {'builder'}
assert server.OPERATION_ALLOWED_ROLES['submit_to_audit'] == {'builder'}
assert 'builder' in server.OPERATION_ALLOWED_ROLES['file_bug']
assert 'ops' not in server.OPERATION_ALLOWED_ROLES['start_work']
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "TICKET_BOARD_ASSIGNEES": "unassigned,builder,audit,director",
        "TICKET_BOARD_CALLER_ROLES": "director,builder,audit,user",
        "TICKET_BOARD_IMPLEMENTER_ROLES": "builder",
    }
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def test_cli_writes_reviewable_artifacts() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-board-provision.") as tmp:
        output_dir = Path(tmp) / "out"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ticket-board-provision-project"),
                "--project",
                "porter",
                "--owner-user",
                "otto-agent",
                "--port",
                "8872",
                "--output-dir",
                str(output_dir),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        assert f"wrote {output_dir}" in result.stdout
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        assert plan["port"] == 8872
        assert plan["database"] == "porter_ticket_board"
        assert plan["source_repo"] == str(ROOT)
        assert plan["socket_path"] == "/run/porter-ticket-board/ticket-board.sock"
        assert (output_dir / "porter-ticket-board.service").exists()
        assert (output_dir / "porter-ticket-board-notify-listener.service").exists()
        assert (output_dir / "porter-ticket-board.conf").exists()
        assert (output_dir / "49-porter-ticket-board-deploy.rules").exists()
        assert (output_dir / "porter-database.sql").exists()
        assert (output_dir / "porter-workflow.sql").exists()
        assert (output_dir / "operator-commands.sh").exists()

        workflow_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ticket-board-provision-project"),
                "--project",
                "porter",
                "--owner-user",
                "otto-agent",
                "--implementer-role",
                "builder",
                "--render",
                "workflow-sql",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert "Seed the default five-stage workflow for porter" in workflow_result.stdout
        assert "ARRAY['builder']::text[]" in workflow_result.stdout


def main() -> int:
    test_non_pgu_project_is_fully_parameterized()
    test_pgu_project_render_matches_live_port_database_and_frame_dir()
    test_database_sql_bootstraps_schema_compatible_roles_and_project_database()
    test_custom_database_actor_roles_fail_loud_until_schema_supports_them()
    test_board_role_environment_drives_python_gate()
    test_cli_writes_reviewable_artifacts()
    print("ticket_board_project_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
