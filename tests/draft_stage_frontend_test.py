#!/usr/bin/env python3
"""Regression test: draft tickets have an opt-in UI and release through release_draft."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert '"draft": "Draft"' in HTML
    assert '"analysis": "Triage"' in HTML
    assert 'id="createDraftInput"' in HTML
    assert "Create as draft" in HTML
    assert "initial_state: createDraftInput.checked ? 'draft'" in HTML
    assert "createDraftInput.checked = false;" in HTML
    assert "createDraftInput.addEventListener('keydown', submitCreateOnEnter);" in HTML
    assert "if (ticket.state === 'draft') {\n        return 'analysis';" in HTML
    assert "releaseButton.textContent = 'Release to Triage';" in HTML
    assert "await updateTicketAction(ticketId, 'release_draft', {}, normalizedCaller);" in HTML
    assert "detail-stage-draft" in HTML
    print("draft_stage_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
