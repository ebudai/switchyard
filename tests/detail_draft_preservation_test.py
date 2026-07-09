#!/usr/bin/env python3
"""Regression test: detail-pane drafts survive live board rerenders."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "detailDraft: null," in HTML
    assert "function rememberDetailDraft()" in HTML
    assert "detailContentEl.querySelectorAll('[data-draft-key]')" in HTML
    assert "function restoreDetailDraft(ticketId, fields)" in HTML
    assert "element.setSelectionRange(entry.selectionStart, entry.selectionEnd);" in HTML
    assert "bindDetailDraftField(draftFields, commentText, 'commentText', '');" in HTML
    assert "rememberDetailDraft();" in HTML
    assert "clearDetailDraft(ticketId);" in HTML
    print("detail_draft_preservation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
