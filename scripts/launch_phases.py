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

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
import sys
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
