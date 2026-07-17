#!/usr/bin/env python3
"""Regression test: detail UI exposes a director manual-control toggle."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.frontend import HTML
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


def load_playwright() -> object:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        candidates = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
        candidates.extend(sorted(Path("/tmp/pgu-playwright-venv/lib").glob("python*/site-packages")))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


class ManualControlBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.tickets = [
            {
                "id": "PGU-508",
                "title": "Manual control toggle",
                "body": "",
                "assignee": "ops",
                "state": "analysis",
                "blocked_by": [],
                "blockers": [],
                "blocked_reason": "Manually held for director review.",
                "implementation": "",
                "audit_prompt": "",
                "audit_signoff": False,
                "needs_inspection": False,
                "inspector_signoff": False,
                "needs_eric_signoff": False,
                "eric_signoff": False,
                "manually_controlled": False,
                "parked": False,
                "commit_hash": "",
                "commit_exempt": False,
                "parent_id": "",
                "created": "2026-07-17T00:00:00+00:00",
                "updated": "2026-07-17T00:00:00+00:00",
                "comments": [],
                "screenshots": [],
                "screenshots_info": [],
                "screenshot": None,
                "screenshot_available": False,
            }
        ]

    def store_signature(self) -> tuple[tuple[str, str, bool], ...]:
        return tuple((ticket["id"], ticket["updated"], ticket["manually_controlled"]) for ticket in self.tickets)

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": self.tickets,
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

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        self.update_calls.append((ticket_id, patch, caller_role))
        assert ticket_id == "PGU-508", ticket_id
        assert patch == {"manually_controlled": True}, patch
        assert caller_role == "director", caller_role
        ticket = self.tickets[0]
        ticket["manually_controlled"] = True
        ticket["updated"] = iso_now()
        return ticket


def run_browser_check(playwright: object, server_port: int, app: ManualControlBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-508")).click()
        page.locator(".detail-modal").wait_for(timeout=5000)

        manual_toggle = page.get_by_role("checkbox", name="Manual control")
        assert not manual_toggle.is_checked()
        manual_toggle.check()

        deadline = time.monotonic() + 5
        while len(app.update_calls) < 1 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert len(app.update_calls) == 1
        assert app.tickets[0]["manually_controlled"] is True
    finally:
        browser.close()


def main() -> int:
    assert "toggleControl('Manual control', ticket.manually_controlled" in HTML
    assert "await updateTicket(ticket.id, { manually_controlled: checked }, 'director');" in HTML
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="manual-control-toggle.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = ManualControlBoardApp(frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port, app)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("manual_control_toggle_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
