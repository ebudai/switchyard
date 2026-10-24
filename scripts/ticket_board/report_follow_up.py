"""A reporting tenant's follow-up on its own upstream report (SYRD-677).

A tenant that filed a report could add nothing to it afterwards: the report
credential only created. This adds one append-only operation, a follow-up: an
attributed comment on a report the tenant's own project filed, and on no other
ticket.

* **Who.** Every tenant used to hold the board's one shared report token, and a
  report named its own project. A follow-up's project is read from the
  credential instead: `bound_token` binds a credential to one project with an
  HMAC of the board's binding key (`TICKET_BOARD_REPORT_BINDING_KEY`), a secret
  of the board's own that no tenant is ever given -- not the report secret,
  which every tenant held before -- so the board verifies it without a
  registry, a request never names its project, and a tenant that kept the old
  shared token cannot make another project's. `refresh_upstream_report_
  credential` hands each tenant the token bound to its own project. The shared
  token still files reports, as before; a bound one files only its project's.
* **Which report.** `ticket_board.report_provenance` records a report's origin
  when it is filed (backfilled for existing reports) and is never changed, so a
  Director's edit of a ticket's origin fields moves no authority.
* **Exactly what.** As for `file_report` (SYRD-602), nothing is written until
  the tenant confirms a preview: the digest binds the destination board, the
  ticket, the credential's project, the report's source reference, the kind and
  the literal text, and the board recomputes it from what it holds.
* **What it does.** One comment, attributed `reporter:<project>`, and one notice
  to the Director, both once per confirmation. No state, assignee, flag, field
  or link changes; a withdrawal or supersession is a request to the Director.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from http import HTTPStatus
from typing import Any

from .report_confirmation import DIGEST_CHARS

REPORT_TOKEN_HEADER = "X-Ticket-Board-Report-Token"  # the server's and the write client's
CREDENTIAL_PREFIX = "syrdr1"
#: The launcher's project slug rule (`team_launcher.PROJECT_SLUG_RE`).
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
KINDS = {"comment": "Comment", "correction": "Correction", "withdrawal_request": "Withdrawal requested",
         "supersession": "Superseded"}
MAX_TEXT_CHARS = 8000
FIELDS = frozenset({"ticket_id", "kind", "text", "confirmation"})
DIGEST_VERSION = "syrd677-v1"
ACTIONS = frozenset({"file_report", "report_follow_up"})


def bound_token(binding_key: str, project: str) -> str:
    """The report credential of one project: the board's binding key, bound to that project."""
    mac = hmac.new(binding_key.encode("utf-8"), f"syrd-677 report credential:{project}".encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{CREDENTIAL_PREFIX}.{project}.{mac}"


def named_project(token: str) -> str:
    """The project a bound credential names, unverified: what a preview says it will send as. '' when unbound."""
    parts = str(token or "").strip().split(".")
    return parts[1] if len(parts) == 3 and parts[0] == CREDENTIAL_PREFIX and PROJECT_SLUG_RE.fullmatch(parts[1]) else ""


def usable_binding_key(secret: str, binding_key: str) -> str:
    """The binding key, when it is one: set, and not the report secret every tenant once held."""
    secret, binding_key = str(secret or "").strip(), str(binding_key or "").strip()
    return binding_key if binding_key and not hmac.compare_digest(binding_key.encode(), secret.encode()) else ""


def credential_project(secret: str, binding_key: str, token: str) -> str | None:
    """The project a credential is bound to; '' for the board's shared token; None for neither."""
    secret, token = str(secret or "").strip(), str(token or "").strip()
    binding_key = usable_binding_key(secret, binding_key)
    if not token:
        return None
    if secret and hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8")):
        return ""
    project = named_project(token)
    if binding_key and project and hmac.compare_digest(token.encode("utf-8"),
                                                       bound_token(binding_key, project).encode("utf-8")):
        return project
    return None


def follow_up_digest(*, destination: dict[str, str], ticket_id: str, origin_project: str, external_source_ref: str,
                     kind: str, text: str) -> str:
    canonical = json.dumps({
        "version": DIGEST_VERSION,
        "destination": {"project": destination["project"], "ticket_prefix": destination["ticket_prefix"]},
        "ticket_id": ticket_id, "origin_project": origin_project, "external_source_ref": external_source_ref,
        "kind": kind, "text": text,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def preview_text(*, destination: dict[str, str], board_url: str, ticket: dict[str, Any], origin_project: str,
                 kind: str, text: str, digest: str, resubmit: str) -> str:
    return "\n".join([
        "Report follow-up preview -- nothing has been sent.",
        f"  board:       {destination['project']} ({destination['ticket_prefix']}) at {board_url}",
        f"  report:      {ticket.get('id', '')} -- {ticket.get('title', '')}",
        f"  now:         {ticket.get('state', '')}, assigned to {ticket.get('assignee', '') or 'unassigned'}",
        f"  filed from:  {origin_project}"
        + (f" ({ticket.get('external_source_ref')})" if ticket.get("external_source_ref") else ""),
        f"  kind:        {kind} ({KINDS[kind]})",
        f"  as:          reporter:{origin_project}",
        "  text:",
        *[f"    {line}" for line in text.splitlines() or [""]],
        "It is added as a comment and the Director is told; it changes nothing else on the ticket.",
        f"digest: {digest}",
        f"To send exactly this, run the same command with: {resubmit}",
    ])


def read_report(api_url: str, ticket_id: str, *, timeout: float) -> dict[str, Any]:
    """The report as the upstream board shows it, read and nothing written: what a follow-up preview names."""
    from urllib import error, parse, request

    url = f"{api_url.rstrip('/')}/{parse.quote(ticket_id.strip().upper(), safe='')}"
    try:
        with request.urlopen(request.Request(url, method="GET"), timeout=timeout) as response:
            ticket = json.loads(response.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        reason = "it has no such ticket" if exc.code == 404 else f"HTTP {exc.code}"
        raise LookupError(f"report-follow-up: {ticket_id} cannot be read from the upstream board: {reason}. "
                          "Nothing was sent.") from exc
    except (error.URLError, OSError, ValueError) as exc:
        raise LookupError(f"report-follow-up: cannot read the upstream board: {getattr(exc, 'reason', exc)}. "
                          "Nothing was sent.") from exc
    if not isinstance(ticket, dict) or not ticket.get("id"):
        raise LookupError(f"report-follow-up: the upstream board did not return {ticket_id}. Nothing was sent.")
    return ticket


def authenticate(handler: Any) -> str:
    """The report credential on this request: '' for the board's shared token, the project for a bound one."""
    if handler.role_authority is not None:
        raise PermissionError("tenant report filing is only available over HTTP")
    if not handler.report_token:
        raise PermissionError(f"{REPORT_TOKEN_HEADER} is not configured")
    project = credential_project(handler.report_token, getattr(handler.server, "report_binding_key", ""),
                                 handler.headers.get(REPORT_TOKEN_HEADER, ""))
    if project is None:
        raise PermissionError(f"missing or invalid {REPORT_TOKEN_HEADER}")
    return project


def handle(handler: Any, action: str, payload: Any) -> None:
    """POST /api/tickets/actions/{file_report,report_follow_up}: the report credential's two operations."""
    project = authenticate(handler)
    if not isinstance(payload, dict):
        raise ValueError(f"{action} takes a JSON object")
    if action == "file_report":
        # A bound credential files reports from its own project only; the shared one, as before.
        if project and str(payload.get("origin_project", "")).strip().lower() != project:
            raise PermissionError(f"this report credential is {project}'s: it files reports from {project} only")
        return handler.handle_file_report(payload)
    if not project:
        raise PermissionError(
            "a follow-up needs this project's own report credential, bound to it; this is the board's shared "
            "report token. An operator refreshes it: `sudo switchyard upgrade <project> --only upstream-report`")
    from .extension_operations import refuse_on_oracle

    refuse_on_oracle(handler.app, action)  # SYRD-645
    result = submit(handler.app, project, payload)
    handler.events.notify_change(handler.app.store_signature())
    handler.send_json(result, HTTPStatus.CREATED if result["created"] else HTTPStatus.OK)


def checked(payload: dict[str, Any]) -> dict[str, str]:
    unknown = sorted(set(payload) - FIELDS)
    if unknown:
        raise PermissionError(f"report_follow_up cannot set: {', '.join(unknown)}")
    values = {name: payload.get(name) for name in FIELDS}
    if not all(isinstance(value, str) for value in values.values()):
        raise ValueError("report_follow_up takes ticket_id, kind, text and confirmation, each a string")
    ticket_id, kind, text = values["ticket_id"].strip().upper(), values["kind"].strip(), values["text"]
    if not ticket_id:
        raise ValueError("report_follow_up needs a ticket_id")
    if kind not in KINDS:
        raise ValueError(f"report_follow_up kind must be one of: {', '.join(KINDS)}")
    if not text.strip():
        raise ValueError("report_follow_up text must be non-empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"report_follow_up text is limited to {MAX_TEXT_CHARS} characters")
    if not re.fullmatch(f"[0-9a-f]{{{DIGEST_CHARS}}}", values["confirmation"].strip()):
        raise ValueError("report_follow_up needs the confirmation digest its preview printed")
    return {"ticket_id": ticket_id, "kind": kind, "text": text, "confirmation": values["confirmation"].strip()}


def _refusal(exc: Exception) -> Exception:
    """The database's refusal, as the HTTP status that says what it is."""
    code = getattr(getattr(exc, "diag", None), "sqlstate", None) or getattr(exc, "sqlstate", None)
    message = str(getattr(getattr(exc, "diag", None), "message_primary", "") or exc).strip()
    if code == "42501":
        return PermissionError(message)
    if code == "P0002":
        return FileNotFoundError(message)
    return ValueError(message)


def submit(app: Any, project: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Add the confirmed follow-up once: {ticket_id, comment_id, created}. A used confirmation adds nothing."""
    follow_up = checked(payload)
    destination = {"project": str(app.project), "ticket_prefix": str(app.ticket_prefix)}
    try:
        with app._pg_connect() as conn:
            with conn.transaction():
                target = conn.execute("SELECT found, origin_project, external_source_ref, shown_source_ref "
                                      "FROM ticket_board.report_follow_up_target(%s)", (follow_up["ticket_id"],)).fetchone()
                found, origin, source_ref, shown = (target["found"], target["origin_project"], target["external_source_ref"],
                                                    target["shown_source_ref"]) if isinstance(target, dict) else tuple(target)
                if not found:
                    raise FileNotFoundError(f"there is no ticket {follow_up['ticket_id']} on this board")
                if origin is None:
                    raise PermissionError(f"{follow_up['ticket_id']} was not filed as a report; a follow-up is only for "
                                          "a report its own project filed")
                if origin != project:
                    raise PermissionError(f"{follow_up['ticket_id']} was filed from another project; this credential "
                                          f"is {project}'s")
                digest = follow_up_digest(destination=destination, ticket_id=follow_up["ticket_id"],
                                          origin_project=project, external_source_ref=source_ref,
                                          kind=follow_up["kind"], text=follow_up["text"])
                if not hmac.compare_digest(digest, follow_up["confirmation"]) and shown != source_ref and hmac.compare_digest(
                        follow_up_digest(destination=destination, ticket_id=follow_up["ticket_id"], origin_project=project,
                                         external_source_ref=shown, kind=follow_up["kind"], text=follow_up["text"]),
                        follow_up["confirmation"]):
                    raise PermissionError(f"{follow_up['ticket_id']}'s source reference was changed on this board after it "
                                          f"was filed ({source_ref!r} when filed, {shown!r} now); a follow-up is bound to "
                                          "the report as it was filed. Ask the upstream Director.")
                if not hmac.compare_digest(digest, follow_up["confirmation"]):
                    raise ValueError("report_follow_up confirmation does not match this follow-up on this board: its "
                                     "ticket, kind, text, destination or the report's source reference changed. "
                                     "Preview it again.")
                row = conn.execute("SELECT comment_id, created FROM ticket_board.append_report_follow_up("
                                   "%s, %s, %s, %s, %s, %s)",
                                   (follow_up["ticket_id"], project, source_ref, follow_up["kind"], follow_up["text"],
                                    digest)).fetchone()
    except (FileNotFoundError, PermissionError, ValueError):
        raise
    except Exception as exc:  # the database refused: say what it said
        if type(exc).__module__.startswith("psycopg"):
            raise _refusal(exc) from exc
        raise
    comment_id, created = (row["comment_id"], row["created"]) if isinstance(row, dict) else tuple(row)
    return {"ticket_id": follow_up["ticket_id"], "comment_id": int(comment_id), "created": bool(created),
            **({} if created else {"replayed": True})}
