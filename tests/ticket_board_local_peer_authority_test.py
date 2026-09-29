#!/usr/bin/env python3
"""SYRD-503: local peer identity and role authority, with temporary paths and fake accounts only."""

from __future__ import annotations

import ast
import grp
import logging
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import local_peer_authority as owner  # noqa: E402
from scripts.ticket_board import peer_identity  # noqa: E402
from scripts.ticket_board import server  # noqa: E402

PUBLIC = (
    "PANE_SOCKET_MODE", "SO_PEERCRED_FORMAT", "CallerIdentityError", "LocalRoleAuthority", "PeerCredentials",
    "ProcessRoleAuthority", "RoleAccount", "allowed_peer_uids", "peer_credentials", "restrict_socket_to_tenant",
)
UIDS = {"syrd-director": 4101, "syrd-main": 4102, "syrd-ops": 4103, "shared": 4200}
ME = pwd.getpwuid(os.getuid()).pw_name
MY_GROUP = grp.getgrgid(os.getgid()).gr_name


class Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.name, record.levelname, record.getMessage()))

    def __enter__(self) -> Logs:
        logger = logging.getLogger("scripts.ticket_board.server")
        logger.addHandler(self)
        self.old_level = logger.level
        logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_args: Any) -> None:
        logger = logging.getLogger("scripts.ticket_board.server")
        logger.removeHandler(self)
        logger.setLevel(self.old_level)

    def messages(self) -> list[str]:
        return [message for _, _, message in self.records]


def refusal(operation: Any, kind: type[BaseException] = owner.CallerIdentityError) -> str:
    try:
        operation()
    except kind as exc:
        return str(exc)
    raise AssertionError(f"expected {kind.__name__}")


def test_server_keeps_public_names_and_logger() -> None:
    for name in PUBLIC:
        assert getattr(server, name) is getattr(owner, name), name
    assert owner.LOGGER is server.LOGGER and owner.LOGGER.name == "scripts.ticket_board.server"
    assert issubclass(owner.CallerIdentityError, PermissionError)
    assert (owner.SO_PEERCRED_FORMAT, owner.PANE_SOCKET_MODE) == ("3i", 0o660)
    tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    runtime = [
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module not in (None, "__future__")
    ]
    assert runtime == ["dataclasses", "pathlib", "typing", "peer_identity"], runtime
    guarded = [node for node in tree.body if isinstance(node, ast.If)]
    assert len(guarded) == 1 and ast.unparse(guarded[0].test) == "TYPE_CHECKING", ast.dump(guarded[0])
    # An installed release imports the package as `ticket_board`, not `scripts.ticket_board`.
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'scripts'); import ticket_board.server as s, ticket_board.local_peer_authority as a;"
         " print(s.LOGGER.name, a.LOGGER.name, s.CallerIdentityError is a.CallerIdentityError)"],
        cwd=ROOT, capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert child.stdout.split() == ["ticket_board.server", "ticket_board.server", "True"], child


def test_account_table_fails_closed() -> None:
    fake = UIDS.get
    with Logs() as logs:
        table = owner.LocalRoleAuthority.from_environ(
            {"TICKET_BOARD_ROLE_ACCOUNTS": " Director=syrd-director,main=syrd-main  ops=shared app=shared qa=ghost junk"},
            resolve_uid=fake,
        )
    assert table.roles() == ["director", "main"] and table.uids() == {4101, 4102}
    assert table.role_for_uid(4101) == "director" and table.uid_for_role(" DIRECTOR ") == 4101
    assert table.account_for_role("Main") == "syrd-main" and table.account_for_role("ops") is None
    assert table.shared_accounts() == {"shared": ["app", "ops"]} and table.unresolved_roles() == {"qa": "ghost"}
    assert any("Ignoring malformed TICKET_BOARD_ROLE_ACCOUNTS entry 'junk'" == m for m in logs.messages()), logs.records
    assert any("qa is configured to run as ghost" in m for m in logs.messages())
    assert {name for name, _, _ in logs.records} == {"scripts.ticket_board.server"}
    with Logs() as logs:
        shared = owner.LocalRoleAuthority({"director": "shared", "ops": "shared", "main": "shared"}, resolve_uid=fake)
        text = refusal(lambda: shared.role_for_uid(4200))
    assert text == "this Unix account is not a configured role for this project"
    assert shared.shared_accounts() == {"shared": ["director", "main", "ops"]} and shared.roles() == []
    assert any("each role has its own Unix account" in m for m in logs.messages())
    assert any("not a configured role account" in m for m in logs.messages())
    with Logs() as logs:
        empty = owner.LocalRoleAuthority.from_environ({}, resolve_uid=fake)
    assert empty.roles() == [] and any("TICKET_BOARD_ROLE_ACCOUNTS is not configured" in m for m in logs.messages())
    real = owner.LocalRoleAuthority({"main": ME, "ops": "no-such-user-syrd503"})
    assert real.uid_for_role("main") == os.getuid() and real.unresolved_roles() == {"ops": "no-such-user-syrd503"}


class App:
    def __init__(self) -> None:
        self.asked: list[tuple[int, int, int]] = []

    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int) -> dict[str, str] | None:
        self.asked.append((pid, start_time, uid))
        return {"role": "main"} if (pid, start_time) == (11, 5) else None


def test_process_authority_binds_role_to_the_live_registered_pane() -> None:
    assert refusal(lambda: owner.ProcessRoleAuthority(App(), "ghost", resolve_uid=lambda _a: None), RuntimeError) == (
        "project account 'ghost' does not exist"
    )
    assert refusal(lambda: owner.ProcessRoleAuthority(App(), "  ", resolve_uid=lambda _a: 1), RuntimeError) == (
        "project account '' does not exist"
    )
    default = owner.ProcessRoleAuthority(App(), " proj ", resolve_uid={"proj": 4300}.get)
    assert default.resolve_session is peer_identity.session_identity and default.session_live is peer_identity.session_is_live
    sessions = {pid: types.SimpleNamespace(pid=pid, start_time=start) for pid, start in ((11, 5), (12, 6), (13, 7))}
    app = App()
    authority = owner.ProcessRoleAuthority(
        app, "proj", resolve_uid={"proj": 4300}.get, resolve_session=sessions.get, session_live=lambda s: s.pid != 13,
    )
    C = owner.PeerCredentials
    for cred, text in [
        (C(11, 4301, 1), "local board writes must come from this project's account"),
        (C(99, 4300, 1), "local board writes must come from a live launcher pane"),
        (C(13, 4300, 1), "local board writes must come from a live launcher pane"),
        (C(12, 4300, 1), "this live pane has no PostgreSQL role assignment"),
    ]:
        assert refusal(lambda: authority.role_for_peer(cred)) == text, cred
    assert app.asked == [(12, 6, 4300)], app.asked
    assert authority.role_for_peer(C(11, 4300, 1)) == "main"
    assert refusal(lambda: authority.role_for_uid(4300)) == "a shared project uid does not identify a role"
    assert authority.uids() == {4300}


def test_peer_credentials_come_from_the_kernel() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert owner.peer_credentials(left) == owner.PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())
    finally:
        left.close()
        right.close()


def modes(run: Path) -> dict[str, str]:
    return {path.name: oct(path.stat().st_mode & 0o7777) for path in sorted([run, *run.iterdir()])}


def socket_dir(tmp: str, with_socket: bool = True) -> tuple[Path, Path]:
    run = Path(tmp) / "run"
    run.mkdir(mode=0o755)
    run.chmod(0o755)
    sock = run / "ticket-board.sock"
    if with_socket:
        sock.write_text("")
        sock.chmod(0o666)
    return run, sock


def test_socket_is_handed_to_the_tenant_group_only() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd503-sock.") as tmp:
        run, sock = socket_dir(tmp)
        owner.restrict_socket_to_tenant(sock, environ={"TICKET_BOARD_SOCKET_GROUP": f" {MY_GROUP} "})
        assert modes(run) == {"run": "0o750", "pane-state": "0o770", "ticket-board.sock": "0o660"}, modes(run)
        assert sock.stat().st_gid == os.getgid()
    for env, text in [
        ({}, "TICKET_BOARD_SOCKET_GROUP is not set for"),
        ({"TICKET_BOARD_SOCKET_GROUP": "no-such-group-syrd503"}, "TICKET_BOARD_SOCKET_GROUP=no-such-group-syrd503 does not resolve"),
    ]:
        with tempfile.TemporaryDirectory(prefix="syrd503-sock.") as tmp, Logs() as logs:
            run, sock = socket_dir(tmp)
            owner.restrict_socket_to_tenant(sock, environ=env)
            assert modes(run) == {"run": "0o755", "ticket-board.sock": "0o666"}, modes(run)
            assert [level for _, level, _ in logs.records] == ["WARNING"] and logs.messages()[0].startswith(text)
    real_chown = os.chown
    with tempfile.TemporaryDirectory(prefix="syrd503-sock.") as tmp, Logs() as logs:
        run, sock = socket_dir(tmp)

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise PermissionError(1, "Operation not permitted")

        os.chown = refuse
        try:
            owner.restrict_socket_to_tenant(sock, environ={"TICKET_BOARD_SOCKET_GROUP": MY_GROUP})
        finally:
            os.chown = real_chown
        assert modes(run) == {"run": "0o755", "pane-state": "0o755", "ticket-board.sock": "0o666"}, modes(run)
        assert [m.split(" to group")[0] for m in logs.messages()] == [
            f"Could not restrict {run}", f"Could not restrict {sock}", f"Could not restrict {run / 'pane-state'}",
        ], logs.messages()
        assert all("the board service must be a member of that group" in m for m in logs.messages())
    with tempfile.TemporaryDirectory(prefix="syrd503-sock.") as tmp:
        run, sock = socket_dir(tmp, with_socket=False)
        owner.restrict_socket_to_tenant(sock, environ={"TICKET_BOARD_SOCKET_GROUP": MY_GROUP})
        assert modes(run) == {"run": "0o750", "pane-state": "0o770"}, modes(run)


def test_allowed_peer_uids() -> None:
    assert owner.allowed_peer_uids({}) == set()
    with Logs() as logs:
        assert owner.allowed_peer_uids({"TICKET_BOARD_ALLOWED_PEER_UIDS": "4101, 4102 x"}) == {4101, 4102, os.getuid()}
        assert owner.allowed_peer_uids({"TICKET_BOARD_ALLOWED_PEER_UIDS": "x"}) == set()
        assert owner.allowed_peer_uids({"TICKET_BOARD_TENANT_USER": "no-such-user-syrd503"}) == set()
    assert owner.allowed_peer_uids({"TICKET_BOARD_TENANT_USER": ME}) == {os.getuid()}
    assert logs.messages() == [
        "Ignoring non-numeric TICKET_BOARD_ALLOWED_PEER_UIDS entry 'x'",
        "Ignoring non-numeric TICKET_BOARD_ALLOWED_PEER_UIDS entry 'x'",
        "TICKET_BOARD_TENANT_USER=no-such-user-syrd503 does not resolve to a local account; peer uid enforcement will not admit it",
    ], logs.messages()


def handler(authority: Any, cred: Any) -> Any:
    h = server.TicketBoardHandler.__new__(server.TicketBoardHandler)
    h.server = types.SimpleNamespace(role_authority=authority)
    h._local_peer_credentials = cred
    return h


def test_handler_checks_credentials_then_tenant_then_role_accounts() -> None:
    C = owner.PeerCredentials
    table = owner.LocalRoleAuthority({"main": "syrd-main"}, resolve_uid=UIDS.get)
    real = server.allowed_peer_uids
    try:
        def unreachable() -> set[int]:
            raise AssertionError("allowed uids read before the credential check")

        server.allowed_peer_uids = unreachable
        assert refusal(lambda: handler(None, None).require_allowed_peer(), ValueError) == "local socket request missing peer credentials"
        server.allowed_peer_uids = lambda: {4101}
        assert handler(None, C(3, 4101, 1)).require_allowed_peer() == C(3, 4101, 1)
        assert handler(table, C(4, 4102, 1)).require_allowed_peer() == C(4, 4102, 1)
        with Logs() as logs:
            assert refusal(lambda: handler(table, C(5, 7777, 1)).require_allowed_peer(), PermissionError) == (
                "this board socket does not serve that local user"
            )
        assert logs.records == [("scripts.ticket_board.server", "WARNING",
                                 "Rejected local board connection from uid=7777 pid=5: not this tenant's account or one of its role accounts")]
        server.allowed_peer_uids = lambda: set()
        assert handler(None, C(6, 7777, 1)).require_allowed_peer() == C(6, 7777, 1)
        assert refusal(lambda: handler(table, C(7, 7777, 1)).require_allowed_peer(), PermissionError) == (
            "this board socket does not serve that local user"
        )
    finally:
        server.allowed_peer_uids = real


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_local_peer_authority_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
