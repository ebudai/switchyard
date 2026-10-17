"""Whether the board service can actually read the commit store it is configured with.

The board verifies every commit hash against `TICKET_BOARD_COMMIT_GIT_DIR`, as
the board service's account. The boundary phase grants that read for a store
inside the tenant owner's home, and grants nothing for a store anywhere else:
a path another account owns is not ours to re-mode or traverse-grant
(SYRD-157). So a store elsewhere was never checked by anybody, and
`repair-boundary` called a boundary closed while the board could not open
`/data/git` -- `sapi-agent:sapi-agent 0750` -- on the way to MEFP's store
(SYRD-604).

This reads the answer from the filesystem: every component of the path, and
the entries git opens at the top of the repository, are judged from their mode
bits and POSIX ACLs against the identity the board unit runs with -- the
service user, and the unit's `Group=`/`SupplementaryGroups=`. Nothing is tried
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

#: The entries git opens at the top of a repository before it can resolve a
#: commit. Checked only when present: a store not yet cloned is not an access
#: objection, and git does without `packed-refs`.
GIT_ENTRIES: tuple[tuple[str, str], ...] = (
    ("HEAD", "r"),
    ("config", "r"),
    ("packed-refs", "r"),
    ("objects", "rx"),
    ("refs", "rx"),
)


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


def closed_components(store: str, identity: ServiceIdentity, *,
                      runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> list[Closed]:
    """Every component between `/` and the store, and the store's git entries, the identity cannot use.

    Asked only of a store that exists. A component the caller cannot even
    examine is closed as well -- whether the service can reach the store has
    then not been established.
    """
    resolved = PurePosixPath(os.path.realpath(store))
    targets: list[tuple[str, str]] = [
        (str(PurePosixPath(*resolved.parts[: index + 1])), "x") for index in range(len(resolved.parts) - 1)
    ]
    targets.append((str(resolved), "rx"))
    closed: list[Closed] = []
    for path, want in targets:
        try:
            status = os.lstat(path)
        except OSError:
            closed.append(Closed(path, want, None, False))
            return closed
        entries = _acl(path, runner)
        if not _permits(status, entries, identity, want):
            closed.append(Closed(path, want, status, entries is not None and len(entries) > 3))
    for name, want in GIT_ENTRIES:
        path = str(resolved / name)
        try:
            status = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            closed.append(Closed(path, want, None, False))
            continue
        if not stat.S_ISDIR(status.st_mode):
            want = want.replace("x", "")
        entries = _acl(path, runner)
        if not _permits(status, entries, identity, want):
            closed.append(Closed(path, want, status, entries is not None and len(entries) > 3))
    return closed


def grant_commands(store: str, closed: Sequence[Closed], principal: str) -> list[str]:
    """The read/traverse grant that would open the path, for the owner to decide on.

    Traverse on each closed directory above the store; read on the store and
    everything in it, with a default entry so what git writes later stays
    readable. Never write, and nothing on a component that is already open.
    """
    resolved = os.path.realpath(store)
    commands: list[str] = []
    for item in closed:
        if item.path != resolved and not item.path.startswith(resolved + "/"):
            commands.append(f"sudo setfacl -m {principal}:--x {shlex.quote(item.path)}")
    if any(item.path == resolved or item.path.startswith(resolved + "/") for item in closed):
        commands.append(f"sudo setfacl -R -m {principal}:rX,d:{principal}:rX {shlex.quote(resolved)}")
    return commands


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
    granted = bool(getattr(plan, "board_service_traversal", True))
    group = str(getattr(plan, "roles_group", "") or "")
    principal = f"g:{group}" if group else f"u:{identity.user}"
    problems: list[str] = []
    for store in stores:
        closed = closed_components(store, identity, runner=runner)
        if not closed:
            continue
        reason = (
            f"{identity.describe()}, the account {project}'s board verifies commits as, cannot read "
            f"its commit store {store}: {_describe(closed[0])}"
        )
        if granted and _inside(owner_home, store):
            problems.append(reason)
            continue
        first = closed[0].path
        owner = _user_name(closed[0].status.st_uid) if closed[0].status is not None else "its owner"
        why = (
            f"it is outside {owner_home}, so the boundary phase does not grant it and Switchyard will "
            "not change a path another account owns"
            if not _inside(owner_home, store)
            else "this plan has board_service_traversal=false, so the boundary phase grants it nothing"
        )
        commands = grant_commands(store, closed, principal) if all(c.status is not None for c in closed) else []
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
