#!/usr/bin/env python3
"""Regression test: failed audit sign-off toggles revert and show an error."""

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
        repo_venv = Path.home() / "Projects" / "pgu" / ".venv" / "lib"
        candidates = sorted(repo_venv.glob("python*/site-packages"))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


class ToggleFailureBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.update_calls = 0
        self.tickets: list[dict[str, Any]] = [
            {
                "id": "PGU-279",
                "title": "Audit signoff comment",
                "body": "Verify failed signoff toggles do not lie.",
                "assignee": "audit",
                "state": "audit",
                "blocked_by": [],
                "blocked_reason": "",
                "implementation": "Done.",
                "audit_prompt": "",
                "audit_signoff": False,
                "needs_inspection": False,
                "inspector_signoff": False,
                "needs_eric_signoff": False,
                "eric_signoff": False,
                "commit_hash": "abcdef1",
                "commit_exempt": False,
                "parent_id": "",
                "created": "2026-07-12T00:00:00+00:00",
                "updated": "2026-07-12T00:00:00+00:00",
                "comments": [],
                "screenshots": [],
                "screenshots_info": [],
                "screenshot": None,
                "screenshot_available": False,
            }
        ]

    def store_signature(self) -> tuple[tuple[str, bool], ...]:
        return tuple((str(ticket["id"]), bool(ticket["audit_signoff"])) for ticket in self.tickets)

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
        self.update_calls += 1
        raise AssertionError(f"empty audit signoff should be rejected before update_ticket: {ticket_id} {patch} {caller_role}")


def run_browser_check(playwright: object, server_port: int, app: ToggleFailureBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Audit").wait_for(timeout=5000)
        page.locator(".card", has_text="PGU-279").click()

        audit_toggle = page.get_by_role("checkbox", name="Audit signoff")
        audit_toggle.wait_for(timeout=5000)
        assert audit_toggle.is_checked() is False

        audit_toggle.click()
        page.wait_for_function(
            "() => document.body.innerText.includes('audit_sign_off requires a non-empty comment')",
            timeout=5000,
        )
        assert audit_toggle.is_checked() is False
        assert app.tickets[0]["audit_signoff"] is False
        assert app.update_calls == 0
    finally:
        browser.close()


def main() -> int:
    assert "await updateTicketAction(ticketId, 'audit_sign_off', { text: actionReason(patch) }, normalizedCaller);" in HTML
    assert "await updateTicketAction(ticketId, 'audit_sign_off', {}, normalizedCaller);" not in HTML
    assert "input.checked = previousChecked;" in HTML

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = ToggleFailureBoardApp(frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                run_browser_check(p, server.server_port, app)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("audit_signoff_comment_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
