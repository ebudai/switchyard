#!/usr/bin/env python3
"""Regression test: blocked reasons are surfaced high on cards and detail views."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function manualBlockedSummary(ticket)" in HTML
    assert "function guardBlockedSummary(ticket)" in HTML
    assert "function renderAlertStack(ticket, { detail = false } = {})" in HTML
    assert "Blocked Reason" in HTML
    assert "Save Blocked Reason" in HTML
    assert "Advance Blocked" in HTML
    assert "card.classList.add('card-blocked');" in HTML
    assert "blocked_reason: blockedReasonInput.value" in HTML
    assert "Required when blocked-by dependencies are set. Also use this for non-dependency stalls." in HTML
    assert "Blocked by ${formatBlockedByList(unresolved)}. Add a blocked reason." in HTML
    print("blocked_reason_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
