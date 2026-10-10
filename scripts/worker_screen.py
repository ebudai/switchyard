"""Whether a freshly started worker can take work, or is stopped at its provider's own question (SYRD-566).

"Started" has meant a live session running the role's CLI (SYRD-477, SYRD-560).
On otto that was true of a Codex worker that could take nothing: it was
stopped at a model-retirement notice, waiting for somebody to answer it, and
the start was reported as a success. A worker in that state is blocked, and
saying so -- with the question and what answers it -- is the report agreeing
with the machine.

The screen is the only witness before the first hook (Codex fires none until
its first prompt), and it has to be watched for a moment: Codex draws its
ordinary prompt first and its retirement notice over it seconds later. So a start is
watched until the screen shows a question, stands still without one for
`SETTLE_SECONDS`, or `WATCH_SECONDS` pass; a blank screen has not drawn yet and
never counts as settled. A capture that cannot be made ends the watch with no
answer rather than a guess. Nothing here
answers the question: that is the person's (see
scripts/ticket_board/provider_prompt.py).
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from scripts.ticket_board import provider_prompt

#: Longest a fresh worker's screen is watched.
WATCH_SECONDS = 20.0
#: How long a screen without a question must stand still to count as settled.
#: Codex 0.162 on a retiring model draws its ordinary prompt at 0.5 s and
#: replaces it with the retirement notice at 4.0 s (measured three times,
#: offline): twice that gap.
SETTLE_SECONDS = 8.0
POLL_SECONDS = 0.5


def capture(target: str, runner: Callable[..., Any]) -> list[str] | None:
    """The pane's visible rows, or None when they cannot be read.

    A real pane always captures its rows, blank or not; nothing at all is not a
    screen (the rule delivery_proof.read_composer reads a composer by).
    """
    try:
        proc = runner(["tmux", "capture-pane", "-p", "-t", target], text=True,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = getattr(proc, "stdout", None)
    if getattr(proc, "returncode", 1) != 0 or not isinstance(stdout, str) or not stdout:
        return None
    return stdout.splitlines()


def open_prompt(target: str, runner: Callable[..., Any]) -> provider_prompt.ProviderPrompt | None:
    """The question the pane shows now, if any: one look, for status."""
    lines = capture(target, runner)
    return None if lines is None else provider_prompt.open_prompt(lines)


def startup_prompt(
    target: str,
    runner: Callable[..., Any],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> provider_prompt.ProviderPrompt | None:
    """The question a freshly started worker stopped at, or None once its screen settles without one."""
    deadline = clock() + WATCH_SECONDS
    last: list[str] | None = None
    stable_since = clock()
    while True:
        lines = capture(target, runner)
        if lines is None:
            return None
        prompt = provider_prompt.open_prompt(lines)
        if prompt is not None:
            return prompt
        now = clock()
        if lines != last or not any(row.strip() for row in lines):
            last, stable_since = lines, now  # a blank screen is a CLI that has not drawn yet, never settled
        elif now - stable_since >= SETTLE_SECONDS:
            return None
        if now >= deadline:
            return None
        sleep(POLL_SECONDS)


def blocked_detail(cli: str, prompt: provider_prompt.ProviderPrompt) -> str:
    """One sentence an operator can act on: what the worker is stopped at, and what answers it."""
    options = " / ".join(f'"{option.split("  ")[0]}"' for option in prompt.options)
    return (f"{cli} is stopped at {prompt.description} ({options}), so it cannot take work and nothing answers "
            f"it by itself. To unblock it, {prompt.remedy}")


def live_question(role: Any, runner: Callable[..., Any], *, watch: bool = False) -> str:
    """What a live worker's pane is stopped at, said with what answers it; "" when it is not.

    One look for status; `watch` for a worker just started, until its screen
    settles (`startup_prompt`).
    """
    prompt = startup_prompt(role.target, runner) if watch else open_prompt(role.target, runner)
    return "" if prompt is None else blocked_detail(_cli(role), prompt)


def _cli(role: Any) -> str:
    from scripts import team_launcher

    return team_launcher._role_cli_name(role)
