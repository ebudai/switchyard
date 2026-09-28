"""A tenant board's authority units: installed from root's staged copies, and only then activated.

- `install_board_authority_files` installs each generated unit a tenant's
  board authority needs -- the system unit, the listener and the canary --
  from root's own staged directory, `0644` with the ownership each is given,
  and reloads systemd only once every one of them installed. Nothing is
  restarted: the listener stays stopped until the roles have verified.
- `install_board_authority` is the whole operation for callers with no
  release deploy in between: the units, then the board activated and
  restarted -- and nothing activated when a unit did not install.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-401), in their original
order. The launcher imports this module and re-exports both names, so the
identity cutover that installs the units through it, and every suite that
calls or patches these there, reach the same objects. The launcher facilities
-- the staged provisioning directory and its root, the current user, the
units a tenant's authority installs and the board's activation -- and the
installer the whole operation calls are read from `team_launcher` when they
run, as they were, so a patch on the launcher still intercepts. The
`print_func` defaults are bound when each function is defined, as they were.
The standard-library names are this module's own imports, the same objects.
`ProjectConfig` is imported for annotations only. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def install_board_authority_files(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install the staged units and reload systemd. Nothing is restarted here.

    This has to happen before the release deploy, not after it. `deploy-restart`
    compares the release's own production unit with the one installed and
    refuses when they differ, because a daemon-reload is deliberately outside
    the board's deploy grant -- and the whole point of this transaction is that
    the new unit differs: it carries `SupplementaryGroups`, the strict runtime
    directory mode and the per-role identity table. With the old unit still
    installed, the migration this transaction *is* is indistinguishable from
    operator drift, and the deploy correctly refuses (SYRD-63).

    Installing the file and reloading changes nothing about what is running, so
    the invariant behind the old ordering still holds: the old board is never
    restarted under a unit its release cannot serve. The restart happens with
    the new binary, inside the deploy or in `activate_board_authority` after it.

    The same three units the printed operator sequence installs, including the
    canary -- the deploy starts that one through systemd, so a transaction that
    left it uninstalled or stale was relying on provisioning having got there
    first (SYRD-63).
    """
    from scripts import team_launcher as launcher

    problems: list[str] = []
    staged_dir = launcher.privileged_provision_dir(config.project, root=launcher.switchyard_privileged_provision_root())
    owner = config.run_as_user or launcher.current_user_name()
    for unit, destination, ownership in launcher.authority_unit_installs(
        config, config_path=config_path
    ):
        # The staged source is the generated unit; a destination that is a copy
        # of it says so in its name rather than being a second, different file.
        staged = staged_dir / unit.split(" (", 1)[0]
        if not staged.is_file():
            problems.append(f"{unit} has not been generated under {staged_dir}")
            continue
        result = runner(
            ["install", "-D", "-m", "0644", *ownership, str(staged), str(destination)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            problems.append(f"could not install {unit} (exit {result.returncode})")
    if problems:
        return problems
    if runner(["systemctl", "daemon-reload"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode != 0:
        return ["systemctl daemon-reload failed"]
    print_func(
        f"switchyard: installed {config.project}'s generated units and reloaded systemd; "
        f"{owner}'s listener stays stopped until the roles have verified"
    )
    return []


def install_board_authority(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install the staged units, reload, restart and health-check the board.

    The whole operation, for callers with no release deploy between the two
    halves. The identity transaction has one, and uses the halves (SYRD-63).
    """
    from scripts import team_launcher as launcher

    problems = launcher.install_board_authority_files(
        config, runner=runner, config_path=config_path, print_func=print_func
    )
    if problems:
        return problems
    # The listener stays stopped through the release and schema migration; it is
    # started again only once the workers, their writes and the presentation
    # have all verified (SYRD-45).
    return launcher.activate_board_authority(config, runner=runner, restart=True, print_func=print_func)
