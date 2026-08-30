#!/usr/bin/env python3
"""Regression test: ticket detail renders in a closable full-screen overlay."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "id=\"detailOverlay\"" in HTML
    assert "class=\"detail-overlay\"" in HTML
    assert "id=\"detailCloseBtn\"" in HTML
    assert "function openDetail(ticketId)" in HTML
    assert "function closeDetail()" in HTML
    assert "detailOverlayEl.hidden = !isOpen;" in HTML
    assert "detailCloseBtn.addEventListener('click', () => {" in HTML
    assert "detailOverlayEl.addEventListener('click', (event) => {" in HTML
    assert "if (event.key !== 'Escape') {" in HTML
    assert "if (state.detailOpen) {" in HTML
    assert "body.detail-open" in HTML
    print("detail_modal_overlay_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
