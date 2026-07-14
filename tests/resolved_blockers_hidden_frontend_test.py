#!/usr/bin/env python3
"""Regression test: resolved blockers disappear from UI unless the ticket is manually held."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

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


class StaticBoardApp:
    store_backend = "postgres"

    def __init__(self, tickets: list[dict[str, object]], frames: Path, assets: Path) -> None:
        self.tickets = tickets
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(ticket["id"]), str(ticket["updated"])) for ticket in self.tickets)

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


def ticket_payload(
    ticket_id: str,
    title: str,
    *,
    blocked_reason: str,
    blockers: list[dict[str, object]],
    manually_controlled: bool = False,
    parked: bool = False,
) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "ops",
        "state": "in_progress",
        "blocked_by": [str(blocker["id"]) for blocker in blockers],
        "blockers": blockers,
        "blocked_reason": blocked_reason,
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_inspection": False,
        "inspector_signoff": False,
        "needs_eric_signoff": False,
        "eric_signoff": False,
        "manually_controlled": manually_controlled,
        "parked": parked,
        "commit_hash": "",
        "commit_exempt": False,
        "parent_id": "",
        "created": "2026-07-14T00:00:00+00:00",
        "updated": "2026-07-14T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
        "screenshots_info": [],
        "screenshot": None,
        "screenshot_available": False,
    }


def run_browser_check(playwright: object, server_port: int) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Implementation").wait_for(timeout=5000)
        page.wait_for_function("() => document.querySelectorAll('.card').length === 2")

        stale_card = page.locator(".card", has=page.locator(".card-id", has_text="PGU-394"))
        held_card = page.locator(".card", has=page.locator(".card-id", has_text="PGU-395"))

        assert "card-blocked" not in (stale_card.get_attribute("class") or "")
        assert stale_card.locator(".card-alert-blocked").count() == 0

        assert "card-blocked" in (held_card.get_attribute("class") or "")
        held_alert = held_card.locator(".card-alert-blocked")
        assert held_alert.count() == 1
        assert "needs driver PGU-393" in held_alert.inner_text()

        stale_card.click()
        page.locator(".detail-modal").wait_for(timeout=5000)
        assert page.locator(".detail-modal .alert-blocked").count() == 0
        page.locator("#detailCloseBtn").click()
        page.locator(".detail-modal").wait_for(state="hidden", timeout=5000)

        held_card.click()
        page.locator(".detail-modal").wait_for(timeout=5000)
        detail_alert = page.locator(".detail-modal .alert-blocked")
        assert detail_alert.count() == 1
        assert "needs driver PGU-393" in detail_alert.inner_text()
    finally:
        browser.close()


def main() -> int:
    assert "const visibleBlockedBy = unresolvedBlockedBy(ticket);" in HTML
    assert "linkedTicketRow(visibleBlockedBy)" in HTML
    assert "const manuallyHeld = !!ticket.manually_controlled || !!ticket.parked;" in HTML
    assert "if (reason && manuallyHeld) {" in HTML
    assert "return 'No ticket blockers recorded.';" in HTML
    assert "return `Blocked by: ${formatBlockedByList(unresolved)}`;" in HTML
    assert "Resolved blockers:" not in HTML
    assert "Unresolved blockers:" not in HTML

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="resolved-blockers-hidden.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        tickets = [
            ticket_payload(
                "PGU-394",
                "Resolved-only blockers should look clear",
                blocked_reason="needs driver PGU-393 before continuing",
                blockers=[{"id": "PGU-393", "resolved": True}],
            ),
            ticket_payload(
                "PGU-395",
                "Manually controlled stall should still show blocked",
                blocked_reason="needs driver PGU-393 before continuing",
                blockers=[{"id": "PGU-393", "resolved": True}],
                manually_controlled=True,
            ),
        ]

        app = StaticBoardApp(tickets, frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("resolved_blockers_hidden_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
