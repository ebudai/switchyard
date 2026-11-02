"""The Director's Codex turn that ended on "Selected model is at capacity": retried, then the operator told (SYRD-728).

SYRD-594 retries a ticket owner's such turn through the notification queue and leaves the Director out: an alert
about the Director's pane would wait in that same pane, and a Director turn may be about no ticket. Here:

* **Recognised** as SYRD-594 recognises it (`capacity_retry.turn_end_trace`): a Codex pane whose turn started and
  has not ended, the exact error at the margin above its empty prompt, still, with no provider question. Quota,
  authentication, refusals, a model that is not there, and a reply quoting the error are not it. The error carries
  no retry delay, so the board's own delays (1, 2, then 5 minutes, `capacity_retry.delays_seconds`) are the only
  ones.
* **Retried** (`run`): each pass reports the Director's pane to the board (`note_director_capacity`), which keeps one
  episode in the database and claims an attempt there before this types it: a restarted listener neither resets nor
  repeats one. The pane is read again just before typing; if it changed, nothing is typed. The turn the retry
  starts is recorded, as its hook names it, and only that exact turn's failure is the board's. The prompt keeps the
  conversation (nothing is cleared) and asks the Director to check what the interrupted turn already did before
  repeating anything.
* **Stopped** when a turn ends normally, a person types or starts a turn of their own, or the Director's runtime or
  pane process changes; and **escalated** when the attempts are spent: one user_events row for the phone bridge and
  the board page's banner (`status`), never a notice to the same pane.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from . import capacity_retry

ROLE = "director"
REPORT_FUNCTION = "ticket_board.note_director_capacity(jsonb, double precision[], timestamptz)"


def pane_process(gate: Any, target: str) -> dict | None:
    """{pid, start_time} of the pane's process, as tmux and /proc name it; None when either cannot be read."""
    pid = gate._pane_pid(target)
    if pid is None:
        return None
    try:
        started = next((row[4] for row in gate.process_table_reader() if row[0] == pid and len(row) > 4), 0.0)
    except OSError:
        return None
    return {"pid": pid, "start_time": round(float(started), 1)} if started else None


def reading(gate: Any) -> tuple[str, Any, dict] | None:
    """(target, hook state, report) for the Director's pane when it runs Codex; None otherwise."""
    from . import notify_listener as listener

    target = getattr(gate, "role_targets", {}).get(ROLE)
    if not target:
        return None
    try:
        state = gate.state_store.read(target)
    except OSError:
        return None
    if state is None or not state.source.startswith("codex."):
        return None
    turn = state.turn_started_at if state.turn_started_at is not None else state.updated_at
    trace = capacity_retry.turn_end_trace(gate, target, state)
    stalled = trace is not None and not trace.busy
    return target, state, {
        "capacity_turn": capacity_retry._iso(turn) if stalled else None,
        "ended_turn": capacity_retry._iso(turn) if state.state == "idle"
        and state.source in listener.IDLE_TURN_END_SOURCES else None,
        "human": trace is not None and trace.reason == "human_composing",
        "pane": pane_process(gate, target),
    }


def started_turn(gate: Any, target: str, since: float) -> str | None:
    """The turn a send at `since` started, as the recipient's own hook recorded its start; None when none has.

    Recorded beside the send, so a failed turn is the board's only when it is exactly that turn: a person's turn
    begun a moment later is a different turn, whatever the clock says (SYRD-728)."""
    store = getattr(gate, "state_store", None)  # a gate that reads no hook state names no turn
    try:
        state = store.read(target) if store is not None else None
    except OSError:
        return None
    turn = getattr(state, "turn_started_at", None)
    return capacity_retry._iso(turn) if turn is not None and turn >= since else None


def _still_failed(gate: Any, target: str, state: Any) -> bool:
    """Read again just before typing: the same failed turn, still at its empty prompt."""
    try:
        now = gate.state_store.read(target)
    except OSError:
        return False
    if now is None or (now.state, now.turn_started_at) != (state.state, state.turn_started_at):
        return False
    trace = capacity_retry.turn_end_trace(gate, target, now)
    return trace is not None and not trace.busy and trace.reason == capacity_retry.TRACE_REASON


def run(listener: Any, conn: Any, gate: Any) -> int:
    """One pass for the Director's pane; returns 1 when it typed a retry or raised the alert. Never raises."""
    from .notification_dispatch import display_message

    try:
        seen = reading(gate)
        if seen is None:
            return 0
        target, state, report = seen
        present = getattr(listener, "_director_capacity_present", None)
        if present is None:
            row = conn.execute(f"SELECT to_regprocedure('{REPORT_FUNCTION}') IS NOT NULL AS present").fetchone()
            present = listener._director_capacity_present = bool(row["present"] if isinstance(row, dict) else row[0])
        if not present:
            return 0
        row = conn.execute("SELECT ticket_board.note_director_capacity(%s::jsonb, %s::double precision[]) AS action",
                           (json.dumps(report, sort_keys=True), list(capacity_retry.delays_seconds()))).fetchone()
        action = row["action"] if isinstance(row, dict) else row[0]
        action = json.loads(action) if isinstance(action, str) else dict(action or {})
        if "escalated" in action:
            listener.logger.warning("Director capacity retries exhausted (episode %s): %s", action["escalated"],
                                    action.get("message", ""))
            return 1
        if "retry" not in action:
            return 0
        outcome, turn = "not_sent", None
        if _still_failed(gate, target, state):
            since = time.time()
            try:
                listener.sender(target, display_message(action["message"]))
                outcome = "sent"
            except (OSError, subprocess.SubprocessError) as exc:
                listener.logger.warning("Director capacity retry %s was not delivered: %s", action["retry"], exc)
                outcome = "send_failed"
            # The turn this retry started, by the pane's own hook, waited for as a delivery's receipt is. Without it,
            # no later failed turn is taken for the board's: the episode stops rather than retry over a person.
            deadline = time.monotonic() + float(getattr(listener, "submission_confirm_seconds", 5.0))
            while outcome == "sent" and (turn := started_turn(gate, target, since)) is None and time.monotonic() < deadline:
                time.sleep(0.05)
        conn.execute("SELECT ticket_board.record_director_capacity_attempt(%s, %s, %s, %s::timestamptz)",
                     (action["episode"], action["attempt"], outcome, turn))
        listener.logger.info("Director capacity retry %s of %s: %s", action["retry"], action["of"], outcome)
        return 1 if outcome == "sent" else 0
    except Exception as exc:  # logged; the notices this pass delivers matter more
        listener.logger.warning("Director capacity pass failed: %s", exc)
        return 0


def status(app: Any) -> dict[str, Any]:
    """GET /api/director-capacity: the standing alert and the open episode; empty on a board older than them, or one
    with no database (the in-memory app) to keep them in."""
    if not hasattr(app, "_pg_connect"):
        return {"alert": None, "open": None}
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.director_capacity_status()') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"alert": None, "open": None}
        row = conn.execute("SELECT ticket_board.director_capacity_status() AS status").fetchone()
    result = row["status"] if isinstance(row, dict) else row[0]
    return json.loads(result) if isinstance(result, str) else dict(result)
