"""Writing a presentation layout into a desktop account's home, including root's write across accounts.

`write_desktop_layout` writes the layout a terminal will open and hands it to
the desktop account, or says why it cannot; a refusal is returned, never
raised. Writing into another account's home is root's, so it goes through
`_write_crossing_desktop_layout`, which opens every directory from that home
down with `_open_owned_directory_chain`: no symlink is followed, every
component must be owned by that account, and what is missing is created 0700
and given to it from an open descriptor.

The account lookups, the destination check, the home and the private JSON
writer stay in `scripts/team_launcher.py` and are read there when a function
runs. The suites patch `write_desktop_layout` on the launcher, and its callers
reach it there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-317). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence



def _open_owned_directory_chain(
    home: Path, relative: Sequence[str], *, uid: int, gid: int
) -> tuple[int, str, list[str]]:
    """Open `home/relative` without following anything, creating what is missing.

    Every component from the home down has to be a real directory owned by
    `uid`: a symlink anywhere in the chain would move root's write to wherever
    it points, and a component owned by somebody else is one that account did
    not create and cannot be assumed to control. Missing components are created
    0700 and given to `uid` as they are made, from an open descriptor, so there
    is no path lookup between making one and owning it.
    """
    created: list[str] = []
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            return -1, f"{home} is a symlink or not a directory", created
        return -1, f"{home} cannot be opened ({exc.strerror})", created
    walked = home
    try:
        if os.fstat(fd).st_uid != uid:
            owner = os.fstat(fd).st_uid
            os.close(fd)
            return -1, f"{home} is owned by uid {owner}, not by uid {uid}", created
        for component in relative:
            walked = walked / component
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=fd)
                created.append(str(walked))
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.fchown(child, uid, gid)
                os.fchmod(child, 0o700)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    os.close(fd)
                    return -1, f"{walked} is a symlink or not a directory", created
                raise
            owner = os.fstat(child).st_uid
            if owner != uid:
                os.close(child)
                os.close(fd)
                return -1, f"{walked} is owned by uid {owner}, not by uid {uid}", created
            os.close(fd)
            fd = child
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        return -1, f"{walked} cannot be prepared ({exc.strerror})", created
    return fd, "", created


def _write_crossing_desktop_layout(
    path: Path, payload: dict[str, Any], *, gui_user: str, uid: int, project: str
) -> str:
    """Root's write of one layout into another account's own state directory."""
    from scripts import team_launcher as launcher

    problem = launcher.desktop_layout_destination_problem(path, gui_user=gui_user, project=project)
    if problem:
        return f"refusing to write the presentation layout: {problem}"
    try:
        gid = pwd.getpwnam(gui_user).pw_gid
    except KeyError:
        return f"{gui_user} is not a local account"
    home = Path(launcher._gui_home(gui_user))
    relative = Path(os.path.normpath(path)).parent.relative_to(home).parts
    fd, problem, _created = _open_owned_directory_chain(home, relative, uid=uid, gid=gid)
    if fd < 0:
        return f"refusing to write the presentation layout: {problem}"
    staged = f".{path.name}.{os.getpid()}.tmp"
    try:
        try:
            existing = os.lstat(path.name, dir_fd=fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                return f"refusing to write the presentation layout: {path} is not a regular file"
            if existing.st_uid != uid:
                return (
                    f"refusing to write the presentation layout: {path} is owned by uid "
                    f"{existing.st_uid}, not by uid {uid}"
                )
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            os.write(descriptor, body)
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        os.replace(staged, path.name, src_dir_fd=fd, dst_dir_fd=fd)
    except OSError as exc:
        try:
            os.unlink(staged, dir_fd=fd)
        except OSError:
            pass
        return f"cannot give {gui_user} the presentation layout {path}: {exc.strerror or exc}"
    finally:
        os.close(fd)
    return ""


def write_desktop_layout(
    path: Path,
    payload: dict[str, Any],
    *,
    gui_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    project: str = "",
) -> str:
    """Write the layout and hand it to the desktop account; say why if it cannot.

    A refusal is returned rather than raised so the caller can say what a human
    should do instead. Launching a terminal at a file it cannot read is the
    failure this exists to prevent, and it fails as an abort with an empty log.

    Crossing into another account is root writing inside somebody else's home,
    so it goes through `_write_crossing_desktop_layout`, which walks that home
    without following anything and refuses what it does not expect (SYRD-233).
    """
    from scripts import team_launcher as launcher

    uid = launcher.uid_for_user(gui_user) if gui_user else None
    if uid is None:
        return f"{gui_user or 'the desktop account'} is not a local account"
    crossing = uid != os.getuid()
    if crossing and os.geteuid() != 0:
        return (
            f"this invocation cannot give {gui_user} a readable layout: it is running as "
            f"{launcher.current_user_name()}, and only root can write into another account's state directory"
        )
    if crossing:
        return _write_crossing_desktop_layout(
            path, payload, gui_user=gui_user, uid=uid, project=project
        )
    created = [parent for parent in reversed(path.parents) if not parent.exists()]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        launcher._write_private_json_atomic(path, payload)
    except OSError as exc:
        return f"cannot write the presentation layout {path}: {exc}"
    if not crossing:
        return ""
    try:
        gid = pwd.getpwnam(gui_user).pw_gid
    except KeyError:
        gid = -1
    try:
        # Every directory this call had to create, not just the last one: a
        # root-owned directory inside somebody's home is one they cannot
        # remove and did not ask for.
        for target in (*created, path):
            os.chown(target, uid, gid)
        path.parent.chmod(0o700)
        path.chmod(0o600)
    except OSError as exc:
        return f"cannot give {gui_user} the presentation layout {path}: {exc}"
    return ""
