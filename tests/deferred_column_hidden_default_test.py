#!/usr/bin/env python3
"""Regression test: deferred/backlog tickets are hidden until explicitly requested."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.frontend import HTML


def main() -> int:
    assert "{ key: 'backlog', label: 'Backlog' }," in HTML
    assert 'id="showDeferredInput"' in HTML
    assert 'id="showDeferredCount"' in HTML
    assert "Show Deferred" in HTML
    assert "showDeferred: false," in HTML
    assert "const showDeferredInput = document.getElementById('showDeferredInput');" in HTML
    assert "const showDeferredCountEl = document.getElementById('showDeferredCount');" in HTML
    assert "if (column.key === 'backlog' && !state.showDeferred) {" in HTML
    assert "const deferredCount = columnTicketCount('backlog');" in HTML
    assert "showDeferredInput.checked = state.showDeferred;" in HTML
    assert "showDeferredCountEl.textContent = `(${deferredCount})`;" in HTML
    assert "showDeferredInput.addEventListener('change', () => {" in HTML
    print("deferred_column_hidden_default_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
