"""The Director's lease operations, and the read-only lease status (SYRD-616).

* `extend_lease` moves a live lease's deadline by 1 minute to 4 hours, with a
  reason; the board records who, why and the new deadline.
* `interrupt_lease` asks for a live lease's process group to be interrupted;
  the listener, which runs as the pane's account, carries it out
  (`execution_lease_pass`).
* `status` is what `ticket-board-read leases` and `/api/execution-leases` show:
  open leases, newest first, and with `include_ended` the ended ones the board
  still keeps.

Authority is the database's (`require_actor`), as for every board operation.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

OPERATIONS = frozenset({"extend_lease", "interrupt_lease"})
SHOWN = ("id", "role", "ticket_id", "state", "pid", "pgid", "started_at", "command", "deadline", "deadline_source",
         "hard", "turn_started_at", "evidence", "extensions", "alert_kind", "alerted_at", "alert_cleared_at",
         "alert_clear_reason", "interrupt", "first_seen_at", "last_seen_at", "ended_at", "end_reason")


def _lease_id(payload: dict[str, Any]) -> int:
    value = payload.get("lease")
    if type(value) is not int or value <= 0:
        raise ValueError("lease must be a lease number")
    return value


def _reason(payload: dict[str, Any]) -> str:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reason is required")
    return reason.strip()


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if ticket_id is not None:
        raise ValueError(f"{operation} names its lease in the request, not a ticket path")
    if operation == "extend_lease":
        if set(payload) - {"lease", "minutes", "reason"}:
            raise ValueError("extend_lease takes lease, minutes and reason")
        minutes = payload.get("minutes")
        if type(minutes) is not int:
            raise ValueError("minutes must be a whole number")  # the bound is the database's
        sql = "SELECT ticket_board.extend_execution_lease(%s, %s, %s) AS lease"
        params: tuple[Any, ...] = (_lease_id(payload), minutes * 60, _reason(payload))
    elif operation == "interrupt_lease":
        if set(payload) - {"lease", "reason"}:
            raise ValueError("interrupt_lease takes lease and reason")
        sql = "SELECT ticket_board.request_execution_lease_interrupt(%s, %s) AS lease"
        params = (_lease_id(payload), _reason(payload))
    else:
        raise ValueError(f"unknown lease operation: {operation}")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(sql, params).fetchone()
    return {"lease": _plain(dict(row["lease"]))}


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    return {key: (value.isoformat() if isinstance(value, datetime) else value) for key, value in row.items() if key in SHOWN}


def status(app: Any, *, role: str = "", include_ended: bool = False) -> dict[str, Any]:
    """The board's leases, read-only; empty on a board older than them."""
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regclass('ticket_board.execution_leases') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"leases": []}
        rows = conn.execute(
            "SELECT * FROM ticket_board.execution_leases WHERE (%s = '' OR role = %s) AND (%s OR ended_at IS NULL) "
            "ORDER BY ended_at IS NOT NULL, first_seen_at DESC LIMIT 200", (role, role, include_ended)).fetchall()
    return {"leases": [_plain(dict(row)) for row in rows]}


def _ago(value: Any, now: float) -> str:
    if not value:
        return "-"
    seconds = int(now - datetime.fromisoformat(str(value)).timestamp())
    sign, seconds = ("in " if seconds < 0 else ""), abs(seconds)
    text = f"{seconds // 3600} h {seconds % 3600 // 60} min" if seconds >= 3600 else f"{seconds // 60} min {seconds % 60} s"
    return sign + text + ("" if sign else " ago")


def describe(payload: dict[str, Any], *, now: float | None = None) -> str:
    """`ticket-board-read leases`, for a person."""
    now = time.time() if now is None else now
    leases = payload.get("leases") or []
    if not leases:
        return "No execution leases."
    lines = []
    for lease in leases:
        interrupt = lease.get("interrupt") or {}
        lines.append(
            f"lease {lease['id']}  {lease['role']}  {lease.get('ticket_id') or '(no ticket)'}  {lease['state']}  "
            f"pid {lease['pid']} pgid {lease['pgid']}\n"
            f"  started {_ago(lease['started_at'], now)}; deadline {_ago(lease['deadline'], now)} ({lease['deadline_source']}"
            f"{', hard' if lease.get('hard') else ''})\n"
            f"  `{lease['command']}`"
            + (f"\n  alerted {_ago(lease['alerted_at'], now)} ({lease['alert_kind']})"
               + (f", cleared: {lease['alert_clear_reason']}" if lease.get("alert_cleared_at") else "")
               if lease.get("alerted_at") else "")
            + "".join(f"\n  extended by {e.get('by')}: {e.get('reason')} -> {e.get('deadline')}"
                      for e in lease.get("extensions") or [])
            + (f"\n  interrupt by {interrupt.get('requested_by')}: {interrupt.get('reason')} [{interrupt.get('stage')}]"
               if interrupt else "")
            + (f"\n  ended {_ago(lease['ended_at'], now)}: {lease.get('end_reason')}" if lease.get("ended_at") else ""))
    return "\n".join(lines)
