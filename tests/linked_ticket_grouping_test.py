#!/usr/bin/env python3
"""Regression test: parent links stay navigable without grouping board cards."""

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


def ticket_payload(
    ticket_id: str,
    title: str,
    *,
    state: str,
    parent_id: str = "",
    assignee: str = "app",
    audit_signoff: bool = False,
    commit_exempt: bool = False,
) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": assignee,
        "state": state,
        "blocked_by": [],
        "blocked_reason": "",
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": audit_signoff,
        "needs_user_signoff": False,
        "user_signoff": False,
        "commit_hash": "",
        "commit_exempt": commit_exempt,
        "parent_id": parent_id,
        "created": "2026-07-09T00:00:00+00:00",
        "updated": "2026-07-09T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }


def run_browser_check(playwright: object, server_port: int) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")

        page.get_by_text("PGU-1", exact=True).wait_for(timeout=5000)
        analysis_column = page.locator(".column").filter(has=page.locator(".column-title", has_text="Triage"))
        analysis_column.get_by_text("PGU-1", exact=True).wait_for(timeout=5000)
        assert analysis_column.locator(".card-id").all_text_contents() == ["PGU-1"]

        implementation_column = page.locator(".column").filter(
            has=page.locator(".column-title", has_text="Implementation")
        )
        implementation_column.get_by_text("PGU-2", exact=True).wait_for(timeout=5000)
        assert implementation_column.locator(".card-id").all_text_contents() == ["PGU-2"]
        assert page.locator(".column .child-ticket-item").count() == 0

        show_done = page.locator("#showDoneInput")
        show_done.check()
        page.locator("#showDoneCount").get_by_text("(1)", exact=True).wait_for(timeout=5000)

        done_column = page.locator(".column").filter(has=page.locator(".column-title", has_text="Done"))
        done_column.get_by_text("PGU-3", exact=True).wait_for(timeout=5000)
        assert done_column.locator(".card-id").all_text_contents() == ["PGU-3"]
        assert done_column.locator(".empty").count() == 0

        # Parent and child links remain available in the detail modal even
        # though the board itself never nests one ticket inside another.
        analysis_column.locator(".card", has_text="PGU-1").click()
        modal = page.locator(".detail-modal")
        modal.wait_for(timeout=5000)
        modal.get_by_text("2 linked children", exact=True).wait_for(timeout=5000)
        modal.locator(".child-ticket-item").filter(has_text="PGU-2").click()
        assert page.locator("#detailModalTitle").inner_text() == "PGU-2 - Active child"
        modal.get_by_text("Parent", exact=True).wait_for(timeout=5000)
        parent_links = modal.get_by_role("button", name="PGU-1", exact=True)
        assert parent_links.count() == 2
        parent_links.first.click()
        assert page.locator("#detailModalTitle").inner_text() == "PGU-1 - Parent"
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="linked-ticket-grouping.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        tickets = [
            ticket_payload("PGU-1", "Parent", state="analysis"),
            ticket_payload("PGU-2", "Active child", state="in_progress", parent_id="PGU-1"),
            ticket_payload(
                "PGU-3",
                "Done child",
                state="done",
                parent_id="PGU-1",
                audit_signoff=True,
                commit_exempt=True,
            ),
        ]

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

    print("linked_ticket_grouping_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
