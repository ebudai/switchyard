"""Board-level operations that live in their own modules, behind one route (SYRD-537, SYRD-541).

The server admits each through its role policy as for any operation, then hands
it here; nothing in this module decides authority, which the database checks.
"""

from __future__ import annotations

from typing import Any

from . import pull_queue, reminder_snooze, report_intake, size_review

OPERATIONS = reminder_snooze.OPERATIONS | size_review.OPERATIONS | report_intake.OPERATIONS


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation in reminder_snooze.OPERATIONS:
        if ticket_id is not None:
            raise ValueError(f"{operation} names its tickets in the request, not the path")
        return reminder_snooze.perform(app, operation, payload, caller_role=caller_role)
    if operation in report_intake.OPERATIONS:  # SYRD-548
        if ticket_id is not None:
            raise ValueError(f"{operation} is a board policy, not a ticket's")
        return report_intake.perform(app, operation, payload, caller_role=caller_role)
    return size_review.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """What the board measures before a declared transition is attempted."""
    size_review.scan_for_transition(app, ticket_id, action, payload, caller_role=caller_role)


def ticket_fields(row: Any) -> dict[str, Any]:
    """The ticket JSON's extension fields, from the read query's row.

    reminder_snooze is scheduling, not delivery status, and null unless the
    ticket is snoozed (SYRD-537); size_review is the packet's size evidence and
    null until a candidate is measured (SYRD-541); serial_queue is the ticket's
    place in its implementer's queue, and null outside one (SYRD-568).
    """
    return {"reminder_snooze": row.get("reminder_snooze"), "size_review": row.get("size_review"),
            "serial_queue": row.get("serial_queue")}


def reservation_fields(app: Any) -> dict[str, Any]:
    """GET /api/reservations beside each implementer's slot: the ready queue
    (SYRD-539) and each implementer's assigned queue (SYRD-568)."""
    return {"pull_queue": pull_queue.status(app), "queues": serial_queues(app)}


def serial_queues(app: Any) -> dict[str, list[dict[str, Any]]]:
    """Each implementer's assigned implementation work in the order it will be taken (SYRD-568).

    The board's own order (ticket_board.serial_queue): the active ticket, then
    the tickets that can start, then the blocked ones. Empty on a board older
    than the queue.
    """
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.serial_queue()') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {}
        rows = conn.execute(
            "SELECT implementer, ticket_id, state, queue_position, active, waiting_on FROM ticket_board.serial_queue()"
        ).fetchall()
    queues: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        queues.setdefault(str(row["implementer"]), []).append({
            "ticket": str(row["ticket_id"]), "state": str(row["state"]), "position": int(row["queue_position"]),
            "active": bool(row["active"]), "waiting_on": [str(t) for t in row["waiting_on"] or []],
        })
    return queues


def reminder_snoozes(app: Any) -> list[dict[str, Any]]:
    return reminder_snooze.batches(app)
