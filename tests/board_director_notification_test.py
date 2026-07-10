#!/usr/bin/env python3
"""Regression tests: board-created tickets notify the director immediately."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


class FakeNotifier:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        self.created.append(
            {
                "id": str(ticket["id"]),
                "title": str(ticket["title"]),
            }
        )

    def close(self) -> None:
        return


class NonPersistingCreateApp(TicketBoardApp):
    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        screenshots: list[str] | None = None,
        assignee: str,
        needs_eric_signoff: bool,
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
    ) -> dict[str, object]:
        return {
            "id": "PGU-2",
            "title": title,
            "body": body,
            "assignee": assignee,
            "state": "analysis",
        }


def write_ticket(path: Path, ticket_id: str, title: str) -> None:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "app",
        "state": "analysis",
        "blocked_by": [],
        "parent_id": "",
        "blocked_reason": "",
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_eric_signoff": False,
        "eric_signoff": False,
        "commit_hash": "",
        "commit_exempt": False,
        "created": "2026-07-09T00:00:00+00:00",
        "updated": "2026-07-09T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_server_create_notifies_director() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-server.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()

        notifier = FakeNotifier()
        server = TicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets), director_notifier=notifier)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "title": "Instant notify",
                    "body": "Created through API.",
                    "assignee": "app",
                    "needs_eric_signoff": False,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/tickets",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            assert notifier.created == [{"id": payload["ticket"]["id"], "title": "Instant notify"}], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_create_uses_new_high_water_mark_and_persists() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-high-water.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        write_ticket(store / "PGU-1.json", "PGU-1", "Bloom halo")
        write_ticket(store / "PGU-2.json", "PGU-2", "Dust perf")
        write_ticket(store / "PGU-174.json", "PGU-174", "Latest existing")

        notifier = FakeNotifier()
        server = TicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets), director_notifier=notifier)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "title": "Persisted after high watermark",
                    "body": "Created through API.",
                    "assignee": "app",
                    "needs_eric_signoff": False,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/tickets",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            assert payload["ticket"]["id"] == "PGU-175", payload
            persisted = json.loads((store / "PGU-175.json").read_text(encoding="utf-8"))
            assert persisted["id"] == "PGU-175"
            assert persisted["title"] == "Persisted after high watermark"
            assert notifier.created == [{"id": "PGU-175", "title": "Persisted after high watermark"}], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_create_does_not_notify_when_ticket_is_not_persisted() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-no-persist.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        write_ticket(store / "PGU-1.json", "PGU-1", "Existing ticket")

        notifier = FakeNotifier()
        server = TicketBoardServer(("127.0.0.1", 0), NonPersistingCreateApp(store, frames, assets), director_notifier=notifier)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "title": "Phantom create",
                    "body": "Should fail",
                    "assignee": "app",
                    "needs_eric_signoff": False,
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/tickets",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=5)
            except HTTPError as exc:
                assert exc.code == 400
                assert "created ticket was not persisted" in exc.read().decode("utf-8")
            else:
                raise AssertionError("non-persisted create should fail")
            assert notifier.created == [], notifier.created
            assert not (store / "PGU-2.json").exists()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_director_notifier_batches_quick_creates() -> None:
    sent: list[str] = []
    notifier = DirectorNotifier(sender=sent.append, batch_window_seconds=0.05)
    notifier.notify_ticket_created({"id": "PGU-75", "title": "Alpha"})
    notifier.notify_ticket_created({"id": "PGU-76", "title": "Beta"})
    time.sleep(0.2)
    notifier.close()
    assert sent == ["New tickets for you: PGU-75 -- Alpha; PGU-76 -- Beta"], sent


def main() -> int:
    test_server_create_notifies_director()
    test_server_create_uses_new_high_water_mark_and_persists()
    test_server_create_does_not_notify_when_ticket_is_not_persisted()
    test_director_notifier_batches_quick_creates()
    print("board_director_notification_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
