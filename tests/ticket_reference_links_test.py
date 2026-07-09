#!/usr/bin/env python3
"""Regression test: PGU-N references render as clickable ticket links."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert r"const TICKET_REF_PATTERN = /\b(PGU-\d+)\b/ig;" in HTML
    assert "function buildTicketReference(ticketId)" in HTML
    assert "openDetail(normalizedId);" in HTML
    assert "function appendLinkedTicketText(container, text)" in HTML
    assert "function linkedPreview(label, text, emptyText = '(none)')" in HTML
    assert "function linkedTicketRow(ticketIds)" in HTML
    assert "body.appendChild(linkedTextBlock(ticket.body, '(no body)'));" in HTML
    assert "linkedPreview('Rendered Preview', ticket.implementation, '(no implementation yet)')" in HTML
    assert "linkedPreview('Rendered Preview', ticket.audit_prompt, '(no audit prompt yet)')" in HTML
    assert "blockedByLinksLabel.textContent = 'Linked Tickets';" in HTML
    assert "appendLinkedTicketText(text, comment.text);" in HTML
    assert "reference.disabled = true;" in HTML
    print("ticket_reference_links_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
