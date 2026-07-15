"""HTTP write client for the PGU ticket board action API."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import parse, request

CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
DEFAULT_BOARD_URL = os.environ.get("PGU_TICKET_BOARD_URL", "http://127.0.0.1:8770")
DEFAULT_BOARD_SOCKET = os.environ.get("PGU_TICKET_BOARD_SOCKET", "/run/pgu-ticket-board/ticket-board.sock")
LEGACY_BOARD_SOCKET = "/tmp/pgu-ticket-board.sock"
DEFAULT_CALLER_ROLE = "director"
PANE_SESSION_CALLER_ROLES = {
    "pgu-director": "director",
    "pgu-main": "main",
    "pgu-app": "app",
    "pgu-ops": "ops",
    "pgu-audit": "audit",
    "pgu-inspector": "inspector",
    "pgu-perf": "perf",
    "pgu-research": "research",
}


class TicketBoardWriteError(RuntimeError):
    """Raised when the board rejects a write request."""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _normalize_api_url(board_url: str) -> str:
    normalized = board_url.rstrip("/")
    if normalized.endswith("/api/tickets"):
        return normalized
    if normalized.endswith("/api"):
        return f"{normalized}/tickets"
    return f"{normalized}/api/tickets"


def _normalize_api_path(board_url: str) -> str:
    parsed = parse.urlparse(_normalize_api_url(board_url))
    return parsed.path or "/api/tickets"


def _default_socket_path(board_url: str, environ: Mapping[str, str] = os.environ) -> str | None:
    explicit = environ.get("PGU_TICKET_BOARD_SOCKET", "").strip()
    if explicit:
        return explicit
    if board_url != DEFAULT_BOARD_URL:
        return None
    if Path(DEFAULT_BOARD_SOCKET).exists():
        return DEFAULT_BOARD_SOCKET
    if DEFAULT_BOARD_SOCKET != LEGACY_BOARD_SOCKET and Path(LEGACY_BOARD_SOCKET).exists():
        return LEGACY_BOARD_SOCKET
    return None


def _has_explicit_socket_path(socket_path: str | None, environ: Mapping[str, str] = os.environ) -> bool:
    return bool(socket_path or environ.get("PGU_TICKET_BOARD_SOCKET", "").strip())


def _caller_role_from_tmux_session(environ: Mapping[str, str] = os.environ) -> str | None:
    pane_id = environ.get("TMUX_PANE", "").strip()
    if not pane_id:
        return None
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{session_name}"],
            check=False,
            text=True,
            capture_output=True,
            timeout=0.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return PANE_SESSION_CALLER_ROLES.get(proc.stdout.strip())


def default_caller_role(environ: Mapping[str, str] = os.environ) -> str:
    explicit = environ.get("PGU_TICKET_BOARD_CALLER_ROLE", "").strip().lower()
    if explicit:
        return explicit
    return _caller_role_from_tmux_session(environ) or DEFAULT_CALLER_ROLE


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def _git_error_detail(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stderr.strip() or proc.stdout.strip() or f"git exited with status {proc.returncode}"


@dataclass(frozen=True)
class TicketBoardWriteClient:
    board_url: str = DEFAULT_BOARD_URL
    caller_role: str = field(default_factory=default_caller_role)
    timeout: float = 10.0
    socket_path: str | None = None

    @property
    def api_url(self) -> str:
        return _normalize_api_url(self.board_url)

    @property
    def api_path(self) -> str:
        return _normalize_api_path(self.board_url)

    @property
    def effective_socket_path(self) -> str | None:
        return self.socket_path or _default_socket_path(self.board_url)

    def for_caller(self, caller_role: str) -> "TicketBoardWriteClient":
        return TicketBoardWriteClient(self.board_url, caller_role, self.timeout, self.socket_path)

    def _post(self, path: str, payload: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        role = (caller_role or self.caller_role).strip().lower()
        if not role:
            raise ValueError("caller_role must be non-empty")
        socket_path = self.effective_socket_path
        if socket_path:
            try:
                return self._post_unix(path, payload, role, socket_path)
            except OSError as exc:
                if _has_explicit_socket_path(self.socket_path):
                    raise
                print(f"WARNING: ticket board Unix socket {socket_path} failed ({exc}); falling back to TCP {self.api_url}", file=sys.stderr)
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

    def _post_unix(self, path: str, payload: dict[str, Any], role: str, socket_path: str) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        conn = UnixHTTPConnection(socket_path, self.timeout)
        try:
            self._request_unix_json(conn, "/api/register-caller", {"role": role}, expected_status=200)
            return self._request_unix_json(
                conn,
                f"{self.api_path}{path}",
                payload,
                expected_status=None,
                caller_role_header=role,
                body=body,
            )
        finally:
            conn.close()

    def _request_unix_json(
        self,
        conn: UnixHTTPConnection,
        path: str,
        payload: dict[str, Any],
        *,
        expected_status: int | None,
        caller_role_header: str | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        request_body = body if body is not None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if caller_role_header is not None:
            headers[CALLER_ROLE_HEADER] = caller_role_header
        conn.request("POST", path, body=request_body, headers=headers)
        response = conn.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        if expected_status is not None and response.status != expected_status:
            raise TicketBoardWriteError(response_body or f"HTTP {response.status}")
        if expected_status is None and response.status >= 400:
            raise TicketBoardWriteError(response_body or f"HTTP {response.status}")
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

    def _require_commit_pushed_to_origin(self, commit_hash: str, *, operation: str) -> None:
        normalized = commit_hash.strip()
        if not normalized:
            return
        resolved = _run_git(["rev-parse", "--verify", f"{normalized}^{{commit}}"])
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise TicketBoardWriteError(f"unknown commit_hash: {normalized}")
        fetch = _run_git(["fetch", "origin"])
        if fetch.returncode != 0:
            raise TicketBoardWriteError(
                f"unable to verify commit {normalized} against origin before {operation}: {_git_error_detail(fetch)}"
            )
        contains = _run_git(
            [
                "for-each-ref",
                "--format=%(refname:short)",
                f"--contains={resolved.stdout.strip()}",
                "refs/remotes/origin",
            ]
        )
        if contains.returncode != 0:
            raise TicketBoardWriteError(
                f"unable to verify commit {normalized} against origin before {operation}: {_git_error_detail(contains)}"
            )
        if not any(line.strip().startswith("origin/") for line in contains.stdout.splitlines()):
            raise TicketBoardWriteError(
                f"commit {normalized} is not pushed to origin; "
                f"push your branch (git push origin HEAD:refs/heads/feature/...) before {operation}."
            )

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
        needs_inspection: bool = False,
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
            "needs_inspection": needs_inspection,
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

    def force_move(
        self,
        ticket_id: str,
        *,
        state: str,
        assignee: str,
        suppress_notification: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "force_move",
            {"state": state, "assignee": assignee, "suppress_notification": suppress_notification},
            caller_role=caller_role,
        )

    def override_move(
        self,
        ticket_id: str,
        *,
        state: str,
        assignee: str,
        notify: bool = True,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._ticket_action(
            ticket_id,
            "override_move",
            {"state": state, "assignee": assignee, "suppress_notification": not notify},
            caller_role=caller_role,
        )

    def start_work(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "start_work", caller_role=caller_role)

    def submit_to_audit(self, ticket_id: str, *, commit_hash: str = "", caller_role: str | None = None) -> dict[str, Any]:
        self._require_commit_pushed_to_origin(commit_hash, operation="submit_to_audit")
        return self._ticket_action(ticket_id, "submit_to_audit", {"commit_hash": commit_hash}, caller_role=caller_role)

    def request_commit_exempt(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "request_commit_exempt", {"reason": reason}, caller_role=caller_role)

    def start_task(self, ticket_id: str, *, text: str = "", caller_role: str | None = None) -> dict[str, Any]:
        payload = {"text": text} if text else None
        return self._ticket_action(ticket_id, "start_task", payload, caller_role=caller_role)

    def complete_task(self, ticket_id: str, *, text: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "complete_task", {"text": text}, caller_role=caller_role)

    def await_role(self, ticket_id: str, *, role: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "await_role", {"role": role}, caller_role=caller_role)

    def clear_awaiting_role(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "clear_awaiting_role", caller_role=caller_role)

    def audit_sign_off(self, ticket_id: str, *, text: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "audit_sign_off", {"text": text}, caller_role=caller_role)

    def audit_kick_back(
        self,
        ticket_id: str,
        *,
        reason: str,
        target_assignee: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload = {"reason": reason}
        if target_assignee:
            payload["target_assignee"] = target_assignee
        return self._ticket_action(ticket_id, "audit_kick_back", payload, caller_role=caller_role)

    def inspector_sign_off(self, ticket_id: str, *, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "inspector_sign_off", caller_role=caller_role)

    def inspector_kick_back(
        self,
        ticket_id: str,
        *,
        recommendations: str,
        target_assignee: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        payload = {"recommendations": recommendations}
        if target_assignee:
            payload["target_assignee"] = target_assignee
        return self._ticket_action(ticket_id, "inspector_kick_back", payload, caller_role=caller_role)

    def eric_sign_off(self, ticket_id: str, *, text: str = "", caller_role: str | None = None) -> dict[str, Any]:
        payload = {"text": text} if text else None
        return self._ticket_action(ticket_id, "eric_sign_off", payload, caller_role=caller_role)

    def eric_reopen(self, ticket_id: str, *, reason: str, caller_role: str | None = None) -> dict[str, Any]:
        return self._ticket_action(ticket_id, "eric_reopen", {"reason": reason}, caller_role=caller_role)

    def mark_done(self, ticket_id: str, *, commit_hash: str = "", caller_role: str | None = None) -> dict[str, Any]:
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
    caller_default = default_caller_role()
    parser = argparse.ArgumentParser(description="Write tickets through the board action API.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Board root or /api/tickets URL (default: {DEFAULT_BOARD_URL})")
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=None,
        help=(
            "Unix-domain board socket for local pane writes. Defaults to PGU_TICKET_BOARD_SOCKET, "
            f"or {DEFAULT_BOARD_SOCKET} when it exists and --board-url is the default."
        ),
    )
    parser.add_argument(
        "--caller-role",
        default=caller_default,
        help=(
            "Value for X-PGU-Caller-Role "
            f"(default: PGU_TICKET_BOARD_CALLER_ROLE, current pgu-* tmux session, else {DEFAULT_CALLER_ROLE}; "
            f"currently {caller_default})"
        ),
    )
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
    create.add_argument("--needs-inspection", action="store_true")
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

    force_move = subparsers.add_parser("force-move")
    force_move.add_argument("ticket_id")
    force_move.add_argument("--state", required=True)
    force_move.add_argument("--assignee", required=True)
    force_move.add_argument("--suppress-notification", action="store_true")

    override_move = subparsers.add_parser("override-move")
    override_move.add_argument("ticket_id")
    override_move.add_argument("--state", required=True)
    override_move.add_argument("--assignee", required=True)
    override_move.add_argument("--no-notify", action="store_true")

    for name in ("start-work", "inspector-sign-off", "eric-sign-off", "defer"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")

    start_task = subparsers.add_parser("start-task")
    start_task.add_argument("ticket_id")
    start_task.add_argument("--text", default="")

    complete_task = subparsers.add_parser("complete-task")
    complete_task.add_argument("ticket_id")
    complete_task.add_argument("--text", required=True)

    audit_sign = subparsers.add_parser("audit-sign-off")
    audit_sign.add_argument("ticket_id")
    audit_sign.add_argument("--text", required=True)

    submit = subparsers.add_parser("submit-to-audit")
    submit.add_argument("ticket_id")
    submit.add_argument("--commit-hash", default="")

    request_exempt = subparsers.add_parser("request-commit-exempt")
    request_exempt.add_argument("ticket_id")
    request_exempt.add_argument("--reason", required=True)

    await_role = subparsers.add_parser("await-role")
    await_role.add_argument("ticket_id")
    await_role.add_argument("--role", required=True)

    clear_awaiting = subparsers.add_parser("clear-awaiting-role")
    clear_awaiting.add_argument("ticket_id")

    audit_kick = subparsers.add_parser("audit-kick-back")
    audit_kick.add_argument("ticket_id")
    audit_kick.add_argument("--reason", required=True)
    audit_kick.add_argument("--target-assignee", default="")

    for name in ("eric-reopen", "cancel"):
        sub = subparsers.add_parser(name)
        sub.add_argument("ticket_id")
        sub.add_argument("--reason", required=True)

    inspector_kick = subparsers.add_parser("inspector-kick-back")
    inspector_kick.add_argument("ticket_id")
    inspector_kick.add_argument("--recommendations", required=True)
    inspector_kick.add_argument("--target-assignee", default="")

    done = subparsers.add_parser("mark-done")
    done.add_argument("ticket_id")
    done.add_argument("--commit-hash", default="")

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
    try:
        args = _build_parser().parse_args(argv)
        client = TicketBoardWriteClient(args.board_url, args.caller_role, socket_path=args.socket_path)
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
                needs_inspection=args.needs_inspection,
                needs_eric_signoff=args.needs_eric_signoff,
                comment_text=args.comment_text,
            )
        elif command == "file_bug":
            response = client.file_bug(title=args.title, body=args.body, source_ticket_id=args.source_ticket_id, assignee=args.assignee)
        elif command == "route":
            response = client.route(args.ticket_id, state=args.state, assignee=args.assignee)
        elif command == "force_move":
            response = client.force_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                suppress_notification=args.suppress_notification,
            )
        elif command == "override_move":
            response = client.override_move(
                args.ticket_id,
                state=args.state,
                assignee=args.assignee,
                notify=not args.no_notify,
            )
        elif command == "submit_to_audit":
            response = client.submit_to_audit(args.ticket_id, commit_hash=args.commit_hash)
        elif command == "request_commit_exempt":
            response = client.request_commit_exempt(args.ticket_id, reason=args.reason)
        elif command == "start_task":
            response = client.start_task(args.ticket_id, text=args.text)
        elif command == "complete_task":
            response = client.complete_task(args.ticket_id, text=args.text)
        elif command == "await_role":
            response = client.await_role(args.ticket_id, role=args.role)
        elif command == "clear_awaiting_role":
            response = client.clear_awaiting_role(args.ticket_id)
        elif command == "audit_sign_off":
            response = client.audit_sign_off(args.ticket_id, text=args.text)
        elif command == "audit_kick_back":
            response = client.audit_kick_back(args.ticket_id, reason=args.reason, target_assignee=args.target_assignee)
        elif command in {"eric_reopen", "cancel"}:
            response = getattr(client, command)(args.ticket_id, reason=args.reason)
        elif command == "inspector_kick_back":
            response = client.inspector_kick_back(
                args.ticket_id,
                recommendations=args.recommendations,
                target_assignee=args.target_assignee,
            )
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
    except TicketBoardWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(_ticket_from_response(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
