#!/usr/bin/env python3
"""Regression test: user_review tickets expose a top-level User sign-off banner."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".user-banner" in HTML
    assert "Awaiting UAT sign-off" in HTML
    assert "Signed Off ✓" in HTML
    assert "Review this ticket during UAT, then sign off when it looks right." in HTML
    assert "UAT sign-off recorded. Waiting for director completion." in HTML
    assert "function ericReviewCheckText(ticket)" in HTML
    assert "firstNonEmptyLine(ticket.implementation)" in HTML
    assert "firstNonEmptyLine(ticket.body)" in HTML
    assert "ericBannerNote.textContent = signoffRecorded" in HTML
    assert ".user-summary" in HTML
    assert ".user-banner-confirmed" in HTML
    assert ".user-signoff-confirmation" in HTML
    assert "ericBannerSignoffButton.type = 'button';" in HTML
    assert "ericBannerSignoffButton.textContent = 'Sign Off';" in HTML
    assert "await submitEricSignoff(ticket.id, commentWho.value, commentText.value);" in HTML
    assert "Signed-off UAT Snapshot" in HTML
    print("user_review_banner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
