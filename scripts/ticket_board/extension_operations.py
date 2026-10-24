"""Board-level operations that live in their own modules, behind one route (SYRD-537, SYRD-541).

The server admits each through its role policy as for any operation, then hands
it here; nothing in this module decides authority, which the database checks.
"""

from __future__ import annotations

from typing import Any

from . import (audit_return, cohort_ops, provider_hold_ops, context_continuation, documentation_candidate, execution_lease_ops, pull_queue, reminder_snooze,
               report_intake, router_role, size_review, uat_instructions, ui_delegation, user_wait)

OPERATIONS = (reminder_snooze.OPERATIONS | size_review.OPERATIONS | report_intake.OPERATIONS
              | router_role.OPERATIONS | ui_delegation.OPERATIONS | context_continuation.OPERATIONS
              | uat_instructions.OPERATIONS | execution_lease_ops.OPERATIONS | cohort_ops.OPERATIONS
              | provider_hold_ops.OPERATIONS | user_wait.OPERATIONS | audit_return.OPERATIONS)


#: The board's reads that the Oracle core lacks: those modules' own routes, and images (attachments are not in the
#: core). On Oracle each is answered with the plain refusal, never attempted. A trailing "/" takes an argument.
ORACLE_ABSENT_READS = ("/api/reservations", "/api/context-recovery/", "/api/board-stall", "/api/cohorts",
                       "/api/provider-holds", "/api/execution-leases", "/api/reminder-snoozes", "/api/image/",
                       "/api/thumb/")


def oracle_absent_read(app: Any, path: str) -> str:
    """The plain refusal for a read the Oracle core lacks, or "" to serve it (SYRD-645)."""
    if getattr(app, "store_backend", "postgres") != "oracle":
        return ""
    route = next((r for r in ORACLE_ABSENT_READS if path == r or (r.endswith("/") and path.startswith(r))), "")
    return oracle_refusal(route.rstrip("/")) if route else ""


def oracle_refusal(name: str) -> str:
    return (f"{name} is not in the Oracle core yet (docs/syrd-642-oracle-backend.md lists the follow-on slices); "
            "nothing was changed")


def refuse_on_oracle(app: Any, name: str) -> None:
    """None of these modules is in the Oracle core yet (SYRD-645): each reads PostgreSQL's board, so say so plainly."""
    if getattr(app, "store_backend", "postgres") == "oracle":
        raise NotImplementedError(oracle_refusal(name))


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    refuse_on_oracle(app, operation)
    if operation in reminder_snooze.OPERATIONS:
        if ticket_id is not None:
            raise ValueError(f"{operation} names its tickets in the request, not the path")
        return reminder_snooze.perform(app, operation, payload, caller_role=caller_role)
    if operation in router_role.OPERATIONS:  # SYRD-599
        return router_role.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in report_intake.OPERATIONS:  # SYRD-548
        if ticket_id is not None:
            raise ValueError(f"{operation} is a board policy, not a ticket's")
        return report_intake.perform(app, operation, payload, caller_role=caller_role)
    if operation in context_continuation.OPERATIONS:  # SYRD-626
        return context_continuation.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in uat_instructions.OPERATIONS:  # SYRD-629
        return uat_instructions.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in user_wait.OPERATIONS:  # SYRD-662
        return user_wait.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in audit_return.OPERATIONS:  # SYRD-673
        return audit_return.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in execution_lease_ops.OPERATIONS:  # SYRD-616
        return execution_lease_ops.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in provider_hold_ops.OPERATIONS:  # SYRD-644
        return provider_hold_ops.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in cohort_ops.OPERATIONS:  # SYRD-628
        return cohort_ops.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    if operation in ui_delegation.OPERATIONS:  # SYRD-598
        return ui_delegation.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)
    return size_review.perform(app, operation, payload, caller_role=caller_role, ticket_id=ticket_id)


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """What the board measures, and a documentation decision checks (SYRD-624), before a declared transition."""
    if getattr(app, "store_backend", "postgres") == "oracle":  # SYRD-642: neither check is in the Oracle core yet
        from .documentation_gate import GATE_ACTIONS
        if action in GATE_ACTIONS:
            raise NotImplementedError(f"{action} is not in the Oracle core yet: its documentation-candidate check "
                                      "is not, and a gate is never taken unchecked; nothing was changed")
        return
    documentation_candidate.before_transition(app, ticket_id, action, payload, caller_role=caller_role)
    size_review.scan_for_transition(app, ticket_id, action, payload, caller_role=caller_role)


def ticket_fields(row: Any) -> dict[str, Any]:
    """The ticket JSON's extension fields, from the read query's row.

    reminder_snooze is scheduling, not delivery status, and null unless the
    ticket is snoozed (SYRD-537); size_review is the packet's size evidence and
    null until a candidate is measured (SYRD-541); serial_queue is the ticket's
    place in its implementer's queue, and null outside one (SYRD-568);
    privileged_jobs is root's record of each privileged job asked for the
    ticket, as the board service read it (SYRD-113); routing is what Router
    classifies against while it holds the ticket, and its decisions after,
    null on a ticket Router never touched (SYRD-599); context_continuation is
    whether its first hand-off continued its author's conversation and why, or
    the Director's request that it start fresh, null when neither (SYRD-626);
    uat is the User's current UAT instructions and questions, null when there are
    none (SYRD-629); user_wait is its wait on the User -- the Director's request,
    or why it is not valid and the repair -- null when it waits on nobody of the
    kind (SYRD-662); audit_return is where Audit's kick-back would return it, or
    why it would be refused and the repair -- null outside the stage that
    kick-back leaves (SYRD-673).
    """
    return {"reminder_snooze": row.get("reminder_snooze"), "size_review": row.get("size_review"),
            "serial_queue": row.get("serial_queue"), "privileged_jobs": row.get("privileged_jobs") or [],
            "routing": row.get("routing"), "context_continuation": row.get("context_continuation"),
            "uat": row.get("uat"), "user_wait": row.get("user_wait"), "audit_return": row.get("audit_return")}


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


def context_recovery(app: Any, role: str) -> dict[str, Any]:
    """GET /api/context-recovery/<role>: what a launch of the role must resume (SYRD-626)."""
    return context_continuation.recovery(app, role)


def reminder_snoozes(app: Any) -> list[dict[str, Any]]:
    return reminder_snooze.batches(app)


def execution_lease_status(app: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    """SYRD-616: GET /api/execution-leases[?role=R&all=1], read-only."""
    return execution_lease_ops.status(app, role=(query.get("role") or [""])[0],
                                      include_ended=(query.get("all") or [""])[0] in ("1", "true"))


def provider_hold_status(app: Any) -> dict[str, Any]:
    """SYRD-644: GET /api/provider-holds, read-only."""
    return provider_hold_ops.status(app)


def cohort_status(app: Any) -> dict[str, Any]:
    """SYRD-628: GET /api/cohorts, read-only."""
    return cohort_ops.status(app)


def board_stall_status(app: Any) -> dict[str, Any]:
    """SYRD-637: the board stall alert's state, read-only."""
    from . import board_stall

    return board_stall.status(app)
