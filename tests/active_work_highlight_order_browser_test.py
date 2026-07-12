#!/usr/bin/env python3
"""Regression test: active-work highlighted cards float to the top of a column."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
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
        self.revision = 0

    def store_signature(self) -> tuple[tuple[str, str, bool, int], ...]:
        return tuple(
            (
                str(ticket["id"]),
                str(ticket["updated"]),
                bool(ticket.get("active_work_highlight")),
                self.revision,
            )
            for ticket in self.tickets
        )

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

    def set_highlighted(self, ticket_ids: set[str]) -> None:
        self.revision += 1
        for ticket in self.tickets:
            ticket["active_work_highlight"] = ticket["id"] in ticket_ids
            ticket["updated"] = iso_now()


def ticket_payload(ticket_id: str, title: str, *, created: str, highlighted: bool = False) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "ops",
        "state": "in_progress",
        "blocked_by": [],
        "blocked_reason": "",
        "implementation": "ready to go",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_inspection": False,
        "inspector_signoff": False,
        "needs_eric_signoff": False,
        "eric_signoff": False,
        "manually_controlled": False,
        "commit_hash": "",
        "commit_exempt": False,
        "parent_id": "",
        "created": created,
        "updated": "2026-07-12T00:00:00+00:00",
        "active_work_highlight": highlighted,
        "active_work_owner_role": "ops" if highlighted else "",
        "active_work_notified_at": "2026-07-12T00:00:00+00:00" if highlighted else "",
        "comments": [],
        "screenshots": [],
        "screenshots_info": [],
        "screenshot": None,
        "screenshot_available": False,
    }


def implementation_card_ids(page: object) -> list[str]:
    return page.evaluate(
        """() => Array.from(
          Array.from(document.querySelectorAll('.column'))
            .find((column) => column.querySelector('.column-title')?.textContent === 'Implementation')
            .querySelectorAll('.card .card-id')
        ).map((node) => node.textContent)"""
    )


def wait_for_implementation_order(page: object, expected: list[str]) -> None:
    page.wait_for_function(
        """(expected) => {
          const column = Array.from(document.querySelectorAll('.column'))
            .find((item) => item.querySelector('.column-title')?.textContent === 'Implementation');
          if (!column) {
            return false;
          }
          const ids = Array.from(column.querySelectorAll('.card .card-id')).map((node) => node.textContent);
          return JSON.stringify(ids) === JSON.stringify(expected);
        }""",
        arg=expected,
        timeout=5000,
    )


def run_browser_check(playwright: object, server: TicketBoardServer, app: StaticBoardApp) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{server.server_port}/", wait_until="domcontentloaded")
        wait_for_implementation_order(page, ["PGU-302", "PGU-301", "PGU-303"])

        app.set_highlighted({"PGU-302", "PGU-303"})
        server.events.notify_change(app.store_signature())
        wait_for_implementation_order(page, ["PGU-302", "PGU-303", "PGU-301"])

        first_refresh_order = implementation_card_ids(page)
        server.events.notify_change(app.store_signature())
        wait_for_implementation_order(page, first_refresh_order)
        assert implementation_card_ids(page) == first_refresh_order

        app.set_highlighted(set())
        server.events.notify_change(app.store_signature())
        wait_for_implementation_order(page, ["PGU-301", "PGU-302", "PGU-303"])
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="active-work-highlight-order.") as tmp:
        root = Path(tmp)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        tickets = [
            ticket_payload("PGU-301", "Old unhighlighted", created="2026-07-12T00:00:00+00:00"),
            ticket_payload("PGU-302", "Middle highlighted", created="2026-07-12T00:01:00+00:00", highlighted=True),
            ticket_payload("PGU-303", "New unhighlighted", created="2026-07-12T00:02:00+00:00"),
        ]

        app = StaticBoardApp(tickets, frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                run_browser_check(p, server, app)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("active_work_highlight_order_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
