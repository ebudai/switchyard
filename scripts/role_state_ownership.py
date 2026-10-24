"""A tenant's role state store: who owns it, and giving it back without following anything the tenant controls.

- `_open_tenant_state_root`, `_root_placed_link` and `_close_quietly` open a
  store root one component at a time from its parent's descriptor with
  `O_NOFOLLOW`, allowing a symlink only where root placed it in a directory
  only root can write.
- `_walk_tenant_state_tree` visits a root and everything beneath it, in sorted
  order and never through a symlink, no deeper than `STATE_TREE_MAX_DEPTH`,
  offering a chown that acts through the descriptor each path was read
  through.
- `role_state_roots`, `role_state_ownership_problems` and `_uid_owner_name`
  report, read-only, what in the store is not the project account's, and what
  cannot be walked safely.
- `repair_role_state_ownership` gives it back -- refusing first, root only,
  honouring a dry run -- and reads it again afterwards.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-392), in their original
order. The launcher imports this module and re-exports every name, so its own
caller, the identity cutover that reads the walk through it, and every suite
that patches or rebinds these there reach the same objects. The current user
and each role's session directory -- launcher facilities -- and every name
defined here that another definition here reads when it runs, the depth limit
included, are read from `team_launcher` when it runs, as it was, so a patch on
the launcher still intercepts. The `print_func` default is bound when the
function is defined, as it was. The standard-library names are this module's
own imports, the same objects. `ProjectConfig` is imported for annotations
only. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import errno
import os
import pwd
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


STATE_TREE_MAX_DEPTH = 64


def _open_tenant_state_root(root: Path) -> tuple[int, str]:
    """Open `root` as a directory, following nothing the tenant could have set.

    Every component is opened with O_NOFOLLOW from its parent's descriptor. A
    symlink is allowed only where root itself put it -- the link and the
    directory holding it are root's, and nobody else can write that directory
    -- because a chown walk that follows a link the tenant can create or swap
    reaches whatever the tenant aims it at, however carefully the final chown
    is written. `follow_symlinks=False` protects the last component and nothing
    above it (SYRD-233 DAT).

    Returns (fd, "") for a directory, (-1, "") when it is simply not there, and
    (-1, reason) when it is refused. The caller owns the descriptor.
    """
    from scripts import team_launcher as launcher

    try:
        fd = os.open(root.anchor or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        return -1, f"{root.anchor or '/'} cannot be opened ({exc.strerror})"
    walked = Path(root.anchor or "/")
    try:
        for component in root.relative_to(walked).parts:
            walked = walked / component
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                launcher._close_quietly(fd)
                return -1, ""
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    launcher._close_quietly(fd)
                    return -1, f"{walked} cannot be opened ({exc.strerror})"
                allowed = launcher._root_placed_link(component, dir_fd=fd)
                if allowed:
                    launcher._close_quietly(fd)
                    return -1, allowed
                try:
                    child = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd
                    )
                except OSError as followed:
                    launcher._close_quietly(fd)
                    return -1, f"{walked} cannot be opened ({followed.strerror})"
            # The child is the one to close if closing its parent fails.
            previous, fd = fd, child
            os.close(previous)
    except OSError as exc:  # pragma: no cover - defensive
        launcher._close_quietly(fd)
        return -1, f"{walked} cannot be opened ({exc.strerror})"
    except BaseException:
        launcher._close_quietly(fd)
        raise
    return fd, ""


def _root_placed_link(component: str, *, dir_fd: int) -> str:
    """"" when this symlink is root's own layout, else why it is refused."""
    try:
        info = os.lstat(component, dir_fd=dir_fd)
    except OSError as exc:
        return f"{component} cannot be inspected ({exc.strerror})"
    if not stat.S_ISLNK(info.st_mode):
        return f"{component} is not a directory"
    holder = os.fstat(dir_fd)
    if info.st_uid != 0 or holder.st_uid != 0 or holder.st_mode & 0o022:
        return (
            f"{component} is a symlink that root did not place (it belongs to uid "
            f"{info.st_uid}, in a directory owned by uid {holder.st_uid}), and a root-run "
            "chown does not follow one"
        )
    return ""


def _close_quietly(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:  # pragma: no cover - defensive
        pass


def _walk_tenant_state_tree(
    root: Path, act: Callable[[Path, os.stat_result, Callable[[int, int], None]], str]
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Visit `root` and everything beneath it without following any symlink.

    `act` is called for each path with its `lstat` and a `chown(uid, gid)` that
    acts through the descriptor the path was just read through, so nothing can
    be swapped between deciding about a path and changing it; it returns a
    reason to report, or "". A symlink is visited as the link itself and never
    descended into, so it cannot become a traversal root.

    With `tenant_paths`' guarantees (SYRD-667): nothing on another filesystem
    than the root's is visited -- a mount below it is a refusal -- and `chown`
    changes exactly the object that was examined, held by an `O_PATH`
    descriptor bound to its inode, never a name looked up again; a directory
    is descended into only when what opens at its name is the directory that
    was examined. A file with
    more than one hard link is never given away: another name, perhaps another
    account's, holds the same inode. Either is a refusal, and nothing is
    changed for it.

    Returns (findings, refusals). A refusal is a root that could not be walked
    safely, which is a reason to stop rather than a path to fix.
    """
    from scripts import team_launcher as launcher

    findings: list[tuple[Path, str]] = []
    refusals: list[tuple[Path, str]] = []
    fd, problem = launcher._open_tenant_state_root(root)
    if problem:
        return findings, [(root, problem)]
    if fd < 0:
        return findings, refusals

    from scripts import tenant_paths

    def chown_observed(child: Path, name: str, directory: int, info: os.stat_result, uid: int, gid: int) -> None:
        if (info.st_uid, info.st_gid) == (uid, gid):
            return  # nothing given away, so nothing for another name to share in
        if not stat.S_ISDIR(info.st_mode) and info.st_nlink > 1:
            refusals.append((child, f"has {info.st_nlink} hard links; root does not give away a file another name "
                                    "may hold"))
            return
        try:
            handle = os.open(name, tenant_paths.HANDLE, dir_fd=directory)
        except FileNotFoundError:
            refusals.append((child, "vanished: it changed while root was walking it"))
            return
        try:
            held = os.fstat(handle)
            if (held.st_ino, held.st_dev, stat.S_IFMT(held.st_mode)) != (
                    info.st_ino, info.st_dev, stat.S_IFMT(info.st_mode)):
                refusals.append((child, "was replaced while root was walking it"))
                return
            tenant_paths._fchown_handle(handle, uid, gid)
        finally:
            os.close(handle)

    def visit(display: Path, directory: int, depth: int) -> None:
        if depth > launcher.STATE_TREE_MAX_DEPTH:
            refusals.append((display, f"is nested deeper than {launcher.STATE_TREE_MAX_DEPTH} directories"))
            return
        try:
            names = sorted(entry.name for entry in os.scandir(directory))
        except OSError as exc:
            findings.append((display, f"cannot be listed ({exc.strerror})"))
            return
        for name in names:
            child = display / name
            try:
                info = os.lstat(name, dir_fd=directory)
            except OSError as exc:
                findings.append((child, f"cannot be inspected ({exc.strerror})"))
                continue
            if info.st_dev != root_dev:
                refusals.append((child, "is on another filesystem than its store; root does not cross a mount"))
                continue

            def chown(uid: int, gid: int, _child: Path = child, _name: str = name, _fd: int = directory,
                      _info: os.stat_result = info) -> None:
                chown_observed(_child, _name, _fd, _info, uid, gid)

            reason = act(child, info, chown)
            if reason:
                findings.append((child, reason))
            if not stat.S_ISDIR(info.st_mode):
                continue
            try:
                below = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
            except OSError as exc:
                findings.append((child, f"cannot be opened ({exc.strerror})"))
                continue
            try:
                # The directory examined, or none: O_NOFOLLOW refuses a link swapped in at the name, but another real
                # directory renamed there opens just the same (Audit of 458bcd63).
                held = os.fstat(below)
                if (held.st_ino, held.st_dev, stat.S_IFMT(held.st_mode)) != (
                        info.st_ino, info.st_dev, stat.S_IFMT(info.st_mode)):
                    refusals.append((child, "was replaced while root was walking it"))
                    continue
                visit(child, below, depth + 1)
            finally:
                os.close(below)

    try:
        info = os.fstat(fd)
        root_dev = info.st_dev

        def chown_root(uid: int, gid: int, _fd: int = fd) -> None:
            os.fchown(_fd, uid, gid)

        reason = act(root, info, chown_root)
        if reason:
            findings.append((root, reason))
        visit(root, fd, 1)
    finally:
        os.close(fd)
    return findings, refusals


def role_state_roots(config: ProjectConfig) -> list[Path]:
    """The session store and each role's store, deduplicated, in walk order."""
    from scripts import team_launcher as launcher

    roots = [config.session_dir.expanduser()]
    for role in config.roles:
        directory = launcher.role_session_dir(config, role).expanduser()
        if directory not in roots:
            roots.append(directory)
    return roots


def role_state_ownership_problems(
    config: ProjectConfig,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """(wrong owner, refused) for this tenant's role state store.

    Read-only, so an upgrade can report them before it repairs them, and so a
    launch can say why it is leaving a role alone. The store is the tenant's
    own: every directory and file under it belongs to the project account, and
    anything that does not is something a privileged run left behind
    (SYRD-233).

    A refusal is not a path to chown. It is a store root that is a symlink, or
    is not a directory, or cannot be walked without following one, and it stops
    the repair rather than redirecting it (SYRD-233 DAT).
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    try:
        wanted = pwd.getpwnam(owner).pw_uid
    except KeyError:
        return [], []
    found: list[tuple[Path, str]] = []
    refused: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def inspect(path: Path, info: os.stat_result, _chown: Callable[[int, int], None]) -> str:
        if path in seen:
            return ""
        seen.add(path)
        if info.st_uid != wanted:
            return f"is owned by {launcher._uid_owner_name(info.st_uid) or info.st_uid}, not {owner}"
        return ""

    for root in launcher.role_state_roots(config):
        findings, refusals = launcher._walk_tenant_state_tree(root, inspect)
        found.extend(findings)
        refused.extend(refusals)
    return found, refused


def _uid_owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return ""


def repair_role_state_ownership(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Give the tenant account its own role state back. Root's to do.

    `ensure_owner_state_dirs` assigns the store's two top-level directories and
    is not recursive, and repatriation -- which does walk the per-role ones --
    returns immediately for a tenant already on isolated state with no legacy
    account bindings. So a role directory created by a root-run launch stays
    root's, and the project account cannot record that role's provider state
    ever again (SYRD-233 live UAT).
    """
    from scripts import team_launcher as launcher

    owner = config.run_as_user or launcher.current_user_name()
    problems, refusals = launcher.role_state_ownership_problems(config)
    if refusals:
        # Before anything is changed, and whoever is asking: a store root that
        # is a symlink is not a store this may walk, and repairing "what it
        # points at" is how a root-run chown ends up outside the tenant's tree.
        path, reason = refusals[0]
        print_func(
            f"switchyard: refusing to touch {config.project}'s role state: {path} {reason}. "
            f"No ownership was changed. Make it a real directory under {owner}'s state store, "
            f"then run `sudo switchyard upgrade {config.project}` again."
        )
        return False
    if not problems:
        return True
    if os.geteuid() != 0:
        print_func(
            f"switchyard: {config.project}'s role state is not all {owner}'s "
            f"({len(problems)} path(s), first: {problems[0][0]} {problems[0][1]}), and only root "
            f"can give it back. Run `sudo switchyard upgrade {config.project}`."
        )
        return False
    if dry_run:
        print_func(
            f"switchyard: would give {owner} back {len(problems)} path(s) of {config.project}'s "
            f"role state, starting with {problems[0][0]} ({problems[0][1]}); nothing written"
        )
        return True
    try:
        ids = pwd.getpwnam(owner)
    except KeyError:
        print_func(f"switchyard: {owner} is not a local account; {config.project}'s role state was left alone")
        return False
    # The store's roots are the tenant's configuration's to name -- its session directory, each role's name --
    # so one outside the owner's roots is refused before anything is given away (SYRD-609).
    from scripts import owner_writes

    roots = launcher.role_state_roots(config)
    outside = [root for root in roots if not owner_writes.within_owner_root(owner, root)]
    if outside:
        print_func(
            f"switchyard: refusing to touch {config.project}'s role state: {outside[0]} is outside {owner}'s home "
            "and every directory root chose for it. No ownership was changed."
        )
        return False
    failures: list[str] = []

    def give_back(path: Path, info: os.stat_result, chown: Callable[[int, int], None]) -> str:
        if info.st_uid == ids.pw_uid:
            return ""
        try:
            # Through the descriptor this path was just read through, and never
            # through what a symlink points at: the link itself is the tenant's,
            # its target may be anywhere at all.
            chown(ids.pw_uid, ids.pw_gid)
        except OSError as exc:
            failures.append(f"switchyard: could not give {path} back to {owner}: {exc.strerror}")
        return ""

    for root in roots:
        _findings, denied = launcher._walk_tenant_state_tree(root, give_back)
        if denied:
            failures.append(f"switchyard: could not walk {denied[0][0]}: {denied[0][1]}")
        if failures:
            print_func(failures[0])
            return False
    remaining, still_refused = launcher.role_state_ownership_problems(config)
    if still_refused:
        print_func(
            f"switchyard: {config.project}'s role state could not be re-read after the repair: "
            f"{still_refused[0][0]} {still_refused[0][1]}"
        )
        return False
    if remaining:
        print_func(
            f"switchyard: {config.project}'s role state still has {len(remaining)} path(s) that "
            f"are not {owner}'s, starting with {remaining[0][0]}"
        )
        return False
    print_func(
        f"switchyard: gave {owner} back {len(problems)} path(s) of {config.project}'s role state, "
        "so its roles can record what their runtimes were started against"
    )
    return True
