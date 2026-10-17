"""A role that keeps failing to resolve its ticket: prompted a bounded number of times, then the Director told once (SYRD-595).

Measured on main with a provider that fails every turn (SYRD Audit's agy, out of
quota, 2026-10-16): the owner was prompted "this turn ended without resolving
it" once per grace period with no end, and the Director was told "may be stuck"
and "the grace period has passed" in between -- both recipients about the same
stall, at the same time. A provider whose hook stayed busy after the failure got
nothing at all, ever.

Each pass reports every role's pane to the board (`ticket_board.note_role_stall`),
which keeps one episode per ticket round in the database, so a restarted listener
or a replaced runtime cannot fan one episode out to both recipients:

* **Phase `role`.** The owner's repair prompts are the attempts, and the
  Director hears nothing about the stall.
* **Escalated.** After `max_attempts()` prompts reached the pane and the turn
  after the last one ended unresolved too (or never came within the grace), or
  once the pane has said busy since it took the work with nothing moving on
  screen past the grace: one message to the Director -- role, ticket, runtime
  generation, what the pane shows, and the attempts -- and no automatic prompt
  to the owner after it.
* **Handed back.** A Director comment on the ticket starts a new `role` phase.
* **Closed** when the ticket moves or changes hands, or its owner writes on
  it; or, opened on a prompt that then never went, as withdrawn. A prompt left
  queued from an earlier round of the ticket counts for nothing (SYRD-527).
  While it is resolved for now (a snooze, a lease, its own notice on its
  way) nothing is decided about it.

Delivery asks the board too (`notice_is_current`), so what the phase does not
allow is never typed: never the owner and the Director about one stall at once.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_ENV = "TICKET_BOARD_STALL_MAX_ATTEMPTS"
#: Payload kinds an open episode decides about (SYRD-595), beside its own escalation.
NOTICE_KINDS = frozenset({"stall_escalation", "unresolved_turn", "nudge", "escalation", "idle_reminder",
                          "unresolved_turn_repair", "capacity_retry"})
#: How much of the pane's last text the Director is shown.
CONDITION_CHARS = 300


def max_attempts(environ: dict[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get(MAX_ATTEMPTS_ENV, "")
    try:
        value = int(raw) if raw.strip() else DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAX_ATTEMPTS


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def condition(gate: Any, target: str) -> str | None:
    """The last line of text the pane shows above its cursor: what the provider last said."""
    try:
        cursor = gate.cursor_position_runner(
            ["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y} #{pane_height}"],
            check=True, text=True, capture_output=True, timeout=2.0)
        capture = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-t", target],
                                           check=True, text=True, capture_output=True, timeout=2.0)
        cursor_y = int(cursor.stdout.split()[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    lines = capture.stdout.splitlines()[:max(cursor_y, 0)]
    # A rule or prompt frame is not something the provider said.
    said = [line.strip() for line in lines if line.strip() and set(line.strip()) - set("─━-=_│|╭╮╰╯ ")]
    return said[-1][:CONDITION_CHARS] if said else None


def busy_still(gate: Any, target: str, state: Any, grace_seconds: float) -> bool:
    """The hook has said busy for longer than the grace, nothing on the screen moves, and no question is open."""
    if state.state != "busy" or state.turn_started_at is None or gate.wall_time() - state.updated_at < grace_seconds:
        return False
    if gate._working_timer_trace(target, sample_delay_seconds=gate.idle_working_timer_sample_delay_seconds) is not None:
        return False
    return gate._open_prompt_trace(target) is None


def report(gate: Any, grace_seconds: float, watched: set[str]) -> dict[str, dict]:
    """Each role's own turn as its hook recorded it, and whether its pane is busy with nothing moving.

    The pane's last text is read only for a role whose episode is open (`watched`) or whose pane is busy
    with nothing moving: it is what the Director is shown when it is escalated.
    """
    seen: dict[str, dict] = {}
    for role, target in sorted(gate.role_targets.items()):
        if role == "director":
            continue
        try:
            state = gate.state_store.read(target)
        except OSError:
            continue
        if state is None:
            continue  # cannot tell
        still = busy_still(gate, target, state, grace_seconds)
        seen[role] = {
            "state": state.state,
            "updated_at": _iso(state.updated_at),
            "turn_started_at": _iso(state.turn_started_at),
            "busy_still": still,
            "condition": condition(gate, target) if still or role in watched else None,
        }
    return seen


def _present(holder: Any, conn: Any) -> bool:
    """Whether this board has the episodes yet: a listener can be newer than its board's migrations."""
    present = getattr(holder, "_role_stall_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_role_stall(jsonb, integer, interval, "
                           "timestamptz)') IS NOT NULL AS present").fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        holder._role_stall_present = present
    return present


def _first(row: Any) -> Any:
    return (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None


def _text(value: Any) -> str:
    """The listener's connection returns text as bytes."""
    return value.decode() if isinstance(value, (bytes, bytearray, memoryview)) else str(value)


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many escalations the board queued. Never stops the listener."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store"):
        return 0
    try:
        if not _present(listener, conn):
            return 0
        watched = {_text(_first(row)) for row in conn.execute(
            "SELECT owner_role FROM ticket_board.role_stall_episodes WHERE closed_at IS NULL").fetchall()}
        grace = float(listener.unresolved_turn_grace_seconds)
        roles = report(gate, grace, watched)
        if not roles:
            return 0
        queued = int(_first(conn.execute(
            "SELECT ticket_board.note_role_stall(%s::jsonb, %s, %s::interval)",
            # Raw text, not \u escapes: a board whose database is SQL_ASCII cannot translate those.
            (json.dumps(roles, sort_keys=True, ensure_ascii=False), max_attempts(), f"{grace} seconds")).fetchone()) or 0)
    except Exception as exc:  # logged; the notices this pass delivers matter more
        listener.logger.warning("Role stall pass failed: %s", exc)
        return 0
    if queued:
        listener.logger.info("Escalated %s stalled role ticket(s) to the Director", queued)
    return queued


def notice_is_current(holder: Any, conn: Any, ticket_id: str, target_role: str, payload: str) -> bool:
    """Whether an open stall episode allows this notice: one recipient at a time, and one escalation."""
    if not _present(holder, conn):
        return True
    row = conn.execute("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb) AS current",
                       (ticket_id, target_role, payload)).fetchone()
    return row is not None and bool(_first(row))
