#!/usr/bin/env python3
"""Reading a document root trusts, without following anything on the way.

The bounded privileged helper re-derives a project's board URL from root-owned
registration state rather than from argv, because a caller that could name the
board could name one that would agree with it. That reasoning holds only if the
READ is as narrow as the rule: `Path.read_text` follows every symlink, so a
tenant-controlled link at its own registered configuration pointed the helper at
a file of the tenant's choosing, and the `board_url` taken from it is what the
helper then authorized against (SYRD-242).

Deliberately dependency-free -- json, os, stat, pathlib and nothing else. This
module is imported by the helper that polkit runs as root from its own staged
copy, so anything it imports must be there too. It must never import
`team_launcher`.

Every read here answers with (document, problem). A problem names the path and
says what was wrong with the FILE; it never quotes the file's bytes, its text,
or a parser's view of them, because the file may be one somebody else chose.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import Any

#: Bits that let somebody other than the owner rewrite a thing.
SHARED_WRITE = stat.S_IWGRP | stat.S_IWOTH


def _walk_no_follow(path: Path) -> tuple[int, str]:
    """Open the directory holding `path`, refusing a link at any component.

    Checking the final name is not enough: an ancestor several levels up can be
    a symlink, and then the whole subtree below it belongs to wherever it
    points. Returns (dir_fd, problem); the caller closes the descriptor.
    """
    absolute = Path(os.path.abspath(str(path)))
    try:
        fd = os.open(absolute.anchor or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return -1, f"{absolute.anchor or '/'} cannot be opened ({exc.strerror})"
    for component in absolute.parent.parts[1:]:
        try:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
        except OSError as exc:
            try:
                kind = os.lstat(component, dir_fd=fd)
                linked = stat.S_ISLNK(kind.st_mode)
            except OSError:
                linked = False
            os.close(fd)
            if linked:
                return -1, f"{absolute}: its ancestor {component} is a symlink"
            if exc.errno == errno.ENOENT:
                return -1, f"{absolute} does not exist"
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                return -1, f"{absolute}: its ancestor {component} is not a directory"
            return -1, f"{absolute}: its ancestor {component} is unusable ({exc.strerror})"
        os.close(fd)
        fd = child
    return fd, ""


def _read_document(
    path: Path,
    *,
    permitted_uids: frozenset[int] | None,
    what: str,
    require_root_directory: bool = False,
    permitted_from_directory: bool = False,
) -> tuple[Any, str]:
    """One document, opened by descriptor and judged before it is parsed.

    The directory it lives in is judged from the same descriptor the walk
    ended on, never from a second lookup by path: a `stat()` on the parent
    would follow the very link this exists to refuse.
    """
    absolute = Path(os.path.abspath(str(path)))
    dir_fd, problem = _walk_no_follow(absolute)
    if dir_fd < 0:
        return None, f"refusing to read {what}: {problem}"
    try:
        directory = os.fstat(dir_fd)
        if not stat.S_ISDIR(directory.st_mode):
            return None, f"refusing to read {what}: {absolute.parent} is not a directory"
        if directory.st_mode & SHARED_WRITE:
            return None, (
                f"refusing to read {what}: {absolute.parent} is mode "
                f"{stat.S_IMODE(directory.st_mode):04o}, so anybody in its group or beyond can "
                "replace what is in it"
            )
        if require_root_directory and directory.st_uid not in _trusted_uids():
            return None, (
                f"refusing to read {what}: {absolute.parent} is owned by uid "
                f"{directory.st_uid} rather than by root"
            )
        if permitted_from_directory:
            permitted_uids = None if directory.st_uid == 0 else frozenset({0, directory.st_uid})
        try:
            fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None, f"refusing to read {what}: {absolute} is a symlink"
            if exc.errno == errno.ENOENT:
                return None, f"refusing to read {what}: {absolute} does not exist"
            return None, f"refusing to read {what}: {absolute} cannot be opened ({exc.strerror})"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, f"refusing to read {what}: {absolute} is not a regular file"
            if info.st_nlink != 1:
                return None, (
                    f"refusing to read {what}: {absolute} has {info.st_nlink} links, so it is "
                    "another file under a second name"
                )
            if info.st_mode & SHARED_WRITE:
                return None, (
                    f"refusing to read {what}: {absolute} is mode "
                    f"{stat.S_IMODE(info.st_mode):04o}, which anybody in its group or beyond "
                    "can write"
                )
            # Who may own it is the caller's rule; None means the directory
            # already settled it.
            if permitted_uids is not None and info.st_uid not in permitted_uids:
                expected = ", ".join(str(uid) for uid in sorted(permitted_uids))
                return None, (
                    f"refusing to read {what}: {absolute} is owned by uid {info.st_uid} rather "
                    f"than by uid {expected}"
                )
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Why, never what: this file may be one somebody else pointed us at,
        # and a decoder's message quotes the bytes it choked on.
        return None, f"refusing to read {what}: {absolute} is not UTF-8 text"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, (
            f"refusing to read {what}: {absolute} is not JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        )


def _trusted_uids() -> frozenset[int]:
    """Root, and whoever this process already is.

    Under the polkit boundary this process IS root, so this is root and the
    check is the one that matters. Run unprivileged -- a test, an operator
    inspecting their own state -- an account's own files are no less trustworthy
    to it than root's, and demanding root there would only mean the safe read
    could not be exercised at all.
    """
    return frozenset({0, os.geteuid()})


def read_root_document(path: Path, *, what: str) -> tuple[Any, str]:
    """A document only root may have written, in a directory only root may write."""
    return _read_document(
        path, permitted_uids=_trusted_uids(), what=what, require_root_directory=True
    )


def read_tenant_document(path: Path, *, what: str) -> tuple[Any, str]:
    """A tenant's own generated document, judged by who could have written it.

    Only root can write into a root-owned directory, so a file in one was put
    there by root -- provisioning writes a tenant's documents and hands them to
    its owner, which is exactly that shape. A directory the tenant owns can be
    filled with anything, so there the document must belong to that owner or to
    root (the SYRD-228 rule, in the module the helper may import).
    """
    return _read_document(path, permitted_uids=None, what=what, permitted_from_directory=True)
