#!/usr/bin/env python3
"""Regression test: UAT Return for Rework uses User's reopen action, not route."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML
from tests.ticket_board_ui_harness import BoardHarness, load_playwright, ticket_payload, wait_for_ticket_field


def run_browser_check(playwright: Any, harness: BoardHarness) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(harness.url, wait_until="domcontentloaded")
        page.locator(".column-title", has_text="UAT").wait_for(timeout=5000)
        page.locator(".card", has=page.locator(".card-id", has_text="PGU-512")).click()
        modal = page.locator(".detail-modal")
        modal.wait_for(timeout=5000)

        author = modal.get_by_role("combobox", name="Comment author")
        assert author.input_value() == "user"
        assert modal.get_by_placeholder("Add a comment or bounce-back note").input_value() == ""
        modal.get_by_role("button", name="Return for Rework").click()

        wait_for_ticket_field(page, harness.url, "PGU-512", "ticket.state === 'analysis' && ticket.comments.length === 0")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        assert page_errors == []
        assert harness.app.action_log == [("PGU-512", "user", {"state": "analysis"})]
    finally:
        browser.close()


def main() -> int:
    assert "await updateTicketAction(ticketId, 'user_reopen', { reason: actionReason(patch) }, normalizedCaller);" in HTML
    assert "nextState === 'analysis' &&\n          patch.comment &&" not in HTML
    sync_playwright = load_playwright()
    tickets = [
        ticket_payload(
            "PGU-512",
            "Return for Rework",
            state="user_review",
            assignee="director",
            needs_user_signoff=True,
        )
    ]
    with BoardHarness(tickets) as harness:
        with sync_playwright() as playwright:
            run_browser_check(playwright, harness)
    print("user_reopen_return_rework_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
