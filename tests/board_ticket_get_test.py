#!/usr/bin/env python3
"""Regression test: per-ticket GET reads return ticket JSON without hitting action routes."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import TicketBoardServer


class QuietNotifier:
    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return


class MemoryTicketApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.tickets = {
            "PGU-399": {
                "id": "PGU-399",
                "title": "GET ticket",
                "body": "Read-only endpoint coverage.",
                "assignee": "ops",
                "state": "in_progress",
                "blocked_by": [],
                "blockers": [],
                "parent_id": "",
                "blocked_reason": "",
                "implementation": "",
                "audit_prompt": "",
                "audit_signoff": False,
                "needs_inspection": False,
                "inspector_signoff": False,
                "needs_user_signoff": False,
                "user_signoff": False,
                "regression": False,
                "manually_controlled": False,
                "commit_hash": "",
                "commit_exempt": False,
                "created": "2026-07-14T00:00:00+00:00",
                "updated": "2026-07-14T00:00:00+00:00",
                "comments": [],
                "screenshots": [],
                "screenshots_info": [],
                "screenshot": None,
                "screenshot_available": False,
            }
        }

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

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((ticket_id, str(ticket["updated"])) for ticket_id, ticket in self.tickets.items()))

    def get_ticket(self, ticket_id: str) -> dict[str, object]:
        normalized = str(ticket_id).strip().upper()
        if normalized not in self.tickets:
            raise FileNotFoundError("ticket not found")
        return self.tickets[normalized]


def get_json(server: TicketBoardServer, path: str) -> dict[str, object]:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200, (path, response.status, body)
        return json.loads(body)


def get_error(server: TicketBoardServer, path: str, *, status: int) -> str:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == status, (path, exc.code, body)
        return body
    raise AssertionError(f"{path} unexpectedly succeeded")


def test_get_ticket_returns_ticket_json() -> None:
    with tempfile.TemporaryDirectory(prefix="board-ticket-get.") as tmpdir:
        root = Path(tmpdir)
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryTicketApp(root / "frames", root / "assets"),
            director_notifier=QuietNotifier(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = get_json(server, "/api/tickets/PGU-399")
            assert payload["id"] == "PGU-399", payload
            assert payload["title"] == "GET ticket", payload
            assert payload["assignee"] == "ops", payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_get_ticket_normalizes_lowercase_ids() -> None:
    with tempfile.TemporaryDirectory(prefix="board-ticket-get.") as tmpdir:
        root = Path(tmpdir)
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryTicketApp(root / "frames", root / "assets"),
            director_notifier=QuietNotifier(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = get_json(server, "/api/tickets/pgu-399")
            assert payload["id"] == "PGU-399", payload
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_get_ticket_returns_404_for_unknown_ticket() -> None:
    with tempfile.TemporaryDirectory(prefix="board-ticket-get.") as tmpdir:
        root = Path(tmpdir)
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryTicketApp(root / "frames", root / "assets"),
            director_notifier=QuietNotifier(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert get_error(server, "/api/tickets/PGU-999", status=404) == "ticket not found"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_get_ticket_does_not_treat_action_paths_as_ticket_reads() -> None:
    with tempfile.TemporaryDirectory(prefix="board-ticket-get.") as tmpdir:
        root = Path(tmpdir)
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryTicketApp(root / "frames", root / "assets"),
            director_notifier=QuietNotifier(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert get_error(server, "/api/tickets/PGU-399/actions/add_comment", status=404) == "Not found"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main() -> int:
    test_get_ticket_returns_ticket_json()
    test_get_ticket_normalizes_lowercase_ids()
    test_get_ticket_returns_404_for_unknown_ticket()
    test_get_ticket_does_not_treat_action_paths_as_ticket_reads()
    print("board_ticket_get_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
