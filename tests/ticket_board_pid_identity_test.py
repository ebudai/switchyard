#!/usr/bin/env python3
"""Regression tests for Unix-socket PID-derived board caller identity."""

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
    CallerRegistry,
    PANE_SOCKET_MODE,
    PeerCredentials,
    TicketBoardEventHub,
    TicketBoardUnixServer,
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
        "needs_eric_signoff": False,
        "eric_signoff": False,
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
        ticket.update(patch)
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


def assert_registry_rejects_duplicate_live_role_and_releases_stale_pid() -> None:
    alive_pids = {1001, 1002}
    registry = CallerRegistry(pid_alive=lambda pid: pid in alive_pids)
    logger = logging.getLogger("scripts.ticket_board.server")
    handler = RecordingLogHandler()
    logger.addHandler(handler)
    try:
        registry.register(PeerCredentials(pid=1001, uid=1000, gid=1000), "ops")
        assert registry.pid_for_role("ops") == 1001
        try:
            registry.register(PeerCredentials(pid=1002, uid=1000, gid=1000), "ops")
        except PermissionError:
            pass
        else:
            raise AssertionError("duplicate live role registration should be rejected")
        assert any("Rejected caller registration for role ops" in message for message in handler.messages), handler.messages

        registry.release(1001)
        assert registry.pid_for_role("ops") is None

        registry.register(PeerCredentials(pid=1001, uid=1000, gid=1000), "ops")
        alive_pids.remove(1001)
        registry.register(PeerCredentials(pid=1002, uid=1000, gid=1000), "ops")
        assert registry.pid_for_role("ops") == 1002
    finally:
        logger.removeHandler(handler)


def assert_unix_socket_write_derives_role_and_releases_registration() -> None:
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
        registry = CallerRegistry()
        server = TicketBoardUnixServer(socket_path, app, events=events, director_notifier=QuietNotifier(), caller_registry=registry)
        socket_mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert socket_mode == PANE_SOCKET_MODE == 0o666, oct(socket_mode)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = TicketBoardWriteClient(socket_path=str(socket_path), caller_role="ops")
            started = client.start_work("PGU-300")["ticket"]
            assert started["state"] == "in_progress", started
            assert app.tickets["PGU-300"]["state"] == "in_progress"

            wait_until(lambda: registry.pid_for_role("ops") is None)

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
                assert registry.pid_for_role("ops") == os.getpid()

                conn.request(
                    "POST",
                    "/api/register-caller",
                    body=json.dumps({"role": "ops"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                assert response.status == 200, response.read().decode("utf-8")
                response.read()
                assert registry.pid_for_role("ops") == os.getpid()

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

            wait_until(lambda: registry.pid_for_role("ops") is None)

            status, body = request_unix(
                socket_path,
                "/api/tickets/PGU-301/actions/start_work",
                {},
                caller_header="ops",
            )
            assert status == 400, (status, body)
            assert "has not registered" in body, body
        finally:
            server.shutdown()
            server.server_close()
            events.close()
            thread.join(timeout=2)


def main() -> int:
    assert_registry_rejects_duplicate_live_role_and_releases_stale_pid()
    assert_unix_socket_write_derives_role_and_releases_registration()
    print("ticket_board_pid_identity_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
