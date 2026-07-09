#!/usr/bin/env python3
"""Regression test: creating a ticket should not auto-open detail."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_script_app import SCRIPT_APP


def main() -> int:
    assert "async function createTicket()" in SCRIPT_APP
    assert "setCreateStatus(`Created ${result.ticket.id}.`);" in SCRIPT_APP
    assert "state.selectedId = result.ticket.id;" not in SCRIPT_APP
    assert "state.detailOpen = true;" not in SCRIPT_APP
    print("create_ticket_stays_on_board_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
