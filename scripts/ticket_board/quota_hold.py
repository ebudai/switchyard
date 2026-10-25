"""A role's provider out of quota: one bounded hold, from fresh evidence only (SYRD-644).

Each listener pass, for every role with a registered provider process:

* **Evidence.** What the provider itself said, tied to the turn it answered
  and to the registered process -- its pid and start time, as the hook recorded
  them -- never re-read from an old screen (provider_quota recognises the words):
  - Claude Code: the pane hook's record of its StopFailure (provider_error),
    a `rate_limit` whose transcript record shows a rejected quota. Its time is
    the hook's own: the failure came after the turn it ended.
  - Codex: the quota line ending the screen above its empty prompt, while the
    hook says the turn that started it, which that process took, has not ended
    normally. Its time is that turn's start, so the same old line under the
    same turn is never new.

  `note_provider_quota` begins, repeats or renews the role's hold from it, and
  refuses evidence that is not from the registered process or not newer than
  the hold's last release.
* **Facts.** Each role's provider session and its latest normal turn end:
  `reconcile_provider_quota_holds` ends a hold the role's registration,
  process, session or a normal turn has overtaken, and releases a hold whose
  deadline has come -- after which exactly one fresh attempt goes.

The suppression itself is the board's (`ticket_turn_is_resolved`, asking
`role_provider_hold_is_current`) and the delivery check's (`hold_delivery`,
asking `role_provider_hold_until` and `provider_held_ticket_owner`).
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from . import provider_quota

GRACE_ENV = "TICKET_BOARD_QUOTA_HOLD_GRACE_SECONDS"
RECHECK_ENV = "TICKET_BOARD_QUOTA_HOLD_RECHECK_SECONDS"
DEFAULT_GRACE_SECONDS = 120.0
DEFAULT_RECHECK_SECONDS = 1800.0
#: A Codex pane whose last turn did not end normally: its own turn start, or the
#: listener's rewrite of a stale one, which keeps that turn (pane_activity_gate).
CODEX_UNENDED = {("busy", "codex.UserPromptSubmit"), ("idle", "listener.stale_codex_busy_recovery")}


#: The provider's own punctuation, said in ASCII: a board in SQL_ASCII counts bytes and would cut a character in half.
_ASCII = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "·": "-", "■": "*", "…": "...", "—": "-", "–": "-"})


def plain(text: str) -> str:
    return text.translate(_ASCII).encode("ascii", errors="replace").decode("ascii")


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value >= 0 else default


def _codex_screen(gate: Any, target: str) -> dict[str, Any] | None:
    try:
        cursor = gate.cursor_position_runner(
            ["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y} #{pane_height}"],
            check=True, text=True, capture_output=True, timeout=2.0)
        capture = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-t", target],
                                           check=True, text=True, capture_output=True, timeout=2.0)
        cursor_y = int(cursor.stdout.split()[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return provider_quota.codex_quota(capture.stdout.splitlines(), cursor_y, zone=provider_quota.local_zone())


def _registered_process(record: Any, registration: dict[str, Any]) -> bool:
    """Whether a hook's record names the registered provider process: its pid AND its start time, since a reused
    pid, or a replacement in the same pane, is another process."""
    try:
        return (int(record["pane_pid"]), int(record["pane_start_time"])) == (registration["pid"], registration["start_time"])
    except (KeyError, TypeError, ValueError):
        return False


def evidence_for(gate: Any, role: str, target: str, state: Any, registration: dict[str, Any]) -> dict[str, Any] | None:
    """The quota evidence this pane offers now, as the board takes it; None when there is none."""
    runtime = registration["runtime"]
    found: dict[str, Any] | None = None
    observed: float | None = None
    turn = ""
    source = ""
    error = state.provider_error if isinstance(state.provider_error, dict) else None
    if runtime == "claude" and error and error.get("source") == "claude.StopFailure":
        if not _registered_process(error, registration):
            return None  # another process's record, or one that names none
        try:
            observed = float(error["observed_at"])
        except (KeyError, TypeError, ValueError):
            return None
        found = provider_quota.classify_claude(str(error.get("error") or ""), error.get("quota"),
                                               str(error.get("message") or ""), observed=observed)
        turn, source = f"stopfailure:{observed}", "claude.StopFailure"
    elif (runtime == "codex" and (state.state, state.source) in CODEX_UNENDED and state.turn_started_at is not None
          and _registered_process(getattr(state, "turn_process", None), registration)):
        found = _codex_screen(gate, target)
        observed, turn, source = state.turn_started_at, f"turn:{state.turn_started_at}", "codex.screen"
    if not found or found.get("kind") != "quota" or observed is None:
        return None
    session = state.last_session_start.get("session_id") if isinstance(state.last_session_start, dict) else ""
    return {
        "runtime": runtime, "generation": registration["generation"], "pid": registration["pid"],
        "start_time": registration["start_time"],
        "session": str((error or {}).get("session_id") or session or ""), "target": target, "source": source,
        "observed_at": provider_quota.iso(observed), "turn": turn, "message": plain(found.get("message", "")),
        "limit": found.get("limit", ""), "reset_at": provider_quota.iso(found.get("reset_at")),
        **({"count": found["count"]} if "count" in found else {}),
        "grace_seconds": _seconds(GRACE_ENV, DEFAULT_GRACE_SECONDS),
        "recheck_seconds": _seconds(RECHECK_ENV, DEFAULT_RECHECK_SECONDS),
    }


def facts_for(state: Any) -> dict[str, Any]:
    """What can end a hold early: the pane's provider session, and its latest normal turn end."""
    from .pane_activity_gate import IDLE_TURN_END_SOURCES

    session = state.last_session_start.get("session_id") if isinstance(state.last_session_start, dict) else ""
    ended = state.state == "idle" and state.source in IDLE_TURN_END_SOURCES
    return {"session": str(session or ""), "normal_end_at": provider_quota.iso(state.updated_at) if ended else None}


def _text(value: Any) -> str:
    """The listener's connection returns text as bytes."""
    return value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray, memoryview)) else str(value)


def _present(listener: Any, conn: Any) -> bool:
    present = getattr(listener, "_provider_quota_holds_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_provider_quota(text, jsonb)') IS NOT NULL AS present").fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        listener._provider_quota_holds_present = present
    return present


def run(listener: Any, conn: Any) -> dict[str, Any]:
    """One pass. Never stops the listener: a failure is logged, and the pass's deliveries still go."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store") or not _present(listener, conn):
        return {}
    try:
        registrations = {
            _text(row["role"] if isinstance(row, dict) else row[0]): {
                "runtime": _text(row["runtime"] if isinstance(row, dict) else row[1]),
                "generation": int(row["generation"] if isinstance(row, dict) else row[2]),
                "pid": int(row["process_pid"] if isinstance(row, dict) else row[3]),
                "start_time": int(row["process_start_time"] if isinstance(row, dict) else row[4]),
            }
            for row in conn.execute("SELECT role, runtime, generation, process_pid, process_start_time "
                                    "FROM ticket_board.role_runtime_assignments "
                                    "WHERE process_pid IS NOT NULL").fetchall()
        }
        facts: dict[str, Any] = {}
        noted: dict[str, Any] = {}
        for role, target in sorted(gate.role_targets.items()):
            registration = registrations.get(role)
            if registration is None:
                continue
            try:
                state = gate.state_store.read(target)
            except OSError:
                continue
            if state is None:
                continue
            facts[role] = facts_for(state)
            evidence = evidence_for(gate, role, target, state, registration)
            if evidence is not None:
                row = conn.execute("SELECT ticket_board.note_provider_quota(%s, %s::jsonb)",
                                   (role, json.dumps(evidence, sort_keys=True, ensure_ascii=False))).fetchone()
                noted[role] = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
        row = conn.execute("SELECT ticket_board.reconcile_provider_quota_holds(%s::jsonb, clock_timestamp())",
                           (json.dumps(facts, sort_keys=True, ensure_ascii=False),)).fetchone()
        reconciled = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
    except Exception as exc:  # logged; never fatal to the pass
        listener.logger.warning("Provider quota hold pass failed: %s", exc)
        return {}
    began = {role: result for role, result in noted.items() if isinstance(result, dict) and not result.get("stale")}
    if began or (isinstance(reconciled, dict) and any(reconciled.values())):
        listener.logger.info("Provider quota holds: noted %s; reconciled %s", began, reconciled)
    return {"noted": noted, "reconciled": reconciled}


#: Director notices whose only cause, while a role's provider is held, is that
#: role being unavailable: dropped, since the hold's own notice already said it.
#: A permission prompt, a blocked pane or a long command is not that.
DIRECTOR_KINDS_ABOUT_ROLE = frozenset({"unresolved_turn", "escalation", "stall_escalation", "provider_capacity"})
HOLD_ERROR = "provider_quota_hold"


def _held_subject(conn: Any, ticket_id: str, payload: dict[str, Any]) -> str:
    """The held role a Director notice is about -- the one it names, else its ticket's owner; "" when not held."""
    named = str(payload.get("owner_role") or payload.get("role") or "").strip()
    if named:
        return named if _until(conn, named)[0] is not None else ""
    row = conn.execute("SELECT ticket_board.provider_held_ticket_owner(%s, clock_timestamp())", (ticket_id,)).fetchone()
    return _text((next(iter(row.values())) if isinstance(row, dict) else row[0]) or "") if row else ""


def _until(conn: Any, role: str) -> Any:
    row = conn.execute("SELECT ticket_board.role_provider_hold_until(%s, clock_timestamp()), clock_timestamp()",
                       (role,)).fetchone()
    if not row:
        return None, None
    values = list(row.values()) if isinstance(row, dict) else list(row)
    return values[0], values[1]


def hold_delivery(listener: Any, conn: Any, notification_id: int, attempts: int, kind: str, target_role: str,
                  ticket_id: str, payload: str, phase: str) -> bool:
    """True when a held provider takes this notice: requeued until the hold ends, or, for the Director, dropped.

    Asked at claim and again just before the send, like every other hold on
    delivery. A notice to the held role waits -- nothing about its work is
    cancelled -- and goes at the release, as the one fresh attempt.
    """
    if not getattr(listener, "_provider_quota_holds_present", False):
        return False
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    try:
        if target_role != "director":
            until, now = _until(conn, target_role)
            if until is None:
                return False
            listener.logger.info("Holding %s notification %s for %s at %s: its provider is out of quota until %s",
                                 kind, notification_id, target_role, phase, until)
            listener.ledger.requeue(conn, notification_id, attempts, f"{HOLD_ERROR} until {until.isoformat()}",
                                    delay_seconds=max((until - now).total_seconds(), 1.0))
            return True
        if kind not in DIRECTOR_KINDS_ABOUT_ROLE or parsed.get("reason") in {HOLD_ERROR, "permission_prompt"}:
            return False
        role = _held_subject(conn, ticket_id, parsed)
        if not role:
            return False
    except Exception as exc:  # never blocks a delivery on a failed check
        listener.logger.warning("Provider quota hold check failed for notification %s: %s", notification_id, exc)
        return False
    listener.logger.info("Dropping %s notification %s about %s at %s: its provider is out of quota, as the Director was told",
                         kind, notification_id, role, phase)
    safe = listener.ledger.safe_json_payload(payload)
    listener.ledger.trace(conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role, kind=kind,
                          event="drop", busy_reason=HOLD_ERROR, detail={"payload": safe, "role": role, "phase": phase})
    listener.ledger.trace(conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role, kind=kind,
                          event="listener_ack", detail={"reason": HOLD_ERROR, "phase": phase})
    listener.ledger.ack(conn, notification_id)
    listener.ledger.forget(notification_id)
    return True
