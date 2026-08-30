#!/usr/bin/env python3
"""Regression test: every state/gate-flag combination renders in one column."""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import STATES
from tests.ticket_board_ui_harness import BoardHarness, load_playwright, ticket_payload

GATE_FLAGS = ("needs_audit", "needs_inspection", "needs_user_signoff")


def build_ticket_matrix() -> list[dict[str, Any]]:
    tickets: list[dict[str, Any]] = []
    for ticket_id in ("PGU-645", "PGU-752"):
        tickets.append(
            ticket_payload(
                ticket_id,
                f"{ticket_id} synthetic user-review visibility guard",
                state="user_review",
                assignee="user",
                needs_audit=True,
                needs_inspection=False,
                needs_user_signoff=False,
            )
        )
    next_id = 799000
    for state in STATES:
        for values in itertools.product((False, True), repeat=len(GATE_FLAGS)):
            flags = dict(zip(GATE_FLAGS, values, strict=True))
            tickets.append(
                ticket_payload(
                    f"PGU-{next_id}",
                    "Visibility matrix "
                    + state
                    + " "
                    + " ".join(f"{name}={value}" for name, value in flags.items()),
                    state=state,
                    assignee="user" if state == "user_review" else "ops",
                    **flags,
                )
            )
            next_id += 1
    return tickets


def run_browser_check(playwright: object, harness: BoardHarness, expected_ids: list[str]) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(harness.url, wait_until="domcontentloaded")
        page.locator(".column-title", has_text="UAT").wait_for(timeout=5000)
        page.locator("#showDeferredInput").check()
        page.locator("#showDoneInput").check()
        page.locator("#showCancelledInput").check()
        page.wait_for_function("() => document.querySelectorAll('.column').length === 11")

        placements = page.evaluate(
            """() => {
              const result = {};
              document.querySelectorAll('.column').forEach((column) => {
                const columnTitle = column.querySelector('.column-title')?.textContent || '';
                column.querySelectorAll('.card-id').forEach((cardId) => {
                  const ticketId = cardId.textContent || '';
                  if (!ticketId) {
                    return;
                  }
                  if (!result[ticketId]) {
                    result[ticketId] = [];
                  }
                  result[ticketId].push(columnTitle);
                });
              });
              return result;
            }"""
        )
    finally:
        browser.close()

    invisible = [ticket_id for ticket_id in expected_ids if ticket_id not in placements]
    duplicated = {ticket_id: columns for ticket_id, columns in placements.items() if len(columns) != 1}
    assert not invisible, f"tickets rendered in zero columns: {invisible}"
    assert not duplicated, f"tickets rendered in multiple columns: {duplicated}"


def main() -> int:
    try:
        sync_playwright = load_playwright()
    except ModuleNotFoundError:
        print(
            "ticket_board_state_gate_visibility_test: skipped, missing Playwright Python package; "
            "install playwright and browser binaries to run browser UI suites"
        )
        return 0

    tickets = build_ticket_matrix()
    with BoardHarness(tickets) as harness:
        with sync_playwright() as playwright:
            run_browser_check(playwright, harness, [str(ticket["id"]) for ticket in tickets])
    print("ticket_board_state_gate_visibility_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
