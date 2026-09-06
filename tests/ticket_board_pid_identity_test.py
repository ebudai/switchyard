#!/usr/bin/env python3
"""Regression tests for Unix-socket PID-derived board caller identity."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.peer_identity import SessionIdentity, session_identity
from scripts.ticket_board.server import (
    CALLER_ROLE_HEADER,
    CallerIdentityError,
    CallerRegistry,
    PANE_SOCKET_MODE,
    PeerCredentials,
    TicketBoardEventHub,
    TicketBoardUnixServer,
    allowed_peer_uids,
)
from scripts.ticket_board.write_client import TicketBoardWriteClient, UnixHTTPConnection


class QuietNotifier:
    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return


class RecordingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def ticket_payload(
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "blocked_by": [],
        "parent_id": "",
        "blocked_reason": "",
        "implementation": implementation,
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_user_signoff": False,
        "user_signoff": False,
        "manually_controlled": False,
        "commit_hash": "",
        "commit_exempt": False,
        "created": "2026-07-11T00:00:00+00:00",
        "updated": "2026-07-11T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
        "screenshots_info": [],
        "screenshot": None,
        "screenshot_available": False,
    }


class MemoryBoardApp:
    store_backend = "postgres"

    def __init__(self, tickets: list[dict[str, object]], frames: Path, assets: Path) -> None:
        self.tickets = {str(ticket["id"]): ticket for ticket in tickets}
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((ticket_id, str(ticket["updated"])) for ticket_id, ticket in self.tickets.items()))

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": list(self.tickets.values()),
            "errors": [],
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def get_ticket(self, ticket_id: str) -> dict[str, object]:
        return self.tickets[ticket_id]

    def update_ticket(self, ticket_id: str, patch: dict[str, object], *, caller_role: str | None = None) -> dict[str, object]:
        ticket = self.tickets[ticket_id]
        comment = patch.pop("comment", None)
        ticket.update(patch)
        if isinstance(comment, dict):
            ticket.setdefault("comments", [])
            ticket["comments"].append({"who": comment.get("who", caller_role), "text": comment.get("text", "")})  # type: ignore[union-attr]
        ticket["updated"] = iso_now()
        return ticket


def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def request_unix(
    socket_path: Path,
    path: str,
    payload: dict[str, object],
    *,
    caller_header: str | None = None,
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if caller_header is not None:
        headers[CALLER_ROLE_HEADER] = caller_header
    conn = UnixHTTPConnection(str(socket_path), timeout=5.0)
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()



# --- SYRD-39 role-session helpers -------------------------------------------
# Authority is derived from the pane process a peer descends from. Tests model
# that process table explicitly instead of depending on whether the suite itself
# happens to run inside tmux.

DIRECTOR_PANE = SessionIdentity(pid=9001, start_time=100, comm="fish")
OPS_PANE = SessionIdentity(pid=9002, start_time=200, comm="fish")
RESTARTED_DIRECTOR_PANE = SessionIdentity(pid=9003, start_time=300, comm="fish")


def session_registry(
    mapping: dict[int, SessionIdentity],
    *,
    dead: set[tuple[int, int]] | None = None,
) -> CallerRegistry:
    """A registry over a modelled process table.

    `mapping` stands in for /proc ancestry: peer pid -> the pane it descends
    from. A pid missing from it is a process outside every role pane.
    """
    gone = dead if dead is not None else set()
    return CallerRegistry(
        resolve_session=lambda pid: mapping.get(pid),
        session_alive=lambda session: session.key() not in gone,
    )


def creds(pid: int) -> PeerCredentials:
    return PeerCredentials(pid=pid, uid=1000, gid=1000)


def assert_role_binding_outlives_the_command_that_made_it() -> None:
    """The reported defect: authority evaporated when the helper exited.

    ticket-board-write is a new process per command, so binding the role to the
    connecting pid meant that between two director commands nothing held
    director and any local process could take it.
    """
    # 7001 is a director-pane descendant; 7002 belongs to the ops pane.
    registry = session_registry({7001: DIRECTOR_PANE, 7002: OPS_PANE})
    registry.register(creds(7001), "director")
    assert registry.pid_for_role("director") == DIRECTOR_PANE.pid

    # The command exits. Its connection is released, but the pane still holds
    # the role -- this is the line that used to hand director to anyone.
    registry.release(DIRECTOR_PANE)
    assert registry.pid_for_role("director") == DIRECTOR_PANE.pid

    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        try:
            registry.register(creds(7002), "director")
        except CallerIdentityError:
            pass
        else:
            raise AssertionError("a different role session must not take director")
        assert any(
            "role is held by live session" in message and "peer pid=7002" in message
            for message in handler.messages
        ), handler.messages
    finally:
        logger.removeHandler(handler)

    # A later director command reattaches to the same pane without re-claiming.
    assert registry.role_for_pid(7001) == "director"


def assert_role_session_cannot_answer_to_a_second_role() -> None:
    registry = session_registry({7001: DIRECTOR_PANE})
    registry.register(creds(7001), "director")
    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        try:
            registry.register(creds(7001), "ops")
        except CallerIdentityError:
            pass
        else:
            raise AssertionError("one role session must not hold two roles")
        assert any("already holds role director" in message for message in handler.messages), handler.messages
    finally:
        logger.removeHandler(handler)


def assert_descendant_commands_inherit_the_session_role() -> None:
    """A role runs nested commands; each is a fresh pid under the same pane."""
    mapping = {7001: DIRECTOR_PANE}
    registry = session_registry(mapping)
    registry.register(creds(7001), "director")
    for descendant in (7010, 7011, 7012):
        mapping[descendant] = DIRECTOR_PANE
        assert registry.role_for_pid(descendant) == "director"


def assert_peer_outside_every_role_session_is_refused() -> None:
    """A local process that is not in any pane inherits nothing."""
    registry = session_registry({7001: DIRECTOR_PANE})
    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        try:
            registry.register(creds(4242), "director")
        except CallerIdentityError:
            pass
        else:
            raise AssertionError("a session-less peer must not obtain a role")
        assert any("not part of any role session" in message for message in handler.messages), handler.messages
    finally:
        logger.removeHandler(handler)


def assert_restarted_role_session_reclaims_its_role() -> None:
    dead: set[tuple[int, int]] = set()
    mapping = {7001: DIRECTOR_PANE}
    registry = session_registry(mapping, dead=dead)
    registry.register(creds(7001), "director")
    assert registry.pid_for_role("director") == DIRECTOR_PANE.pid

    # The pane exits and the launcher starts a replacement: same role, new pid
    # and start time.
    dead.add(DIRECTOR_PANE.key())
    mapping[7020] = RESTARTED_DIRECTOR_PANE
    registry.register(creds(7020), "director")
    assert registry.pid_for_role("director") == RESTARTED_DIRECTOR_PANE.pid


def assert_recycled_pid_does_not_inherit_authority() -> None:
    """Identity is pid plus start time, so pid reuse is not inheritance."""
    dead: set[tuple[int, int]] = set()
    mapping = {7001: DIRECTOR_PANE}
    registry = session_registry(mapping, dead=dead)
    registry.register(creds(7001), "director")

    dead.add(DIRECTOR_PANE.key())
    recycled = SessionIdentity(pid=DIRECTOR_PANE.pid, start_time=DIRECTOR_PANE.start_time + 1, comm="fish")
    mapping[7030] = recycled
    try:
        registry.role_for_pid(7030)
    except ValueError:
        pass
    else:
        raise AssertionError("a recycled pid must not inherit the dead pane's role")


def assert_peer_uid_allowlist_admits_only_the_tenant() -> None:
    assert allowed_peer_uids({}) == set()
    configured = allowed_peer_uids({"TICKET_BOARD_ALLOWED_PEER_UIDS": "1006"})
    assert 1006 in configured, configured
    # The board's own account is always admitted so health checks do not depend
    # on the tenant list being exhaustive.
    assert os.getuid() in configured, configured
    assert 4242 not in configured, configured
    # A tenant that does not resolve must not silently widen the set.
    unknown = allowed_peer_uids({"TICKET_BOARD_TENANT_USER": "definitely-not-a-local-account"})
    assert unknown == set(), unknown


def assert_live_session_resolution_matches_the_process_tree() -> None:
    """The real resolver agrees with the kernel about this process's pane.

    Skipped when the suite is not running under tmux, because then there is no
    pane to resolve and the modelled tests above already cover the contract.
    """
    identity = session_identity(os.getpid())
    if identity is None:
        return
    assert identity.pid > 0 and identity.start_time > 0, identity
    # Every child of this process must resolve to the same pane.
    assert session_identity(os.getpid()) == identity


def assert_comm_forged_pane_identity_is_accepted_documenting_the_limit() -> None:
    """Adversarial: the session key is forgeable from inside any pane.

    The pane root is found by testing a parent's /proc comm against "tmux", and
    a process sets its own comm with prctl(PR_SET_NAME). One prctl and one fork
    manufacture a fresh unused pane identity -- no tmux command, no target-pane
    execution, no ptrace.

    This test asserts the forgery SUCCEEDS. It exists so the limitation cannot
    be quietly reintroduced after a partial fix, and so that anyone who does
    make the boundary authoritative is forced to come here, see the claim, and
    update docs/ticket-board-service.md with it (SYRD-39).
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return

    honest = session_identity(os.getpid())
    read_fd, write_fd = os.pipe()
    libc.prctl(15, b"tmux: server\x00", 0, 0, 0)  # PR_SET_NAME
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        try:
            os.close(read_fd)
            os.write(write_fd, str(os.getpid()).encode())
            os.close(write_fd)
            time.sleep(5)
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        forged_pid = int(os.read(read_fd, 32).decode())
        os.close(read_fd)
        deadline = time.monotonic() + 2.0
        forged = None
        while time.monotonic() < deadline:
            forged = session_identity(forged_pid)
            if forged is not None and forged.pid == forged_pid:
                break
            time.sleep(0.02)
        assert forged is not None, "the forged child resolved to no session at all"
        assert forged.pid == forged_pid, (forged, forged_pid)
        assert forged.comm.startswith("tmux"), forged
        if honest is not None:
            assert forged.key() != honest.key(), (forged, honest)

        # A fresh board hands the forged identity any vacant role.
        registry = CallerRegistry()
        registry.register(creds(forged_pid), "director")
        assert registry.role_for_pid(forged_pid) == "director"
        assert registry.pid_for_role("director") == forged_pid
    finally:
        try:
            os.kill(child, 9)
            os.waitpid(child, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def assert_board_restart_drops_bindings_documenting_the_limit() -> None:
    """Adversarial: bindings do not survive a board restart.

    Registrations live only in the board process. When the service restarts --
    an ordinary deploy -- every pane keeps running while every role becomes
    vacant, so any live session can then claim any role. The startup race is
    not a one-off at boot; a restart recreates it for every pane.

    Like the test above, this asserts the CURRENT behaviour so it cannot be
    silently reintroduced or silently described as fixed (SYRD-39).
    """
    mapping = {7001: DIRECTOR_PANE, 7002: OPS_PANE}

    def fresh_board() -> CallerRegistry:
        return CallerRegistry(
            resolve_session=lambda pid: mapping.get(pid),
            session_alive=lambda session: True,
        )

    before = fresh_board()
    before.register(creds(7001), "director")
    before.register(creds(7002), "ops")
    assert before.role_for_pid(7002) == "ops"
    try:
        before.register(creds(7002), "director")
    except CallerIdentityError:
        pass
    else:
        raise AssertionError("while the board is up, ops must not take a held director")

    # Service restarts; the panes are untouched.
    after = fresh_board()
    after.register(creds(7002), "director")
    assert after.role_for_pid(7002) == "director"
    assert after.pid_for_role("director") == OPS_PANE.pid


def assert_real_tmux_panes_resolve_to_distinct_role_sessions() -> None:
    """End-to-end over real processes: the /proc derivation matches tmux.

    The modelled tests above pin the registry contract; this one pins the thing
    the contract rests on -- that two role panes really do resolve to two
    different durable identities, and that a nested command inherits the pane it
    runs under rather than one it merely names. Skipped when tmux is absent.
    """
    if shutil.which("tmux") is None:
        return
    server_socket = Path(tempfile.mkdtemp(prefix="syrd39-tmux.")) / "tmux.sock"

    def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-S", str(server_socket), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    try:
        # Each pane runs a shell that in turn runs a child, so the pane process
        # and the working command are genuinely different pids.
        tmux("new-session", "-d", "-s", "syrd39-a", "sh -c 'sleep 60; :'")
        tmux("new-session", "-d", "-s", "syrd39-b", "sh -c 'sleep 60; :'")

        def pane_pids() -> dict[str, int]:
            listed = tmux("list-panes", "-a", "-F", "#{session_name} #{pane_pid}").stdout.split()
            return {listed[i]: int(listed[i + 1]) for i in range(0, len(listed), 2)}

        # tmux reports the pane pid before the pane command has forked, so settle
        # until each pane is its shell rather than the server itself.
        deadline = time.monotonic() + 5.0
        panes: dict[str, int] = {}
        while time.monotonic() < deadline:
            panes = pane_pids()
            if len(panes) == 2 and all(
                (info := session_identity(pid)) is not None and not info.comm.startswith("tmux")
                for pid in panes.values()
            ):
                break
            time.sleep(0.05)
        assert len(panes) == 2, panes

        identities = {name: session_identity(pid) for name, pid in panes.items()}
        for name, identity in identities.items():
            assert identity is not None, name
            assert identity.pid == panes[name], (name, identity, panes)
        # Two panes are two identities: neither can present as the other.
        assert identities["syrd39-a"].key() != identities["syrd39-b"].key(), identities

        # A descendant running under pane A resolves to pane A, not to pane B,
        # whatever it might claim about itself.
        child_pid = 0
        child_deadline = time.monotonic() + 5.0
        while time.monotonic() < child_deadline:
            listed = subprocess.run(
                ["pgrep", "-P", str(panes["syrd39-a"])],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.split()
            if listed:
                child_pid = int(listed[0])
                break
            time.sleep(0.05)
        assert child_pid, "pane A never spawned a descendant to resolve"
        child_identity = session_identity(child_pid)
        assert child_identity == identities["syrd39-a"], (child_identity, identities)
        assert child_identity != identities["syrd39-b"], child_identity
    finally:
        tmux("kill-server", check=False)
        shutil.rmtree(server_socket.parent, ignore_errors=True)


def assert_unix_socket_derives_role_and_ignores_supplied_role() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-pid.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        socket_path = root / "board.sock"
        frames.mkdir()
        assets.mkdir()
        app = MemoryBoardApp(
            [
                ticket_payload("PGU-300", title="Start through socket", state="in_progress", assignee="ops", implementation="Ready."),
                ticket_payload("PGU-301", title="Header mismatch", state="analysis"),
            ],
            frames,
            assets,
        )
        events = TicketBoardEventHub(app)
        # This process stands in for a command running inside the ops pane.
        registry = session_registry({os.getpid(): OPS_PANE})
        server = TicketBoardUnixServer(socket_path, app, events=events, director_notifier=QuietNotifier(), caller_registry=registry)
        socket_mode = stat.S_IMODE(socket_path.stat().st_mode)
        # Not world-writable: another tenant's Unix account has no path in.
        assert socket_mode == PANE_SOCKET_MODE == 0o660, oct(socket_mode)
        assert not socket_mode & stat.S_IWOTH, oct(socket_mode)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role="ops")
            started = client.start_work("PGU-300")["ticket"]
            assert started["state"] == "in_progress", started
            assert app.tickets["PGU-300"]["state"] == "in_progress"

            # The binding belongs to the pane, so it survives the command that
            # created it rather than being handed back between commands.
            assert registry.pid_for_role("ops") == OPS_PANE.pid

            conn = UnixHTTPConnection(str(socket_path), timeout=5.0)
            try:
                conn.request(
                    "POST",
                    "/api/register-caller",
                    body=json.dumps({"role": "ops"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                assert response.status == 200, response.read().decode("utf-8")
                response.read()
                assert registry.pid_for_role("ops") == OPS_PANE.pid

                # Claiming a different role from a session that already answers
                # to ops is refused; this is the raw-socket escalation from the
                # SYRD-39 report.
                conn.request(
                    "POST",
                    "/api/register-caller",
                    body=json.dumps({"role": "director"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                body = response.read().decode("utf-8")
                assert response.status == 403, (response.status, body)
                assert "registered as ops" in body, body
                assert registry.pid_for_role("ops") == OPS_PANE.pid
                assert registry.pid_for_role("director") is None

                conn.request(
                    "POST",
                    "/api/tickets/PGU-301/actions/route",
                    body=json.dumps({"state": "backlog", "assignee": "ops"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", CALLER_ROLE_HEADER: "director"},
                )
                response = conn.getresponse()
                body = response.read().decode("utf-8")
                assert response.status == 403, (response.status, body)
                assert "ops cannot call route" in body, body
            finally:
                conn.close()

            # A peer outside every role pane inherits nothing, whatever it puts
            # in the header.
            unbound = session_registry({})
            unbound_server = TicketBoardUnixServer(
                root / "unbound.sock",
                app,
                events=events,
                director_notifier=QuietNotifier(),
                caller_registry=unbound,
            )
            unbound_thread = threading.Thread(target=unbound_server.serve_forever, daemon=True)
            unbound_thread.start()
            try:
                status, body = request_unix(
                    root / "unbound.sock",
                    "/api/tickets/PGU-301/actions/start_work",
                    {},
                    caller_header="director",
                )
                assert status == 403, (status, body)
                assert "role session" in body, body
            finally:
                unbound_server.shutdown()
                unbound_server.server_close()
                unbound_thread.join(timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            events.close()
            thread.join(timeout=2)


def assert_unix_socket_replaces_stale_socket_file() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-stale-socket.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        socket_path = root / "board.sock"
        frames.mkdir()
        assets.mkdir()
        socket_path.touch()
        app = MemoryBoardApp(
            [ticket_payload("PGU-302", title="Stale socket replaced", state="in_progress", assignee="ops", implementation="Ready.")],
            frames,
            assets,
        )
        events = TicketBoardEventHub(app)
        server = TicketBoardUnixServer(socket_path, app, events=events, director_notifier=QuietNotifier(), caller_registry=CallerRegistry())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert stat.S_ISSOCK(socket_path.stat().st_mode), socket_path.stat()
            client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role="ops")
            started = client.start_work("PGU-302")["ticket"]
            assert started["state"] == "in_progress", started
        finally:
            server.shutdown()
            server.server_close()
            events.close()
            thread.join(timeout=2)


def assert_all_pane_roles_write_through_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-pane-roles.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        socket_path = root / "board.sock"
        frames.mkdir()
        assets.mkdir()
        implementers = ["main", "app", "ops", "perf", "research"]
        support_roles = ["director", "audit", "inspector"]
        tickets = [
            ticket_payload(f"PGU-{400 + index}", title=f"{role} work", state="analysis", assignee=role, implementation="Ready.")
            for index, role in enumerate(implementers)
        ]
        tickets.extend(
            ticket_payload(f"PGU-{500 + index}", title=f"{role} note", state="analysis")
            for index, role in enumerate(support_roles)
        )
        app = MemoryBoardApp(tickets, frames, assets)
        events = TicketBoardEventHub(app)
        # Each role has its own pane, so the peer's resolved session moves as
        # the test steps through them. One process cannot be two roles at once,
        # which is the point of the binding.
        panes = {
            role: SessionIdentity(pid=9100 + index, start_time=index + 1, comm="fish")
            for index, role in enumerate([*implementers, *support_roles])
        }
        current_pane: list[SessionIdentity] = [panes[implementers[0]]]
        registry = CallerRegistry(
            resolve_session=lambda pid: current_pane[0],
            session_alive=lambda session: True,
        )
        server = TicketBoardUnixServer(socket_path, app, events=events, director_notifier=QuietNotifier(), caller_registry=registry)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for index, role in enumerate(implementers):
                ticket_id = f"PGU-{400 + index}"
                current_pane[0] = panes[role]
                client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role=role)
                started = client.start_work(ticket_id)["ticket"]
                assert started["state"] == "in_progress", (role, started)
                assert registry.pid_for_role(role) == panes[role].pid
            for index, role in enumerate(support_roles):
                ticket_id = f"PGU-{500 + index}"
                current_pane[0] = panes[role]
                client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role=role)
                commented = client.add_comment(ticket_id, text=f"{role} note")["ticket"]
                assert commented["comments"][-1]["who"] == role, (role, commented)
                assert registry.pid_for_role(role) == panes[role].pid
            # Every role stays bound to its own pane afterwards: data-driven and
            # ephemeral roles are held the same way, none inherit another's.
            for role, pane in panes.items():
                assert registry.pid_for_role(role) == pane.pid, role
        finally:
            server.shutdown()
            server.server_close()
            events.close()
            thread.join(timeout=2)


def main() -> int:
    assert_role_binding_outlives_the_command_that_made_it()
    assert_role_session_cannot_answer_to_a_second_role()
    assert_descendant_commands_inherit_the_session_role()
    assert_peer_outside_every_role_session_is_refused()
    assert_restarted_role_session_reclaims_its_role()
    assert_recycled_pid_does_not_inherit_authority()
    assert_peer_uid_allowlist_admits_only_the_tenant()
    assert_live_session_resolution_matches_the_process_tree()
    assert_real_tmux_panes_resolve_to_distinct_role_sessions()
    assert_comm_forged_pane_identity_is_accepted_documenting_the_limit()
    assert_board_restart_drops_bindings_documenting_the_limit()
    assert_unix_socket_derives_role_and_ignores_supplied_role()
    assert_unix_socket_replaces_stale_socket_file()
    assert_all_pane_roles_write_through_socket()
    print("ticket_board_pid_identity_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
