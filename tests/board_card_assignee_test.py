#!/usr/bin/env python3
"""Regression test: board cards show compact role text without workflow controls."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".card-assignee" in HTML
    assert ".card-assignee-value" in HTML
    assert ".card-assignee-label" not in HTML
    assert "assigneeLabel.textContent" not in HTML
    assert "function roleLabel(role)" in HTML
    assert "assigneeValue.textContent = roleLabel(ticket.assignee);" in HTML
    assert "buildOption(assigneeInput, assignee, roleLabel(assignee))" in HTML
    assert "buildOption(assigneeSelect, assignee, roleLabel(assignee))" in HTML
    assert "titleWrap.append(idEl, titleEl, assigneeLine);" in HTML
    assert "stateTag.textContent = stateLabel(ticket.state);" not in HTML
    assert "card.append(top, tags, badges);" not in HTML
    assert "advanceButton.textContent = `Advance -> ${stateLabel(nextState)}`;" not in HTML
    assert "stateSelect.addEventListener('change'" not in HTML
    print("board_card_assignee_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
