"""The events that need the User, as this board recorded them, for the host's phone bridge (SYRD-714).

GET /api/user-events?after=<id>&limit=<n> answers `ticket_board.user_events_after`: at most 50 events after an id,
oldest first, each with its stable key, kind, ticket, short title and whether it still stands, and the latest id the
board holds. It is a read and nothing else: it acknowledges, suppresses and changes nothing, and it never reads the
role-pane notification queue. A board whose schema predates the record answers `{"available": false}`.
"""

from __future__ import annotations

import json
from typing import Any

PATH = "/api/user-events"
MAX_EVENTS = 50


def _number(query: dict[str, list[str]], name: str, default: int) -> int:
    text = (query.get(name) or [""])[0].strip()
    if not text:
        return default
    if not text.isdigit() or len(text) > 18:
        raise ValueError(f"{name} must be a non-negative whole number")
    return int(text)


def read(app: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    after, limit = _number(query, "after", 0), min(max(_number(query, "limit", 20), 1), MAX_EVENTS)
    with app._pg_connect() as conn:
        row = conn.execute("SELECT to_regprocedure('ticket_board.user_events_after(bigint, integer)') IS NOT NULL "
                           "AS present").fetchone()
        if not (row["present"] if isinstance(row, dict) else row[0]):
            return {"available": False, "project": getattr(app, "project", "")}
        row = conn.execute("SELECT ticket_board.user_events_after(%s, %s) AS events", (after, limit)).fetchone()
    value = row["events"] if isinstance(row, dict) else row[0]
    answer = json.loads(value) if isinstance(value, str) else dict(value or {})
    return {"available": True, "project": getattr(app, "project", ""), **answer}
