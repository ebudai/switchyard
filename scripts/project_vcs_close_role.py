"""Choosing a provisioned tenant's VCS close role: `switchyard set-vcs-close-role`.

`set_project_vcs_close_role_command` regenerates the tenant's plan from its
recorded plan data with the named, existing role as the one allowed to mark
work done (`_project_plan_for_vcs_close_role`, which refuses pgu, an unknown or
malformed role, and a tenant whose workflow is declared). It checks the SQL
renders before anything is written, writes the plan, board unit and workflow
SQL beside the configuration for the owner (`_write_vcs_close_role_artifacts`),
applies the SQL as postgres (`_apply_vcs_close_role_board_sql`), and only then
installs and restarts the board unit.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-356). The launcher
imports this module at its top and re-exports every name, so the CLI and the
suites call the same objects. Every launcher facility these use -- the plan
data readers and builder shared with `project_role_add`, the renderers, the
owner file and JSON writers, the unit installer -- and every name defined here
that another definition here calls is read from `team_launcher` when it runs,
as it was. The standard-library names are this module's own imports, the same
objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


def _project_plan_for_vcs_close_role(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
) -> ProjectBoardProvision:
    from scripts import team_launcher as launcher

    role = role_name.strip().lower()
    if not launcher.ROLE_RE.fullmatch(role):
        raise SystemExit("team-launcher: VCS close role must match ^[a-z][a-z0-9_-]{0,63}$")
    if config.project == "pgu":
        raise SystemExit("team-launcher: pgu uses the full built-in workflow; set-vcs-close-role is only for provisioned projects")
    configured_roles = {configured.role for configured in config.roles}
    if role not in configured_roles:
        raise SystemExit(f"team-launcher: VCS close role {role!r} does not exist in project {config.project}")
    plan_data = launcher._plan_data_from_config(config, config_path)
    if plan_data.get("workflow") or launcher._load_json(config_path).get("workflow"):
        raise SystemExit("use ticket-board-workflow apply to update configured roles and stages")
    audit_roles = launcher._configured_audit_roles(config, plan_data=plan_data)
    implementer_roles = launcher._configured_implementer_roles(config, plan_data=plan_data)
    include_designer = any(configured.role == "designer" for configured in config.roles)
    return launcher.build_plan(
        project=config.project,
        project_name=config.project_name,
        owner_user=str(launcher._loaded_plan_field(plan_data, "owner_user", config.run_as_user or launcher.current_user_name())),
        owner_home=launcher._owner_home_from_plan_data(config, plan_data),
        control_user=launcher._regenerated_control_user(config, plan_data),
        port=launcher._loaded_plan_field(plan_data, "port", None),
        database=launcher._loaded_plan_field(plan_data, "database", None),
        source_repo=Path(str(launcher._loaded_plan_field(plan_data, "source_repo", launcher._repo_root()))),
        commit_git_dir=launcher._commit_git_dir_from_plan_data(config, plan_data),
        ticket_prefix=str(launcher._loaded_plan_field(plan_data, "ticket_prefix", config.ticket_prefix)),
        board_root=(
            Path(str(plan_data["board_root"]))
            if plan_data.get("board_root")
            else launcher._tenant_board_root_from_config(config)
        ),
        asset_dir=Path(str(plan_data["asset_dir"])) if plan_data.get("asset_dir") else None,
        frame_dir=Path(str(plan_data["frame_dir"])) if plan_data.get("frame_dir") else None,
        implementer_roles=implementer_roles,
        include_designer=include_designer,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
        board_service_traversal=bool(launcher._loaded_plan_field(plan_data, "board_service_traversal", True)),
        vcs_close_role=role,
    )


def _write_vcs_close_role_artifacts(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    provision_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config: ProjectConfig,
) -> tuple[Path, Path, Path]:
    from scripts import team_launcher as launcher

    plan_path = provision_dir / "plan.json"
    board_unit_path = provision_dir / plan.board_unit
    workflow_sql_path = provision_dir / f"{plan.project}-vcs-close-role.sql"
    launcher._write_json_atomic(plan_path, {key: value for key, value in plan.__dict__.items()})
    board_unit_path.write_text(launcher.render_board_unit(plan), encoding="utf-8")
    workflow_sql_path.write_text(launcher.render_vcs_close_role_sql(plan), encoding="utf-8")
    for path in (plan_path, board_unit_path, workflow_sql_path):
        launcher.ensure_owner_file(config, path, runner=runner)
    return plan_path, board_unit_path, workflow_sql_path


def _apply_vcs_close_role_board_sql(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    sql = launcher.render_vcs_close_role_sql(plan)
    result = runner(
        ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", plan.admin_database_url, "-f", "-"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        reason = launcher._proc_failure_reason(result, f"psql failed with exit {result.returncode}")
        raise SystemExit(
            f"team-launcher: failed to configure VCS close role {role_name} in board database {plan.database}: {reason}"
        )


def set_project_vcs_close_role_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    plan = launcher._project_plan_for_vcs_close_role(config, config_path=config_path, role_name=role_name)
    role = dict(plan.operation_allowed_roles)["mark_done"][0]
    launcher.render_vcs_close_role_sql(plan)
    plan_path, board_unit_path, workflow_sql_path = launcher._write_vcs_close_role_artifacts(
        plan,
        role_name=role,
        provision_dir=config_path.parent,
        runner=runner,
        config=config,
    )
    launcher._apply_vcs_close_role_board_sql(plan, role_name=role, runner=runner)
    launcher._install_and_restart_board_unit(plan, board_unit_path=board_unit_path, runner=runner)
    print_func(
        f"team-launcher: set VCS close role for {config.project} to {role}; "
        f"updated {plan_path}, {board_unit_path}, and {workflow_sql_path}"
    )
    return 0
