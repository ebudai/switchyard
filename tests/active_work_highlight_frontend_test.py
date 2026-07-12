#!/usr/bin/env python3
"""Regression test: board cards highlight the notification-inferred active ticket."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".card-active-work" in HTML
    assert "ticket.active_work_highlight" in HTML
    assert "card.classList.add('card-active-work');" in HTML
    print("active_work_highlight_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
