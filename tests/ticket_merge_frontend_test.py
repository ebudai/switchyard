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
    # SYRD-598: a merge is a Director operation the User confirms, with both tickets by ID and title, under
    # a grant for exactly that source and destination; the page no longer merges as the Director.
    assert "async function mergeTicket(" not in HTML
    assert "await runBoardUiOperation(ticket, 'merge', { target_id: targetId });" in HTML
    assert "line(`Merging ${prepared.ticket_id} (${prepared.title}) into ${prepared.target.ticket_id} (${prepared.target.title})`);" in HTML
    print("ticket_merge_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
