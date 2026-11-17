"""tmux targets that name exactly one session (SYRD-832).

tmux resolves a target session by its exact name first and, failing that, by prefix. On mefp, 2026-11-17, the roles
`audit` and `audit-2` had the sessions `mefp-audit` and `mefp-audit-2`. audit-2's started first, and every question
about `mefp-audit` -- has-session, list-panes, display-message -- was answered by `mefp-audit-2`. audit was never
started, the launcher waited for a registration that could not come, and restart-role read audit-2's pane pid as
audit's. A send or a kill aimed at an absent session lands on its prefix-sharing neighbour the same way.

`exact_tmux_target` puts tmux's exact-match `=` before a target's session name: `=mefp-audit:0.0`, and for a bare
session name `=mefp-audit:` -- with the colon, because a command whose -t is a pane or a window (capture-pane,
send-keys, set-option) refuses a bare `=mefp-audit` ("can't find pane"), while `=name:` serves every kind (measured,
tmux 3.8: has-session, kill-session, list-clients, list-panes -s, set-environment, capture-pane, send-keys, set-option,
new-session -t). A pane, window or session id (`%12`, `@3`, `$1`), a special token (`{last}`, `!`, `-`), an empty
target and one that is already exact are returned unchanged. A pane of a session's current window is written
`session:.pane`, never `session.pane`: tmux allows a dot in a session name, so `=session.pane:` names no session.
`tests/tmux_exact_target_lint_test.py` holds every session target Switchyard builds in `scripts/` to it.
"""

from __future__ import annotations

UNCHANGED_PREFIXES = ("=", "%", "@", "$", "{", "!", "-", ":", ".")


def exact_tmux_target(target: str) -> str:
    """`target` with its session name matched exactly by tmux: never another session's prefix."""
    text = str(target or "")
    if not text or text.startswith(UNCHANGED_PREFIXES):
        return text
    return f"={text}" if ":" in text else f"={text}:"
