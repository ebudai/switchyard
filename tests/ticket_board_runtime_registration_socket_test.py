#!/usr/bin/env python3
"""Runtime registration over the real Unix socket, with real process authority.

The board rejected every runtime registration on a process-authority tenant with
`'ProcessRoleAuthority' object has no attribute 'uids'`. Nothing caught it,
because the existing process-authority regressions call `role_for_peer()`
directly: they never reach `require_allowed_peer()`, which is where the tenant
boundary unions the authority's uids and where registration actually died --
before `session_for_peer()` could refuse or admit anything.

So this drives the production `TicketBoardUnixServer` and `TicketBoardHandler`
over a real AF_UNIX socket, with a real `ProcessRoleAuthority`, and lets
SO_PEERCRED supply the credentials the kernel actually sees. Nothing here stands
in for the peer identity, the handler, or the authority object.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.peer_identity import SessionIdentity
from scripts.ticket_board.server import (
    ProcessRoleAuthority,
    TicketBoardUnixServer,
)

# The pane this test is pretending to be is this very process, because that is
# whose pid and uid SO_PEERCRED will report to the server.
PANE_PID = os.getpid()
PANE_UID = os.getuid()
PANE_START_TIME = 11001
SIBLING_PID = PANE_PID + 1
SIBLING_START_TIME = 11002


class RecordingApp:
    """Only the runtime-assignment surface the registration path touches."""

    def __init__(self, *, registered: bool) -> None:
        self.assignment = (
            {
                "role": "inspector",
                "runtime": "claude",
                "target": "syrd-inspector:0.0",
                "process_pid": PANE_PID,
                "process_start_time": PANE_START_TIME,
                "process_uid": PANE_UID,
                "generation": 1,
            }
            if registered
            else None
        )
        self.registrations: list[dict[str, object]] = []

    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int):
        if self.assignment is None:
            return None
        if (pid, start_time, uid) != (
            int(self.assignment["process_pid"]),
            int(self.assignment["process_start_time"]),
            int(self.assignment["process_uid"]),
        ):
            return None
        return self.assignment

    def runtime_assignment(self, role: str):
        if self.assignment is None or role != self.assignment["role"]:
            return None
        return self.assignment

    def register_runtime_assignment(self, **payload):
        self.registrations.append(dict(payload))
        self.assignment = {
            "role": payload["role"],
            "runtime": payload["runtime"],
            "target": payload["target"],
            "process_pid": payload["process_pid"],
            "process_start_time": payload["process_start_time"],
            "process_uid": payload["process_uid"],
            "generation": int(payload["expected_generation"]) + 1,
        }
        return self.assignment


class QuietNotifier:
    def notify_ticket_created(self, *args, **kwargs) -> None:
        return None


class Events:
    def notify_change(self, *args, **kwargs) -> None:
        return None


def serve(socket_path: Path, app, authority):
    server = TicketBoardUnixServer(
        socket_path,
        app,
        events=Events(),
        director_notifier=QuietNotifier(),
        role_authority=authority,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def register_caller(socket_path: Path, payload: dict[str, object]) -> tuple[int, str]:
    """A real client on a real AF_UNIX socket; the kernel names the peer."""
    connection = http.client.HTTPConnection("localhost")
    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.sock.connect(str(socket_path))
    try:
        connection.request(
            "POST",
            "/api/register-caller",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()


def process_authority(app, *, project_uid: int, sessions: dict[int, SessionIdentity]):
    return ProcessRoleAuthority(
        app,
        "syrd-project-account",
        resolve_uid=lambda _account: project_uid,
        resolve_session=sessions.get,
        session_live=lambda _identity: True,
    )


def registration_payload(role: str = "inspector") -> dict[str, object]:
    return {
        "role": role,
        "runtime": "claude",
        "target": f"syrd-{role}:0.0",
        "worktree": f"/home/syrd/worktrees/{role}",
        "session_dir": "/home/syrd/sessions",
    }


def with_server(app, authority, fn):
    with tempfile.TemporaryDirectory(prefix="board-registration.") as tmp:
        socket_path = Path(tmp) / "ticket-board.sock"
        server, thread = serve(socket_path, app, authority)
        try:
            return fn(socket_path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_an_already_registered_pane_is_recognised_over_the_real_socket() -> None:
    """The crash was here, before any role was resolved.

    On the broken build this returns 500 and the body carries the AttributeError
    rather than a role, which is exactly what every wrapped CLI saw.
    """
    app = RecordingApp(registered=True)
    authority = process_authority(
        app,
        project_uid=PANE_UID,
        sessions={PANE_PID: SessionIdentity(PANE_PID, PANE_START_TIME)},
    )
    status, body = with_server(
        app, authority, lambda path: register_caller(path, {"role": "inspector"})
    )
    assert status == 200, (status, body)
    assert "uids" not in body, f"the tenant boundary crashed instead of deciding: {body}"
    payload = json.loads(body)
    assert payload["role"] == "inspector", payload
    assert payload["uid"] == PANE_UID and payload["pid"] == PANE_PID, payload
    assert app.registrations == [], "an already-registered pane must not re-register"


def test_an_unregistered_pane_publishes_its_assignment_and_gets_its_role() -> None:
    """The path the launcher takes on a fresh pane, end to end."""
    app = RecordingApp(registered=False)
    authority = process_authority(
        app,
        project_uid=PANE_UID,
        sessions={PANE_PID: SessionIdentity(PANE_PID, PANE_START_TIME)},
    )
    status, body = with_server(
        app, authority, lambda path: register_caller(path, registration_payload())
    )
    assert status == 200, (status, body)
    payload = json.loads(body)
    assert payload["role"] == "inspector", payload
    assert len(app.registrations) == 1, app.registrations
    recorded = app.registrations[0]
    # The server derives these; a caller cannot choose the process it is.
    assert recorded["process_pid"] == PANE_PID, recorded
    assert recorded["process_start_time"] == PANE_START_TIME, recorded
    assert recorded["process_uid"] == PANE_UID, recorded


def test_a_foreign_uid_is_refused_at_the_tenant_boundary() -> None:
    """The boundary the missing method was supposed to be enforcing.

    The authority resolves to some other account, so this peer is not this
    tenant's and never reaches session resolution.
    """
    app = RecordingApp(registered=True)
    authority = process_authority(
        app,
        project_uid=PANE_UID + 1,
        sessions={PANE_PID: SessionIdentity(PANE_PID, PANE_START_TIME)},
    )
    assert authority.uids() == {PANE_UID + 1}, authority.uids()
    status, body = with_server(
        app, authority, lambda path: register_caller(path, registration_payload())
    )
    assert status == 403, (status, body)
    assert "does not serve that local user" in body, body
    assert app.registrations == [], "a foreign uid registered a runtime assignment"


def test_an_unregistered_sibling_cannot_take_a_live_role() -> None:
    """Same uid, different process: the shared account is not role authority.

    The tenant boundary admits it -- that is the whole point of admitting the
    project uid there -- and the process checks behind it are what refuse.
    """
    app = RecordingApp(registered=True)
    authority = process_authority(
        app,
        project_uid=PANE_UID,
        # The connecting process resolves to a session that is not the one
        # holding the inspector assignment.
        sessions={PANE_PID: SessionIdentity(SIBLING_PID, SIBLING_START_TIME)},
    )
    status, body = with_server(
        app, authority, lambda path: register_caller(path, registration_payload())
    )
    assert status == 403, (status, body)
    assert "held by a live pane" in body, body
    assert app.registrations == [], "a sibling process registered over a live role"


def test_a_pane_with_no_live_session_is_refused() -> None:
    """A uid inside the tenant, but no live launcher pane behind the pid."""
    app = RecordingApp(registered=True)
    authority = ProcessRoleAuthority(
        app,
        "syrd-project-account",
        resolve_uid=lambda _account: PANE_UID,
        resolve_session=lambda _pid: None,
        session_live=lambda _identity: True,
    )
    status, body = with_server(
        app, authority, lambda path: register_caller(path, registration_payload())
    )
    assert status == 403, (status, body)
    assert "live launcher pane" in body, body
    assert app.registrations == [], "a pane with no live session registered anyway"


def test_the_tenant_boundary_is_answered_by_every_authority_the_socket_accepts() -> None:
    """No feature check may be added to require_allowed_peer() instead of this.

    Skipping the union when an authority lacks the method would turn a crash
    into a silently unenforced tenant boundary, which is worse than the outage
    it replaced.
    """
    from scripts.ticket_board.server import LocalRoleAuthority, TicketBoardHandler
    import inspect

    for authority_class in (ProcessRoleAuthority, LocalRoleAuthority):
        assert callable(getattr(authority_class, "uids", None)), (
            f"{authority_class.__name__} cannot answer the tenant boundary"
        )
    source = inspect.getsource(TicketBoardHandler.require_allowed_peer)
    for evasion in ("hasattr", "getattr(", "try:"):
        assert evasion not in source, (
            f"require_allowed_peer() guards the union with {evasion!r}; the boundary "
            "must be unconditional and the authority must answer it"
        )


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ticket_board_runtime_registration_socket_test: ok ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
