#!/usr/bin/env python3
"""Regression test: user_review tickets open on the User's UAT panel, at the top of the detail (SYRD-629).

The panel replaced the earlier User sign-off banner, whose text was derived from the
ticket's body and implementation; uat_panel_browser_test drives the panel itself."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function renderUatPanel(ticket, draftFields = null)" in HTML
    assert ".uat-panel {" in HTML
    assert "'UAT: What you need to do'" in HTML
    assert "[['do_this', 'Do this'], ['you_should_see', 'You should see'], ['then_choose', 'Then choose']]" in HTML
    assert "`Prepared by ${roleLabel(instructions.prepared_by)} · last updated ${instructions.prepared_at}`" in HTML
    assert "'Director must prepare UAT steps'" in HTML
    assert "'UAT: Accepted ✓'" in HTML
    assert "'Questions or observations'" in HTML
    # The banner it replaced is gone: its steps were reconstructed from the body and implementation.
    for gone in (".user-banner", "Awaiting UAT sign-off", "Review this ticket during UAT, then sign off when it looks right.",
                 "function userReviewCheckText(ticket)", "firstNonEmptyLine(ticket.body)",
                 "userBannerSignoffButton", ".user-signoff-confirmation", "Signed-off UAT Snapshot"):
        assert gone not in HTML, gone
    print("user_review_banner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
