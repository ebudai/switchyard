#!/usr/bin/env python3
"""Regression test: linked ticket groups collapse into a parent card in the frontend."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function normalizedParentId(ticket)" in HTML
    assert "function childTicketsForBoard(ticket)" in HTML
    assert "function buildChildTicketList(children" in HTML
    assert "Save Parent Link" in HTML
    assert "linked child" in HTML
    assert "parent_id" in HTML
    print("linked_ticket_grouping_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
