#!/usr/bin/env python3
"""Regression test: board columns sort tickets oldest-first by created time and PGU number."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function ticketNumber(ticketId)" in HTML
    assert "function compareTicketsOldestFirst(left, right)" in HTML
    assert "const createdCompare = (left.created || '').localeCompare(right.created || '');" in HTML
    assert "return ticketNumber(left.id) - ticketNumber(right.id);" in HTML
    assert ".sort(compareTicketsOldestFirst);" in HTML
    print("column_oldest_order_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
