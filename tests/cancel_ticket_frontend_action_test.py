#!/usr/bin/env python3
"""Regression test: Cancel asks for a reason and calls the cancel action, confirmed by the User (SYRD-598)."""

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
from tests.board_ui_stub import cancel_op, stub_board_ui


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


class CancelBoardApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.update_calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.tickets: list[dict[str, Any]] = [
            {
                "id": "PGU-293",
                "title": "Cancel from UI",
                "body": "Verify the Cancel Ticket button sends a reason.",
                "assignee": "ops",
                "state": "analysis",
                "blocked_by": [],
                "blocked_reason": "",
                "implementation": "Ready.",
                "audit_prompt": "",
                "audit_signoff": False,
                "needs_inspection": False,
                "inspector_signoff": False,
                "needs_user_signoff": False,
                "user_signoff": False,
                "commit_hash": "",
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

    def store_signature(self) -> tuple[tuple[str, str, int], ...]:
        return tuple((str(ticket["id"]), str(ticket["state"]), len(ticket["comments"])) for ticket in self.tickets)

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

    def cancel(self, ticket_id: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        self.update_calls.append((ticket_id, payload, caller_role))
        assert ticket_id == "PGU-293", ticket_id
        assert caller_role == "user", caller_role
        assert payload == {"reason": "No longer needed."}, payload
        ticket = self.tickets[0]
        ticket["state"] = "cancelled"
        ticket["comments"].append({"who": "user", "text": "No longer needed.", "ts": iso_now()})
        ticket["updated"] = iso_now()
        return ticket


def run_browser_check(playwright: object, server_port: int, app: CancelBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        # SYRD-598: cancelling is a Director operation the board describes with its required reason, and the
        # User confirms; the confirmed request carries the grant (tests/board_ui_stub.py stands in for it).
        seen = stub_board_ui(page, lambda: app.tickets, lambda ticket: [cancel_op(ticket)],
                             perform=lambda ticket_id, _op, payload: app.cancel(ticket_id, payload, caller_role="user"))
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Triage").wait_for(timeout=5000)
        page.locator(".card", has_text="PGU-293").click()

        page.locator("[data-board-ui-op='cancel']").click()
        dialog = page.locator("[data-board-ui-dialog='cancel']")
        dialog.locator("[data-board-ui-review]").click()
        dialog.locator("[data-board-ui-error]", has_text="Why is required.").wait_for(timeout=5000)
        assert app.update_calls == [] and not [s for s in seen if not s.get("describe")]
        assert app.tickets[0]["state"] == "analysis"

        dialog.locator("[data-board-ui-field='reason']").fill("No longer needed.")
        dialog.locator("[data-board-ui-review]").click()
        summary = dialog.locator("[data-board-ui-summary]")
        summary.wait_for(timeout=5000)
        assert "Why: No longer needed." in summary.inner_text(), summary.inner_text()
        dialog.locator("[data-board-ui-confirm]").click()
        page.wait_for_function(
            "() => document.getElementById('createStatus').textContent.includes('Cancel: done for PGU-293.')",
            timeout=5000,
        )
        assert len(app.update_calls) == 1
        assert app.tickets[0]["state"] == "cancelled"
        assert app.tickets[0]["comments"][-1]["text"] == "No longer needed."
    finally:
        browser.close()


def main() -> int:
    assert "await updateTicketAction(ticketId, 'cancel', { reason: actionReason(patch) }, normalizedCaller);" in HTML
    assert "await cancelTicket(ticket.id, cancelReason(commentText.value));" not in HTML
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = CancelBoardApp(frames, assets)
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

    print("cancel_ticket_frontend_action_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
