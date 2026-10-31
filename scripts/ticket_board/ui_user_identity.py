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

The User's phone reaches the board through this host's Tailscale Serve (SYRD-718), which proxies to the board over
loopback as root: the kernel names root, not the User's account. What tailscaled adds is the requester's tailnet
login (Tailscale-User-Login), and only tailscaled can add it: it strips any the client sent, sets it only for an
untagged tailnet peer -- never for Funnel, which it marks, nor for the host's own traffic -- and no account but root
can own its connection. So the board believes that header only on a connection the kernel says root owns, for a page
named by its tailnet name (`*.ts.net`) or tailnet address over HTTPS -- never a name a rebinding site could give it --
and only for a login root recorded beside an approved board UI user. That is the User,
for the User's own actions; the board UI's Director operations stay the desktop account's alone.
"""

from __future__ import annotations

import grp
import ipaddress
import json
import os
import pwd
import re
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
#: Who owns Tailscale Serve's connection to the board: tailscaled runs as root. Only a test, standing in its own
#: proxy for tailscaled, sets this.
TAILNET_PROXY_UIDS = frozenset({0})
#: What a proxy in front of the board adds. A request carrying any came through one, or says it did.
PROXY_HEADERS = ("Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-User-Profile-Pic", "Tailscale-Funnel-Request",
                 "Tailscale-Headers-Info", "Tailscale-App-Capabilities", "X-Forwarded-For", "X-Forwarded-Host",
                 "X-Forwarded-Proto", "Forwarded")
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
#: A tailnet login as root records it: one word with an @, as Tailscale names its users (eric@example.com,
#: eric@github); tailscaled sends such a login as it is.
TAILNET_LOGIN = re.compile(r"[!-?A-~]+@[!-?A-~]+")
TAILNET_NAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net")
TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class UiUsers(NamedTuple):
    uids: frozenset[int]
    problem: str  # why the board UI lends nothing, or ''
    tailnet_logins: frozenset[str] = frozenset()  # casefolded, each recorded beside an approved user (SYRD-718)


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
    logins: set[str] = set()
    for entry in entries:
        account = entry.get("account") if isinstance(entry, dict) else None
        uid = entry.get("uid") if isinstance(entry, dict) else None
        login = entry.get("tailnet_login", "") if isinstance(entry, dict) else None
        if not isinstance(account, str) or type(uid) is not int or not isinstance(login, str) \
                or (login and not TAILNET_LOGIN.fullmatch(login)):
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
        if login:
            logins.add(login.casefold())
    return UiUsers(frozenset(uids), "", frozenset(logins))


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


def _host_name(host: str) -> str:
    """A Host header's name, without its port."""
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def proxied(headers: Any) -> bool:
    """Whether a request came through a proxy in front of the board, or says it did: it carries a proxy's header,
    or names the board by anything but a loopback name. A browser on this host never does either."""
    host = (headers.get("Host") or "").strip().lower()
    return any(headers.get(name) is not None for name in PROXY_HEADERS) or bool(host and _host_name(host) not in LOOPBACK_NAMES)


def _tailnet(address: str) -> bool:
    try:
        return any(ipaddress.ip_address(address) in network for network in TAILNET_NETWORKS)
    except ValueError:
        return False


def _tailnet_host(host: str) -> bool:
    """Whether a Host header names this board's page by its tailnet name or tailnet address, with any port."""
    name = _host_name(host)
    return bool(TAILNET_NAME.fullmatch(name)) or _tailnet(name[1:-1] if name.startswith("[") else name)


def tailnet_refusal(connection: Any, headers: Any, environ: Mapping[str, str] | None = None) -> str:
    """Why this request is not the approved User's through this host's Tailscale Serve, or '' when it is (SYRD-718)."""
    uid = tcp_peer_uid(connection)
    if uid is None or uid not in TAILNET_PROXY_UIDS:
        return (f"this connection belongs to {_account(uid)}, not this host's Tailscale Serve; a proxy's headers "
                "count only from tailscaled")
    if headers.get("Tailscale-Funnel-Request") is not None:
        return "a request through Tailscale Funnel is the internet's, never the User's"
    if not _tailnet_host((headers.get("Host") or "").strip().lower()) \
            or (headers.get("X-Forwarded-Proto") or "").strip().lower() != "https":
        return "only the board's own page, by its tailnet name (*.ts.net) or address over HTTPS, is the User's over the tailnet"
    login = (headers.get("Tailscale-User-Login") or "").strip()
    if not login:
        return "Tailscale named no tailnet user for this request (a tagged device, or the host's own traffic)"
    users = configured(environ)
    if users.problem:
        return users.problem
    if login.casefold() not in users.tailnet_logins:
        return (f"the tailnet user {login} is not one root recorded for an approved board UI user "
                "(switchyard upgrade --board-ui-user ACCOUNT=LOGIN)")
    return ""
