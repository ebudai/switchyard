"""The board stall alert: the Director is told, once, when runnable work has made no durable progress (SYRD-637).

Per-ticket paths already speak for themselves: an owner's unresolved turn is
prompted and then escalated (SYRD-595), triage tells the Director about new
intake (SYRD-120), a pull policy reports idle capacity (SYRD-539). None of them
says "nothing on this board has moved for half an hour". This does, once per
episode:

* On every listener pass, `run` reports what the listener sees of each role --
  the classifier pull pickup already uses (`pull_pickup.why_not_eligible`):
  no registered process, process gone, stopped on a prompt, working, background
  work, or idle -- and asks the board (`ticket_board.note_board_stall`) to judge.
* The board decides what is runnable now from the declared workflow and its
  own authorities -- stage shape, ownership, holds, blockers, dependencies,
  snoozes, serial slots and review lanes, pull claimability -- and when the
  last durable progress was, from events its own triggers record. After
  `threshold_seconds()` of runnable work with no progress it opens an episode
  and enqueues one digest to the Director, in one transaction, and closes the
  episode on progress or when nothing runnable remains.
* What the listener sees is evidence in the digest, never a reason to stay
  quiet: a busy pane may be a long test or a hang, and the Director decides.

`status` reads the state back (`ticket_board.board_stall_status`), for
GET /api/board-stall and `ticket-board-read stall`.
"""

from __future__ import annotations

import json
import os
from typing import Any

THRESHOLD_ENV = "TICKET_BOARD_BOARD_STALL_SECONDS"
DEFAULT_THRESHOLD_SECONDS = 1800
THRESHOLD_RANGE = (60, 86400)
#: The digest's payload kind: the Director's, and current only while its episode is open.
NOTICE_KIND = "board_stall"


def threshold_seconds(environ: dict[str, str] | None = None) -> int:
    """The one policy seam: how long runnable work may go without progress, bounded; 30 minutes by default."""
    raw = (os.environ if environ is None else environ).get(THRESHOLD_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_THRESHOLD_SECONDS
    except ValueError:
        return DEFAULT_THRESHOLD_SECONDS
    return min(max(value, THRESHOLD_RANGE[0]), THRESHOLD_RANGE[1])


def _present(conn: Any, signature: str) -> bool:
    row = conn.execute("SELECT to_regprocedure(%s) IS NOT NULL AS present", (signature,)).fetchone()
    return bool(row["present"] if isinstance(row, dict) else row[0]) if row is not None else False


def role_evidence(listener: Any) -> dict[str, dict[str, str]]:
    """What the listener sees of each declared role this pass: evidence for the digest, never a filter."""
    from .pull_pickup import why_not_eligible

    gate = getattr(getattr(listener, "activity_gate", None), "__self__", None)
    seen: dict[str, dict[str, str]] = {}
    for role in (getattr(listener, "workflow", None) or {}).get("roles", []):
        name = role.get("name")
        if not role.get("active") or name in ("director", "user", "unassigned"):
            continue
        if not role.get("runtime"):
            seen[name] = {"state": "system actor", "detail": "no pane; run by the board's listener"}
            continue
        if gate is None:
            seen[name] = {"state": "unknown", "detail": "the listener has no activity gate"}
            continue
        try:
            why = why_not_eligible(gate, name)
        except Exception as exc:  # evidence, not a decision: say what could not be read
            seen[name] = {"state": "unknown", "detail": f"could not be read ({type(exc).__name__})"}
            continue
        seen[name] = {"state": why or "idle", "detail": ""}
    return seen


def run(listener: Any, conn: Any) -> dict | None:
    """One pass: report the roles and let the board open, notify or close the episode."""
    if not getattr(listener, "workflow", None) or not _present(
            conn, "ticket_board.note_board_stall(jsonb, integer, timestamptz)"):
        return None
    try:
        row = conn.execute("SELECT ticket_board.note_board_stall(%s::jsonb, %s) AS result",
                           (json.dumps(role_evidence(listener)), threshold_seconds())).fetchone()
    except Exception as exc:  # one pass's failure is logged; the next pass asks again from the board's own state
        listener.logger.warning("board stall evaluation failed: %s", str(exc).splitlines()[0] if str(exc) else exc)
        return None
    result = row["result"] if isinstance(row, dict) else row[0]
    return json.loads(result) if isinstance(result, (str, bytes, bytearray)) else result


def notice_is_current(conn: Any, payload: Any) -> bool:
    """A digest is delivered only while its episode is open."""
    row = conn.execute("SELECT ticket_board.board_stall_notice_is_current(%s::jsonb) AS current",
                       (payload if isinstance(payload, str) else json.dumps(payload),)).fetchone()
    return bool(row["current"] if isinstance(row, dict) else row[0]) if row is not None else False


def status(app: Any) -> dict[str, Any]:
    """The stall state as the last evaluation left it, for GET /api/board-stall."""
    with app._pg_connect() as conn:
        if not _present(conn, "ticket_board.board_stall_status(timestamptz)"):
            return {"available": False}
        row = conn.execute("SELECT ticket_board.board_stall_status(clock_timestamp()) AS status").fetchone()
    value = row["status"] if isinstance(row, dict) else row[0]
    return {"available": True, **(json.loads(value) if isinstance(value, str) else (value or {}))}


def describe(state: dict[str, Any]) -> str:
    """The state as a person reads it: `ticket-board-read stall`."""
    if not state.get("available", True):
        return "board stall: this board predates the stall alert (SYRD-637)"
    lines = [f"board stall: last evaluated {state.get('evaluated_at') or 'never'}; "
             f"threshold {state.get('threshold_seconds')}s"]
    last = state.get("last_progress") or {}
    lines.append(f"last progress: {last.get('event', 'none recorded')} at {last.get('at', '-')}")
    runnable = state.get("runnable") or []
    if runnable:
        lines.append(f"runnable since {state.get('runnable_since')}; clock started {state.get('clock_started_at')}; "
                     f"fires at {state.get('fires_at')}")
        lines += [f"  {item['ticket']} {item['state']} -> {item.get('role') or 'nobody'}"
                  f" ({item.get('evidence') or 'no evidence'})" for item in runnable]
    else:
        lines.append("no runnable work: no clock is running")
    for ticket, why in sorted((state.get("excluded") or {}).items()):
        lines.append(f"  not counted: {ticket}: {why}")
    episode = state.get("open_episode")
    lines.append(f"open episode: {episode['id']} since {episode['opened_at']} (notice {episode.get('notification_id')})"
                 if episode else "open episode: none")
    return "\n".join(lines)
