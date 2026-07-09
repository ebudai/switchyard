#!/usr/bin/env python3
"""Regression test: board cards show an explicit assignee row."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".card-assignee" in HTML
    assert ".card-assignee-label" in HTML
    assert ".card-assignee-value" in HTML
    assert "assigneeLabel.textContent = 'assignee';" in HTML
    assert "assigneeValue.textContent = ticket.assignee;" in HTML
    assert "titleWrap.append(idEl, titleEl, assigneeLine);" in HTML
    print("board_card_assignee_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
