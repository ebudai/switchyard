#!/usr/bin/env python3
"""Regression test: mobile layout does not reserve full-height space for the collapsed create section."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "@supports (height: 100dvh)" in HTML
    assert "@media (max-width: 980px) {" in HTML
    assert "aside {\n          min-height: 0;\n        }" in HTML
    print("mobile_create_section_gap_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
