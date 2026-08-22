#!/usr/bin/env python3
"""Regression test: ticket-board-read is a read-only formatted board CLI."""

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
            self._send_json({"tickets": self._tickets(), "errors": [], "states": [], "assignees": []})
            return
        if self.path == "/api/tickets/PGU-506":
            self._send_json(self._ticket("PGU-506"))
            return
        self._send_text("ticket not found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        self.server.posts.append(self.path)  # type: ignore[attr-defined]
        self._send_text("writes forbidden in read client test", HTTPStatus.METHOD_NOT_ALLOWED)

    def _tickets(self) -> list[dict[str, object]]:
        return [
            self._ticket("PGU-506"),
            self._ticket("PGU-507", state="backlog", assignee="ops", title="Queued ops work"),
            self._ticket("PGU-508", state="done", assignee="ops", title="Done ticket"),
            self._ticket("PGU-509", state="director_review", assignee="director", title="Needs director"),
            self._ticket("PGU-510", state="analysis", assignee="app", title="Awaiting director", awaiting_role="director"),
        ]

    def _ticket(
        self,
        ticket_id: str,
        *,
        state: str = "in_progress",
        assignee: str = "ops",
        title: str = "Read client fixture",
        awaiting_role: str = "",
    ) -> dict[str, object]:
        return {
            "id": ticket_id,
            "title": title,
            "body": "Ticket body.",
            "implementation": "Implementation notes.",
            "state": state,
            "assignee": assignee,
            "awaiting_role": awaiting_role,
            "blocked_by": ["PGU-500"] if ticket_id == "PGU-506" else [],
            "blockers": [{"id": "PGU-500", "resolved": False}] if ticket_id == "PGU-506" else [],
            "blocked_reason": "Waiting on PGU-500." if ticket_id == "PGU-506" else "",
            "needs_inspection": True,
            "inspector_signoff": False,
            "needs_user_signoff": True,
            "user_signoff": False,
            "audit_signoff": False,
            "manually_controlled": False,
            "regression": True,
            "commit_hash": "abc1234",
            "comments": [
                {"who": "audit", "ts": "2026-07-17T02:00:00+00:00", "text": "Second comment."},
                {"who": "ops", "ts": "2026-07-17T01:00:00+00:00", "text": "First comment."},
            ],
        }

    def _send_json(self, payload: object) -> None:
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


def run_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = read_client_module.main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def assert_source_is_read_only() -> None:
    source = (ROOT / "scripts" / "ticket_board" / "read_client.py").read_text(encoding="utf-8")
    assert "write_client" not in source
    assert "UnixHTTPConnection" not in source
    assert "register-caller" not in source
    assert "POST" not in source


def assert_client_reads_ticket_and_board(base_url: str, server: RecordingServer) -> None:
    client = TicketBoardReadClient(base_url)
    ticket = client.get_ticket("pgu-506")
    assert ticket["id"] == "PGU-506", ticket
    board = client.get_board()
    assert len(board["tickets"]) == 5, board
    assert "/api/tickets/PGU-506" in server.gets
    assert "/api/board" in server.gets
    assert server.posts == []


def assert_ticket_and_comments_output(base_url: str) -> None:
    code, stdout, stderr = run_cli(["--board-url", base_url, "ticket", "pgu-506"])
    assert code == 0, stderr
    assert "PGU-506 [in_progress/ops] Read client fixture" in stdout
    assert "needs_inspection: True" in stdout
    assert "blocked_by: PGU-500" in stdout
    assert "Body:" in stdout and "Ticket body." in stdout
    assert "Implementation:" in stdout and "Implementation notes." in stdout
    assert stdout.index("First comment.") < stdout.index("Second comment.")

    code, stdout, stderr = run_cli(["--board-url", base_url, "PGU-506", "--json"])
    assert code == 0, stderr
    assert json.loads(stdout)["id"] == "PGU-506"

    code, stdout, stderr = run_cli(["--board-url", base_url, "comments", "PGU-506"])
    assert code == 0, stderr
    assert "First comment." in stdout
    assert "Ticket body." not in stdout

    code, stdout, stderr = run_cli(["--board-url", base_url, "comments", "PGU-506", "--json"])
    assert code == 0, stderr
    assert [comment["text"] for comment in json.loads(stdout)] == ["First comment.", "Second comment."]


def assert_queue_board_blocker_director_output(base_url: str) -> None:
    code, stdout, stderr = run_cli(["--board-url", base_url, "queue", "ops"])
    assert code == 0, stderr
    assert "ops:" in stdout
    assert stdout.index("PGU-506") < stdout.index("PGU-507")
    assert "PGU-508" not in stdout

    code, stdout, stderr = run_cli(["--board-url", base_url, "board"])
    assert code == 0, stderr
    assert "in_progress:" in stdout
    assert "backlog:" in stdout
    assert "PGU-508" not in stdout

    code, stdout, stderr = run_cli(["--board-url", base_url, "board", "--all", "--json"])
    assert code == 0, stderr
    assert json.loads(stdout)["done"][0]["id"] == "PGU-508"

    code, stdout, stderr = run_cli(["--board-url", base_url, "blockers", "PGU-506"])
    assert code == 0, stderr
    assert "blocked_by: PGU-500" in stdout
    assert "blocked_reason: Waiting on PGU-500." in stdout

    code, stdout, stderr = run_cli(["--board-url", base_url, "director"])
    assert code == 0, stderr
    assert "PGU-509" in stdout
    assert "PGU-510" in stdout


def assert_errors_are_reported(base_url: str) -> None:
    client = TicketBoardReadClient(base_url)
    try:
        client.get_ticket("PGU-999")
    except TicketBoardReadError as exc:
        assert "ticket not found" in str(exc), exc
    else:
        raise AssertionError("unknown ticket unexpectedly succeeded")

    code, _, stderr = run_cli(["--board-url", base_url])
    assert code == 1
    assert "subcommand required" in stderr


def main() -> int:
    assert_source_is_read_only()
    server = RecordingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert_client_reads_ticket_and_board(base_url, server)
        assert_ticket_and_comments_output(base_url)
        assert_queue_board_blocker_director_output(base_url)
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
