"""Whether root may execute a file: the whole path to it, its owners, modes and access control entries.

`untrusted_root_executable_reasons` says why root must not execute a file -- a
symlink on the way, a step that is not a directory or not a regular file, an
owner other than the entitled one, a group- or world-writable mode, or an
access control entry that grants somebody else write -- and `acl_write_grants`
reads those entries through `getfacl` (SYRD-62).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-473).
`project_provision` imports this module and re-exports both names, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What the trust check reads of `project_provision` --
`acl_write_grants` -- is read through it when the check runs, so a patch there
still reaches it. This module imports `project_provision` only inside the
function that needs it, when it runs, with the same fallback for direct script
execution.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Callable


def acl_write_grants(
    path: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> tuple[list[str], str]:
    """Access control entries that let somebody other than root write this.

    Mode bits are not the whole story and `ls -l` shows only a trailing `+`:
    the grant behind this was `user:<control role>:rwx` on a file whose mode
    said `root:root`. Returns the offending entries and, separately, why the
    list could not be read -- which is itself a refusal, because a permission
    that cannot be checked has not been checked (SYRD-62).
    """
    try:
        proc = runner(
            ["getfacl", "-p", "--omit-header", "--absolute-names", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return [], str(exc)
    if getattr(proc, "returncode", 1) != 0:
        return [], (str(getattr(proc, "stderr", "") or "").strip() or "getfacl failed")
    grants: list[str] = []
    for line in str(getattr(proc, "stdout", "") or "").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        fields = entry.split(":")
        default = fields[0] == "default"
        if default:
            fields = fields[1:]
        if len(fields) < 3:
            continue
        kind, qualifier, perms = fields[0], fields[1], fields[2]
        if kind == "mask" or "w" not in perms:
            continue
        # The base entries are the mode bits, and those are checked directly.
        # A default entry is different: it decides what a file created here
        # later will carry, so `default:other::rw-` is a standing grant even
        # though nothing writable exists yet.
        named = bool(qualifier) and kind in {"user", "group"}
        inherited_world = default and kind == "other"
        if not (named or inherited_world):
            continue
        grants.append(f"{'default:' if default else ''}{kind}:{qualifier}:{perms}")
    return grants, ""


def untrusted_root_executable_reasons(
    path: Path,
    *,
    boundary: Path | None = None,
    owner_uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    """Why root must not execute this file. Empty means every step of it is root's.

    A root-run script is only as trustworthy as the whole path to it. The file's
    own mode says nothing while the directory holding it belongs to somebody
    else, who can unlink it and put their own script at the same name -- which
    is exactly the shape this found: a `root:root` script in a tenant-owned
    directory, both carrying a named `rwx` entry for the control role.

    So the file, every directory above it up to `boundary` (the filesystem root
    unless a caller narrows it), the absence of any symlink along the way, and
    the access control entries of each are all checked (SYRD-62).

    `owner_uid` is the identity entitled to have written all of it. It defaults
    to this process's own real uid, which is root wherever this decides whether
    root may execute something -- naming it rather than a literal zero is what
    lets the security cases be exercised as a namespace root instead of only on
    a host.
    """
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return [f"{path} is not an absolute path in normal form"]
    expected_uid = os.getuid() if owner_uid is None else owner_uid
    reasons: list[str] = []
    real = Path(os.path.realpath(candidate))
    if real != candidate:
        reasons.append(f"{candidate} is reached through a symlink and resolves to {real}")
    stop = Path(boundary) if boundary is not None else Path(candidate.anchor or "/")
    chain: list[Path] = []
    for entry in (candidate, *candidate.parents):
        chain.append(entry)
        if entry == stop:
            break
    else:
        reasons.append(f"{candidate} is not under {stop}")
    for entry in chain:
        try:
            info = entry.lstat()
        except OSError as exc:
            reasons.append(f"{entry} cannot be inspected: {exc}")
            continue
        if stat.S_ISLNK(info.st_mode):
            reasons.append(f"{entry} is a symlink")
            continue
        if entry == candidate and not stat.S_ISREG(info.st_mode):
            reasons.append(f"{entry} is not a regular file")
        if entry != candidate and not stat.S_ISDIR(info.st_mode):
            reasons.append(f"{entry} is not a directory")
        if info.st_uid != expected_uid:
            reasons.append(
                f"{entry} is owned by uid {info.st_uid} rather than by uid {expected_uid}"
            )
        if info.st_mode & 0o022:
            reasons.append(
                f"{entry} is group- or world-writable (mode {stat.S_IMODE(info.st_mode):04o})"
            )
        grants, unreadable = provision.acl_write_grants(entry, runner=runner)
        if unreadable:
            reasons.append(f"{entry} access control list could not be read: {unreadable}")
        reasons.extend(f"{entry} grants write through {grant}" for grant in grants)
    return reasons
