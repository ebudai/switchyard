#!/usr/bin/env python3
"""Regression test: board write client uses action routes with caller headers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import write_client as write_client_module
from scripts.ticket_board.server import CALLER_ROLE_HEADER
from scripts.ticket_board.write_client import TicketBoardWriteClient


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
            "state": payload.get("state", "analysis"),
            "assignee": payload.get("assignee", caller or "director"),
            "blocked_by": payload.get("blocked_by", []),
            "blocked_reason": payload.get("blocked_reason", ""),
            "comments": [],
            "manually_controlled": bool(payload.get("manually_controlled", False)),
            "commit_hash": payload.get("commit_hash", ""),
        }
        if operation == "start_work":
            ticket["state"] = "in_progress"
        elif operation == "submit_to_audit":
            ticket["state"] = "audit"
        elif operation == "audit_sign_off":
            ticket["state"] = "director_review"
            ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "audit_kick_back":
            ticket["state"] = "analysis"
        elif operation == "eric_sign_off":
            ticket["state"] = "director_review"
            if payload.get("text"):
                ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "eric_reopen":
            ticket["state"] = "analysis"
        elif operation == "mark_done":
            ticket["state"] = "done"
        elif operation == "defer":
            ticket["state"] = "backlog"
        elif operation == "cancel":
            ticket["state"] = "cancelled"
        elif operation == "add_comment":
            ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "file_bug":
            ticket["parent_id"] = payload.get("source_ticket_id", "")
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


def request_pairs(requests: list[tuple[str, str | None, dict[str, object]]]) -> list[tuple[str, str | None]]:
    return [(path, caller_role) for path, caller_role, _ in requests]


def assert_action_requests(requests: list[tuple[str, str | None, dict[str, object]]]) -> None:
    assert requests, "client should send requests"
    pairs = request_pairs(requests)
    for path, caller_role in pairs:
        assert caller_role, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)
    assert ("/api/tickets/actions/create_ticket", "director") in pairs
    assert ("/api/tickets/actions/file_bug", "ops") in pairs
    assert ("/api/tickets/PGU-100/actions/route", "director") in pairs
    assert ("/api/tickets/PGU-101/actions/start_work", "ops") in pairs
    assert ("/api/tickets/PGU-101/actions/submit_to_audit", "ops") in pairs
    assert ("/api/tickets/PGU-102/actions/audit_sign_off", "audit") in pairs
    assert ("/api/tickets/PGU-103/actions/audit_kick_back", "audit") in pairs
    assert ("/api/tickets/PGU-104/actions/eric_sign_off", "eric") in pairs
    assert ("/api/tickets/PGU-105/actions/eric_reopen", "eric") in pairs
    assert ("/api/tickets/PGU-106/actions/mark_done", "director") in pairs
    assert ("/api/tickets/PGU-107/actions/defer", "director") in pairs
    assert ("/api/tickets/PGU-108/actions/cancel", "director") in pairs
    assert ("/api/tickets/PGU-109/actions/set_manually_controlled", "director") in pairs
    assert ("/api/tickets/PGU-111/actions/set_blockers", "director") in pairs
    assert ("/api/tickets/PGU-112/actions/add_comment", "director") in pairs


def exercise_client(base_url: str, commit_hash: str) -> None:
    client = TicketBoardWriteClient(base_url, "director")
    assert client.create_ticket(title="Client create", body="Created.", assignee="director")["ticket"]["id"] == "PGU-NEW"
    assert client.file_bug(title="Bug", body="Body", source_ticket_id="PGU-NEW", caller_role="ops")["ticket"]["parent_id"] == "PGU-NEW"
    assert client.route("PGU-100", state="backlog", assignee="ops")["ticket"]["state"] == "backlog"
    assert client.start_work("PGU-101", caller_role="ops")["ticket"]["state"] == "in_progress"
    assert client.submit_to_audit("PGU-101", commit_hash=commit_hash, caller_role="ops")["ticket"]["state"] == "audit"
    assert client.submit_to_audit("PGU-113", caller_role="ops")["ticket"]["commit_hash"] == ""
    audit_signed = client.audit_sign_off("PGU-102", text="Audit verified.", caller_role="audit")
    assert audit_signed["ticket"]["state"] == "director_review"
    assert audit_signed["ticket"]["comments"][-1]["text"] == "Audit verified."
    assert client.audit_kick_back("PGU-103", reason="Needs another pass.", caller_role="audit")["ticket"]["state"] == "analysis"
    eric_signed = client.eric_sign_off("PGU-104", text="Eric approves.", caller_role="eric")
    assert eric_signed["ticket"]["state"] == "director_review"
    assert eric_signed["ticket"]["comments"][-1]["text"] == "Eric approves."
    assert client.eric_reopen("PGU-105", reason="Needs design revision.", caller_role="eric")["ticket"]["state"] == "analysis"
    assert client.mark_done("PGU-106", commit_hash=commit_hash)["ticket"]["state"] == "done"
    assert client.mark_done("PGU-114")["ticket"]["commit_hash"] == ""
    assert client.defer("PGU-107")["ticket"]["state"] == "backlog"
    assert client.cancel("PGU-108", reason="No longer needed.")["ticket"]["state"] == "cancelled"
    assert client.set_manually_controlled("PGU-109", True)["ticket"]["manually_controlled"] is True
    assert client.set_blockers("PGU-111", blocked_by=["PGU-110"], blocked_reason="Waiting.")["ticket"]["blocked_by"] == ["PGU-110"]
    assert client.add_comment("PGU-112", text="Director note.")["ticket"]["comments"][-1]["who"] == "director"


def assert_auto_socket_connect_failure_falls_back_to_tcp(base_url: str, socket_path: Path) -> None:
    socket_path.touch()
    socket_path.chmod(0)
    old_default_url = write_client_module.DEFAULT_BOARD_URL
    old_default_socket = write_client_module.DEFAULT_BOARD_SOCKET
    old_env_socket = os.environ.pop("PGU_TICKET_BOARD_SOCKET", None)
    try:
        write_client_module.DEFAULT_BOARD_URL = base_url
        write_client_module.DEFAULT_BOARD_SOCKET = str(socket_path)
        client = TicketBoardWriteClient(base_url, "ops")
        assert client.start_work("PGU-120")["ticket"]["state"] == "in_progress"
        explicit_client = TicketBoardWriteClient(base_url, "ops", socket_path=str(socket_path))
        try:
            explicit_client.start_work("PGU-120")
        except OSError:
            pass
        else:
            raise AssertionError("explicit socket connection failure should not fall back to TCP")
    finally:
        write_client_module.DEFAULT_BOARD_URL = old_default_url
        write_client_module.DEFAULT_BOARD_SOCKET = old_default_socket
        if old_env_socket is not None:
            os.environ["PGU_TICKET_BOARD_SOCKET"] = old_env_socket
        socket_path.unlink(missing_ok=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-write-client.") as tmpdir:
        root = Path(tmpdir)
        server = RecordingServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            exercise_client(base_url, "abcdef1")
            assert_auto_socket_connect_failure_falls_back_to_tcp(base_url, root / "blocked.sock")
            assert_action_requests(server.requests)
            assert ("/api/tickets/PGU-120/actions/start_work", "ops") in request_pairs(server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("ticket_board_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
