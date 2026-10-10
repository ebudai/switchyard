"""Tell the Director, once, when background work holds a role's notices too long (SYRD-557).

The hold is right and stays exactly as it was. A role whose turn ended on
background work that is still provably live (SYRD-538), or whose pane is
running a child that is work (SYRD-101, SYRD-212), keeps its notices queued
until the work ends: nothing here delivers through it, interrupts it or kills
anything.

What was missing was anyone hearing about it. On otto a wait loop whose
`pgrep -f` matched itself ran for 2 h 44 min, and two ready ticket notices sat
behind it the whole time with nobody told. So every requeue that one of these
holds causes is noted on the board, which keeps one episode per role:

* the first hold opens the role's episode, and later holds -- of any of that
  role's notices -- join it;
* once the episode is older than `alert_after_seconds()`, one ticket_update
  tells the Director, never a second for the same episode;
* when the last of its notices leaves the queue (delivered, superseded or
  dead-lettered) the episode closes: an alert still waiting is withdrawn, and
  one already sent is followed by a line saying the hold ended.

The episode is kept in the database (pgu980), so a restarted listener joins the
hold it left rather than starting the clock again.
"""

from __future__ import annotations

import os
from typing import Any

#: How long background work may hold a role's notices before the Director is
#: told. Otto's own tenant watchdog settled on half an hour, and an ordinary
#: mutation run or suite sweep finishes inside it.
DEFAULT_ALERT_AFTER_SECONDS = 1800.0
ALERT_AFTER_ENV = "TICKET_BOARD_BACKGROUND_HOLD_ALERT_SECONDS"

#: The holds that are background work. The owner's own turn-end background
#: tasks (SYRD-538), and a pane child the activity gate judged to be work --
#: this turn's or, until SYRD-212's bound releases it, an earlier one's. A pane
#: held for a half-typed line, an unreadable cursor or a startup latch is not
#: background work and opens no episode.
HOLD_REASONS = frozenset({
    "owner_background_work",
    "pane_child_work",
    "prior_turn_child_work",
    "orphaned_prior_turn_waiter",
})
#: The requeue error `_defer_for_background_work` writes, before its phase.
OWNER_BACKGROUND_WORK = "owner_background_work"


def alert_after_seconds(environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(ALERT_AFTER_ENV, "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_ALERT_AFTER_SECONDS
    except ValueError:
        return DEFAULT_ALERT_AFTER_SECONDS
    return value if value >= 0 else DEFAULT_ALERT_AFTER_SECONDS


def _present(ledger: Any, conn: Any) -> bool:
    """Whether this board records hold episodes yet: a listener can be newer than its board's migrations."""
    present = getattr(ledger, "_background_hold_present", None)
    if present is None:
        row = conn.execute(
            "SELECT to_regprocedure('ticket_board.note_background_hold(bigint, text, interval)') IS NOT NULL AS present"
        ).fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        ledger._background_hold_present = present
    return present


def note(ledger: Any, conn: Any, notification_id: int, reason: str) -> None:
    """Record that background work held this notice again; the board decides whether that is news.

    Never fatal: a failure here is logged, and the hold goes on exactly as it
    would have without it.
    """
    if reason not in HOLD_REASONS:
        return
    try:
        if not _present(ledger, conn):
            return
        conn.execute(
            "SELECT ticket_board.note_background_hold(%s::bigint, %s::text, %s::interval)",
            (notification_id, reason, f"{alert_after_seconds():g} seconds"),
        )
    except Exception as exc:  # The hold must never depend on its own bookkeeping.
        ledger.logger.warning("Failed to note the background hold of notification %s: %s", notification_id, exc)
