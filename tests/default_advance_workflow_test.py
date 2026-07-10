#!/usr/bin/env python3
"""Regression test: board exposes a default advance workflow with explicit guards."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function defaultAdvanceState(ticket)" in HTML
    assert "return ticket.needs_eric_signoff ? 'eric_review' : 'director_review';" in HTML
    assert "if (ticket.state === 'eric_review') {" in HTML
    assert "return 'director_review';" in HTML
    assert "if (ticket.state === 'director_review') {" in HTML
    assert "return 'done';" in HTML
    assert "function advanceBlockedReason(ticket)" in HTML
    assert "return 'ready';" in HTML
    assert "Assign the ticket before advancing to ready." in HTML
    assert "Save implementation before advancing to ready." in HTML
    assert "Assign the ticket before advancing to in progress." in HTML
    assert "Save implementation before advancing to in progress." in HTML
    assert "already has an in-progress ticket" in HTML
    assert "rootTicketForBoard(item).id !== ticketRootId" in HTML
    assert "Save audit prompt before advancing to audit." not in HTML
    assert "Set audit signoff before advancing to Eric review." in HTML
    assert "Set audit signoff before advancing to Director Review." in HTML
    assert "Record Eric signoff before advancing to Director Review." in HTML
    assert "Save a verified commit hash or enable no-commit override before advancing to done." in HTML
    assert "Advance -> ${stateLabel(detailAdvanceState)}" in HTML
    assert "await advanceTicket(ticket.id);" in HTML
    print("default_advance_workflow_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
