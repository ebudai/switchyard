"""Reading root's and the tenant's records by descriptor, following no link anywhere on the way.

- `expected_privileged_uid` and `root_controlled_problems_for` say whose files
  count as root's, through the documented provision-root override that lets the
  suites exercise these paths without /etc.
- `_walk_no_follow` opens a path's parent one component at a time with
  `O_NOFOLLOW`, so a symlinked ancestor is refused rather than followed, and
  tells a missing path from an unusable one.
- `read_plan_no_follow` reads one plan authority through that walk: no symlink,
  a regular file, and -- as the caller requires -- one link, the right owner,
  nobody else able to write it; the bytes are never quoted back.
- `_directory_owner_no_follow` and `read_tenant_document_no_follow` read a
  tenant's own generated document as root, entitling it by who owns the
  directory it sits in.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-382), in their original
order. The launcher imports this module and re-exports every name, so the
launcher's own callers, the fourteen privileged modules that read these through
it, and every suite that patches them there reach the same objects. Every
launcher facility these use -- the override's variable and the `PlanDocument`
result class, which stay in the launcher -- and every name defined here that
another definition here reads when it runs, is read from `team_launcher` when
it runs, as it was, so a patch on the launcher still intercepts. The
publication boundary's root-control check is still imported inside the one
function that uses it. The standard-library names are this module's own
imports, the same objects. This module never imports `team_launcher` at its
top.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import PlanDocument


def expected_privileged_uid() -> int:
    """Whose files count as root's for the privileged provision directory.

    Root's on a host. When the privileged provision root has been overridden it
    is the caller's own uid, because that override is the documented seam for
    exercising these paths without writing to /etc and a fixture cannot own a
    root-owned file. The same seam the trusted-release checks use (SYRD-97).
    """
    from scripts import team_launcher as launcher

    return os.getuid() if os.environ.get(launcher.PRIVILEGED_PROVISION_ROOT_ENV, "").strip() else 0


def root_controlled_problems_for(path: str) -> list[str]:
    """The whole-path root ownership check, through the documented test seam."""
    from scripts import team_launcher as launcher

    from scripts.ticket_board.publication_boundary import root_controlled_problems

    overridden = os.environ.get(launcher.PRIVILEGED_PROVISION_ROOT_ENV, "").strip()
    return root_controlled_problems(path, base=overridden or "/")


def read_plan_no_follow(
    path: Path,
    *,
    require_root_owned: bool,
    require_owner_uids: Sequence[int] | None = None,
    require_single_link: bool = False,
    require_not_shared_writable: bool = False,
) -> tuple[PlanDocument | None, str]:
    """Read one plan authority by fd, refusing symlinks at every component.

    `Path.read_text` on a tenant-controlled directory follows whatever is there.
    The tenant can replace its plan with a symlink to anything, and root would
    then read, truncate and re-own the referent instead (SYRD-100 review).
    """
    from scripts import team_launcher as launcher

    relative = Path(str(path).lstrip("/"))
    dir_fd, problem = launcher._walk_no_follow(Path(path.anchor or "/"), relative)
    if dir_fd < 0:
        return None, f"{path}: {problem}"
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None, f"{path} is a symlink, so it is not a plan this will read or write"
            if exc.errno == errno.ENOENT:
                return None, f"{path} does not exist"
            return None, f"{path} could not be opened ({exc.strerror})"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, f"{path} is not a regular file"
            if require_not_shared_writable and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None, (
                    f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                    "group or beyond can write"
                )
            if require_single_link and info.st_nlink != 1:
                # A second link is a second name for somebody else's file, and
                # the owner check passes on the file it points at rather than on
                # the document the tenant is entitled to write (SYRD-228).
                return None, (
                    f"{path} has {info.st_nlink} links, so it is another file under a second "
                    "name rather than this project's own document"
                )
            if require_owner_uids is not None:
                # Root reads a tenant's own document here, so the question is
                # not whether root owns it but whether it belongs to somebody
                # entitled to have written it -- the account root is acting for,
                # or root -- and whether anybody else can rewrite it between
                # this read and the decision made from it (SYRD-155).
                if info.st_uid not in set(require_owner_uids):
                    expected = ", ".join(str(uid) for uid in require_owner_uids)
                    return None, (
                        f"{path} is owned by uid {info.st_uid} rather than by uid {expected}, "
                        "so it is not a document this project's owner or root wrote"
                    )
                if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return None, (
                        f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                        "group or beyond can write"
                    )
            if require_root_owned:
                expected = launcher.expected_privileged_uid()
                if info.st_uid != expected:
                    return None, (
                        f"{path} is owned by uid {info.st_uid} rather than by uid {expected}"
                    )
                if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return None, (
                        f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                        "group or beyond can write"
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
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        # Why, never what: this may be a file the tenant pointed root at, and a
        # decoder message quotes the bytes it choked on (SYRD-228).
        return None, f"{path} is not readable as a plan document: it is not UTF-8 text"
    except json.JSONDecodeError as exc:
        return None, (
            f"{path} is not readable as a plan document: it is not JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        )
    if not isinstance(data, dict):
        return None, f"{path} is not a plan document"
    return launcher.PlanDocument(path, data, raw, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), ""


def _directory_owner_no_follow(directory: Path) -> tuple[os.stat_result | None, str]:
    """A directory's own stat, reached without following a link on the way."""
    from scripts import team_launcher as launcher

    relative = Path(str(directory).lstrip("/")) / "_"
    dir_fd, problem = launcher._walk_no_follow(Path(directory.anchor or "/"), relative)
    if dir_fd < 0:
        return None, f"{directory}: {problem}"
    try:
        info = os.fstat(dir_fd)
    finally:
        os.close(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        return None, f"{directory} is not a directory"
    return info, ""


def read_tenant_document_no_follow(path: Path, *, what: str) -> tuple[dict[str, Any] | None, str]:
    """Read a tenant's own generated document as root, following nothing.

    The privileged upgrade reads the tenant's plan and its generated
    configuration from a directory the tenant owns. `Path.read_text` follows
    whatever is there, so a symlink planted at either name sent root to read a
    file of the tenant's choosing -- reported, if it was not JSON, as a raw
    traceback rather than as a refusal (SYRD-228). The SYRD-227 ownership
    repair already opened these paths this way; the reads that ran BEFORE it
    did not.

    Entitled means the directory's own owner or root, because only root can
    change an owner: a file somebody else owns, one anybody else can write, one
    that is a second link to another file, and a symlink at any component are
    all refused. Returns (document, why not).
    """
    from scripts import team_launcher as launcher

    directory, problem = launcher._directory_owner_no_follow(path.parent)
    if directory is None:
        return None, f"refusing to read {what} at {path}: {problem}"
    if directory.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, (
            f"refusing to read {what} at {path}: {path.parent} is mode "
            f"{stat.S_IMODE(directory.st_mode):04o}, so anybody in its group or beyond can "
            "replace what is in it"
        )
    # Who may have put the file there is a question about the directory. Only
    # root can write into a root-owned directory, so a file it holds is one
    # root placed -- provisioning writes the tenant's own documents there and
    # gives them to the owner, which is exactly that shape. A directory the
    # tenant owns is one the tenant can fill with anything, so there the
    # document has to belong to that owner or to root.
    permitted = None if directory.st_uid == 0 else sorted({0, directory.st_uid})
    document, problem = launcher.read_plan_no_follow(
        path,
        require_root_owned=False,
        require_owner_uids=permitted,
        require_single_link=True,
        require_not_shared_writable=True,
    )
    if document is None:
        return None, f"refusing to read {what} at {path}: {problem}"
    return dict(document.data), ""


def _walk_no_follow(base: Path, relative: Path) -> tuple[int, str]:
    """Open `base/relative`'s parent by walking components with O_NOFOLLOW.

    Returns (dir_fd, problem). lstat on the leaf and its immediate parent was
    not enough: an ancestor several levels up can be a symlink, and the whole
    subtree then belongs to wherever it points (SYRD-39).
    """
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return -1, "missing"
    except OSError as exc:
        return -1, f"cannot open {base} ({exc.strerror})"
    for component in relative.parent.parts:
        parent_fd_for_report = fd
        try:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                # O_DIRECTORY|O_NOFOLLOW reports ENOTDIR for a symlinked
                # directory on Linux and ELOOP elsewhere. Either way it is
                # refused; name it accurately so the manifest is actionable.
                # This is the ancestor case lstat on the leaf could not see.
                try:
                    kind = os.lstat(component, dir_fd=parent_fd_for_report)
                    if stat.S_ISLNK(kind.st_mode):
                        os.close(fd)
                        return -1, f"ancestor {component} is a symlink"
                except OSError:
                    pass
                os.close(fd)
                return -1, f"ancestor {component} is not a directory"
            os.close(fd)
            if exc.errno == errno.ENOENT:
                # Nothing there yet: absent, not unsafe.
                return -1, "missing"
            return -1, f"ancestor {component} is unusable ({exc.strerror})"
        os.close(fd)
        fd = child
    return fd, ""
