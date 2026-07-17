"""Read-only HTTP client for the PGU ticket board API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable
from urllib import error as urllib_error
from urllib import parse, request

DEFAULT_BOARD_URL = os.environ.get("PGU_TICKET_BOARD_URL", "http://127.0.0.1:8770")
TERMINAL_STATES = {"done", "cancelled"}
ACTIVE_STATES = {"in_progress", "inspection", "audit", "director_review", "eric_review"}
REVIEW_STATES = {"inspection", "audit", "director_review", "eric_review"}


class TicketBoardReadError(RuntimeError):
    """Raised when the board rejects a read request."""


def normalize_api_url(board_url: str) -> str:
    normalized = board_url.rstrip("/")
    if normalized.endswith("/api/tickets"):
        return normalized
    if normalized.endswith("/api"):
        return f"{normalized}/tickets"
    return f"{normalized}/api/tickets"


def root_url(board_url: str) -> str:
    normalized = board_url.rstrip("/")
    if normalized.endswith("/api/tickets"):
        return normalized.removesuffix("/api/tickets")
    if normalized.endswith("/api"):
        return normalized.removesuffix("/api")
    return normalized


def ticket_number(ticket_id: str) -> int:
    match = re.search(r"(\d+)$", str(ticket_id))
    return int(match.group(1)) if match else -1


def sort_tickets(tickets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(tickets, key=lambda ticket: ticket_number(str(ticket.get("id", ""))))


def ticket_ref(ticket: dict[str, Any]) -> str:
    title = str(ticket.get("title", "")).strip()
    return f"{ticket.get('id', '')} [{ticket.get('state', '')}/{ticket.get('assignee', '')}] {title}".rstrip()


def wrap_text(text: str, prefix: str = "  ") -> list[str]:
    lines = str(text or "").splitlines() or [""]
    return [f"{prefix}{line}" if line else prefix.rstrip() for line in lines]


def sorted_comments(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    comments = ticket.get("comments", [])
    if not isinstance(comments, list):
        return []
    return sorted((comment for comment in comments if isinstance(comment, dict)), key=lambda item: str(item.get("ts", "")))


@dataclass(frozen=True)
class TicketBoardReadClient:
    board_url: str = DEFAULT_BOARD_URL
    timeout: float = 10.0

    @property
    def api_url(self) -> str:
        return normalize_api_url(self.board_url)

    @property
    def root_url(self) -> str:
        return root_url(self.board_url)

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        encoded_ticket = parse.quote(ticket_id.strip().upper(), safe="")
        if not encoded_ticket:
            raise ValueError("ticket_id must be non-empty")
        return self._get_json(f"{self.api_url}/{encoded_ticket}")

    def get_board(self) -> dict[str, Any]:
        return self._get_json(f"{self.root_url}/api/board")

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            with request.urlopen(url, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            message = response_body or f"HTTP {exc.code}"
            if exc.code == 404 and "not found" in message.lower():
                raise TicketBoardReadError("ticket not found") from exc
            raise TicketBoardReadError(message) from exc
        except urllib_error.URLError as exc:
            raise TicketBoardReadError(f"board unavailable: {exc.reason}") from exc
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise TicketBoardReadError("ticket board response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise TicketBoardReadError("ticket board response was not an object")
        return parsed


def comments_payload(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted_comments(ticket)


def blockers_payload(ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ticket.get("id", ""),
        "blocked_by": ticket.get("blocked_by", []),
        "blockers": ticket.get("blockers", []),
        "blocked_reason": ticket.get("blocked_reason", ""),
    }


def queue_payload(board: dict[str, Any], role: str | None) -> dict[str, list[dict[str, Any]]]:
    tickets = [ticket for ticket in board_tickets(board) if queue_ticket_matches(ticket)]
    roles = [role] if role else sorted({str(ticket.get("assignee", "unassigned")) or "unassigned" for ticket in tickets})
    return {
        current_role: sort_queue_tickets(ticket for ticket in tickets if str(ticket.get("assignee", "unassigned")) == current_role)
        for current_role in roles
    }


def board_payload(board: dict[str, Any], *, include_all: bool) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ticket in board_tickets(board):
        state = str(ticket.get("state", ""))
        if not include_all and state in TERMINAL_STATES:
            continue
        grouped.setdefault(state, []).append(ticket)
    return {state: sort_tickets(tickets) for state, tickets in grouped.items()}


def director_payload(board: dict[str, Any]) -> list[dict[str, Any]]:
    return sort_tickets(ticket for ticket in board_tickets(board) if needs_director(ticket))


def board_tickets(board: dict[str, Any]) -> list[dict[str, Any]]:
    tickets = board.get("tickets", [])
    if not isinstance(tickets, list):
        return []
    return [ticket for ticket in tickets if isinstance(ticket, dict)]


def queue_ticket_matches(ticket: dict[str, Any]) -> bool:
    state = str(ticket.get("state", ""))
    return state in ACTIVE_STATES or state == "backlog"


def sort_queue_tickets(tickets: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    state_order = {"in_progress": 0, "inspection": 1, "audit": 2, "director_review": 3, "eric_review": 4, "backlog": 5}
    return sorted(tickets, key=lambda ticket: (state_order.get(str(ticket.get("state", "")), 99), ticket_number(str(ticket.get("id", "")))))


def needs_director(ticket: dict[str, Any]) -> bool:
    state = str(ticket.get("state", ""))
    assignee = str(ticket.get("assignee", ""))
    awaiting = str(ticket.get("awaiting_role", ""))
    return state == "director_review" or awaiting == "director" or (assignee == "director" and state in ACTIVE_STATES | {"backlog"})


def format_ticket(ticket: dict[str, Any]) -> str:
    lines = [
        ticket_ref(ticket),
        "",
        "Flags:",
        f"  needs_inspection: {bool(ticket.get('needs_inspection', False))}",
        f"  inspector_signoff: {bool(ticket.get('inspector_signoff', False))}",
        f"  needs_eric_signoff: {bool(ticket.get('needs_eric_signoff', False))}",
        f"  eric_signoff: {bool(ticket.get('eric_signoff', False))}",
        f"  audit_signoff: {bool(ticket.get('audit_signoff', False))}",
        f"  manually_controlled: {bool(ticket.get('manually_controlled', False))}",
        f"  regression: {bool(ticket.get('regression', False))}",
        f"  commit_hash: {ticket.get('commit_hash', '') or '(none)'}",
        "",
        "Blockers:",
        f"  blocked_by: {format_list(ticket.get('blocked_by', []))}",
        f"  blockers: {format_blocker_records(ticket.get('blockers', []))}",
        f"  blocked_reason: {ticket.get('blocked_reason', '') or '(none)'}",
        "",
        "Body:",
        *wrap_text(str(ticket.get("body", "") or "(empty)")),
        "",
        "Implementation:",
        *wrap_text(str(ticket.get("implementation", "") or "(empty)")),
        "",
        "Comments:",
        *format_comments(ticket),
    ]
    return "\n".join(lines)


def format_comments(ticket: dict[str, Any]) -> list[str]:
    comments = sorted_comments(ticket)
    if not comments:
        return ["  (none)"]
    lines: list[str] = []
    for comment in comments:
        who = comment.get("who", "?")
        ts = comment.get("ts", "?")
        text = str(comment.get("text", ""))
        lines.append(f"  [{ts}] {who}:")
        lines.extend(wrap_text(text or "(empty)", "    "))
    return lines


def format_comments_only(ticket: dict[str, Any]) -> str:
    return "\n".join(format_comments(ticket))


def format_queue(payload: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    for role, tickets in payload.items():
        lines.append(f"{role}:")
        if tickets:
            lines.extend(f"  {ticket_ref(ticket)}" for ticket in tickets)
        else:
            lines.append("  (empty)")
    return "\n".join(lines)


def format_board(payload: dict[str, list[dict[str, Any]]]) -> str:
    lines: list[str] = []
    for state in sorted(payload):
        lines.append(f"{state}:")
        tickets = payload[state]
        if tickets:
            lines.extend(f"  {ticket_ref(ticket)}" for ticket in tickets)
        else:
            lines.append("  (empty)")
    return "\n".join(lines)


def format_blockers(payload: dict[str, Any]) -> str:
    lines = [
        str(payload.get("id", "")),
        f"  blocked_by: {format_list(payload.get('blocked_by', []))}",
        f"  blockers: {format_blocker_records(payload.get('blockers', []))}",
        f"  blocked_reason: {payload.get('blocked_reason', '') or '(none)'}",
    ]
    return "\n".join(lines)


def format_director(tickets: list[dict[str, Any]]) -> str:
    if not tickets:
        return "(none)"
    return "\n".join(ticket_ref(ticket) for ticket in tickets)


def format_list(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "(none)"
    return ", ".join(str(item) for item in value)


def format_blocker_records(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "(none)"
    pieces = []
    for item in value:
        if isinstance(item, dict):
            suffix = " resolved" if item.get("resolved") else " unresolved"
            pieces.append(f"{item.get('id', '')}{suffix}")
        else:
            pieces.append(str(item))
    return ", ".join(pieces)


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read tickets from the board API.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Board root or /api/tickets URL (default: {DEFAULT_BOARD_URL})")
    parser.add_argument("--json", action="store_true", help="print raw JSON for the selected read")
    subparsers = parser.add_subparsers(dest="command")

    ticket = subparsers.add_parser("ticket", help="read one full ticket")
    ticket.add_argument("ticket_id")
    ticket.add_argument("--json", action="store_true", help="print raw ticket JSON")

    comments = subparsers.add_parser("comments", help="read one ticket's comment thread")
    comments.add_argument("ticket_id")
    comments.add_argument("--json", action="store_true", help="print raw comments JSON")

    queue = subparsers.add_parser("queue", help="show active/backlog queue by role")
    queue.add_argument("role", nargs="?")
    queue.add_argument("--json", action="store_true", help="print queue JSON")

    board = subparsers.add_parser("board", help="show board tickets grouped by state")
    board.add_argument("--all", action="store_true", help="include done/cancelled tickets")
    board.add_argument("--json", action="store_true", help="print grouped board JSON")

    blockers = subparsers.add_parser("blockers", help="show blocker fields for one ticket")
    blockers.add_argument("ticket_id")
    blockers.add_argument("--json", action="store_true", help="print blocker JSON")

    director = subparsers.add_parser("director", help="show work needing director attention")
    director.add_argument("--json", action="store_true", help="print director queue JSON")
    mine = subparsers.add_parser("mine", help="alias for director")
    mine.add_argument("--json", action="store_true", help="print director queue JSON")

    return parser


def normalize_argv(argv: list[str] | None) -> list[str]:
    raw = list(sys.argv[1:] if argv is None else argv)
    skip_next = False
    for index, item in enumerate(raw):
        if skip_next:
            skip_next = False
            continue
        if item == "--":
            return raw
        if item == "--board-url":
            skip_next = True
            continue
        if item.startswith("-"):
            continue
        if re.fullmatch(r"(?i)pgu-\d+", item):
            return [*raw[:index], "ticket", *raw[index:]]
        return raw
    return raw


def command_wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(normalize_argv(argv))
        client = TicketBoardReadClient(args.board_url)
        command = args.command
        if command is None:
            raise TicketBoardReadError("subcommand required; use ticket, comments, queue, board, blockers, or director")

        if command == "ticket":
            ticket = client.get_ticket(args.ticket_id)
            print_json(ticket) if command_wants_json(args) else print(format_ticket(ticket))
        elif command == "comments":
            ticket = client.get_ticket(args.ticket_id)
            print_json(comments_payload(ticket)) if command_wants_json(args) else print(format_comments_only(ticket))
        elif command == "queue":
            payload = queue_payload(client.get_board(), getattr(args, "role", None))
            print_json(payload) if command_wants_json(args) else print(format_queue(payload))
        elif command == "board":
            payload = board_payload(client.get_board(), include_all=bool(getattr(args, "all", False)))
            print_json(payload) if command_wants_json(args) else print(format_board(payload))
        elif command == "blockers":
            payload = blockers_payload(client.get_ticket(args.ticket_id))
            print_json(payload) if command_wants_json(args) else print(format_blockers(payload))
        elif command in {"director", "mine"}:
            payload = director_payload(client.get_board())
            print_json(payload) if command_wants_json(args) else print(format_director(payload))
        else:
            raise TicketBoardReadError(f"unknown command: {command}")
        return 0
    except (OSError, TicketBoardReadError, ValueError) as exc:
        print(f"ticket-board-read: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
