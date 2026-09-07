#!/usr/bin/env python3
"""SYRD-59: open descendants stay on the board when their ancestors are not.

The board grouped every ticket under its ROOT and rendered only roots. Once a
root reached a terminal state its card left the board -- Show Done is off by
default -- and it took every open descendant with it, so live, actionable
tickets simply had no card anywhere. The live board showed empty Triage, In
Progress and User Review columns while /api/board returned open tickets in all
three.

These drive the real page and read the real DOM, because the bug was entirely in
what did or did not get rendered. A card is a `.card-id`; a row inside another
card's linked-children list is a `.child-ticket-id`. Telling those apart is the
whole point: "visible" has to mean a card in its own column, and no ticket may
be in two places at once.
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
    """A ticket may be a card, or a row under one card. Never both, never two."""
    placements: dict[str, list[str]] = {}
    for title, column in board.items():
        for ticket_id in column["cards"]:
            placements.setdefault(ticket_id, []).append(f"card in {title}")
        for ticket_id in column["children"]:
            placements.setdefault(ticket_id, []).append(f"child row in {title}")
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

#: The reported shape, with the real board's nesting depth: an open ticket whose
#: root is done, and a deeper open ticket whose whole chain to the root is done
#: except for one open link.
REPORTED_TICKETS = [
    ticket("PGU-1", "Completed root", state="done"),
    ticket("PGU-2", "Open child of a done root", state="user_review", parent_id="PGU-1"),
    ticket("PGU-3", "Done link in the chain", state="done", parent_id="PGU-2"),
    ticket("PGU-4", "Open grandchild under a done link", state="in_progress", parent_id="PGU-3"),
    ticket("PGU-5", "Second open child of the done root", state="analysis", parent_id="PGU-1"),
    ticket("PGU-6", "Ordinary open root", state="analysis"),
    ticket("PGU-7", "Open child of an open root", state="in_progress", parent_id="PGU-6"),
    # Three open levels. A grandchild whose parent is itself only a row inside
    # another card still has to be rendered somewhere.
    ticket("PGU-8", "Open grandchild of an open root", state="user_review", parent_id="PGU-7"),
]


def check_reported_failure(page: Any, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("PGU-6", exact=True).first.wait_for(timeout=5000)
    board = read_board(page)

    # Show Done is off, so the done root is not on the board at all...
    assert "Done" not in board, sorted(board)

    # ...but its open descendants are, each in the column for its OWN state.
    assert board["User Review"]["cards"] == ["PGU-2"], board["User Review"]
    assert board["Triage"]["cards"] == ["PGU-5", "PGU-6"], board["Triage"]

    # The grandchild's nearest open ancestor is PGU-2, so it belongs to that
    # card rather than floating up on its own. The done link between them is
    # skipped rather than orphaning it.
    assert "PGU-4" not in board["In Progress"]["cards"], board["In Progress"]
    assert "PGU-4" in board["User Review"]["children"], board["User Review"]

    # Ordinary grouping is untouched: an open child of an open root still sits
    # inside its parent's card and does not get a second card of its own.
    assert "PGU-7" not in board["In Progress"]["cards"], board["In Progress"]
    assert "PGU-7" in board["Triage"]["children"], board["Triage"]

    # And a third open level is not lost between the two: it belongs to the
    # same card its parent does.
    assert "PGU-8" not in board["User Review"]["cards"], board["User Review"]
    assert "PGU-8" in board["Triage"]["children"], board["Triage"]

    assert_no_ticket_rendered_twice(board)
    assert_counts_match_cards(board)


def check_show_done_on(page: Any) -> None:
    page.locator("#showDoneInput").check()
    page.locator(".column-title", has_text="Done").first.wait_for(timeout=5000)
    board = read_board(page)

    # Revealing the completed work must not take the open work away again. The
    # naive fix -- rescue a descendant only while its ancestor is hidden --
    # would empty these columns the moment Show Done is ticked.
    assert board["User Review"]["cards"] == ["PGU-2"], board["User Review"]
    assert board["Triage"]["cards"] == ["PGU-5", "PGU-6"], board["Triage"]
    assert "PGU-4" in board["User Review"]["children"], board["User Review"]
    assert "PGU-8" in board["Triage"]["children"], board["Triage"]

    # The done root is now on the board, and is the only new card.
    assert "PGU-1" in board["Done"]["cards"], board["Done"]

    assert_no_ticket_rendered_twice(board)
    # The Done column's count is every done ticket rather than its card count,
    # which is what the Show Done toggle has always reported.
    assert_counts_match_cards(board, skip={"Done"})

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


def check_terminal_states_come_from_the_workflow(page: Any, url: str) -> None:
    """Terminality is read from the workflow document, not from a name list.

    A tenant whose terminal stage is called something else has to get the same
    behaviour, so this workflow has no stage named 'done' at all. Code that
    tested for that name would treat "archived" as ordinary, let it host its
    child, and PGU-11 would have no card of its own.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("PGU-12", exact=True).first.wait_for(timeout=5000)
    board = read_board(page)
    assert set(board) == {"Intake", "Building", "Archived"}, sorted(board)

    # Terminal ancestor: the child is rescued into its own column.
    assert "PGU-11" in board["Building"]["cards"], board["Building"]

    # Non-terminal ancestor in the same workflow: the child stays grouped, so
    # this is proving the distinction and not just "everything gets a card".
    assert "PGU-13" not in board["Building"]["cards"], board["Building"]
    assert "PGU-13" in board["Intake"]["children"], board["Intake"]

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
                    check_terminal_states_come_from_the_workflow(
                        page, f"http://127.0.0.1:{renamed.server_port}/"
                    )
                    print("  terminal states come from the workflow document: ok")
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
