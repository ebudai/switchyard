#!/usr/bin/env python3
"""Regression test: ticket detail exposes the director merge control."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "Merge Into…" in HTML
    assert "Merge target ticket" in HTML
    assert "window.confirm(" in HTML
    assert "body: JSON.stringify({ target_id: targetTicketId, actor: 'director' })" in HTML
    assert "Merged ${sourceTicketId} into ${targetTicketId}." in HTML
    print("ticket_merge_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
