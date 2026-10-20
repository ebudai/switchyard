"""A pane whose hook says `blocked` is waiting on somebody, whatever wrote it (SYRD-633).

The activity gate reads a `blocked` hook as busy and holds every notice behind
it, which is right: a notice typed into a question ends in Enter, and Enter
answers it. What was wrong was who heard about it. On syrd, 2026-10-20, App's
response was stopped by its provider's safeguards and its hook then wrote
`blocked` from `claude.Notification.permission_prompt`; nothing replaced it for
ten hours, and SYRD-629's activation waited behind it 111 times.

SYRD-234's generator did raise one escalation -- on SYRD-624, the lowest id App
held, which was blocked on another ticket. Escalation eligibility refuses a
ticket with unresolved blockers, so it was dropped as stale before the Director
saw it, and its dedupe key, per role and blocked instant, then kept every later
pass from saying anything about SYRD-629. The gate's trace kept only
`hook_blocked`: no source, no age, and -- because a blocked hook returns before
the child probe -- not the two finished-ticket VMs still running under the pane.

Here every blocked pane is one episode, kept by the board (pgu1001), whatever
its source:

* **What it is.** `read` takes the hook's own record -- its source, when it was
  written, the session it carries -- and classifies it: a `permission_prompt`
  from a source known to raise one; `restored`, a record written before the
  provider now in the pane started or carrying another process's session, so it
  says nothing about the live provider; or `unknown_source`. It also lists the
  pane's detached children from one process-table sample, as SYRD-583
  describes them, so a blocked hook no longer hides them.
* **Who is told, and when.** `process` reports every role it could read to
  `ticket_board.note_blocked_panes` on each pass. The board opens one episode
  per role and blocked instant and, once the pane has said blocked for the
  configured bound, tells the Director once, on the held notice's own ticket if
  one is waiting and otherwise on the role's active ticket. The alert is a kind
  only the Director is told, so another ticket's blockers can never drop it.
* **When it ends.** When the role's hook says anything else, or `blocked` from
  a later instant, the episode closes; an alert not yet sent is withdrawn.

Nothing is typed into a blocked pane, nothing is stopped, and the worker is not
nudged: the held notices go out through the ordinary gate once the pane's own
hook says it is free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import background_child, session_context

HOOK_BLOCKED = "hook_blocked"
PERMISSION_PROMPT = "permission_prompt"
RESTORED = "restored"
UNKNOWN_SOURCE = "unknown_source"


def _iso(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat() if epoch else ""


@dataclass(frozen=True)
class Blocked:
    """One pane's `blocked` record, as the hook wrote it and the gate can check it."""

    role: str
    target: str
    source: str
    since: float
    classification: str
    #: Why it was classified as it was, in words, for the trace and the Director.
    why: str = ""
    #: The session the record carries (its pane's latest SessionStart), if any.
    session: str = ""
    #: When the process now in the pane started; None when that is unknown.
    provider_started_at: float | None = None
    holders: tuple[background_child.Holder, ...] = ()

    def as_detail(self) -> dict[str, Any]:
        return {"source": self.source, "since": _iso(self.since), "classification": self.classification,
                "why": self.why, "session": self.session, "provider_started_at": _iso(self.provider_started_at),
                "holders": [holder.as_detail() for holder in self.holders]}


def _classify(gate: Any, role: str, state: Any, started: float | None) -> tuple[str, str]:
    from . import notify_listener as listener

    start = state.last_session_start if isinstance(state.last_session_start, dict) else None
    if started is not None and state.updated_at < started:
        return RESTORED, (f"it was written at {_iso(state.updated_at)}, before the process now in the pane started "
                          f"({_iso(started)}), so it says nothing about the live provider")
    if start is not None and session_context._other_process(gate, role, start):
        return RESTORED, (f"it carries session {start.get('session_id')}, which another process started, not the "
                          f"provider registered for {role}")
    if state.source in listener.PERMISSION_PROMPT_BLOCK_SOURCES:
        return PERMISSION_PROMPT, "its CLI raised a permission prompt that nothing answered"
    return UNKNOWN_SOURCE, "the board does not know that source as one that raises a permission prompt"


def holders(gate: Any, target: str, turn_started_at: float | None = None) -> tuple[background_child.Holder, ...]:
    """The pane's detached children, from one sample: what SYRD-583 would name if the hook said idle.

    One started before the turn the pane is blocked in is a finished turn's.
    """
    pane_pid = gate._pane_pid(target)
    if pane_pid is None:
        return ()
    sample = gate._child_work_sample(pane_pid)
    if not sample.observed or not sample.detached:
        return ()
    finished = frozenset(pid for pid, started in sample.starts if turn_started_at and 0 < started < turn_started_at)
    return background_child.describe(sample, sample.detached, finished, proc_root=Path(gate.proc_root))


def read(gate: Any, target: str, role: str | None = None) -> Blocked | None:
    """The pane's blocked record, classified, with its children; None when its hook does not say blocked."""
    state = gate.state_store.read(target)
    if state is None or state.state != "blocked":
        return None
    role = role or gate._role_for_target(target)
    started = gate.provider_started_at(target)
    classification, why = _classify(gate, role, state, started)
    start = state.last_session_start if isinstance(state.last_session_start, dict) else {}
    return Blocked(role, target, state.source, state.updated_at, classification, why,
                   str(start.get("session_id") or ""), started, holders(gate, target, state.turn_started_at))


def _screen(gate: Any, target: str) -> str:
    """What the pane shows, as far as it bears on the record: a question, or an empty ready prompt."""
    try:
        composing = gate._target_cursor_state(target)
        prompt = gate._open_prompt_by_target.get(target)
        snapshot = gate.composer_snapshot(target)
    except Exception:  # what the screen shows is evidence, never a reason the pass fails
        return ""
    if prompt is not None:
        return f"Its screen shows its CLI's own question ({prompt.reason})."
    if composing is False and snapshot.available and snapshot.marker_found and not snapshot.content_length:
        return ("Its screen shows its empty ready prompt and no question, so the record may be stale: a prompt "
                "can be dismissed, or a turn stopped by a provider error or safeguard, without a hook saying so.")
    return ""


def describe(conn: Any, gate: Any, blocked: Blocked) -> str:
    """What the Director is told the pane is, and the one safe way to release it."""
    runtime = gate.role_runtimes.get(blocked.role, "its CLI")
    lines = [f"It is classified {blocked.classification}: {blocked.why}."]
    if screen := _screen(gate, blocked.target):
        lines.append(screen)
    if blocked.holders:
        origins = background_child.origins(conn, blocked.holders)
        identity = getattr(gate, "role_identities", {}).get(blocked.role)
        lines.append("Under its pane: " + " ".join(
            background_child.sentence(holder, origins.get(holder.key), provider_pid=getattr(identity, "pid", None))
            for holder in blocked.holders))
    lines.append(
        f"Nothing was typed into the pane and nothing will be while its hook says blocked. Look at it first: "
        f"`tmux capture-pane -p -t {blocked.target}`. If it shows a question, answer it there. If it shows "
        f"{runtime}'s ready prompt, the record is stale: give {blocked.role} a prompt "
        f"(`directorctl send {blocked.target} <message>`) so its next turn writes a fresh hook state, or restart "
        f"{blocked.role}. The held notices go out by themselves once it is released.")
    return " ".join(lines)


def present(owner: Any, conn: Any) -> bool:
    """Whether this board keeps blocked-pane episodes: a listener can be newer than its board's migrations."""
    found = getattr(owner, "_blocked_pane_present", None)
    if found is None:
        try:
            row = conn.execute(
                "SELECT to_regprocedure('ticket_board.note_blocked_panes(jsonb, interval)') IS NOT NULL"
            ).fetchone()
        except Exception as exc:  # asked again next pass
            owner.logger.warning("Failed to check for blocked-pane episodes: %s", exc)
            return False
        value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else False
        found = value is True or value in ("t", b"t", 1)
        owner._blocked_pane_present = found
    return found


def run(listener: Any, conn: Any) -> int:
    """One pass, before the reminder generators: SYRD-234's permission-prompt one then leaves every blocked pane here."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "state_store") or not present(listener, conn):
        return 0
    listener.idle_nudges.blocked_panes_on_board = True
    return process(listener, conn, gate, float(listener.permission_prompt_grace_seconds))


def process(owner: Any, conn: Any, gate: Any, alert_after_seconds: float) -> int:
    """Report every pane this pass could read; the board opens, alerts and closes the episodes. Never fatal."""
    observed: list[str] = []
    blocked: dict[str, dict[str, Any]] = {}
    for role, target in sorted(gate.role_targets.items()):
        if role == "director":
            continue  # an alert about the Director's own pane would wait behind it
        try:
            found = read(gate, target, role)
            if found is not None:
                blocked[role] = {**found.as_detail(), "target": target, "what": describe(conn, gate, found)}
            observed.append(role)
        except Exception as exc:  # a pane that cannot be read is neither blocked nor released
            owner.logger.warning("Failed to read %s's pane for a blocked hook: %s", role, exc)
    try:
        row = conn.execute(
            "SELECT ticket_board.note_blocked_panes(%s::jsonb, %s::interval)",
            (json.dumps({"observed": observed, "blocked": blocked}, sort_keys=True, ensure_ascii=False),
             f"{alert_after_seconds:g} seconds"),
        ).fetchone()
    except Exception as exc:
        owner.logger.warning("Failed to note blocked panes: %s", exc)
        return 0
    value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else 0
    try:
        alerted = int(value)
    except (TypeError, ValueError):
        return 0
    if alerted:
        owner.logger.info("Told the director about %s blocked pane(s): %s", alerted, ", ".join(sorted(blocked)))
    return alerted
