#!/usr/bin/env python3
"""Regression test: the board has a narrow-screen layout that does not force page overflow."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "const mobileSectionMedia = window.matchMedia('(max-width: 900px)');" in HTML
    assert "boardEl.style.gridTemplateColumns = '1fr';" in HTML
    assert "boardEl.style.minWidth = '0';" in HTML
    assert "@media (max-width: 640px) {" in HTML
    assert "grid-template-columns: minmax(0, 1fr);" in HTML
    assert "max-width: 100vw;" in HTML
    assert ".board-scroll {\n        overflow-x: hidden;\n      }" in HTML
    assert ".attach-set-grid {\n        grid-template-columns: 1fr;\n      }" in HTML
    assert "overflow-wrap: anywhere;" in HTML
    print("mobile_board_width_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
