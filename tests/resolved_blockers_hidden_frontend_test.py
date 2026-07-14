#!/usr/bin/env python3
"""Regression test: resolved blockers stay in data but disappear from board UI display."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "const visibleBlockedBy = unresolvedBlockedBy(ticket);" in HTML
    assert "linkedTicketRow(visibleBlockedBy)" in HTML
    assert "return 'No ticket blockers recorded.';" in HTML
    assert "return `Blocked by: ${formatBlockedByList(unresolved)}`;" in HTML
    assert "Resolved blockers:" not in HTML
    assert "Unresolved blockers:" not in HTML
    print("resolved_blockers_hidden_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
