#!/usr/bin/env python3
"""Regression test: frontend exposes multi-attachment add/remove gallery controls."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "pendingCreateScreenshots" in HTML
    assert "Add Attachment" in HTML
    assert ".attachment-gallery" in HTML
    assert ".attachment-remove" in HTML
    assert "ticketScreenshotEntries(ticket).length" in HTML
    assert "screenshots: uniquePaths([...ticketScreenshotPaths(ticket), screenshotSelect.value])" in HTML
    assert "screenshots: ticketScreenshotPaths(ticket).filter((item) => item !== path)" in HTML
    assert "state.pendingCreateScreenshots = uniquePaths([...state.pendingCreateScreenshots, uploaded.path]);" in HTML
    print("multi_attachment_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
