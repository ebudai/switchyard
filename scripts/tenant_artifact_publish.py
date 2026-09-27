"""Writing the tenant's copy of a generated file into its own tree, following nothing on the way.

- `publish_tenant_artifact` walks to the tenant's provision directory one
  component at a time with `O_NOFOLLOW`, writes the file beside its name
  relative to that directory's descriptor, gives it its mode -- and, as root,
  to the tenant's account -- and renames it into place, so a symlink left at
  the name is replaced, never followed. It returns whether it did, and why not.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-387). The launcher
imports this module and re-exports the name, so the two modules that read it
through the launcher -- the runtime artifact refresh and the upgrade records --
and every suite that patches it there reach the same object. The no-follow
walk it uses stays in `scripts/no_follow_records.py` and is read from
`team_launcher` when it runs, as it was, so a patch on the launcher still
intercepts. `os`, `pwd` and `Path` are this module's own imports, the same
objects. `ProjectConfig` is imported for annotations only. This module never
imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def publish_tenant_artifact(
    config: ProjectConfig, provision_dir: Path, name: str, body: bytes
) -> tuple[bool, str]:
    """Write the tenant's copy of a generated file without following anything.

    These are untrusted output: root produces them for the tenant's own tooling
    and never reads them back to decide what root installs. The write still runs
    as root, so every path component is opened with O_NOFOLLOW and the file is
    replaced by rename rather than truncated in place -- a symlink left at the
    destination is replaced, never followed to its referent (SYRD-39).
    """
    from scripts import team_launcher as launcher

    destination = provision_dir / name
    relative = Path(str(provision_dir).lstrip("/")) / name
    dir_fd, problem = launcher._walk_no_follow(Path(provision_dir.anchor or "/"), relative)
    if dir_fd < 0:
        return False, f"switchyard: refusing to write {destination}: {problem}"
    mode = 0o755 if name.endswith(".sh") else 0o644
    staged = f".{name}.new"
    try:
        descriptor = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode, dir_fd=dir_fd
        )
        try:
            os.write(descriptor, body)
            os.fchmod(descriptor, mode)
            if os.geteuid() == 0 and config.run_as_user:
                try:
                    owner = pwd.getpwnam(config.run_as_user)
                except KeyError:
                    owner = None
                if owner is not None:
                    os.fchown(descriptor, owner.pw_uid, owner.pw_gid)
        finally:
            os.close(descriptor)
        os.rename(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        return False, f"switchyard: could not write {destination}: {exc}"
    finally:
        os.close(dir_fd)
    return True, ""
