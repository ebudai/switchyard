"""A provider's safety refusal holds its whole account, until a person clears it (SYRD-672).

Repeated cybersecurity or safety refusals can get a provider account suspended, so the first one is a circuit
breaker. Each listener pass, for every role with a registered provider process:

* **Evidence**, only when positively identified, and only from the registered process -- its pid and start time:
  - Claude Code: the pane hook's `last_refusal`, the turn's last transcript entry ending on `stop_reason: "refusal"`.
    Its identity is that entry's transcript uuid.
  - Codex: one of its own cybersecurity lines (`CODEX_REFUSAL_LINES`, from its binary) as Codex's own event block at
    the margin, directly above the prompt with nothing after it, while the hook says the turn that process took has
    not ended normally. Its identity is that occurrence: the role's pane, which refusal it is in the pane's history,
    and a digest of its block and prompt echo -- never the turn or the process now running, so an old line under a new
    turn, or after a restart in the same pane, is not new.

  `note_provider_refusal` begins the account's hold from it, once per identity ever, so an old record or screen can
  never hold the account again after a clear. Nothing less certain begins a hold: a person holds by hand
  (`hold_provider_account`) instead.
* **Delivery**, asked before every send by `quota_hold.hold_delivery`: a notice to any role on a held account waits,
  and so does every notice to a Director whose own account is held. It fails closed: a check that cannot be made
  holds the notice. The hold's own notice to the Director is the one that goes.

The rest -- reminders, nudges, stall notices, pull claims, admission -- ask the board's own predicates, which the
migration (pgu1030) extends. Nothing here, ever, asks the provider anything.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

#: Codex's own words when it refuses for cybersecurity, as its binary carries them (codex-cli 0.166.0, 2026-11-07).
CODEX_REFUSAL_LINES = (
    "This request has been flagged for possible cybersecurity risk.",
    "Your account was flagged for potentially high-risk cyber activity",
)
#: How Codex draws an event of its own at the margin -- an error, or a warning -- never a prompt or a reply's text.
CODEX_EVENT_MARKS = ("■ ", "⚠ ")
HOLD_ERROR = "provider_account_hold"
NOTICE_REASON = "provider_safety_hold"
RECHECK_SECONDS = 300.0


def _first(row: Any) -> Any:
    if not row:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def codex_refusal(rows: list[str], cursor_y: int, history: list[str]) -> dict[str, Any] | None:
    """The refusal ending the live turn: Codex's own event block, at the margin, directly above the prompt at the
    cursor, with nothing after it -- no later prompt echo, no reply. None otherwise.

    Its identity is the occurrence, not the turn now running: which refusal it is in the pane's history (its ordinal)
    and a digest of its block and the prompt echo above it. So an old refusal still showing when a new turn starts is
    the same occurrence, and a refusal drawn afresh is a new one.
    """
    from .provider_quota import CODEX_PROMPT

    if not 0 <= cursor_y < len(rows) or not rows[cursor_y].startswith(CODEX_PROMPT):
        return None
    block: list[str] = []
    top = cursor_y
    for top in range(cursor_y - 1, -1, -1):
        line = rows[top]
        if not line.strip():
            if block:
                break
            continue
        if line.startswith(CODEX_PROMPT):
            return None  # a prompt echo after any event: the live turn's output is not a refusal
        block.insert(0, line.rstrip())
        if line.startswith(CODEX_EVENT_MARKS):
            break
    text = " ".join(" ".join(block).split())
    if not block or not block[0].startswith(CODEX_EVENT_MARKS) or not any(r in text for r in CODEX_REFUSAL_LINES):
        return None
    echo = next((rows[i].rstrip() for i in range(top - 1, -1, -1) if rows[i].startswith(CODEX_PROMPT)), "")
    ordinal = sum(1 for line in history if line.startswith(CODEX_EVENT_MARKS) and any(r in line for r in CODEX_REFUSAL_LINES))
    return {"ordinal": max(ordinal, 1), "digest": hashlib.sha256(f"{echo}\n{text}".encode()).hexdigest()[:16]}


def _codex_screen(gate: Any, target: str) -> dict[str, Any] | None:
    try:
        cursor = gate.cursor_position_runner(
            ["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y} #{pane_height}"],
            check=True, text=True, capture_output=True, timeout=2.0)
        rows = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-t", target],
                                        check=True, text=True, capture_output=True, timeout=2.0).stdout.splitlines()
        history = gate.capture_pane_runner(["tmux", "capture-pane", "-p", "-J", "-S", "-", "-t", target],
                                           check=True, text=True, capture_output=True, timeout=2.0).stdout.splitlines()
        cursor_y = int(cursor.stdout.split()[1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError):
        return None
    return codex_refusal(rows, cursor_y, history)


def evidence_for(gate: Any, role: str, target: str, state: Any, registration: dict[str, Any]) -> dict[str, Any] | None:
    """The refusal this pane shows now, as the board takes it; None when there is none."""
    from .quota_hold import CODEX_UNENDED, _registered_process

    runtime = registration["runtime"]
    found: dict[str, Any] | None = None
    refusal = state.last_refusal if isinstance(getattr(state, "last_refusal", None), dict) else None
    if runtime == "claude" and refusal and str(refusal.get("evidence") or ""):
        if not _registered_process(refusal, registration):
            return None  # another process's record
        found = {"category": str(refusal.get("category") or "")[:32], "source": str(refusal.get("source") or "")[:64],
                 "evidence": f"claude:{refusal['evidence']}"}
    elif (runtime == "codex" and (state.state, state.source) in CODEX_UNENDED and state.turn_started_at is not None
          and _registered_process(getattr(state, "turn_process", None), registration)):
        seen = _codex_screen(gate, target)
        if seen is None:
            return None
        found = {"category": "cyber", "source": "codex.screen", "evidence": (
            f"codex:{role[:48]}:{seen['ordinal']}:{seen['digest']}")}
    if found is None:
        return None
    return {"runtime": runtime, "pid": registration["pid"], "start_time": registration["start_time"], **found}


def _present(listener: Any, conn: Any) -> bool:
    present = getattr(listener, "_provider_account_holds_present", None)
    if present is None:
        row = conn.execute("SELECT to_regprocedure('ticket_board.note_provider_refusal(text, jsonb)') IS NOT NULL "
                           "AS present").fetchone()
        present = bool(_first(row))
        listener._provider_account_holds_present = present
    return present


def run(listener: Any, conn: Any, registrations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One pass over the registered panes. Never stops the listener: a failure is logged."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store") or not _present(listener, conn):
        return {}
    noted: dict[str, Any] = {}
    try:
        for role, target in sorted(gate.role_targets.items()):
            registration = registrations.get(role)
            if registration is None:
                continue
            try:
                state = gate.state_store.read(target)
            except OSError:
                continue
            evidence = evidence_for(gate, role, target, state, registration) if state is not None else None
            if evidence is not None:
                result = _first(conn.execute("SELECT ticket_board.note_provider_refusal(%s, %s::jsonb)",
                                             (role, json.dumps(evidence, sort_keys=True))).fetchone())
                noted[role] = result
                if isinstance(result, dict) and result.get("began"):
                    listener.logger.warning("Provider account %s held: %s's provider refused on safety grounds (hold %s)",
                                            result.get("runtime"), role, result.get("hold"))
    except Exception as exc:  # logged; never fatal to the pass
        listener.logger.warning("Provider refusal pass failed: %s", exc)
    return noted


def _held(conn: Any, role: str) -> bool:
    return _first(conn.execute("SELECT ticket_board.role_account_hold(%s)", (role,)).fetchone()) is not None


def typing_held(listener: Any, conn: Any, role: str) -> bool:
    """Whether the board may type nothing into this role's pane now -- a restore, a context note, a retry.

    Fails closed, like the delivery check: a hold that cannot be checked holds.
    """
    try:
        return _present(listener, conn) and _held(conn, role)
    except Exception as exc:
        listener.logger.warning("Not typing to %s: its provider account hold could not be checked (%s)",
                                role, type(exc).__name__)
        return True


def hold_delivery(listener: Any, conn: Any, notification_id: int, attempts: int, kind: str, target_role: str,
                  payload: Any, phase: str) -> bool:
    """True when a held account takes this notice: requeued, to be asked again, never sent.

    Fails closed: a check that cannot be made holds the notice too. Only the hold's own notice to a Director whose
    account is not the held one goes.
    """
    try:
        if not _present(listener, conn):
            return False
    except Exception as exc:
        why = f"{HOLD_ERROR}: the hold could not be checked ({type(exc).__name__})"
        listener.logger.info("Holding %s notification %s for %s at %s: %s", kind, notification_id, target_role, phase, why)
        listener.ledger.requeue(conn, notification_id, attempts, why, delay_seconds=RECHECK_SECONDS)
        return True
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except ValueError:
        parsed = {}
    try:
        if not _held(conn, target_role):
            return False
        why = f"{HOLD_ERROR}: {target_role}'s provider account is held"
    except Exception as exc:
        why = f"{HOLD_ERROR}: the hold could not be checked ({type(exc).__name__})"
    else:
        if isinstance(parsed, dict) and parsed.get("reason") == NOTICE_REASON and target_role == "director":
            why = f"{HOLD_ERROR}: the Director's own provider account is held"
    listener.logger.info("Holding %s notification %s for %s at %s: %s", kind, notification_id, target_role, phase, why)
    listener.ledger.requeue(conn, notification_id, attempts, why, delay_seconds=RECHECK_SECONDS)
    return True
