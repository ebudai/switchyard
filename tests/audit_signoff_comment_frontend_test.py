#!/usr/bin/env python3
"""Regression test: audit sign-off sends the audit write-up in-call."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "await updateTicketAction(ticketId, 'audit_sign_off', { text: actionReason(patch) }, normalizedCaller);" in HTML
    assert "await updateTicketAction(ticketId, 'audit_sign_off', {}, normalizedCaller);" not in HTML
    assert "audit_sign_off requires a non-empty comment" in (ROOT / "scripts" / "ticket_board" / "server.py").read_text(
        encoding="utf-8"
    )
    assert "await advanceTicket(ticket.id, commentText.value);" in HTML
    print("audit_signoff_comment_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
