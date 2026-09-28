"""Repairing a project's managed Git policy hooks.

`repair_repository_policy_hooks` reinstalls the managed policy hooks of the
repositories a project's config names -- missing or stale ones -- through
`scripts.repository_hooks`. It is idempotent, keeps the existing hooks
directory's owner, touches nothing the config does not name, and on failure
only warns: a hook repair must not fail an otherwise good upgrade. A dry run
says what it would do and installs nothing.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-449). The launcher
imports this module and re-exports the name; `upgrade_phases.py` still reads it
there. It reads nothing from the launcher; the hook installer is imported from
`scripts.repository_hooks` when it runs, exactly as before, so a suite that
rebinds it there still intercepts it. Its default printer is the builtin
`print`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable


def repair_repository_policy_hooks(
    config_path: Path,
    *,
    source_repo: Path,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> tuple[Path, ...]:
    """Reinstall this project's managed Git policy hooks, missing or stale.

    Idempotent: the installer rewrites its own managed hook and preserves any
    pre-existing one, so repeated upgrades converge rather than accumulate. Ownership is
    taken from the existing hooks directory, so a root-run upgrade leaves the tenant's
    hooks owned by the tenant. Only the repositories named in this project's config are
    touched -- no global hooksPath, no scanning of arbitrary homes.
    """
    if dry_run:
        print_func(f"switchyard: would reinstall managed Git policy hooks for {config_path}")
        return ()
    from scripts import repository_hooks

    try:
        installed = repository_hooks.install_project_config(config_path, source_root=source_repo)
    except Exception as exc:
        # Warning-only policy: a repair failure must not fail an otherwise good upgrade.
        print_func(f"warning: switchyard: could not repair Git policy hooks: {exc}")
        return ()
    for path in installed:
        print_func(f"switchyard: reinstalled managed Git policy hook {path}")
    return installed
