#!/usr/bin/env python3
"""Regression test: failed workflow advances surface backend errors in the UI.

SYRD-598: moving a ticket on is a Director operation the User confirms in the board UI (Route, with its
stage and owner); the confirmed request carries the grant (tests/board_ui_stub.py stands in for it). When
the board refuses the move, the page says the board's reason and the ticket stays where it was.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.board_ui_stub import field, stub_board_ui  # noqa: E402
from tests.ticket_board_ui_harness import (  # noqa: E402
    BoardHarness,
    load_playwright,
    ticket_payload,
)


def route_op(ticket: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "route", "label": "Route", "mode": "delegated",
            "fields": [field("state", "choice", "To", options=["in_progress"], required=True),
                       field("assignee", "choice", "Assignee", options=["app", "ops"], required=True)]}


def run_browser_check(playwright: Any) -> None:
    tickets = [ticket_payload("PGU-789", "Transition failure", state="analysis", assignee="ops")]
    with BoardHarness(tickets) as harness:
        attempts: list[tuple[str, str, dict[str, Any]]] = []

        def refuse(ticket_id: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append((ticket_id, operation, payload))
            raise ValueError("backend refused this workflow transition")

        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            stub_board_ui(page, lambda: list(harness.app.tickets.values()), lambda ticket: [route_op(ticket)], perform=refuse)

            page.goto(harness.url, wait_until="domcontentloaded")
            page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
            page.locator(".card", has=page.locator(".card-id", has_text="PGU-789")).click()
            modal = page.locator(".detail-modal")
            modal.wait_for(timeout=5000)
            modal.locator("[data-board-ui-op='route']").click()
            dialog = page.locator("[data-board-ui-dialog='route']")
            dialog.locator("[data-board-ui-field='state']").select_option("in_progress")
            dialog.locator("[data-board-ui-field='assignee']").select_option("app")
            dialog.locator("[data-board-ui-review]").click()
            dialog.locator("[data-board-ui-confirm]").click()

            page.wait_for_function(
                "() => document.getElementById('createStatus')?.innerText.includes('backend refused this workflow transition')",
                timeout=5000,
            )
            page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
            assert harness.ticket("PGU-789")["state"] == "analysis"
            assert attempts == [("PGU-789", "route", {"state": "in_progress", "assignee": "app"})], attempts
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
