#!/usr/bin/env python3
"""Regression tests: board-created tickets notify the director immediately."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
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
    test_director_notifier_batches_quick_creates()
    print("board_director_notification_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
