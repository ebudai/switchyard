#!/usr/bin/env python3
"""Regression test: ticket detail header shows stage as a compact chip."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "metaLine1.className = 'detail-ticket-headline';" in HTML
    assert "const stageChip = document.createElement('span');" in HTML
    assert "stageChip.className = `detail-stage-chip detail-stage-${ticket.state}`;" in HTML
    assert "stageChip.textContent = stateLabel(ticket.state);" in HTML
    assert "metaLine1.append(strong, stageChip, titleText);" in HTML
    assert "metaLine2.textContent = `Created: ${formatWhen(ticket.created)} | Updated: ${formatWhen(ticket.updated)}`;" in HTML
    assert ".detail-stage-chip {" in HTML
    assert ".detail-stage-analysis {" in HTML
    assert ".detail-stage-in_progress," in HTML
    assert ".detail-stage-audit," in HTML
    assert ".detail-stage-done {" in HTML
    assert "State: ${stateLabel(ticket.state)} |" not in HTML
    print("detail_stage_chip_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
