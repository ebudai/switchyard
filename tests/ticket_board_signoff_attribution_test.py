#!/usr/bin/env python3
"""SYRD-81: a transition cannot become somebody else's review.

The Director may move a ticket anywhere and may narrate an override, so the
question this file answers is what happens when either is pointed at a review:
can a move raise `audit_signoff`, `inspector_signoff` or `user_signoff`, and can
a caller act as a role it is not? Clearing an approval during a kick-back has to
keep working, because that is an ordinary part of sending work back.

Driven through the production handler over a real AF_UNIX socket with a real
ProcessRoleAuthority, so the attribution under test is the one the kernel
reports rather than one a fixture asserts.
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
from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer
from scripts.ticket_board.workflow_config import validate

PANE_PID = os.getpid()
PANE_UID = os.getuid()
PANE_START = 4242

SIGNOFF_ACTIONS = ("audit_sign_off", "inspector_sign_off", "user_sign_off")
SIGNOFF_FLAGS = ("audit_signoff", "inspector_signoff", "user_signoff")

DOCUMENT = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))


class DirectorPaneApp:
    """Just enough board for the handler; every write is recorded, none applied."""

    def __init__(self) -> None:
        self.workflow_actions: list[tuple[str, str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self.ticket = {
            "id": "SYRD-81",
            "state": "audit",
            "assignee": "audit",
            "audit_signoff": False,
            "inspector_signoff": False,
            "user_signoff": False,
            "needs_audit": True,
            "blockers": [],
            "comments": [],
        }

    # The pane is the director's.
    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int):
        if (pid, start_time, uid) != (PANE_PID, PANE_START, PANE_UID):
            return None
        return {"role": "director", "process_pid": pid, "process_start_time": start_time, "process_uid": uid}

    def workflow_configuration(self):
        return DOCUMENT

    def workflow_roles(self):
        return [role["name"] for role in DOCUMENT["roles"] if role["active"]]

    def get_ticket(self, ticket_id: str):
        return dict(self.ticket)

    def store_signature(self):
        return ()

    def perform_workflow_action(self, ticket_id, action, payload, *, caller_role):
        self.workflow_actions.append((action, caller_role, dict(payload)))
        return dict(self.ticket)

    def update_ticket(self, ticket_id, patch, *, caller_role=None):
        self.patches.append((caller_role, dict(patch)))
        return dict(self.ticket)

    def force_move_ticket(self, ticket_id, state, assignee, *, suppress_notification=False, caller_role=None):
        self.patches.append((caller_role, {"force_move": state, "assignee": assignee}))
        return dict(self.ticket)


class Quiet:
    def notify_ticket_created(self, *a, **k) -> None: ...
    def notify_change(self, *a, **k) -> None: ...


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._path)
        self.sock = sock


def serve(app):
    tmp = tempfile.TemporaryDirectory(prefix="signoff-attribution.")
    socket_path = Path(tmp.name) / "board.sock"
    authority = ProcessRoleAuthority(
        app,
        "syrd-project-account",
        resolve_uid=lambda _a: PANE_UID,
        resolve_session=lambda pid: SessionIdentity(PANE_PID, PANE_START),
        session_live=lambda _i: True,
    )
    server = TicketBoardUnixServer(
        socket_path, app, events=Quiet(), director_notifier=Quiet(), role_authority=authority
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return tmp, socket_path, server, thread


def action(socket_path: Path, operation: str, payload: dict, *, claim: str | None = None):
    connection = UnixConnection(str(socket_path))
    try:
        headers = {"Content-Type": "application/json"}
        if claim is not None:
            headers["X-Ticket-Board-Caller-Role"] = claim
        connection.request(
            "POST",
            f"/api/tickets/SYRD-81/actions/{operation}",
            body=json.dumps(payload),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()


def with_board(fn):
    app = DirectorPaneApp()
    tmp, socket_path, server, thread = serve(app)
    try:
        return fn(app, socket_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        tmp.cleanup()


def test_a_director_pane_cannot_invoke_any_review_sign_off() -> None:
    """The direct route: ask for the reviewer's own action and be refused."""

    def check(app, socket_path):
        for operation in SIGNOFF_ACTIONS:
            status, body = action(socket_path, operation, {"text": "not mine to give"})
            assert status == 403, (operation, status, body)
            assert "cannot call" in body, (operation, body)
        assert app.workflow_actions == [], app.workflow_actions
        assert app.patches == [], app.patches

    with_board(check)


def test_a_claimed_reviewer_role_is_inert_and_the_pane_is_what_answers() -> None:
    """A supplied caller role buys nothing: the kernel decides who called.

    The header is data, not identity. It is deliberately not an error either,
    because a pane's configured role can legitimately go stale -- a Director
    reassignment (SYRD-77) changes the registered role while the pane's
    environment still names the old one -- and a board that refused on the
    mismatch would break every write from a pane that was reassigned under it.
    So the claim is ignored and the action is attributed to the real pane, which
    is then refused on its own capabilities. Refused either way; never the
    claimed role's authority.
    """

    def check(app, socket_path):
        for claimed in ("audit", "inspector", "user", "director"):
            status, body = action(
                socket_path, "audit_sign_off", {"text": "claiming to be someone else"}, claim=claimed
            )
            assert status == 403, (claimed, status, body)
            # Refused as the pane, not as the role that was named.
            assert "director cannot call audit_sign_off" in body, (claimed, body)
        assert app.workflow_actions == [], app.workflow_actions
        assert app.patches == [], app.patches

    with_board(check)


def test_a_generic_workflow_move_is_attributed_to_the_pane_that_made_it() -> None:
    """Whatever the payload says, the caller recorded is the real one."""

    def check(app, socket_path):
        status, body = action(socket_path, "mark_done", {"commit_hash": ""})
        assert status in (200, 400, 403), (status, body)
        for _action, caller, _payload in app.workflow_actions:
            assert caller == "director", (caller, app.workflow_actions)
        for caller, _patch in app.patches:
            assert caller == "director", (caller, app.patches)

    with_board(check)


def test_the_workflow_action_payload_cannot_carry_an_actor() -> None:
    """The declared-transition path takes routing data, not identity.

    perform_workflow_action() accepts a closed set of fields, so a move cannot
    smuggle a caller_role, an actor, or a sign-off flag alongside it.
    """
    from scripts.ticket_board.app import TicketBoardApp
    import inspect

    source = inspect.getsource(TicketBoardApp.perform_workflow_action)
    assert 'set(payload) - {"target", "assignee", "commit_hash", "text", "reason"}' in source, source
    for forbidden in ("caller_role", "actor", "audit_signoff", "inspector_signoff", "user_signoff"):
        assert f'"{forbidden}"' not in source.split("unknown workflow action payload field")[0], forbidden


def test_the_declared_workflow_gives_no_review_to_the_director() -> None:
    """Every approval belongs to exactly the role being reviewed by it.

    An approve transition is the only thing that raises a stage's sign-off, so
    the actors on those transitions are the whole question. Read from the
    shipped document rather than asserted about it.
    """
    signoff_stages = {stage["name"]: stage["signoff"] for stage in DOCUMENT["stages"] if stage["signoff"]}
    assert set(signoff_stages.values()) == set(SIGNOFF_FLAGS), signoff_stages

    approvals = [t for t in DOCUMENT["transitions"] if t["primitive"] == "approve"]
    assert approvals, "the fixture workflow has no approvals to check"
    for transition in approvals:
        stage = transition["from"]
        assert stage in signoff_stages, transition
        owners = next(s for s in DOCUMENT["stages"] if s["name"] == stage)["owners"]
        assert transition["actors"] == owners, transition
        assert "director" not in transition["actors"], transition


def test_a_kick_back_still_clears_the_approval_it_sends_back() -> None:
    """The allowed case. Returning work clears review, and must keep doing so."""
    returns = [t for t in DOCUMENT["transitions"] if t["primitive"] == "return"]
    assert returns, "the fixture workflow has no kick-backs to check"
    cleared = {flag for transition in returns for flag in transition["clear_signoffs"]}
    assert cleared, returns
    assert cleared <= set(SIGNOFF_FLAGS), cleared
    # The director's own kick-back out of DAT is one of them: sending work back
    # is not impersonating the review it discards.
    director_returns = [t for t in returns if "director" in t["actors"]]
    assert director_returns, returns
    for transition in director_returns:
        assert transition["to"] == "in_progress", transition


def test_an_override_cannot_raise_a_sign_off() -> None:
    """The narrated override moves tickets; it does not review them.

    SYRD-78 gave the declarative guard its force_move escape and refused a
    sign-off change under it in the same breath. This holds that shape: the
    escape and the refusal have to stay together, or the override becomes the
    way to manufacture the review it exists to route around.
    """
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    marker = "current_setting('ticket_board.force_move', true) = 'on'"
    assert marker in schema, "the declarative override escape is gone"
    guarded = schema.split(marker, 1)[1].split("RETURN proposed;", 1)[0]
    assert "a forced move cannot change sign-off" in guarded, guarded[:400]
    assert "'signoff'" in guarded, guarded[:400]


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ticket_board_signoff_attribution_test: ok ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
