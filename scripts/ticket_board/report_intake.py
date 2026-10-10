"""The Director's report-intake policy: where a tenant report that asks for Backlog lands (SYRD-548).

The database decides everything (`ticket_board.set_report_intake`,
`ticket_board.report_intake` and the five-argument `file_report`, migration
pgu981). This is the board action around it. A report may ask for `backlog`
and nothing else; it lands there only when the Director has said so here, and
in Triage otherwise, with the request written on the ticket either way.
"""

from __future__ import annotations

from typing import Any

OPERATIONS = frozenset({"set_report_intake"})
#: What a report may ask for. The empty request is the report every client sent before.
REQUESTABLE_STAGES = ("", "backlog")
_DECISIONS = {"backlog": True, "triage": False}


def requested_stage(value: object) -> str:
    """A report's requested stage, normalised, or ValueError for one it may not ask for."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("requested_stage must be a string")
    stage = value.strip().lower()
    if stage not in REQUESTABLE_STAGES:
        raise ValueError(f"a report may request backlog or nothing, not {value!r}")
    return stage


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
    if operation != "set_report_intake":
        raise ValueError(f"unknown report intake operation: {operation}")
    if set(payload) - {"backlog_requests", "reason"}:
        raise ValueError("set_report_intake takes backlog_requests and reason")
    decision = str(payload.get("backlog_requests") or "").strip().lower()
    if decision not in _DECISIONS:
        raise ValueError("backlog_requests must be backlog or triage")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(
                "SELECT ticket_board.set_report_intake(%s, %s) AS result",
                (_DECISIONS[decision], str(payload.get("reason") or "")),
            ).fetchone()
    return {"report_intake": dict(row["result"])}
