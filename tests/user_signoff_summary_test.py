#!/usr/bin/env python3
"""Regression test: the User's UAT panel shows the Director's steps, not a summary of the ticket (SYRD-629).

Background stays outside the panel: the earlier banner summarised the body,
implementation, Audit prompt and every sign-off above the User's decision."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "const instructions = ticket.uat && ticket.uat.instructions;" in HTML
    assert "if (instructions && instructions.current) {" in HTML
    # A ticket without current steps shows the Director's latest explicit UAT request, never one rebuilt from history.
    assert "function uatLegacyRequest(ticket)" in HTML
    assert "/^\\s*(\\*\\*)?\\s*(user\\s+)?UAT\\b/i" in HTML
    for gone in (".user-summary", "UAT Check Before Sign-off", "function checklistItemsFromText(text)",
                 "function userReviewSummarySections(ticket)", "function userReviewStatusItems(ticket)",
                 "['Body', ticket.body]", "['Audit Prompt', ticket.audit_prompt]",
                 "{ label: 'UAT sign-off', ok: !ticket.needs_user_signoff || !!ticket.user_signoff }"):
        assert gone not in HTML, gone
    print("user_signoff_summary_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
