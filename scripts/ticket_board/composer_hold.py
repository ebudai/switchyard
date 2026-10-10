"""Tell the Director, once, when a composer holds a role's notice while its turn is over (SYRD-570).

The anti-clobber hold is right and stays exactly as it was. A pane whose
composer looks typed into -- or whose cursor cannot be read -- keeps its
notices queued, because a person may be writing there: nothing here submits
into the composer, clears it, sends a redraw key, kills anything, or calls the
notice delivered.

What was missing was anyone hearing about it when it lasts. On otto, Hermes
redraw residue held OTTO-71 and OTTO-79's notices for hours while the runtime's
own trusted hook said the turn was over. SYRD-550 stopped that residue reading
as input; any other ambiguous or genuinely stuck composer still holds silently.
So every gate deferral is noted on the board, which keeps one episode per role:

* a composer hold (`HOLD_REASONS`) while the role's trusted hook says idle opens
  the role's episode, and later ones -- for any of its notices -- join it;
* once the episode is older than `alert_after_seconds()`, one ticket_update
  tells the Director: the tickets, the attempts, how long, the gate's reason,
  and what a person can do -- never a second for the same episode;
* the episode closes when its last notice leaves the queue (delivered,
  superseded or dead-lettered), or when a deferral shows the role is really
  working after all: a busy hook, a child at work. An alert still waiting is
  withdrawn; one already sent is followed by one line saying how it ended.

The episode lives in the database, so a restarted listener joins it rather than
starting the clock again.
"""

from __future__ import annotations

import os
from typing import Any

#: Long enough that a person writing a careful reply is not reported, short
#: enough that a stuck composer is not left for hours as OTTO-71's was.
DEFAULT_ALERT_AFTER_SECONDS = 1800.0
ALERT_AFTER_ENV = "TICKET_BOARD_COMPOSER_HOLD_ALERT_SECONDS"

#: The gate's reasons that are the composer's: a line that looks typed, or a
#: cursor that cannot be read to tell. Only these, under a trusted idle hook,
#: are a composer hold.
HOLD_REASONS = frozenset({"human_composing", "cursor_state_unavailable"})

#: The gate's reasons that are the role at work: its hook, its screen, its
#: working timer or its children say so. Any of these makes current work
#: authoritative and ends a composer hold. A reason that says neither -- no hook
#: state yet, a startup latch -- leaves the episode as it is.
WORK_REASONS = frozenset({
    "hook_busy",
    "hook_blocked",
    "pane_content_changed",
    "working_timer",
    "foreign_runtime_pane_content_changed",
    "foreign_runtime_working_timer",
    "pane_child_work",
    "prior_turn_child_work",
    "orphaned_prior_turn_waiter",
})


def alert_after_seconds(environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(ALERT_AFTER_ENV, "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_ALERT_AFTER_SECONDS
    except ValueError:
        return DEFAULT_ALERT_AFTER_SECONDS
    return value if value >= 0 else DEFAULT_ALERT_AFTER_SECONDS


def trusted_idle(state: Any) -> bool:
    """Whether a hook state is the runtime's own word that its turn is over."""
    from .pane_activity_gate import TRUSTED_IDLE_SOURCES

    return state is not None and state.state == "idle" and state.source in TRUSTED_IDLE_SOURCES


def _present(ledger: Any, conn: Any) -> bool:
    """Whether this board records composer holds yet: a listener can be newer than its board's migrations."""
    present = getattr(ledger, "_composer_hold_present", None)
    if present is None:
        row = conn.execute(
            "SELECT to_regprocedure('ticket_board.note_composer_hold(bigint, text, boolean, interval)') IS NOT NULL AS present"
        ).fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        ledger._composer_hold_present = present
    return present


def note(ledger: Any, conn: Any, notification_id: int, target_role: str, reason: str, trusted: bool) -> None:
    """Record one gate deferral; the board decides whether it opens, joins or ends a composer hold.

    A deferral that shows the role at work (`WORK_REASONS`) ends the role's
    episode, if it has one: current work is authoritative, so the hold is no
    longer the composer's. Asked once per role until a composer hold is noted
    again, so ordinary busy deferrals do not each reach the board.

    Never fatal: a failure here is logged, and the hold goes on exactly as it
    would have without it.
    """
    if target_role == "director":  # an alert about the Director's pane would wait in that same pane
        return
    composer = reason in HOLD_REASONS and trusted
    if not composer and reason not in WORK_REASONS:
        return  # neither a composer hold nor work: nothing to record either way
    settled = ledger.__dict__.setdefault("_composer_hold_settled_roles", set())
    if not composer and target_role in settled:
        return
    try:
        if not _present(ledger, conn):
            return
        conn.execute(
            "SELECT ticket_board.note_composer_hold(%s::bigint, %s::text, %s::boolean, %s::interval)",
            (notification_id, reason, composer, f"{alert_after_seconds():g} seconds"),
        )
    except Exception as exc:  # The hold must never depend on its own bookkeeping.
        ledger.logger.warning("Failed to note the composer hold of notification %s: %s", notification_id, exc)
        return
    if composer:
        settled.discard(target_role)
    else:
        settled.add(target_role)
