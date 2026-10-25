"""Where an upgrade proves an installed onboarding doc is the one a release shipped (SYRD-683).

An installed onboarding doc carries the Switchyard commit it was copied from.
An upgrade replaces it only when it is still exactly what that commit shipped,
so it needs the doc's body at that commit. From a source checkout that is the
checkout's own history. From an immutable installed release -- no `.git` --
it was looked for only in the tenant's `--commit-git-dir`, which for most
tenants is the project's own repository: it never holds Switchyard commits, so
every doc was skipped on every upgrade (pie, d24a15cf -> fd06cac8).

`shipped_body` reads the body from trusted sources only:

1. The installed release of exactly that commit,
   `<install root>/releases/<commit>`. Its marker must record that commit, and
   the release, its docs and the doc are opened one component at a time by
   descriptor, never through a link, each owned by root and writable by
   nobody else, from `/` down. No Git is needed: the copy is compared with the
   shipped body byte for byte.
2. Otherwise, a source cache named in `--commit-git-dir`, but only one that is
   root's in the same way: a tenant's `control.git` is the tenant's to
   rewrite, so it is never read as Switchyard history. It is read with replace
   refs off, and both commits must be in it; the ancestry check runs there.

Each source looked at, and why it could not be used, is returned for the
message. `TRUST` is where every proof starts and who must own each component.
Only a suite moves it, to a directory and account of its own; nothing in the
environment does.
"""

from __future__ import annotations

import io
import json
import os
import pwd
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Where a proof starts, and the uid every component from there down must belong to.
TRUST: tuple[str, int] = ("/", 0)

FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
#: An onboarding doc is a few hundred lines; anything near this is not one.
MAX_DOC_BYTES = 4 * 1024 * 1024


class Untrusted(Exception):
    """Why a path cannot be read as a release's or a source cache's."""


@dataclass(frozen=True)
class ShippedBody:
    """A doc's body as one release shipped it, where it was read, and the history for its ancestry check."""

    body: str | None
    source: str = ""
    #: Trusted Git history to check ancestry in; None when the body came from the release itself.
    git_args: tuple[str, ...] | None = None
    #: Each source inspected, and why it could not be used; set when `body` is None.
    problems: tuple[str, ...] = field(default_factory=tuple)


def _owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _check_owned(fd: int, where: str) -> os.stat_result:
    info = os.fstat(fd)
    uid = TRUST[1]
    if info.st_uid != uid:
        raise Untrusted(f"{where} belongs to {_owner_name(info.st_uid)}, not {_owner_name(uid)}")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Untrusted(f"{where} is writable by its group or by others")
    return info


def _open_at(fd: int | None, name: str, where: str, *, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
    try:
        return os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        raise Untrusted(f"{where} does not exist") from None
    except OSError as exc:
        # O_NOFOLLOW refuses a link at the last component with ELOOP; O_DIRECTORY a link or file with ENOTDIR.
        try:
            linked = stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode)
        except OSError:
            linked = False
        raise Untrusted(f"{where} is a symbolic link" if linked else f"{where} cannot be opened ({exc.strerror})") from None


def open_trusted_dir(path: Path) -> int:
    """`path`, an absolute directory, opened from `TRUST`'s base one component at a time; each must be TRUST's."""
    base, _uid = TRUST
    text = str(path)
    segments = text.split("/")[1:]
    if not text.startswith("/") or any(segment in ("", ".", "..") for segment in segments):
        raise Untrusted(f"{text} is not a plain absolute path")
    prefix = base.rstrip("/")
    if text != prefix and not text.startswith(prefix + "/"):
        raise Untrusted(f"{text} is not under {base}")
    fd = _open_at(None, base, base, directory=True)
    try:
        _check_owned(fd, base)
        where = prefix
        for segment in text[len(prefix):].split("/")[1:]:
            where = f"{where}/{segment}"
            child = _open_at(fd, segment, where, directory=True)
            os.close(fd)
            fd = child
            _check_owned(fd, where)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_trusted_file(dir_fd: int, name: str, where: str) -> bytes:
    """A regular file in a proven directory, opened without following a link; it must be TRUST's too."""
    fd = _open_at(dir_fd, name, where, directory=False)
    try:
        info = _check_owned(fd, where)
        if not stat.S_ISREG(info.st_mode):
            raise Untrusted(f"{where} is not a regular file")
        if info.st_size > MAX_DOC_BYTES:
            raise Untrusted(f"{where} is {info.st_size} bytes, too large for an onboarding doc")
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read(MAX_DOC_BYTES + 1)
    finally:
        os.close(fd)


def as_installed(raw: bytes) -> str:
    """The text as installation reads a shipped doc (`read_text`: UTF-8, universal newlines), so the comparison is
    with exactly what was stamped -- and a copy whose line endings were changed afterwards no longer matches."""
    return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8").read()


def release_doc(commit: str, name: str) -> tuple[str | None, str, str]:
    """`name` as the installed release of exactly `commit` shipped it: (body, where it was read, why not)."""
    from scripts import team_launcher as launcher

    release = launcher.switchyard_shared_install_root() / "releases" / commit
    where = f"{release}/docs/onboarding/{name}"
    try:
        fd = open_trusted_dir(release)
        try:
            marker = json.loads(read_trusted_file(fd, launcher.SWITCHYARD_RELEASE_MARKER_NAME,
                                                  f"{release}/{launcher.SWITCHYARD_RELEASE_MARKER_NAME}"))
            recorded = str(marker.get("commit") or "") if isinstance(marker, dict) else ""
            if recorded != commit:
                raise Untrusted(f"{release} records {recorded or 'no release'}, not {commit}")
            for segment, at in (("docs", f"{release}/docs"), ("onboarding", f"{release}/docs/onboarding")):
                child = _open_at(fd, segment, at, directory=True)
                os.close(fd)
                fd = child
                _check_owned(fd, at)
            return as_installed(read_trusted_file(fd, name, where)), where, ""
        finally:
            os.close(fd)
    except Untrusted as refusal:
        return None, where, str(refusal)
    except (OSError, ValueError) as exc:  # unreadable, not JSON, not UTF-8
        return None, where, f"{where} cannot be read ({exc})"


def cache_problem(cache: Path) -> str:
    """Why `cache` is not a source cache root keeps, or "": proven like a release, its objects and refs too."""
    try:
        fd = open_trusted_dir(cache)
    except Untrusted as refusal:
        return str(refusal)
    try:
        for name in ("objects", "refs"):
            child = _open_at(fd, name, f"{cache}/{name}", directory=True)
            try:
                _check_owned(child, f"{cache}/{name}")
            finally:
                os.close(child)
        read_trusted_file(fd, "config", f"{cache}/config")
        return ""
    except Untrusted as refusal:
        return str(refusal)
    finally:
        os.close(fd)


def shipped_body(
    name: str,
    commit: str,
    *,
    current_commit: str,
    commit_git_dir: str | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> ShippedBody:
    """`name`'s body as the release of exactly `commit` shipped it, from the installed release or a root-kept cache."""
    from scripts import team_launcher as launcher

    if not FULL_COMMIT.fullmatch(commit):
        return ShippedBody(None, problems=(f"{commit!r} is not an exact release commit",))
    body, where, problem = release_doc(commit, name)
    if body is not None:
        return ShippedBody(body, source=where)
    problems = [problem]
    caches = launcher.explicit_source_caches(commit_git_dir)
    if not caches:
        problems.append("no source cache was named in --commit-git-dir")
    for cache in caches:
        refusal = cache_problem(cache)
        if refusal:
            problems.append(f"source cache {cache} is not root's, so it is not read as Switchyard history: {refusal}")
            continue
        git_args = ("git", "--no-replace-objects", f"--git-dir={cache}")
        missing = [
            wanted for wanted in dict.fromkeys((commit, current_commit))
            if runner([*git_args, "cat-file", "-e", f"{wanted}^{{commit}}"], stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, text=True).returncode != 0
        ]
        if missing:
            problems.append(f"source cache {cache} does not hold {', '.join(missing)}")
            continue
        shown = runner([*git_args, "show", f"{commit}:docs/onboarding/{name}"], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        if shown.returncode != 0:
            problems.append(f"source cache {cache} has no docs/onboarding/{name} at {commit}")
            continue
        raw = shown.stdout if isinstance(shown.stdout, bytes) else str(shown.stdout or "").encode("utf-8")
        try:
            return ShippedBody(as_installed(raw), source=f"source cache {cache} at {commit}", git_args=git_args)
        except UnicodeDecodeError:
            problems.append(f"source cache {cache}: docs/onboarding/{name} at {commit} is not UTF-8")
    return ShippedBody(None, problems=tuple(problems))


def is_ancestor(git_args: tuple[str, ...], commit: str, current_commit: str,
                runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool | None:
    """Whether `commit` is in `current_commit`'s history in a cache `shipped_body` proved root's; None when unknown."""
    done = runner([*git_args, "merge-base", "--is-ancestor", commit, current_commit], stdout=subprocess.PIPE,
                  stderr=subprocess.PIPE, text=True)
    return {0: True, 1: False}.get(done.returncode)
