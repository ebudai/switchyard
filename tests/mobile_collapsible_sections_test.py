#!/usr/bin/env python3
"""Regression test: mobile board sections collapse by default and show live counts."""

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


def ticket_payload(ticket_id: str, title: str, *, state: str, needs_eric_signoff: bool = False) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "app",
        "state": state,
        "blocked_by": [],
        "blocked_reason": "",
        "implementation": "ready to go",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_eric_signoff": needs_eric_signoff,
        "eric_signoff": False,
        "commit_hash": "",
        "commit_exempt": False,
        "parent_id": "",
        "created": "2026-07-09T00:00:00+00:00",
        "updated": "2026-07-09T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }


def run_browser_check(playwright: object, server_port: int) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        mobile = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        mobile.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")

        create_toggle = mobile.locator("#createSectionToggle")
        create_content = mobile.locator("#createSectionContent")
        mobile.wait_for_function(
            "() => document.getElementById('createSectionContent')?.hidden === true"
            " && document.getElementById('createSectionToggle')?.getAttribute('aria-expanded') === 'false'",
            timeout=5000,
        )
        create_toggle.click()
        assert create_toggle.get_attribute("aria-expanded") == "true"
        create_content.locator("#titleInput").wait_for(timeout=5000)

        analysis_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Triage"))
        implementation_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Implementation"))
        audit_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Audit"))
        eric_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Eric Review"))

        analysis_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        implementation_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        audit_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        assert eric_head.locator(".mobile-section-count").count() == 0

        analysis_column = mobile.locator(".column").filter(has=mobile.locator(".column-title", has_text="Triage"))
        assert analysis_head.get_attribute("aria-expanded") == "false"
        assert analysis_column.locator(".column-body").is_hidden()
        analysis_head.click()
        assert analysis_head.get_attribute("aria-expanded") == "true"
        analysis_column.get_by_text("PGU-1", exact=True).wait_for(timeout=5000)

        mobile.locator("#showDoneInput").check()
        done_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Done"))
        done_head.get_by_text("(2)", exact=True).wait_for(timeout=5000)
        assert done_head.get_attribute("aria-expanded") == "false"
        done_head.click()
        done_column = mobile.locator(".column").filter(has=mobile.locator(".column-title", has_text="Done"))
        done_column.get_by_text("PGU-1", exact=True).wait_for(timeout=5000)
        done_column.get_by_text("2 linked children", exact=True).wait_for(timeout=5000)

        desktop = browser.new_page(viewport={"width": 1440, "height": 900})
        desktop.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        assert desktop.locator("#createSectionToggle").is_hidden()
        desktop_analysis_head = desktop.locator(".column-head").filter(
            has=desktop.locator(".column-title", has_text="Triage"),
        )
        assert desktop_analysis_head.get_attribute("aria-expanded") is None
        desktop.locator(".column").filter(
            has=desktop.locator(".column-title", has_text="Triage"),
        ).get_by_text("PGU-1", exact=True).wait_for(timeout=5000)
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="mobile-collapsible-sections.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        tickets = [
            ticket_payload("PGU-1", "Analysis task", state="analysis"),
            ticket_payload("PGU-2", "Implementation task", state="in_progress"),
            ticket_payload("PGU-3", "Audit task", state="audit"),
        ]
        child_done = ticket_payload("PGU-4", "Done child A", state="done")
        child_done["parent_id"] = "PGU-1"
        child_done["commit_exempt"] = True
        child_done["audit_signoff"] = True
        tickets.append(child_done)
        child_done_2 = ticket_payload("PGU-5", "Done child B", state="done")
        child_done_2["parent_id"] = "PGU-1"
        child_done_2["commit_exempt"] = True
        child_done_2["audit_signoff"] = True
        tickets.append(child_done_2)

        app = StaticBoardApp(tickets, frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                run_browser_check(p, server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    print("mobile_collapsible_sections_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
