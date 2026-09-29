"""How the launcher runs a command as a project owner, and drives the owner's user manager.

`_owner_command_args` runs a command directly when the caller already is the
owner, else through `sudo -u <owner>`. `_owner_command_env_args` does the same
through `env`, with the owner's HOME, a PATH of the owner's own bin directories
ahead of the default pane PATH, and the terminal presentation variables.
`_tenant_owner_home` is the owner home the tenant records in its plan, else the
account's home, else `/home/<owner>`. `_owner_user_systemctl` builds the
`sh -c` script that exports the owner's XDG_RUNTIME_DIR and session bus address
and runs `systemctl --user` (after a `daemon-reload` for start and restart).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-459). The launcher
imports this module and re-exports all four, so every module that reads them
through the launcher still reaches the launcher's names. Everything they read
of the launcher -- each other included, and the current user, the PATH
helpers, the plan record and the account home lookup -- is read through it
when they run, so a patch there still reaches them. This module imports
`team_launcher` only inside the functions that need it, when they run.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from scripts.project_identity import ProjectConfig


def _owner_command_args(owner_user: str, command: Sequence[str]) -> list[str]:
    from scripts import team_launcher as launcher

    if owner_user == launcher.current_user_name():
        return list(command)
    return ["sudo", "-u", owner_user, *command]


def _owner_command_env_args(owner_user: str, owner_home: Path, command: Sequence[str]) -> list[str]:
    from scripts import team_launcher as launcher

    path = launcher._prepend_paths(launcher.DEFAULT_PANE_BASE_PATH, launcher._owner_home_bin_dirs(owner_home))
    return launcher._owner_command_args(
        owner_user,
        [
            "env",
            f"HOME={owner_home}",
            f"PATH={path}",
            *launcher._terminal_presentation_env(),
            *command,
        ],
    )


def _tenant_owner_home(config: ProjectConfig, config_path: Path | None) -> Path:
    """The owner home this tenant actually records, not merely the passwd one."""
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    if config_path is not None:
        recorded = str(launcher._plan_data_from_config(config, config_path).get("owner_home") or "").strip()
        if recorded:
            return Path(recorded)
    return launcher.home_dir_for_user(owner) or Path("/home") / owner


def _owner_user_systemctl(
    config: ProjectConfig, action: str, unit: str, *, config_path: Path | None = None
) -> list[str]:
    """Drive the owner's user manager the way the rest of the launcher does."""
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    home = launcher._tenant_owner_home(config, config_path)
    # Some questions are asked of the manager itself rather than of a unit.
    operation = f"systemctl --user {action}"
    if unit:
        operation = f"{operation} {shlex.quote(unit)}"
    if action in {"start", "restart"}:
        operation = f"systemctl --user daemon-reload && {operation}"
    # Exported rather than prefixed. A prefix binds to one command, and every
    # action that needs a reload is two: `... systemctl --user daemon-reload &&
    # systemctl --user restart <unit>` ran the restart with no XDG_RUNTIME_DIR
    # and no bus address at all, so it could not reach the manager it had just
    # reloaded and exited 1. That is the "could not start the notify listener"
    # a rollback reported while the same unit started immediately by hand with
    # the owner's runtime directory set (SYRD-61).
    script = (
        'runtime="/run/user/$(id -u)"; '
        'export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
        + operation
    )
    return launcher._owner_command_env_args(owner, home, ["sh", "-c", script])
