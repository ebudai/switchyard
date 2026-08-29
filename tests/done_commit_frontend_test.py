#!/usr/bin/env python3
"""Regression test: board detail view exposes commit tracking controls."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "Commit Hash" in HTML
    assert "Save Commit" not in HTML
    assert "No commit required" in HTML
    assert "await updateDetailTicket({ commit_hash: commitHashInput.value });" not in HTML
    assert "{ commit_hash: patch.commit_hash || ticket?.commit_hash || '' }" in HTML
    assert "commit_exempt: checked" in HTML
    assert "A verified git commit is required before moving this ticket to done." in HTML
    print("done_commit_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
