#!/usr/bin/env python3
"""A real browser keeps up with the board, and is not told the board's secrets.

Two things the server-side integration test cannot show:

- the client's own reload queue. A board event that arrives while a fetch is
  already in flight must still render, and only the browser runs that loop.
- what a person actually sees when live updates fail. The reason travels to
  every open client, so a database error's text -- routinely carrying the
  connection string, user and database name -- must never be in it.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tests.ticket_board_ui_harness import BoardHarness, load_playwright, ticket_payload  # noqa: E402
from scripts.ticket_board.server import TicketBoardEventHub  # noqa: E402

TICKET_ID = "PGU-1"
SECRET = "postgresql://board_user:hunter2@/tenant_db?host=/run/postgresql"
SECRET_PARTS = (SECRET, "hunter2", "board_user", "tenant_db")


class GatedSnapshot:
    """Holds one board fetch open, so a later event lands mid-flight."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._inner = app.snapshot
        self._armed = threading.Event()
        self.entered = threading.Event()
        self._release = threading.Event()
        app.snapshot = self  # type: ignore[method-assign]

    def __call__(self) -> dict[str, object]:
        if not self._armed.is_set():
            return self._inner()
        self._armed.clear()
        # Taken now and delivered later, so the held response is genuinely
        # stale. Snapshotting after the release would hand the client the
        # newest state and the reload queue would never be needed -- which is
        # how the first version of this test passed without it.
        held = self._inner()
        self.entered.set()
        self._release.wait(timeout=20.0)
        return held

    def arm(self) -> None:
        self.entered.clear()
        self._release.clear()
        self._armed.set()

    def release(self) -> None:
        self._release.set()


def _sync_frame(base_url: str, *, degraded: bool, timeout: float = 25.0) -> dict[str, Any]:
    """Read sync frames off the wire until one reports the wanted health."""
    frames: queue.Queue[dict[str, Any]] = queue.Queue()

    def read() -> None:
        try:
            with urllib.request.urlopen(f"{base_url}events", timeout=timeout) as response:
                event = ""
                for raw in response:
                    line = raw.decode("utf-8").rstrip("\n")
                    if line.startswith("event: "):
                        event = line.removeprefix("event: ").strip()
                    elif line.startswith("data: ") and event == "sync":
                        payload = json.loads(line.removeprefix("data: "))
                        if bool(payload.get("degraded")) is degraded:
                            frames.put({"raw": line, "payload": payload})
                            return
        except Exception:  # noqa: BLE001 - reported by the empty queue
            return

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        return frames.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"the stream never reported degraded={degraded}")


def _await(predicate: Any, *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(message)


def run_browser_check(playwright: object) -> None:
    tickets = [ticket_payload(TICKET_ID, title="first title", state="analysis", assignee="director")]
    with BoardHarness(tickets) as harness:
        gate = GatedSnapshot(harness.app)
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(harness.url, wait_until="networkidle")
            page.wait_for_function("() => document.body.innerText.includes('first title')", timeout=10000)

            # A second event while the first reload is still in flight.
            gate.arm()
            harness.app.tickets[TICKET_ID]["title"] = "second title"
            harness.server.events.notify_change()
            assert gate.entered.wait(timeout=10), "the board never started the first reload"

            harness.app.tickets[TICKET_ID]["title"] = "third title"
            harness.server.events.notify_change()
            time.sleep(0.3)  # the queued event lands while the fetch is held
            gate.release()

            # The held fetch answers with the second title; only the queued
            # reload can produce the third, and no one touched the browser.
            page.wait_for_function(
                "() => document.body.innerText.includes('third title')", timeout=15000
            )
            assert "second title" not in page.inner_text("body")

            # Live updates fail: the person sees that, and not the conninfo.
            def explode() -> None:
                raise RuntimeError(f"connection failed: {SECRET}")

            harness.app.store_signature = lambda: explode()  # type: ignore[method-assign]
            _await(
                lambda: bool(harness.server.events.degraded_reason),
                timeout=20.0,
                message="repeated scan failures were never treated as degraded",
            )
            frame = _sync_frame(harness.url, degraded=True)
            assert frame["payload"]["degraded"] is True, frame
            assert frame["payload"]["reason"] == TicketBoardEventHub.DEGRADED_REASON
            for leaked in SECRET_PARTS:
                assert leaked not in frame["raw"], f"{leaked!r} reached an SSE frame"

            page.wait_for_function(
                "() => document.body.innerText.includes('Live updates are not reaching this board')",
                timeout=20000,
            )
            rendered = page.inner_text("body")
            for leaked in SECRET_PARTS:
                assert leaked not in rendered, f"{leaked!r} was rendered to the operator"
        finally:
            page.close()
            browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with sync_playwright() as playwright:
        run_browser_check(playwright)
    print("board_live_sync_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
