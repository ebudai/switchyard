"""The Director's pause switch, the board service's half (SYRD-756).

* `pause_work` sets the pause -- `drain` or `stop` -- or changes its mode, with a reason; `resume_work` ends it,
  with a reason only if one is given (SYRD-768). Either is the Director's or the User's: the database decides (`ticket_board.work_pause_actor`),
  as the server's role policy asks first (`OPERATOR_OR_CONTROL_OPERATIONS`). The User reaches them from the board
  page's switches.
* `status` is what `GET /api/work-pause` and `ticket-board-read pause` show: mode, who set it, why, since when, every
  change, what is held for whom, and what a stop did to each role. Read-only.

The listener does the rest (`work_pause`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

OPERATIONS = frozenset({"pause_work", "resume_work"})
MODES = ("drain", "stop")
PATH = "/api/work-pause"


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if ticket_id is not None:
        raise ValueError(f"{operation} is the project's, not a ticket's")
    allowed = {"mode", "reason"} if operation == "pause_work" else {"reason"}
    if set(payload) - allowed:
        raise ValueError(f"{operation} takes {' and '.join(sorted(allowed))}")
    reason = payload.get("reason")
    if operation == "resume_work" and reason is None:
        reason = ""  # SYRD-768: a resume only puts things back; its reason is optional
    if not isinstance(reason, str) or (operation == "pause_work" and not reason.strip()):
        raise ValueError(f"{operation} needs a reason" if operation == "pause_work" else "a resume's reason is text")
    mode = payload.get("mode")
    if operation == "pause_work" and mode not in MODES:
        raise ValueError("pause_work needs a mode: drain (no notifications) or stop (interrupt running turns too)")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            if operation == "pause_work":
                row = conn.execute("SELECT ticket_board.pause_work(%s, %s) AS result", (mode, reason.strip())).fetchone()
            else:
                row = conn.execute("SELECT ticket_board.resume_work(%s) AS result", (reason.strip(),)).fetchone()
    return {"pause": _plain(row["result"] if isinstance(row, dict) else row[0])}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value.isoformat() if isinstance(value, datetime) else value


def status(app: Any, _query: Any = None) -> dict[str, Any]:
    """The pause, read-only; "not paused" on a board older than it."""
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.work_pause_status()') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"paused": False, "pause": None, "recent": []}
        row = conn.execute("SELECT ticket_board.work_pause_status() AS status").fetchone()
    return _plain(row["status"] if isinstance(row, dict) else row[0])
