#!/usr/bin/env python3
"""Regression test: post-sign-off UI state is obvious on cards and in detail."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function userSignoffSummary(ticket)" in HTML
    assert "if (ticket.state === 'director_review') {" in HTML
    assert "card.classList.add('card-signed-off');" in HTML
    assert "card-signoff-state" in HTML
    assert "signoffState.textContent = ticket.user_signoff ? 'Signed Off ✓' : 'Awaiting Sign-off';" in HTML
    assert "Signed off ✓ ${ticketId} moved to Final Sign-Off." in HTML
    assert "UAT signed off. In Final Sign-Off." in HTML
    assert "UAT signed off. Waiting for Final Sign-Off." in HTML
    assert "Signed off ✓ Waiting for director completion." in HTML
    assert "UAT sign-off recorded. Waiting for director completion." in HTML
    assert "userBannerSubtitle.textContent = signoffRecorded ? 'Signed Off ✓' : 'Awaiting UAT sign-off';" in HTML
    assert "userSummaryHead.textContent = signoffRecorded ? 'Signed-off UAT Snapshot' : 'UAT Check Before Sign-off';" in HTML
    print("user_signoff_feedback_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
