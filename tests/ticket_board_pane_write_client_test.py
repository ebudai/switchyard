#!/usr/bin/env python3
"""Regression test: Codex panes write through the board API as their own roles."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.server import CALLER_ROLE_HEADER
from scripts.ticket_board.write_client import TicketBoardWriteClient, default_caller_role


class RecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        caller = self.headers.get(CALLER_ROLE_HEADER)
        self.server.requests.append((self.path, caller, payload))  # type: ignore[attr-defined]
        ticket_id = "PGU-NEW"
        operation = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/tickets/PGU-") and "/actions/" in self.path:
            ticket_id = self.path.removeprefix("/api/tickets/").split("/actions/", 1)[0]
        ticket = {
            "id": ticket_id,
            "title": payload.get("title", "fixture"),
            "state": "analysis",
            "assignee": caller or "unassigned",
            "parent_id": payload.get("source_ticket_id", ""),
            "commit_hash": payload.get("commit_hash", ""),
            "comments": [],
        }
        if operation == "start_work":
            ticket["state"] = "in_progress"
        elif operation == "submit_to_audit":
            ticket["state"] = "audit"
        elif operation == "file_bug":
            ticket["state"] = "analysis"
        elif operation == "add_comment":
            ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        body = json.dumps({"ticket": ticket}).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RecordingServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None, dict[str, object]]] = []
        super().__init__(("127.0.0.1", 0), RecordingHandler)


def run_cli(base_url: str, caller_role: str, *args: str) -> dict[str, object]:
    env = os.environ.copy()
    env["PGU_TICKET_BOARD_CALLER_ROLE"] = caller_role
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ticket-board-write"), "--board-url", base_url, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    parsed = json.loads(proc.stdout)
    assert isinstance(parsed, dict), parsed
    return parsed


def exercise_pane_cli(base_url: str, commit_hash: str) -> None:
    assert default_caller_role({"PGU_TICKET_BOARD_CALLER_ROLE": " app "}) == "app"
    assert TicketBoardWriteClient(base_url, "director").for_caller("ops").caller_role == "ops"

    started = run_cli(base_url, "main", "start-work", "PGU-2100")
    assert started["state"] == "in_progress", started
    submitted = run_cli(base_url, "main", "submit-to-audit", "PGU-2100", "--commit-hash", commit_hash)
    assert submitted["state"] == "audit", submitted
    assert submitted["commit_hash"] == commit_hash, submitted
    exempt_submitted = run_cli(base_url, "main", "submit-to-audit", "PGU-2103")
    assert exempt_submitted["state"] == "audit", exempt_submitted
    assert exempt_submitted["commit_hash"] == "", exempt_submitted

    filed = run_cli(
        base_url,
        "app",
        "file-bug",
        "--title",
        "App pane bug",
        "--body",
        "Filed through the pane write client.",
        "--source-ticket-id",
        "PGU-2101",
        "--assignee",
        "app",
    )
    assert filed["parent_id"] == "PGU-2101"

    commented = run_cli(base_url, "ops", "add-comment", "PGU-2102", "--text", "Ops pane note.")
    assert commented["comments"][-1]["who"] == "ops", commented


def assert_pane_requests(requests: list[tuple[str, str | None, dict[str, object]]]) -> None:
    pairs = [(path, caller_role) for path, caller_role, _ in requests]
    assert ("/api/tickets/PGU-2100/actions/start_work", "main") in pairs
    assert ("/api/tickets/PGU-2100/actions/submit_to_audit", "main") in pairs
    assert ("/api/tickets/PGU-2103/actions/submit_to_audit", "main") in pairs
    assert ("/api/tickets/actions/file_bug", "app") in pairs
    assert ("/api/tickets/PGU-2102/actions/add_comment", "ops") in pairs
    for path, caller_role in pairs:
        assert caller_role in {"main", "app", "ops"}, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)
    empty_submit = next(payload for path, _, payload in requests if path == "/api/tickets/PGU-2103/actions/submit_to_audit")
    assert empty_submit == {"commit_hash": ""}, empty_submit


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-pane-write-client."):
        server = RecordingServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise_pane_cli(f"http://127.0.0.1:{server.server_port}", "abcdef1")
            assert_pane_requests(server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("ticket_board_pane_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
