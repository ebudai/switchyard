#!/usr/bin/env python3
"""Regression test: board header omits the state-sequence blurb."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "<h1>PGU Ticket Board</h1>" in HTML
    assert "States: analysis -> in_progress -> director_review -> audit -> user_review -> done. Done is hidden by default." not in HTML
    assert "Show Done" in HTML
    print("board_header_blurb_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
