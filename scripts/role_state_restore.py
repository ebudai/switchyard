"""Giving a role its state back after an interrupted repatriation.

`restore_interrupted_role_state` finishes what a broken provider-state store
interrupted: it finds the roles whose provider state could not be written,
says what it would do on a dry run, repairs the role-state ownership, and then
finishes those records, so the ordinary launch that follows an upgrade does
not restart panes it was meant to protect (SYRD-233).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-448). The launcher
imports this module and re-exports the name; `upgrade_phases.py` still reads it
there. Everything it reads when it runs -- the owner's home, the current user,
the interrupted roles and how to finish them, the ownership repair and a
role's CLI name -- is read through the launcher, so a suite that rebinds one
there still intercepts it. Its definition-time defaults, `subprocess.run` and
the builtin `print`, are the same objects. This module imports `team_launcher`
only inside the function, when it runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def restore_interrupted_role_state(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    owner_home: Path | None = None,
) -> bool:
    """Give the role state back AND finish what the broken store interrupted.

    Ownership alone is half a repair. A role left running because its store was
    unusable still has no record, `roles_with_stale_provider_runtime` calls a
    missing record stale, and the ordinary launch that follows the upgrade ends
    the very panes this was protecting -- the restart postponed by one command
    rather than avoided (SYRD-233 post-DAT).
    """
    from scripts import team_launcher as launcher

    if owner_home is None:
        owner_home = launcher.home_dir_for_user(config.run_as_user or launcher.current_user_name()) or Path.home()
    captured = launcher._interrupted_provider_state_roles(config, runner=runner, owner_home=owner_home)
    if dry_run and captured:
        for role, pane_pid, _generation in captured:
            print_func(
                f"switchyard: would finish the provider-state record {role.role} could not write, "
                f"so its live {launcher._role_cli_name(role)} (pid {pane_pid}) would not be restarted; "
                "nothing written"
            )
    if not launcher.repair_role_state_ownership(config, dry_run=dry_run, print_func=print_func):
        return False
    if dry_run:
        return True
    return launcher._finish_interrupted_provider_state(
        config, captured, runner=runner, owner_home=owner_home, print_func=print_func
    )
