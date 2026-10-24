"""Root's writes and reads of a tenant's generated files: by descriptor from the owner's home (SYRD-608).

As root on a tenant's behalf the launcher wrote the tenant's generated files
by path, handed them over with `chown o:o PATH` and read some back by path.
Each followed a link the tenant left at the file or at any directory above
it, so a planted link had root overwrite, give away or read back a file of
the tenant's choosing. Everything here goes through `scripts/tenant_paths.py`
from the owner's home instead: no link at any component is followed, a
component swapped after root looked is refused, and a path outside the home
-- an absolute `layout` in the tenant's own config, a `..` -- is refused, never
normalised in. A caller acting for itself (not root, or the owner) is
unchanged.

The home is the trusted root, and so is a project directory root itself chose
for that owner somewhere else (`new --project-path`, SYRD-607): the one `new`
was handed (`trust_project_root`), or one the host's registry names -- read only
once every component of it, and each entry, is proven root's and writable by
nobody else, never from a registry the environment points at (audit of
d4c09c04). Either is used only while it is a real directory the owner owns,
never one derived from the tenant's own documents.

The launcher re-exports these, and they read the launcher's names -- the home
lookup and the current user -- through it when they run, so a patch there
reaches them. This module imports `team_launcher` only inside the functions.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Callable


def owner_home(owner_user: str) -> str:
    """The owner's home: the only trusted starting point for root's work on the owner's files."""
    from scripts import team_launcher as launcher

    home = launcher.home_dir_for_user(owner_user) if owner_user else None
    if home is None:
        raise ValueError("file ownership repair requires run_as_user")
    return str(home)


#: Project directories root chose for an owner in this process (what `new` was handed), beside the home.
TRUSTED_PROJECT_ROOTS: dict[str, set[str]] = {}


def trust_project_root(owner_user: str, project_dir: Path) -> None:
    """Root's own choice of where an owner's project lives: a root for writes on the owner's behalf."""
    TRUSTED_PROJECT_ROOTS.setdefault(owner_user, set()).add(str(project_dir))


#: The registry that may name a project root: the host's, fixed. Never `SWITCHYARD_PROJECT_REGISTRY_DIR` or anything
#: else the caller's environment says; a suite relocates it here, with the directory its proof walks from.
TRUSTED_REGISTRY: tuple[Path, str] = (Path("/etc/switchyard/projects"), "/")


def _registered_project_roots() -> list[str]:
    """Project directories the trusted registry names, from entries proven root's alone from `/` down.

    Each entry's whole path -- the registry directory and everything above it
    included -- must belong to root and be writable by nobody else; only root
    could then change what it says, and it is opened without following a link.
    """
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    registry, base = TRUSTED_REGISTRY
    try:
        names = sorted(name for name in os.listdir(registry) if name.endswith(".json"))
    except OSError:
        return []
    roots = []
    for name in names:
        entry = registry / name
        if root_controlled_problems(str(entry), expect_uid=0, base=base):
            continue
        try:
            descriptor = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                continue
            raw = json.loads(os.read(descriptor, 1 << 20).decode("utf-8"))
        except (OSError, ValueError):
            continue
        finally:
            os.close(descriptor)
        provision = Path(str(raw.get("config_path") or "") if isinstance(raw, dict) else "").parent
        if provision.name == "provision" and provision.parent.name == ".switchyard":
            roots.append(str(provision.parent.parent))
    return roots


def _owned_directory(path: str, owner_user: str) -> bool:
    try:
        status = os.lstat(path)
        return stat.S_ISDIR(status.st_mode) and status.st_uid == pwd.getpwnam(owner_user).pw_uid
    except (OSError, KeyError):
        return False


def _runtime_root(owner_user: str) -> list[str]:
    """The owner's runtime directory (`/run/user/<uid>`): the system's, made for that owner alone."""
    from scripts import team_launcher as launcher

    try:
        return [str(launcher.runtime_dir_for_uid(pwd.getpwnam(owner_user).pw_uid))]
    except KeyError:
        return []


def owner_root(owner_user: str, path: Path) -> str:
    """The trusted root `path` is written under for `owner_user`: the home, the owner's runtime directory, or a
    project directory root chose.

    A path in none of them gets the home, which `tenant_paths` then refuses as
    outside it. Only a normalised absolute path is ever matched.
    """
    home = owner_home(owner_user)
    text = str(path)
    if text != str(PurePosixPath(text)) or not text.startswith("/") or "/../" in f"{text}/":
        return home
    candidates = [*_runtime_root(owner_user), *sorted(TRUSTED_PROJECT_ROOTS.get(owner_user, ())),
                  *_registered_project_roots()]
    runtime = _runtime_root(owner_user)
    for root in [home, *candidates]:
        inside = text == root or text.startswith(root.rstrip("/") + "/")
        # The home and the runtime directory are the owner's by construction; a project directory only while it is.
        if inside and (root == home or root in runtime or _owned_directory(root, owner_user)):
            return root
    return home


def within_owner_root(owner_user: str, path: Path) -> bool:
    """Whether root may act on `path` for `owner_user`: a normalised absolute path inside one of the owner's roots.

    For root's work on a path the tenant's configuration names -- its session
    store, a role's directory, a worktree -- which is otherwise refused rather
    than followed or normalised in (SYRD-609).
    """
    text = str(path)
    if not owner_user or text != str(PurePosixPath(text)) or not text.startswith("/") or "/../" in f"{text}/":
        return False
    root = owner_root(owner_user, path)
    return text == root or text.startswith(root.rstrip("/") + "/")


def owner_handoff_args(owner_user: str, path: Path, *, tree: bool = False, to: str = "", group: str = "") -> list[str]:
    """The argv that gives `path` to `owner_user`: the descriptor helper's `attrs`, rooted at the owner's root.

    With `tree`, the whole tree below it: the helper's `chown-tree`, which changes a link inside as a link and
    never follows one -- where `chown -R` by path followed a link at the top or above it (SYRD-611). With `to`
    (and `group`), a tree in the owner's tree goes to that account instead -- a role's, or what a rollback puts
    back -- still rooted at the owner's root (SYRD-612).
    """
    from scripts.ticket_board.provision_tenant_paths import tenant_paths_argv

    if tree:
        return tenant_paths_argv(owner_root(owner_user, path), "chown-tree", "--owner", to or owner_user,
                                 *(("--group", group) if group else ()), str(path))
    return tenant_paths_argv(owner_root(owner_user, path), "attrs", "--owner", owner_user, "--group", owner_user, str(path))


def root_writes_for(owner_user: str) -> bool:
    """Whether a file written now for `owner_user` is written by root on the owner's behalf."""
    from scripts import team_launcher as launcher

    return bool(owner_user) and os.geteuid() == 0 and launcher.current_user_name() != owner_user


def tenant_root_holding(path: Path, *accounts: str) -> str:
    """The account among `accounts` -- and the owners of the projects the trusted registry names -- whose roots hold
    `path`, or "" (SYRD-666): a directory root must not write into by path."""
    owners = list(accounts)
    for root in _registered_project_roots():
        try:
            owners.append(pwd.getpwuid(os.lstat(root).st_uid).pw_name)
        except (OSError, KeyError):
            continue
    for owner in dict.fromkeys(name for name in owners if name):
        try:
            if within_owner_root(owner, path):
                return owner
        except ValueError:  # no such account, so no tree of its
            continue
    return ""


def root_writes_in(owner_user: str, path: Path) -> bool:
    """Whether root, writing `path` now, writes into one of `owner_user`'s roots -- and so must by descriptor.

    For a directory the caller chose that may be the tenant's or root's own:
    `new`'s output directory is the tenant's provision directory under
    `switchyard new`, and a temporary directory of root's own, or one the
    operator named, under `team-launcher new` (SYRD-611).
    """
    try:
        return root_writes_for(owner_user) and within_owner_root(owner_user, path)
    except ValueError:  # no such account, so no tree of its to write into
        return False


def write_owner_file(owner_user: str, path: Path, content: bytes | str, *, mode: int | None = None,
                     parents_mode: int = 0o755, parent_mode: int | None = None, new_mode: int = 0o600,
                     exclusive: bool = False) -> bool:
    """Publish a generated file the owner is to own: whole, and as root by descriptor from the owner's home.

    As root (and not the owner), the file is `tenant_paths.write_file`d under
    the owner's home: never through a link at any component, never outside
    the home, made owner:owner-group with `mode` (None keeps the replaced
    file's, else `new_mode`) before it is renamed into place; a missing directory on
    the way is made the owner's with `parents_mode`, and `parent_mode` sets the
    parent's own. Otherwise it is the caller's own file, written as before.

    `exclusive` writes only where nothing is yet -- as root, nothing at all at
    the name, a link included, and never over something that arrives after
    the look (SYRD-611). Returns whether it wrote.
    """
    from scripts import tenant_paths

    data = content.encode("utf-8") if isinstance(content, str) else content
    if not root_writes_for(owner_user):
        if exclusive and path.exists():
            return False
        path.write_bytes(data)
        return True
    try:
        account = pwd.getpwnam(owner_user)
    except KeyError:
        raise SystemExit(f"switchyard: refusing to write {path}: {owner_user} is not an account on this host") from None
    try:
        return tenant_paths.write_file(owner_root(owner_user, path), str(path), data, uid=account.pw_uid,
                                       gid=account.pw_gid, mode=mode, parents=(parents_mode, account.pw_uid,
                                                                               account.pw_gid),
                                       parent_mode=parent_mode, new_mode=new_mode, exclusive=exclusive)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {refusal}. Nothing was written.") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {stop}") from None


def write_root_record(owner_user: str, path: Path, content: bytes, *, mode: int | None,
                      parents_mode: int | None = None, new_mode: int = 0o600) -> None:
    """A record root keeps in a tenant's directory, as root's own file: written as `write_owner_file` writes.

    By descriptor from the owner's home, staged where only root can reach it
    and renamed into place; owned by the process's own (kernel) identity, as
    a record root made, not handed to the tenant. With `parents_mode`, a
    missing directory on the way is made as root's own too, as a by-path
    `mkdir -p` by root made it; `mode` None keeps the replaced file's, else
    `new_mode` (SYRD-611).
    """
    from scripts import tenant_paths

    uid, gid = os.getresuid()[1], os.getresgid()[1]
    try:
        tenant_paths.write_file(owner_root(owner_user, path), str(path), content, uid=uid, gid=gid, mode=mode,
                                parents=None if parents_mode is None else (parents_mode, uid, gid), new_mode=new_mode)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {refusal}. Nothing was written.") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {stop}") from None


def rewrite_owner_file(owner_user: str, path: Path, content: bytes | str) -> None:
    """Replace an existing file of the owner's tree whole, keeping the owner and mode it had (SYRD-611).

    Both are read from a descriptor opened on it beneath the owner's root, so
    nothing reached through a link informs them; it must belong to the owner
    or to root, and keeps no special bit.
    """
    from scripts import tenant_paths

    data = content.encode("utf-8") if isinstance(content, str) else content
    status = owner_file_status(owner_user, path)
    if status is None:
        raise SystemExit(f"switchyard: refusing to rewrite {path} for {owner_user}: it does not exist")
    try:
        owner_uid = pwd.getpwnam(owner_user).pw_uid
    except KeyError:
        raise SystemExit(f"switchyard: refusing to write {path}: {owner_user} is not an account on this host") from None
    if status.st_uid not in (owner_uid, 0):
        raise SystemExit(f"switchyard: refusing to rewrite {path} for {owner_user}: it belongs to uid {status.st_uid}")
    try:
        tenant_paths.write_file(owner_root(owner_user, path), str(path), data, uid=status.st_uid, gid=status.st_gid,
                                mode=stat.S_IMODE(status.st_mode) & 0o777)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {refusal}. Nothing was written.") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to write {path} for {owner_user}: {stop}") from None


def copy_owner_tree(source_user: str, source: Path, owner_user: str, target: Path) -> None:
    """Merge `source_user`'s tree at `source` into `owner_user`'s at `target`, replacing nothing (SYRD-612).

    Both by descriptor, each from its own account's root: nothing in the source is followed or copied through a
    link, nothing in the target is created through one, and what is copied is the owner's, with the source's mode
    and times (`tenant_paths.copy_tree`).
    """
    from scripts import tenant_paths

    try:
        account = pwd.getpwnam(owner_user)
    except KeyError:
        raise SystemExit(f"switchyard: refusing to copy into {target}: {owner_user} is not an account on this host") from None
    source_home = owner_root(source_user, source)
    if source_home == "/":
        # An account whose home is `/` -- a system account -- has no tree of its own there to copy from, and what is
        # under `/` is root's, which is never carried into a tenant's tree.
        return
    source_root = tenant_paths._quiet(source_home)
    try:
        target_root = tenant_paths._quiet(owner_root(owner_user, target))
        try:
            tenant_paths.copy_tree(source_root, str(source), target_root, str(target), account.pw_uid, account.pw_gid)
        finally:
            target_root.close()
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to copy {source} into {target} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to copy {source} into {target} for {owner_user}: {stop}") from None
    finally:
        source_root.close()


def read_owner_link(owner_user: str, path: Path) -> str:
    """Where the link at `path`, in the owner's tree, points -- its parent reached by descriptor; "" if no link."""
    from scripts import tenant_paths

    try:
        return tenant_paths.link_target(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {refusal}") from None


def replace_owner_link(owner_user: str, path: Path, target: str) -> None:
    """Point the link at `path`, in the owner's tree, at `target`: by descriptor, renamed into place (SYRD-612)."""
    from scripts import tenant_paths

    root = tenant_paths._quiet(owner_root(owner_user, path))
    try:
        tenant_paths.replace_link(root, str(path), target)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to point {path} at {target} for {owner_user}: {refusal}") from None
    finally:
        root.close()


def remove_root_record(owner_user: str, path: Path) -> None:
    """Remove a record root keeps in the owner's tree: the entry at the name, by descriptor, never followed."""
    from scripts import tenant_paths

    try:
        tenant_paths.remove_file(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to remove {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to remove {path} for {owner_user}: {stop}") from None


def supersede_owner_record(owner_user: str, path: Path) -> bool:
    """`clear_session_record_for_role` by descriptor from the owner's root: the record moved aside to `.superseded`
    (an earlier one to `.superseded.1`) and its resume timeout removed, each entry as itself, never followed
    (SYRD-612). Whether there was a record."""
    from scripts import tenant_paths

    root, name = owner_root(owner_user, path), path.name
    try:
        if tenant_paths.entry_status(root, str(path)) is None:
            return False
        tenant_paths.rename_file(root, str(path.with_name(f"{name}.superseded")), f"{name}.superseded.1")
        tenant_paths.remove_file(root, str(path.with_name(f"{name}.resume_timeout")))
        return tenant_paths.rename_file(root, str(path), f"{name}.superseded")
    except (tenant_paths.Refused, SystemExit) as refusal:
        raise SystemExit(f"switchyard: refusing to clear {path} for {owner_user}: {refusal}") from None


def holds_as_owner(owner_user: str, predicate: Callable[[], bool]) -> bool:
    """Whether `predicate()` holds -- asked, when root acts for `owner_user`, in a child that has become the owner.

    For a question answered by reading the owner's tree by path, as the providers' trust records are: a link there
    leads the child only where the owner can go, never where root can (SYRD-612). An owner that is no account, or a
    child that does not finish, is no.
    """
    from scripts.account_drop import _run_as_account

    if not root_writes_for(owner_user):
        return predicate()
    try:
        account = pwd.getpwnam(owner_user)
    except KeyError:
        return False

    def ask() -> None:
        if not predicate():
            raise LookupError(f"not so for {owner_user}")

    return _run_as_account(account.pw_uid, account.pw_gid, ask)


def ensure_owner_dirs(owner_user: str, paths: list[Path], *, mode: int) -> None:
    """Make or correct each directory as the owner's with `mode`, by descriptor from the owner's root (SYRD-611)."""
    from scripts import tenant_paths

    try:
        account = pwd.getpwnam(owner_user)
    except KeyError:
        raise SystemExit(f"switchyard: refusing to make {paths[0]}: {owner_user} is not an account on this host") from None
    for path in paths:
        home = owner_root(owner_user, path)
        if home in _runtime_root(owner_user):
            # The owner's own runtime directory: proven its, then made by the owner itself (SYRD-655).
            from scripts import owner_runtime_dirs

            try:
                owner_runtime_dirs.ensure_runtime_dirs(home, [str(path)], mode, owner_user)
            except tenant_paths.Refused as refusal:
                raise SystemExit(f"switchyard: refusing to make {path} for {owner_user}: {refusal}. "
                                 "Nothing was made.") from None
            continue
        root = tenant_paths._quiet(home)
        try:
            tenant_paths.ensure_dirs(root, [str(path)], mode, account.pw_uid, account.pw_gid)
        except tenant_paths.Refused as refusal:
            raise SystemExit(f"switchyard: refusing to make {path} for {owner_user}: {refusal}. Nothing was made.") from None
        finally:
            root.close()


def repair_owner_tree(owner_user: str, path: Path) -> None:
    """Give the tree at `path` back to the owner, with each directory above it that is not the owner's, up to the
    nearest one that is: `chown o:o` on those and `chown -R o:o` on the tree, by descriptor from the owner's home
    (SYRD-613).

    Every directory is opened from the home down without following a link or crossing a mount and changed on what
    was opened, so a link at the tree or above it -- planted, or swapped in after root looked -- is refused and what
    it points at keeps its owner. A link inside the tree is changed as a link (`tenant_paths.chown_tree`). A path
    outside the home is refused, never normalised in, and nothing missing is made.
    """
    from scripts import tenant_paths

    account = pwd.getpwnam(owner_user)
    owned = (account.pw_uid, account.pw_gid)
    root = tenant_paths._quiet(owner_home(owner_user))
    held: list[tuple[str, int, os.stat_result]] = []
    try:
        parts = root.relative(str(path))
        if root.fd is None:
            return
        held.append((root.home, os.dup(root.fd), os.fstat(root.fd)))
        for part in parts:
            display = f"{held[-1][0]}/{part}"
            observed = root._observe(held[-1][1], part)
            if observed is None or not (stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode)):
                break
            descriptor = root.open_directory(held[-1][1], part, display, observed)
            held.append((display, descriptor, os.fstat(descriptor)))
        reached = len(held) == len(parts) + 1
        first = len(held)
        while first > 0 and (held[first - 1][2].st_uid, held[first - 1][2].st_gid) != owned:
            first -= 1
        for display, descriptor, _status in held[first:len(held) - reached]:
            root._own(descriptor, display, None, *owned)
        if reached:
            tenant_paths.chown_tree(root, str(path), *owned, if_exists=True)
    finally:
        for _display, descriptor, _status in held:
            os.close(descriptor)
        root.close()


def owner_directory_names(owner_user: str, path: Path) -> list[str] | None:
    """The names in the owner's directory at `path`, listed on a descriptor opened from its root; None when missing.

    A link at the directory or above it is refused, never listed through (SYRD-613).
    """
    from scripts import tenant_paths

    try:
        root = tenant_paths._quiet(owner_root(owner_user, path))
        try:
            descriptor = root.walk(root.relative(str(path)))
            if descriptor is None:
                return None
            try:
                return sorted(os.listdir(descriptor))
            finally:
                os.close(descriptor)
        finally:
            root.close()
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to look in {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to look in {path} for {owner_user}: {stop}") from None


def read_owner_bytes(owner_user: str, path: Path) -> bytes | None:
    """A tenant's file that root is about to act on, read without following a link; None when it is missing.

    Root reading it by name would follow a link the tenant planted and carry
    whatever root can read into a file the tenant owns. As root (and not the
    owner) it is read by descriptor from the owner's home; otherwise it is the
    caller's own, read as before.
    """
    from scripts import tenant_paths

    if not root_writes_for(owner_user):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
    try:
        return tenant_paths.read_file(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {stop}") from None


def claim_owner_file(owner_user: str, path: Path, *, accept: Callable[[bytes], bool], mode: int,
                     apply: bool) -> tuple[bytes | None, str]:
    """Root's read of a file in the owner's tree that should be the owner's, and, if root left it as its own, the
    owner's from now on (SYRD-657): its contents (None when missing) and what happened -- "" (already the owner's),
    "handed", "would hand", or why not.

    One descriptor, opened without following a link at it or above it, is read, and one `fstat` of it decides:
    the owner's own file is left alone; only a file root owns can be handed over, and only if `accept` takes its
    contents; a file any other account owns is left exactly as it is, whatever it holds -- generated-looking
    contents do not show that root made it (Audit of 88c86394). The change is then made on that same descriptor.
    Judging one file and changing whatever is at the name afterwards would let the tenant rename another of root's
    files there in between.
    """
    from scripts import tenant_paths

    root = tenant_paths._quiet(owner_root(owner_user, path))
    try:
        found = root.leaf(root.relative(str(path)))
        if found is None:
            return None, ""
        parent, fd, display, status = found
        try:
            if not stat.S_ISREG(status.st_mode):
                raise tenant_paths.Refused(display, "is not a regular file")
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            content = b"".join(chunks)
            account = pwd.getpwnam(owner_user)
            held = os.fstat(fd)
            if held.st_uid == account.pw_uid:
                return content, ""
            if held.st_uid != 0:
                return content, f"it is uid {held.st_uid}'s, not root's, so root leaves it"
            if not accept(content):
                return content, "it is root's and not what root generates, so root leaves it"
            if not apply:
                return content, "would hand"
            os.fchown(fd, account.pw_uid, account.pw_gid)
            if stat.S_IMODE(held.st_mode) != mode:
                os.fchmod(fd, mode)
            return content, "handed"
        finally:
            os.close(fd)
            os.close(parent)
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {refusal}") from None
    finally:
        root.close()


def owner_file_status(owner_user: str, path: Path) -> os.stat_result | None:
    """The status of a regular file in the owner's tree, opened by descriptor from its root; None when missing.

    For root reading something of the owner's -- its mode, who it belongs to -- without following a link at the
    file or any directory above it, so nothing reached through one informs what root does next (SYRD-610).
    """
    from scripts import tenant_paths

    try:
        return tenant_paths.stat_file(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to read {path} for {owner_user}: {stop}") from None


def owner_entry_status(owner_user: str, path: Path) -> os.stat_result | None:
    """What is at `path` in the owner's tree -- a link reported as one, never followed -- or None when nothing is.

    By descriptor from the owner's root: a link above it is refused, so whether something is there is never
    decided by looking through one (SYRD-611, Audit of 94b20405).
    """
    from scripts import tenant_paths

    try:
        return tenant_paths.entry_status(owner_root(owner_user, path), str(path))
    except tenant_paths.Refused as refusal:
        raise SystemExit(f"switchyard: refusing to look at {path} for {owner_user}: {refusal}") from None
    except SystemExit as stop:
        raise SystemExit(f"switchyard: refusing to look at {path} for {owner_user}: {stop}") from None


def owner_entry_present(owner_user: str, path: Path) -> bool:
    """Whether anything is at `path`: as root for the owner, `owner_entry_status` (a link counts); otherwise the
    caller's own `exists()`, as before."""
    if not root_writes_for(owner_user):
        return path.exists()
    return owner_entry_status(owner_user, path) is not None


def read_owner_json(owner_user: str, path: Path) -> dict[str, Any]:
    """`read_owner_bytes` for a JSON object; missing is `FileNotFoundError`, as `_load_json` has it."""
    from scripts import team_launcher as launcher

    if not root_writes_for(owner_user):
        return launcher._load_json(path)
    data = read_owner_bytes(owner_user, path)
    if data is None:
        raise FileNotFoundError(2, "No such file or directory", str(path))
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed
