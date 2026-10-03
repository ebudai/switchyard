"""A pulled ticket's own conversation: proven, then restored, or parked (SYRD-540).

Under a pull policy an author can start a new ticket after its previous one
passes Audit, and that earlier ticket can come back (MEFP: Final Sign-Off).
Its author must then work it in THAT ticket's conversation -- fresh context is
only ever for a genuinely new ticket.

* Proof. After the listener clears a role's conversation for a new ticket,
  the pane hook records the next SessionStart as `last_session_start`, stamped
  with the provider process and the checkout. A ticket is bound to that
  session only if it says `source: clear`, started after this clear and came
  from the role's registered provider process -- never the pane's newest record
  relabelled. No such proof within BIND_SECONDS records the ticket as
  unconfirmed, and the Director is told once.
* Restore. A ticket whose conversation the pane no longer holds is handed back
  only after the pane is proven back in it: Claude is sent `/resume <session>`
  (Claude Code 2.1.284's own in-session command), and the hand-off waits for a
  SessionStart from the registered process with `source: resume`, the saved
  id and the ticket's worktree. SessionStart can be deferred to the next prompt
  (docs/pgu-816-clear-sessionstart-evidence.md), so after RESUME_WAIT_SECONDS
  one non-actionable CONFIRM_PROMPT -- no ticket, no work -- may draw it out.
* Parked. A restore that is disproved, never confirmed within CONFIRM_SECONDS,
  or cannot be attempted (no proof, another runtime, a runtime with no verified
  in-session resume: Codex, agy, Hermes) is never replaced by a fresh start.
  The saved binding stays, nothing about the ticket is sent to the role, and
  the Director is told once with the ways out it already has: defer and
  re-admit to retry, or start the ticket on another implementer.
"""

from __future__ import annotations

import json
import time
from typing import Any

#: How long a clear may wait for the SessionStart that proves its session.
BIND_SECONDS = 120.0
#: The board records a clear just AFTER sending it (so it never records one that
#: did not happen); a provider can announce the new session within that moment.
CLEAR_SKEW_SECONDS = 2.0
#: How long a resume may wait, after its confirmation prompt, before it is reported as failed.
CONFIRM_SECONDS = 120.0
#: How long a resume may wait for its SessionStart before the confirmation prompt is sent.
RESUME_WAIT_SECONDS = 15.0
#: Providers with a verified in-session resume that takes a session id.
RESUME_COMMANDS = {"claude": "/resume {session_id}"}
#: Sent at most once per attempt, only to draw out a deferred SessionStart: no ticket, no work.
CONFIRM_PROMPT = ("Board check, no action needed: reply with just \"ok\". Do not start, resume or continue any "
                  "work and do not run tools; the board sends your next instructions itself.")
#: How long a restored author may take to return its worktree to the ticket's checkout before the hand-off parks.
PREPARE_SECONDS = 900.0
RESTORED_NOTE = ("Board: your saved conversation {session_id} for {ticket} is restored{why}, and worktree {cwd} is at "
                 "{ticket}'s checkout {head} ({source}).")
#: Sent once, into the proven conversation, when its worktree is known to be elsewhere: a preparation, not the work.
PREPARE_PROMPT = ("Board: before {ticket} is handed back to you, worktree {cwd} must be at {ticket}'s checkout {head} "
                  "({source}); it is now at {current}. Return HEAD to {head} without losing anything: keep or commit any "
                  "uncommitted changes first, and never reset, clean or drop a stash. Do nothing else for {ticket} yet; the "
                  "board hands it over when it sees that checkout at the end of your turn.")


def _text(value: Any) -> Any:
    """The listener's connection hands text back as bytes; everything here is text."""
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray, memoryview)) and not isinstance(value, str) \
        else value


def _json(value: Any) -> Any:
    value = _text(bytes(value) if isinstance(value, memoryview) else value)
    return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value


def _rows(conn: Any, statement: str, params: tuple = ()) -> list[Any]:
    result = conn.execute(statement, params)
    return list(result.fetchall()) if result is not None and hasattr(result, "fetchall") else []


def _scalar(conn: Any, statement: str, params: tuple = ()) -> Any:
    row = conn.execute(statement, params).fetchone()
    if row is None:
        return None
    return _json(next(iter(row.values())) if isinstance(row, dict) else row[0])


def _epoch(value: Any) -> float:
    return value.timestamp() if hasattr(value, "timestamp") else float(value)


def last_session_start(gate: Any, role: str) -> dict | None:
    target = gate.role_targets.get(role) if gate is not None else None
    state = gate.state_store.read(target) if target else None
    start = getattr(state, "last_session_start", None)
    return start if isinstance(start, dict) and start.get("session_id") else None


def _other_process(gate: Any, role: str, start: dict) -> bool:
    """Whether a SessionStart came from anything but the role's registered provider process."""
    identity = (getattr(gate, "role_identities", None) or {}).get(role)
    if identity is None:
        return False  # nothing registered to compare with: the gate's own liveness rules apply
    return (start.get("pane_pid"), start.get("pane_start_time")) != (identity.pid, identity.start_time)


def _worktree(start: dict | None) -> str:
    return str(((start or {}).get("checkout") or {}).get("cwd") or "")


def bind_pass(listener: Any, conn: Any, gate: Any, now: float) -> None:
    """Bind each new ticket to the session its clear started, when that is proven."""
    for row in _rows(conn, "SELECT * FROM ticket_board.pending_ticket_contexts()"):
        ticket, role, cleared_at = (row["ticket_id"], row["role"], row["cleared_at"]) if isinstance(row, dict) else row
        ticket, role = _text(ticket), _text(role)
        cleared = _epoch(cleared_at)
        start = last_session_start(gate, role)
        runtime = listener.role_runtimes.get(role, "")
        fresh = bool(start) and float(start.get("at", 0)) > cleared - CLEAR_SKEW_SECONDS
        if fresh and start.get("source") == "clear" and not _other_process(gate, role, start) \
                and not _bound_elsewhere(conn, role, start["session_id"]):
            evidence = {"session_start": start, "cleared_at": cleared}
            conn.execute("SELECT ticket_board.record_ticket_context(%s, %s, %s, %s, %s::jsonb)",
                         (ticket, role, runtime, start["session_id"], json.dumps(evidence)))
        elif now - cleared > BIND_SECONDS:
            reason = "no SessionStart after the clear" if not fresh \
                else f"the SessionStart after the clear reported source {start.get('source')!r}" \
                if start.get("source") != "clear" else "the SessionStart after the clear was not from its registered process"
            conn.execute("SELECT ticket_board.record_ticket_context(%s, %s, %s, NULL, %s::jsonb)",
                         (ticket, role, runtime, json.dumps({"reason": reason, "cleared_at": cleared})))


def _bound_elsewhere(conn: Any, role: str, session_id: str) -> bool:
    """A session already proven to be another ticket's is never relabelled as this one's."""
    return bool(_scalar(conn, "SELECT count(*) FROM ticket_board.ticket_role_contexts WHERE role=%s AND session_id=%s",
                        (role, session_id)))


def _resolve(conn: Any, attempt_id: int, outcome: str, detail: dict) -> bool:
    return bool(_scalar(conn, "SELECT ticket_board.resolve_ticket_context_restore(%s, %s, %s::jsonb)",
                        (attempt_id, outcome, json.dumps(detail))))


def _status(conn: Any, ticket: str, role: str) -> dict:
    return _scalar(conn, "SELECT ticket_board.ticket_context_status(%s, %s)", (ticket, role)) or {}


def judge(listener: Any, conn: Any, gate: Any, attempt: dict, context: dict | None, now: float) -> str:
    """One open attempt: "confirmed", "failed" (parked, Director told) or "pending" (the prompt sent at most once).

    Confirmed only by a SessionStart after the send, from the registered
    provider process, with source `resume`, the saved id and the ticket's own
    worktree. Anything else after the send disproves it.
    """
    sent = _epoch_iso(attempt["sent_at"])
    role = attempt["role"]
    start = last_session_start(gate, role)
    if start and float(start.get("at", 0)) > sent:
        bound_tree = _worktree((context or {}).get("evidence", {}).get("session_start"))
        if start.get("session_id") != attempt["session_id"]:
            reason = f"the pane reported session {start.get('session_id')} ({start.get('source')}) instead"
        elif start.get("source") != "resume":
            reason = f"session {start.get('session_id')} started with source {start.get('source')!r}, not 'resume'"
        elif _other_process(gate, role, start):
            reason = "the SessionStart came from another provider process than the registered one"
        elif not bound_tree or not _worktree(start):
            # Missing evidence is not proof: both sides must name the worktree.
            reason = ("no worktree evidence: the ticket's binding records none" if not bound_tree
                      else "no worktree evidence: the resumed SessionStart reported none")
        elif _worktree(start) != bound_tree:
            reason = f"it resumed in {_worktree(start)}, not the ticket's worktree {bound_tree}"
        elif _resolve(conn, attempt["id"], "confirmed", {"session_start": start}):
            if attempt["reason"] == "restart":
                _restart_ready(listener, conn, gate, attempt, start)
            return "confirmed"
        else:
            return "pending"  # another pass resolved it first
        _resolve(conn, attempt["id"], "failed", {"reason": reason, "session_start": start})
        return "failed"
    probe_at = (attempt.get("detail") or {}).get("probe_sent_at")
    if probe_at is None:
        if now - sent > RESUME_WAIT_SECONDS and _idle(gate, role) and \
                _scalar(conn, "SELECT ticket_board.note_ticket_context_probe(%s)", (attempt["id"],)):
            _tell(listener, gate, role, CONFIRM_PROMPT)
        return "pending"
    if now - float(probe_at) > CONFIRM_SECONDS:
        _resolve(conn, attempt["id"], "failed", {"reason": "no SessionStart confirmed the resume"})
        return "failed"
    return "pending"


def _idle(gate: Any, role: str) -> bool:
    target = gate.role_targets.get(role)
    return bool(target) and not gate.eligibility_busy(target) and role not in gate.background_work_roles([role])


def confirm_pass(listener: Any, conn: Any, gate: Any, now: float) -> None:
    """Judge each open restore, whether or not a hand-off is waiting on it."""
    for attempt in _rows(conn, "SELECT ticket_board.open_ticket_context_restores() AS a"):
        attempt = _json(attempt["a"] if isinstance(attempt, dict) else attempt[0])
        judge(listener, conn, gate, attempt, _status(conn, attempt["ticket_id"], attempt["role"]).get("context"), now)


def _epoch_iso(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime
    return datetime.fromisoformat(str(value)).timestamp()


def _tell(listener: Any, gate: Any, role: str, text: str) -> None:
    target = gate.role_targets.get(role) if gate is not None else None
    if target:
        try:
            listener.sender(target, text)
        except Exception as exc:  # logged; an unanswered prompt ends in the bounded failure
            listener.logger.warning("Could not send %s the board's context message: %s", role, exc)


def current_checkout(gate: Any, role: str) -> dict:
    """The newest checkout the hook reported: at a SessionStart or at a turn end, whichever came last."""
    target = gate.role_targets.get(role) if gate is not None else None
    state = gate.state_store.read(target) if target else None
    seen = [record for record in ((getattr(state, "last_session_start", None) or {}).get("checkout"),
                                  getattr(state, "last_checkout", None)) if isinstance(record, dict)]
    stamped = [dict(record, at=record.get("at", (getattr(state, "last_session_start", None) or {}).get("at", 0)))
               for record in seen]
    return max(stamped, key=lambda record: float(record.get("at") or 0), default={})


def expected_checkout(status: dict) -> tuple[str, str]:
    """The commit a returned ticket's worktree must be at, and where that comes from -- recorded evidence only."""
    topic = status.get("topic") or {}
    if topic.get("commit"):
        return topic["commit"], f"its last publication {topic.get('ref')}"
    left = status.get("left_checkout") or {}
    if left.get("head"):
        return left["head"], f"the checkout recorded when it was left{', branch ' + left['branch'] if left.get('branch') else ''}"
    return "", ""


def _set_checkout(conn: Any, attempt_id: int, state: str, detail: dict) -> bool:
    return bool(_scalar(conn, "SELECT ticket_board.set_ticket_context_checkout(%s, %s, %s::jsonb)",
                        (attempt_id, state, json.dumps(detail))))


def _restart_ready(listener: Any, conn: Any, gate: Any, attempt: dict, start: dict) -> None:
    """A process restart does not move HEAD: ready only if the new process reports the checkout the last turn ended at."""
    before, now_at = getattr(gate.state_store.read(gate.role_targets.get(attempt["role"])), "last_checkout", None) or {}, \
        (start.get("checkout") or {})
    if before.get("head") and before.get("cwd") == now_at.get("cwd") and before.get("head") == now_at.get("head"):
        if _set_checkout(conn, attempt["id"], "ready", {"expected": before, "current": now_at, "source": "unchanged by a restart"}):
            _tell(listener, gate, attempt["role"], RESTORED_NOTE.format(
                session_id=attempt["session_id"], ticket=attempt["ticket_id"], why=" after its provider restarted",
                cwd=now_at["cwd"], head=now_at["head"], source="where its last turn ended") + f" Continue {attempt['ticket_id']}.")
    else:
        _set_checkout(conn, attempt["id"], "failed", {"reason": f"after the restart it reports {now_at.get('head') or 'no HEAD'}, "
                                                                f"its last turn ended at {before.get('head') or 'an unrecorded checkout'}"})


def checkout_ready(listener: Any, conn: Any, gate: Any, attempt: dict, status: dict, now: float) -> str:
    """A restored rework's worktree: "ready" (proved at the expected commit), "wait" (asked once to return), or "failed"."""
    role, ticket = attempt["role"], attempt["ticket_id"]
    head, source = expected_checkout(status)
    tree = _worktree(((status.get("context") or {}).get("evidence") or {}).get("session_start"))
    if not head:
        _set_checkout(conn, attempt["id"], "failed", {"reason": "no checkout is recorded for it: no publication, and none when it was left"})
        return "failed"
    current = current_checkout(gate, role)
    if current.get("cwd") == tree and current.get("head") == head:
        if _set_checkout(conn, attempt["id"], "ready", {"expected": head, "source": source, "current": current}):
            _tell(listener, gate, role, RESTORED_NOTE.format(session_id=attempt["session_id"], ticket=ticket, why="",
                                                             cwd=tree, head=head, source=source))
        return "ready"
    if attempt.get("checkout_state") is None:
        here = f"{current.get('head') or 'an unreported HEAD'}{' in ' + current['cwd'] if current.get('cwd') and current['cwd'] != tree else ''}"
        if _set_checkout(conn, attempt["id"], "preparing", {"expected": head, "source": source, "current": current}):
            _tell(listener, gate, role, PREPARE_PROMPT.format(ticket=ticket, cwd=tree, head=head, source=source, current=here))
        return "wait"
    if now - _epoch_iso(attempt["checkout_at"]) > PREPARE_SECONDS:
        where = f"{current.get('head') or 'an unreported HEAD'}" + (f" in {current.get('cwd') or 'an unreported worktree'}"
                                                                    if current.get("cwd") != tree else "")
        _set_checkout(conn, attempt["id"], "failed", {"reason": f"it is still at {where}, not {head} in {tree} ({source})",
                                                      "current": current})
        return "failed"
    return "wait"


def repair_pass(listener: Any, conn: Any, gate: Any, now: float) -> None:
    """A provider restarted mid-ticket comes back in its launch session: put it back, once, when idle."""
    for row in _rows(conn, "SELECT ticket_board.held_ticket_contexts() AS c"):
        context = _json(row["c"] if isinstance(row, dict) else row[0])
        role, ticket, session = context["role"], context["ticket_id"], context["session_id"]
        start = last_session_start(gate, role)
        if not start or start.get("session_id") == session or float(start.get("at", 0)) <= _epoch_iso(context["recorded_at"]):
            continue
        if not _idle(gate, role) or _other_process(gate, role, start):
            continue  # not a safe boundary yet, or the new process is not registered yet
        last = _scalar(conn, "SELECT max(sent_at) FROM ticket_board.ticket_context_restores WHERE ticket_id=%s AND role=%s",
                       (ticket, role))
        if last is not None and float(start.get("at", 0)) <= _epoch(last) + CLEAR_SKEW_SECONDS:
            continue  # this SessionStart answers our own attempt, not a new restart: never retried
        reason = why_not_restorable(listener, role, context)
        if reason:
            _park(conn, ticket, role, session, "restart", reason)
        elif _restore(listener, conn, gate, ticket, role, context, reason_kind="restart", observed=start) is None:
            continue


def _park(conn: Any, ticket: str, role: str, session: str | None, reason_kind: str, reason: str) -> None:
    """A restore that cannot even be attempted: one failed attempt, so the hand-off parks and the Director is told once."""
    opened = _scalar(conn, "SELECT ticket_board.open_ticket_context_restore(%s, %s, %s, %s, '{}'::jsonb)",
                     (ticket, role, session, reason_kind))
    _resolve(conn, opened["attempt"]["id"], "failed", {"reason": reason})


def _restore(listener: Any, conn: Any, gate: Any, ticket: str, role: str, context: dict | None,
             *, reason_kind: str, observed: dict | None) -> dict | None:
    """Open the one attempt for (ticket, role) and send the resume; None when the send failed (then parked)."""
    runtime = listener.role_runtimes.get(role, "")
    session = (context or {}).get("session_id")
    opened = _scalar(conn, "SELECT ticket_board.open_ticket_context_restore(%s, %s, %s, %s, %s::jsonb)",
                     (ticket, role, session, reason_kind, json.dumps({"observed": observed})))
    attempt = opened["attempt"]
    if not opened.get("opened") or attempt.get("outcome"):
        return attempt  # already sent: never twice
    try:
        listener.sender(gate.role_targets[role], RESUME_COMMANDS[runtime].format(session_id=session))
    except Exception as exc:
        _resolve(conn, attempt["id"], "failed", {"reason": f"the resume could not be sent: {exc}"})
        return None
    return attempt


def why_not_restorable(listener: Any, role: str, context: dict | None) -> str:
    runtime = listener.role_runtimes.get(role, "")
    if context is None:
        return "no conversation was recorded for it"
    if context.get("state") != "bound":
        return "its conversation was never confirmed"
    if context.get("runtime") != runtime:
        return f"it was worked on {context.get('runtime') or 'another runtime'}, and {role} now runs {runtime}"
    if runtime not in RESUME_COMMANDS:
        return f"{runtime or 'this runtime'} has no verified in-session resume"
    return ""


def before_delivery(listener: Any, conn: Any, gate: Any, *, ticket_id: str, role: str, now: float,
                    handoff: bool) -> str:
    """For a notice of a ticket to its pulled author: "deliver", "wait", "hold" or "park".

    "hold" is a notice that is not the hand-off, about a ticket whose
    conversation the pane does not hold: it waits for the hand-off's restore.
    "park" is a ticket whose restore failed: the notice is not sent at all.
    "deliver" after a restore comes with the board's restored note.
    """
    status = _status(conn, ticket_id, role)
    if status.get("parked"):
        return "park"
    context, attempt, last = status.get("context"), status.get("open"), status.get("last") or {}
    # A failed checkout the Director has since moved the ticket past is retried from the conversation up.
    retry = last.get("outcome") == "confirmed" and last.get("checkout_state") == "failed"
    if not needs_action(status, gate, role):
        return "deliver"
    if not handoff:
        return "hold"
    if attempt is None and (status.get("superseded") or retry or _displaced(gate, role, context)):
        kind = "rework" if status.get("superseded") or retry else "restart"
        reason = why_not_restorable(listener, role, context)
        if reason:
            _park(conn, ticket_id, role, (context or {}).get("session_id"), kind, reason)
            return "park"
        attempt = _restore(listener, conn, gate, ticket_id, role, context, reason_kind=kind,
                           observed=last_session_start(gate, role))
        return "wait" if attempt is not None else "park"
    if attempt is not None:
        verdict = judge(listener, conn, gate, attempt, context, now)
        if verdict != "confirmed":
            return "park" if verdict == "failed" else "wait"
        status = _status(conn, ticket_id, role)
        last = status.get("last") or {}
    # The conversation is proven; the work goes only to a worktree proved at the ticket's checkout.
    if last.get("checkout_state") == "ready":
        readiness = "ready"
    elif last.get("checkout_state") == "failed":
        return "park"
    else:
        readiness = checkout_ready(listener, conn, gate, last, status, now)
    if readiness == "ready":
        conn.execute("SELECT ticket_board.mark_ticket_context_delivered(%s)", (last["id"],))
        return "deliver"
    return "park" if readiness == "failed" else "wait"


def needs_action(status: dict, gate: Any, role: str) -> bool:
    """Whether a notice of this ticket to this role may NOT simply be delivered: parked, a conversation the pane does
    not hold (superseded, an open attempt, displaced), or a proven conversation whose checkout is not ready yet."""
    last = status.get("last") or {}
    return bool(status.get("parked") or status.get("superseded") or status.get("open")
                or _displaced(gate, role, status.get("context"))
                or (last.get("outcome") == "confirmed" and last.get("checkout_state") != "ready"))


def _displaced(gate: Any, role: str, context: dict | None) -> bool:
    """A bound ticket whose pane has since announced another session (a restarted provider, a manual resume)."""
    start = last_session_start(gate, role)
    return bool(context and context.get("session_id") and start and start.get("session_id") != context["session_id"]
                and float(start.get("at", 0)) > _epoch_iso(context["recorded_at"]))


def passes(listener: Any, conn: Any, gate: Any) -> None:
    """The per-pass work, under a pull policy only; every failure is logged and never stops the pass."""
    now = time.time()
    for step in (bind_pass, confirm_pass, repair_pass):
        try:
            step(listener, conn, gate, now)
        except Exception as exc:
            listener.logger.warning("Ticket context %s failed: %s", step.__name__, exc)
