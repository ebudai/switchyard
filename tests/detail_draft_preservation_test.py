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
    assert "function markDetailDraftFieldsSaved(ticketId, savedFields)" in HTML
    assert "element.dataset.serverValue = normalizedValue;" in HTML
    assert "delete state.detailDraft.fields[key];" in HTML
    assert "function savedDraftFieldsForPatch(patch, consumed)" in HTML
    assert "parent_id: 'parentId'," in HTML
    assert "implementation: 'implementation'," in HTML
    assert "audit_prompt: 'auditPrompt'," in HTML
    assert "commit_hash: 'commitHash'," in HTML
    assert "markDetailDraftFieldsSaved(ticketId, savedDraftFieldsForPatch(patch, consumed));" in HTML
    assert "markDetailDraftFieldsSaved(ticketId, savedDraftFieldsForPatch(patch, consumed));\n      await requestBoardReload();" in HTML
    assert "bindDetailDraftField(draftFields, commentText, 'commentText', '');" in HTML
    assert "rememberDetailDraft();" in HTML
    assert "clearDetailDraft(ticketId);" in HTML
    print("detail_draft_preservation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
