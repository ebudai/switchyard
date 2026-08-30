#!/usr/bin/env python3
"""Regression test: blocked state transitions surface backend errors in the UI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "assigneeSelect.addEventListener('change', async () => {" in HTML
    assert "setCreateStatus(error.message, true);" in HTML
    assert "await requestBoardReload();" in HTML
    print("state_transition_error_feedback_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
