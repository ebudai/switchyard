"""Reporting to another project's board is two steps, every time (SYRD-602).

A tenant's agents hold a report token for an upstream board, and one
`file-report` call used to create a ticket there. MEFP created at least three
SYRD tickets that way without meaning to. So every agent-initiated
cross-board report is now:

1. **A preview.** `ticket-board-write file-report ...` reads the destination
   board's public identity, writes nothing anywhere, and prints the whole
   report, the two projects, and a digest.
2. **A submission of exactly that.** The same command with
   `--submit-upstream --expect <digest>`. The digest is bound to the
   destination's identity, the source project, the title, the body, the
   requested state, the source reference and the attachments. Any of them
   changing, or a different destination, makes it a different digest.

The destination board checks it again itself. A report that names no
confirmation, or one that does not match what that board computes from the
payload and its own identity, is refused with nothing created. A confirmation
already used returns the ticket it created rather than a second one, so a
replayed submission creates nothing. There is no standing consent: no
configured URL, token or flag stands in for the per-report digest.

There is deliberately no one-call path. No automated reporter exists; when one
does, it needs its own declared and authorised path on the receiving board --
something an ordinary tenant credential cannot assert -- not a helper that
fills in the digest for whoever calls it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DIGEST_VERSION = "syrd602-v1"
DIGEST_CHARS = 24
#: What a report may carry: the fields the board has always accepted, the
#: confirmation, and nothing that would let a tenant set state or ownership.
REPORT_FIELDS = {"title", "body", "origin_project", "external_source_ref", "source_ref", "requested_stage",
                 "attachments", "confirmation"}


def destination_identity(client_config: dict[str, Any]) -> dict[str, str]:
    """The board a report would land on, as its own public configuration names it."""
    return {
        "project": str(client_config.get("project") or "").strip(),
        "ticket_prefix": str(client_config.get("ticket_prefix") or "").strip(),
    }


def report_digest(*, destination: dict[str, str], source_project: str, title: str, body: str,
                  requested_stage: str = "", external_source_ref: str = "",
                  attachments: list[str] | None = None) -> str:
    """The confirmation for exactly this report to exactly this board."""
    canonical = json.dumps({
        "version": DIGEST_VERSION,
        "destination": {"project": destination["project"], "ticket_prefix": destination["ticket_prefix"]},
        "source_project": source_project, "title": title, "body": body,
        "requested_stage": requested_stage, "external_source_ref": external_source_ref,
        "attachments": list(attachments or []),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def preview_text(*, destination: dict[str, str], project_name: str, board_url: str, source_project: str,
                 caller_role: str, title: str, body: str, requested_stage: str, external_source_ref: str,
                 attachments: list[str], digest: str, resubmit: str) -> str:
    """The bounded preview an agent reads before anything crosses to another board."""
    flat = " ".join(body.split())
    summary = flat if len(flat) <= 300 else flat[:297] + "..."
    state = ("Backlog (deferred), as a request: that board's report-intake policy decides"
             if requested_stage == "backlog" else "Triage")
    return "\n".join([
        "file-report PREVIEW: nothing has been filed.",
        f"This would create a NEW ticket on ANOTHER project's board: {project_name} ({destination['project']}), "
        f"tickets {destination['ticket_prefix']}-N, at {board_url}.",
        f"  from:        project {source_project}, caller role {caller_role or '<none>'}",
        f"  title:       {title}",
        f"  state:       {state}",
        f"  source ref:  {external_source_ref or '<none>'}",
        f"  attachments: {', '.join(attachments) if attachments else 'none'}",
        f"  body:        {len(body.splitlines()) or 1} line(s), {len(body)} characters: {summary}",
        f"  digest:      {digest}",
        "Use file-report only for a Switchyard platform defect or a deliberate escalation to that project. "
        "Ordinary work for your own project goes on your own board (ticket-board-write create-ticket).",
        f"To file exactly this report, run the same command again with: {resubmit}",
    ])


def checked_submission(payload: dict[str, Any], *, project: str, ticket_prefix: str) -> dict[str, str]:
    """The board's half: the report it was sent, if and only if it carries this board's confirmation for it."""
    unknown = sorted(set(payload) - REPORT_FIELDS)
    if unknown:
        raise PermissionError(f"file_report cannot set: {', '.join(unknown)}")
    attachments = payload.get("attachments") or []
    if attachments:
        raise ValueError("file_report carries no attachments; describe them in the body")
    report = {
        "title": str(payload.get("title", "")), "body": str(payload.get("body", "")),
        "origin_project": str(payload.get("origin_project", "")),
        "external_source_ref": str(payload.get("external_source_ref", payload.get("source_ref", ""))),
        "requested_stage": str(payload.get("requested_stage") or ""),
    }
    confirmation = str(payload.get("confirmation") or "").strip()
    if not confirmation:
        raise PermissionError(
            "file_report needs a confirmed preview: run `ticket-board-write file-report ...` to see what would be "
            "filed on this board, then repeat it with --submit-upstream --expect <digest>. Nothing was filed.")
    expected = report_digest(destination={"project": project, "ticket_prefix": ticket_prefix},
                             source_project=report["origin_project"], title=report["title"], body=report["body"],
                             requested_stage=report["requested_stage"],
                             external_source_ref=report["external_source_ref"], attachments=[])
    if confirmation != expected:
        raise PermissionError(
            f"file_report confirmation does not match this report on this board ({project}, {ticket_prefix}-N): "
            "the report, its source or its destination changed since the preview. Preview it again. Nothing was filed.")
    return {**report, "confirmation": confirmation}


def submit(app: Any, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create the confirmed report once: (ticket, created). A used confirmation returns its ticket, uncreated."""
    report = checked_submission(payload, project=str(app.project), ticket_prefix=str(app.ticket_prefix))
    # Title, origin and the requested state are ticket_board.file_report's to refuse, as they always were.
    with app._pg_connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "SELECT ticket_id, created FROM ticket_board.file_confirmed_report(%s, %s, %s, %s, %s, %s)",
                (report["title"], report["body"], report["origin_project"], report["external_source_ref"],
                 report["requested_stage"], report["confirmation"]),
            ).fetchone()
    ticket_id = str(row["ticket_id"] if isinstance(row, dict) else row[0])
    created = bool(row["created"] if isinstance(row, dict) else row[1])
    return app.get_ticket(ticket_id), created
