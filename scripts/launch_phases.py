"""`launch_project`'s phases, each a bounded step the launch runs in order.

`launch_project` in `scripts/team_launcher.py` stays the orchestration: it keeps
its name, signature and defaults, and calls each phase here, at the phase's
old position, by the launcher's own name.

- **P3, runners, owner delegation and paths** (`_launch_runners_and_paths`,
  SYRD-339): the runners worktree and role work go through -- the owner's
  when the launch runs as another account -- and the pane-state directory,
  layout output, window title, layout-owner flag and pane script the rest of
  the launch uses, returned as a frozen `LaunchSetup`.
- **P5, pre-launch preparation** (`_prepare_launch`, SYRD-340): the launcher
  checkout, the running roles and their stale runtimes, the runtime user,
  owner state, hooks, board skill and session seeding, the worktrees, the
  reload's sync and the isolation check, returned as a frozen
  `LaunchPreparation` whose `exit_code` is `None` to go on, or the code the
  launch returns at once.
- **P6, layout, plan and dry run** (`_write_layout_and_plan`, SYRD-341): the
  layout written and handed to its owner, the launch plan, and on a dry run
  the plan printed and 0 returned; it hands nothing on, so it answers `None`
  to go on.
- **P7 and P8, worker start and presentation** (`_start_workers_and_present`,
  SYRD-342): the detached workers started, then the layout mode resolved and
  the viewer, runtime presentation, handed-back or Konsole window opened. They
  stay one phase because the failure recorder nested in them keeps the first
  failing exit and records each failure into the caller's own `failed_roles`,
  which the rest of the launch reads. A code the launch stops on is returned
  as it was; going on returns a frozen `WorkerStartup`.
- **P9, the launch's report and records** (`_report_launch`, SYRD-344): the
  unsafe-window report, the announcement of panes a new window attached to, the
  provider-state record of every role that came up, and the session report,
  then the launch's own exit code, which `launch_project` returns.

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


@dataclass(frozen=True)
class LaunchSetup:
    """What P3 hands the rest of `launch_project`, in the order it assigns them."""

    worktree_runner: Callable[..., subprocess.CompletedProcess[Any]]
    role_process_runner: Callable[..., subprocess.CompletedProcess[Any]]
    delegate_role_sessions_to_owner: bool
    effective_pane_state_dir: Path
    output_path: Path
    window_title: str
    should_assign_layout_owner: bool
    pane_script_path: Path


def _launch_runners_and_paths(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    pane_state_dir: Path | None,
    layout_output: Path | None,
    assign_layout_owner: bool | None,
    script_path: Path,
) -> LaunchSetup:
    """P3 of `launch_project`, unchanged: its runners, owner delegation and paths."""
    from scripts import team_launcher as launcher

    worktree_runner = runner
    # Owner-scoped work (worktrees, the viewer session, layout) uses this.
    # Anything that starts, probes or stops a ROLE selects that role's own
    # account instead, because bin_user only sets PATH and the uid the board
    # sees comes from the runner (SYRD-39).
    role_process_runner = runner
    owner_runner_anchor = config.repository or config.pane_launcher
    delegate_role_sessions_to_owner = bool(config.run_as_user and launcher.current_user_name() != config.run_as_user)
    if delegate_role_sessions_to_owner:
        role_process_runner = launcher._owner_process_runner(owner_user=config.run_as_user, runner=runner)
        if owner_runner_anchor is not None:
            worktree_runner = launcher._owner_project_git_runner(
                owner_user=config.run_as_user,
                project_dir=owner_runner_anchor,
                owned_roots=launcher._control_repository_owned_roots(config),
                runner=runner,
            )
    effective_pane_state_dir = pane_state_dir or launcher.default_pane_state_dir_for_user(config.run_as_user, project=config.project)
    output_path = layout_output or launcher.default_layout_output_path(config, config_path=config_path)
    window_title = launcher.project_window_title(config)
    should_assign_layout_owner = layout_output is None if assign_layout_owner is None else assign_layout_owner
    pane_script_path = config.pane_launcher or script_path
    return LaunchSetup(
        worktree_runner=worktree_runner,
        role_process_runner=role_process_runner,
        delegate_role_sessions_to_owner=delegate_role_sessions_to_owner,
        effective_pane_state_dir=effective_pane_state_dir,
        output_path=output_path,
        window_title=window_title,
        should_assign_layout_owner=should_assign_layout_owner,
        pane_script_path=pane_script_path,
    )


@dataclass(frozen=True)
class LaunchPreparation:
    """What P5 hands the rest of `launch_project`: `exit_code` is None to go on,
    else the code the launch returns at once; then the prepared values, in the
    order P5 assigns them."""

    exit_code: int | None
    config: ProjectConfig
    failed_roles: dict[str, str]
    running_roles: list[RoleConfig]
    reconcile_home: Path | None
    unreconciled_roles: set[str]


def _prepare_launch(
    config: ProjectConfig,
    *,
    allow_stale_launcher: bool,
    config_path: Path,
    dry_run: bool,
    effective_pane_state_dir: Path,
    mode: str,
    no_launcher_self_deploy: bool,
    owner_home: Path | None,
    pane_script_path: Path,
    print_func: Callable[[str], None],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    worktree_runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> LaunchPreparation:
    """P5 of `launch_project`, unchanged: the preparation before the layout is written."""
    from scripts import team_launcher as launcher

    failed_roles: dict[str, str] = {}
    running_roles: list[RoleConfig] = []
    reconcile_home: Path | None = None
    #: Roles still carrying an older provider state than the account has,
    #: because their session could not be ended. Their record is deliberately
    #: not updated, so the next ordinary launch tries again (SYRD-191).
    unreconciled_roles: set[str] = set()
    if not dry_run:
        launcher.ensure_launcher_checkout_current(
            config,
            runner=worktree_runner,
            auto_deploy=not (
                no_launcher_self_deploy
                or launcher._env_truthy_any(launcher.NO_LAUNCHER_SELF_DEPLOY_ENV, launcher.LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV)
            ),
            allow_stale=allow_stale_launcher,
        )
        if mode == "attach-or-start":
            running_roles = launcher._running_project_roles(config, runner=runner)
            reconcile_home = owner_home or launcher._owner_home_for_auth(
                config.run_as_user or launcher.current_user_name()
            )
            running_roles, unreconciled_roles = launcher._drop_roles_with_stale_provider_runtime(
                config,
                running_roles,
                owner_home=reconcile_home,
                runner=runner,
                print_func=print_func,
            )
        launcher.ensure_configured_runtime_user(config, runner=runner)
        launcher.ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        launcher.ensure_generated_project_pane_hooks(
            config,
            config_path=config_path,
            script_path=pane_script_path,
            pane_state_dir=effective_pane_state_dir,
            runner=runner,
        )
        launcher.ensure_generated_project_board_skill(
            config,
            config_path=config_path,
            script_path=pane_script_path,
            runner=runner,
            print_func=print_func,
        )
        launcher.seed_default_session_dir_from_legacy_sources(config.session_dir)
        if mode == "attach-or-start":
            worktree_roles = (
                [role for role in config.roles if role.role not in {running.role for running in running_roles}]
                if running_roles and config.control_repository is not None
                else config.roles
            )
            failed_roles = launcher._prepare_project_worktrees_for_launch(
                config,
                running_roles=running_roles,
                runner=worktree_runner,
            ).failed_roles
            if worktree_roles and config.control_repository is not None and set(failed_roles) == {role.role for role in worktree_roles}:
                reason = next(iter(failed_roles.values()), "unknown error")
                print(f"team-launcher: failed to prepare control repository for {config.project}: {reason}", file=sys.stderr)
                return LaunchPreparation(
                    exit_code=1,
                    config=config,
                    failed_roles=failed_roles,
                    running_roles=running_roles,
                    reconcile_home=reconcile_home,
                    unreconciled_roles=unreconciled_roles,
                )
        elif mode == "reload":
            launcher.fetch_project_worktree_ref(config, runner=worktree_runner)
            config = launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
            config = launcher.prepare_project_desktop(config, runner=runner)
        # Worktrees are created here, after the operator artifact has already
        # run, so a fresh project reaches this point with trees still owned by
        # the project owner. Say so every launch until the handoff is done,
        # rather than starting roles that cannot write their own trees.
        isolation_gaps = launcher.role_isolation_gaps(config)
        if isolation_gaps:
            print_func(
                "team-launcher: refusing to launch " + config.project
                + "; its resumable state is not ready for project-account runtime:\n  "
                + "\n  ".join(isolation_gaps)
                + f"\nRun `sudo switchyard upgrade {config.project}` after every live role is at a "
                "resumable checkpoint. The migration leaves dedicated accounts intact."
            )
            return LaunchPreparation(
                exit_code=1,
                config=config,
                failed_roles=failed_roles,
                running_roles=running_roles,
                reconcile_home=reconcile_home,
                unreconciled_roles=unreconciled_roles,
            )
    return LaunchPreparation(
        exit_code=None,
        config=config,
        failed_roles=failed_roles,
        running_roles=running_roles,
        reconcile_home=reconcile_home,
        unreconciled_roles=unreconciled_roles,
    )


def _write_layout_and_plan(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool,
    failed_roles: dict[str, str],
    force_reload: bool,
    layout_environ: dict[str, str] | None,
    layout_mode: str,
    mode: str,
    output_path: Path,
    pane_script_path: Path,
    pane_state_dir: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    should_assign_layout_owner: bool,
    window_title: str,
) -> int | None:
    """P6 of `launch_project`, unchanged: the layout, the plan and the dry run's exit."""
    from scripts import team_launcher as launcher

    launcher.materialize_layout(
        config,
        config_path=config_path,
        mode=mode,
        script_path=pane_script_path,
        output_path=output_path,
        pane_state_dir=pane_state_dir,
        force_reload=force_reload,
        failed_roles=failed_roles,
    )
    if should_assign_layout_owner:
        launcher.ensure_layout_output_owner(config, output_path, runner=runner)
    plan = {
        "project": config.project,
        "window_title": window_title,
        "mode": mode,
        "layout": str(output_path),
        "run_as_user": config.run_as_user or launcher.current_user_name(),
        "worktree_ref": launcher.worktree_ref(config) if config.repository is not None else None,
        "roles": [
            {
                "role": role.role,
                "slot": role.slot,
                "tmux_session": role.tmux_session,
                "target": role.target,
                "workdir": str(Path.home()) if role.role in failed_roles else role.workdir,
                "command": (
                    launcher.failed_role_command(role, failed_roles[role.role])
                    if role.role in failed_roles
                    else launcher.pane_command(
                        config.project,
                        role,
                        config_path=config_path,
                        mode=mode,
                        script_path=pane_script_path,
                        pane_state_dir=pane_state_dir,
                        force_reload=force_reload,
                        skip_launcher_check=True,
                        run_as_user=launcher.role_run_as_user(config, role),
                    )
                ),
                "worktree_error": failed_roles.get(role.role, ""),
            }
            for role in config.roles
            if not role.detached
        ],
        "detached_roles": [
            {
                "role": role.role,
                "tmux_session": role.tmux_session,
                "target": role.target,
                "workdir": role.workdir,
            }
            for role in config.roles
            if role.detached
        ],
    }
    if dry_run:
        resolved_layout_mode = (
            launcher.resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
            if layout_mode != launcher.LAYOUT_MODE_AUTO or layout_environ is not None
            else launcher.LAYOUT_MODE_SEPARATE
        )
        if resolved_layout_mode == launcher.LAYOUT_MODE_VIEWER:
            plan["layout_mode"] = launcher.LAYOUT_MODE_VIEWER
            plan["viewer_session"] = launcher.viewer_session_for_project(config.project)
            plan["viewer_roles"] = [role.role for role in launcher.visible_roles_for_viewer(config)]
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    return None


@dataclass(frozen=True)
class WorkerStartup:
    """What P7 and P8 hand the rest of `launch_project` when the launch goes on,
    in the order they assign them. A launch that stops gets its code instead."""

    worker_start_exit_code: int
    launch_started_at: float
    launch_started_ns: int
    resolved_layout_mode: str


def _start_workers_and_present(
    config: ProjectConfig,
    *,
    allow_stale_launcher: bool,
    config_path: Path,
    delegate_role_sessions_to_owner: bool,
    effective_pane_state_dir: Path,
    failed_roles: dict[str, str],
    force_reload: bool,
    konsole_process_launcher: Callable[..., Any] | None,
    layout_environ: dict[str, str] | None,
    layout_mode: str,
    layout_output: Path | None,
    mode: str,
    output_path: Path,
    pane_script_path: Path,
    print_func: Callable[[str], None],
    role_process_runner: Callable[..., subprocess.CompletedProcess[Any]],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    window_title: str,
) -> WorkerStartup | int:
    """P7 and P8 of `launch_project`, unchanged: the workers started, then the
    presentation. A code the launch stops on is returned as it was; going on
    returns a `WorkerStartup`. Failures are recorded into the caller's own
    `failed_roles`."""
    from scripts import team_launcher as launcher

    from scripts import presentation_controller

    use_runtime_presentation = presentation_controller.presentation_enabled(config, config_path=config_path)
    worker_start_exit_code = 0

    def record_worker_start_failure(role: RoleConfig, result: int) -> None:
        nonlocal worker_start_exit_code
        worker_start_exit_code = worker_start_exit_code or int(result)
        reason = f"pane start failed with exit {result}"
        failed_roles[role.role] = reason
        print(f"team-launcher: {reason} for {role.role}; leaving presentation recovery status", file=sys.stderr)

    launch_started_at = time.time()
    launch_started_ns = time.time_ns()
    for role in config.roles:
        if not role.detached:
            continue
        if role.role in failed_roles:
            print(f"skipping detached role {role.role}: {failed_roles[role.role]}", file=sys.stderr)
            continue
        if delegate_role_sessions_to_owner:
            result = runner(
                launcher.pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=mode,
                    script_path=pane_script_path,
                    pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                    force_reload=force_reload,
                    skip_launcher_check=True,
                    allow_stale_launcher=allow_stale_launcher,
                    run_as_user=launcher.role_run_as_user(config, role),
                )
            ).returncode
        else:
            result = launcher.run_detached_role(
                role,
                mode=mode,
                session_dir=launcher.role_session_dir(config, role),
                pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                force_reload=force_reload,
                bin_user=launcher.role_run_as_user(config, role),
                runner=launcher.role_process_runner_for(config, role, runner=runner),
            )
        if result != 0:
            if not use_runtime_presentation:
                return result
            record_worker_start_failure(role, result)
    resolved_layout_mode = launcher.resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
    if resolved_layout_mode == launcher.LAYOUT_MODE_VIEWER:
        viewer_roles = launcher.visible_roles_for_viewer(config)
        for role in viewer_roles:
            if role.role in failed_roles:
                print(f"skipping visible role {role.role}: {failed_roles[role.role]}", file=sys.stderr)
                continue
            if delegate_role_sessions_to_owner:
                result = runner(
                    launcher.pane_command_args(
                        config.project,
                        role,
                        config_path=config_path,
                        mode=mode,
                        script_path=pane_script_path,
                        pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                        force_reload=force_reload,
                        skip_launcher_check=True,
                        allow_stale_launcher=allow_stale_launcher,
                        no_attach=True,
                        run_as_user=launcher.role_run_as_user(config, role),
                    )
                ).returncode
            else:
                result = launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode=mode,
                    session_dir=launcher.role_session_dir(config, role),
                    pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                    force_reload=force_reload,
                    bin_user=launcher.role_run_as_user(config, role),
                    runner=launcher.role_process_runner_for(config, role, runner=runner),
                )
            if result != 0:
                if not use_runtime_presentation:
                    return result
                record_worker_start_failure(role, result)
        launcher.ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        launchable_viewer_roles = [role for role in viewer_roles if role.role not in failed_roles]
        if use_runtime_presentation:
            launch_result = presentation_controller.launch_presentation(
                config,
                config_path=config_path,
                layout=launcher.LAYOUT_MODE_VIEWER,
                state_path=output_path.with_name("presentation.json") if layout_output is not None else None,
                runner=runner,
                # The panes this is about were started a moment ago, and their
                # runtime registration is their own asynchronous work. Sampling
                # once refused a launch that had in fact succeeded (SYRD-162).
                assignment_wait_seconds=launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
                print_func=print_func,
                unstarted=tuple(failed_roles),
            )
        elif launchable_viewer_roles:
            launch_result = launcher.launch_tmux_viewer_session(
                launchable_viewer_roles,
                viewer_session=launcher.viewer_session_for_project(config.project),
                window_title=window_title,
                runner=role_process_runner,
            )
        elif viewer_roles:
            launch_result = 1
        else:
            launch_result = 0
        if launch_result == 0 and launcher.running_through_tenant_control():
            # After the whole chain, not inside one arm of it. The viewer is
            # reached two ways -- through the presentation controller when this
            # tenant has presentation state, and directly when it does not --
            # and the first of those never handed anything back. Live Zorin took
            # exactly that one: every pane attached, the command returned, and
            # no window and no complaint (SYRD-211 live UAT).
            #
            # The viewer is one tiled session holding every role, and it is
            # detached: this account cannot display it and there are no per-slot
            # display sessions to open tabs on. So the caller is told it is a
            # viewer and shown one tab.
            if not launcher.hand_presentation_back_to_the_caller(
                config,
                slot_count=1,
                window_title=window_title,
                layout=launcher.LAYOUT_MODE_VIEWER,
                slot_titles=[window_title or launcher.project_window_title(config)],
                pane_program=Path(launcher.display_attach_helper_path(config.project)),
                print_func=print_func,
            ):
                # Said rather than swallowed. This account has no screen, so a
                # handoff that cannot be made means nobody is going to open a
                # window and nothing downstream will notice.
                print_func(
                    f"warning: switchyard: {config.project}'s panes are up, but this "
                    "invocation was given no way to hand its window back, so none will "
                    "open. Run it again from the session that owns the screen"
                )
    else:
        launcher.ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        if use_runtime_presentation:
            for role in launcher.visible_roles_for_viewer(config):
                if role.role in failed_roles:
                    continue
                if delegate_role_sessions_to_owner:
                    result = runner(
                        launcher.pane_command_args(
                            config.project,
                            role,
                            config_path=config_path,
                            mode=mode,
                            script_path=pane_script_path,
                            pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                            force_reload=force_reload,
                            skip_launcher_check=True,
                            allow_stale_launcher=allow_stale_launcher,
                            no_attach=True,
                            run_as_user=launcher.role_run_as_user(config, role),
                        )
                    ).returncode
                else:
                    result = launcher.ensure_visible_role_session_for_viewer(
                        role,
                        mode=mode,
                        session_dir=launcher.role_session_dir(config, role),
                        pane_state_dir=launcher.role_pane_state_dir(config, role, effective_pane_state_dir),
                        force_reload=force_reload,
                        bin_user=launcher.role_run_as_user(config, role),
                        runner=launcher.role_process_runner_for(config, role, runner=runner),
                    )
                if result != 0:
                    record_worker_start_failure(role, result)
            launch_result = presentation_controller.launch_presentation(
                config,
                config_path=config_path,
                layout=launcher.LAYOUT_MODE_SEPARATE,
                state_path=output_path.with_name("presentation.json") if layout_output is not None else None,
                runner=runner,
                process_launcher=konsole_process_launcher,
                # Same race, same bound: these workers were started above.
                assignment_wait_seconds=launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
                print_func=print_func,
                unstarted=tuple(failed_roles),
            )
        elif launcher.running_through_tenant_control() and launcher.hand_presentation_back_to_the_caller(
            config,
            slot_count=len(launcher.visible_roles_for_viewer(config)),
            window_title=window_title,
            print_func=print_func,
        ):
            # Opened by the caller, which owns the screen. This account does
            # not, and Konsole started from here goes nowhere (SYRD-211).
            launch_result = 0
        else:
            refusal = launcher.legacy_presentation_refusal(config, output_path=output_path)
            if refusal:
                # The workers are up and stay up; only the window is refused.
                # Handing the terminal this path is the defect itself: it is in
                # the owner's 0700 state directory and the terminal runs as the
                # desktop account, so it aborts with "A problem occurred when
                # loading the Layout" and nothing says why (SYRD-233).
                print_func(refusal)
                launch_result = 1
            else:
                launch_result = launcher.launch_konsole_window(
                    output_path,
                    project=config.project,
                    window_title=window_title,
                    runner=runner,
                    process_launcher=konsole_process_launcher,
                )
    if launch_result != 0:
        return launch_result
    return WorkerStartup(
        worker_start_exit_code=worker_start_exit_code,
        launch_started_at=launch_started_at,
        launch_started_ns=launch_started_ns,
        resolved_layout_mode=resolved_layout_mode,
    )


def _report_launch(
    config: ProjectConfig,
    *,
    config_path: Path,
    effective_pane_state_dir: Path,
    failed_roles: dict[str, str],
    launch_started_at: float,
    launch_started_ns: int,
    mode: str,
    print_func: Callable[[str], None],
    reconcile_home: Path | None,
    report_session_records: bool,
    resolved_layout_mode: str,
    running_roles: list[RoleConfig],
    session_record_poll: float,
    session_record_timeout: float,
    unreconciled_roles: set[str],
    worker_start_exit_code: int,
) -> int:
    """P9 of `launch_project`, unchanged: the unsafe-window report, the attach
    announcement, the provider-state records and the session report; it answers
    the launch's own exit code."""
    from scripts import team_launcher as launcher

    unsafe_windows = launcher.unsafe_root_presentation_windows(config, config_path=config_path)
    if unsafe_windows:
        print_func(launcher.unsafe_presentation_report(config, unsafe_windows))
    if mode == "attach-or-start" and resolved_layout_mode != launcher.LAYOUT_MODE_VIEWER and not unsafe_windows:
        attached_visible_roles = [role for role in running_roles if not role.detached and role.role not in failed_roles]
        if attached_visible_roles:
            attached_names = ", ".join(role.role for role in attached_visible_roles)
            plural = "pane" if len(attached_visible_roles) == 1 else "panes"
            print_func(
                f"switchyard: opened a new window attached to running {plural}: {attached_names}; "
                "the previous window may be closed if no longer needed"
            )
    # What each role's runtime has now been started against. Written after the
    # launch, for every role that actually came up: a role whose start failed,
    # or whose stale session could not be ended, keeps its old record so the
    # next ordinary launch reconciles it instead of forgetting (SYRD-191).
    if mode == "attach-or-start" and reconcile_home is not None:
        for role in config.roles:
            if role.role in failed_roles or role.role in unreconciled_roles:
                continue
            cli = launcher._role_cli_name(role)
            if not cli:
                continue
            launcher.record_provider_state_generation(
                config, role, launcher.provider_state_generation(cli, owner_home=reconcile_home)
            )
    if report_session_records:
        launcher.report_launch_session_records(
            config,
            timeout_seconds=session_record_timeout,
            poll_seconds=session_record_poll,
            fallback_changed_since_ns=launch_started_ns,
            pane_state_dir=effective_pane_state_dir,
            pane_state_updated_since=launch_started_at,
            attached_roles=running_roles if mode == "attach-or-start" else (),
            print_func=print_func,
        )
    return worker_start_exit_code
