"""Reversible tenant suspend and resume: what `switchyard suspend` and
`switchyard resume` do to one tenant.

- `suspend_tenant` closes the tenant's window, stops its sessions, contains any
  process that escaped its panes, then stops its listener and its board. It
  attempts every step and returns every problem; nothing persistent is removed.
- `resume_tenant` repairs the staged role tooling first, then starts the board
  (or leaves a running one alone) and, only once the board is up, the
  listener. It stops at the first boundary that fails and says what was not
  started after it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-405), in their original
order. The launcher imports this module and re-exports both names, and
`switchyard_main` still calls them through its own globals. Everything they call
-- the board and listener units, the window, the sessions, residual
containment, the staged tooling -- is read through the launcher at call time,
so a suite that rebinds one there still intercepts it. The `subprocess.run`,
`os.kill` and `print` defaults are bound at definition, as they were, from this
module's own imports: the same objects. `ProjectConfig` is an annotation only.
This module imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def suspend_tenant(
    config: ProjectConfig,
    *,
    config_path: Path,
    gui_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Suspend one tenant: reversible, project-scoped, and honest about failure.

    The order is the dependency order read backwards, because each step's
    consumer has to go first: the window before the sessions it frames, the
    sessions before the listener that wakes them, the listener before the board
    it reads. Nothing here removes state -- no database, no worktree, no
    registration, no journal -- which is the whole difference between this and
    `teardown`.

    Every step runs even if an earlier one failed. A window that would not close
    is no reason to leave a board serving, and reporting the first failure while
    silently skipping the rest is how a tenant ends up half suspended with one
    line of output about it.
    """
    from scripts import team_launcher as launcher

    problems: list[str] = []
    problems.extend(
        launcher.close_presentation_window(
            config, config_path=config_path, gui_user=gui_user,
            proc_root=proc_root, signaller=signaller, print_func=print_func,
        )
    )
    if launcher.stop_project(config, runner=runner, print_func=print_func) != 0:
        problems.append(f"not every {config.project} session could be stopped")
    problems.extend(
        launcher.contain_residual_project_processes(
            config, proc_root=proc_root, signaller=signaller, print_func=print_func
        )
    )
    listener_problems = launcher.stop_owner_listener(config, runner=runner, config_path=config_path)
    problems.extend(listener_problems)
    if not listener_problems:
        print_func(f"stopped listener: {launcher._listener_user_unit(config)}")
    board_problems = launcher._board_system_unit_action(config, "stop", runner=runner)
    problems.extend(board_problems)
    if not board_problems:
        print_func(f"stopped board: {launcher._board_system_unit(config)}")
    return problems


def resume_tenant(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Bring a suspended tenant back, in dependency order, stopping at the boundary.

    Forwards this time: the board before the listener that reads it, and both
    before the sessions whose panes talk to them. Unlike the suspension, this
    STOPS at the first failed boundary and says which one -- starting sessions
    against a board that did not come up produces panes that cannot register,
    and a report of success over them is worse than the failure.
    """
    from scripts import team_launcher as launcher

    # The staged bundle first, because it is what every pane this resume leads
    # to will run. This can be reached as root (`sudo switchyard start`) or as
    # the project owner (the control bridge crossed to it), so it repairs only
    # in the first case and reports in the second: the owner may not stage
    # root's files, and the operator's own path repairs these two programs
    # before it crosses, through the recorded privileged command
    # `ensure_tenant_control_helper` already uses (SYRD-211, SYRD-249 review).
    staging_problems = launcher.ensure_staged_role_tooling(config, runner=runner, print_func=print_func)
    if staging_problems:
        return staging_problems + [
            f"{config.project} was not resumed: its panes would start against tooling that is "
            "not staged. Nothing was stopped or removed"
        ]
    # Idempotent by asking first. `systemctl start` on a live unit is a no-op,
    # but the listener is restored with `restart`, which would bounce a healthy
    # one -- and resuming an already-running tenant must not interrupt it.
    if launcher.board_system_unit_is_active(config, runner=runner):
        print_func(f"already running board: {launcher._board_system_unit(config)}")
        board_problems: list[str] = []
    else:
        board_problems = launcher._board_system_unit_action(config, "start", runner=runner)
    if board_problems:
        return board_problems + [
            f"{config.project} was not resumed past its board; nothing after it was started, and "
            "the tenant is still suspended rather than half up"
        ]
    if not board_problems and not launcher.board_system_unit_is_active(config, runner=runner):
        return [f"{launcher._board_system_unit(config)} did not come up; nothing after it was started"]
    print_func(f"started board: {launcher._board_system_unit(config)}")
    if launcher.capture_listener_state(config, runner=runner, config_path=config_path) == "active":
        print_func(f"already running listener: {launcher._listener_user_unit(config)}")
        return []
    listener_problems = launcher.start_owner_listener(config, runner=runner, config_path=config_path)
    if listener_problems:
        return listener_problems + [
            f"{config.project}'s board is up but its listener is not; its roles would run without "
            "notifications, so no session was started"
        ]
    print_func(f"started listener: {launcher._listener_user_unit(config)}")
    return []
