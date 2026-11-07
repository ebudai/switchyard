"""The Director restores a mistaken reopen (SYRD-746).

`ticket-board-write restore-reopened <id> --reason "..."` puts back the closed disposition the ticket's last reopen
took away -- its stage, owner, commit provenance and sign-offs, at once -- as the board recorded it when the ticket left
its terminal stage. It is refused unless nothing has happened since: the ticket still where the reopen left it, with no
candidate and no sign-off of its own. It never makes a sign-off that was not there or a commit that was not recorded,
and it records the repair: a comment, a field-audit row for each field it moves, and the record marked restored.

Authority is the database's: `restore_reopened_ticket` requires the Director's `director_edit`.
"""

from __future__ import annotations

import json
from typing import Any

OPERATIONS = frozenset({"restore_reopened"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    if set(payload) - {"reason"}:
        raise ValueError(f"{operation} takes a reason only: what it restores is what the board recorded at the reopen")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{operation} needs a reason: what the reopen mistook")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.restore_reopened_ticket(%s, %s) AS result",
                               (ticket_id, reason.strip())).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return json.loads(result) if isinstance(result, str) else dict(result)
