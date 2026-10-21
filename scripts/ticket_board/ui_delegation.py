"""The User's confirmed board-UI action, with the control role's operation permission for that one action (SYRD-598).

The board UI used to act as the Director by saying so: its requests named
`director` in a header the HTTP server trusts. It now acts as the User, and when
the User confirms a concrete Director-only action, the server mints a grant for
exactly that action (`ticket_board.mint_ui_delegation`) and hands the page a
token. The action's own request carries the token; the database consumes the
grant in the transaction that performs the action, before it runs, and its
Director-only checks accept the User only for the operation that grant names.

"Came from the UI" rests on all of these, never on one of them alone:

* a board session the server issues with the page (an HttpOnly, SameSite=Strict
  cookie) and keeps in memory, so a restarted server knows none and every page
  must be reloaded;
* the request's Host and Origin naming this server, and Sec-Fetch-Site, when the
  browser sends it, saying same-origin -- which also stops cross-site requests;
* the caller being the User, over HTTP (never the Unix socket, whose callers are
  role panes);
* a single-use grant, bound to the session, the ticket, the operation, the exact
  payload and the ticket as the User saw it, which expires within minutes.

The decision stays the User's: the action runs with caller role `user`, so every
comment, field audit and "changed by" names the User, and the grant records that
the control role's operation permission was used through the board UI.
"""

from __future__ import annotations

import contextvars
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from typing import Any

from . import ui_user_identity

SESSION_COOKIE = "tb_ui_session"
DELEGATION_HEADER = "X-Ticket-Board-UI-Delegation"
#: How long a board page's session lasts without a reload.
SESSION_SECONDS = 12 * 3600.0
#: How long a confirmation stays usable; the database caps it at five minutes.
CONFIRMATION_SECONDS = 120

_pending: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ui_delegation", default=None)

#: Over HTTP a request acts as the User and nobody else: anyone who can reach the port can read its
#: write token, so a role named in a header proves nothing. Role panes act through the board's Unix
#: socket, where the kernel says who is calling. Only in-process test fixtures, which stand in for those
#: panes, set this; no request, environment or configuration can.
TRUST_HEADER_ROLES = False


class UiSessions:
    """The board pages this server process rendered. A restart forgets them all."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._expires: dict[str, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._expires = {k: v for k, v in self._expires.items() if v > now}
            self._expires[token] = now + SESSION_SECONDS
        return token

    def valid(self, token: str) -> bool:
        with self._lock:
            expires = self._expires.get(token)
        return expires is not None and expires > time.monotonic()


def _sessions(server: Any) -> UiSessions:
    sessions = getattr(server, "ui_sessions", None)
    if sessions is None:
        sessions = server.ui_sessions = UiSessions()
    return sessions


def issue_session(handler: Any) -> None:
    """With the board page: the session its confirmations are bound to. Over HTTP only, and only to a
    connection the kernel says a board UI user owns: anyone can fetch the page, but not as the User."""
    if handler.role_authority is None and not ui_user_identity.refusal(handler.connection):
        token = _sessions(handler.server).issue()
        handler.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")


def _session(handler: Any) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(handler.headers.get("Cookie", ""))
    except Exception:  # a malformed header is no session
        cookie = SimpleCookie()
    morsel = cookie.get(SESSION_COOKIE)
    token = morsel.value if morsel is not None else ""
    if not token or not _sessions(handler.server).valid(token):
        raise PermissionError("this board page's session is unknown or expired; reload the board and confirm again")
    return token


def _from_the_user(handler: Any) -> None:
    """The board page, as the User: this board's own origin, from a connection a board UI user owns."""
    _same_origin(handler)
    why = ui_user_identity.refusal(handler.connection)
    if why:
        raise PermissionError(f"a board UI confirmation is the User's own: {why}")


def _same_origin(handler: Any) -> None:
    if handler.role_authority is not None:
        raise PermissionError("board UI confirmations are only for the board page over HTTP")
    host = handler.headers.get("Host", "").strip().lower()
    port = handler.server.server_address[1]
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        raise PermissionError("a board UI confirmation must come from this board's own page")
    if handler.headers.get("Origin", "").strip().lower() != f"http://{host}":
        raise PermissionError("a board UI confirmation must come from this board's own page")
    fetch_site = handler.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site.strip().lower() != "same-origin":
        raise PermissionError("a board UI confirmation must come from this board's own page")


def http_role(handler: Any, role: str) -> str:
    """The role an HTTP request acts as: the User, whatever else its header names."""
    if role != "user" and not TRUST_HEADER_ROLES:
        raise PermissionError(f"over HTTP a request acts as the User, not {role}; role panes act through the "
                              "board's Unix socket")
    return role


def control_role(app: Any) -> str:
    """The role whose operation permission a confirmed board-UI action uses."""
    from .workflow_config import DIRECTOR_IDENTIFYING_CAPABILITIES

    cfg = app.workflow_configuration()
    if not cfg:
        return "director"
    roles = sorted(r["name"] for r in cfg["roles"]
                   if r.get("active") and DIRECTOR_IDENTIFYING_CAPABILITIES <= set(r.get("capabilities") or []))
    if not roles:
        raise PermissionError("this board's workflow declares no control role to act for")
    return roles[0]


class ConfirmationRefused(PermissionError):
    """A board UI confirmation the board refused: the message is the board's own first line, fit for the page."""


def _refused(exc: Exception) -> Exception:
    """The board's refusal as one clear line; anything that is not a refusal is left as it was."""
    if getattr(exc, "sqlstate", None) in {"42501", "40001", "22023"}:
        return ConfirmationRefused(str(exc).splitlines()[0])
    return exc


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray, memoryview)) else str(value)


def mint(handler: Any, request: dict[str, Any]) -> dict[str, Any]:
    """POST /api/ui/delegations: the User confirmed a concrete action; a single-use token for it."""
    _same_origin(handler)
    if handler.caller_role() != "user":
        raise PermissionError("only the User confirms board UI actions")
    if set(request) - {"ticket_id", "operation", "payload", "ticket_version", "prepare", "describe"}:
        raise ValueError("a board UI confirmation takes ticket_id, operation, payload and ticket_version")
    ticket_id = str(request.get("ticket_id", "")).strip().upper()
    if request.get("describe") is True and ticket_id:
        return describe(handler, ticket_id)  # what anyone may read; lent operations only for the User
    _from_the_user(handler)
    session = _session(handler)
    operation = str(request.get("operation", "")).strip()
    payload = request.get("payload")
    if not ticket_id or not operation or not isinstance(payload, dict):
        raise ValueError("a board UI confirmation needs a ticket, an operation and its payload")
    authority = control_role(handler.app)
    # The control role must be able to do it now; the database checks the same, and what it may lend.
    try:
        handler.require_operation_allowed(operation, authority, ticket_id)
    except ValueError as exc:  # an operation this board does not know is not one it can lend
        raise ConfirmationRefused(str(exc)) from exc
    if request.get("prepare") is True:
        # What the confirmation shows: the ticket as it is now, and the version a grant will be bound to.
        ticket = handler.app.get_ticket(ticket_id)
        prepared = {"ticket_id": ticket_id, "operation": operation, "payload": payload, "authority": authority,
                    "title": ticket.get("title", ""), "state": ticket.get("state", ""),
                    "assignee": ticket.get("assignee", ""), "ticket_version": ticket_version(handler.app, ticket_id)}
        if operation == "merge":  # both tickets, by ID and title, are what the User confirms
            target = handler.app.get_ticket(str(payload.get("target_id", "")).strip().upper())
            prepared["target"] = {"ticket_id": target.get("id", ""), "title": target.get("title", ""),
                                  "state": target.get("state", "")}
        return prepared
    version = str(request.get("ticket_version", "")).strip()
    if not version:
        raise ValueError("a board UI confirmation needs the ticket version it was shown")
    token = secrets.token_urlsafe(32)
    app = handler.app
    try:
        with app._pg_connect() as conn:
            with conn.transaction():
                app._pg_set_caller_role(conn, "user")
                row = conn.execute(
                    "SELECT ticket_board.mint_ui_delegation(%s, %s, %s, %s, %s::jsonb, %s, %s) AS expires_at",
                    (token, session, ticket_id, operation, json.dumps(payload, sort_keys=True), version, CONFIRMATION_SECONDS),
                ).fetchone()
    except Exception as exc:
        raise _refused(exc) from exc
    expires = row["expires_at"] if isinstance(row, dict) else row[0]
    return {"token": token, "ticket_id": ticket_id, "operation": operation, "authority": authority,
            "expires_at": expires.isoformat() if hasattr(expires, "isoformat") else _text(expires)}


def _field(name: str, kind: str, label: str, *, value: Any = None, options: list | None = None,
           required: bool = False) -> dict[str, Any]:
    field = {"name": name, "kind": kind, "label": label, "required": required}
    if value is not None:
        field["value"] = value
    if options is not None:
        field["options"] = options
    return field


def _rows(app: Any, statement: str, params: tuple) -> list:
    with app._pg_connect() as conn:
        return [tuple(_text(v) if isinstance(v, (bytes, bytearray, memoryview)) else v
                      for v in (row.values() if isinstance(row, dict) else row))
                for row in conn.execute(statement, params).fetchall()]


def _uat_decision(cfg: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """The User's own decision out of a User Review stage, placed in the UAT panel with what it does: the question
    its confirmation asks, and where the ticket goes and who acts next (SYRD-629). Nothing for any other action."""
    stages = {s["name"]: s for s in cfg["stages"]}
    source, destination = stages.get(action["from"]) or {}, stages.get(action["to"]) or {}
    if source.get("kind") != "review" or "user" not in (source.get("owners") or []):
        return {}
    labels = {r["name"]: r.get("label") or r["name"] for r in cfg["roles"]}
    owners = " or ".join(labels.get(o, o) for o in destination.get("owners") or []) or "nobody"
    where = destination.get("label") or action["to"]
    if action["primitive"] == "approve":
        return {"placement": "uat_panel", "uat_decision": "accept", "question": "Accept this ticket?",
                "next": f"It moves to {where}, and the {owners} acts next."}
    if destination.get("owners") == ["director"] and destination.get("kind") != "review":
        return {"placement": "uat_panel", "uat_decision": "send",
                "question": "Send this ticket and your message to the Director?",
                "next": f"It moves to {where}. The Director reviews your message and decides what happens next; "
                        "nothing goes back to the implementer unless the Director sends it."}
    return {"next": f"It moves to {where}, and the {owners} acts next."}


def describe(handler: Any, ticket_id: str) -> dict[str, Any]:
    """What the board UI may offer for one ticket now, and each operation's payload: the User's own declared
    actions, done directly, and what the control role may do that the board lends, done under a confirmation.
    Computed from the live workflow and the database, so the page keeps no table of its own."""
    app = handler.app
    ticket = app.get_ticket(ticket_id)
    cfg = app.workflow_configuration()
    authority = control_role(app)
    reason = _field("reason", "text", "Why", required=True)
    # Anyone may read what the User may do; only a board UI user's connection is offered the lent operations.
    unavailable = ui_user_identity.refusal(handler.connection)

    def lendable(operation: str) -> bool:
        if unavailable:
            return False
        try:
            handler.require_operation_allowed(operation, authority, ticket_id)
        except (PermissionError, ValueError):
            return False
        return bool(_rows(app, "SELECT ticket_board.ui_delegable(%s, %s)", (ticket_id, operation))[0][0])

    def owners(stage: str) -> list[str]:
        if cfg:
            return next((list(s.get("owners") or []) for s in cfg["stages"] if s["name"] == stage), [])
        return [r[0] for r in _rows(app, "SELECT unnest(owner_roles) FROM ticket_board.workflow_stages WHERE name = %s",
                                    (stage,))]

    offered: list[dict[str, Any]] = []

    def offer(operation: str, label: str, mode: str, fields: list[dict[str, Any]], **extra: Any) -> None:
        offered.append({"operation": operation, "label": label, "mode": mode, "fields": fields, **extra})

    if cfg:
        kinds = {s["name"]: s.get("kind") for s in cfg["stages"]}
        active = {r["name"] for r in cfg["roles"] if r.get("active")}
        router = any(r.get("kind") == "router" and r.get("active") for r in cfg["roles"])
        for action in ticket.get("workflow_actions") or []:
            fields = [_field("target", "fixed", "To", value=action["to"])]
            why = [_field("text", "text", "Why", required=True)] if action.get("require_reason") else []
            mine = "user" in action["actors"] and (not action.get("owner_scoped") or ticket.get("assignee") == "user")
            if mine and not action.get("require_commit"):
                offer(action["action"], action["label"], "direct", fields + why, to=action["to"],
                      **_uat_decision(cfg, action))
                continue
            if authority not in action["actors"] or not lendable(action["action"]):
                continue
            # SYRD-599: where Router runs, the control role's move into Implementation names no owner and
            # Router chooses one; naming an active Implementation owner is the separate, direct route.
            if (router and kinds.get(action["to"]) == "implementation" and action.get("primitive") == "move"
                    and kinds.get(action["from"]) not in ("implementation", "review")):
                implementers = [o for o in owners(action["to"]) if o in active]
                offer(action["action"], "Route through Router", "delegated", fields + why, to=action["to"],
                      variant="router")
                offer(action["action"], "Assign directly", "delegated", fields + why + [
                    _field("assignee", "choice", "Implementation owner", options=implementers, required=True)],
                    to=action["to"], variant="direct")
                continue
            if action["action"] == "route" or action.get("primitive") == "route":
                fields.append(_field("assignee", "choice", "Assignee", options=owners(action["to"]), required=True))
            offer(action["action"], action["label"], "delegated", fields + why, to=action["to"])
    else:
        transitions = _rows(app, "SELECT to_stage, action_name FROM ticket_board.workflow_transitions "
                                 "WHERE from_stage = %s AND %s = ANY(allowed_roles) ORDER BY action_name, to_stage",
                            (ticket.get("state", ""), authority))
        destinations = sorted({to for to, action in transitions if action == "route"})
        if destinations and lendable("route"):
            offer("route", "Route", "delegated", [_field("state", "choice", "To", options=destinations, required=True),
                                                  _field("assignee", "choice", "Assignee",
                                                         options=sorted({o for d in destinations for o in owners(d)}),
                                                         required=True)])
        for action, label, fields in (("cancel", "Cancel", [reason]), ("defer", "Defer to backlog", []),
                                      ("mark_done", "Mark done", [])):
            if any(a == action for _to, a in transitions) and lendable(action):
                offer(action, label, "delegated", fields)
    if lendable("reassign"):
        offer("reassign", "Reassign", "delegated",
              [_field("assignee", "choice", "Assignee",
                      options=[o for o in owners(ticket.get("state", "")) if o != ticket.get("assignee")], required=True),
               reason])
    if lendable("set_manually_controlled"):
        held = bool(ticket.get("manually_controlled"))
        offer("set_manually_controlled", "Release manual control" if held else "Hold under manual control",
              "delegated", [_field("manually_controlled", "fixed", "Manual control", value=not held)], placement="toggle")
    if lendable("set_blockers"):
        offer("set_blockers", "Set blockers", "delegated",
              [_field("blocked_by", "list", "Blocked by", value=list(ticket.get("blocked_by") or [])),
               _field("blocked_reason", "text", "Blocked reason", value=ticket.get("blocked_reason") or "")],
              placement="blockers")
    external = [b for b in (ticket.get("blocked_by") or []) if ":" in str(b)]
    if external and lendable("release_external_blocker"):
        offer("release_external_blocker", "Release an external blocker", "delegated",
              [_field("ref", "choice", "Blocker", options=external, required=True), reason])
    if cfg and lendable("recover_stalled_ticket"):
        offer("recover_stalled_ticket", "Recover a stalled ticket", "delegated", [reason])
    delivery = ticket.get("active_work_delivery") or {}
    owner = ticket.get("active_work_owner_role") or ""
    if delivery.get("state") in {"pending", "failed"} and owner and lendable("dismiss_notification"):
        offer("dismiss_notification", "Dismiss the undelivered notice", "delegated",
              [_field("ticket_id", "fixed", "Ticket", value=ticket_id), _field("target_role", "fixed", "To", value=owner),
               _field("kind", "fixed", "Notice", value="transition"), reason])
    publication = ticket.get("publication") or {}
    if publication.get("id") and lendable("resolve_publication"):
        offer("resolve_publication", "Resolve the publication request", "delegated",
              [_field("request_id", "fixed", "Request", value=publication["id"]),
               _field("outcome", "choice", "Outcome", options=["rejected", "published"], required=True),
               _field("detail", "text", "Detail", required=True)])
    if lendable("merge"):
        offer("merge", "Merge into another ticket", "delegated",
              [_field("target_id", "ticket", "Merge into", required=True)], placement="merge")
    given = [name for name in ((cfg or {}).get("flags") or {}) if (cfg["flags"][name].get("kind") == "signoff")] \
        if cfg else ["inspector_signoff", "audit_signoff", "user_signoff"]
    given = [name for name in given
             if _rows(app, "SELECT ticket_board.signoff_raised(%s, %s)", (ticket_id, name))[0][0]] if given else []
    if given and lendable("withdraw_signoff"):
        offer("withdraw_signoff", "Withdraw a sign-off", "delegated",
              [_field("signoff", "choice", "Sign-off", options=given, required=True), reason])
    return {"ticket_id": ticket_id, "authority": authority, "operations": offered, "delegation_unavailable": unavailable}


def ticket_version(app: Any, ticket_id: str) -> str:
    """The ticket as the board shows it now: what a confirmation is bound to."""
    with app._pg_connect() as conn:
        row = conn.execute("SELECT ticket_board.ui_ticket_version(%s) AS version", (ticket_id,)).fetchone()
    return _text(row["version"] if isinstance(row, dict) else row[0]) if row else ""


def authority_for(handler: Any, operation: str, caller: str, ticket_id: str | None, payload: dict[str, Any]) -> str:
    """The role an action's permission is checked against: the caller's own, or the control role's for the
    User's confirmed board-UI action, whose grant this request then consumes in the action's own transaction."""
    token = handler.headers.get(DELEGATION_HEADER, "").strip()
    if not token:
        _pending.set(None)
        return caller
    _from_the_user(handler)
    session = _session(handler)
    if caller != "user":
        raise PermissionError("a board UI confirmation is the User's only")
    if not ticket_id:
        raise PermissionError("a board UI confirmation is for one ticket")
    # Lent only to an operation the grant names, before anything runs: some operations act before their
    # first write (a git measurement, a file), so a token for one action must never open another. Session,
    # use, expiry, payload and staleness are checked where the grant is consumed, in the action's transaction.
    ticket_id = str(ticket_id).strip().upper()
    with handler.app._pg_connect() as conn:
        row = conn.execute(
            "SELECT g.ticket_id, g.operation, ticket_board.ui_delegable(%s, %s) AS lendable FROM (SELECT NULL) x"
            " LEFT JOIN ticket_board.ui_delegations g ON g.token_sha256 = encode(sha256(convert_to(%s, 'UTF8')), 'hex')",
            (ticket_id, operation, token),
        ).fetchone()
    granted_ticket, granted_operation, lendable = (row["ticket_id"], row["operation"], row["lendable"]) \
        if isinstance(row, dict) else tuple(row)
    granted_ticket, granted_operation = (None if v is None else _text(v) for v in (granted_ticket, granted_operation))
    if granted_ticket is None:
        raise ConfirmationRefused("unknown board UI confirmation")
    if (granted_ticket, granted_operation) != (ticket_id, operation):
        raise ConfirmationRefused(f"this board UI confirmation was for {granted_operation} on {granted_ticket}, "
                                  f"not {operation} on {ticket_id}")
    if not lendable:
        raise ConfirmationRefused(f"the board UI can no longer lend {operation} for {ticket_id}")
    _pending.set({"token": token, "session": session, "ticket_id": ticket_id,
                  "operation": operation, "payload": json.dumps(payload, sort_keys=True)})
    return control_role(handler.app)


#: Operations this module performs: lowering one named approval (the server hands them here).
OPERATIONS = frozenset({"withdraw_signoff"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation != "withdraw_signoff" or not ticket_id:
        raise ValueError(f"{operation} is one ticket's")
    if set(payload) - {"signoff", "reason"}:
        raise ValueError("withdraw_signoff takes the sign-off to withdraw and why")
    ticket_id = str(ticket_id).strip().upper()
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            conn.execute("SELECT ticket_board.withdraw_signoff(%s, %s, %s)",
                         (ticket_id, str(payload.get("signoff", "")), str(payload.get("reason", ""))))
            return {"ticket": app._pg_get_ticket(ticket_id, conn)}


def confirmed(operation: str) -> bool:
    """Whether this request carries the User's board-UI confirmation for `operation`, not yet consumed."""
    pending = _pending.get()
    return pending is not None and pending["operation"] == operation


def begin_request() -> None:
    """Every request starts with no confirmation pending: a kept-alive connection serves its next request on
    the same thread, and a confirmation one request did not spend must never be spent by another."""
    _pending.set(None)


def consume_pending(conn: Any, caller_role: str) -> None:
    """In the first transaction this request writes as the User: consume its confirmation, before anything runs."""
    pending = _pending.get()
    if pending is None or caller_role != "user":
        return
    _pending.set(None)
    try:
        conn.execute("SELECT ticket_board.consume_ui_delegation(%s, %s, %s, %s, %s::jsonb)",
                     (pending["token"], pending["session"], pending["ticket_id"], pending["operation"], pending["payload"]))
    except Exception as exc:  # the transaction is rolled back: nothing of the action happened
        raise _refused(exc) from exc
