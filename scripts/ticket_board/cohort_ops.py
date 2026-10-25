"""The Director's cohort operations, and the read-only cohort status (SYRD-628).

* `set_cohort_quota` lowers a provider cohort's ceiling -- 0 is exhausted --
  until cleared or until it expires, with a reason;
* `clear_cohort_quota` removes the Director's record, with a reason;
* `record_cohort_change` is how convergence (`switchyard worker-pool converge`)
  writes down each start, stop and deferral, before and after it acts;
* `begin_cohort_drain` / `end_cohort_drain` bracket a stop: while a worker is
  draining no claim lands on it, so the stop cannot cut into new work;
* `status` is what `/api/cohorts` and `ticket-board-read cohorts` show.

Authority is the database's (`require_actor`), as for every board operation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

OPERATIONS = frozenset({"set_cohort_quota", "clear_cohort_quota", "record_cohort_change", "begin_cohort_drain",
                        "end_cohort_drain"})
CHANGE_KINDS = ("start", "stop", "deferred", "start_failed", "stop_failed")


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _whole(payload: dict[str, Any], key: str, *, optional: bool = False) -> int | None:
    value = payload.get(key)
    if value is None and optional:
        return None
    if type(value) is not int:
        raise ValueError(f"{key} must be a whole number")  # bounds are the database's
    return value


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if ticket_id is not None:
        raise ValueError(f"{operation} names its cohort in the request, not a ticket path")
    if operation == "set_cohort_quota":
        if set(payload) - {"runtime", "ceiling", "reason", "expires_minutes"}:
            raise ValueError("set_cohort_quota takes runtime, ceiling, reason and expires_minutes")
        minutes = _whole(payload, "expires_minutes", optional=True)
        sql = "SELECT ticket_board.set_cohort_quota(%s, %s, %s, %s) AS result"
        params: tuple[Any, ...] = (_text(payload, "runtime"), _whole(payload, "ceiling"), _text(payload, "reason"),
                                   None if minutes is None else minutes * 60)
    elif operation == "clear_cohort_quota":
        if set(payload) - {"runtime", "reason"}:
            raise ValueError("clear_cohort_quota takes runtime and reason")
        sql = "SELECT ticket_board.clear_cohort_quota(%s, %s) AS result"
        params = (_text(payload, "runtime"), _text(payload, "reason"))
    elif operation == "record_cohort_change":
        if set(payload) - {"runtime", "role", "kind", "source", "reason", "detail"}:
            raise ValueError("record_cohort_change takes runtime, role, kind, source, reason and detail")
        detail = payload.get("detail") or {}
        if not isinstance(detail, dict):
            raise ValueError("detail must be an object")
        from psycopg.types.json import Jsonb

        sql = "SELECT ticket_board.record_cohort_change(%s, %s, %s, %s, %s, %s) AS result"
        params = (_text(payload, "runtime"), _text(payload, "role"), _text(payload, "kind"),
                  _text(payload, "source"), _text(payload, "reason"), Jsonb(detail))
    elif operation == "begin_cohort_drain":
        if set(payload) - {"role", "reason", "seconds"}:
            raise ValueError("begin_cohort_drain takes role, reason and seconds")
        sql = "SELECT ticket_board.begin_cohort_drain(%s, %s, %s) AS result"
        params = (_text(payload, "role"), _text(payload, "reason"), _whole(payload, "seconds", optional=True) or 600)
    elif operation == "end_cohort_drain":
        if set(payload) - {"role"}:
            raise ValueError("end_cohort_drain takes role")
        sql = "SELECT ticket_board.end_cohort_drain(%s) AS result"
        params = (_text(payload, "role"),)
    else:
        raise ValueError(f"unknown cohort operation: {operation}")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(sql, params).fetchone()
    return {"result": _plain(row["result"])}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value.isoformat() if isinstance(value, datetime) else value


def status(app: Any) -> dict[str, Any]:
    """The board's cohorts, read-only; empty on a board older than them."""
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.cohort_status(timestamptz)') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"cohorts": [], "events": []}
        row = conn.execute("SELECT ticket_board.cohort_status(clock_timestamp()) AS status").fetchone()
    return _plain(row["status"])


def describe(payload: dict[str, Any]) -> str:
    """`ticket-board-read cohorts`: one block per cohort, then the latest changes."""
    lines: list[str] = []
    for c in payload.get("cohorts") or []:
        pins = " ".join(f"{key}={c[key]}" for key in ("model", "effort") if c.get(key))
        limit = c.get("effective_ceiling")
        lines.append(f"{c['runtime']}: target {c.get('target', '-')}, ceiling {c.get('ceiling', '-')}, "
                     f"now {'unlimited' if limit is None else limit}" + (f" ({pins})" if pins else ""))
        held = c.get("holding") or []
        lines.append(f"  holding {len(held)} of {len(c.get('members') or [])} member(s)"
                     + (": " + ", ".join(f"{h['role']}={h['ticket']}" for h in held) if held else ""))
        for q in c.get("quota") or []:
            until = f" until {q['expires_at']}" if q.get("expires_at") else ""
            lines.append(f"  quota {q['source']}: ceiling {q['ceiling']}{until} -- {q['reason']}")
        lines.append("  usage telemetry: " + ("a provider signal" if c.get("telemetry") == "signal"
                                              else "none; static limits only"))
    if not lines:
        return "No scheduling cohorts are declared."
    events = payload.get("events") or []
    if events:
        lines.append("recent changes:")
        for e in events[:10]:
            who = f" {e['role']}" if e.get("role") else ""
            lines.append(f"  {e['at']} {e['runtime']}{who} {e['change']} ({e['source']}, {e['actor']}): {e['reason']}")
    return "\n".join(lines)
