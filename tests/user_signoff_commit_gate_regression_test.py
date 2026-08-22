#!/usr/bin/env python3
"""Regression test: UAT sign-off records approval without forcing a done transition."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "async function submitUserSignoff(ticketId, who, text)" in HTML
    assert "const patch = { user_signoff: true };" in HTML
    assert "const patch = { user_signoff: true, state: 'done' };" not in HTML
    assert "setCreateStatus(`Signed off ✓ ${ticketId} moved to Final Sign-Off.`);" in HTML
    assert "return 'UAT signed off. In Final Sign-Off.'" in HTML
    assert "setCreateStatus(error.message, true);" in HTML
    print("user_signoff_commit_gate_regression_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
