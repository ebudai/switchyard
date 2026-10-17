"""Who is on the other end of a board-page connection, by the kernel's account (SYRD-598).

A board page reaches the board over loopback TCP, and so does every other local process: the write
token is handed to anyone who asks, and Host, Origin and Sec-Fetch-Site are whatever the client sends.
None of that says a person is at the page. What a client cannot choose is the account that owns its
end of the connection. The kernel records it for every socket in /proc/net/tcp(6), and the board
shares the host's network namespace, so it can read who connected.

The board UI may lend a Director operation only to a connection owned by an approved board UI user:
the human's own desktop account, which root provisioning resolved to a numeric uid and recorded in the
project's root-owned record (/etc/switchyard/projects/<slug>.json, `board_ui`). The board learns where
that record is from its unit's command line (--board-ui-record), which root renders: not from its
environment, which the tenant's EnvironmentFile can override. Nothing the tenant can write decides it.
Root, the board service, the tenant's project account and its role accounts are never board UI users --
the record names the last two, and the environment may only add to them -- and a record naming one is
refused whole, so a stale or malformed record turns delegation off rather than widening it.
"""

from __future__ import annotations

import grp
import ipaddress
import json
import os
import pwd
import socket
import stat
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

RECORD_KEY = "board_ui"
#: The project's root-owned record, from the unit's --board-ui-record; None when the board was not given one.
RECORD_PATH: Path | None = None
#: Who may own the record and every directory above it: root.
TRUSTED_OWNER_UIDS = frozenset({0})
PROC_TABLES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
OPERATOR_REMEDY = ("an operator must configure an approved board UI user (switchyard upgrade records the desktop "
                   "owner; a headless host names one with --board-ui-user)")
#: An account's uid by name. A module attribute so a test can name accounts that exist only in its namespace.
account_uid: Callable[[str], int] = lambda name: pwd.getpwnam(name).pw_uid


class UiUsers(NamedTuple):
    uids: frozenset[int]
    problem: str  # why the board UI lends nothing, or ''


def _hex_endpoint(host: str, port: int) -> set[str]:
    """An endpoint as /proc/net/tcp(6) prints it: 32-bit words in host order, then the port."""
    packed = socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
    words = "".join(packed[i:i + 4][::-1].hex().upper() for i in range(0, len(packed), 4))
    forms = {f"{words}:{port:04X}"}
    if len(packed) == 16 and packed[:12] == b"\0" * 10 + b"\xff\xff":  # ::ffff:a.b.c.d, as tcp lists it
        forms.add(f"{packed[12:][::-1].hex().upper()}:{port:04X}")
    return forms


def _loopback(host: str) -> bool:
    address = ipaddress.ip_address(host)
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def tcp_peer_uid(connection: Any, tables: tuple[Path, ...] = PROC_TABLES) -> int | None:
    """The uid that owns the client's end of this loopback connection. None -- refuse -- when the client is
    not on loopback, the kernel's table cannot be read, or it does not name exactly one owner."""
    try:
        server, client = connection.getsockname(), connection.getpeername()
        if not (_loopback(client[0]) and _loopback(server[0])):
            return None
        want_local, want_remote = _hex_endpoint(client[0], client[1]), _hex_endpoint(server[0], server[1])
    except (OSError, ValueError, IndexError, TypeError):
        return None
    owners: set[int] = set()
    readable = False
    for table in tables:
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for line in lines:
            fields = line.split()
            if len(fields) > 7 and fields[1] in want_local and fields[2] in want_remote:
                owners.add(int(fields[7]))
    return owners.pop() if readable and len(owners) == 1 else None


def _untrusted(path: Path) -> str:
    """Why `path` is not root's alone, or '': it and every directory above it, owner, mode and no symlink."""
    for part in [path, *path.parents]:
        try:
            info = os.lstat(part)
        except OSError as exc:
            return f"{part} cannot be read ({exc.strerror})"
        if stat.S_ISLNK(info.st_mode):
            return f"{part} is a symbolic link"
        if info.st_uid not in TRUSTED_OWNER_UIDS:
            return f"{part} is not owned by root"
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX):
            return f"{part} is writable by others"
    return ""


def excluded_uids(record: Mapping[str, Any], environ: Mapping[str, str]) -> dict[int, str]:
    """Accounts that are never a board UI user, and why: root, the board service, and the tenant's project
    account and role accounts as root recorded them. The environment may add to these, never remove."""
    excluded = {0: "root", os.geteuid(): "the board service"}

    def add(name: object, why: str) -> None:
        try:
            excluded.setdefault(account_uid(str(name).strip()), why)
        except (KeyError, ValueError):
            pass

    def add_group(name: object) -> None:
        try:
            for member in grp.getgrnam(str(name).strip()).gr_mem:
                add(member, "a role account")
        except KeyError:
            pass

    if record.get("service_user"):
        add(record["service_user"], "the board service")
    for name in [record.get("tenant_user"), environ.get("TICKET_BOARD_TENANT_USER")]:
        if name:
            add(name, "the project account")
    for name in list(record.get("role_accounts") or []) + [
            chunk.partition("=")[2] for chunk in str(environ.get("TICKET_BOARD_ROLE_ACCOUNTS") or "").replace(",", " ").split()]:
        if name:
            add(name, "a role account")
    for name in [record.get("roles_group"), environ.get("TICKET_BOARD_SOCKET_GROUP")]:
        if name:
            add_group(name)
    return excluded


def configured(environ: Mapping[str, str] | None = None) -> UiUsers:
    """The approved board UI users from the project's root-owned record, or why there are none."""
    source = os.environ if environ is None else environ
    path = RECORD_PATH
    if path is None:
        return UiUsers(frozenset(), f"this board was started without its project record (--board-ui-record); "
                                    f"{OPERATOR_REMEDY}")
    why = _untrusted(path)
    if why:
        return UiUsers(frozenset(), f"the project record is not trusted: {why}; {OPERATOR_REMEDY}")
    try:
        record = json.loads(path.read_text(encoding="utf-8")).get(RECORD_KEY) or {}
        entries = record.get("users") or []
    except (OSError, ValueError, AttributeError) as exc:
        return UiUsers(frozenset(), f"the project record cannot be read ({exc}); {OPERATOR_REMEDY}")
    if not isinstance(entries, list) or not entries:
        return UiUsers(frozenset(), f"no approved board UI user is recorded in {path}; {OPERATOR_REMEDY}")
    excluded = excluded_uids(record, source)
    uids: set[int] = set()
    for entry in entries:
        account = entry.get("account") if isinstance(entry, dict) else None
        uid = entry.get("uid") if isinstance(entry, dict) else None
        if not isinstance(account, str) or type(uid) is not int:
            return UiUsers(frozenset(), f"the project record's {RECORD_KEY} is malformed; {OPERATOR_REMEDY}")
        try:
            current = account_uid(account)
        except KeyError:
            current = None
        if current != uid:
            return UiUsers(frozenset(), f"the recorded board UI user {account} (uid {uid}) no longer exists as recorded; "
                                        f"{OPERATOR_REMEDY}")
        if uid in excluded:
            return UiUsers(frozenset(), f"the project record names {account} (uid {uid}), which is {excluded[uid]} and "
                                        f"never a board UI user; board UI delegation is off until it is corrected: "
                                        f"{OPERATOR_REMEDY}")
        uids.add(uid)
    return UiUsers(frozenset(uids), "")


def _account(uid: int | None) -> str:
    if uid is None:
        return "an account the kernel did not name uniquely"
    try:
        return f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    except KeyError:
        return f"uid {uid}"


def refusal(connection: Any, environ: Mapping[str, str] | None = None) -> str:
    """Why this connection may not use the board UI's Director operations, or '' when it may."""
    users = configured(environ)
    if users.problem:
        return users.problem
    uid = tcp_peer_uid(connection)
    if uid is not None and uid in users.uids:
        return ""
    return (f"this connection belongs to {_account(uid)}, not an approved board UI user; the board UI lends "
            f"Director operations only to {', '.join(_account(u) for u in sorted(users.uids))}")
