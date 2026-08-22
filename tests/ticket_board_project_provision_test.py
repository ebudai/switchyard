#!/usr/bin/env python3
"""Tests for per-project ticket-board provisioning artifact rendering."""

from __future__ import annotations

import json
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
    render_tmpfiles,
)


def test_non_pgu_project_is_fully_parameterized() -> None:
    plan = build_plan(project="stellaris", owner_user="stellaris-agent", port=8871)

    assert plan.board_unit == "stellaris-ticket-board.service"
    assert plan.listener_unit == "stellaris-ticket-board-notify-listener.service"
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

    combined = "\n".join(
        [
            json.dumps(plan.__dict__, sort_keys=True),
            render_board_unit(plan),
            render_listener_unit(plan),
            render_tmpfiles(plan),
            render_database_sql(plan),
            render_operator_commands(plan),
        ]
    )
    assert "127.0.0.1 --port 8871" in combined
    assert "PGDATABASE=stellaris_ticket_board" in combined
    assert "PGU_TICKET_BOARD_SOCKET=/run/stellaris-ticket-board/ticket-board.sock" in combined
    assert "PGU_TICKET_BOARD_PANE_STATE_DIR=%t/stellaris-ticket-board/pane-state" in combined
    assert "ReadWritePaths=/home/stellaris-agent/.claude/stellaris-tickets-assets /home/stellaris-agent/.claude/stellaris-ticket-frames" in combined
    assert f"SOURCE_REPO='{ROOT}'" in combined
    assert "BOARD_ROOT='/home/stellaris-agent/stellaris-ticketboard-live'" in combined
    assert "TICKET_BOARD_SKIP_MIGRATIONS=1" in combined
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
    assert "--port 8770" in board_unit
    assert "--frames /tmp/pgu-frames" in board_unit
    assert "--assets /home/agent/.claude/pgu-tickets-assets" in board_unit
    assert "ReadWritePaths=/home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames" in board_unit
    assert tmpfiles == "d /tmp/pgu-frames 1777 root root -\n"
    assert "sudo install -d -m 1777 -o root -g root '/tmp/pgu-frames'" in commands
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
        assert (output_dir / "porter-database.sql").exists()
        assert (output_dir / "operator-commands.sh").exists()


def main() -> int:
    test_non_pgu_project_is_fully_parameterized()
    test_pgu_project_render_matches_live_port_database_and_frame_dir()
    test_database_sql_bootstraps_schema_compatible_roles_and_project_database()
    test_custom_database_actor_roles_fail_loud_until_schema_supports_them()
    test_cli_writes_reviewable_artifacts()
    print("ticket_board_project_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
