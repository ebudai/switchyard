"""Adding a role to a provisioned tenant: `switchyard add-role`'s command and its helpers.

`add_project_role_command` validates the role and its CLI (an operator at a
terminal chooses from the supported list), checks the regenerated plan's SQL
before anything is written, then -- unless it is recovering a half-added role --
appends the role to the configuration (`_write_added_role_config`, with
`_add_role_payload`, the slot and layout rules and the design artifact). It
writes the updated plan artifacts, registers the role in the board database
(`_apply_add_role_board_sql`), restarts the board unit, prepares the role's
worktree as the owner, and starts its pane -- or, when the role's Unix account
does not exist yet, hands the operator the commands that create it and starts
nothing (SYRD-39, SYRD-51, SYRD-115).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-355). The launcher
imports this module at its top and re-exports every name, so `switchyard
add-role` and the suites call the same objects. Every launcher facility these
use -- the plan loaders and builder, the renderers, the owner runner, the
worktree and pane helpers, the terminal selector, the constants -- and every
name defined here that another definition here calls is read from
`team_launcher` when it runs, as it was. So is the launcher's own file, which
the pane script falls back to beside. `local_account_exists`, bound as a
default when the command is defined, comes from the leaf
`scripts.host_accounts`, the same object the launcher re-exports. The
standard-library modules are this module's own imports, the same objects. This
module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.host_accounts import local_account_exists

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision, ProjectConfig


def _is_recognized_generated_project_layout(config: ProjectConfig, *, config_path: Path) -> bool:
    from scripts import team_launcher as launcher

    if not launcher._is_generated_project_layout_template(config, config_path=config_path):
        return False
    role_count = sum(1 for role in config.roles if not role.detached)
    try:
        existing_layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return existing_layout in launcher._known_generated_project_layout_payloads(role_count)


def _vcs_close_role_from_plan_data(plan_data: dict[str, Any]) -> str | None:
    for item in plan_data.get("operation_allowed_roles") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        operation, roles = item
        if operation != "mark_done" or not isinstance(roles, (list, tuple)) or not roles:
            continue
        return str(roles[0])
    return None


def _project_plan_for_added_role(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    audit_role: bool = False,
) -> ProjectBoardProvision:
    from scripts import team_launcher as launcher

    plan_data = launcher._plan_data_from_config(config, config_path)
    if plan_data.get("workflow") or launcher._load_json(config_path).get("workflow"):
        raise SystemExit("use ticket-board-workflow apply to update configured roles and stages")
    implementer_roles = launcher._configured_implementer_roles(
        config,
        plan_data=plan_data,
        extra_role=None if audit_role else role_name,
    )
    audit_roles = launcher._configured_audit_roles(
        config,
        plan_data=plan_data,
        extra_role=role_name if audit_role else None,
    )
    include_designer = any(role.role == "designer" for role in config.roles)
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
        vcs_close_role=launcher._vcs_close_role_from_plan_data(plan_data),
    )


def _next_visible_role_slot(config: ProjectConfig) -> int:
    slots = [role.slot for role in config.roles if not role.detached and role.slot is not None]
    return max(slots) + 1 if slots else 0


def _add_role_payload(
    config: ProjectConfig,
    *,
    role_name: str,
    cli: str,
    detached: bool,
    slot: int | None,
    directorctl: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cli": [cli],
        "live_commands": [cli],
        "role": role_name,
        "target": f"{config.project}-{role_name}:0.0",
        "tmux_session": f"{config.project}-{role_name}",
        "yolo": True,
    }
    if directorctl:
        payload["env"] = {"TICKET_BOARD_DIRECTORCTL": directorctl}
    if not detached:
        if slot is None:
            raise ValueError("visible role requires slot")
        payload["slot"] = slot
    else:
        payload["detached"] = True
    if config.control_repository is not None and config.worktree_base is not None:
        payload["workdir"] = str(config.worktree_base / role_name)
    elif config.repository is not None:
        payload["workdir"] = str(config.repository)
    return payload


def _update_project_design_artifact_for_role(
    config: ProjectConfig,
    config_path: Path,
    *,
    role_name: str,
    cli: str,
    audit_role: bool = False,
) -> Path | None:
    from scripts import team_launcher as launcher

    artifact_path = config_path.parent.parent / f"{config.project}.project.json"
    try:
        artifact = launcher._load_json(artifact_path)
    except OSError:
        return None
    project_data = artifact.get("project")
    if not isinstance(project_data, dict):
        return None
    roles_key = "audit_roles" if audit_role else "roles"
    raw_roles = project_data.get(roles_key)
    if isinstance(raw_roles, list):
        roles = [str(item).strip().lower() for item in raw_roles if str(item).strip()]
    else:
        roles = []
    if role_name not in roles:
        roles.append(role_name)
        project_data[roles_key] = roles
    if audit_role:
        project_data["include_audit"] = True
    raw_role_clis = project_data.get("role_clis")
    if not isinstance(raw_role_clis, dict):
        raw_role_clis = {}
        project_data["role_clis"] = raw_role_clis
    raw_role_clis[role_name] = cli
    launcher._write_json_atomic(artifact_path, artifact)
    return artifact_path


def _write_added_role_config(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    cli: str,
    detached: bool,
    slot: int | None,
    relayout: bool = False,
    audit_role: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[ProjectConfig, bool]:
    from scripts import team_launcher as launcher

    if any(role.role == role_name for role in config.roles):
        raise SystemExit(f"team-launcher: role {role_name!r} already exists in project {config.project}")
    if slot is not None and slot < 0:
        raise SystemExit("team-launcher: add-role slot must be non-negative")
    should_regenerate_layout = False
    if not detached:
        visible_count = sum(1 for role in config.roles if not role.detached)
        if visible_count >= launcher.MAX_VISIBLE_PANES_PER_WINDOW:
            raise SystemExit(
                f"team-launcher: cannot add {role_name} as visible; at most {launcher.MAX_VISIBLE_PANES_PER_WINDOW} panes "
                "can be visible in one window; add it with --detached or detach another role first"
            )
        if slot is None:
            slot = launcher._next_visible_role_slot(config)
        occupant = next(
            (role for role in config.roles if not role.detached and role.slot == slot),
            None,
        )
        if occupant is not None:
            raise SystemExit(f"team-launcher: cannot add {role_name} to slot {slot}; slot is occupied by {occupant.role}")
        next_slot = launcher._next_visible_role_slot(config)
        recognized_generated_layout = launcher._is_recognized_generated_project_layout(config, config_path=config_path)
        if recognized_generated_layout:
            if slot != next_slot:
                raise SystemExit(f"team-launcher: generated layouts append new visible roles at slot {next_slot}")
            should_regenerate_layout = True
        elif relayout:
            if slot != next_slot:
                raise SystemExit(
                    f"team-launcher: --relayout regenerates visible roles contiguously; "
                    f"add {role_name} at slot {next_slot} or omit --slot"
                )
            should_regenerate_layout = True
        else:
            layout_slot_count = launcher._layout_slot_count(config)
            if slot < layout_slot_count:
                should_regenerate_layout = False
            else:
                generated_count = max(next_slot + 1, visible_count + 1)
                raise SystemExit(
                    f"team-launcher: cannot add {role_name} to visible slot {slot}; layout {config.layout} "
                    f"has {layout_slot_count} slot(s), so no pane exists for the new role; use --detached "
                    f"to add it headless, or pass --relayout to replace the existing layout with a generated "
                    f"{generated_count}-pane layout"
                )
    raw_config = launcher._load_json(config_path)
    raw_roles = raw_config.get("roles")
    if not isinstance(raw_roles, list):
        raise SystemExit(f"{config_path} must define a roles list")
    board_root = launcher._tenant_board_root_from_config_or_plan(config, config_path)
    directorctl = str(board_root / "current" / "scripts" / "directorctl") if board_root is not None else ""
    raw_roles.append(
        launcher._add_role_payload(
            config,
            role_name=role_name,
            cli=cli,
            detached=detached,
            slot=slot,
            directorctl=directorctl,
        )
    )
    launcher._write_json_atomic(config_path, raw_config)
    launcher.ensure_owner_file(config, config_path, runner=runner)
    updated_config = launcher.load_project_config(config.project, config_path)
    if should_regenerate_layout:
        visible_count = max(
            (role.slot or 0) for role in updated_config.roles if not role.detached
        ) + 1
        updated_config.layout.write_text(
            json.dumps(launcher._new_project_layout_payload(visible_count), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        launcher.ensure_owner_file(updated_config, updated_config.layout, runner=runner)
    artifact_path = launcher._update_project_design_artifact_for_role(
        updated_config,
        config_path,
        role_name=role_name,
        cli=cli,
        audit_role=audit_role,
    )
    if artifact_path is not None:
        launcher.ensure_owner_file(updated_config, artifact_path, runner=runner)
    return launcher.load_project_config(config.project, config_path), should_regenerate_layout


def _write_updated_project_plan_artifacts(
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
    add_role_sql_path = provision_dir / f"{plan.project}-add-role.sql"
    launcher._write_json_atomic(plan_path, {key: value for key, value in plan.__dict__.items()})
    board_unit_path.write_text(launcher.render_board_unit(plan), encoding="utf-8")
    add_role_sql_path.write_text(launcher.render_add_role_sql(plan, role_name), encoding="utf-8")
    for path in (plan_path, board_unit_path, add_role_sql_path):
        launcher.ensure_owner_file(config, path, runner=runner)
    return plan_path, board_unit_path, add_role_sql_path


def _apply_add_role_board_sql(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    sql = launcher.render_add_role_sql(plan, role_name)
    result = runner(
        ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", plan.admin_database_url, "-f", "-"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        reason = launcher._proc_failure_reason(result, f"psql failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to register role {role_name} in board database {plan.database}: {reason}")


def add_project_role_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    cli: str = "",
    detached: bool = False,
    slot: int | None = None,
    relayout: bool = False,
    audit_role: bool = False,
    start: bool = True,
    script_path: Path | None = None,
    pane_state_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    # A new role can only be started once the Unix account it must run as
    # exists; injectable so tests can state that precondition explicitly.
    account_exists: Callable[[str], bool] = local_account_exists,
    interactive: bool | None = None,
    input_func: Callable[[str], str] = input,
) -> int:
    from scripts import team_launcher as launcher

    if not cli:
        # The same list `switchyard new` and a runtime switch offer. A flag
        # still decides it for a script; what changes is that an operator at a
        # terminal no longer has to know the four names (SYRD-115).
        if sys.stdin.isatty() if interactive is None else interactive:
            cli = launcher.terminal_select.select_one(
                launcher._runtime_field(role_name, default="codex"),
                input_func=input_func,
                print_func=print_func,
            )
        else:
            cli = "codex"
    role = (
        launcher._validate_new_project_audit_role(role_name, context="audit role")
        if audit_role
        else launcher._validate_new_project_implementer_role(role_name, context="role")
    )
    cli_name = launcher._validate_new_project_cli(cli, context=f"CLI for {role}")
    preflight_plan = launcher._project_plan_for_added_role(
        config,
        config_path=config_path,
        role_name=role,
        audit_role=audit_role,
    )
    launcher.render_add_role_sql(preflight_plan, role)
    existing_role = next((candidate for candidate in config.roles if candidate.role == role), None)
    recovering_existing_role = existing_role is not None
    if recovering_existing_role:
        updated_config = config
        regenerated_layout = False
    else:
        updated_config, regenerated_layout = launcher._write_added_role_config(
            config,
            config_path=config_path,
            role_name=role,
            cli=cli_name,
            detached=detached,
            slot=slot,
            relayout=relayout,
            audit_role=audit_role,
            runner=runner,
        )
    plan = launcher._project_plan_for_added_role(
        updated_config,
        config_path=config_path,
        role_name=role,
        audit_role=audit_role,
    )
    _plan_path, board_unit_path, _sql_path = launcher._write_updated_project_plan_artifacts(
        plan,
        role_name=role,
        provision_dir=config_path.parent,
        runner=runner,
        config=updated_config,
    )
    launcher._apply_add_role_board_sql(plan, role_name=role, runner=runner)
    launcher._install_and_restart_board_unit(plan, board_unit_path=board_unit_path, runner=runner)
    role_runner = runner
    if updated_config.run_as_user and launcher.current_user_name() != updated_config.run_as_user:
        role_runner = launcher._owner_project_git_runner(
            owner_user=updated_config.run_as_user,
            project_dir=updated_config.repository or updated_config.pane_launcher or Path("/"),
            owned_roots=launcher._control_repository_owned_roots(updated_config),
            runner=runner,
        )
    worktree_result = launcher.ensure_project_worktrees(
        replace(updated_config, roles=[launcher._role_by_name(updated_config, role)]),
        refresh=True,
        runner=role_runner,
    )
    if not worktree_result.ok:
        reason = next(iter(worktree_result.failed_roles.values()), "unknown error")
        raise SystemExit(f"team-launcher: failed to prepare worktree for {role}: {reason}")
    pending_account_commands = ""
    if start:
        pane_role = launcher._role_by_name(updated_config, role)
        pane_user = launcher.role_run_as_user(updated_config, pane_role)
        # A role started under the wrong account has the wrong uid, and the
        # board would give it either no authority or another role's. Refuse to
        # start it and hand the operator the commands that create the account,
        # rather than starting something that looks fine and is not (SYRD-39).
        if pane_user and not account_exists(pane_user):
            from scripts.ticket_board.project_provision import role_account_commands

            add_role_owner = updated_config.run_as_user or launcher.current_user_name()
            pending_account_commands = role_account_commands(
                updated_config.project,
                role,
                add_role_owner,
                launcher.board_service_user(updated_config),
                # The whole role set, including the one just added: the control
                # interface is installed as one file (SYRD-51).
                role_accounts=launcher.role_control_accounts(updated_config),
                worktree=pane_role.workdir,
                owner_home=str(launcher.home_dir_for_user(add_role_owner) or Path("/home") / add_role_owner),
                worktree_base=str(
                    updated_config.worktree_base or Path(pane_role.workdir).parent
                ),
                control_repository=(
                    str(updated_config.control_repository.expanduser())
                    if updated_config.control_repository is not None
                    else ""
                ),
            )
        else:
            pane_script_path = updated_config.pane_launcher or script_path or Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME)
            pane_args = launcher.pane_command_args(
                updated_config.project,
                pane_role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=pane_script_path,
                pane_state_dir=pane_state_dir or launcher.default_pane_state_dir_for_user(pane_user, project=updated_config.project),
                skip_launcher_check=True,
                no_attach=True,
                run_as_user=pane_user,
            )
            pane_proc = runner(pane_args)
            if pane_proc.returncode != 0:
                raise SystemExit(f"team-launcher: added {role}, but failed to start its pane with exit {pane_proc.returncode}")
    if recovering_existing_role:
        message = f"team-launcher: role {role} already exists in {updated_config.project}; reapplied board registration"
    else:
        role_label = "auditor role" if audit_role else "role"
        message = f"team-launcher: added {role_label} {role} to {updated_config.project}"
    if pending_account_commands:
        message += (
            f"; its Unix account {launcher.role_run_as_user(updated_config, launcher._role_by_name(updated_config, role))} "
            "does not exist yet, so the role was not started. Run these as an operator, then start it:\n"
            + pending_account_commands
        )
    if not detached:
        visible_count = sum(1 for candidate in updated_config.roles if not candidate.detached)
        if regenerated_layout:
            message += f"; regenerated layout for {visible_count} visible pane(s)"
        if start:
            if recovering_existing_role:
                message += "; started tmux session for the existing role"
            else:
                message += "; started tmux session for the new role"
        message += "; relaunch the project window to display newly added visible slots"
    print_func(message)
    return 0
