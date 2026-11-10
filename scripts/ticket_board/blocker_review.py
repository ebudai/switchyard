"""The owner asks the Director to review a standing external blocker (SYRD-726).

`ticket-board-write request-blocker-review <id> --ref operator:eric --reason-file PATH` tells the Director, once, that
the blocker it names is ready to review, and why. It is for the role working the ticket in an implementation stage,
from that role's own pane, while the blocker stands -- when `request-dependency` is refused because of it. It
releases nothing and moves nothing: only the Director releases, replaces or keeps a blocker, and its release tells
the owner.

The same reason again is the request already made. A new one waits until the Director has acted on the last (a
Director comment on the ticket), or the blocker was released or recorded again.

Who is asking is never what the request says: it must come over the board's Unix socket under process authority, and
the database checks again that the process is the calling role's registered live runtime (as SYRD-723's
set_blocked_reason does).
"""

from __future__ import annotations

import json
from typing import Any

from .blocker_explanation import _calling_process

OPERATIONS = frozenset({"request_blocker_review"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str, ticket_id: str | None = None,
            handler: Any = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    others = sorted(set(payload) - {"ref", "reason"})
    if others:
        raise PermissionError(f"{operation} names a blocker and a reason only, not {others[0]}: the Director alone "
                              "releases, replaces or keeps a blocker")
    ref, reason = payload.get("ref"), payload.get("reason")
    if not isinstance(ref, str) or not isinstance(reason, str):
        raise ValueError(f"{operation} takes ref (the standing external blocker) and reason, both as text")
    pid, start_time, uid = _calling_process(handler, "a blocker review is asked for")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.request_blocker_review(%s, %s, %s, %s, %s, %s) AS result",
                               (ticket_id, ref, reason, pid, start_time, uid)).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return json.loads(result) if isinstance(result, str) else dict(result)
