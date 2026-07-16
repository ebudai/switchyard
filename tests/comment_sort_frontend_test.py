#!/usr/bin/env python3
"""Regression test: ticket detail comments render newest-first."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "const sortedComments = ticket.comments" in HTML
    assert ".map((comment, index) => ({ comment, index }))" in HTML
    assert "const leftTime = Date.parse(left.comment.ts || '');" in HTML
    assert "const rightTime = Date.parse(right.comment.ts || '');" in HTML
    assert "return rightTime - leftTime;" in HTML
    assert "return leftValid ? -1 : 1;" in HTML
    assert "return right.index - left.index;" in HTML
    assert "sortedComments.forEach(({ comment }) => {" in HTML
    assert "ticket.comments.forEach((comment) => {" not in HTML
    print("comment_sort_frontend_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
