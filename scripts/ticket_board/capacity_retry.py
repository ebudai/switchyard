"""A Codex turn that ended on "Selected model is at capacity": retried with backoff, then the Director told (SYRD-594).

Codex 0.163.0 ends a turn whose model request found the model at capacity --
`server_is_overloaded` in the response stream, or an HTTP 503 it has already
retried itself -- by drawing `CODEX_CAPACITY_ERROR` at the margin and returning
to its empty prompt. It runs no Stop hook for that turn: the hook state stays
the turn's own UserPromptSubmit until the next turn starts (measured,
tests/fixtures/syrd594_codex_capacity_screens.json). So the board read the pane
as working. 120 s later the generic stale-Codex rule rewrote it to idle, as it
does after any Codex error, and the owner was told "this turn ended without
resolving it" -- the wrong cause, again every two minutes while the provider
stayed full -- or, where a board condition holds that prompt back, nothing.

* **Recognised** (`turn_end_trace`): a Codex pane whose hook says a turn
  started and has not ended, whose last line above its prompt is exactly that
  error, which is still, has no provider question open and an empty composer.
  The gate reads it as the end of the turn: deliverable, and left as it is
  rather than rewritten as idle, so the generic reminders stay out of it.
  Output that quotes the error is not it: that turn ends with Codex's own Stop
  hook, and Codex draws a reply's lines after its bullet, never at the margin.
* **Retried** (`run`): every pass reports each Codex role's pane to the board
  (`ticket_board.note_provider_capacity`), which keeps one episode per role in
  the database, so a restarted listener neither resets nor repeats an attempt.
  While the role's current ticket is unchanged and its pane still shows the
  error, the board queues one retry after each of `delays_seconds()` (1, 2 and
  then 5 minutes), timed from the failure it answers, and only when nothing else
  for the role is waiting to go. The retry goes out through the ordinary path
  and its gates and starts a new turn, as the User's nudge did.
* **Escalated**: when the last retry has failed too, the Director is told once,
  with the role, the ticket, the condition and each failure's time.

The episode closes when a turn ends normally, or when the ticket moves or
changes hands; what of it is not sent yet is withdrawn, and a queued retry is
withdrawn as soon as the pane is working again. Only this error, as measured:
not a quota error, a rate limit, or any other provider output.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

#: Codex 0.163.0's whole line for a turn whose request found the model at capacity.
CODEX_CAPACITY_ERROR = "■ Selected model is at capacity. Please try a different model."
#: The prompt Codex's cursor sits after when it waits for input.
CODEX_PROMPT = "›"
TRACE_REASON = "codex_capacity_turn_end"
#: The wait before each retry; there are as many retries as waits (the ticket's 1, 2 and 5 minutes).
DEFAULT_DELAYS_SECONDS = (60.0, 120.0, 300.0)
DELAYS_ENV = "TICKET_BOARD_CAPACITY_RETRY_DELAYS"


def delays_seconds(environ: dict[str, str] | None = None) -> tuple[float, ...]:
    raw = (os.environ if environ is None else environ).get(DELAYS_ENV, "")
    try:
        delays = tuple(float(part) for part in raw.split(",")) if raw.strip() else DEFAULT_DELAYS_SECONDS
    except ValueError:
        return DEFAULT_DELAYS_SECONDS
    return delays if delays and all(delay >= 0 for delay in delays) else DEFAULT_DELAYS_SECONDS


def error_ends_screen(lines: list[str], cursor_y: int) -> bool:
    """Whether the cursor is on Codex's prompt and the last line with text above it is the capacity error."""
    if not 0 <= cursor_y < len(lines) or not lines[cursor_y].startswith(CODEX_PROMPT):
        return False
    above = next((line for line in reversed(lines[:cursor_y]) if line.strip()), "")
    return above.rstrip() == CODEX_CAPACITY_ERROR


def _shows_error(gate: Any, target: str) -> bool:
    try:
        cursor = gate.cursor_position_runner(
            ["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y} #{pane_height}"],
            check=True, text=True, capture_output=True, timeout=2.0)
        capture = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-t", target],
                                           check=True, text=True, capture_output=True, timeout=2.0)
        cursor_y = int(cursor.stdout.split()[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return False
    return error_ends_screen(capture.stdout.splitlines(), cursor_y)


def turn_end_trace(gate: Any, target: str, state: Any) -> Any:
    """The gate's reading of a Codex pane whose turn ended on the capacity error; None for any other pane.

    Asked only of a Codex pane's own hook state, whose one busy state is a turn's start (UserPromptSubmit).
    """
    from . import notify_listener as listener

    if state.state != "busy" or not _shows_error(gate, target):
        return None
    moving = gate._working_timer_trace(target, sample_delay_seconds=gate.idle_working_timer_sample_delay_seconds)
    if moving is not None:
        return moving
    composing = gate._target_cursor_state(target)
    if (prompt := gate._open_prompt_trace(target)) is not None:
        return prompt
    if composing is None:
        return listener.ActivityTrace(True, "cursor_state_unavailable")
    if composing:
        return listener.ActivityTrace(True, "human_composing")
    return listener.ActivityTrace(False, TRACE_REASON)


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def report(gate: Any) -> dict[str, dict]:
    """Each Codex role's pane, for the board: the turn that ended on the error, the turn that ended normally, or working."""
    from . import notify_listener as listener

    seen: dict[str, dict] = {}
    for role, target in sorted(gate.role_targets.items()):
        try:
            state = gate.state_store.read(target)
        except OSError:
            continue
        if state is None or not state.source.startswith("codex."):
            continue  # not a Codex pane's own word, or nothing to read: cannot tell
        turn = state.turn_started_at if state.turn_started_at is not None else state.updated_at
        trace = turn_end_trace(gate, target, state)
        stalled = trace is not None and not trace.busy
        seen[role] = {
            "capacity_turn": _iso(turn) if stalled else None,
            "ended_turn": _iso(turn) if state.state == "idle" and state.source in listener.IDLE_TURN_END_SOURCES else None,
            "working": state.state != "idle" and not stalled,
        }
    return seen


def _present(listener: Any, conn: Any) -> bool:
    """Whether this board has the episodes yet: a listener can be newer than its board's migrations."""
    present = getattr(listener, "_provider_capacity_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_provider_capacity(jsonb, double precision[], "
                           "timestamptz)') IS NOT NULL AS present").fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        listener._provider_capacity_present = present
    return present


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many notices the board queued. Never stops the listener."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store"):
        return 0
    try:
        roles = report(gate)
        if not roles or not _present(listener, conn):
            return 0
        row = conn.execute("SELECT ticket_board.note_provider_capacity(%s::jsonb, %s::double precision[])",
                           (json.dumps(roles, sort_keys=True), list(delays_seconds()))).fetchone()
        queued = int((next(iter(row.values())) if isinstance(row, dict) else row[0]) or 0) if row else 0
    except Exception as exc:  # logged; the notices this pass delivers matter more
        listener.logger.warning("Provider-capacity retry pass failed: %s", exc)
        return 0
    if queued:
        listener.logger.info("Queued %s provider-capacity retry or escalation notice(s): %s", queued, roles)
    return queued
