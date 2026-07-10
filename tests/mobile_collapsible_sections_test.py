#!/usr/bin/env python3
"""Regression test: mobile board sections collapse by default and show live counts."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
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


def write_ticket(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


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
        assert create_toggle.get_attribute("aria-expanded") == "false"
        assert create_content.is_hidden()
        create_toggle.click()
        assert create_toggle.get_attribute("aria-expanded") == "true"
        create_content.locator("#titleInput").wait_for(timeout=5000)

        analysis_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Analysis"))
        ready_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Ready"))
        audit_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Audit"))
        eric_head = mobile.locator(".column-head").filter(has=mobile.locator(".column-title", has_text="Eric Review"))

        analysis_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        ready_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        audit_head.get_by_text("(1)", exact=True).wait_for(timeout=5000)
        assert eric_head.locator(".mobile-section-count").count() == 0

        analysis_column = mobile.locator(".column").filter(has=mobile.locator(".column-title", has_text="Analysis"))
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
            has=desktop.locator(".column-title", has_text="Analysis"),
        )
        assert desktop_analysis_head.get_attribute("aria-expanded") is None
        desktop.locator(".column").filter(
            has=desktop.locator(".column-title", has_text="Analysis"),
        ).get_by_text("PGU-1", exact=True).wait_for(timeout=5000)
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="mobile-collapsible-sections.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()

        write_ticket(store / "PGU-1.json", ticket_payload("PGU-1", "Analysis task", state="analysis"))
        write_ticket(store / "PGU-2.json", ticket_payload("PGU-2", "Ready task", state="ready"))
        write_ticket(store / "PGU-3.json", ticket_payload("PGU-3", "Audit task", state="audit"))
        child_done = ticket_payload("PGU-4", "Done child A", state="done")
        child_done["parent_id"] = "PGU-1"
        child_done["commit_exempt"] = True
        child_done["audit_signoff"] = True
        write_ticket(store / "PGU-4.json", child_done)
        child_done_2 = ticket_payload("PGU-5", "Done child B", state="done")
        child_done_2["parent_id"] = "PGU-1"
        child_done_2["commit_exempt"] = True
        child_done_2["audit_signoff"] = True
        write_ticket(store / "PGU-5.json", child_done_2)

        app = TicketBoardApp(store, frames, assets)
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
