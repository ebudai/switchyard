"""The root-owned tooling each role runs: staged from the selected release, and repaired only by root.

- `refresh_staged_role_tooling` restages a tenant's per-role tooling from the
  exact selected release, with the same renderer the operator script uses,
  and verifies what landed before it says so.
- `ensure_staged_role_tooling` is the gate a launch passes before any window
  opens: a bundle that is present and root-owned is healthy; one somebody else
  could have written is refused, never replaced; an incomplete one is
  restaged by root from the release it was staged from, and anyone else is
  told what is wrong and runs nothing.
- `STAGED_TOOLING_OWNER_UID` is who owns a staged bundle on a host: root.
- `_staged_tooling_dir` names where a tenant's bundle is staged.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-399), in their original
order. The launcher imports this module and re-exports every name, so its own
resume path and launch-problem checks, the phases and pane hooks that read
these through it, and every suite that calls or patches them there reach the
same objects. The launcher facilities -- the launch-problem check and the
provisioning renderer, verifier and staging path it imports -- and every name
defined here that another definition here reads when it runs are read from
`team_launcher` when it runs, as they were, so a patch on the launcher still
intercepts. The `expect_uid`, `euid_getter`, `runner` and `print_func`
defaults are bound when each function is defined, as they were. The
standard-library names are this module's own imports, the same objects.
`ProjectConfig` is imported for annotations only. This module never imports
`team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def refresh_staged_role_tooling(
    config: ProjectConfig,
    *,
    release_root: Path,
    staging_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Restage the root-owned per-role tooling from the selected release.

    Staging used to happen only inside the account-creation script, so a tenant
    whose accounts already existed skipped it: the roles kept the previous
    release's hooks and board clients while the board moved on under them, and
    the provenance marker named a release the staged files did not come from.
    It is part of the artifacts phase now, which runs on every upgrade including
    a resumed one, and it stages from the exact selected release rather than
    from the `current` symlink (SYRD-62).

    The same renderer the operator script uses, so what an upgrade installs and
    what that script installs cannot drift apart.
    """
    from scripts import team_launcher as launcher

    script = "set -eu\n" + "\n".join(
        launcher.role_tooling_staging_commands(config.project, str(release_root), staging_root=staging_root)
    )
    result = runner(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(result, "returncode", 1) != 0:
        detail = (str(getattr(result, "stderr", "") or "").strip() or "no output")[:400]
        return [
            f"could not stage {config.project}'s role tooling from {release_root} "
            f"(exit {result.returncode}): {detail}"
        ]
    problems = launcher.staged_role_tooling_problems(
        config.project, str(release_root), staging_root=launcher._staged_tooling_dir(config, staging_root)
    )
    if problems:
        return problems
    print_func(
        f"switchyard: staged {config.project} role tooling in "
        f"{launcher._staged_tooling_dir(config, staging_root)} from {release_root}"
    )
    return []


#: Who owns a staged bundle on a host: root, always. `install -o root -g root`
#: is what puts it there, and every account that later READS it -- the desktop
#: operator running `switchyard new`, the project owner the control bridge
#: crosses to -- is somebody else. The verifier defaults to the reader's own
#: uid, which is the right default for the sandbox fixtures that stage as
#: themselves and the wrong one for every production path, so this says root
#: explicitly and the override exists only for those fixtures (SYRD-249 review).
STAGED_TOOLING_OWNER_UID = 0


def ensure_staged_role_tooling(
    config: ProjectConfig,
    *,
    release_root: Path | None = None,
    staging_root: Path | None = None,
    install_root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
    euid_getter: Callable[[], int] = os.geteuid,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """The bundle a pane runs, present and root-owned, or why it is not.

    Checked before any window opens. A modern single-owner tenant declares no
    per-role accounts, and the whole staging step used to sit behind a test for
    those, so provisioning reported success and then opened six tabs onto
    `sudo: /usr/local/lib/switchyard/<project>/switchyard-display-attach:
    command not found` (SYRD-249).

    Asked through `staged_bundle_launch_problems`, which is the same question
    the operator's half asks before it crosses: a launch repairs absence, never
    drift, and judges a tenant against the release its own bundle was staged
    from. An older tenant whose bundle is complete is healthy, and stays on the
    release its board is pinned to.

    Repaired here only by root, and root is the only account that could: the
    staging commands install as `root:root` at a path no tenant may write. An
    unprivileged caller -- the owner the bridge crossed to -- is told what is
    wrong and runs nothing.
    """
    from scripts import team_launcher as launcher

    absent, hostile, release = launcher.staged_bundle_launch_problems(
        config.project,
        release_root=str(release_root) if release_root is not None else "",
        root=staging_root,
        install_root=install_root,
        expect_uid=expect_uid,
    )
    if hostile:
        return hostile + [
            f"{config.project}'s staged tooling is not something a launch may replace"
        ]
    if not absent:
        return []
    repair = (
        f"`switchyard start {config.project}` from the operator's own account repairs this "
        f"before it crosses, and `sudo switchyard upgrade {config.project}` restages it"
    )
    if euid_getter() != 0:
        return absent + [
            f"{config.project}'s panes would open on tooling that is not staged, and repairing "
            f"it is root's: {repair}"
        ]
    staged = launcher._staged_tooling_dir(config, staging_root)
    print_func(
        f"switchyard: {config.project}'s staged role tooling in {staged} is incomplete; "
        f"restaging it from {release}"
    )
    repaired = launcher.refresh_staged_role_tooling(
        config, release_root=release, staging_root=staging_root,
        runner=runner, print_func=print_func,
    )
    if not repaired:
        return []
    return repaired + [
        f"{config.project}'s panes would open on tooling that is not there: {repair}"
    ]


def _staged_tooling_dir(config: ProjectConfig, staging_root: Path | None) -> Path:
    from scripts import team_launcher as launcher

    return Path(launcher.role_tooling_staging_dir(config.project, root=staging_root))
