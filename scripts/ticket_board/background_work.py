"""Whether a role that ended its turn is still working in the background (SYRD-538).

A Claude Code turn can end while a background subagent or a backgrounded
command keeps working, and the session is woken again when it finishes. The
pane's Stop hook then reads idle, and the unresolved-turn prompt fired at every
wake: five prompts in four minutes during one Explore job on SYRD-537, each one
costing the session a turn.

Claude's own Stop input says which it is: `background_tasks`, the in-flight
work registered in the session. The pane hook records that list with the pane
root that wrote it. It counts here only while it can still be true:

* the recorded pane root is the role's registered provider process, by pid
  AND start time, and that process is still running -- so a provider that died
  or was restarted, or a pid the kernel has reused, ends it;
* it is younger than a ceiling, so a hung job falls back to ordinary prompting
  and escalation instead of silencing them forever (SYRD-403's lesson);
* the hook state is still the turn end that recorded it -- any later hook write
  (a turn start, a Stop with nothing in flight) replaces it.

Nothing else counts: not a live descendant process, not a scheduled wakeup, and
not a turn end from a runtime whose hooks do not report background work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .peer_identity import PROC_ROOT, SessionIdentity, read_process

#: How long a turn end's background work can hold off unresolved-turn and idle
#: reminders. Long enough for this project's longest measured bounded runs
#: (mutation plans and suite comparisons finish inside SYRD-403's 3000 s
#: whole-run bound); a job still running after it is treated as idle work again.
DEFAULT_LIMIT_SECONDS = 3600.0
LIMIT_ENV = "TICKET_BOARD_BACKGROUND_WORK_LIMIT_SECONDS"


def limit_seconds(environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(LIMIT_ENV, "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_LIMIT_SECONDS
    except ValueError:
        return DEFAULT_LIMIT_SECONDS
    return value if value >= 0 else DEFAULT_LIMIT_SECONDS


@dataclass(frozen=True)
class BackgroundWork:
    """A turn end's in-flight background work, as the pane hook recorded it."""

    pane_pid: int
    pane_start_time: int
    session_id: str
    task_ids: tuple[str, ...]

    @classmethod
    def parse(cls, raw: Any) -> "BackgroundWork | None":
        if not isinstance(raw, dict):
            return None
        try:
            pid, start = int(raw.get("pane_pid")), int(raw.get("pane_start_time"))
        except (TypeError, ValueError):
            return None
        tasks = [task for task in raw.get("tasks") or () if isinstance(task, dict) and str(task.get("id") or "")]
        if pid <= 1 or start <= 0 or not tasks:
            return None
        return cls(pid, start, str(raw.get("session_id") or ""), tuple(str(task["id"]) for task in tasks))


def why_not_working(
    state: Any,
    registered: SessionIdentity | None,
    *,
    now: float,
    limit: float,
    proc_root: Path = PROC_ROOT,
) -> str:
    """"" when the state proves live background work; otherwise why it does not."""
    work = BackgroundWork.parse(getattr(state, "background_work", None))
    if state is None or state.state != "idle" or work is None:
        return "no background work recorded"
    if registered is None:
        return "no registered provider process"
    if (work.pane_pid, work.pane_start_time) != (registered.pid, registered.start_time):
        return "recorded by another provider process"
    process = read_process(registered.pid, proc_root=proc_root)
    if process is None or process.start_time != registered.start_time:
        return "the provider process is gone"
    if now - state.updated_at > limit:
        return "older than the background-work limit"
    return ""


#: Reminders about an owner that wait while that owner's background work is live.
DEFERRED_KINDS = frozenset({"unresolved_turn_repair", "idle_reminder", "unresolved_turn", "escalation"})


def reminder_owner(kind: str, target_role: str, payload: str) -> str:
    """The owner a queued reminder is about, or "" for anything that is not such a reminder.

    Initial assignments, transitions, comments, handoffs and permission-prompt
    escalations are not reminders about an idle owner, and are never held.
    """
    if kind not in DEFERRED_KINDS:
        return ""
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if kind in {"unresolved_turn_repair", "idle_reminder"}:
        return target_role
    if kind == "unresolved_turn" or (kind == "escalation" and "idle_since" in parsed):
        # A permission-prompt escalation carries neither owner_role nor
        # idle_since: a pane stopped on a prompt is waiting, not working.
        return str(parsed.get("owner_role") or "")
    return ""


def working_owner(gate: Any, kind: str, target_role: str, payload: str) -> str:
    """The owner a queued reminder is about, when the gate says that owner's background work is live; else ""."""
    owner = reminder_owner(kind, target_role, payload)
    working_roles = getattr(gate, "background_work_roles", None)
    if not owner or not callable(working_roles):
        return ""
    return owner if owner in working_roles([owner]) else ""
