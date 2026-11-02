"""An implementer keeps its own ticket's blocker explanation current (SYRD-723).

`ticket-board-write set-blocked-reason <id> --blocked-reason "..."` replaces the ticket's `blocked_reason`, and
`--clear` empties it. That text is all it touches. It is for the role that owns the ticket in an implementation
stage, from that role's own pane; the Director's `set-blockers` and the board UI's edit are unchanged.

The explanation is a note (SYRD-285): saving one creates no wait, and clearing one ends none. A dependency is asked
for with `request-dependency`, and set and released by the Director; the work moves only by its declared
transition. While a blocker stands, its explanation can be revised but not removed.

Who is asking is never what the request says:
- the request must come over the board's Unix socket under process authority, from a pane the kernel names
  (`SO_PEERCRED`) and the board resolves to its registered runtime;
- the database checks again that this process is the calling role's registered live runtime, that the role owns the
  ticket in an implementation stage, and records the change in the field audit with that runtime.
"""

from __future__ import annotations

import json
from typing import Any

OPERATIONS = frozenset({"set_blocked_reason"})
#: What a caller might try to change alongside the text, and the way that change is actually made.
ELSEWHERE = {
    "blocked_by": "a dependency is asked for with request-dependency, and set or released by the Director",
    "state": "the work moves only by its declared transition",
    "assignee": "a new owner is the Director's reassign",
    "awaiting_role": "a wait is request-dependency or await-role, and clear-awaiting-role",
}


def _calling_process(handler: Any, what: str = "a blocker explanation is set") -> tuple[int, int, int]:
    """(pid, start time, uid) of the registered pane making this request, as the kernel names it."""
    authority = getattr(handler, "role_authority", None)
    if handler is None or authority is None or not hasattr(authority, "session_for_peer"):
        raise PermissionError(f"{what} only from the owning role's own pane, over the board's socket under process "
                              "authority")
    credentials = handler.require_allowed_peer()
    identity = authority.session_for_peer(credentials)
    return identity.pid, identity.start_time, credentials.uid


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str, ticket_id: str | None = None,
            handler: Any = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    others = sorted(set(payload) - {"blocked_reason"})
    if others:
        raise PermissionError(f"{operation} changes only the blocker explanation, not {others[0]}"
                              + (f": {ELSEWHERE[others[0]]}" if others[0] in ELSEWHERE else ""))
    reason = payload.get("blocked_reason")
    if not isinstance(reason, str):
        raise ValueError(f"{operation} takes blocked_reason as text; empty text clears it")
    pid, start_time, uid = _calling_process(handler)
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.set_blocked_reason(%s, %s, %s, %s, %s) AS result",
                               (ticket_id, reason, pid, start_time, uid)).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return json.loads(result) if isinstance(result, str) else dict(result)
