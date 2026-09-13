#!/usr/bin/env python3
"""SYRD-104: a ticket card never reaches outside its own stage column.

The reported regression: with the full workflow visible, the Implementation
lane's cards painted across the Implementation border and over the Inspection
column, and the Final Sign-Off card ran past the right edge of the window.
Measured in a real browser at the ticket's 2048x1152 viewport before the
repair, the two Implementation cards were 320px wide inside a 205px column --
128px of card hanging over the neighbouring lane.

The cause was not the titles. A stage column is a grid item of the board,
whose tracks `syncBoardGridColumns` sizes, and a grid item's automatic minimum
size is its content's minimum. One 40-character commit hash in a blocked
reason has no break opportunity in it, so it set a minimum wider than the
track and every box between the track and that text -- column, body, card,
alert stack, alert -- was pushed out with it. Both halves are covered here:
the boxes now carry a `min-width` floor, and the text that has no break
opportunity of its own is allowed to break anywhere.

These cases assert geometry read from the rendered page rather than the
stylesheet's text, because the property under test is where the pixels land.
"""

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
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer

#: The token from the reported board: a full commit hash, and nothing in it
#: that a browser will break on its own.
COMMIT_HASH = "6b8e588ee0fa88cf1008a9e187aaca0f21b4b425"
#: Worse than anything on the real board, to show the floor is structural.
UNBREAKABLE = "x" * 200
#: An identifier with no break opportunity, for the chips built out of it.
LONG_ID = "PGU-" + ("9" * 40)


def load_playwright() -> object:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        candidates = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
        candidates.extend(sorted(Path("/tmp/pgu-playwright-venv/lib").glob("python*/site-packages")))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


class ContainmentBoardApp:
    """A board holding every shape the ticket names, one per lane."""

    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.tickets: list[dict[str, Any]] = [
            # The reported card: a blocked panel carrying a commit hash.
            self.ticket(
                "PGU-591",
                "HIGH PRIORITY: Restore native rendered Codex scrollback without raw-output regressions",
                state="in_progress",
                assignee="app",
                blocked_by=["PGU-596"],
                blocked_reason=(
                    "Shared launcher and live Board are both at "
                    f"{COMMIT_HASH}, finish-upgrade exited 0, and PGU-596 now waits on "
                    "PGU-600 to exercise the deployed workflow."
                ),
                active_work_highlight=True,
            ),
            # A second card in the same lane, to show a lane's widest card is
            # what stretched its neighbours.
            self.ticket(
                "PGU-596",
                "HIGH PRIORITY: Deploy and live-accept the Director-only publication handoff",
                state="in_progress",
                assignee="ops",
            ),
            # A title with no break opportunity at all.
            self.ticket(
                "PGU-597",
                f"Unbreakable title {UNBREAKABLE} end",
                state="analysis",
                assignee="director",
            ),
            # A blocked panel with no break opportunity at all.
            self.ticket(
                "PGU-598",
                "Blocked reason with nothing to break on",
                state="audit",
                assignee="audit",
                blocked_by=["PGU-596"],
                blocked_reason=f"Waiting on {UNBREAKABLE} to land.",
            ),
            # Enough chips to wrap several rows, plus a signed-off panel.
            self.ticket(
                "PGU-599",
                "Many chips in one card",
                state="director_review",
                assignee="director",
                blocked_by=["PGU-591", "PGU-596", "PGU-597", "PGU-598"],
                blocked_reason="Blocked by PGU-591, PGU-596, PGU-597 and PGU-598 together.",
                needs_user_signoff=True,
                user_signoff=True,
            ),
            # A ticket id long enough to reach outside the lane on its own,
            # which is what puts the id, the `blocked <id>` badge and the
            # inline chips built from it under the same rule as the prose.
            self.ticket(
                LONG_ID,
                "An identifier with nothing to break on",
                state="inspection",
                assignee="inspector",
                blocked_by=[LONG_ID],
                blocked_reason=f"Waiting on {LONG_ID} to resolve.",
            ),
            self.ticket("PGU-600", "A short one", state="dat", assignee="main"),
        ]

    def ticket(
        self,
        ticket_id: str,
        title: str,
        *,
        state: str,
        assignee: str,
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        active_work_highlight: bool = False,
        needs_user_signoff: bool = False,
        user_signoff: bool = False,
    ) -> dict[str, Any]:
        blocked_by = list(blocked_by or [])
        return {
            "id": ticket_id,
            "title": title,
            "body": "",
            "assignee": assignee,
            "state": state,
            "blocked_by": blocked_by,
            "blockers": [{"id": item, "resolved": False} for item in blocked_by],
            "blocked_reason": blocked_reason,
            "implementation": "",
            "audit_prompt": "",
            "needs_audit": True,
            "audit_signoff": False,
            "needs_inspection": False,
            "inspector_signoff": False,
            "needs_user_signoff": needs_user_signoff,
            "user_signoff": user_signoff,
            "manually_controlled": False,
            "parked": False,
            "regression": False,
            "commit_hash": "",
            "commit_exempt": False,
            "parent_id": "",
            "active_work_highlight": active_work_highlight,
            "active_work_owner_role": assignee,
            "created": "2026-09-01T00:00:00+00:00",
            "updated": "2026-09-11T00:00:00+00:00",
            "comments": [],
            "screenshots": [],
            "screenshots_info": [],
            "screenshot": None,
            "screenshot_available": False,
        }

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(item["id"]), str(item["updated"])) for item in self.tickets)

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

    def update_ticket(self, *args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("this suite never writes")


#: Read back per card: where it sits, where its lane sits, whether anything
#: inside it had to be cut off, and where its chips landed.
MEASURE = """
() => {
  const columns = [];
  document.querySelectorAll('.column').forEach((column) => {
    const columnRect = column.getBoundingClientRect();
    const cards = [];
    column.querySelectorAll('.card').forEach((card) => {
      const cardRect = card.getBoundingClientRect();
      const chips = [];
      card.querySelectorAll('.badge, .tag, .ticket-ref').forEach((chip) => {
        const chipRect = chip.getBoundingClientRect();
        chips.push({left: chipRect.left, right: chipRect.right});
      });
      cards.push({
        id: card.querySelector('.card-id')?.textContent || '',
        left: cardRect.left,
        right: cardRect.right,
        width: cardRect.width,
        hiddenWidth: card.scrollWidth - card.clientWidth,
        chips,
      });
    });
    columns.push({
      label: column.querySelector('.column-title')?.textContent || '',
      left: columnRect.left,
      right: columnRect.right,
      width: columnRect.width,
      cards,
    });
  });
  const board = document.querySelector('.board');
  return {
    columns,
    boardScrollWidth: board.scrollWidth,
    boardWidth: board.getBoundingClientRect().width,
    documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}
"""


def measure(page: object, width: int, height: int) -> dict[str, Any]:
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_function(
        "() => document.querySelectorAll('.column').length > 1"
        " && document.querySelectorAll('.card').length === 7",
        timeout=10000,
    )
    return page.evaluate(MEASURE)


def assert_cards_stay_in_their_lane(data: dict[str, Any], *, width: int) -> None:
    """The ticket's first requirement, read straight off the rendered page."""
    seen_cards = 0
    for column in data["columns"]:
        for card in column["cards"]:
            seen_cards += 1
            where = f"{width}px viewport, {column['label']} / {card['id']}"
            # Half a pixel of tolerance: a card sits inside its column's
            # border and padding, so this is never a near-miss in practice.
            assert card["left"] >= column["left"] - 0.5, (where, card, column)
            assert card["right"] <= column["right"] + 0.5, (where, card, column)
            # Nothing inside the card had to be cut off to achieve that, which
            # is what says the text wrapped rather than being clipped away.
            assert card["hiddenWidth"] == 0, (where, card["hiddenWidth"])
            for chip in card["chips"]:
                assert chip["left"] >= card["left"] - 0.5, (where, chip, card)
                assert chip["right"] <= card["right"] + 0.5, (where, chip, card)
    assert seen_cards == 7, seen_cards


def assert_lanes_do_not_overlap(data: dict[str, Any], *, width: int) -> None:
    """No card reaches the lane to its right -- the reported symptom itself."""
    ordered = sorted(data["columns"], key=lambda column: column["left"])
    for left_column, right_column in zip(ordered, ordered[1:]):
        for card in left_column["cards"]:
            assert card["right"] <= right_column["left"], (
                f"{width}px viewport",
                left_column["label"],
                card,
                right_column["label"],
            )


def assert_scrolling_comes_from_the_columns(data: dict[str, Any], *, width: int) -> None:
    """Horizontal scroll is allowed, but only the lanes may cause it.

    `syncBoardGridColumns` asks for `repeat(n, minmax(205px, 1fr))` with a
    16px gap, so a board wider than that is a board something overflowed.
    """
    column_count = len(data["columns"])
    expected = (column_count * 205) + ((column_count - 1) * 16)
    assert column_count == 8, column_count
    assert data["boardScrollWidth"] <= max(expected, data["boardWidth"]) + 0.5, (
        f"{width}px viewport",
        data["boardScrollWidth"],
        expected,
        data["boardWidth"],
    )


def run_browser_check(playwright: object, server_port: int) -> None:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 2048, "height": 1152})
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server_port}/", wait_until="domcontentloaded")

        # The ticket's stated acceptance viewport, then a narrower desktop
        # where the board must scroll horizontally and still contain its cards.
        for width, height in ((2048, 1152), (1440, 900), (1100, 900)):
            data = measure(page, width, height)
            assert_cards_stay_in_their_lane(data, width=width)
            assert_lanes_do_not_overlap(data, width=width)
            assert_scrolling_comes_from_the_columns(data, width=width)

        assert page_errors == [], page_errors
    finally:
        browser.close()


def main() -> int:
    sync_playwright = load_playwright()
    with tempfile.TemporaryDirectory(prefix="board-column-containment.") as tmpdir:
        root = Path(tmpdir)
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()

        app = ContainmentBoardApp(frames, assets)
        server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                run_browser_check(playwright, server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("board_column_containment_browser_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
