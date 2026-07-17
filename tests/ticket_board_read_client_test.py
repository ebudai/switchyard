#!/usr/bin/env python3
"""Regression test: ticket-board-read performs read-only board API calls."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import read_client as read_client_module
from scripts.ticket_board.read_client import TicketBoardReadClient, TicketBoardReadError


class RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self.server.gets.append(self.path)  # type: ignore[attr-defined]
        if self.path == "/api/board":
            self._send_json(
                {
                    "tickets": [self._ticket("PGU-506")],
                    "errors": [],
                    "states": ["analysis"],
                    "assignees": ["ops"],
                }
            )
            return
        if self.path == "/api/tickets/PGU-506":
            self._send_json(self._ticket("PGU-506"))
            return
        self._send_text("ticket not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        self.server.posts.append(self.path)  # type: ignore[attr-defined]
        self._send_text("writes forbidden in read client test", HTTPStatus.METHOD_NOT_ALLOWED)

    def _ticket(self, ticket_id: str) -> dict[str, object]:
        return {
            "id": ticket_id,
            "title": "Read client fixture",
            "state": "in_progress",
            "assignee": "ops",
            "blocked_by": [],
            "blocked_reason": "",
            "comments": [],
        }

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: HTTPStatus) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RecordingServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[str] = []
        super().__init__(("127.0.0.1", 0), RecordingHandler)


def assert_client_reads_ticket_and_board(base_url: str, server: RecordingServer) -> None:
    client = TicketBoardReadClient(base_url)
    ticket = client.get_ticket("pgu-506")
    assert ticket["id"] == "PGU-506", ticket
    board = client.get_board()
    assert board["tickets"][0]["id"] == "PGU-506", board
    assert "/api/tickets/PGU-506" in server.gets
    assert "/api/board" in server.gets
    assert server.posts == []


def assert_cli_prints_json(base_url: str) -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert read_client_module.main(["--board-url", base_url, "--compact", "PGU-506"]) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["id"] == "PGU-506", payload

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert read_client_module.main(["--board-url", base_url, "ticket", "pgu-506"]) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["id"] == "PGU-506", payload

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert read_client_module.main(["--board-url", base_url, "board"]) == 0
    payload = json.loads(stdout.getvalue())
    assert payload["tickets"][0]["id"] == "PGU-506", payload


def assert_errors_are_reported(base_url: str) -> None:
    client = TicketBoardReadClient(base_url)
    try:
        client.get_ticket("PGU-999")
    except TicketBoardReadError as exc:
        assert "ticket not found" in str(exc), exc
    else:
        raise AssertionError("unknown ticket unexpectedly succeeded")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        assert read_client_module.main(["--board-url", base_url]) == 1
    assert "ticket id required" in stderr.getvalue(), stderr.getvalue()


def main() -> int:
    server = RecordingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert_client_reads_ticket_and_board(base_url, server)
        assert_cli_prints_json(base_url)
        assert_errors_are_reported(base_url)
        assert server.posts == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("ticket_board_read_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
