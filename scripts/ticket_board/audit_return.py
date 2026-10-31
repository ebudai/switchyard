"""Where Audit's kick-back returns a ticket, and the Director's record of it (SYRD-673).

`audit_kick_back` used to land in the declared Implementation stage, with an
Implementation owner picked by the executor. A ticket that reached Audit from a
designer's draft went to an implementer who had never touched it. The board now
records who produced the revision a ticket carries, and Audit's kick-back
returns the work to that stage and owner. When the board does not know, or the
workflow no longer admits the return, the kick-back is refused, and the
Director records the return with `set-audit-return`.

SYRD-719 keeps two layers apart. The producer is the work's substantive origin
(Design/a designer, Implementation/an implementer); a pass through the
documentation gate -- the Librarian's documentation commit, or no change -- is
recorded beside it, and never replaces it. Audit chooses explicitly:
`audit_kick_back` returns any substantive or mixed finding to the substantive
origin, and `audit_documentation_kick_back` returns a documentation-only
finding to the Documentation stage and owner the ticket passed.

The database decides every rule. This module only carries the request and the
`--target-assignee` confirmation (SYRD-307), which names where a declared
return goes.
"""

from __future__ import annotations

from typing import Any

OPERATIONS = frozenset({"set_audit_return"})
ACTION = "audit_kick_back"
DOCUMENTATION_ACTION = "audit_documentation_kick_back"


def _layer(transition: dict[str, Any], ticket: dict[str, Any]) -> dict[str, Any] | None:
    """Audit's record for one of its two returns: the substantive origin, or the documentation layer; None for any
    other transition, and on a board older than SYRD-673 (or, for the documentation return, SYRD-719)."""
    status = ticket.get("audit_return")
    if transition["primitive"] != "return" or not isinstance(status, dict):
        return None
    if transition["action"] == ACTION:
        return status
    return status.get("documentation") if transition["action"] == DOCUMENTATION_ACTION else None


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    unknown = set(payload) - {"stage", "assignee", "reason"}
    if unknown:
        raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
    values = [payload.get(key, "") for key in ("stage", "assignee", "reason")]
    if not all(isinstance(value, str) for value in values):
        raise ValueError("set-audit-return's stage, assignee and reason are text")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.set_audit_return(%s, %s, %s, %s) AS result",
                               (ticket_id, *values)).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return {"ticket_id": ticket_id, "audit_return": result}


def returns_to(app: Any, ticket: dict[str, Any], transition: dict[str, Any], owners: list[str]) -> str:
    """Who a declared `return` sends `ticket` to, as the executor decides it.

    Audit's kick-back goes to the producer of the revision under review, and its documentation return to the
    Documentation owner the ticket passed (SYRD-719), or nowhere while that is not valid; a board older than
    SYRD-673 reports none, and its kick-back is the ordinary return. Any other return
    goes to the recorded implementer when that is an owner of its destination, else the current assignee, else
    the destination's first owner.
    """
    status = _layer(transition, ticket)
    if status is not None:
        return str(status.get("assignee") or "") if status.get("valid") else ""
    with app._pg_connect() as conn:
        row = conn.execute("SELECT last_implementer_assignee FROM ticket_board.ticket_notification_state "
                           "WHERE ticket_id = %s", (ticket["id"],)).fetchone()
    recorded = str((row["last_implementer_assignee"] if isinstance(row, dict) else row[0]) or "") if row else ""
    if recorded in owners:
        return recorded
    if ticket["assignee"] in owners:
        return str(ticket["assignee"])
    return owners[0] if owners else ""


def confirm_target(app: Any, ticket: dict[str, Any], transition: dict[str, Any], owners: list[str], target: str) -> None:
    """Refuse, before anything changes, a `--target-assignee` that is not where the return goes (SYRD-307).

    An Audit kick-back with no valid origin is left to the executor, whose refusal names the Director's repair.
    """
    action, ticket_id = transition["action"], ticket["id"]
    origin = _layer(transition, ticket) is not None
    to = returns_to(app, ticket, transition, owners)
    if origin and not to:
        return
    if target != to:
        where = ("the Documentation owner it passed" if action == DOCUMENTATION_ACTION
                 else "the producer of its revision" if origin else "its recorded implementer")
        raise ValueError(f"{action} returns {ticket_id} to {where}, {to or 'nobody'}; it cannot send it to {target}. "
                         "Kick it back without --target-assignee, then have the Director reassign it")


def advertise(transition: dict[str, Any], ticket: dict[str, Any]) -> dict[str, Any]:
    """Audit's two returns as a ticket offers them: the declared action, plus where it returns this ticket.

    `to` stays the declared destination, so clients that read it are unchanged. `returns_to` is the recorded
    stage and owner, or null with `refused`: why it would be refused, and the Director's repair or, for the
    documentation return, the ordinary kick-back instead (SYRD-719).
    """
    status = _layer(transition, ticket)
    if status is None:
        return transition
    if status.get("valid"):
        return {**transition, "returns_to": {"stage": status.get("stage"), "assignee": status.get("assignee")}}
    if transition["action"] == DOCUMENTATION_ACTION:
        return {**transition, "returns_to": None, "refused": f"{status.get('problem')}; instead: {status.get('otherwise')}"}
    problem = status.get("problem") or "the board has no record of who produced its revision"
    return {**transition, "returns_to": None, "refused": f"{problem}; repair: {status.get('repair') or 'set-audit-return'}"}
