#!/usr/bin/env python3
"""Dump durable ticket-board notification trace rows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = os.environ.get("TICKET_BOARD_DATABASE_URL") or os.environ.get("DATABASE_URL", "")


def row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in row.items()
    }


def dump_trace(args: argparse.Namespace) -> int:
    filters: list[str] = []
    params: list[Any] = []
    if args.ticket_id:
        filters.append("ticket_id = %s")
        params.append(args.ticket_id.strip().upper())
    if args.notification_id is not None:
        filters.append("notification_id = %s")
        params.append(args.notification_id)

    where = "WHERE " + " AND ".join(filters) if filters else ""
    limit = max(1, args.limit)
    query = f"""
SELECT id,
       ts,
       ticket_id,
       notification_id,
       target_role,
       kind,
       event,
       ticket_state_at_event,
       ticket_assignee_at_event,
       pane_busy_determination,
       busy_reason,
       region_digest,
       detail
FROM ticket_board.notification_trace
{where}
ORDER BY ts DESC, id DESC
LIMIT %s
"""
    params.append(limit)
    with psycopg.connect(args.database, row_factory=dict_row) as conn:
        rows = [row_to_dict(row) for row in conn.execute(query, params)]

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    for row in rows:
        print(
            "{ts} #{id} ticket={ticket_id} notification={notification_id} "
            "target={target_role} kind={kind} event={event} "
            "ticket_state={ticket_state_at_event} assignee={ticket_assignee_at_event} "
            "pane={pane_busy_determination} reason={busy_reason} digest={region_digest}".format(**row)
        )
        detail = row.get("detail")
        if detail:
            print("  detail=" + json.dumps(detail, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dump ticket_board.notification_trace rows.")
    parser.add_argument("--database", default=DEFAULT_DATABASE_URL, help="PostgreSQL connection string.")
    parser.add_argument("--ticket-id", help="Filter to one ticket id, e.g. PGU-273.")
    parser.add_argument("--notification-id", type=int, help="Filter to one notification queue id.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.database:
        raise SystemExit("--database or TICKET_BOARD_DATABASE_URL/DATABASE_URL is required")
    return dump_trace(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
