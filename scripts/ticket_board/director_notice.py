"""The Director's ad hoc phone notification, through the host's phone bridge (SYRD-714).

`ticket-board-write notify-user --title T --message M [--ticket ID] [--urgent --reason R]` answers a preview: the
notification exactly as the phone will show it, and its digest. Nothing is recorded. The same command with
`--confirm <digest>` records it, for the host bridge to send as `director_ad_hoc` with every other User event --
through the same outbox, retries, dead letters and filters. The Director never sees or supplies a provider
credential, never chooses a recipient, and never supplies a link: a link is built by the bridge from a ticket of
this board, and only from that. A notification without a ticket previews `link: none` and is sent with no link.

Who is asking is never what the request says:
- the request must come over the board's Unix socket under process authority, from a pane the kernel names
  (`SO_PEERCRED`) and the board resolves to its registered runtime -- a header, the HTTP port or a detached shell
  is no Director;
- the database checks again that this process is the control role's registered live pane, and records it.

The text is refused, not cleaned, when it holds a control character, a URL, something shaped like a credential (a
write token or a piece of one included), or another board's ticket (`phone_text.ad_hoc_problem`). The database holds the rate:
5 an hour, 1 urgent, which needs a reason; whether an urgent one may break through the phone's quiet hours is the
host operator's separate decision (`director_urgent` in the bridge's configuration). A notification is advisory:
it writes no comment, moves no ticket, clears no blocker and acknowledges nothing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import phone_text

OPERATIONS = frozenset({"notify_user"})
FIELDS = frozenset({"title", "message", "ticket", "urgent", "reason", "confirm"})


def _calling_process(handler: Any) -> tuple[int, int, int]:
    """(pid, start time, uid) of the registered pane making this request, as the kernel names it."""
    authority = getattr(handler, "role_authority", None)
    if handler is None or authority is None or not hasattr(authority, "session_for_peer"):
        raise PermissionError("an ad hoc phone notification is sent only by the Director's own pane, over the board's "
                              "socket under process authority")
    # The caller's role was resolved from this same live session's registration; the database checks it again.
    credentials = handler.require_allowed_peer()
    identity = authority.session_for_peer(credentials)
    return identity.pid, identity.start_time, credentials.uid


def preview(project: str, prefix: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The notification as the phone will show it, and its digest; raises ValueError naming what may not be sent."""
    unknown = set(payload) - FIELDS
    if unknown:
        raise ValueError(f"notify_user does not take {', '.join(sorted(unknown))}")
    title, message, reason = payload.get("title"), payload.get("message"), payload.get("reason", "")
    ticket, urgent = payload.get("ticket") or "", payload.get("urgent", False)
    if type(urgent) is not bool:
        raise ValueError("urgent is true or false")
    problems = [phone_text.ad_hoc_problem("title", title, phone_text.AD_HOC_TITLE_LIMIT, prefix),
                phone_text.ad_hoc_problem("message", message, phone_text.AD_HOC_MESSAGE_LIMIT, prefix),
                phone_text.ad_hoc_problem("reason", reason, phone_text.AD_HOC_REASON_LIMIT, prefix, required=urgent)]
    if not isinstance(ticket, str) or (ticket and not phone_text.TICKET.fullmatch(ticket)) or (
            ticket and ticket.split("-", 1)[0] != prefix):
        problems.append(f"ticket must be one of this board's {prefix}-N tickets")
    if reason and not urgent:
        problems.append("a reason is for an urgent notification only")
    problem = next((p for p in problems if p), "")
    if problem:
        raise ValueError(f"not sent: {problem}")
    heading, body = phone_text.ad_hoc_message(project, title, message, ticket)
    shown = {"title": heading, "message": body, "ticket": ticket, "urgent": urgent, "reason": reason,
             "link": f"the board's page for {ticket}, over the tailnet, when the host serves it" if ticket else "none"}
    digest = hashlib.sha256(json.dumps({"project": project, **shown}, sort_keys=True).encode("utf-8")).hexdigest()
    return {"preview": shown, "digest": digest}


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str, ticket_id: str | None = None,
            handler: Any = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is not None:
        raise ValueError(f"{operation} is a board action, not a ticket's: name a ticket with ticket")
    pid, start_time, uid = _calling_process(handler)
    project, prefix = getattr(app, "project", ""), getattr(app, "ticket_prefix", "")
    shown = preview(project, prefix, payload)
    confirm = payload.get("confirm")
    if confirm in (None, ""):
        return {**shown, "recorded": False,
                "send": "the same request with confirm=<digest> sends exactly this; nothing was recorded"}
    if confirm != shown["digest"]:
        raise ValueError("not sent: this is not the notification that was previewed (its digest differs); preview "
                         "again")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(
                "SELECT ticket_board.record_director_notice(%s, %s, %s, %s, %s, %s, %s, %s, %s) AS result",
                (pid, start_time, uid, payload["title"], payload["message"], payload.get("ticket") or None,
                 shown["preview"]["urgent"], shown["preview"]["reason"], shown["digest"])).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    result = json.loads(result) if isinstance(result, str) else dict(result)
    return {**shown, "recorded": True, **result}
