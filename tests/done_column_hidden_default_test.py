#!/usr/bin/env python3
"""Regression test: the board hides the Done column until explicitly requested."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "Done is hidden by default." in HTML
    assert "id=\"showDoneInput\"" in HTML
    assert "id=\"showDoneCount\"" in HTML
    assert "showDone: false," in HTML
    assert "function visibleColumns()" in HTML
    assert "return state.showDone ? COLUMNS : COLUMNS.filter((column) => column.key !== 'done');" in HTML
    assert "function renderDoneToggle()" in HTML
    assert "visibleColumns().forEach((column) => {" in HTML
    assert "showDoneInput.addEventListener('change', () => {" in HTML
    print("done_column_hidden_default_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
