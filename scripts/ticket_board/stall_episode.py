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
  screen, and no progress for the board's stall threshold (SYRD-675): one
  message to the Director -- role, ticket, runtime generation, what the pane
  shows, and the attempts -- and no automatic prompt to the owner after it.
* **Handed back.** A Director comment on the ticket starts a new `role` phase.
* **Closed** when the ticket moves or changes hands, or its owner writes on
  it; or, opened on a prompt that then never went, as withdrawn. A prompt left
  queued from an earlier round of the ticket counts for nothing (SYRD-527).
  While it is resolved for now (a snooze, a lease, its own notice on its
  way) nothing is decided about it.

Delivery asks the board too (`notice_is_current`), so what the phase does not
allow is never typed: never the owner and the Director about one stall at once.
An escalation carries the progress clock it saw (`ticket_board.role_progress_at`);
delivery adds the owner's hook as it is now, and drops it once anything since
says the work moved -- progress, a bounded child, a replaced runtime, the hook --
naming why (`stale_reason`) in the drop's trace (SYRD-675).

The hook's word includes the turn's tool calls (SYRD-695): Claude Code and
Codex hooks write only a turn's start and end, so a long turn of short tool
calls -- none long enough to earn a lease -- looked like a pane saying busy
with nothing moving. A tool call its own provider's hook recorded during the
turn is when the hook last spoke (`tool_activity`).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from . import board_stall, tool_activity

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


def hook_word_at(gate: Any, state: Any, now: float | None = None) -> float:
    """When the hook last spoke about the pane: its own write, or a tool call of the turn in flight (SYRD-695)."""
    return tool_activity.hook_word_at(state, getattr(gate, "wall_time", time.time)() if now is None else now)


def _tool_activity(gate: Any, state: Any) -> dict:
    """The turn's latest tool call, for the record, when there is one: a pane with none reads as it always has."""
    activity = tool_activity.turn_tool_activity_at(state, getattr(gate, "wall_time", time.time)())
    return {} if activity is None else {"tool_activity_at": _iso(activity)}


def busy_still(gate: Any, target: str, state: Any, grace_seconds: float) -> bool:
    """The hook has said busy for longer than the grace, with no tool call since, nothing on the screen moves, and no
    question is open."""
    if state.state != "busy" or state.turn_started_at is None or gate.wall_time() - hook_word_at(gate, state) < grace_seconds:
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
            # The board's clock and its stale reasons read the hook's latest word, tool calls included (SYRD-695).
            "updated_at": _iso(hook_word_at(gate, state)),
            **_tool_activity(gate, state),
            "turn_started_at": _iso(state.turn_started_at),
            "busy_still": still,
            "condition": condition(gate, target) if still or role in watched else None,
        }
    return seen


def owner_hook(gate: Any, role: str) -> dict | None:
    """The role's own turn as its hook says it now, in the report's form; None when it cannot be read."""
    target = gate.role_targets.get(role)
    try:
        state = gate.state_store.read(target) if target else None
    except OSError:
        return None
    if state is None:
        return None
    return {"state": state.state, "updated_at": _iso(hook_word_at(gate, state)), "turn_started_at": _iso(state.turn_started_at),
            **_tool_activity(gate, state)}


def _with_owner_hook(holder: Any, payload: str) -> str:
    """A stall escalation asked about at delivery, with its owner's hook as it is now (SYRD-675)."""
    read = getattr(holder, "stall_owner_hook", None)
    try:
        parsed = json.loads(payload)
    except ValueError:
        return payload
    if not callable(read) or not isinstance(parsed, dict) or parsed.get("kind") != "stall_escalation":
        return payload
    hook = read(str(parsed.get("role") or ""))
    return payload if hook is None else json.dumps({**parsed, "owner_hook": hook}, ensure_ascii=False)


def _present(holder: Any, conn: Any) -> bool:
    """Whether this board has the episodes yet: a listener can be newer than its board's migrations."""
    present = getattr(holder, "_role_stall_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_role_stall(jsonb, integer, interval, "
                           "timestamptz, interval)') IS NOT NULL AS threshold, to_regprocedure('ticket_board."
                           "note_role_stall(jsonb, integer, interval, timestamptz)') IS NOT NULL AS plain").fetchone()
        takes = bool(row and (row["threshold"] if isinstance(row, dict) else row[0]))
        present = takes or bool(row and (row["plain"] if isinstance(row, dict) else row[1]))
        holder._role_stall_present = present
        holder._role_stall_takes_threshold = takes
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
        # Delivery asks about an escalation with its owner's hook as it is then: the gate reads it (SYRD-675).
        eligibility = getattr(listener, "eligibility", None)
        if eligibility is not None:
            eligibility.stall_owner_hook = lambda role: owner_hook(gate, role)
        grace = float(listener.unresolved_turn_grace_seconds)
        roles = report(gate, grace, watched)
        if not roles:
            return 0
        # Raw text, not \u escapes: a board whose database is SQL_ASCII cannot translate those.
        args = (json.dumps(roles, sort_keys=True, ensure_ascii=False), max_attempts(), f"{grace} seconds")
        if getattr(listener, "_role_stall_takes_threshold", False):
            # The stall threshold this listener is configured with, authoritative from its first pass: the board
            # stall pass that records it runs after this one (SYRD-675).
            queued = int(_first(conn.execute(
                "SELECT ticket_board.note_role_stall(%s::jsonb, %s, %s::interval, clock_timestamp(), %s::interval)",
                (*args, f"{board_stall.threshold_seconds()} seconds")).fetchone()) or 0)
        else:
            queued = int(_first(conn.execute("SELECT ticket_board.note_role_stall(%s::jsonb, %s, %s::interval)",
                                             args).fetchone()) or 0)
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
                       (ticket_id, target_role, _with_owner_hook(holder, payload))).fetchone()
    return row is not None and bool(_first(row))


def stale_reason(holder: Any, conn: Any, payload: str) -> str | None:
    """Why a stall escalation dropped at delivery no longer held: stable trace text, or None (SYRD-675)."""
    if not _present(holder, conn) or not _present_reason(holder, conn):
        return None
    row = conn.execute("SELECT ticket_board.role_stall_escalation_stale_reason(%s::jsonb) AS reason",
                       (_with_owner_hook(holder, payload),)).fetchone()
    reason = _first(row)
    return None if reason is None else _text(reason)


def _present_reason(holder: Any, conn: Any) -> bool:
    present = getattr(holder, "_stall_reason_present", None)
    if present is None:
        present = bool(_first(conn.execute("SELECT to_regprocedure('ticket_board.role_stall_escalation_stale_reason("
                                           "jsonb, timestamptz)') IS NOT NULL").fetchone()))
        holder._stall_reason_present = present
    return present
