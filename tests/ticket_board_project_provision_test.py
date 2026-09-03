#!/usr/bin/env python3
"""Tests for per-project ticket-board provisioning artifact rendering."""

from __future__ import annotations

import json
import os
import pwd
import shutil
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
    render_add_role_sql,
    render_listener_unit,
    render_operator_commands,
    render_polkit_rule,
    render_tmpfiles,
    render_vcs_close_role_sql,
    render_workflow_sql,
)
from standalone_test_runner import run_module_tests


def test_non_pgu_project_is_fully_parameterized() -> None:
    plan = build_plan(project="stellaris", owner_user="stellaris-agent", port=8871)

    assert plan.board_unit == "stellaris-ticket-board.service"
    assert plan.ticket_prefix == "STELLARIS"
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
    assert plan.workflow_seed == "default-project"
    assert plan.draft_roles == ("designer",)
    assert plan.implementer_roles == ("app", "main")
    assert plan.assignee_roles == ("unassigned", "designer", "app", "main", "audit", "director", "user")
    assert plan.caller_roles == ("director", "designer", "app", "main", "audit", "user")

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
    assert "TICKET_BOARD_PROJECT=stellaris" in combined
    assert "TICKET_BOARD_TICKET_PREFIX=STELLARIS" in combined
    assert "PGDATABASE=stellaris_ticket_board" in combined
    assert "TICKET_BOARD_SOCKET=/run/stellaris-ticket-board/ticket-board.sock" in combined
    assert "TICKET_BOARD_DRAFT_ROLES=designer" in combined
    assert "TICKET_BOARD_IMPLEMENTER_ROLES=app,main" in combined
    assert "TICKET_BOARD_ASSIGNEES=unassigned,designer,app,main,audit,director,user" in combined
    assert "TICKET_BOARD_CALLER_ROLES=director,designer,app,main,audit,user" in combined
    assert "TICKET_BOARD_OPERATION_ALLOWED_ROLES=" not in combined
    assert "EnvironmentFile=-/home/stellaris-agent/.config/stellaris/ticket-board.env" in combined
    assert "sudo install -d -m 0755 -o 'stellaris-agent' -g 'stellaris-agent' '/home/stellaris-agent/.config'" in combined
    assert "sudo install -d -m 0700 -o 'stellaris-agent' -g 'stellaris-agent' '/home/stellaris-agent/.config/stellaris'" in combined
    assert "if ! sudo -u 'stellaris-agent' test -s '/home/stellaris-agent/.config/stellaris/ticket-board.env'; then" in combined
    assert "TICKET_BOARD_TENANT_REPORT_TOKEN=%s" in combined
    assert "sudo -u 'stellaris-agent' chmod 0600 '/home/stellaris-agent/.config/stellaris/ticket-board.env'" in combined
    assert "TICKET_BOARD_PANE_STATE_DIR=%t/stellaris-ticket-board/pane-state" in combined
    assert (
        "sudo -u 'stellaris-agent' -H env XDG_RUNTIME_DIR=\"$owner_runtime_dir\" "
        "TICKET_BOARD_PROJECT='stellaris' "
        "TICKET_BOARD_PANE_STATE_DIR=\"$owner_runtime_dir/stellaris-ticket-board/pane-state\" "
        "TICKET_BOARD_PANE_SESSION_DIR='/home/stellaris-agent/.local/state/stellaris-ticket-board/pane-sessions' "
        "'/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket-board-install-pane-hooks' install "
        "--home '/home/stellaris-agent' "
        "--hook-source '/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket-board-pane-idle-hook' "
        "--bin-path '/home/stellaris-agent/.local/bin/ticket-board-pane-idle-hook' "
        "--seed-codex-hook-trust-if-new"
    ) in combined
    assert combined.index("systemctl --user daemon-reload") < combined.index("ticket-board-install-pane-hooks")
    assert combined.index("ticket-board-install-pane-hooks") < combined.index("systemctl --user enable --now stellaris-ticket-board-notify-listener.service")
    assert "PGU_TICKET_BOARD_SOCKET=" not in combined
    assert "PGU_TICKET_BOARD_PANE_STATE_DIR=" not in combined
    assert "ReadWritePaths=/home/stellaris-agent/.claude/stellaris-tickets-assets /home/stellaris-agent/.claude/stellaris-ticket-frames" in combined
    assert f"SOURCE_REPO='{ROOT}'" in combined
    assert render_operator_commands(plan).splitlines()[:2] == [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
    ]
    assert "BOARD_ROOT='/home/stellaris-agent/stellaris-ticketboard-live'" in combined
    assert "TICKET_BOARD_PROJECT='stellaris'" in combined
    assert "TICKET_BOARD_SKIP_MIGRATIONS=1" in combined
    assert "if ! getent passwd 'boardsvc' >/dev/null 2>&1; then" in combined
    assert "sudo useradd -r -M -d /nonexistent -s \"$service_shell\" 'boardsvc'" in combined
    assert combined.index("if ! getent passwd 'boardsvc'") < combined.index("setfacl -R -m u:boardsvc:rx")
    peer_auth_command = (
        f"sudo env PG_DATABASE='stellaris_ticket_board' PG_IDENT_MAP='pgu_ticket_board_service' "
        f"SERVICE_USER='boardsvc' SERVICE_ROLE='ticket_board_service' "
        f"'{ROOT}/scripts/ticket-board-boardsvc-setup.sh' --apply-peer-auth"
    )
    assert peer_auth_command in combined
    assert combined.index("sudo useradd -r -M -d /nonexistent") < combined.index("--apply-peer-auth")
    assert combined.index("--apply-peer-auth") < combined.index("sudo cat 'stellaris-database.sql'")
    assert combined.index("--apply-peer-auth") < combined.index("curl -fsS http://127.0.0.1:8871/api/board")
    assert "sudo cat 'stellaris-workflow.sql' | sudo -u postgres psql -X -v ON_ERROR_STOP=1 'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' -f -" in combined
    assert combined.index("ticket_board/schema.sql") < combined.index("scripts/ticket-board-migrate")
    assert combined.index("scripts/ticket-board-migrate") < combined.index("sudo cat 'stellaris-workflow.sql'")
    assert combined.index("sudo cat 'stellaris-workflow.sql'") < combined.index("ticket_board/rbac.sql")
    assert "Seed the default project workflow for stellaris" in combined
    assert "('draft', 'Draft', 0, ARRAY['designer']::text[]" in combined
    assert "('analysis', 'Triage', 1, ARRAY['director']::text[]" in combined
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in combined
    assert "('done', 'Done', 9, ARRAY[]::text[], NULL, NULL, NULL, true)" in combined
    assert "('cancelled', 'Cancelled', 10, ARRAY[]::text[], NULL, NULL, NULL, true)" in combined
    assert "('draft', 'analysis', 'release_draft', ARRAY['designer', 'director', 'user']::text[]" in combined
    assert "('draft', 'cancelled', 'cancel', ARRAY['director']::text[]" in combined
    assert "('in_progress', 'analysis', 'route', ARRAY['director']::text[]" in combined
    assert (
        "ADD CONSTRAINT tickets_assignee_check CHECK "
        "(assignee IN ('unassigned', 'designer', 'app', 'main', 'audit', 'director', 'user'))"
    ) in combined
    assert "('audit', 'analysis', 'route', ARRAY['director']::text[]" in combined
    assert "('audit', 'in_progress', 'audit_kick_back', ARRAY['audit']::text[]" in combined
    assert "('audit', 'director_review', 'audit_sign_off', ARRAY['audit']::text[]" in combined
    assert "('director_review', 'analysis', 'route', ARRAY['director']::text[]" in combined
    assert "('director_review', 'analysis', 'user_reopen', ARRAY['user']::text[]" in combined
    assert "('director_review', 'in_progress', 'route', ARRAY['director']::text[]" in combined
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" in combined
    assert "('director_review', 'cancelled', 'cancel', ARRAY['director']::text[]" in combined
    assert "('done', 'analysis', 'route', ARRAY['director']::text[]" in combined
    assert "('done', 'analysis', 'user_reopen', ARRAY['user']::text[]" in combined
    assert "('cancelled', 'analysis', 'route', ARRAY['director']::text[]" in combined
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


def test_non_pgu_project_can_insert_vcs_stage_owned_by_ops() -> None:
    plan = build_plan(
        project="stellaris",
        owner_user="stellaris-agent",
        port=8871,
        implementer_roles=("app", "main"),
        vcs_close_role="ops",
    )

    board_unit = render_board_unit(plan)
    workflow_sql = render_workflow_sql(plan)

    assert plan.operation_allowed_roles == (("mark_done", ("ops",)),)
    assert plan.implementer_roles == ("app", "main")
    assert "Environment=TICKET_BOARD_OPERATION_ALLOWED_ROLES=mark_done=ops" in board_unit
    assert "('vcs', 'VCS', 7, ARRAY['ops']::text[], NULL, NULL, NULL, false)" in workflow_sql
    assert "('done', 'Done', 10, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('cancelled', 'Cancelled', 11, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('director_review', 'vcs', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('vcs', 'done', 'mark_done', ARRAY['ops']::text[]" in workflow_sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" not in workflow_sql


def test_vcs_close_role_is_not_an_implementation_owner() -> None:
    plan = build_plan(
        project="mefp",
        owner_user="mefp-agent",
        port=8878,
        implementer_roles=("app", "main"),
        vcs_close_role="archivist",
    )

    board_unit = render_board_unit(plan)
    workflow_sql = render_workflow_sql(plan)

    assert plan.implementer_roles == ("app", "main")
    assert plan.assignee_roles == ("unassigned", "designer", "app", "main", "archivist", "audit", "director", "user")
    assert plan.caller_roles == ("director", "designer", "app", "main", "archivist", "audit", "user")
    assert plan.operation_allowed_roles == (("mark_done", ("archivist",)),)
    assert "Environment=TICKET_BOARD_IMPLEMENTER_ROLES=app,main" in board_unit
    assert "Environment=TICKET_BOARD_ASSIGNEES=unassigned,designer,app,main,archivist,audit,director,user" in board_unit
    assert "Environment=TICKET_BOARD_CALLER_ROLES=director,designer,app,main,archivist,audit,user" in board_unit
    assert "Environment=TICKET_BOARD_OPERATION_ALLOWED_ROLES=mark_done=archivist" in board_unit
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in workflow_sql
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app', 'archivist']::text[]" not in workflow_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main']::text[]" in workflow_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main', 'archivist']::text[]" not in workflow_sql
    assert "('in_progress', 'audit', 'submit_to_audit', ARRAY['app', 'main']::text[]" in workflow_sql
    assert "('in_progress', 'audit', 'submit_to_audit', ARRAY['app', 'main', 'archivist']::text[]" not in workflow_sql
    assert "('vcs', 'VCS', 7, ARRAY['archivist']::text[]" in workflow_sql
    assert "('vcs', 'done', 'mark_done', ARRAY['archivist']::text[]" in workflow_sql
    assert (
        "ADD CONSTRAINT ticket_notification_state_last_implementer_assignee_check\n"
        "    CHECK (\n"
        "        last_implementer_assignee = ''\n"
        "        OR last_implementer_assignee IN ('app', 'main')"
    ) in workflow_sql
    assert "'archivist')" not in workflow_sql[
        workflow_sql.index("ticket_notification_state_last_implementer_assignee_check") :
        workflow_sql.index("ALTER TABLE ticket_board.ticket_notification_queue")
    ]


def test_vcs_close_role_is_removed_from_legacy_implementer_input() -> None:
    plan = build_plan(
        project="mefp",
        owner_user="mefp-agent",
        port=8878,
        implementer_roles=("app", "main", "archivist"),
        vcs_close_role="archivist",
    )

    workflow_sql = render_workflow_sql(plan)

    assert plan.implementer_roles == ("app", "main")
    assert "archivist" in plan.assignee_roles
    assert "archivist" in plan.caller_roles
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in workflow_sql
    assert "('vcs', 'VCS', 7, ARRAY['archivist']::text[]" in workflow_sql


def test_vcs_close_role_cannot_be_the_only_implementer() -> None:
    try:
        build_plan(
            project="mefp",
            owner_user="mefp-agent",
            port=8878,
            implementer_roles=("archivist",),
            vcs_close_role="archivist",
        )
        raise AssertionError("expected close-only implementer rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "at least one implementer role is required outside the VCS close role" in message


def test_non_pgu_project_can_configure_multiple_audit_roles() -> None:
    plan = build_plan(
        project="stellaris",
        owner_user="stellaris-agent",
        port=8871,
        implementer_roles=("main",),
        audit_roles=("audit_gemini", "audit_gpt"),
    )

    board_unit = render_board_unit(plan)
    workflow_sql = render_workflow_sql(plan)

    assert plan.audit_roles == ("audit_gemini", "audit_gpt")
    assert plan.assignee_roles == ("unassigned", "designer", "main", "audit_gemini", "audit_gpt", "director", "user")
    assert plan.caller_roles == ("director", "designer", "main", "audit_gemini", "audit_gpt", "user")
    assert "TICKET_BOARD_ASSIGNEES=unassigned,designer,main,audit_gemini,audit_gpt,director,user" in board_unit
    assert "TICKET_BOARD_CALLER_ROLES=director,designer,main,audit_gemini,audit_gpt,user" in board_unit
    assert (
        "TICKET_BOARD_OPERATION_ALLOWED_ROLES="
        "file_bug=main,audit_gemini,audit_gpt;"
        "start_task=main,director,audit_gemini,audit_gpt;"
        "complete_task=main,director,audit_gemini,audit_gpt;"
        "audit_sign_off=audit_gemini,audit_gpt;"
        "audit_kick_back=audit_gemini,audit_gpt"
    ) in board_unit
    assert "('audit', 'Audit', 3, ARRAY['audit_gemini', 'audit_gpt']::text[]" in workflow_sql
    assert "('audit', 'director_review', 'audit_sign_off', ARRAY['audit_gemini', 'audit_gpt']::text[]" in workflow_sql
    assert "('audit', 'in_progress', 'audit_kick_back', ARRAY['audit_gemini', 'audit_gpt']::text[]" in workflow_sql


def test_pgu_plan_rejects_vcs_close_override() -> None:
    try:
        build_plan(project="pgu", owner_user="agent", vcs_close_role="ops")
        raise AssertionError("expected pgu vcs override rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "--vcs-close-role is only for provisioned projects" in message


def test_pgu_plan_rejects_custom_audit_roles() -> None:
    try:
        build_plan(project="pgu", owner_user="agent", audit_roles=("audit_gemini", "audit_gpt"))
        raise AssertionError("expected pgu audit role override rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "custom audit roles are only for provisioned projects" in message


def test_non_pgu_project_can_omit_audit_role_and_stage() -> None:
    plan = build_plan(
        project="stellaris",
        owner_user="stellaris-agent",
        port=8871,
        include_designer=False,
        include_audit=False,
        implementer_roles=("code",),
    )

    workflow_sql = render_workflow_sql(plan)
    combined = "\n".join([json.dumps(plan.__dict__, sort_keys=True), render_board_unit(plan), workflow_sql])

    assert plan.draft_roles == ()
    assert plan.implementer_roles == ("code",)
    assert plan.assignee_roles == ("unassigned", "code", "director", "user")
    assert plan.caller_roles == ("director", "code", "user")
    assert "TICKET_BOARD_DRAFT_ROLES=" in combined
    assert "TICKET_BOARD_IMPLEMENTER_ROLES=code" in combined
    assert "TICKET_BOARD_ASSIGNEES=unassigned,code,director,user" in combined
    assert "TICKET_BOARD_CALLER_ROLES=director,code,user" in combined
    assert "('audit', 'Audit'" not in workflow_sql
    assert "ARRAY['audit']::text[]" not in workflow_sql
    assert "'audit_sign_off'" not in workflow_sql
    assert "'audit_kick_back'" not in workflow_sql
    assert "('in_progress', 'audit'" not in workflow_sql
    assert "('in_progress', 'director_review', 'submit_to_audit', ARRAY['code']::text[]" in workflow_sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" in workflow_sql
    assert (
        "ADD CONSTRAINT tickets_assignee_check CHECK "
        "(assignee IN ('unassigned', 'code', 'director', 'user'))"
    ) in workflow_sql


def test_add_role_sql_expands_existing_project_constraints_and_workflow_roles() -> None:
    plan = build_plan(
        project="mefp",
        owner_user="mefp-agent",
        port=8878,
        implementer_roles=("app", "main", "ops"),
    )
    sql = render_add_role_sql(plan, "ops")

    assert "Add implementer role ops to the existing project workflow for mefp" in sql
    assert "DELETE FROM ticket_board.tickets" not in sql
    assert "DELETE FROM ticket_board.workflow_stages" not in sql
    assert "DROP CONSTRAINT IF EXISTS tickets_assignee_check" in sql
    assert (
        "ADD CONSTRAINT tickets_assignee_check CHECK "
        "(assignee IN ('unassigned', 'designer', 'app', 'main', 'ops', 'audit', 'director', 'user'))"
    ) in sql
    assert (
        "ADD CONSTRAINT ticket_notification_state_last_implementer_assignee_check\n"
        "    CHECK (\n"
        "        last_implementer_assignee = ''\n"
        "        OR last_implementer_assignee IN ('app', 'main', 'ops')"
    ) in sql
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app', 'ops']::text[]" in sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main', 'ops']::text[]" in sql
    assert "('in_progress', 'audit', 'submit_to_audit', ARRAY['app', 'main', 'ops']::text[]" in sql
    assert "ON CONFLICT (name) DO UPDATE SET" in sql
    assert "ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE SET" in sql


def test_vcs_close_role_sql_updates_existing_project_close_workflow() -> None:
    plan = build_plan(
        project="mefp",
        owner_user="mefp-agent",
        port=8878,
        implementer_roles=("app", "main", "archivist"),
        vcs_close_role="archivist",
    )
    sql = render_vcs_close_role_sql(plan)

    assert "Configure VCS close role archivist for the existing project workflow for mefp" in sql
    assert "DELETE FROM ticket_board.tickets" not in sql
    assert "DELETE FROM ticket_board.workflow_stages" not in sql
    assert "DELETE FROM ticket_board.workflow_transitions\nWHERE action_name = 'mark_done';" in sql
    assert (
        "ADD CONSTRAINT workflow_stages_name_check CHECK "
        "(name IN ('draft', 'analysis', 'in_progress', 'audit', 'dat', 'user_review', 'director_review', 'vcs', 'done', 'cancelled'))"
    ) in sql
    assert (
        "ADD CONSTRAINT tickets_assignee_check CHECK "
        "(assignee IN ('unassigned', 'designer', 'app', 'main', 'archivist', 'audit', 'director', 'user'))"
    ) in sql
    assert "('vcs', 'VCS', 7, ARRAY['archivist']::text[]" in sql
    assert "('director_review', 'vcs', 'route', ARRAY['director']::text[]" in sql
    assert "('vcs', 'done', 'mark_done', ARRAY['archivist']::text[]" in sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" not in sql
    assert "ON CONFLICT (name) DO UPDATE SET" in sql
    assert "ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE SET" in sql


def test_vcs_close_role_sql_rejects_pgu_plan() -> None:
    pgu_plan = build_plan(project="pgu", owner_user="agent")
    try:
        render_vcs_close_role_sql(pgu_plan)
        raise AssertionError("expected pgu workflow override rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "only for provisioned project workflows" in message


def test_operator_commands_use_peer_portable_postgres_admin_invocations() -> None:
    plan = build_plan(project="stellaris", owner_user="stellaris-agent", port=8871)
    commands = render_operator_commands(plan)

    assert plan.admin_database_url == "postgresql:///stellaris_ticket_board?host=/var/run/postgresql"
    assert "user=postgres" not in "\n".join(
        line for line in commands.splitlines() if line.startswith("sudo ")
    )
    assert (
        "sudo cat 'stellaris-database.sql' | "
        "sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f -"
    ) in commands
    assert (
        "sudo cat '/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket_board/schema.sql' | "
        "sudo -u postgres psql -X -v ON_ERROR_STOP=1 "
        "'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' "
        "-f -"
    ) in commands
    assert (
        "sudo env "
        "TICKET_BOARD_ADMIN_DATABASE_URL='postgresql:///stellaris_ticket_board?host=/var/run/postgresql' "
        "'/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket-board-migrate'"
    ) in commands
    assert (
        "sudo cat 'stellaris-workflow.sql' | "
        "sudo -u postgres psql -X -v ON_ERROR_STOP=1 "
        "'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' "
        "-f -"
    ) in commands
    assert (
        "sudo cat '/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket_board/rbac.sql' | "
        "sudo -u postgres psql -X -v ON_ERROR_STOP=1 "
        "'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' "
        "-f -"
    ) in commands
    assert "sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f 'stellaris-database.sql'" not in commands
    assert "sudo -u postgres psql -X -v ON_ERROR_STOP=1 'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' -f '/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket_board/schema.sql'" not in commands
    assert "sudo -u postgres env TICKET_BOARD_ADMIN_DATABASE_URL=" not in commands
    assert "sudo -u postgres psql -X -v ON_ERROR_STOP=1 'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' -f 'stellaris-workflow.sql'" not in commands
    assert "sudo -u postgres psql -X -v ON_ERROR_STOP=1 'postgresql:///stellaris_ticket_board?host=/var/run/postgresql' -f '/home/stellaris-agent/stellaris-ticketboard-live/current/scripts/ticket_board/rbac.sql'" not in commands


def test_operator_commands_create_owned_parents_before_systemd_paths() -> None:
    plan = build_plan(project="otto", owner_user="otto-agent", port=8873)
    commands = render_operator_commands(plan)
    board_root = "sudo install -d -m 0755 -o 'otto-agent' -g 'otto-agent' '/home/otto-agent/otto-ticketboard-live'"
    deploy = (
        "sudo env TICKET_BOARD_PROJECT='otto' "
        "TICKET_BOARD_COMMIT_GIT_DIR='/data/git/otto_scheduler.git' "
        f"SOURCE_REPO='{ROOT}' "
        "BOARD_ROOT='/home/otto-agent/otto-ticketboard-live'"
    )
    claude_parent = "sudo install -d -m 0755 -o 'otto-agent' -g 'otto-agent' '/home/otto-agent/.claude'"
    asset_frame_leaf = (
        "sudo install -d -m 0775 -o 'otto-agent' -g 'otto-agent' "
        "'/home/otto-agent/.claude/otto-tickets-assets' "
        "'/home/otto-agent/.claude/otto-ticket-frames'"
    )
    config_parents = (
        "sudo install -d -m 0755 -o 'otto-agent' -g 'otto-agent' "
        "'/home/otto-agent/.config' "
        "'/home/otto-agent/.config/systemd' "
        "'/home/otto-agent/.config/systemd/user'"
    )
    listener_install = (
        "sudo install -m 0644 -o 'otto-agent' -g 'otto-agent' "
        "'otto-ticket-board-notify-listener.service' "
        "'/home/otto-agent/.config/systemd/user/otto-ticket-board-notify-listener.service'"
    )

    assert board_root in commands
    assert "sudo install -d -m 0755 '/home/otto-agent/otto-ticketboard-live'" not in commands
    assert commands.index(board_root) < commands.index(deploy)
    assert claude_parent in commands
    assert asset_frame_leaf in commands
    assert commands.index(claude_parent) < commands.index(asset_frame_leaf)
    assert config_parents in commands
    assert listener_install in commands
    assert commands.index(config_parents) < commands.index(listener_install)


def test_operator_commands_wait_for_linger_user_bus_before_user_systemctl() -> None:
    plan = build_plan(project="otto", owner_user="otto-agent", port=8873)
    commands = render_operator_commands(plan)

    enable_linger = "sudo loginctl enable-linger 'otto-agent'"
    owner_uid = 'owner_uid="$(id -u \'otto-agent\')"'
    owner_runtime = 'owner_runtime_dir="/run/user/$owner_uid"'
    owner_bus = 'owner_bus="$owner_runtime_dir/bus"'
    poll_socket = '[ -S "$owner_bus" ] && break'
    timeout_error = (
        "ERROR: user bus for otto-agent did not appear at $owner_bus "
        "within 30s after enable-linger"
    )
    daemon_reload = (
        "sudo -u 'otto-agent' env XDG_RUNTIME_DIR=\"$owner_runtime_dir\" "
        "DBUS_SESSION_BUS_ADDRESS=\"unix:path=$owner_bus\" "
        "systemctl --user daemon-reload"
    )
    enable_listener = (
        "sudo -u 'otto-agent' env XDG_RUNTIME_DIR=\"$owner_runtime_dir\" "
        "DBUS_SESSION_BUS_ADDRESS=\"unix:path=$owner_bus\" "
        "systemctl --user enable --now otto-ticket-board-notify-listener.service"
    )

    assert enable_linger in commands
    assert owner_uid in commands
    assert owner_runtime in commands
    assert owner_bus in commands
    assert "for _ in $(seq 1 300); do" in commands
    assert poll_socket in commands
    assert timeout_error in commands
    assert daemon_reload in commands
    assert enable_listener in commands
    assert "/run/user/$(id -u 'otto-agent')" not in commands
    assert commands.index(enable_linger) < commands.index(owner_uid)
    assert commands.index(owner_bus) < commands.index(daemon_reload)
    assert commands.index(timeout_error) < commands.index(daemon_reload)
    assert commands.index(daemon_reload) < commands.index(enable_listener)


def test_operator_commands_can_verify_existing_owner_linger_without_modifying_it() -> None:
    plan = build_plan(project="otto", owner_user="otto-agent", port=8873)
    commands = render_operator_commands(plan, enable_owner_linger=False)

    assert "sudo loginctl enable-linger 'otto-agent'" not in commands
    assert "sudo loginctl show-user 'otto-agent' -p Linger --value 2>/dev/null | grep -qx yes" in commands
    assert "switchyard refuses to modify an existing owner user" in commands
    assert "enable linger or start that user" in commands
    assert "systemd manager" in commands
    assert commands.index("loginctl show-user") < commands.index("owner_uid=")


def test_board_service_traversal_capability_controls_owner_home_acls() -> None:
    grant_plan = build_plan(
        project="zeta",
        owner_user="zeta-agent",
        port=8874,
        board_service_traversal=True,
    )
    deny_plan = build_plan(
        project="zeta",
        owner_user="zeta-agent",
        port=8874,
        board_service_traversal=False,
    )

    grant_commands = render_operator_commands(grant_plan)
    deny_commands = render_operator_commands(deny_plan)

    assert grant_plan.board_service_traversal is True
    assert deny_plan.board_service_traversal is False
    assert "sudo setfacl -m u:boardsvc:--x '/home/zeta-agent'" in grant_commands
    assert "sudo setfacl -m u:boardsvc:--x '/home/zeta-agent/.claude'" in grant_commands
    assert "sudo setfacl -R -m u:boardsvc:rx '/home/zeta-agent/zeta-ticketboard-live'" in grant_commands
    assert "sudo setfacl -R -m u:boardsvc:rwx '/home/zeta-agent/.claude/zeta-tickets-assets'" in grant_commands
    assert "if ! getent passwd 'boardsvc' >/dev/null 2>&1; then" in deny_commands
    assert "sudo useradd -r -M -d /nonexistent -s \"$service_shell\" 'boardsvc'" in deny_commands
    assert "sudo setfacl -m u:boardsvc:--x '/home/zeta-agent'" not in deny_commands
    assert "sudo setfacl -m u:boardsvc:--x '/home/zeta-agent/.claude'" not in deny_commands
    assert "sudo setfacl -R -m u:boardsvc:rx '/home/zeta-agent/zeta-ticketboard-live'" not in deny_commands
    assert "sudo setfacl -R -m u:boardsvc:rwx '/home/zeta-agent/.claude/zeta-tickets-assets'" not in deny_commands
    assert "board_service_traversal=false" in deny_commands
    assert "board health check will fail" in deny_commands


def test_owned_readwrite_path_layout_passes_systemd_verify() -> None:
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        return

    current_user = pwd.getpwuid(os.getuid()).pw_name
    with tempfile.TemporaryDirectory(prefix="pgu-provision-systemd-path.") as tmp:
        root = Path(tmp) / "home" / current_user
        asset_dir = root / ".claude" / "otto-tickets-assets"
        frame_dir = root / ".claude" / "otto-ticket-frames"
        asset_dir.mkdir(parents=True)
        frame_dir.mkdir(parents=True)
        unit = Path(tmp) / "otto-ticket-board.service"
        unit.write_text(
            "\n".join(
                [
                    "[Unit]",
                    "Description=Path ownership verification",
                    "",
                    "[Service]",
                    "Type=oneshot",
                    f"User={current_user}",
                    "ExecStart=/bin/true",
                    f"ReadWritePaths={asset_dir} {frame_dir}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run([systemd_analyze, "verify", str(unit)], check=True)


def test_pgu_project_render_matches_live_port_database_and_frame_dir() -> None:
    plan = build_plan(project="pgu", owner_user="agent")
    board_unit = render_board_unit(plan)
    tmpfiles = render_tmpfiles(plan)
    commands = render_operator_commands(plan)

    assert plan.port == 8770
    assert plan.database == "pgu"
    assert plan.ticket_prefix == "PGU"
    assert plan.board_unit == "pgu-ticket-board.service"
    assert plan.runtime_directory == "pgu-ticket-board"
    assert plan.socket_path == "/run/pgu-ticket-board/ticket-board.sock"
    assert plan.board_root == "/home/agent/pgu-ticketboard-live"
    assert plan.asset_dir == "/home/agent/.claude/pgu-tickets-assets"
    assert plan.frame_dir == "/tmp/pgu-frames"
    assert plan.commit_git_dir == "/data/git/switchyard.git:/data/git/pgu.git"
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


def test_project_slug_allows_underscore_and_rejects_dash() -> None:
    plan = build_plan(project="otto_scheduler", owner_user="otto_scheduler-agent", port=8899)

    assert plan.database == "otto_scheduler_ticket_board"
    assert plan.board_unit == "otto_scheduler-ticket-board.service"

    try:
        build_plan(project="otto-scheduler", owner_user="otto-scheduler-agent", port=8899)
        raise AssertionError("expected dashed project slug rejection")
    except SystemExit as exc:
        message = str(exc)

    assert "project must match ^[a-z0-9][a-z0-9_]{0,39}$" in message


def test_custom_ticket_prefix_is_rendered_into_board_service_environment() -> None:
    plan = build_plan(project="otto", owner_user="otto-agent", ticket_prefix="ot")
    board_unit = render_board_unit(plan)

    assert plan.ticket_prefix == "OT"
    assert "EnvironmentFile=-/home/otto-agent/.config/otto/ticket-board.env" in board_unit
    assert "Environment=TICKET_BOARD_PROJECT=otto" in board_unit
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=OT" in board_unit
    assert "Environment=TICKET_BOARD_COMMIT_GIT_DIR=/data/git/otto_scheduler.git" in board_unit


def test_project_units_honor_ticket_board_python_override() -> None:
    old_value = os.environ.get("TICKET_BOARD_PYTHON")
    os.environ["TICKET_BOARD_PYTHON"] = "/opt/switchyard/venv/bin/python"
    try:
        plan = build_plan(project="otto", owner_user="otto-agent")
        assert (
            "ExecStart=/opt/switchyard/venv/bin/python "
            "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board.py"
        ) in render_board_unit(plan)
        assert (
            "ExecStart=/opt/switchyard/venv/bin/python "
            "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-notify-listener"
        ) in render_listener_unit(plan)
    finally:
        if old_value is None:
            os.environ.pop("TICKET_BOARD_PYTHON", None)
        else:
            os.environ["TICKET_BOARD_PYTHON"] = old_value


def test_project_units_ignore_non_executable_shared_python() -> None:
    old_override = os.environ.pop("TICKET_BOARD_PYTHON", None)
    old_shared = os.environ.get("SWITCHYARD_SHARED_PYTHON")
    with tempfile.TemporaryDirectory() as tmpdir:
        candidate = Path(tmpdir) / "python"
        candidate.write_text("#!/bin/sh\n", encoding="utf-8")
        candidate.chmod(0o644)
        os.environ["SWITCHYARD_SHARED_PYTHON"] = str(candidate)
        try:
            plan = build_plan(project="otto", owner_user="otto-agent")
            assert "ExecStart=/usr/bin/python3 " in render_board_unit(plan)
            assert "ExecStart=/usr/bin/python3 " in render_listener_unit(plan)
            assert str(candidate) not in render_board_unit(plan)
            assert str(candidate) not in render_listener_unit(plan)
        finally:
            if old_shared is None:
                os.environ.pop("SWITCHYARD_SHARED_PYTHON", None)
            else:
                os.environ["SWITCHYARD_SHARED_PYTHON"] = old_shared
            if old_override is not None:
                os.environ["TICKET_BOARD_PYTHON"] = old_override


def test_live_legacy_project_commit_repositories_are_rendered() -> None:
    pgu_plan = build_plan(project="pgu", owner_user="agent")
    mefp_plan = build_plan(project="mefp", owner_user="stellaris-agent")
    otto_plan = build_plan(project="otto", owner_user="otto-agent")
    porter_plan = build_plan(project="porter", owner_user="porter-agent")

    assert pgu_plan.commit_git_dir == "/data/git/switchyard.git:/data/git/pgu.git"
    assert mefp_plan.commit_git_dir == "/data/git/stellaris-fixpatch.git"
    assert otto_plan.commit_git_dir == "/data/git/otto_scheduler.git"
    assert porter_plan.commit_git_dir == "/home/porter-agent/.local/state/switchyard/projects/porter/control.git"


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

assert app.ASSIGNEES == ('unassigned', 'designer', 'builder', 'audit', 'director', 'user')
assert app.CALLER_ROLES == ('director', 'designer', 'builder', 'audit', 'user')
assert server.DRAFT_ROLES == {'designer'}
assert server.IMPLEMENTER_ROLES == {'builder'}
assert server.OPERATION_ALLOWED_ROLES['release_draft'] == {'designer', 'director', 'user'}
assert server.OPERATION_ALLOWED_ROLES['start_work'] == {'builder'}
assert server.OPERATION_ALLOWED_ROLES['submit_to_audit'] == {'builder'}
assert 'builder' in server.OPERATION_ALLOWED_ROLES['file_bug']
assert 'ops' not in server.OPERATION_ALLOWED_ROLES['start_work']
assert server.OPERATION_ALLOWED_ROLES == server.DEFAULT_OPERATION_ALLOWED_ROLES
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "TICKET_BOARD_ASSIGNEES": "unassigned,designer,builder,audit,director,user",
        "TICKET_BOARD_CALLER_ROLES": "director,designer,builder,audit,user",
        "TICKET_BOARD_DRAFT_ROLES": "designer",
        "TICKET_BOARD_IMPLEMENTER_ROLES": "builder",
    }
    subprocess.run([sys.executable, "-c", script], check=True, env=env)


def test_operation_allowed_roles_environment_overrides_python_gate() -> None:
    script = """
from scripts.ticket_board import server

assert server.OPERATION_ALLOWED_ROLES['mark_done'] == {'ops'}
assert server.OPERATION_ALLOWED_ROLES['route'] == {'director'}
assert server.DEFAULT_OPERATION_ALLOWED_ROLES['mark_done'] == {'director'}
assert server.OPERATION_ALLOWED_ROLES != server.DEFAULT_OPERATION_ALLOWED_ROLES
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "TICKET_BOARD_ASSIGNEES": "unassigned,ops,director,user",
        "TICKET_BOARD_CALLER_ROLES": "director,ops,user",
        "TICKET_BOARD_IMPLEMENTER_ROLES": "ops",
        "TICKET_BOARD_OPERATION_ALLOWED_ROLES": "mark_done=ops",
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
                "--ticket-prefix",
                "PRT",
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
        assert plan["ticket_prefix"] == "PRT"
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
        assert (output_dir / "operator-commands.sh").read_text(encoding="utf-8").splitlines()[:2] == [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
        ]

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
        assert "Seed the default project workflow for porter" in workflow_result.stdout
        assert "ARRAY['builder']::text[]" in workflow_result.stdout

        multi_audit_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ticket-board-provision-project"),
                "--project",
                "porter",
                "--owner-user",
                "otto-agent",
                "--implementer-role",
                "builder",
                "--audit-role",
                "audit_gemini",
                "--audit-role",
                "audit_gpt",
                "--render",
                "workflow-sql",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert "ARRAY['audit_gemini', 'audit_gpt']::text[]" in multi_audit_result.stdout


def main() -> int:
    run_module_tests(globals())
    print("ticket_board_project_provision_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
