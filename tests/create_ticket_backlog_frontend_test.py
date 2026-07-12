#!/usr/bin/env python3
"""Regression test: create form can choose Analysis or Backlog only."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_markup import MARKUP
from scripts.ticket_board.frontend_script_app import SCRIPT_APP


def main() -> int:
    assert 'id="createStateInput"' in MARKUP
    assert '<option value="analysis" selected>Analysis</option>' in MARKUP
    assert '<option value="backlog">Backlog</option>' in MARKUP
    assert "state: createStateInput.value" in SCRIPT_APP
    assert "createStateInput.value = 'analysis';" in SCRIPT_APP
    assert "createStateInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    print("create_ticket_backlog_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
