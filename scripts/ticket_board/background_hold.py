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

from . import provider_prompt

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

#: How long a pane stopped at its provider's own question may hold notices
#: before the Director is told (SYRD-566). Short: unlike background work, the
#: question never ends by itself, and two minutes is long enough for a person
#: already at the pane to answer it first.
DEFAULT_PROMPT_ALERT_AFTER_SECONDS = 120.0
PROMPT_ALERT_AFTER_ENV = "TICKET_BOARD_PROVIDER_PROMPT_ALERT_SECONDS"


def alert_after_seconds(environ: dict[str, str] | None = None, *, env: str = ALERT_AFTER_ENV,
                        default: float = DEFAULT_ALERT_AFTER_SECONDS) -> float:
    raw = (os.environ if environ is None else environ).get(env, "")
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        return default
    return value if value >= 0 else default


def prompt_alert_after_seconds(environ: dict[str, str] | None = None) -> float:
    return alert_after_seconds(environ, env=PROMPT_ALERT_AFTER_ENV, default=DEFAULT_PROMPT_ALERT_AFTER_SECONDS)


def prompt_hold_description(kind: str) -> str:
    """What the Director is told a pane held at this question is, and what releases it."""
    description, remedy = provider_prompt.KINDS[kind]
    return (f"its CLI is stopped at {description}. Nothing answers it by itself, and nothing was typed into it. "
            f"To release them, {remedy}.")


def _present(ledger: Any, conn: Any) -> Any:
    """Which hold record this board has: a listener can be newer than its board's migrations.

    "described" once a hold can say what it is (SYRD-566); any other true value
    for SYRD-557's, which says only "background work"; false before either.
    """
    present = getattr(ledger, "_background_hold_present", None)
    if present is None:
        row = conn.execute(
            "SELECT CASE WHEN to_regprocedure('ticket_board.note_background_hold(bigint, text, interval, text)') IS NOT NULL "
            "THEN 'described' WHEN to_regprocedure('ticket_board.note_background_hold(bigint, text, interval)') IS NOT NULL "
            "THEN 'background' ELSE '' END AS present"
        ).fetchone()
        present = (row["present"] if isinstance(row, dict) else row[0]) if row else ""
        present = present.decode() if isinstance(present, bytes) else present
        ledger._background_hold_present = present
    return present


def note(ledger: Any, conn: Any, notification_id: int, reason: str) -> None:
    """Record that background work, or a provider's own question, held this notice again.

    The board decides whether that is news.

    Never fatal: a failure here is logged, and the hold goes on exactly as it
    would have without it.
    """
    prompt = reason[len(provider_prompt.PROVIDER_PROMPT_PREFIX):] if reason.startswith(
        provider_prompt.PROVIDER_PROMPT_PREFIX) else ""
    if reason not in HOLD_REASONS and prompt not in provider_prompt.KINDS:
        return
    try:
        present = _present(ledger, conn)
        if not present:
            return
        if prompt:
            if present != "described":
                return  # an older board would call it background work, which it is not
            conn.execute(
                "SELECT ticket_board.note_background_hold(%s::bigint, %s::text, %s::interval, %s::text)",
                (notification_id, reason, f"{prompt_alert_after_seconds():g} seconds", prompt_hold_description(prompt)),
            )
            return
        conn.execute(
            "SELECT ticket_board.note_background_hold(%s::bigint, %s::text, %s::interval)",
            (notification_id, reason, f"{alert_after_seconds():g} seconds"),
        )
    except Exception as exc:  # The hold must never depend on its own bookkeeping.
        ledger.logger.warning("Failed to note the background hold of notification %s: %s", notification_id, exc)
