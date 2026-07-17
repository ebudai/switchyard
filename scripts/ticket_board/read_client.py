"""HTTP read client for the PGU ticket board API."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse, request

from .write_client import (
    DEFAULT_BOARD_SOCKET,
    DEFAULT_BOARD_URL,
    TicketBoardWriteError,
    UnixHTTPConnection,
    _default_socket_path,
    _has_explicit_socket_path,
    _normalize_api_path,
    _normalize_api_url,
)


class TicketBoardReadError(RuntimeError):
    """Raised when the board rejects a read request."""


@dataclass(frozen=True)
class TicketBoardReadClient:
    board_url: str = DEFAULT_BOARD_URL
    timeout: float = 10.0
    socket_path: str | None = None

    @property
    def api_url(self) -> str:
        return _normalize_api_url(self.board_url)

    @property
    def api_path(self) -> str:
        return _normalize_api_path(self.board_url)

    @property
    def root_url(self) -> str:
        normalized = self.board_url.rstrip("/")
        if normalized.endswith("/api/tickets"):
            return normalized.removesuffix("/api/tickets")
        if normalized.endswith("/api"):
            return normalized.removesuffix("/api")
        return normalized

    @property
    def effective_socket_path(self) -> str | None:
        return self.socket_path or _default_socket_path(self.board_url)

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        encoded_ticket = parse.quote(ticket_id.strip().upper(), safe="")
        if not encoded_ticket:
            raise ValueError("ticket_id must be non-empty")
        return self._get_json(f"/{encoded_ticket}")

    def get_board(self) -> dict[str, Any]:
        return self._get_json("/api/board", absolute_path=True)

    def _get_json(self, path: str, *, absolute_path: bool = False) -> dict[str, Any]:
        socket_path = self.effective_socket_path
        if socket_path:
            try:
                return self._get_unix(path, socket_path, absolute_path=absolute_path)
            except OSError as exc:
                if _has_explicit_socket_path(self.socket_path):
                    raise
                print(
                    f"WARNING: ticket board Unix socket {socket_path} failed ({exc}); falling back to TCP {self.root_url}",
                    file=sys.stderr,
                )
        url = f"{self.root_url}{path}" if absolute_path else f"{self.api_url}{path}"
        try:
            with request.urlopen(url, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise TicketBoardReadError(response_body or f"HTTP {exc.code}") from exc
        return self._parse_object(response_body)

    def _get_unix(self, path: str, socket_path: str, *, absolute_path: bool) -> dict[str, Any]:
        request_path = path if absolute_path else f"{self.api_path}{path}"
        conn = UnixHTTPConnection(socket_path, self.timeout)
        try:
            conn.request("GET", request_path)
            response = conn.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise TicketBoardReadError(response_body or f"HTTP {response.status}")
            return self._parse_object(response_body)
        finally:
            conn.close()

    def _parse_object(self, response_body: str) -> dict[str, Any]:
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise TicketBoardReadError("ticket board response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise TicketBoardReadError("ticket board response was not an object")
        return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read tickets from the board API.")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL, help=f"Board root or /api/tickets URL (default: {DEFAULT_BOARD_URL})")
    parser.add_argument(
        "--socket",
        dest="socket_path",
        default=None,
        help=(
            "Unix-domain board socket for local reads. Defaults to PGU_TICKET_BOARD_SOCKET, "
            f"or {DEFAULT_BOARD_SOCKET} when it exists and --board-url is the default."
        ),
    )
    parser.add_argument("--compact", action="store_true", help="print compact JSON instead of indented JSON")
    parser.add_argument("target", nargs="?", help="ticket id shorthand, `ticket`, or `board`")
    parser.add_argument("ticket_id", nargs="?", help="ticket id when using `ticket PGU-N`")
    return parser


def _print_json(payload: dict[str, Any], *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        client = TicketBoardReadClient(args.board_url, socket_path=args.socket_path)
        target = (args.target or "").strip()
        if target == "board":
            payload = client.get_board()
        else:
            ticket_id = args.ticket_id if target == "ticket" else target
            if not ticket_id:
                raise TicketBoardReadError("ticket id required; use `ticket-board-read PGU-N` or `ticket-board-read board`")
            payload = client.get_ticket(ticket_id)
        _print_json(payload, compact=args.compact)
        return 0
    except (OSError, TicketBoardReadError, TicketBoardWriteError, ValueError) as exc:
        print(f"ticket-board-read: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
