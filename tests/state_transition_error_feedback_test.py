#!/usr/bin/env python3
"""Regression test: failed workflow advances surface backend errors in the UI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.ticket_board_ui_harness import (  # noqa: E402
    BoardHarness,
    InMemoryTicketBoardApp,
    load_playwright,
    ticket_payload,
)


class FailingAdvanceApp(InMemoryTicketBoardApp):
    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        if patch == {"state": "in_progress"}:
            self.operation_log.append(("start_work", str(ticket_id).strip().upper(), caller_role, {}))
            raise ValueError("backend refused this workflow transition")
        return super().update_ticket(ticket_id, patch, caller_role=caller_role)


class FailingAdvanceHarness(BoardHarness):
    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        super().__init__(tickets)
        self.app = FailingAdvanceApp(tickets, frame_dir=self.frames, asset_dir=self.assets)
        self.server.app = self.app


def run_browser_check(playwright: Any) -> None:
    tickets = [ticket_payload("PGU-789", "Transition failure", state="analysis", assignee="ops")]
    with FailingAdvanceHarness(tickets) as harness:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(harness.url, wait_until="domcontentloaded")
            page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
            page.locator(".card", has=page.locator(".card-id", has_text="PGU-789")).click()
            modal = page.locator(".detail-modal")
            modal.wait_for(timeout=5000)
            modal.get_by_role("button", name="Advance -> Implementation").click()

            page.wait_for_function(
                "() => document.getElementById('createStatus')?.innerText.includes('backend refused this workflow transition')",
                timeout=5000,
            )
            page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
            assert harness.ticket("PGU-789")["state"] == "analysis"
            assert harness.app.operation_log == [("start_work", "PGU-789", "ops", {})]
            assert page_errors == []
        finally:
            browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with sync_playwright() as playwright:
        run_browser_check(playwright)
    print("state_transition_error_feedback_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
