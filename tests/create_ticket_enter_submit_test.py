#!/usr/bin/env python3
"""Regression test: create-form Enter submit excludes multiline body text."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend_script_app import SCRIPT_APP


def main() -> int:
    assert "async function handleCreateSubmit()" in SCRIPT_APP
    assert "function submitCreateOnEnter(event)" in SCRIPT_APP
    assert "if (event.key !== 'Enter'" in SCRIPT_APP
    assert "event.preventDefault();" in SCRIPT_APP
    assert "void handleCreateSubmit();" in SCRIPT_APP
    assert "titleInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    assert "bodyInput.addEventListener('keydown', submitCreateOnEnter);" not in SCRIPT_APP
    assert "assigneeInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    assert "screenshotInput.addEventListener('keydown', submitCreateOnEnter);" in SCRIPT_APP
    print("create_ticket_enter_submit_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
