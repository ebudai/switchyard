"""A pulled author keeps its conversation into the next ticket of the same package (SYRD-626).

The first hand-off of a ticket to an ephemeral, pulled author normally clears
its conversation (SYRD-135). Just before that clear, the board is asked once,
durably, whether the pane's conversation continues instead
(`decide_ticket_context_continuation`): the previous ticket passed its release
gate, the two share a declared parent hierarchy, nothing else ran between, no
returned work is waiting to be restored, and the pane is provably still in that
ticket's session -- same runtime, same registered process, same worktree. This
module reports what the pane shows; the database decides and records it, so a
retry, a restarted listener or a duplicate notice finds the same answer.

A continuation sends nothing of its own. The new ticket is bound to the
continued session (SYRD-540 restores it from there later), no clear is sent,
and the ticket's own notice is delivered with one line saying so. Anything else
-- a fresh decision, or a check that failed -- leaves the ordinary clear to run.
"""

from __future__ import annotations

import json
from typing import Any

from . import session_context

#: The Director's package boundary, through the board's extension route.
OPERATIONS = frozenset({"force_fresh_context"})
NOTE = ("Board: {ticket} follows {previous} in {root}'s hierarchy, so this conversation continues: nothing was "
        "cleared, and what you learned on {previous} still applies. {ticket} is a new ticket -- read it in full and "
        "work only its own scope.")
FAILED = "ticket_context_continuation_failed"


def observed(gate: Any, role: str, runtime: str) -> dict[str, Any]:
    """What the pane shows about the conversation it holds; a missing fact stays missing (None), never guessed."""
    start = session_context.last_session_start(gate, role) if gate is not None else None
    identity = (getattr(gate, "role_identities", None) or {}).get(role) if gate is not None else None
    registered = None
    if start and identity is not None:
        registered = (start.get("pane_pid"), start.get("pane_start_time")) == (identity.pid, identity.start_time)
    checkout = session_context.current_checkout(gate, role) if gate is not None else {}
    return {"runtime": runtime or None, "session_id": (start or {}).get("session_id") or None,
            "registered_process": registered, "checkout": checkout or None}


def _scalar(conn: Any, statement: str, params: tuple) -> Any:
    result = conn.execute(statement, params)
    row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
    if row is None:
        return None
    return session_context._json(next(iter(row.values())) if isinstance(row, dict) else row[0])


def before_clear(clearer: Any, conn: Any, *, notification_id: int, ticket_id: str, target_role: str, kind: str,
                 target: str, attempts: int) -> bool | None:
    """True: continued, deliver the ticket with no clear. False: requeued. None: start fresh, clear as always."""
    trace = dict(conn=conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role, kind=kind)
    facts = observed(clearer.gate(), target_role, clearer.role_runtimes().get(target_role, ""))
    try:
        decision = _scalar(conn, "SELECT ticket_board.decide_ticket_context_continuation(%s, %s, %s::jsonb)",
                           (ticket_id, target_role, json.dumps(facts, sort_keys=True)))
    except Exception as exc:  # the check failed: fresh, said once here and in the trace
        clearer.logger.warning("Context continuity check for %s on %s failed; starting it fresh: %s", ticket_id,
                               target_role, exc)
        _trace(clearer, **trace, event="context_continuation_failed", busy_reason=FAILED,
               detail={"target": target, "error": str(exc)[:500], "observed": facts})
        return None
    if not isinstance(decision, dict) or decision.get("decision") != "continue":
        return None
    try:
        applied = _scalar(conn, "SELECT ticket_board.apply_ticket_context_continuation(%s, %s)", (ticket_id, target_role))
    except Exception as exc:  # decided but not bound: nothing is sent, and the retry binds it
        clearer.logger.warning("Could not bind %s to %s's continued conversation: %s", ticket_id, target_role, exc)
        _trace(clearer, **trace, event="context_continuation_failed", busy_reason=FAILED,
               detail={"target": target, "error": str(exc)[:500], "from_ticket": decision.get("from_ticket")})
        clearer.ledger.requeue(conn, notification_id, attempts, FAILED)
        return False
    if applied != "applied":
        return None  # its conversation lost its binding: the database made it fresh and told the Director
    _trace(clearer, **trace, event="context_continuation",
           detail={"target": target, "from_ticket": decision.get("from_ticket"), "shared_root": decision.get("shared_root"),
                   "shared_ancestor": decision.get("shared_ancestor"), "reason": decision.get("reason"),
                   "session_id": facts.get("session_id"), "attempts": attempts})
    return True


def _trace(clearer: Any, *, conn: Any, **fields: Any) -> None:
    try:
        clearer.ledger.trace(conn, **fields)
    except Exception as exc:  # a lost trace never changes what is delivered
        clearer.logger.warning("Could not record %s for %s: %s", fields.get("event"), fields.get("ticket_id"), exc)


def announced(conn: Any, ticket_id: str, target_role: str, message: str) -> str:
    """The ticket's notice, with the continuation it arrives in said first -- for its own assignment round only."""
    try:
        note = _scalar(conn, "SELECT ticket_board.ticket_context_continuation_note(%s, %s)", (ticket_id, target_role))
    except Exception:  # the notice itself is never held back by its preface
        return message
    if not isinstance(note, dict) or note.get("decision") != "continue" or not note.get("from_ticket"):
        return message
    return NOTE.format(ticket=ticket_id, previous=note["from_ticket"], root=note.get("shared_root") or note["from_ticket"]) \
        + "\n\n" + message


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    """force_fresh_context: the database checks the Director's authority and that nothing was decided yet."""
    if operation not in OPERATIONS or ticket_id is None or set(payload) - {"reason"}:
        raise ValueError("force_fresh_context takes a ticket and a reason")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.request_fresh_ticket_context(%s, %s) AS result",
                               (ticket_id, str(payload.get("reason") or ""))).fetchone()
    return dict(row["result"])


def recovery(app: Any, role: str) -> dict[str, Any]:
    """What a launch of `role` must resume (ticket_board.role_context_recovery); a read, for the launcher."""
    role = str(role or "").strip().lower()
    if not role:
        raise ValueError("context recovery names a role")
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.role_context_recovery(text)') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"role": role, "action": "unknown", "reason": "this board predates launch recovery (SYRD-626)"}
        row = conn.execute("SELECT ticket_board.role_context_recovery(%s) AS result", (role,)).fetchone()
    return dict(row["result"])
