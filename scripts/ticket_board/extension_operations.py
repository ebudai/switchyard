"""Board-level operations that live in their own modules, behind one route (SYRD-537, SYRD-541).

The server admits each through its role policy as for any operation, then hands
it here; nothing in this module decides authority, which the database checks.
"""

from __future__ import annotations

from typing import Any

from . import reminder_snooze, size_review

OPERATIONS = reminder_snooze.OPERATIONS | size_review.OPERATIONS


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation in reminder_snooze.OPERATIONS:
        if ticket_id is not None:
            raise ValueError(f"{operation} names its tickets in the request, not the path")
        return reminder_snooze.perform(app, operation, payload, caller_role=caller_role)
    return size_review.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """What the board measures before a declared transition is attempted."""
    size_review.scan_for_transition(app, ticket_id, action, payload, caller_role=caller_role)


def ticket_fields(row: Any) -> dict[str, Any]:
    """The ticket JSON's extension fields, from the read query's row.

    reminder_snooze is scheduling, not delivery status, and null unless the
    ticket is snoozed (SYRD-537); size_review is the packet's size evidence and
    null until a candidate is measured (SYRD-541).
    """
    return {"reminder_snooze": row.get("reminder_snooze"), "size_review": row.get("size_review")}


def reminder_snoozes(app: Any) -> list[dict[str, Any]]:
    return reminder_snooze.batches(app)
