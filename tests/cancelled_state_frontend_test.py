#!/usr/bin/env python3
"""Regression test: cancelled is a hidden terminal column with an explicit reasoned action."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert 'id="showCancelledInput"' in HTML
    assert 'id="showCancelledCount"' in HTML
    assert "showCancelled: false," in HTML
    assert "const showCancelledInput = document.getElementById('showCancelledInput');" in HTML
    assert "const showCancelledCountEl = document.getElementById('showCancelledCount');" in HTML
    assert "if (column.key === 'cancelled' && !state.showCancelled) {" in HTML
    assert "const cancelledCount = columnTicketCount('cancelled');" in HTML
    assert "showCancelledInput.checked = state.showCancelled;" in HTML
    assert "showCancelledCountEl.textContent = `(${cancelledCount})`;" in HTML
    assert "showCancelledInput.addEventListener('change', () => {" in HTML
    # SYRD-598: cancelling is a Director operation. The page no longer cancels as the Director; the server
    # describes cancel with its required reason, and the User confirms it (board_ui_delegation tests).
    assert "function cancelReason(text)" not in HTML
    assert "async function cancelTicket(ticketId, text)" not in HTML
    assert "who: 'director'," not in HTML
    assert "}, 'director');" not in HTML
    assert "cancelButton.textContent = 'Cancel Ticket';" not in HTML
    assert "void renderBoardUiActions(ticket, workflowActions);" in HTML
    print("cancelled_state_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
