#!/usr/bin/env python3
"""Regression test: desktop board grid tracks every visible column."""

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


def ticket_payload(ticket_id: str, title: str, *, state: str, needs_eric_signoff: bool = False) -> dict[str, object]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "ops",
        "state": state,
        "blocked_by": [],
        "blocked_reason": "",
        "implementation": "ready to go",
        "audit_prompt": "",
        "audit_signoff": False,
        "needs_inspection": False,
        "inspector_signoff": False,
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
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")
        page.locator(".column-title", has_text="Final Sign-Off").wait_for(timeout=5000)

        columns = page.locator(".column")
        assert columns.count() == 6

        metrics = page.evaluate(
            """() => {
              const scroll = document.querySelector('.board-scroll');
              const board = document.getElementById('board');
              const columnRects = Array.from(document.querySelectorAll('.column')).map((column) => {
                const rect = column.getBoundingClientRect();
                const title = column.querySelector('.column-title')?.textContent || '';
                return { title, top: Math.round(rect.top), left: Math.round(rect.left) };
              });
              return {
                scrollWidth: scroll.scrollWidth,
                clientWidth: scroll.clientWidth,
                gridTemplateColumns: getComputedStyle(board).gridTemplateColumns,
                columnRects,
              };
            }"""
        )
        assert len(metrics["gridTemplateColumns"].split()) == 6
        tops = {column["top"] for column in metrics["columnRects"]}
        assert len(tops) == 1, metrics["columnRects"]
        assert metrics["columnRects"][-1]["title"] == "Final Sign-Off"

        page.locator("#showDeferredInput").check()
        page.locator("#showDoneInput").check()
        page.locator("#showCancelledInput").check()
        page.wait_for_function("() => document.querySelectorAll('.column').length === 9")
        expanded_metrics = page.evaluate(
            """() => {
              const scroll = document.querySelector('.board-scroll');
              const board = document.getElementById('board');
              const columnRects = Array.from(document.querySelectorAll('.column')).map((column) => {
                const rect = column.getBoundingClientRect();
                const title = column.querySelector('.column-title')?.textContent || '';
                return { title, top: Math.round(rect.top), left: Math.round(rect.left) };
              });
              return {
                scrollWidth: scroll.scrollWidth,
                clientWidth: scroll.clientWidth,
                gridTemplateColumns: getComputedStyle(board).gridTemplateColumns,
                columnRects,
              };
            }"""
        )
        assert expanded_metrics["scrollWidth"] > expanded_metrics["clientWidth"]
        assert len(expanded_metrics["gridTemplateColumns"].split()) == 9
        expanded_tops = {column["top"] for column in expanded_metrics["columnRects"]}
        assert len(expanded_tops) == 1, expanded_metrics["columnRects"]
        assert expanded_metrics["columnRects"][-1]["title"] == "Cancelled"
    finally:
        browser.close()


def main() -> int:
    assert "grid-template-columns: repeat(6, minmax(205px, 1fr));" not in HTML
    assert "function syncBoardGridColumns(columns)" in HTML
    assert "const columns = visibleColumns();" in HTML
    assert "boardEl.style.gridTemplateColumns = `repeat(${columnCount}, minmax(${minColumnWidth}px, 1fr))`;" in HTML
    assert "boardEl.style.minWidth = `max(100%, ${minBoardWidth}px)`;" in HTML

    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        tickets = [
            ticket_payload("PGU-1", "Analysis task", state="analysis"),
            ticket_payload("PGU-2", "Implementation task", state="in_progress"),
            ticket_payload("PGU-3", "Inspection task", state="inspection"),
            ticket_payload("PGU-4", "Audit task", state="audit"),
            ticket_payload("PGU-5", "Eric task", state="eric_review", needs_eric_signoff=True),
            ticket_payload("PGU-6", "Director task", state="director_review"),
            ticket_payload("PGU-7", "Backlog task", state="backlog"),
            ticket_payload("PGU-8", "Done task", state="done"),
            ticket_payload("PGU-9", "Cancelled task", state="cancelled"),
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

    print("board_grid_visible_columns_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
