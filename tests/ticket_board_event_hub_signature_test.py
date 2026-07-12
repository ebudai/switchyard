#!/usr/bin/env python3
"""Regression test: board SSE wakes clients when the app signature changes."""

from __future__ import annotations

import sys
import time
import queue
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.server import TicketBoardEventHub


class SignatureApp:
    def __init__(self) -> None:
        self.signature = (("PGU-1", "trace-1"),)

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return self.signature


def main() -> int:
    app = SignatureApp()
    hub = TicketBoardEventHub(app, scan_interval_seconds=0.02)
    try:
        listener, initial_version = hub.register()
        app.signature = (("PGU-1", "trace-2"),)
        deadline = time.monotonic() + 1.0
        next_version = None
        while time.monotonic() < deadline:
            try:
                next_version = listener.get(timeout=0.1)
                break
            except queue.Empty:
                pass
        assert next_version is not None, "signature-only changes must wake board event clients"
        assert next_version > initial_version
    finally:
        hub.close()
    print("ticket_board_event_hub_signature_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
