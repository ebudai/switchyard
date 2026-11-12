"""A provider's safety refusal holds its whole account, until a person clears it (SYRD-672).

Repeated cybersecurity or safety refusals can get a provider account suspended, so the first one is a circuit
breaker. Each listener pass, for every role with a registered provider process:

* **Evidence**, only when positively identified, and only from the registered process -- its pid and start time:
  - Claude Code: the pane hook's `last_refusal`, the turn's last transcript entry ending on `stop_reason: "refusal"`.
    Its identity is that entry's transcript uuid.
  - Codex: one of its own cybersecurity lines (`CODEX_REFUSAL_LINES`, from its binary) as Codex's own event block at
    the margin, directly above the prompt with nothing after it, while the hook says the turn that process took has
    not ended normally -- and first seen by the board, by that process, after that turn began
    (`note_refusal_sighting`, kept in the database across listener restarts). An old line under a new turn was seen
    before it began, so it is not new; a respawned pane refusing the same prompt in the same words is. Its identity
    is that process and turn.

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
    """What ends the live turn on a Codex screen: {"signature": ...} when it is a refusal -- Codex's own event block at
    the margin, directly above the prompt at the cursor, with nothing after it (no later prompt echo, no reply) --
    {"signature": None} when the screen reads plainly and is not one, and None when it cannot be read (no prompt at
    the cursor), which says nothing either way.

    The signature names what is drawn -- the block, the prompt echo above it, and how many refusals the pane's history
    holds -- so the same drawing seen again has the same signature. It is not the refusal's identity: an identical
    refusal drawn afresh may have it too, after the history is cleared. Whether it is fresh is when it was first seen.
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
            return {"signature": None}  # a prompt echo after any event: the live turn's output is not a refusal
        block.insert(0, line.rstrip())
        if line.startswith(CODEX_EVENT_MARKS):
            break
    text = " ".join(" ".join(block).split())
    if not block or not block[0].startswith(CODEX_EVENT_MARKS) or not any(r in text for r in CODEX_REFUSAL_LINES):
        return {"signature": None}
    echo = next((rows[i].rstrip() for i in range(top - 1, -1, -1) if rows[i].startswith(CODEX_PROMPT)), "")
    ordinal = sum(1 for line in history if line.startswith(CODEX_EVENT_MARKS) and any(r in line for r in CODEX_REFUSAL_LINES))
    return {"signature": hashlib.sha256(f"{ordinal}\n{echo}\n{text}".encode()).hexdigest()[:16]}


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


def evidence_for(gate: Any, role: str, target: str, state: Any, registration: dict[str, Any],
                 sighted: Any = None) -> dict[str, Any] | None:
    """The refusal this pane shows now, as the board takes it; None when there is none.

    Codex: `sighted(signature)` records what the screen shows and answers when the board first saw it, by this
    process (epoch seconds, or None). A refusal is this turn's only when first seen after the turn began: one already
    showing when it began is an older turn's, however it is drawn, and holds nothing (Audit, rounds 1 and 2). Its
    identity is then the process and the turn: a respawned pane, whose history is gone, refusing the same prompt
    with the same words, is a new refusal.
    """
    from .quota_hold import CODEX_UNENDED, _registered_process

    runtime = registration["runtime"]
    found: dict[str, Any] | None = None
    refusal = state.last_refusal if isinstance(getattr(state, "last_refusal", None), dict) else None
    if runtime == "claude" and refusal and str(refusal.get("evidence") or ""):
        if not _registered_process(refusal, registration):
            return None  # another process's record
        found = {"category": str(refusal.get("category") or "")[:32], "source": str(refusal.get("source") or "")[:64],
                 "evidence": f"claude:{refusal['evidence']}"}
    elif (runtime == "codex" and sighted is not None and (state.state, state.source) in CODEX_UNENDED
          and state.turn_started_at is not None
          and _registered_process(getattr(state, "turn_process", None), registration)):
        seen = _codex_screen(gate, target)
        if seen is None:
            return None  # unreadable: nothing recorded, nothing taken
        first = sighted(seen["signature"])
        if seen["signature"] is None or first is None or first < float(state.turn_started_at):
            return None
        found = {"category": "cyber", "source": "codex.screen", "evidence": (
            f"codex:{registration['pid']}:{registration['start_time']}:{state.turn_started_at}")}
    if found is None:
        return None
    return {"runtime": runtime, "pid": registration["pid"], "start_time": registration["start_time"], **found}


def _sighting(listener: Any, conn: Any, role: str, registration: dict[str, Any], signature: str | None) -> float | None:
    """note_refusal_sighting, written only when what the pane shows changes; its answer kept for the next pass."""
    seen = getattr(listener, "_refusal_sightings", None)
    if seen is None:
        seen = listener._refusal_sightings = {}
    key = (registration["pid"], registration["start_time"], signature)
    if role in seen and seen[role][0] == key:
        return seen[role][1]
    first = _first(conn.execute("SELECT ticket_board.note_refusal_sighting(%s, %s, %s, %s)",
                                (role, registration["pid"], registration["start_time"], signature)).fetchone())
    first = None if first is None else float(first)
    seen[role] = (key, first)
    return first


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
            sighted = lambda signature, role=role, registration=registration: _sighting(  # noqa: E731
                listener, conn, role, registration, signature)
            evidence = evidence_for(gate, role, target, state, registration, sighted) if state is not None else None
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


def typing_held(listener: Any, conn: Any, role: str, *, pause: bool = True) -> bool:
    """Whether the board may type nothing into this role's pane now -- a restore, a context note, a retry.

    Fails closed, like the delivery check: a hold that cannot be checked holds. A work pause holds too (SYRD-756),
    except where `pause` is False: a notice's own delivery, which only the pause's own notices reach while it stands.
    """
    from . import work_pause

    if pause and work_pause.typing_held(listener, conn, role):
        return True
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
