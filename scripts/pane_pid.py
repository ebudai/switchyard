"""A role pane's process id, as its tmux server reports it.

- `tmux_pane_pid_args` is the argv that asks tmux for the pid of the process
  running in a role's pane.
- `pane_pid_for_role` runs it and answers that pid, or 0 when tmux fails or
  answers something that is not a number.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-409), in their original
order. The launcher imports this module and re-exports both names; the live
runtime, the identity cutover and the tmux live-command match still read
`pane_pid_for_role` through the launcher when they run. `pane_pid_for_role`
reads the argv builder through the launcher at call time, so a suite that
rebinds it there still intercepts it. The `subprocess.run` default is bound at
definition, as it was, from this module's own import: the same object.
`RoleConfig` is an annotation only. This module imports `team_launcher` only
inside the function, when it runs.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


def tmux_pane_pid_args(role: RoleConfig) -> list[str]:
    return ["tmux", "display-message", "-p", "-t", role.target, "#{pane_pid}"]


def pane_pid_for_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    proc = runner(launcher.tmux_pane_pid_args(role), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return 0
    try:
        return int(str(proc.stdout).strip())
    except ValueError:
        return 0
