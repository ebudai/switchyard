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
    assert "function defaultAdvanceState(ticket)" in HTML
    assert "if (ticket.state === 'backlog') {" in HTML
    assert "return 'analysis';" in HTML
    assert "if (ticket.state === 'analysis') {" in HTML
    assert "return 'in_progress';" in HTML
    assert "ticket.needs_audit !== false ? 'audit' : (ticket.needs_user_signoff ? 'dat' : 'director_review')" in HTML
    assert "return ticket.needs_user_signoff ? 'dat' : 'director_review';" in HTML
    assert "if (ticket.state === 'dat') {" in HTML
    assert "return 'user_review';" in HTML
    assert "if (ticket.state === 'user_review') {" in HTML
    assert "return 'director_review';" in HTML
    assert "if (ticket.state === 'director_review') {" in HTML
    assert "return 'done';" in HTML
    assert "function advanceBlockedReason(ticket)" in HTML
    assert "nextState === 'in_progress' && Object.prototype.hasOwnProperty.call(patch, 'assignee')" in HTML
    assert "{ state: nextState, assignee: patch.assignee }" in HTML
    assert "return 'ready';" not in HTML
    assert "function isImplementerAssignee(role)" in HTML
    assert "Assign an implementer before advancing to Implementation." in HTML
    assert "Resolve blockers before advancing: ${formatBlockedByList(unresolved)}." in HTML
    assert "Save implementation before advancing to Implementation." not in HTML
    assert "already has an in-progress ticket" not in HTML
    assert "Save audit prompt before advancing to audit." not in HTML
    assert "Set audit signoff before advancing to DAT." in HTML
    assert "Set audit signoff before advancing to Final Sign-Off." in HTML
    assert "ticket.needs_audit === false || !!ticket.audit_signoff" in HTML
    assert "Record UAT sign-off before advancing to Final Sign-Off." in HTML
    assert "Save a verified commit hash or enable no-commit override before advancing to done." in HTML
    assert "Advance -> ${stateLabel(detailAdvanceState)}" in HTML
    assert "await advanceTicket(ticket.id, commentText.value);" in HTML
    print("default_advance_workflow_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
