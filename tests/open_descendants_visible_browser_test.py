#!/usr/bin/env python3
"""SYRD-59/SYRD-70: every visible ticket is a card in its own state.

Parent links provide detail-view context only. They must never host, relocate,
hide, or duplicate a ticket on the board, regardless of ancestry depth or the
parent's state. These checks drive the real page and inspect the real DOM.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import iso_now
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer


def load_playwright() -> Any:
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


def stage(name: str, label: str, *, terminal: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "owners": ["director"],
        "kind": "system",
        "gate": None,
        "skip_to": None,
        "signoff": None,
        "terminal": terminal,
        "notify": {"kind": "assignee", "role": None},
    }


def workflow(stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "ticket-board.workflow.v1",
        "project": "pgu",
        "roles": [{"name": "director", "active": True}],
        "stages": stages,
        "transitions": [],
        "remove_stages": [],
        "flags": {},
        "queue": {},
        "reassign": {},
    }


class StaticBoardApp:
    store_backend = "postgres"

    def __init__(
        self,
        tickets: list[dict[str, Any]],
        stages: list[dict[str, Any]],
        frames: Path,
        assets: Path,
    ) -> None:
        self.tickets = tickets
        self.stages = stages
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(ticket["id"]), str(ticket["updated"])) for ticket in self.tickets)

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": self.tickets,
            "errors": [],
            "states": [item["name"] for item in self.stages],
            "columns": [{"key": item["name"], "label": item["label"]} for item in self.stages],
            "assignees": ["app", "director"],
            "caller_roles": ["app", "director"],
            "workflow": workflow(self.stages),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }


def ticket(ticket_id: str, title: str, *, state: str, parent_id: str = "") -> dict[str, Any]:
    return {
        "id": ticket_id,
        "title": title,
        "body": f"Body for {ticket_id}",
        "assignee": "app",
        "state": state,
        "blocked_by": [],
        "blockers": [],
        "blocked_reason": "",
        "implementation": "",
        "audit_prompt": "",
        "audit_signoff": True,
        "needs_audit": False,
        "needs_user_signoff": False,
        "user_signoff": False,
        "commit_hash": "",
        "commit_exempt": True,
        "parent_id": parent_id,
        "workflow_actions": [],
        "created": "2026-09-01T00:00:00+00:00",
        "updated": "2026-09-01T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }


BOARD_SNAPSHOT_JS = """
() => Array.from(document.querySelectorAll('.column')).map((column) => ({
  title: column.querySelector('.column-title').textContent.trim(),
  count: (column.querySelector('.count') || {}).textContent || '',
  countHidden: Boolean((column.querySelector('.count') || {}).hidden),
  cards: Array.from(column.querySelectorAll('.card .card-id')).map((el) => el.textContent.trim()),
  children: Array.from(column.querySelectorAll('.child-ticket-id')).map((el) => el.textContent.trim()),
}))
"""


def read_board(page: Any) -> dict[str, dict[str, Any]]:
    columns = page.evaluate(BOARD_SNAPSHOT_JS)
    return {column["title"]: column for column in columns}


def assert_no_ticket_rendered_twice(board: dict[str, dict[str, Any]]) -> None:
    """Every rendered ticket has one placement and no card contains child rows."""
    placements: dict[str, list[str]] = {}
    for title, column in board.items():
        for ticket_id in column["cards"]:
            placements.setdefault(ticket_id, []).append(f"card in {title}")
        for ticket_id in column["children"]:
            placements.setdefault(ticket_id, []).append(f"child row in {title}")
        assert column["children"] == [], f"{title}: nested child rows remain: {column['children']}"
    duplicated = {key: value for key, value in placements.items() if len(value) > 1}
    assert not duplicated, f"tickets rendered more than once: {duplicated}"


def assert_counts_match_cards(board: dict[str, dict[str, Any]], *, skip: set[str] = frozenset()) -> None:
    for title, column in board.items():
        if title in skip:
            continue
        expected = len(column["cards"])
        shown = 0 if column["countHidden"] else int(column["count"] or 0)
        assert shown == expected, f"{title}: count says {shown}, {expected} cards rendered"


def serve(tickets: list[dict[str, Any]], stages: list[dict[str, Any]], root: Path) -> TicketBoardServer:
    frames = root / "frames"
    assets = root / "assets"
    frames.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    app = StaticBoardApp(tickets, stages, frames, assets)
    server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
    return server


STANDARD_STAGES = [
    stage("draft", "Draft"),
    stage("analysis", "Triage"),
    stage("in_progress", "In Progress"),
    stage("user_review", "User Review"),
    stage("done", "Done", terminal=True),
    stage("cancelled", "Cancelled", terminal=True),
]

#: Open and terminal parents, mixed-stage families, and three ancestry levels.
REPORTED_TICKETS = [
    ticket("PGU-1", "Completed root", state="done"),
    ticket("PGU-2", "Open child of a done root", state="user_review", parent_id="PGU-1"),
    ticket("PGU-3", "Done link in the chain", state="done", parent_id="PGU-2"),
    ticket("PGU-4", "Open grandchild under a done link", state="in_progress", parent_id="PGU-3"),
    ticket("PGU-5", "Second open child of the done root", state="analysis", parent_id="PGU-1"),
    ticket("PGU-6", "Ordinary open root", state="analysis"),
    ticket("PGU-7", "Open child of an open root", state="in_progress", parent_id="PGU-6"),
    # Three open levels: each level must still be an independent card.
    ticket("PGU-8", "Open grandchild of an open root", state="user_review", parent_id="PGU-7"),
    # The live SYRD shape that prompted SYRD-70: both implementation tickets
    # must remain in Implementation rather than appearing under their UAT parent.
    ticket("SYRD-19", "Live UAT parent", state="user_review"),
    ticket("SYRD-66", "Live implementation child A", state="in_progress", parent_id="SYRD-19"),
    ticket("SYRD-67", "Live implementation child B", state="in_progress", parent_id="SYRD-19"),
]


def check_reported_failure(page: Any, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("PGU-6", exact=True).first.wait_for(timeout=5000)
    board = read_board(page)

    # Show Done is off, so the done root is not on the board at all...
    assert "Done" not in board, sorted(board)

    # ...but every open ticket is a card in the column for its OWN state.
    assert board["User Review"]["cards"] == ["PGU-2", "PGU-8", "SYRD-19"], board["User Review"]
    assert board["Triage"]["cards"] == ["PGU-5", "PGU-6"], board["Triage"]
    assert board["In Progress"]["cards"] == ["PGU-4", "PGU-7", "SYRD-66", "SYRD-67"], board[
        "In Progress"
    ]

    assert_no_ticket_rendered_twice(board)
    assert_counts_match_cards(board)


def check_show_done_on(page: Any) -> None:
    page.locator("#showDoneInput").check()
    page.locator(".column-title", has_text="Done").first.wait_for(timeout=5000)
    assert page.locator("#showDoneCount").inner_text() == "(2)"
    board = read_board(page)

    # Revealing completed work must not relocate any open family member.
    assert board["User Review"]["cards"] == ["PGU-2", "PGU-8", "SYRD-19"], board["User Review"]
    assert board["Triage"]["cards"] == ["PGU-5", "PGU-6"], board["Triage"]
    assert board["In Progress"]["cards"] == ["PGU-4", "PGU-7", "SYRD-66", "SYRD-67"], board[
        "In Progress"
    ]

    # Each terminal ticket is also its own card in its terminal stage.
    assert board["Done"]["cards"] == ["PGU-1", "PGU-3"], board["Done"]

    assert_no_ticket_rendered_twice(board)
    assert_counts_match_cards(board)

    page.locator("#showDoneInput").uncheck()


#: A workflow whose terminal stage is not called "done", and whose open stages
#: are not called "analysis" or "in_progress" either.
RENAMED_STAGES = [
    stage("intake", "Intake"),
    stage("building", "Building"),
    stage("archived", "Archived", terminal=True),
]

RENAMED_TICKETS = [
    ticket("PGU-10", "Archived root", state="archived"),
    ticket("PGU-11", "Open child of an archived root", state="building", parent_id="PGU-10"),
    ticket("PGU-12", "Open root", state="intake"),
    ticket("PGU-13", "Open child of an open root", state="building", parent_id="PGU-12"),
]


def check_parent_state_never_controls_child_placement(page: Any, url: str) -> None:
    """Custom workflow names and parent terminality do not affect placement."""
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("PGU-12", exact=True).first.wait_for(timeout=5000)
    board = read_board(page)
    assert set(board) == {"Intake", "Building", "Archived"}, sorted(board)

    # Parent terminality and vocabulary are irrelevant to card placement.
    assert board["Intake"]["cards"] == ["PGU-12"], board["Intake"]
    assert board["Building"]["cards"] == ["PGU-11", "PGU-13"], board["Building"]
    assert board["Archived"]["cards"] == ["PGU-10"], board["Archived"]

    assert_no_ticket_rendered_twice(board)
    assert_counts_match_cards(board)


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="open-descendants.") as tmpdir:
        root = Path(tmpdir)
        reported = serve(REPORTED_TICKETS, STANDARD_STAGES, root / "a")
        renamed = serve(RENAMED_TICKETS, RENAMED_STAGES, root / "b")
        threads = []
        for server in (reported, renamed):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1600, "height": 1000})
                    check_reported_failure(page, f"http://127.0.0.1:{reported.server_port}/")
                    print("  open descendants of a done root are visible: ok")
                    check_show_done_on(page)
                    print("  they stay visible with Show Done on: ok")
                    check_parent_state_never_controls_child_placement(
                        page, f"http://127.0.0.1:{renamed.server_port}/"
                    )
                    print("  parent state never controls child placement: ok")
                finally:
                    browser.close()
        finally:
            for server in (reported, renamed):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    print("open_descendants_visible_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
