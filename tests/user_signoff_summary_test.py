#!/usr/bin/env python3
"""Regression test: UAT banner shows a consolidated sign-off summary."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".user-summary" in HTML
    assert "UAT Check Before Sign-off" in HTML
    assert "function checklistItemsFromText(text)" in HTML
    assert "function ericReviewSummarySections(ticket)" in HTML
    assert "function ericReviewStatusItems(ticket)" in HTML
    assert "Audit sign-off" in HTML
    assert "{ label: 'UAT sign-off', ok: !ticket.needs_user_signoff || !!ticket.user_signoff }" in HTML
    assert "Inspector sign-off" in HTML
    assert "Commit evidence" in HTML
    assert "['Implementation', ticket.implementation]" in HTML
    assert "['Body', ticket.body]" in HTML
    assert "['Audit Prompt', ticket.audit_prompt]" in HTML
    assert "ericBanner.append(ericBannerSubtitle, ericBannerTitle, ericBannerNote, ericSummary, ericBannerActions);" in HTML
    print("user_signoff_summary_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
