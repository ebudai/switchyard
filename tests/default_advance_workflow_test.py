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
    assert "{ key: 'ready', label: 'Ready' }" not in HTML
    assert "stateLabel(key)" in HTML
    assert "columnKey !== 'user_review' || ticket.needs_user_signoff" not in HTML
    assert "Only tickets flagged for UAT appear here." not in HTML
    # SYRD-598: the page keeps no transition table and moves nothing as the Director. What may be done for a
    # ticket -- a legacy board's routes included, with the stages its workflow_transitions allow -- is what
    # the server describes, and the User confirms it; the database enforces each transition's gates.
    for gone in ("function defaultAdvanceState(ticket)", "function advanceBlockedReason(ticket)",
                 "function stateTransitionCallerRole(ticket)", "Advance -> ${stateLabel(detailAdvanceState)}",
                 "await advanceTicket(ticket.id, commentText.value);"):
        assert gone not in HTML, gone
    assert "return boardUiRequest({ describe: true, ticket_id: ticketId });" in HTML
    assert "void renderBoardUiActions(ticket, workflowActions);" in HTML
    assert "nextState === 'in_progress' && Object.prototype.hasOwnProperty.call(patch, 'assignee')" in HTML
    assert "{ state: nextState, assignee: patch.assignee }" in HTML
    assert "return 'ready';" not in HTML
    assert "function isImplementerAssignee(role)" in HTML
    assert "Save implementation before advancing to Implementation." not in HTML
    assert "already has an in-progress ticket" not in HTML
    assert "Save audit prompt before advancing to audit." not in HTML
    print("default_advance_workflow_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
