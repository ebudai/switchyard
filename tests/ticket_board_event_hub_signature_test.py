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
        self.failure: Exception | None = None

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        if self.failure is not None:
            raise self.failure
        return self.signature


def reconciler_survives_and_reports_a_failing_scan() -> None:
    """A reconciler that dies quietly leaves a board that looks current.

    Before this, one raised scan ended the watch thread for the life of the
    process: pushes still worked, reconciliation silently did not, and an open
    board showed no sign of it.
    """
    app = SignatureApp()
    hub = TicketBoardEventHub(app, scan_interval_seconds=0.02)
    try:
        listener, _version = hub.register()
        app.failure = RuntimeError("postgres is unreachable")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not hub.degraded_reason:
            time.sleep(0.02)
        assert hub._thread.is_alive(), "a failing scan killed the reconciler"
        assert hub.degraded_reason == "postgres is unreachable", hub.degraded_reason
        # Open clients are woken so they can say the board may be stale.
        assert listener.get(timeout=1.0)

        app.failure = None
        app.signature = (("PGU-1", "trace-3"),)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and hub.degraded_reason:
            time.sleep(0.02)
        assert not hub.degraded_reason, "recovery was never reported"
        assert listener.get(timeout=1.0), "a change after recovery must still notify"
    finally:
        hub.close()


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
    reconciler_survives_and_reports_a_failing_scan()
    print("ticket_board_event_hub_signature_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
