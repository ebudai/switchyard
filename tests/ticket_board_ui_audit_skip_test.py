#!/usr/bin/env python3
"""Regression test: missing Playwright reports an explicit UI-audit skip."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import ticket_board_ui_audit_test


def test_missing_playwright_is_an_explicit_skip() -> None:
    original_load_playwright = ticket_board_ui_audit_test.load_playwright
    output = io.StringIO()
    try:
        ticket_board_ui_audit_test.load_playwright = lambda: (_ for _ in ()).throw(ModuleNotFoundError("playwright"))
        with contextlib.redirect_stdout(output):
            assert ticket_board_ui_audit_test.main() == 0
    finally:
        ticket_board_ui_audit_test.load_playwright = original_load_playwright

    rendered = output.getvalue()
    assert "ticket_board_ui_audit_test: skipped" in rendered
    assert "missing Playwright Python package" in rendered


def main() -> int:
    test_missing_playwright_is_an_explicit_skip()
    print("ticket_board_ui_audit_skip_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
