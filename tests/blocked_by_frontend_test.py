#!/usr/bin/env python3
"""Regression test: blocked_by is editable in the board frontend."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "function parseBlockedByInput(value)" not in HTML
    assert "function unresolvedBlockedBy(ticket)" in HTML
    assert "function availableBlockerTickets(ticket)" in HTML
    assert "function compareTicketsNewestNumberFirst(left, right)" in HTML
    assert ".sort(compareTicketsNewestNumberFirst);" in HTML
    assert "Blocked By" in HTML
    assert "blockedByInput" not in HTML
    assert "Choose a non-terminal ticket…" in HTML
    assert "Only non-terminal tickets are selectable as blockers." in HTML
    assert "!['done', 'cancelled'].includes(candidate.state)" in HTML
    assert "addBlockedByButton.textContent = 'Add Blocker';" in HTML
    assert "Add Selected" not in HTML
    assert "Save Blockers" not in HTML
    assert "const nextBlockedBy = Array.from(new Set([...(ticket.blocked_by || []), blockerId]));" in HTML
    assert "const blockedReasonValue = blockedReasonInput.value.trim() || `Waiting on ${blockerId}.`;" in HTML
    assert "persistDetailDraftField(" not in HTML
    assert "await updateTicket(ticket.id, {" in HTML
    assert "blocked_by: nextBlockedBy," in HTML
    assert "blocked_reason: blockedReasonValue," in HTML
    assert "appendLinkedTicketText(body, text);" in HTML
    assert "appendLinkedTicketText(body, item.text);" in HTML
    assert "blocked_by" in HTML
    print("blocked_by_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
