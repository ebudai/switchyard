"""A Director's reminder snooze for named tickets, until a deadline (SYRD-537).

The database decides everything -- who may snooze, which tickets, whether a
ticket is still the one that was snoozed, and when a batch is due
(`ticket_board.snooze_reminders` and friends, migration pgu970). This module is
the thin Python around it: the board's action and read, the listener's
due-time pass, and the delivery check's question about one queued row.

What a snooze silences, for a member ticket only: the OPTIONAL reminders --
idle reminders, nudges, their escalations, the unresolved-turn prompt and its
grace escalation, and the later steps of a wait's schedule. Never a handoff:
not a transition, not triage, not a wait's first step, not publication or
in-place update notices, and not a permission-prompt escalation, which is
about a pane rather than a ticket.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

OPERATIONS = frozenset({"snooze_reminders", "clear_reminder_snooze"})
#: The payload kind of the one notice a batch writes at its deadline.
REMINDER_SNOOZE_DUE = "reminder_snooze_due"
#: Distinct from `stale_notification` (the ticket moved) and from the
#: supersessions: this reminder was deliberately deferred, so it is discarded --
#: never acked, which would count it as delivered and arm the next escalation.
REMINDER_SNOOZED = "reminder_snoozed"
_ALWAYS_OPTIONAL = frozenset({"idle_reminder", "nudge", "unresolved_turn_repair", "unresolved_turn"})


def is_optional_reminder(parsed: dict[str, Any]) -> bool:
    """Whether a queued notice is one a snooze defers. Handoffs never are."""
    kind = str(parsed.get("kind") or "").strip().lower()
    if kind in _ALWAYS_OPTIONAL:
        return True
    if kind == "escalation":
        return str(parsed.get("reason") or "") != "permission_prompt"
    if kind == "awaiting_role":
        return parsed.get("step") in (2, 3, 4)
    return False


def snoozed_reminder_detail(conn: Any, ticket_id: str, payload: str) -> dict[str, Any] | None:
    """Why this queued notice must not be delivered now, or None to carry on."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not is_optional_reminder(parsed):
        return None
    present = conn.execute(
        "SELECT to_regprocedure('ticket_board.ticket_reminders_snoozed(text,timestamptz)') IS NOT NULL AS present"
    ).fetchone()
    if present is None or not bool(present["present"] if isinstance(present, dict) else present[0]):
        return None  # a board older than the snooze has none to honour
    row = conn.execute(
        "SELECT ticket_board.ticket_reminders_snoozed(%s, clock_timestamp()) AS snoozed",
        (ticket_id,),
    ).fetchone()
    if row is None or not bool(row["snoozed"] if isinstance(row, dict) else row[0]):
        return None
    return {"reason": REMINDER_SNOOZED, "snoozed_kind": str(parsed.get("kind") or "")}


def discard_if_snoozed(
    eligibility: Any, conn: Any, notification_id: int, ticket_id: str, target_role: str, kind: str, payload: str,
    phase: str,
) -> bool:
    """Discard an optional reminder about a snoozed ticket; True if it went.

    Whatever wrote it -- a generator before the snooze, or one that does not
    know about snoozes -- it is not delivered while the snooze holds. It is
    discarded through the eligibility check's own drop, never acked, so it
    neither counts as sent nor arms an escalation.
    """
    detail = snoozed_reminder_detail(conn, ticket_id, payload)
    if detail is None:
        return False
    eligibility._drop_superseded_notification(
        conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role,
        kind=kind, detail=detail, phase=phase, reason=REMINDER_SNOOZED,
    )
    return True


def parse_deadline(value: object) -> datetime:
    """An unambiguous instant: ISO 8601 with an explicit offset or Z, never local time."""
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"deadline must be ISO 8601 with an offset, e.g. 2026-10-03T07:00:00-04:00: {text!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"deadline needs an explicit offset or Z; {text!r} would be read in someone's local time")
    return parsed


def _tickets(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("tickets must be a list of ticket ids")
    return value


def _apply_flag(payload: dict[str, Any]) -> bool:
    apply = payload.get("apply", False)
    if type(apply) is not bool:
        raise ValueError("apply must be true or false")
    return apply


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
    """The board action: preview unless `apply`, and the database's answer either way."""
    if operation == "snooze_reminders":
        if set(payload) - {"tickets", "until", "reason", "apply"}:
            raise ValueError("snooze_reminders takes tickets, until, reason and apply")
        sql = "SELECT ticket_board.snooze_reminders(%s, %s, %s, %s) AS result"
        params: tuple[Any, ...] = (
            _tickets(payload.get("tickets")), parse_deadline(payload.get("until")),
            str(payload.get("reason") or ""), _apply_flag(payload),
        )
    elif operation == "clear_reminder_snooze":
        if set(payload) - {"batch", "tickets", "reason", "apply"}:
            raise ValueError("clear_reminder_snooze takes batch, tickets, reason and apply")
        batch = payload.get("batch")
        if type(batch) is not int:
            raise ValueError("batch must be a batch number")
        sql = "SELECT ticket_board.clear_reminder_snooze(%s, %s, %s, %s) AS result"
        params = (batch, _tickets(payload.get("tickets")), str(payload.get("reason") or ""), _apply_flag(payload))
    else:
        raise ValueError(f"unknown reminder snooze operation: {operation}")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(sql, params).fetchone()
    return dict(row["result"])


def batches(app: Any) -> list[dict[str, Any]]:
    """Open batches and the most recent closed ones, with each member's live status."""
    with app._pg_connect() as conn:
        row = conn.execute("SELECT ticket_board.reminder_snoozes(clock_timestamp()) AS batches").fetchone()
    return list(row["batches"] or [])


def emit_due(conn: Any, logger: Any) -> int:
    """The listener's pass: one notice per batch whose deadline has passed."""
    try:
        row = conn.execute("SELECT ticket_board.emit_due_reminder_snoozes(clock_timestamp())").fetchone()
    except Exception as exc:
        logger.warning("Failed to emit due reminder snoozes: %s", exc)
        return 0
    value = 0 if row is None else (row[0] if not isinstance(row, dict) else next(iter(row.values())))
    emitted = int(value or 0)
    if emitted:
        logger.info("Emitted %s due reminder-snooze notices", emitted)
    return emitted
