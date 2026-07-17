#!/usr/bin/env python3
"""Regression test: detail view exposes editable ticket titles."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "Title" in HTML
    assert "Save Title" in HTML
    assert "bindDetailDraftField(draftFields, titleEditInput, 'title', titleEditInput.value);" in HTML
    assert "await updateDetailTicket({ title: titleEditInput.value });" in HTML
    assert "await updateTicketAction(ticketId, 'edit_fields', editable, normalizedCaller);" in HTML
    print("title_edit_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
