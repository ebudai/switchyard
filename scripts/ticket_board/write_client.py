"""HTTP write client for the PGU ticket board action API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse, request

CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
DEFAULT_BOARD_URL = os.environ.get("PGU_TICKET_BOARD_URL", "http://127.0.0.1:8770")


class TicketBoardWriteError(RuntimeError):
    """Raised when the board rejects a write request."""


def _normalize_api_url(board_url: str) -> str:
    normalized = board_url.rstrip("/")
    if normalized.endswith("/api/tickets"):
        return normalized
    if normalized.endswith("/api"):
        return f"{normalized}/tickets"
    return f"{normalized}/api/tickets"


@dataclass(frozen=True)
class TicketBoardWriteClient:
    board_url: str = DEFAULT_BOARD_URL
    caller_role: str = "director"
    timeout: float = 10.0

    @property
    def api_url(self) -> str:
        return _normalize_api_url(self.board_url)

    def for_caller(self, caller_role: str) -> "TicketBoardWriteClient":
        return TicketBoardWriteClient(self.board_url, caller_role, self.timeout)

    def _post(self, path: str, payload: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        role = (caller_role or self.caller_role).strip().lower()
        if not role:
            raise ValueError("caller_role must be non-empty")
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.api_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", CALLER_ROLE_HEADER: role},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise TicketBoardWriteError(response_body or f"HTTP {exc.code}") from exc
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise TicketBoardWriteError("ticket board response was not an object")
        return parsed

    def _ticket_action(
        self,
        ticket_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        *,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        encoded_ticket = parse.quote(ticket_id.strip().upper(), safe="")
        encoded_operation = parse.quote(operation, safe="")
        return self._post(f"/{encoded_ticket}/actions/{encoded_operation}", payload or {}, caller_role=caller_role)

    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        assignee: str = "unassigned",
        state: str = "analysis",
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        implementation: str = "",
        audit_prompt: str = "",
        needs_eric_signoff: bool = False,
        comment_text: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "screenshot": screenshot,
            "screenshots": screenshots or [],
            "assignee": assignee,
            "state": state,
            "blocked_by": blocked_by or [],
            "blocked_reason": blocked_reason,
            "implementation": implementation,
            "audit_prompt": audit_prompt,
            "needs_eric_signoff": needs_eric_signoff,
        }
        if comment_text:
            payload["comment_text"] = comment_text
        return self._post("/actions/create_ticket", payload, caller_role=caller_role)

    def file_bug(
        self,
        *,
        title: str,
        body: str,
        source_ticket_id: str,
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        assignee: str = "unassigned",
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        needs_eric_signoff: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/actions/file_bug",
            {
                "title": title,
                "body": body,
                "source_ticket_id": source_ticket_id,
                "screenshot": screenshot,
                "screenshots": screenshots or [],
                "assignee": assignee,
                "blocked_by": blocked_by or [],
                "blocked_reason": blocked_reason,
                "needs_eric_signoff": needs_eric_signoff,
            },
            caller_role=caller_role,
        )

    def route(self, ticket_id: str, *, state: str, assignee: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "route", {"state": state, "assignee": assignee}, caller_role=caller_role)

    def start_work(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "start_work", caller_role=caller_role)

    def submit_to_audit(self, ticket_id: str, *, commit_hash: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "submit_to_audit", {"commit_hash": commit_hash}, caller_role=caller_role)

    def audit_sign_off(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "audit_sign_off", caller_role=caller_role)

    def audit_kick_back(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "audit_kick_back", {"reason": reason}, caller_role=caller_role)

    def eric_sign_off(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "eric_sign_off", caller_role=caller_role)

    def eric_reopen(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "eric_reopen", {"reason": reason}, caller_role=caller_role)

    def mark_done(self, ticket_id: str, *, commit_hash: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "mark_done", {"commit_hash": commit_hash}, caller_role=caller_role)

    def defer(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "defer", caller_role=caller_role)

    def cancel(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "cancel", {"reason": reason}, caller_role=caller_role)

    def set_manually_controlled(self, ticket_id: str, value: bool, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "set_manually_controlled", {"manually_controlled": value}, caller_role=caller_role)

    def set_blockers(
        self,
        ticket_id: str,
        *,
        blocked_by: list[str],
        blocked_reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "set_blockers",
            {"blocked_by": blocked_by, "blocked_reason": blocked_reason},
            caller_role=caller_role,
        )

    def add_comment(self, ticket_id: str, *, text: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "add_comment", {"text": text}, caller_role=caller_role)


def _ticket_from_response(response: dict[str, Any]) -> dict[str, Any]:
    ticket = response.get("ticket")
    if not isinstance(ticket, dict):
        raise TicketBoardWriteError("ticket board response did not include ticket")
    return ticket


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write tickets through the board action API.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Board root or /api/tickets URL (default: {DEFAULT_BOARD_URL})")
    parser.add_argument("--caller-role", default="director", help="Value for X-PGU-Caller-Role (default: director)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-ticket")
    create.add_argument("--title", required=True)
    create.add_argument("--body", required=True)
    create.add_argument("--assignee", default="unassigned")
    create.add_argument("--state", default="analysis")
    create.add_argument("--screenshot")
    create.add_argument("--blocked-by", action="append", default=[])
    create.add_argument("--blocked-reason", default="")
    create.add_argument("--implementation", default="")
    create.add_argument("--audit-prompt", default="")
    create.add_argument("--comment-text", default="")
    create.add_argument("--needs-eric-signoff", action="store_true")

    file_bug = subparsers.add_parser("file-bug")
    file_bug.add_argument("--title", required=True)
    file_bug.add_argument("--body", required=True)
    file_bug.add_argument("--source-ticket-id", required=True)
    file_bug.add_argument("--assignee", default="unassigned")

    route = subparsers.add_parser("route")
    route.add_argument("ticket_id")
    route.add_argument("--state", required=True)
    route.add_argument("--assignee", required=True)

    for name in ("start-work", "audit-sign-off", "eric-sign-off", "defer"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")

    submit = subparsers.add_parser("submit-to-audit")
    submit.add_argument("ticket_id")
    submit.add_argument("--commit-hash", required=True)

    for name in ("audit-kick-back", "eric-reopen", "cancel"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")
        sub.add_argument("--reason", required=True)

    done = subparsers.add_parser("mark-done")
    done.add_argument("ticket_id")
    done.add_argument("--commit-hash", required=True)

    manual = subparsers.add_parser("set-manually-controlled")
    manual.add_argument("ticket_id")
    manual.add_argument("--value", action=argparse.BooleanOptionalAction, default=True)

    blockers = subparsers.add_parser("set-blockers")
    blockers.add_argument("ticket_id")
    blockers.add_argument("--blocked-by", action="append", required=True)
    blockers.add_argument("--blocked-reason", required=True)

    comment = subparsers.add_parser("add-comment")
    comment.add_argument("ticket_id")
    comment.add_argument("--text", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    client = TicketBoardWriteClient(args.board_url, args.caller_role)
    command = args.command.replace("-", "_")
    if command == "create_ticket":
        response = client.create_ticket(
            title=args.title,
            body=args.body,
            screenshot=args.screenshot,
            assignee=args.assignee,
            state=args.state,
            blocked_by=args.blocked_by,
            blocked_reason=args.blocked_reason,
            implementation=args.implementation,
            audit_prompt=args.audit_prompt,
            needs_eric_signoff=args.needs_eric_signoff,
            comment_text=args.comment_text,
        )
    elif command == "file_bug":
        response = client.file_bug(title=args.title, body=args.body, source_ticket_id=args.source_ticket_id, assignee=args.assignee)
    elif command == "route":
        response = client.route(args.ticket_id, state=args.state, assignee=args.assignee)
    elif command == "submit_to_audit":
        response = client.submit_to_audit(args.ticket_id, commit_hash=args.commit_hash)
    elif command in {"audit_kick_back", "eric_reopen", "cancel"}:
        response = getattr(client, command)(args.ticket_id, reason=args.reason)
    elif command == "mark_done":
        response = client.mark_done(args.ticket_id, commit_hash=args.commit_hash)
    elif command == "set_manually_controlled":
        response = client.set_manually_controlled(args.ticket_id, args.value)
    elif command == "set_blockers":
        response = client.set_blockers(args.ticket_id, blocked_by=args.blocked_by, blocked_reason=args.blocked_reason)
    elif command == "add_comment":
        response = client.add_comment(args.ticket_id, text=args.text)
    else:
        response = getattr(client, command)(args.ticket_id)
    print(json.dumps(_ticket_from_response(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
