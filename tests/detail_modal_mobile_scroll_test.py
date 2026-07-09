#!/usr/bin/env python3
"""Regression test: the mobile detail modal uses a scrollable dvh-safe layout."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert ".detail-overlay {" in HTML
    assert "overflow: auto;" in HTML
    assert ".detail-modal-body {" in HTML
    assert "min-height: 0;" in HTML
    assert "-webkit-overflow-scrolling: touch;" in HTML
    assert "padding-bottom: calc(16px + env(safe-area-inset-bottom, 0px));" in HTML
    assert "@supports (height: 100dvh)" in HTML
    assert "height: 100dvh;" in HTML
    assert "max-height: 100dvh;" in HTML
    print("detail_modal_mobile_scroll_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
