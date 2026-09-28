"""This host's shared release, activated by root, and the privileged boundary it carries.

- `install_host_privileged_boundary` installs the host-wide privileged
  boundary -- the root-owned helper, its package and the polkit catalogue --
  from one installed release, the shared release the host runs, never a
  tenant's pin, and verifies what it installed.
- `switchyard_install_shared_release_command` moves `/opt/switchyard/current`
  to an exact commit or back to the recorded previous one, root only, with a
  dry run, and installs the boundary from the release it just activated.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-400), in their original
order. The launcher imports this module and re-exports both names, so its
command dispatch, the recovered-pin check that installs the boundary through
it, and every suite that calls or patches these there reach the same
objects. The command reads the boundary installer from `team_launcher` when it
runs, as it did, so a patch on the launcher still intercepts. The privileged
action catalogue, the activation and the install commands are still imported
inside each function when it runs. The `runner` and `print_func` defaults are
bound when each function is defined, as they were. The standard-library names
are this module's own imports, the same objects. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable


def install_host_privileged_boundary(
    release_root: Path,
    *,
    dry_run: bool = False,
    staging_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install this host's privileged boundary from one installed release.

    The boundary -- the root-owned helper, its package and the polkit catalogue
    -- is host-wide, and its helper always runs the SHARED launcher. So it is
    installed from the shared release the host runs, never from a tenant's pin.
    Staging it only inside a tenant upgrade, from that tenant's pinned release,
    is what left MEFP unable to receive `preview-upgrade`: its pin was older
    than the host (SYRD-284). The same `install_commands` provisioning and
    staging use, run directly as root; re-runnable, and it removes the boundary
    when the release carries none (a rollback to a release older than it).
    """
    from scripts.ticket_board import privileged_install

    root, policy_dir = privileged_install.roots_for(staging_root)
    if dry_run:
        print_func(f"switchyard: would install this host's privileged boundary from {release_root}")
        return []
    commands = privileged_install.install_commands(
        str(release_root), root=root, policy_dir=policy_dir, sudo=""
    )
    script = "\n".join(line for line in commands if line and not line.lstrip().startswith("#"))
    result = runner(["sh", "-euc", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(result, "returncode", 1) != 0:
        return [
            f"the privileged boundary could not be installed from {release_root} "
            f"(exit {result.returncode}): {(str(result.stderr or '').strip() or 'no output')[:400]}"
        ]
    problems = privileged_install.verify_installation(root, policy_dir)
    if not problems:
        print_func(f"switchyard: installed this host's privileged boundary from {release_root}")
    return problems


def switchyard_install_shared_release_command(
    commit: str,
    *,
    rollback: bool = False,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
    boundary_root: Path | None = None,
) -> int:
    """Activate a shared release, as root, with a way back recorded first.

    `boundary_root` redirects where the privileged boundary is installed; only
    a sandbox passes it.
    """
    from scripts import team_launcher as launcher

    from scripts.ticket_board import privileged_actions, shared_release_activation

    if rollback and commit:
        print_func("switchyard: give either --commit or --rollback, not both")
        return 2
    if not rollback:
        try:
            commit = privileged_actions.action_for("install-shared-release").validate(
                {"commit": commit}
            )["commit"]
        except privileged_actions.ArgumentError as exc:
            print_func(f"switchyard: {exc}")
            return 2
    if os.geteuid() != 0:
        # Not a refusal on principle: without root the pointer cannot move, and
        # a message saying so beats a permission error from three frames down.
        print_func(
            "switchyard: install-shared-release changes /opt/switchyard/current and must "
            "run as root. It is reached through `switchyard privileged-action <project> "
            "install-shared-release commit=<sha>`, which asks polkit for it"
        )
        return 1
    if dry_run:
        record = shared_release_activation.recorded_rollback()
        current = shared_release_activation.read_pointer()
        print_func(f"switchyard: current points at {current or 'nothing'}")
        print_func(
            "switchyard: would activate "
            + (f"the recorded previous target {record.get('previous_target') or 'nothing'}"
               if rollback else commit)
        )
        return 0
    try:
        if rollback:
            result = shared_release_activation.rollback(print_func=print_func)
        else:
            result = shared_release_activation.activate(commit, print_func=print_func)
    except shared_release_activation.ActivationFailed as exc:
        print_func(f"switchyard: {exc}")
        return 1
    print_func(f"switchyard: {result.describe()}")
    if getattr(result, "rolled_back", False) or not getattr(result, "release_root", ""):
        return 0
    # The host's boundary follows the host's shared release, so installing one
    # installs the other: the actions a release catalogues are usable as soon
    # as it is current, with no tenant upgrade in between (SYRD-284).
    problems = launcher.install_host_privileged_boundary(
        Path(result.release_root), staging_root=boundary_root, print_func=print_func
    )
    if problems:
        for problem in problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: {result.commit} is current, but this host's privileged boundary was "
            "not installed from it; run this again to repair it"
        )
        return 1
    return 0
