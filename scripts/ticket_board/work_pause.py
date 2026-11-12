"""The Director's pause switch, the listener's half (SYRD-756).

The board records the pause (`ticket_board.pause_work`, `resume_work`; pgu1036): drain or stop, who set it, why and
since when. This module does what only the listener can, while one stands:

* **Delivery** (`hold_delivery`, asked first at a notice's claim and again just before its send): a notice to any
  role but the Director waits, requeued as `work_paused` -- held, not dropped, and not yet judged stale either, so
  what it is about is decided at the resume, once, when the resume makes it due. A check that cannot be made holds,
  as the account hold's does (SYRD-672). Everything else the listener would type --
  a context restore, a resume command, a capacity retry, a submit press, a menu press -- asks `typing_held`, which
  now says held for a paused role too. An image the desktop owner pastes to a role (image_handoff) is the User's own
  message, as typing into the pane is, and still goes.
* **Stop now** (`run`, at the start of every pass and between deliveries). For each paused role's pane:
  - a turn that was running when stop was set (the hook's turn start before `stop_since`) is interrupted once, with
    its runtime's own interrupt, measured offline on each CLI this host runs (tests/fixtures/syrd756_probe): Claude
    Code, Codex and agy Esc, Copilot Esc Esc (one Esc does nothing there). Hermes's own interrupt is a message: typed
    while it works (`msg=interrupt`, its default busy input), the message stops the turn and becomes the next
    prompt, so for Hermes the pause notice is that message. Never Ctrl-C, which a second press or an idle pane turns
    into an exit. A pane waiting on its provider's own question is left alone: an Esc there answers it. A turn begun
    after stop was set was begun by a person typing, which still works, and is left alone too.
  - Claude Code, Codex and Copilot run no turn-end hook for an interrupted turn, and agy ran none offline: the pane
    hook still says busy. Once the screen shows the runtime's own words for an interrupted turn and is still, with
    no provider question open, the turn is over and the hook state is written idle (`listener.work_pause_interrupt`),
    as SYRD-594 does for a Codex turn its capacity error ended. Claude Code puts a prompt it had not yet answered
    back in the composer; that is recorded as `prompt_returned`, and nothing is typed over it.
  - Once its interrupted turn is over, the role is given one notice, once, typed here at an empty composer with no
    provider question open -- not queued, since a background command left running under the pane would hold any
    queued delivery until it ended. No other hold is passed: a held provider account or one out of quota is typed
    nothing.
  - Background commands under the pane -- its own sessions, as the lease pass finds them -- are reported to the
    Director once, with pid, command and ticket. Nothing is killed.

The Director's pane is never touched. The pause is the database's; a restarted listener reads it there, and so
delivers nothing while paused.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

HOLD_ERROR = "work_paused"
#: The pause's own queued notices: the resume's, and the Director's background report. The board decides whether each
#: is still current (notification_eligibility); the stop notice is typed by `run`, not queued.
NOTICE_KINDS = frozenset({"work_resumed", "work_pause_background"})
#: How often a held notice asks again while the pause stands; the resume makes it due at once.
RECHECK_SECONDS = 300.0
#: Each runtime's own interrupt for a running turn, as tmux key names (measured, tests/fixtures/syrd756_probe).
INTERRUPT_KEYS = {"claude": ("Escape",), "codex": ("Escape",), "agy": ("Escape",), "copilot": ("Escape", "Escape")}
#: Runtimes whose interrupt is a message typed while they work, and the busy input row that says so.
MESSAGE_INTERRUPT = {"hermes": "msg=interrupt"}
#: What each runtime draws for a turn its interrupt ended (measured): its screen's word that the turn is over.
INTERRUPTED_WORDS = {
    "claude": "Interrupted · What should Claude do instead?",
    "codex": "■ Conversation interrupted",
    "copilot": "Operation cancelled by user",
    "agy": "Interrupted · What should Antigravity CLI do instead?",
}
SETTLE_SOURCE = "listener.work_pause_interrupt"
#: The pause between the presses of a two-key interrupt (Copilot's Esc Esc).
KEY_GAP_SECONDS = 0.3
#: How much of the bottom of a screen holds the latest turn's end.
SCREEN_TAIL_LINES = 24


def _one(conn: Any, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _present(listener: Any, conn: Any) -> bool:
    present = getattr(listener, "_work_pause_present", None)
    if present is None:
        value = _one(conn, "SELECT to_regprocedure('ticket_board.role_paused(text)') IS NOT NULL")
        present = listener._work_pause_present = value is True or value in ("t", b"t", 1)
    return present


def current(conn: Any) -> dict[str, Any] | None:
    """The pause standing now, or None."""
    row = _one(conn, "SELECT to_jsonb(p) FROM ticket_board.work_pauses p WHERE p.ended_at IS NULL")
    if isinstance(row, (str, bytes)):
        row = json.loads(row)
    return row if isinstance(row, dict) else None


def role_paused(conn: Any, role: str) -> int | None:
    value = _one(conn, "SELECT ticket_board.role_paused(%s)", (role,))
    return int(value) if value is not None else None


def paused_seconds(conn: Any, since: float) -> float:
    """How long work has been paused since `since` (epoch seconds), up to now: what a clock measured in Python adds to
    its deadline so it does not run while paused. 0 on a board older than the pause."""
    if not _one(conn, "SELECT to_regprocedure('ticket_board.unpaused_since(timestamp with time zone, timestamp with "
                      "time zone)') IS NOT NULL"):
        return 0.0
    value = _one(conn, "SELECT extract(epoch FROM ticket_board.unpaused_since(to_timestamp(%s), clock_timestamp()) "
                       "- to_timestamp(%s))", (since, since))
    return max(float(value or 0.0), 0.0)


def hold_delivery(listener: Any, conn: Any, notification_id: int, attempts: int, kind: str, target_role: str,
                  ticket_id: str, payload: Any, phase: str) -> bool:
    """True when the pause takes this notice: requeued as `work_paused`, to be asked again, never sent now."""
    if target_role == "director":
        return False
    try:
        if not _present(listener, conn):
            return False
        paused = role_paused(conn, target_role)
    except Exception as exc:
        why = f"{HOLD_ERROR}: the pause could not be checked ({type(exc).__name__})"
    else:
        if paused is None:
            return False
        why = f"{HOLD_ERROR}: pause {paused}"
    listener.logger.info("Holding %s notification %s for %s at %s: %s", kind, notification_id, target_role, phase, why)
    listener.ledger.requeue(conn, notification_id, attempts, why, delay_seconds=RECHECK_SECONDS)
    return True


def typing_held(listener: Any, conn: Any, role: str) -> bool:
    """Whether the pause holds anything typed into this role's pane now; a check that cannot be made holds."""
    if role == "director":
        return False
    try:
        return _present(listener, conn) and role_paused(conn, role) is not None
    except Exception as exc:
        listener.logger.warning("Not typing to %s: the work pause could not be checked (%s)", role, type(exc).__name__)
        return True


def project_paused(listener: Any, conn: Any) -> bool:
    """Whether any pause stands: no claim, route or worker start begins meanwhile. A check that cannot be made holds."""
    try:
        return _present(listener, conn) and current(conn) is not None
    except Exception as exc:
        listener.logger.warning("Starting nothing: the work pause could not be checked (%s)", type(exc).__name__)
        return True


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _screen(gate: Any, target: str) -> list[str] | None:
    """The pane's rows down to its last drawn one: these CLIs draw from the top and leave the rest of the pane blank."""
    try:
        capture = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-t", target],
                                           check=True, text=True, capture_output=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = capture.stdout.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _keys(gate: Any, target: str, *keys: str, literal: bool = False) -> None:
    gate.capture_pane_runner(["tmux", "send-keys", "-t", target, *(["-l"] if literal else []), *keys],
                             check=True, text=True, capture_output=True, timeout=5.0)


def _row(conn: Any, pause: int, role: str) -> dict[str, Any]:
    value = _one(conn, "SELECT to_jsonb(w) FROM ticket_board.work_pause_roles w WHERE pause_id = %s AND role = %s",
                 (pause, role))
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def run(listener: Any, conn: Any) -> int:
    """One pass of a stop pause; returns how many panes were interrupted. Never fatal to the pass."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store") or not getattr(gate, "role_targets", None):
        return 0
    try:
        if not _present(listener, conn):
            return 0
        pause = current(conn)
        if pause is None or pause.get("mode") != "stop":
            return 0
        stop_since = _epoch(pause.get("stop_since"))
        interrupted = 0
        for role, target in sorted(gate.role_targets.items()):
            if role_paused(conn, role) != int(pause["id"]):
                continue  # the Director, and a role with no pane: the board says which roles a pause holds
            try:
                interrupted += _role(listener, conn, gate, pause, stop_since, role, target)
            except Exception as exc:  # one pane's failure is not the others'
                listener.logger.warning("Work pause pass failed for %s: %s", role, exc)
        return interrupted
    except Exception as exc:
        listener.logger.warning("Work pause pass failed: %s", exc)
        return 0


def _role(listener: Any, conn: Any, gate: Any, pause: dict[str, Any], stop_since: float | None, role: str,
          target: str) -> int:
    pause_id = int(pause["id"])
    runtime = str((getattr(gate, "role_runtimes", None) or {}).get(role) or "")
    _report_background(listener, conn, gate, pause_id, role, target)
    state = gate.state_store.read(target)
    record = _row(conn, pause_id, role)
    turn = getattr(state, "turn_started_at", None) if state is not None else None
    interrupted = 0
    if (state is not None and state.state in ("busy", "blocked") and turn is not None and stop_since is not None
            and turn <= stop_since):  # once per turn: the board records it first, and refuses it twice
        interrupted = _interrupt(listener, conn, gate, pause_id, role, target, runtime, turn)
        record = _row(conn, pause_id, role)
    if record.get("interrupted_at") and not record.get("settled") and record.get("turn_started_at") == turn:
        _settle(listener, conn, gate, pause_id, role, target, runtime, state)
        record = _row(conn, pause_id, role)
    elif record.get("interrupted_at") and not record.get("settled") and state is not None and state.state == "idle":
        _one(conn, "SELECT ticket_board.note_work_pause_settled(%s, %s, 'idle')", (pause_id, role))
        record = _row(conn, pause_id, role)
    if record.get("settled") == "idle":
        _tell(listener, conn, gate, pause_id, role, target)  # once: the board records it before it is typed
    return interrupted


def _may_type(listener: Any, conn: Any, role: str) -> bool:
    """No other hold forbids typing into this pane: a held provider account or a provider out of quota."""
    from . import provider_refusal, quota_hold

    if provider_refusal.typing_held(listener, conn, role, pause=False):
        return False
    try:
        return quota_hold._until(conn, role)[0] is None
    except Exception:  # a check that cannot be made holds
        return False


def _tell(listener: Any, conn: Any, gate: Any, pause_id: int, role: str, target: str) -> None:
    """The stop's one notice, typed once the interrupted turn is over: at an empty composer, with no question open."""
    if not _may_type(listener, conn, role):
        return
    composing = gate._target_cursor_state(target)
    if composing is None or composing or gate._open_prompt_trace(target) is not None:
        return  # someone's text in the composer, or the provider's own question: never typed over
    notice = _one(conn, "SELECT ticket_board.note_work_pause_notice(%s, %s)", (pause_id, role))
    if isinstance(notice, (str, bytes)):
        notice = json.loads(notice)
    if not isinstance(notice, dict) or not notice.get("text"):
        return
    from .notification_dispatch import display_message

    try:
        listener.sender(target, display_message(notice["text"]))
    except Exception as exc:  # recorded as told; the Director sees it in the read, and the pane is never typed twice
        listener.logger.warning("Work paused: %s's notice could not be typed: %s", role, exc)
        return
    listener.logger.info("Work paused: %s told, on %s", role, notice.get("ticket"))


def _interrupt(listener: Any, conn: Any, gate: Any, pause_id: int, role: str, target: str, runtime: str,
               turn: float) -> int:
    from . import provider_prompt

    lines = _screen(gate, target)
    if lines is None:
        return 0
    if provider_prompt.open_prompt(lines) is not None:
        return 0  # its provider's own question: an Esc would answer it, and nothing runs until a person does
    if runtime in MESSAGE_INTERRUPT:
        if not any(MESSAGE_INTERRUPT[runtime] in line for line in lines[-SCREEN_TAIL_LINES:]):
            return 0  # not its busy input row: typing now would not interrupt anything
        if not _may_type(listener, conn, role):
            return 0
        if not _one(conn, "SELECT ticket_board.note_work_pause_interrupt(%s, %s, %s, %s, %s::jsonb)",
                    (pause_id, role, runtime, turn, json.dumps({"by": "message", "row": MESSAGE_INTERRUPT[runtime]}))):
            return 0
        notice = _one(conn, "SELECT ticket_board.note_work_pause_notice(%s, %s)", (pause_id, role))
        if isinstance(notice, (str, bytes)):
            notice = json.loads(notice)
        text = notice.get("text") if isinstance(notice, dict) else None
        if not text:
            return 0  # told already, or nothing to hang it on: no message, so no interrupt
        listener.logger.info("Work paused: interrupting %s's %s turn with the pause notice (its own interrupt)",
                             role, runtime)
        _keys(gate, target, text, literal=True)
        time.sleep(KEY_GAP_SECONDS)
        _keys(gate, target, "Enter")
        _one(conn, "SELECT ticket_board.note_work_pause_settled(%s, %s, 'typed')", (pause_id, role))
        return 1
    keys = INTERRUPT_KEYS.get(runtime)
    if not keys:
        listener.logger.warning("Work paused: %s runs %s, whose interrupt is not known; its turn is left to finish",
                                role, runtime or "an unknown runtime")
        return 0
    # How many of the runtime's "interrupted" lines the screen shows already: the interrupt's own adds one more.
    before = _said(lines, runtime)
    if not _one(conn, "SELECT ticket_board.note_work_pause_interrupt(%s, %s, %s, %s, %s::jsonb)",
                (pause_id, role, runtime, turn, json.dumps({"by": "keys", "keys": list(keys), "said_before": before}))):
        return 0
    listener.logger.info("Work paused: interrupting %s's %s turn with %s", role, runtime, " ".join(keys))
    for index, key in enumerate(keys):
        if index:
            time.sleep(KEY_GAP_SECONDS)
        _keys(gate, target, key)
    return 1


def _said(lines: list[str], runtime: str) -> int:
    """How many lines of the screen carry the runtime's own words for an interrupted turn."""
    words = INTERRUPTED_WORDS.get(runtime)
    return sum(1 for line in lines if words and words in line.replace("\xa0", " "))


def _settle(listener: Any, conn: Any, gate: Any, pause_id: int, role: str, target: str, runtime: str,
            state: Any) -> None:
    """The interrupted turn is over once its runtime says so on a still screen -- one more of its "interrupted" lines
    than before the interrupt -- or, for Claude Code, once the prompt is back in its composer; write it idle then."""
    if state is None or state.state not in ("busy", "blocked"):
        _one(conn, "SELECT ticket_board.note_work_pause_settled(%s, %s, 'idle')", (pause_id, role))
        return
    lines = _screen(gate, target)
    if runtime not in INTERRUPTED_WORDS or lines is None:
        return
    record = _row(conn, pause_id, role)
    said = _said(lines, runtime) > int(((record.get("interrupt") or {}).get("said_before")) or 0)
    if gate._working_timer_trace(target, sample_delay_seconds=gate.idle_working_timer_sample_delay_seconds) is not None:
        return  # still moving
    composing = gate._target_cursor_state(target)
    if gate._open_prompt_trace(target) is not None or composing is None:
        return
    returned = runtime == "claude" and composing and not said
    if not (said or returned):
        return
    gate.state_store.write(target, "idle", source=SETTLE_SOURCE)
    how = "prompt_returned" if composing else "idle"  # text in the composer: nothing is typed over it
    if _one(conn, "SELECT ticket_board.note_work_pause_settled(%s, %s, %s)", (pause_id, role, how)):
        listener.logger.info("Work paused: %s's interrupted turn is over (%s)", role, how)


def _report_background(listener: Any, conn: Any, gate: Any, pause_id: int, role: str, target: str) -> None:
    """Background commands under the pane, once per pause: pid, command and ticket, to the Director. None killed."""
    if not hasattr(gate, "_pane_pid"):
        return  # once per pause: the board refuses a second report
    from . import background_child, execution_lease_pass
    from . import notify_listener as nl

    pane_pid = gate._pane_pid(target)
    if pane_pid is None:
        return
    table = list(gate.process_table_reader())
    sample = nl.descendant_work_sample(pane_pid, table)
    provider_started = next((float(row[4]) for row in table if int(row[0]) == pane_pid and len(row) > 4), 0.0)
    furniture_until = provider_started + gate.LAUNCHER_SEED_SETTLE_SECONDS
    holders = [h for h in background_child.describe(sample, sample.detached, (), proc_root=Path(gate.proc_root))
               if h.detached and h.started_at > furniture_until]  # a report needs no process group to signal
    if not holders:
        return
    terminal = listener.activity_hold._terminal_states()
    ticket = execution_lease_pass.role_ticket(conn, role, terminal)
    commands = [{"pid": h.pid, **({"pgid": h.pgid} if h.pgid > 0 else {}), "command": h.summary, "ticket": ticket}
                for h in holders]
    if _one(conn, "SELECT ticket_board.report_work_pause_background(%s, %s, %s::jsonb)",
            (pause_id, role, json.dumps(commands))):
        listener.logger.info("Work paused: %s's background commands reported to the Director: %s", role,
                             ", ".join(str(c["pid"]) for c in commands))


def describe(payload: dict[str, Any]) -> str:
    """`ticket-board-read pause`: what stands, and how to change or end it."""
    pause = payload.get("pause")
    if not pause:
        lines = ["Work is not paused."]
        for p in (payload.get("recent") or [])[:1]:
            lines.append(f"Last pause {p['id']} ({p['mode']}): set by {p['began_by']} at {p['began_at']} -- {p['reason']}; "
                         f"resumed by {p.get('ended_by')} at {p.get('ended_at')} -- {p.get('end_reason')}")
        return "\n".join(lines)
    mode = {"drain": "DRAIN (no notifications; a turn already running finishes)",
            "stop": "STOP NOW (running turns interrupted; nothing new starts)"}.get(pause["mode"], pause["mode"])
    lines = [f"WORK PAUSED -- {mode}", f"  pause {pause['id']}, set by {pause['began_by']} at {pause['began_at']}: "
                                      f"{pause['reason']}"]
    for change in pause.get("changes") or []:
        lines.append(f"  changed {change.get('from')} -> {change.get('to')} by {change.get('by')} at {change.get('at')}: "
                     f"{change.get('reason')}")
    held = pause.get("held") or {}
    lines.append("  held for: " + (", ".join(f"{role} ({count})" for role, count in sorted(held.items())) or "nothing yet"))
    for r in pause.get("roles") or []:
        bits = []
        if r.get("interrupted_at"):
            bits.append(f"interrupted {r['interrupted_at']}" + (f", {r['settled']}" if r.get("settled") else ", ending"))
        if r.get("noticed_at"):
            bits.append(f"told on {r.get('notice_ticket')}")
        if r.get("background"):
            bits.append("background left running: " + ", ".join(f"pid {c.get('pid')}" for c in r["background"]))
        if bits:
            lines.append(f"  {r['role']}: " + "; ".join(bits))
    lines.append("  the Director's pane is exempt; the Publisher keeps publishing")
    lines.append("  change: ticket-board-write pause-work --mode drain|stop --reason-file PATH")
    lines.append("  end:    ticket-board-write resume-work --reason-file PATH")
    return "\n".join(lines)
