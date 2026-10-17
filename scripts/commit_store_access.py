"""Whether the board service can actually read the commit store it is configured with.

The board verifies every commit hash against `TICKET_BOARD_COMMIT_GIT_DIR`, as
the board service's account. The boundary phase grants that read for a store
inside the tenant owner's home, and grants nothing for a store anywhere else:
a path another account owns is not ours to re-mode or traverse-grant
(SYRD-157). So a store elsewhere was never checked by anybody, and
`repair-boundary` called a boundary closed while the board could not open
`/data/git` -- `sapi-agent:sapi-agent 0750` -- on the way to MEFP's store
(SYRD-604).

This reads the answer from the filesystem. Every component of the path, and
everything git reads to resolve a commit in the layout the verifier opens (a
working repository's `.git`, directory or gitfile; the common directory; the
whole `objects`, `refs` and `reftable` trees; every alternate object store),
is judged from its mode bits and POSIX ACL against the identity the board
unit runs with: the service user, and the unit's
`Group=`/`SupplementaryGroups=`. A working repository another account owns is
refused by git's own ownership check, which no grant changes. Nothing is tried
as the caller, because the callers are root and the operator, who pass checks
the service fails (reachability is read from the bits). `getfacl` is the only
program it runs.

An objection the boundary phase cannot close -- a store outside the owner
home, or any store when the plan does not let the packet grant the service --
is an `OperatorDependency`: it says who owns the first closed component and
what read/traverse grant would open the path, and nothing here ever makes it.
"""

from __future__ import annotations

import grp
import os
import pwd
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Sequence

#: The single files git reads from a repository before it resolves a commit:
#: HEAD from the git directory, the rest from the common directory. Checked
#: only when present -- git does without `packed-refs` and `shallow`.
GITDIR_FILES = ("HEAD", "commondir")
COMMON_FILES = ("config", "packed-refs", "shallow")
#: The trees a commit is read out of: loose objects, packs and their indexes,
#: the commit graph, refs and a reftable. Every directory in them must be
#: listable and every file readable, because which object a given commit
#: needs is not known in advance.
COMMON_TREES = ("objects", "refs", "reftable")
#: How deep `objects/info/alternates` may chain, as git itself bounds it.
ALTERNATE_DEPTH = 5


class OperatorDependency(str):
    """An objection only somebody outside this tenant can resolve.

    A plain string everywhere objections are strings; callers that offer
    `repair-boundary` as the remedy check for this type and do not offer it.
    """


@dataclass(frozen=True)
class ServiceIdentity:
    user: str
    uid: int
    gids: tuple[int, ...]

    def describe(self) -> str:
        groups = ", ".join(_group_name(gid) for gid in self.gids) or "none"
        return f"{self.user} (uid {self.uid}, groups {groups})"


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _resolve_gid(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return int(name) if name.isdigit() else None


def service_identity(plan: Any) -> tuple[ServiceIdentity | None, str]:
    """The uid and groups the board unit gives its process.

    The unit sets `Group=` and `SupplementaryGroups=` to the plan's
    `roles_group`, and only those are counted. Groups the shared service
    account picks up from the group database are not this tenant's to rely on
    (SYRD-586), so access that depends on one is not access.
    """
    user = str(getattr(plan, "service_user", "") or "")
    if not user:
        return None, "the plan names no board service account"
    try:
        account = pwd.getpwnam(user)
        uid, primary = account.pw_uid, account.pw_gid
    except KeyError:
        if not user.isdigit():
            return None, f"the board service account {user} does not exist on this host"
        uid, primary = int(user), None
    group = str(getattr(plan, "roles_group", "") or "")
    if group:
        gid = _resolve_gid(group)
        if gid is None:
            return None, f"the board unit's group {group} does not exist on this host"
        return ServiceIdentity(user, uid, (gid,)), ""
    if primary is None:
        return ServiceIdentity(user, uid, ()), ""
    return ServiceIdentity(user, uid, tuple(sorted(set(os.getgrouplist(user, primary))))), ""


def _acl(path: str, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> list[tuple[str, str, str]] | None:
    """(tag, qualifier, permissions) for each access entry, or None when there is no ACL to read."""
    done = runner(["getfacl", "-cpn", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if getattr(done, "returncode", 1) != 0:
        return None
    entries = []
    for line in str(getattr(done, "stdout", "") or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("default:"):
            continue
        parts = line.split()[0].split(":")
        if len(parts) == 3:
            entries.append((parts[0], parts[1], parts[2]))
    return entries or None


def _permits(status: os.stat_result, entries: list[tuple[str, str, str]] | None,
             identity: ServiceIdentity, want: str) -> bool:
    """The POSIX.1e access check, for an identity that is not root."""
    mode = stat.S_IMODE(status.st_mode)

    def grants(perms: str) -> bool:
        return all(bit in perms for bit in want)

    if not entries:
        bits = lambda shift: "".join(  # noqa: E731
            letter if mode >> shift & flag else "-" for letter, flag in (("r", 4), ("w", 2), ("x", 1))
        )
        entries = [("user", "", bits(6)), ("group", "", bits(3)), ("other", "", bits(0))]
    mask = next((perms for tag, _q, perms in entries if tag == "mask"), "rwx")

    def masked(perms: str) -> str:
        return "".join(bit if bit in mask else "-" for bit in perms)

    if status.st_uid == identity.uid:
        return grants(next((perms for tag, q, perms in entries if tag == "user" and not q), "---"))
    for tag, qualifier, perms in entries:
        if tag == "user" and qualifier == str(identity.uid):
            return grants(masked(perms))
    matched = False
    for tag, qualifier, perms in entries:
        if tag != "group":
            continue
        gid = status.st_gid if not qualifier else int(qualifier) if qualifier.isdigit() else None
        if gid in identity.gids:
            matched = True
            if grants(masked(perms)):
                return True
    if matched:
        return False
    return grants(next((perms for tag, _q, perms in entries if tag == "other"), "---"))


@dataclass(frozen=True)
class Closed:
    path: str
    want: str
    status: os.stat_result | None
    acl: bool


def _describe(closed: Closed) -> str:
    if closed.status is None:
        return f"{closed.path} could not be examined as uid {os.geteuid()}"
    owner, group = _user_name(closed.status.st_uid), _group_name(closed.status.st_gid)
    acl = "its ACL does not give" if closed.acl else "no ACL entry gives"
    return (
        f"{closed.path} is {owner}:{group} mode {stat.S_IMODE(closed.status.st_mode):04o}, and {acl} "
        f"it {'traverse (--x)' if closed.want == 'x' else 'read (' + closed.want + ')'}"
    )


def _unescape(name: str) -> str:
    """getfacl writes whitespace, backslashes and unprintables in a name as `\\ooo`."""
    out, index = bytearray(), 0
    raw = name.encode("utf-8", "surrogateescape")
    while index < len(raw):
        if raw[index:index + 1] == b"\\" and raw[index + 1:index + 4].isdigit():
            out.append(int(raw[index + 1:index + 4], 8))
            index += 4
        else:
            out.append(raw[index])
            index += 1
    return out.decode("utf-8", "surrogateescape")


def _acl_tree(path: str, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> dict[str, list[tuple[str, str, str]]]:
    """The access entries of everything under `path`, from one `getfacl -R`, links not followed."""
    done = runner(["getfacl", "-R", "-P", "-pn", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    table: dict[str, list[tuple[str, str, str]]] = {}
    current: list[tuple[str, str, str]] | None = None
    for line in str(getattr(done, "stdout", "") or "").splitlines():
        if line.startswith("# file: "):
            current = table.setdefault(_unescape(line[len("# file: "):]), [])
            continue
        line = line.split("#", 1)[0].strip()
        if current is None or not line or line.startswith("default:"):
            continue
        parts = line.split()[0].split(":")
        if len(parts) == 3:
            current.append((parts[0], parts[1], parts[2]))
    return table


class _Walk:
    """What a verifier opening this store would have to read, and what of it the identity cannot."""

    def __init__(self, identity: ServiceIdentity, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> None:
        self.identity, self.runner = identity, runner
        self.closed: list[Closed] = []
        self.roots: list[str] = []
        self.seen: set[str] = set()
        self.walked: set[str] = set()

    def need(self, path: str, want: str, table: dict | None = None) -> bool:
        """Whether `path` exists for the identity; record it closed if it is not usable as `want`."""
        if path in self.seen:
            return True
        self.seen.add(path)
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            return False
        except OSError:
            self.closed.append(Closed(path, want, None, False))
            return False
        if stat.S_ISLNK(status.st_mode):
            target = os.path.realpath(path)
            self.ancestors(target)
            return self.need(target, want)
        if not stat.S_ISDIR(status.st_mode):
            want = want.replace("x", "")
        entries = table[path] if table is not None and table.get(path) else _acl(path, self.runner)
        if not _permits(status, entries, self.identity, want):
            self.closed.append(Closed(path, want, status, entries is not None and len(entries) > 3))
        return True

    def ancestors(self, path: str) -> None:
        parts = PurePosixPath(path).parts
        for index in range(len(parts) - 1):
            self.need(str(PurePosixPath(*parts[: index + 1])), "x")

    def tree(self, top: str) -> None:
        """Every directory listable and every file readable, all the way down, through links.

        Git follows a directory link anywhere in the object database -- an
        `objects` that is a link, or a fan-out directory that is one -- so this
        does too: the target's ancestors need traverse, and the target is
        walked as a tree of its own and granted as one. Each real directory is
        walked once, which is also what stops a link cycle (audit of 7fa34b51).
        """
        real = os.path.realpath(top)
        if not os.path.isdir(real) or real in self.walked:
            return
        self.walked.add(real)
        if real != top:
            self.ancestors(real)
            if real not in self.roots:
                self.roots.append(real)
        table = _acl_tree(real, self.runner)
        for directory, subdirectories, files in os.walk(real, followlinks=False):
            self.need(directory, "rx", table)
            for name in files:
                self.need(os.path.join(directory, name), "r", table)
            for name in subdirectories:
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    self.tree(path)

    def read(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def repository(self, gitdir: str, depth: int = 0) -> None:
        """The git directory, its common directory, and every alternate object store they name."""
        self.roots.append(gitdir)
        for name in GITDIR_FILES:
            self.need(os.path.join(gitdir, name), "r")
        common = gitdir
        pointer = self.read(os.path.join(gitdir, "commondir"))
        if pointer is not None and pointer.strip():
            common = os.path.realpath(os.path.join(gitdir, pointer.strip()))
            self.ancestors(common)
            self.need(common, "rx")
            self.roots.append(common)
        for name in COMMON_FILES:
            self.need(os.path.join(common, name), "r")
        for name in COMMON_TREES:
            self.tree(os.path.join(common, name))
        self.alternates(os.path.join(common, "objects"), depth)

    def alternates(self, objects: str, depth: int) -> None:
        listed = self.read(os.path.join(objects, "info", "alternates"))
        if listed is None or depth >= ALTERNATE_DEPTH:
            return
        for line in listed.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            alternate = os.path.realpath(os.path.join(objects, line))
            if alternate in self.roots:
                continue
            self.roots.append(alternate)
            self.ancestors(alternate)
            if self.need(alternate, "rx"):
                self.tree(alternate)
                self.alternates(alternate, depth + 1)


def _gitfile_target(path: str, base: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    return os.path.realpath(os.path.join(base, first[len("gitdir:"):].strip()))


def closed_components(store: str, identity: ServiceIdentity, *,
                      runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> tuple[list[Closed], list[str], str]:
    """What the identity cannot use of everything a verifier opening the store reads.

    The layout is the verifier's own (`commit_cache.commit_repo_git_args`):
    a store holding `.git` is a working repository opened with `git -C`, and
    anything else is a git directory opened with `--git-dir`. Returned: the
    closed components, the repository trees they belong to (for the grant),
    and why git itself would refuse the repository whatever the bits say --
    `git -C` on a repository another account owns is "dubious ownership"
    unless `safe.directory` names it, and the verifier passes none.

    Asked only of a store that exists. A component the caller cannot even
    examine is closed as well -- whether the service can reach the store has
    then not been established.
    """
    resolved = os.path.realpath(store)
    walk = _Walk(identity, runner)
    walk.ancestors(resolved)
    dotgit = os.path.join(resolved, ".git")
    refused = ""
    if os.path.exists(dotgit):
        walk.need(resolved, "x")
        if os.path.isdir(dotgit):
            gitdir = os.path.realpath(dotgit)
            if gitdir != dotgit:
                walk.ancestors(gitdir)
            walk.need(gitdir, "rx")
            owned = [resolved, gitdir]
        else:
            walk.need(dotgit, "r")
            gitdir = _gitfile_target(dotgit, resolved)
            if gitdir is None:
                return walk.closed + [Closed(dotgit, "r", None, False)], walk.roots, refused
            walk.ancestors(gitdir)
            walk.need(gitdir, "rx")
            owned = [resolved, dotgit, gitdir]
        foreign = sorted({path for path in owned if os.stat(path).st_uid != identity.uid})
        if foreign:
            refused = (
                f"{store} is a working repository the verifier opens with `git -C`, and git refuses one "
                f"another account owns ({', '.join(f'{path} is {_user_name(os.stat(path).st_uid)}' for path in foreign)}) "
                "as dubious ownership unless safe.directory names it, which the verifier does not pass. "
                "No grant changes that: point commit_git_dir at a bare repository, which the verifier "
                f"opens with --git-dir, or have it owned by {identity.user}"
            )
        if os.path.isdir(dotgit):
            walk.roots.append(dotgit)
        walk.repository(gitdir)
    else:
        walk.need(resolved, "rx")
        walk.repository(resolved)
    return walk.closed, walk.roots, refused


def grant_commands(closed: Sequence[Closed], roots: Sequence[str], principal: str) -> list[str]:
    """The read/traverse grant that would open the path, for the owner to decide on.

    Read on each repository tree that holds a closed component, recursively
    and with a default entry so what git writes later stays readable;
    traverse (or read, for a gitfile) on each closed component outside them.
    Never write, and nothing on a component that is already open.
    """
    def root_of(path: str) -> str | None:
        return next((root for root in roots if path == root or path.startswith(root.rstrip("/") + "/")), None)

    commands: list[str] = []
    trees: list[str] = []
    for item in closed:
        root = root_of(item.path)
        if root is not None:
            if root not in trees:
                trees.append(root)
            continue
        perms = {"x": "--x", "r": "r--", "rx": "r-x"}[item.want]
        command = f"sudo setfacl -m {principal}:{perms} {shlex.quote(item.path)}"
        if command not in commands:
            commands.append(command)
    commands.extend(f"sudo setfacl -R -m {principal}:rX,d:{principal}:rX {shlex.quote(root)}" for root in trees)
    return commands


def linked_components(path: str, root: str) -> list[str]:
    """Each component strictly below `root` on the way to `path` that is a link, without following any.

    Root runs `install -d -o <tenant>` and `setfacl -R` on these paths, and
    both act on what a link points at: a tenant who replaces its store, or
    any other directory the phase names, with a link would have root re-own,
    re-mode or grant whatever it chose (SYRD-604).
    """
    if not _inside(root, path):
        return []
    linked: list[str] = []
    current = PurePosixPath(root)
    for part in PurePosixPath(path).relative_to(PurePosixPath(root)).parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                linked.append(str(current))
                break
        except FileNotFoundError:
            break
        except OSError:
            linked.append(str(current))
            break
    return linked


def linked_phase_paths(phase: Sequence[str], owner_home: str) -> list[str]:
    """Every link on the path to anything inside the owner home that a boundary phase's lines name."""
    found: list[str] = []
    for line in phase:
        try:
            words = shlex.split(line)
        except ValueError:
            continue
        for word in words:
            if word.startswith("/"):
                for link in linked_components(word.rstrip("/"), owner_home):
                    if link not in found:
                        found.append(link)
    return found


def phase_grants(plan: Any, store: str) -> Callable[[str], bool]:
    """Whether the boundary phase's commit-store grant reaches a (real) path.

    It grants traverse on the owner home and each directory down to an
    in-home store, and read on the store's tree -- physically, since
    `setfacl -R` does not follow a link it meets inside. Nothing when the
    plan lets it grant nothing, when the store is elsewhere, or when the way
    to the store goes through a link (which the repair refuses to touch).
    """
    owner_home = str(getattr(plan, "owner_home", "") or "")
    if (not bool(getattr(plan, "board_service_traversal", True)) or not _inside(owner_home, store)
            or linked_components(store, owner_home)):
        return lambda _path: False
    home = PurePosixPath(owner_home)
    named = PurePosixPath(store)
    above = {str(home), *(str(home / PurePosixPath(*named.relative_to(home).parts[:index]))
                          for index in range(1, len(named.relative_to(home).parts)))}
    tree = os.path.realpath(store)

    def grants(path: str) -> bool:
        return path in above or path == tree or path.startswith(tree.rstrip("/") + "/")

    return grants


def _absent(store: str) -> bool:
    """Known not to exist. A store this caller cannot even look at is not known absent."""
    try:
        os.stat(store)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _inside(root: str, candidate: str) -> bool:
    try:
        return bool(root) and PurePosixPath(candidate).is_relative_to(PurePosixPath(root)) \
            and PurePosixPath(candidate) != PurePosixPath(root)
    except ValueError:
        return False


def commit_store_problems(plan: Any, *,
                          runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> list[str]:
    """What stops the board service reading each configured commit store, read off the filesystem."""
    stores = [entry for entry in str(getattr(plan, "commit_git_dir", "") or "").split(os.pathsep) if entry.strip()]
    # Only a store that exists can be unreachable, so who the service is only
    # matters once one does.
    stores = [store for store in stores if not _absent(store)]
    if not stores:
        return []
    project = str(getattr(plan, "project", "") or "this project")
    identity, problem = service_identity(plan)
    if identity is None:
        return [OperatorDependency(
            f"whether {project}'s board can read its commit store was not checked: {problem}"
        )]
    owner_home = str(getattr(plan, "owner_home", "") or "")
    group = str(getattr(plan, "roles_group", "") or "")
    principal = f"g:{group}" if group else f"u:{identity.user}"
    problems: list[str] = []
    for store in stores:
        closed, roots, refused = closed_components(store, identity, runner=runner)
        if not closed and not refused:
            continue
        # Judged by where each closed component really is, not by where the
        # store is named: an in-home store whose objects are a link out of the
        # home, or whose gitfile, commondir or alternate points out of it,
        # needs a grant the boundary phase never makes (audit of edbbb456).
        grants = phase_grants(plan, store)
        outside = [item for item in closed if not grants(item.path)]
        shown = outside[0] if outside else None if refused else closed[0]
        reason = (
            f"{identity.describe()}, the account {project}'s board verifies commits as, cannot read "
            f"its commit store {store}: {_describe(shown) if shown else refused}"
        )
        if not outside and not refused:
            problems.append(reason)
            continue
        first = shown.path if shown else store
        owner = _user_name(shown.status.st_uid) if shown and shown.status is not None else "its owner"
        why = (
            "this plan has board_service_traversal=false, so the boundary phase grants it nothing"
            if not bool(getattr(plan, "board_service_traversal", True))
            else f"it is outside {owner_home}, so the boundary phase does not grant it and Switchyard will "
            "not change a path another account owns"
            if not _inside(owner_home, store)
            else f"{first} is not something the boundary phase grants -- it grants only {owner_home}, the "
            f"directories down to {store}, and that store's own tree, never a link out of it -- and "
            "Switchyard will not change a path it does not grant"
            if outside
            else "the boundary phase only grants read, and that is not what stops it"
        )
        if outside and refused:
            why += f". Separately, {refused}"
        commands = grant_commands(outside, roots, principal) if all(c.status is not None for c in outside) else []
        remedy = (
            f" If {owner}, or root for them, agrees to it, the read/traverse-only grant is: "
            + "; ".join(f"`{command}`" for command in commands) + "."
            if commands
            else ""
        )
        problems.append(OperatorDependency(
            f"{reason} -- {why}. repair-boundary cannot close this; it needs an operator decision about "
            f"{first}.{remedy} Then run `pkexec switchyard repair-boundary {project}` to confirm."
        ))
    return problems
