#!/usr/bin/env python3
"""Regression test: Add Blocker persists from the real browser detail UI."""

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


class BlockerBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.tickets: list[dict[str, Any]] = [
            self.ticket("PGU-503", "Target ticket", state="analysis", assignee="ops"),
            self.ticket("PGU-502", "Source blocker", state="analysis", assignee="app"),
            self.ticket("PGU-504", "Newest candidate", state="done", assignee="main"),
        ]

    def ticket(self, ticket_id: str, title: str, *, state: str, assignee: str) -> dict[str, Any]:
        return {
            "id": ticket_id,
            "title": title,
            "body": f"Body for {ticket_id}",
            "assignee": assignee,
            "state": state,
            "blocked_by": [],
            "blockers": [],
            "blocked_reason": "",
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

    def store_signature(self) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return tuple((str(ticket["id"]), str(ticket["updated"]), tuple(ticket["blocked_by"])) for ticket in self.tickets)

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
        assert ticket_id == "PGU-503", ticket_id
        assert caller_role == "director", caller_role
        ticket = next(item for item in self.tickets if item["id"] == ticket_id)
        if len(self.update_calls) == 1:
            assert patch == {"blocked_by": ["PGU-502"], "blocked_reason": "Waiting on PGU-502."}, patch
            ticket["blocked_by"] = list(patch["blocked_by"])
            ticket["blockers"] = [{"id": "PGU-502", "resolved": False}]
            ticket["blocked_reason"] = patch["blocked_reason"]
        else:
            assert patch == {"blocked_by": [], "blocked_reason": ""}, patch
            ticket["blocked_by"] = []
            ticket["blockers"] = []
            ticket["blocked_reason"] = ""
        ticket["updated"] = iso_now()
        return ticket


def run_browser_check(playwright: object, server_port: int, app: BlockerBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-503")).click()
        page.locator(".detail-modal").wait_for(timeout=5000)

        blocker_picker = page.locator(".detail-modal select", has=page.locator("option[value='PGU-502']"))
        blocker_picker.select_option("PGU-502")
        page.locator(".detail-modal").get_by_role("button", name="Add Blocker").click()

        page.wait_for_function(
            "() => document.querySelector('.detail-modal')?.textContent.includes('Waiting on PGU-502.')",
            timeout=5000,
        )
        assert page_errors == []
        assert len(app.update_calls) == 1
        assert app.tickets[0]["blocked_by"] == ["PGU-502"]
        assert app.tickets[0]["blocked_reason"] == "Waiting on PGU-502."
        page.wait_for_function(
            "() => document.querySelector('.detail-modal')?.textContent.includes('PGU-502')",
            timeout=5000,
        )

        page.locator(".detail-modal").get_by_role("button", name="Remove Blocker PGU-502").click()
        page.wait_for_function(
            "() => !document.querySelector('.detail-modal')?.textContent.includes('Waiting on PGU-502.')",
            timeout=5000,
        )
        assert len(app.update_calls) == 2
        assert app.tickets[0]["blocked_by"] == []
        assert app.tickets[0]["blocked_reason"] == ""
    finally:
        browser.close()


def main() -> int:
    assert "persistDetailDraftField(" not in HTML
    assert "await updateTicket(ticket.id, {" in HTML
    assert "blocked_by: nextBlockedBy," in HTML
    assert "blocked_reason: blockedReasonValue," in HTML
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="blocked-by-browser.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = BlockerBoardApp(frames, assets)
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

    print("blocked_by_frontend_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
