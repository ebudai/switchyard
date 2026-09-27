"""The pane-launcher preflight: refusing to open panes a launch cannot keep inert.

When a project configures its own pane launcher, `_verify_pane_launcher_path`
checks, through the caller's runner, that the launcher is executable and that
the inert pane window shipped beside it is too -- a release without that window
would hand a detached pane back to a shell (SYRD-43). Without a configured
launcher it answers the caller's own script path unchanged.

The inert window's path comes from the launcher's `pane_window_program` when
the check runs; that helper stays in `scripts/team_launcher.py`, where other
modules read it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-337). `team_launcher`
imports this module at its top, still exports the function, and calls it by
that name, so a suite's patch of it still reaches the launch. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

def _verify_pane_launcher_path(
    config: ProjectConfig,
    *,
    script_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Path:
    from scripts import team_launcher as launcher

    pane_script_path = config.pane_launcher or script_path
    if config.pane_launcher is None:
        return pane_script_path
    result = runner(["test", "-x", str(pane_script_path)])
    if result.returncode != 0:
        owner = f" by {config.run_as_user}" if config.run_as_user else ""
        raise SystemExit(f"team-launcher: configured pane_launcher {pane_script_path} is not readable/executable{owner}")
    # Every pane's terminal program is the inert window beside that launcher. A
    # release without it would fall back to a shell on detach, which is the
    # whole defect, so refuse rather than open panes that do (SYRD-43).
    pane_window = launcher.pane_window_program(pane_script_path)
    if runner(["test", "-x", str(pane_window)]).returncode != 0:
        raise SystemExit(
            f"team-launcher: {pane_window} is missing or not executable; this release cannot open "
            "panes that stay inert when they detach. Upgrade the shared release before starting."
        )
    return pane_script_path
