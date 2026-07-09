#!/usr/bin/env python3
"""Regression test: blocked_by is editable in the board frontend."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function parseBlockedByInput(value)" in HTML
    assert "function unresolvedBlockedBy(ticket)" in HTML
    assert "Blocked By" in HTML
    assert "blockedByInput.placeholder = 'PGU-23, PGU-25';" in HTML
    assert "await updateTicket(ticket.id, { blocked_by: parseBlockedByInput(blockedByInput.value) });" in HTML
    assert "blocked_by" in HTML
    print("blocked_by_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
