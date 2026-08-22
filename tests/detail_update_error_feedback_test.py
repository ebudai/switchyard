#!/usr/bin/env python3
"""Regression test: failed detail-panel saves surface visible errors."""

from __future__ import annotations

import sys
import tempfile
import threading
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


class RejectingDetailBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.tickets = [
            {
                "id": "PGU-511",
                "title": "Original title",
                "body": "",
                "assignee": "ops",
                "state": "analysis",
                "blocked_by": ["PGU-510"],
                "blockers": [{"id": "PGU-510", "resolved": False}],
                "blocked_reason": "Waiting on PGU-510.",
                "implementation": "",
                "audit_prompt": "",
                "audit_signoff": False,
                "needs_inspection": False,
                "inspector_signoff": False,
                "needs_user_signoff": False,
                "user_signoff": False,
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

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((ticket["id"], ticket["title"]) for ticket in self.tickets)

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
        if "title" in patch:
            raise ValueError("title update rejected by test server")
        if "blocked_by" in patch:
            raise ValueError("remove blocker rejected by test server")
        raise ValueError(f"unexpected update rejected by test server: {patch}")


def title_input(page: object) -> object:
    return page.get_by_role("textbox", name="Short issue title")


def run_browser_check(playwright: object, server_port: int, app: RejectingDetailBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-511")).click()
        page.locator(".detail-modal").wait_for(timeout=5000)

        title_input(page).fill("Rejected title")
        page.get_by_role("button", name="Save Title").click()

        page.wait_for_function(
            "() => document.getElementById('createStatus')?.textContent.includes('title update rejected by test server')",
            timeout=5000,
        )
        assert len(app.update_calls) == 1
        assert app.tickets[0]["title"] == "Original title"

        page.get_by_role("button", name="Remove Blocker PGU-510").click()
        page.wait_for_function(
            "() => document.getElementById('createStatus')?.textContent.includes('remove blocker rejected by test server')",
            timeout=5000,
        )
        assert len(app.update_calls) == 2
        assert app.tickets[0]["blocked_by"] == ["PGU-510"]
    finally:
        browser.close()


def main() -> int:
    assert "const updateDetailTicket = async (patch, callerRole = null) => {" in HTML
    assert "const updateDetailTicketForToggle = async (patch, callerRole = null) => {" in HTML
    assert "setCreateStatus(error.message, true);" in HTML
    assert "await requestBoardReload();" in HTML
    assert "await updateDetailTicket({ title: titleEditInput.value });" in HTML
    assert "await updateDetailTicket({\n              blocked_by: nextBlockedBy,\n            });" in HTML
    assert "await updateDetailTicketForToggle({ manually_controlled: checked }, 'director');" in HTML
    assert "await updateTicket(ticket.id, { title: titleEditInput.value }, detailCallerRole());" not in HTML
    assert "await updateTicket(ticket.id, {\n              blocked_by: nextBlockedBy," not in HTML

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="detail-update-error-feedback.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = RejectingDetailBoardApp(frames, assets)
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

    print("detail_update_error_feedback_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
