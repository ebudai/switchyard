"""What a test's stand-in tmux keys its panes and sessions by, from an exact target (SYRD-832).

Switchyard now names every session target exactly (`=session:`, `=session:window.pane`; see
scripts/ticket_board/tmux_target.py), so tmux never answers for another session by prefix. A stand-in that keys its
fake panes by plain name (`pgu-app:0.0`, `otto-director`) looks the exact form up through `plain_tmux_target`. That a
real tmux treats the exact form as exact is proved against a real server in tests/tmux_exact_session_target_test.py,
and tests/tmux_exact_target_lint_test.py holds every target in scripts/ to it.
"""

from __future__ import annotations


def plain_tmux_target(target: str) -> str:
    """`=pgu-app:0.0` -> `pgu-app:0.0`; `=otto-director:` -> `otto-director`; anything else unchanged."""
    text = str(target or "")
    if not text.startswith("="):
        return text
    text = text[1:]
    return text[:-1] if text.endswith(":") and text.count(":") == 1 else text
