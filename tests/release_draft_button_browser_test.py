#!/usr/bin/env python3
"""Regression test: draft Release to Triage clicks call release_draft."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML
from tests.ticket_board_ui_harness import BoardHarness, load_playwright, ticket_payload, wait_for_ticket_field


def open_ticket(page: Any, ticket_id: str) -> Any:
    page.locator(".column-title", has_text="Draft").wait_for(timeout=5000)
    page.locator(".card", has=page.locator(".card-id", has_text=ticket_id)).click()
    modal = page.locator(".detail-modal")
    modal.wait_for(timeout=5000)
    return modal


def run_success_check(playwright: Any) -> None:
    tickets = [ticket_payload("PGU-515", "Draft release button", state="draft", assignee="unassigned")]
    with BoardHarness(tickets) as harness:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(harness.url, wait_until="domcontentloaded")
            modal = open_ticket(page, "PGU-515")
            assert modal.get_by_role("combobox", name="Comment author").input_value() == "director"
            assert modal.get_by_placeholder("Add a comment or bounce-back note").input_value() == ""
            modal.get_by_role("button", name="Release to Triage").click()

            wait_for_ticket_field(
                page,
                harness.url,
                "PGU-515",
                "ticket.state === 'analysis' && ticket.assignee === 'unassigned' && ticket.comments.length === 0",
            )
            page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
            assert page_errors == []
            assert harness.app.operation_log == [("release_draft", "PGU-515", "director", {})]
            assert harness.app.action_log == [("PGU-515", "director", {"state": "analysis", "assignee": "unassigned"})]
        finally:
            browser.close()


def run_rbac_reject_check(playwright: Any) -> None:
    tickets = [ticket_payload("PGU-516", "Draft release forbidden", state="draft", assignee="unassigned")]
    with BoardHarness(tickets) as harness:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            page.goto(harness.url, wait_until="domcontentloaded")
            modal = open_ticket(page, "PGU-516")
            modal.get_by_role("combobox", name="Comment author").select_option("ops")
            modal.get_by_role("button", name="Release to Triage").click()

            page.locator("#createStatus", has_text="ops cannot call release_draft").wait_for(timeout=5000)
            wait_for_ticket_field(page, harness.url, "PGU-516", "ticket.state === 'draft' && ticket.assignee === 'unassigned'")
            assert page_errors == []
            assert harness.app.operation_log == []
            assert harness.app.action_log == []
        finally:
            browser.close()


def main() -> int:
    assert "releaseButton.textContent = 'Release to Triage';" in HTML
    assert "await updateTicketAction(ticketId, 'release_draft', {}, normalizedCaller);" in HTML
    sync_playwright = load_playwright()
    with sync_playwright() as playwright:
        run_success_check(playwright)
        run_rbac_reject_check(playwright)
    print("release_draft_button_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
