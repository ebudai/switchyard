"""Local peer identity and role authority for the board's Unix socket.

The kernel-supplied SO_PEERCRED identity, the tables and registered pane
sessions that turn it into a board role, and the socket's tenant-only
ownership. TicketBoardServer and its handler decide when these checks run.
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from .peer_identity import SessionIdentity, session_identity, session_is_live

if TYPE_CHECKING:
    from .app import TicketBoardApp

# Records keep the server's logger name, which is where operators and tests
# have always found these refusals.
LOGGER = logging.getLogger(__name__.rpartition(".")[0] + ".server")
SO_PEERCRED_FORMAT = "3i"
# 0666 let every local user, including other tenants' Unix accounts, connect to
# this tenant's board. Group access is granted by the runtime directory instead,
# which the unit chgrps to the tenant group (SYRD-39).
PANE_SOCKET_MODE = 0o660


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True)
class RoleAccount:
    """A configured role and the Unix account that runs it."""

    role: str
    account: str
    uid: int


class CallerIdentityError(PermissionError):
    """The peer's Unix identity does not entitle it to the role it used."""


class ProcessRoleAuthority:
    """Resolve a shared project account to its registered live pane process.

    The shared uid is only the tenant boundary. Role authority comes from the
    `(pid, start_time, uid)` row PostgreSQL recorded atomically with that role's
    runtime and target. A sibling process under the project account therefore
    has no role merely because it can name one in a request.
    """

    def __init__(
        self,
        app: TicketBoardApp,
        project_account: str,
        *,
        resolve_uid: Callable[[str], int | None] | None = None,
        resolve_session: Callable[[int], SessionIdentity | None] = session_identity,
        session_live: Callable[[SessionIdentity], bool] = session_is_live,
    ) -> None:
        self.app = app
        self.project_account = project_account.strip()
        resolver = resolve_uid or LocalRoleAuthority._default_resolve_uid
        self.project_uid = resolver(self.project_account) if self.project_account else None
        self.resolve_session = resolve_session
        self.session_live = session_live
        if self.project_uid is None:
            raise RuntimeError(f"project account {self.project_account!r} does not exist")

    def session_for_peer(self, credentials: PeerCredentials) -> SessionIdentity:
        if credentials.uid != self.project_uid:
            raise CallerIdentityError("local board writes must come from this project's account")
        identity = self.resolve_session(credentials.pid)
        if identity is None or not self.session_live(identity):
            raise CallerIdentityError("local board writes must come from a live launcher pane")
        return identity

    def role_for_peer(self, credentials: PeerCredentials) -> str:
        identity = self.session_for_peer(credentials)
        assignment = self.app.runtime_assignment_for_process(
            identity.pid, identity.start_time, credentials.uid
        )
        if assignment is None:
            raise CallerIdentityError("this live pane has no PostgreSQL role assignment")
        return str(assignment["role"])

    def role_for_uid(self, uid: int) -> str:
        raise CallerIdentityError("a shared project uid does not identify a role")

    def uids(self) -> set[int]:
        """The tenant boundary this authority admits: the project account only.

        require_allowed_peer() unions this into the allowed set before any role
        is resolved, so every authority the socket can be built with has to
        answer it. A process-authority tenant answered it with nothing at all,
        and each runtime registration died on the AttributeError before
        session_for_peer() could refuse anything -- so the board rejected every
        pane, including the ones it would have authorized.

        Admitting exactly the resolved project uid decides nothing later. The
        shared uid was never role authority: the pane still has to be the live
        session behind that pid, and still has to match a PostgreSQL runtime
        assignment, before it is given a role.
        """
        return set() if self.project_uid is None else {self.project_uid}


class LocalRoleAuthority:
    """Maps a local peer's Unix uid to its board role.

    The uid comes from SO_PEERCRED. The kernel sets it and an unprivileged
    process cannot change it, so unlike a role name on the wire, a process
    label, an environment variable or an argv string, it is not something the
    caller can choose. The uid-to-role table comes from this board's own unit
    configuration, which the role accounts do not own and cannot write.

    That combination is what makes the mapping authoritative, and it is why
    there is no registration step here at all. Nothing is remembered between
    requests, so nothing is lost when the board restarts: every request is
    resolved from the kernel-supplied uid and the configured table (SYRD-39).

    It is only as strong as the deployment giving each role its own account. A
    project whose roles share one account cannot be told apart here, and the
    board says so at startup rather than implying a separation it cannot make.
    """

    def __init__(
        self,
        role_accounts: Mapping[str, str] | None = None,
        *,
        resolve_uid: Callable[[str], int | None] | None = None,
    ) -> None:
        self._resolve_uid = resolve_uid or self._default_resolve_uid
        self._by_uid: dict[int, RoleAccount] = {}
        self._by_role: dict[str, RoleAccount] = {}
        self._unresolved: dict[str, str] = {}
        self._shared_accounts: dict[str, list[str]] = {}
        self._ambiguous_uids: set[int] = set()
        for role, account in (role_accounts or {}).items():
            self._add(role.strip().lower(), str(account).strip())

    @staticmethod
    def _default_resolve_uid(account: str) -> int | None:
        try:
            return pwd.getpwnam(account).pw_uid
        except KeyError:
            return None

    def _add(self, role: str, account: str) -> None:
        if not role or not account:
            return
        uid = self._resolve_uid(account)
        if uid is None:
            # Fails closed: an unresolved account grants nothing, and the peer
            # running as it will simply have no role.
            self._unresolved[role] = account
            LOGGER.warning(
                "Role %s is configured to run as %s, which is not a local account; "
                "that role cannot be identified on the local socket",
                role,
                account,
            )
            return
        if uid in self._ambiguous_uids:
            self._shared_accounts.setdefault(account, []).append(role)
            self._by_role.pop(role, None)
            return
        existing = self._by_uid.get(uid)
        if existing is not None and existing.role != role:
            # Two roles on one account cannot be told apart by uid. Refuse both
            # rather than resolving to whichever was configured first: a
            # half-migrated tenant must fail closed, not silently hand one role
            # the other's authority.
            self._ambiguous_uids.add(uid)
            self._by_uid.pop(uid, None)
            self._by_role.pop(existing.role, None)
            self._by_role.pop(role, None)
            self._shared_accounts.setdefault(account, [existing.role]).append(role)
            LOGGER.error(
                "Roles %s and %s both run as account %s; neither can be authorized on the "
                "local socket until each role has its own Unix account",
                existing.role,
                role,
                account,
            )
            return
        entry = RoleAccount(role=role, account=account, uid=uid)
        self._by_uid[uid] = entry
        self._by_role[role] = entry

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        resolve_uid: Callable[[str], int | None] | None = None,
    ) -> "LocalRoleAuthority":
        """Build from TICKET_BOARD_ROLE_ACCOUNTS, e.g. "director=syrd-director,main=syrd-main".

        Role names are data, never hardcoded here: whatever the project declares
        is what the board honours.
        """
        source = os.environ if environ is None else environ
        raw = str(source.get("TICKET_BOARD_ROLE_ACCOUNTS") or "").strip()
        mapping: dict[str, str] = {}
        for chunk in raw.replace(",", " ").split():
            role, sep, account = chunk.partition("=")
            if not sep:
                LOGGER.warning("Ignoring malformed TICKET_BOARD_ROLE_ACCOUNTS entry %r", chunk)
                continue
            mapping[role.strip().lower()] = account.strip()
        authority = cls(mapping, resolve_uid=resolve_uid)
        if not mapping:
            LOGGER.error(
                "TICKET_BOARD_ROLE_ACCOUNTS is not configured; local socket writes cannot "
                "identify a role and will be refused"
            )
        return authority

    def role_for_uid(self, uid: int) -> str:
        entry = self._by_uid.get(uid)
        if entry is None:
            LOGGER.warning(
                "Refused local board caller uid=%s: not a configured role account", uid
            )
            raise CallerIdentityError(
                "this Unix account is not a configured role for this project"
            )
        return entry.role

    def uid_for_role(self, role: str) -> int | None:
        entry = self._by_role.get(role.strip().lower())
        return entry.uid if entry is not None else None

    def account_for_role(self, role: str) -> str | None:
        entry = self._by_role.get(role.strip().lower())
        return entry.account if entry is not None else None

    def roles(self) -> list[str]:
        return sorted(self._by_role)

    def uids(self) -> set[int]:
        """Every uid this table resolves to a role."""
        return set(self._by_uid)

    def shared_accounts(self) -> dict[str, list[str]]:
        """Accounts backing more than one role; those roles are unauthorizable."""
        return {account: sorted(roles) for account, roles in self._shared_accounts.items()}

    def unresolved_roles(self) -> dict[str, str]:
        return dict(self._unresolved)


def peer_credentials(connection: socket.socket) -> PeerCredentials:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(SO_PEERCRED_FORMAT))
    pid, uid, gid = struct.unpack(SO_PEERCRED_FORMAT, raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def restrict_socket_to_tenant(socket_path: Path, *, environ: Mapping[str, str] | None = None) -> None:
    """Hand socket access to the tenant group and no one else.

    The socket used to be 0666 inside a world-traversable directory, so any
    local account -- including another tenant's -- could connect and claim a
    role. Ownership stays with the board service; the tenant reaches it through
    a group the unit grants as a supplementary group, which is why this needs no
    privilege at runtime. Left permissive and logged, never silently, when the
    group is unset or unknown, so a half-configured deployment is visible rather
    than quietly unprotected (SYRD-39).
    """
    source = os.environ if environ is None else environ
    group_name = str(source.get("TICKET_BOARD_SOCKET_GROUP") or "").strip()
    if not group_name:
        LOGGER.warning(
            "TICKET_BOARD_SOCKET_GROUP is not set for %s; the socket is reachable by any "
            "local account that can traverse its directory",
            socket_path,
        )
        return
    try:
        gid = grp.getgrnam(group_name).gr_gid
    except KeyError:
        LOGGER.warning(
            "TICKET_BOARD_SOCKET_GROUP=%s does not resolve to a local group; leaving %s as is",
            group_name,
            socket_path,
        )
        return
    # Roles write pane activity here and the owner's notify listener reads it.
    # Per-role /run/user directories are writable by the role and unreadable by
    # the listener, so state written there is never seen (SYRD-39).
    pane_state_dir = socket_path.parent / "pane-state"
    try:
        pane_state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("Could not create shared pane state directory %s: %s", pane_state_dir, exc)
    for target, mode in (
        (socket_path.parent, 0o750),
        (socket_path, PANE_SOCKET_MODE),
        (pane_state_dir, 0o770),
    ):
        if not target.exists():
            continue
        try:
            os.chown(target, -1, gid)
            target.chmod(mode)
        except OSError as exc:
            LOGGER.warning(
                "Could not restrict %s to group %s (%s); the board service must be a member "
                "of that group",
                target,
                group_name,
                exc,
            )


def allowed_peer_uids(environ: Mapping[str, str] | None = None) -> set[int]:
    """Unix uids permitted on this tenant's local socket.

    Empty means unrestricted, which is what an older deployment that has not
    been through the socket repair still gets; the caller logs that state rather
    than pretending the check is active. Configured deployments name the tenant
    owner, so another tenant's Unix account is refused even if socket
    permissions were loosened by hand (SYRD-39).
    """
    source = os.environ if environ is None else environ
    uids: set[int] = set()
    raw_uids = str(source.get("TICKET_BOARD_ALLOWED_PEER_UIDS") or "").strip()
    for chunk in raw_uids.replace(",", " ").split():
        try:
            uids.add(int(chunk))
        except ValueError:
            LOGGER.warning("Ignoring non-numeric TICKET_BOARD_ALLOWED_PEER_UIDS entry %r", chunk)
    tenant_user = str(source.get("TICKET_BOARD_TENANT_USER") or "").strip()
    if tenant_user:
        try:
            uids.add(pwd.getpwnam(tenant_user).pw_uid)
        except KeyError:
            LOGGER.warning(
                "TICKET_BOARD_TENANT_USER=%s does not resolve to a local account; "
                "peer uid enforcement will not admit it",
                tenant_user,
            )
    if uids:
        # The board's own account always reaches its socket, so health checks and
        # in-process helpers do not depend on the tenant list being complete.
        uids.add(os.getuid())
    return uids
