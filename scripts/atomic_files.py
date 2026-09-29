"""Launcher-owned atomic JSON writers: a file published whole, and a private one in a private directory.

`_write_json_atomic` writes JSON to a temporary file beside the target and
replaces the target with it; for a tenant's control file (`owner_user`), the
owner (when root) and the file's existing mode (else 0600) are set on the open
temporary file before it is published, so no root-owned replacement is ever
left for a later repair. `_write_private_json_atomic` writes a 0600 file in a
private (0700) directory (`_ensure_private_dir`), best effort where the mode
cannot be set. If anything fails before the replace, `_write_json_atomic` removes
its temporary file; the private writer leaves it.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-457). The launcher
imports this module and re-exports all three, so every module that reads them
through the launcher still reaches the launcher's names; the private writer
reads its directory helper through the launcher too. They use only the
standard library otherwise. This module imports `team_launcher` only inside
the function that needs it, when it runs.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import tempfile
from pathlib import Path
from typing import Any


def _write_json_atomic(
    path: Path, payload: dict[str, Any], *, owner_user: str | None = None,
) -> None:
    owner = pwd.getpwnam(owner_user) if owner_user and os.geteuid() == 0 else None
    mode = 0o600
    if owner_user and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode) & 0o777
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # Publish tenant controls with the correct owner already attached;
            # never leave a root-owned replacement for a later repair step.
            if owner is not None:
                os.fchown(handle.fileno(), owner.pw_uid, owner.pw_gid)
            if owner_user:
                os.fchmod(handle.fileno(), mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    from scripts import team_launcher as launcher

    launcher._ensure_private_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
