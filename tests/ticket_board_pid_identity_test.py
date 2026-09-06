#!/usr/bin/env python3
"""Regression tests for Unix-socket board caller identity.

Authority is the peer's Unix uid: the kernel sets it, the caller cannot choose
it, and the uid-to-role table belongs to the board rather than to the roles.
These tests include the adversarial cases -- a role uid reaching for another
role, before and after a board restart, with forged headers and payloads.
"""

from __future__ import annotations

import json
import logging
import os
import stat
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
from scripts.ticket_board.server import (
    CALLER_ROLE_HEADER,
    CallerIdentityError,
    LocalRoleAuthority,
    PANE_SOCKET_MODE,
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




# --- SYRD-39 role identity fixtures ----------------------------------------
# Each role runs as its own Unix account. Tests name accounts and uids
# explicitly rather than depending on accounts existing on the test host.

ROLE_ACCOUNTS = {
    "director": "syrd-director",
    "main": "syrd-main",
    "ops": "syrd-ops",
}
ROLE_UIDS = {"syrd-director": 4101, "syrd-main": 4102, "syrd-ops": 4103}
STRANGER_UID = 4999


def authority(accounts: dict[str, str] | None = None, uids: dict[str, int] | None = None) -> LocalRoleAuthority:
    table = ROLE_UIDS if uids is None else uids
    return LocalRoleAuthority(
        ROLE_ACCOUNTS if accounts is None else accounts,
        resolve_uid=lambda account: table.get(account),
    )


def authority_as(role: str) -> LocalRoleAuthority:
    """An authority in which THIS process's uid is the given role's account."""
    account = ROLE_ACCOUNTS[role]
    return authority(uids={**ROLE_UIDS, account: os.getuid()})


def assert_uid_decides_the_role() -> None:
    auth = authority()
    assert auth.role_for_uid(ROLE_UIDS["syrd-director"]) == "director"
    assert auth.role_for_uid(ROLE_UIDS["syrd-ops"]) == "ops"
    assert auth.uid_for_role("main") == ROLE_UIDS["syrd-main"]
    assert auth.account_for_role("main") == "syrd-main"
    assert auth.roles() == ["director", "main", "ops"]


def assert_unconfigured_account_gets_no_role() -> None:
    auth = authority()
    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        try:
            auth.role_for_uid(STRANGER_UID)
        except CallerIdentityError:
            pass
        else:
            raise AssertionError("an unconfigured uid must not receive a role")
        assert any("not a configured role account" in m for m in handler.messages), handler.messages
    finally:
        logger.removeHandler(handler)


def assert_role_authority_survives_a_board_restart() -> None:
    """The escalation Audit reproduced against the previous design.

    Nothing is remembered between requests, so a restart cannot make a role
    vacant. A fresh authority answers exactly as the old one did.
    """
    before = authority()
    assert before.role_for_uid(ROLE_UIDS["syrd-ops"]) == "ops"

    after = authority()  # board service restarted; role processes untouched
    assert after.role_for_uid(ROLE_UIDS["syrd-ops"]) == "ops"
    assert after.role_for_uid(ROLE_UIDS["syrd-director"]) == "director"
    # The ops account cannot become director before or after the restart,
    # because there is no claim to make.
    assert after.uid_for_role("director") != ROLE_UIDS["syrd-ops"]


def assert_roles_sharing_one_account_are_refused_not_guessed() -> None:
    """A half-migrated tenant must fail closed, not pick a role."""
    shared = {"director": "syrd-shared", "ops": "syrd-shared"}
    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        auth = LocalRoleAuthority(shared, resolve_uid=lambda account: 4200)
        assert auth.shared_accounts() == {"syrd-shared": ["director", "ops"]}, auth.shared_accounts()
        try:
            auth.role_for_uid(4200)
        except CallerIdentityError:
            pass
        else:
            raise AssertionError("a uid backing two roles must not resolve to either")
        assert any("each role has its own Unix account" in m for m in handler.messages), handler.messages
    finally:
        logger.removeHandler(handler)


def assert_missing_account_grants_nothing() -> None:
    auth = LocalRoleAuthority({"director": "syrd-director"}, resolve_uid=lambda account: None)
    assert auth.unresolved_roles() == {"director": "syrd-director"}
    assert auth.uid_for_role("director") is None
    try:
        auth.role_for_uid(4101)
    except CallerIdentityError:
        pass
    else:
        raise AssertionError("an unresolved account must grant nothing")


def assert_role_accounts_are_read_from_configuration() -> None:
    """Roles are data. Nothing here knows what a director is."""
    accounts = {"proj-reviewer": 4301, "proj-scribe": 4302}
    auth = LocalRoleAuthority.from_environ(
        {"TICKET_BOARD_ROLE_ACCOUNTS": "reviewer=proj-reviewer, scribe=proj-scribe"},
        resolve_uid=accounts.get,
    )
    assert auth.roles() == ["reviewer", "scribe"], auth.roles()
    assert auth.role_for_uid(4301) == "reviewer"
    empty = LocalRoleAuthority.from_environ({}, resolve_uid=accounts.get)
    assert empty.roles() == []


def assert_forged_role_claim_is_refused_over_the_socket() -> None:
    """The adversarial case: a process running as the ops account asks for director."""
    with tempfile.TemporaryDirectory(prefix="ticket-board-uid.") as tmpdir:
        root = Path(tmpdir)
        frames, assets = root / "frames", root / "assets"
        frames.mkdir()
        assets.mkdir()
        socket_path = root / "board.sock"
        app = MemoryBoardApp(
            [
                ticket_payload("PGU-300", title="Ops work", state="in_progress", assignee="ops", implementation="Ready."),
                ticket_payload("PGU-301", title="Director work", state="analysis"),
            ],
            frames,
            assets,
        )
        events = TicketBoardEventHub(app)
        # This test process stands in for a command running as the ops account.
        auth = authority_as("ops")
        server = TicketBoardUnixServer(
            socket_path, app, events=events, director_notifier=QuietNotifier(), role_authority=auth
        )
        socket_mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert socket_mode == PANE_SOCKET_MODE == 0o660, oct(socket_mode)
        assert not socket_mode & stat.S_IWOTH, oct(socket_mode)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            # Announcing its own role is fine and changes nothing.
            status, body = request_unix(socket_path, "/api/register-caller", {"role": "ops"})
            assert status == 200, (status, body)
            assert '"role": "ops"' in body, body

            # Announcing another role is refused, and says which account it is.
            status, body = request_unix(socket_path, "/api/register-caller", {"role": "director"})
            assert status == 403, (status, body)
            assert "registered as ops" in body, body

            # A director-only action with a forged header still executes as ops.
            status, body = request_unix(
                socket_path,
                "/api/tickets/PGU-301/actions/route",
                {"state": "backlog", "assignee": "ops"},
                caller_header="director",
            )
            assert status == 403, (status, body)
            assert "ops cannot call route" in body, body

            # The account's own work is unaffected.
            status, body = request_unix(socket_path, "/api/tickets/PGU-300/actions/add_comment", {"text": "ops note"})
            assert status == 200, (status, body)

            # Restart the board; the forged claim is refused exactly as before,
            # with no window in which director is unheld.
            server.shutdown()
            thread.join(timeout=2)
            restarted = TicketBoardUnixServer(
                socket_path, app, events=events, director_notifier=QuietNotifier(), role_authority=authority_as("ops")
            )
            restart_thread = threading.Thread(target=restarted.serve_forever, daemon=True)
            restart_thread.start()
            try:
                status, body = request_unix(socket_path, "/api/register-caller", {"role": "director"})
                assert status == 403, (status, body)
                assert "registered as ops" in body, body
            finally:
                restarted.shutdown()
                restarted.server_close()
                restart_thread.join(timeout=2)
        finally:
            server.server_close()
            events.close()
            thread.join(timeout=2)


def assert_peer_from_an_unconfigured_account_cannot_write() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-stranger.") as tmpdir:
        root = Path(tmpdir)
        frames, assets = root / "frames", root / "assets"
        frames.mkdir()
        assets.mkdir()
        socket_path = root / "board.sock"
        app = MemoryBoardApp([ticket_payload("PGU-400", title="Work", state="analysis")], frames, assets)
        events = TicketBoardEventHub(app)
        # No account in the table matches this process's uid.
        server = TicketBoardUnixServer(
            socket_path, app, events=events, director_notifier=QuietNotifier(), role_authority=authority()
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = request_unix(
                socket_path,
                "/api/tickets/PGU-400/actions/route",
                {"state": "backlog", "assignee": "ops"},
                caller_header="director",
            )
            assert status == 403, (status, body)
            assert "not a configured role" in body, body
        finally:
            server.shutdown()
            server.server_close()
            events.close()
            thread.join(timeout=2)


def assert_peer_uid_allowlist_admits_only_the_tenant() -> None:
    assert allowed_peer_uids({}) == set()
    configured = allowed_peer_uids({"TICKET_BOARD_ALLOWED_PEER_UIDS": "1006"})
    assert 1006 in configured, configured
    assert os.getuid() in configured, configured
    assert 4242 not in configured, configured
    unknown = allowed_peer_uids({"TICKET_BOARD_TENANT_USER": "definitely-not-a-local-account"})
    assert unknown == set(), unknown


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
        server = TicketBoardUnixServer(socket_path, app, events=events, director_notifier=QuietNotifier(), role_authority=authority_as("ops"))
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
    assert_uid_decides_the_role()
    assert_unconfigured_account_gets_no_role()
    assert_role_authority_survives_a_board_restart()
    assert_roles_sharing_one_account_are_refused_not_guessed()
    assert_missing_account_grants_nothing()
    assert_role_accounts_are_read_from_configuration()
    assert_peer_uid_allowlist_admits_only_the_tenant()
    assert_forged_role_claim_is_refused_over_the_socket()
    assert_peer_from_an_unconfigured_account_cannot_write()
    assert_unix_socket_replaces_stale_socket_file()
    print("ticket_board_pid_identity_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
