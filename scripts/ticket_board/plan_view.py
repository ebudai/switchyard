"""The plan gate on the board page, the board service's half (SYRD-850).

* `status` is `GET /api/plan`: `{"plan": ticket_board.plan_status()}` on a board whose declared workflow runs the
  plan gate -- the approved commit and plan file, each milestone's deliverables with their tickets counted by state,
  the unplanned tickets counted apart, the milestones whose re-plan is due -- and `{"plan": null}` on any other
  board, including one whose gate was turned off and still holds the records. Read-only.
* `scope_sql` is the ticket read query's `plan_scope` column: the deliverable the ticket serves, or why it is
  unplanned, as the Director set it (`set_ticket_deliverable`); null when neither is set or the gate is off.

The page's half is `frontend_script_plan.py`. The records and their rules are SYRD-843's (pgu1052).
"""

from __future__ import annotations

from typing import Any

from .plan_gate import policy

PATH = "/api/plan"


def _first(row: Any) -> Any:
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def status(app: Any, _query: Any = None) -> dict[str, Any]:
    """The plan as the board sees it, or none on a board without the gate (or older than it)."""
    with app._pg_connect() as conn:
        if not _first(conn.execute("SELECT to_regprocedure('ticket_board.plan_status()') IS NOT NULL").fetchone()):
            return {"plan": None}
        plan = _first(conn.execute("SELECT ticket_board.plan_status()").fetchone())
    return {"plan": plan if isinstance(plan, dict) and isinstance(plan.get("policy"), dict) else None}


def scope_sql(workflow: dict[str, Any] | None) -> str:
    """The read query's `plan_scope` expression for the ticket row `t`: read only while the gate is declared."""
    if policy(workflow) is None:
        return "NULL::jsonb"
    return ("(SELECT jsonb_build_object('deliverable', td.deliverable, 'unplanned_reason', td.unplanned_reason) "
            "FROM ticket_board.ticket_deliverables td WHERE td.ticket_id = t.id)")
