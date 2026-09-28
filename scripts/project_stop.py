"""Stopping a project's sessions: `switchyard stop`, and the sessions step of a suspension.

`stop_project` stops the project owner's viewer session (probed and killed as
the owner, from the caller's account if they differ), then the project's
presentation, then every role session in its own tmux server. Every step is
attempted even after an earlier one failed, and the first nonzero exit is the
answer.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-407). The launcher
imports this module and re-exports the name; its `main` and
`scripts/tenant_suspension.py` still call it through the launcher. Everything
it calls -- the viewer session name, the current user, the owner runner, the
failure reason, the tmux argv and the role-session stop -- is read through the
launcher at call time, so a suite that rebinds one there still intercepts it.
The presentation controller is imported inside the function, when it runs, as
it was. The `subprocess.run` and `print` defaults are bound at definition, as
they were, from this module's own imports: the same objects. `ProjectConfig`
is an annotation only. This module imports `team_launcher` only inside the
function, when it runs.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def stop_project(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    # The viewer and display sessions belong to the project owner; each role's
    # session lives in that role's own tmux server, so it has to be probed and
    # killed there or stop reports success while the session is still alive
    # (SYRD-39).
    from scripts import team_launcher as launcher

    owner_runner = runner
    if config.run_as_user and launcher.current_user_name() != config.run_as_user:
        owner_runner = launcher._owner_process_runner(owner_user=config.run_as_user, runner=runner)
    exit_code = 0
    viewer_session = launcher.viewer_session_for_project(config.project)
    viewer_exists = owner_runner(
        launcher.tmux_has_session_by_name_args(viewer_session),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if viewer_exists:
        result = owner_runner(
            launcher.tmux_kill_session_by_name_args(viewer_session),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            reason = launcher._proc_failure_reason(result, f"tmux kill-session failed with exit {result.returncode}")
            print_func(f"failed to stop viewer: {viewer_session}: {reason}")
            exit_code = exit_code or int(result.returncode)
        else:
            print_func(f"stopped viewer: {viewer_session}")
    else:
        print_func(f"already stopped viewer: {viewer_session}")
    from scripts import presentation_controller

    presentation_stop = presentation_controller.stop_presentation(
        config,
        runner=runner,
        print_func=print_func,
    )
    exit_code = exit_code or presentation_stop
    # Always, not short-circuited on an earlier failure: a viewer that would not
    # close is no reason to leave every worker running.
    workers_stopped = launcher.stop_role_sessions(config, runner=runner, print_func=print_func)
    return exit_code or workers_stopped
