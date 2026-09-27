"""`launch_project`'s phases, each a bounded step the launch runs in order.

`launch_project` in `scripts/team_launcher.py` stays the orchestration: it keeps
its name, signature and defaults, and calls each phase here, at the phase's
old position, by the launcher's own name.

- **P3, runners, owner delegation and paths** (`_launch_runners_and_paths`,
  SYRD-339): the runners worktree and role work go through -- the owner's
  when the launch runs as another account -- and the pane-state directory,
  layout output, window title, layout-owner flag and pane script the rest of
  the launch uses, returned as a frozen `LaunchSetup`.

Every launcher facility a phase uses is read from `scripts/team_launcher.py`
when the phase runs, so a patch there still reaches it. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


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
