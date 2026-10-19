"""What a launch of a fresh-per-ticket role resumes (SYRD-626).

`fresh_session_per_ticket` is a rule about the ticket boundary, and the notify
listener applies it there: a pane is cleared when a new ticket is handed to it
(SYRD-135), continued when the next ticket of the same package is (SYRD-626).
A launch is not a ticket boundary. MEFP's restart started every such worker
fresh only because it was configured that way (luna-1..8 and luna-10..15), and
started fresh again when no session was recorded locally (luna-9), discarding
the context of tickets they still held.

So before a fresh-per-ticket role starts, the board is asked what the role
holds (`/api/context-recovery/<role>`, ticket_board.role_context_recovery):

* an active ticket already handed over resumes that ticket's proven
  conversation -- the board's binding, even when the local record is missing
  or names another session; on a board without a pull policy, which binds no
  sessions, the role's own record;
* a continuation decided but not yet delivered resumes the conversation it
  continues;
* nothing handed over -- no active ticket, or one not yet delivered -- starts
  fresh, as before;
* a handed-over ticket whose conversation cannot be identified stops the
  start with one error naming the role, the ticket and what is missing,
  instead of erasing it.

A conversation on another runtime than the role now runs cannot be resumed:
that start is fresh and says so, and SYRD-540 parks the ticket's hand-off for
the Director. A board that cannot be asked never causes an erasure: what is
recorded is resumed, and only a role with nothing recorded starts fresh. A
role whose environment names no board keeps the old behaviour exactly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import parse, request

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig

TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Recovery:
    action: str  # "resume", "fresh" or "refuse"
    session_id: str = ""
    message: str = ""
    event: str = ""


def board_get(url: str) -> Any:
    """GET one board read; a seam the suites replace."""
    with request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def decide(role: RoleConfig, recorded: str, *, runtime: str) -> Recovery | None:
    """What this launch of `role` does, given the session its own record names (may be empty); None names no board."""
    board = str((role.env or {}).get("TICKET_BOARD_URL") or "").strip()
    if not board:
        return None
    why = ""
    try:
        answer = board_get(f"{board.rstrip('/')}/api/context-recovery/{parse.quote(role.role, safe='')}")
    except Exception as exc:  # unreachable, refused, an older board: the board cannot be asked
        answer, why = None, str(exc) or type(exc).__name__
    action = answer.get("action") if isinstance(answer, dict) else None
    if action not in ("resume", "fresh", "refuse", "resume_recorded"):
        why = why or f"it answered {answer!r}"
        if recorded:
            return Recovery("resume", recorded, event="recovery_unasked_resume", message=(
                f"team-launcher: could not ask the board whether {role.role} holds an active ticket ({why}); "
                f"resuming its recorded session {recorded} rather than discarding it"))
        return Recovery("fresh", event="recovery_unasked_fresh", message=(
            f"team-launcher: could not ask the board whether {role.role} holds an active ticket ({why}), "
            "and no session is recorded for it; starting a fresh session"))
    ticket, reason = str(answer.get("ticket") or ""), str(answer.get("reason") or "")
    if action == "fresh":
        return Recovery("fresh", event="fresh_per_ticket", message=(
            f"team-launcher: {role.role} is configured for fresh sessions per ticket and {reason}; "
            "starting a fresh session"))
    refusal = (f"team-launcher: not starting {role.role}: {{why}}. A fresh start would erase {ticket}'s conversation; "
               f"to go on without it, the Director can defer {ticket} and admit it again, or start it on another "
               "implementer")
    if action == "refuse":
        return Recovery("refuse", event="recovery_refused", message=refusal.format(why=reason))
    if action == "resume_recorded":
        if recorded:
            return Recovery("resume", recorded, event="recovery_resume", message=(
                f"team-launcher: {reason}; resuming its recorded session {recorded}"))
        return Recovery("refuse", event="recovery_refused", message=refusal.format(
            why=f"{reason}, and no session is recorded for it"))
    session = str(answer.get("session_id") or "")
    was_on = str(answer.get("runtime") or "")
    if not session:
        return Recovery("refuse", event="recovery_refused", message=refusal.format(why=f"{reason}, but no session id"))
    if was_on and runtime and was_on != runtime:
        return Recovery("fresh", event="recovery_runtime_changed", message=(
            f"team-launcher: {reason}, on {was_on}; {role.role} now runs {runtime}, which cannot resume it, so a fresh "
            f"session starts and the board parks {ticket}'s hand-off for the Director"))
    return Recovery("resume", session, event="recovery_resume", message=(
        f"team-launcher: {reason}; resuming it" + (f" (the local record named {recorded or 'none'})"
                                                   if recorded != session else "")))


def prepare(role: RoleConfig, session_dir: Path, *, entry: str) -> Recovery | None:
    """Decide, record the decision in the pane's resume history, and point the record at the session to resume.

    None: the role names no board, and its start goes on exactly as before this change.
    """
    from scripts import team_launcher as launcher
    from scripts.role_sessions import _decided

    recorded = launcher.session_id_for_role(role, session_dir)
    recovery = decide(role, recorded, runtime=launcher._role_cli_name(role))
    if recovery is None:
        return None
    _decided(role, session_dir, entry, recovery.event, recovery.message, recovery.session_id or recorded)
    if recovery.action == "resume" and recovery.session_id != recorded:
        launcher._write_private_json_atomic(
            session_dir / launcher.session_file_name(role.target),
            {"target": role.target, "session_id": recovery.session_id, "source": "board_context_recovery",
             "updated_at": time.time()},
        )
    return recovery
