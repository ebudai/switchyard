#!/usr/bin/env python3
"""Regression test: ticket create/detail forms expose the regression flag."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_markup import MARKUP
from scripts.ticket_board.frontend_script_app import SCRIPT_APP
from scripts.ticket_board.frontend_script_core import SCRIPT_CORE
from scripts.ticket_board.frontend_script_detail import SCRIPT_DETAIL


def main() -> int:
    assert 'id="createRegressionInput"' in MARKUP
    assert "Regression" in MARKUP
    assert "const createRegressionInput = document.getElementById('createRegressionInput');" in SCRIPT_CORE
    assert "regression: createRegressionInput.checked" in SCRIPT_APP
    assert "createRegressionInput.checked = false;" in SCRIPT_APP
    assert "createRegressionInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    assert "toggleControl('Regression', ticket.regression" in SCRIPT_DETAIL
    assert "await updateTicket(ticket.id, { regression: checked }, detailCallerRole());" in SCRIPT_DETAIL
    assert "toggleControl('Manual control', ticket.manually_controlled" in SCRIPT_DETAIL
    assert "await updateTicket(ticket.id, { manually_controlled: checked }, 'director');" in SCRIPT_DETAIL
    assert "Director-only: hold or release this ticket outside automatic workflow movement." in SCRIPT_DETAIL
    assert "await updateTicketAction(ticketId, 'set_manually_controlled', { manually_controlled: patch.manually_controlled }, normalizedCaller);" in SCRIPT_APP
    assert "if (ticket.regression)" in SCRIPT_CORE
    assert "badges.appendChild(badge('regression', false));" in SCRIPT_CORE
    print("ticket_regression_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
