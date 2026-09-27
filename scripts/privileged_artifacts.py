"""What root publishes: its privileged artifacts, rendered where only root can reach and installed without following a link.

- `render_privileged_artifacts` renders a tenant's plan into root's own
  temporary directory, closed to root, and returns the bytes of the privileged
  artifacts and the plan -- the bytes root publishes are the bytes root
  generated.
- `install_privileged_artifacts` writes those bytes into the tenant's
  root-only provisioning directory: each beside its target with `O_NOFOLLOW`,
  given to root at the mode its consumer needs, and renamed into place.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-384), in their original
order. The launcher imports this module and re-exports both names, so its own
caller, the two privileged modules that read them through it, and every suite
that patches them there reach the same objects. Every launcher facility these
use -- the artifact writer and names, the provision root and directory, the
directory repair and the mode selector, which stay elsewhere -- is read from
`team_launcher` when it runs, as it was, so a patch on the launcher still
intercepts. The standard-library names are this module's own imports, the same
objects. `ProjectBoardProvision` is imported for annotations only. This module
never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectBoardProvision


def render_privileged_artifacts(
    plan: ProjectBoardProvision, *, enable_owner_linger: bool = False
) -> dict[str, bytes]:
    """Render everything root installs, in a directory only root can reach.

    Rendering into root's own temporary directory rather than reading the
    tenant's copies back means the bytes root publishes are the bytes root
    generated, with no window in which they could be replaced (SYRD-39).
    """
    from scripts import team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix=f"switchyard-{plan.project}-privileged.") as tmp:
        staged = Path(tmp)
        os.chmod(staged, 0o700)
        launcher.write_artifacts(plan, staged, enable_owner_linger=enable_owner_linger)
        return {
            name: (staged / name).read_bytes()
            for name in (*launcher.privileged_artifact_names(plan), "plan.json")
            if (staged / name).is_file()
        }


def install_privileged_artifacts(
    plan: ProjectBoardProvision,
    rendered: dict[str, bytes],
    *,
    privileged_root: Path | None = None,
) -> Path:
    """Publish rendered bytes into a directory only root can reach.

    The provision directory belongs to the tenant, and a directory's owner can
    replace what is inside it, so root neither writes there nor reads back from
    there: these bytes come straight from the render above. Each file is written
    beside its target and renamed, so an operator never reads a half-written
    unit (SYRD-39).

    "Only root can reach" is enforced here rather than described: the directory
    is closed to root alone and each artifact is written at the least its
    consumer needs, which for all of them is root and nobody else (SYRD-176).
    """
    from scripts import team_launcher as launcher

    base = privileged_root or launcher.switchyard_privileged_provision_root()
    target = launcher.privileged_provision_dir(plan.project, root=base)
    launcher.ensure_privileged_provision_dir(target, root=base)
    for name, body in rendered.items():
        staged = target / f".{name}.new"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, body)
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, launcher.privileged_artifact_mode(name))
        finally:
            os.close(descriptor)
        staged.replace(target / name)
    return target
