#!/usr/bin/env python3
"""Regression test: create form offers a backlog checkbox and maps it to create state."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_markup import MARKUP
from scripts.ticket_board.frontend_script_app import SCRIPT_APP
from scripts.ticket_board.frontend_script_core import SCRIPT_CORE


def main() -> int:
    assert 'id="createBacklogInput"' in MARKUP
    assert "Start in backlog" in MARKUP
    assert 'id="createDraftInput"' in MARKUP
    assert "Create as draft" in MARKUP
    assert 'id="createStateInput"' not in MARKUP
    assert "const createDraftInput = document.getElementById('createDraftInput');" in SCRIPT_CORE
    assert "const createBacklogInput = document.getElementById('createBacklogInput');" in SCRIPT_CORE
    assert "initial_state: createDraftInput.checked ? 'draft' : (createBacklogInput.checked ? 'backlog' : 'analysis')" in SCRIPT_APP
    assert "createDraftInput.checked = false;" in SCRIPT_APP
    assert "createBacklogInput.checked = false;" in SCRIPT_APP
    assert "createDraftInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    assert "createBacklogInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    print("create_ticket_backlog_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
