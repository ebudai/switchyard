#!/usr/bin/env python3
"""Regression test: board write client uses action routes with caller headers."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import subprocess
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
        elif operation == "submit_to_inspection":
            ticket["state"] = "inspection"
            ticket["assignee"] = "inspector"
        elif operation in {"force_move", "override_move"}:
            ticket["state"] = payload.get("state", "analysis")
            ticket["assignee"] = payload.get("assignee", caller or "director")
        elif operation == "submit_to_audit":
            ticket["state"] = "audit"
        elif operation == "request_commit_exempt":
            ticket["state"] = "analysis"
            ticket["assignee"] = "unassigned"
            ticket["comments"] = [{"who": caller, "text": f"Commit exemption requested: {payload.get('reason', '')}"}]
        elif operation == "start_task":
            ticket["state"] = "analysis"
            if payload.get("text"):
                ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "complete_task":
            ticket["state"] = "done"
            ticket["commit_exempt"] = True
            ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "await_role":
            ticket["awaiting_role"] = payload.get("role", "")
        elif operation == "clear_awaiting_role":
            ticket["awaiting_role"] = ""
        elif operation == "audit_sign_off":
            ticket["state"] = "director_review"
            ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "audit_kick_back":
            ticket["state"] = "in_progress"
            ticket["assignee"] = payload.get("target_assignee", payload.get("assignee", "ops"))
        elif operation == "director_dat_sign_off":
            ticket["state"] = "eric_review"
            if payload.get("text"):
                ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "director_dat_kick_back":
            ticket["state"] = "in_progress"
            ticket["assignee"] = payload.get("target_assignee", payload.get("assignee", "ops"))
            if payload.get("reason"):
                ticket["comments"] = [{"who": caller, "text": payload.get("reason", "")}]
        elif operation == "eric_sign_off":
            ticket["state"] = "director_review"
            if payload.get("text"):
                ticket["comments"] = [{"who": caller, "text": payload.get("text", "")}]
        elif operation == "eric_reopen":
            ticket["state"] = "analysis"
        elif operation == "release_draft":
            ticket["state"] = "analysis"
        elif operation == "mark_done":
            ticket["state"] = "done"
        elif operation == "defer":
            ticket["state"] = "backlog"
        elif operation == "cancel":
            ticket["state"] = "cancelled"
        elif operation == "add_comment":
            ticket["comments"] = [{"who": caller, "text": payload.get("text", ""), "urgent": bool(payload.get("urgent", False))}]
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


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def setup_git_repo(root: Path) -> tuple[Path, str, str]:
    origin = root / "origin.git"
    repo = root / "repo"
    run_git(root, "init", "--bare", str(origin))
    run_git(root, "clone", str(origin), str(repo))
    run_git(repo, "config", "core.hooksPath", "")
    run_git(repo, "config", "user.name", "PGU Test")
    run_git(repo, "config", "user.email", "pgu-test@example.com")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "Initial commit")
    run_git(repo, "branch", "-M", "main")
    run_git(repo, "push", "-u", "origin", "main")
    pushed_hash = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "README.md").write_text("initial\nlocal only\n", encoding="utf-8")
    run_git(repo, "commit", "-am", "Local-only commit")
    local_only_hash = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, pushed_hash, local_only_hash


@contextlib.contextmanager
def pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def assert_action_requests(requests: list[tuple[str, str | None, dict[str, object]]]) -> None:
    assert requests, "client should send requests"
    pairs = request_pairs(requests)
    for path, caller_role in pairs:
        assert caller_role, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)
    assert ("/api/tickets/actions/create_ticket", "director") in pairs
    assert ("/api/tickets/actions/file_bug", "ops") in pairs
    assert ("/api/tickets/PGU-100/actions/route", "director") in pairs
    assert ("/api/tickets/PGU-115/actions/force_move", "director") in pairs
    assert ("/api/tickets/PGU-116/actions/override_move", "director") in pairs
    assert ("/api/tickets/PGU-101/actions/start_work", "ops") in pairs
    assert ("/api/tickets/PGU-101/actions/submit_to_inspection", "ops") in pairs
    assert ("/api/tickets/PGU-101/actions/submit_to_audit", "ops") in pairs
    assert ("/api/tickets/PGU-121/actions/request_commit_exempt", "ops") in pairs
    assert ("/api/tickets/PGU-124/actions/start_task", "inspector") in pairs
    assert ("/api/tickets/PGU-124/actions/complete_task", "inspector") in pairs
    assert ("/api/tickets/PGU-123/actions/await_role", "ops") in pairs
    assert ("/api/tickets/PGU-123/actions/clear_awaiting_role", "ops") in pairs
    assert ("/api/tickets/PGU-102/actions/audit_sign_off", "audit") in pairs
    assert ("/api/tickets/PGU-103/actions/audit_kick_back", "audit") in pairs
    assert ("/api/tickets/PGU-102/actions/director_dat_sign_off", "director") in pairs
    assert ("/api/tickets/PGU-102/actions/director_dat_kick_back", "director") in pairs
    assert ("/api/tickets/PGU-104/actions/eric_sign_off", "eric") in pairs
    assert ("/api/tickets/PGU-105/actions/eric_reopen", "eric") in pairs
    assert ("/api/tickets/PGU-140/actions/release_draft", "eric") in pairs
    assert ("/api/tickets/PGU-106/actions/mark_done", "director") in pairs
    assert ("/api/tickets/PGU-107/actions/defer", "director") in pairs
    assert ("/api/tickets/PGU-108/actions/cancel", "director") in pairs
    assert ("/api/tickets/PGU-109/actions/set_manually_controlled", "director") in pairs
    assert ("/api/tickets/PGU-111/actions/set_blockers", "director") in pairs
    assert ("/api/tickets/PGU-112/actions/add_comment", "director") in pairs


def exercise_client(base_url: str, repo: Path, commit_hash: str) -> None:
    with pushd(repo):
        client = TicketBoardWriteClient(base_url, "director")
        assert client.create_ticket(title="Client create", body="Created.", assignee="director")["ticket"]["id"] == "PGU-NEW"
        assert client.file_bug(title="Bug", body="Body", source_ticket_id="PGU-NEW", caller_role="ops")["ticket"]["parent_id"] == "PGU-NEW"
        assert client.route("PGU-100", state="backlog", assignee="ops")["ticket"]["state"] == "backlog"
        forced = client.force_move("PGU-115", state="done", assignee="director", suppress_notification=True)
        assert forced["ticket"]["state"] == "done"
        assert forced["ticket"]["assignee"] == "director"
        overridden = client.override_move("PGU-116", state="cancelled", assignee="director", notify=False)
        assert overridden["ticket"]["state"] == "cancelled"
        assert overridden["ticket"]["assignee"] == "director"
        assert client.start_work("PGU-101", caller_role="ops")["ticket"]["state"] == "in_progress"
        inspected = client.submit_to_inspection("PGU-101", caller_role="ops")
        assert inspected["ticket"]["state"] == "inspection"
        assert inspected["ticket"]["assignee"] == "inspector"
        assert client.submit_to_audit("PGU-101", commit_hash=commit_hash, caller_role="ops")["ticket"]["state"] == "audit"
        assert client.submit_to_audit("PGU-113", caller_role="ops")["ticket"]["commit_hash"] == ""
        requested = client.request_commit_exempt("PGU-121", reason="No repo change for this ticket.", caller_role="ops")
        assert requested["ticket"]["state"] == "analysis"
        assert requested["ticket"]["assignee"] == "unassigned"
        assert "No repo change" in requested["ticket"]["comments"][-1]["text"]
        task_started = client.start_task("PGU-124", text="Starting inspection utility.", caller_role="inspector")
        assert task_started["ticket"]["state"] == "analysis"
        assert task_started["ticket"]["comments"][-1]["text"] == "Starting inspection utility."
        task_completed = client.complete_task("PGU-124", text="Inspection utility complete.", caller_role="inspector")
        assert task_completed["ticket"]["state"] == "done"
        assert task_completed["ticket"]["commit_exempt"] is True
        assert task_completed["ticket"]["comments"][-1]["text"] == "Inspection utility complete."
        assert client.await_role("PGU-123", role="perf", caller_role="ops")["ticket"]["awaiting_role"] == "perf"
        assert client.clear_awaiting_role("PGU-123", caller_role="ops")["ticket"]["awaiting_role"] == ""
        audit_signed = client.audit_sign_off("PGU-102", text="Audit verified.", caller_role="audit")
        assert audit_signed["ticket"]["state"] == "director_review"
        assert audit_signed["ticket"]["comments"][-1]["text"] == "Audit verified."
        dat_signed = client.director_dat_sign_off("PGU-102", text="DAT accepted.", caller_role="director")
        assert dat_signed["ticket"]["state"] == "eric_review"
        assert dat_signed["ticket"]["comments"][-1]["text"] == "DAT accepted."
        dat_kicked = client.director_dat_kick_back(
            "PGU-102",
            reason="Artifact visible.",
            target_assignee="app",
            caller_role="director",
        )
        assert dat_kicked["ticket"]["state"] == "in_progress"
        assert dat_kicked["ticket"]["assignee"] == "app"
        assert dat_kicked["ticket"]["comments"][-1]["text"] == "Artifact visible."
        audit_kicked = client.audit_kick_back("PGU-103", reason="Needs another pass.", target_assignee="ops", caller_role="audit")
        assert audit_kicked["ticket"]["state"] == "in_progress"
        assert audit_kicked["ticket"]["assignee"] == "ops"
        eric_signed = client.eric_sign_off("PGU-104", text="Eric approves.", caller_role="eric")
        assert eric_signed["ticket"]["state"] == "director_review"
        assert eric_signed["ticket"]["comments"][-1]["text"] == "Eric approves."
        assert client.eric_reopen("PGU-105", reason="Needs design revision.", caller_role="eric")["ticket"]["state"] == "analysis"
        assert client.release_draft("PGU-140", caller_role="eric")["ticket"]["state"] == "analysis"
        assert client.mark_done("PGU-106", commit_hash=commit_hash)["ticket"]["state"] == "done"
        assert client.mark_done("PGU-114")["ticket"]["commit_hash"] == ""
        assert client.defer("PGU-107")["ticket"]["state"] == "backlog"
        assert client.cancel("PGU-108", reason="No longer needed.")["ticket"]["state"] == "cancelled"
        assert client.set_manually_controlled("PGU-109", True)["ticket"]["manually_controlled"] is True
        assert client.set_blockers("PGU-111", blocked_by=["PGU-110"], blocked_reason="Waiting.")["ticket"]["blocked_by"] == ["PGU-110"]
        assert client.add_comment("PGU-112", text="Director note.")["ticket"]["comments"][-1]["who"] == "director"
        assert client.add_comment("PGU-112", text="Urgent director note.", urgent=True)["ticket"]["comments"][-1]["urgent"] is True


def assert_submit_rejects_unpushed_commit(
    base_url: str,
    repo: Path,
    local_only_hash: str,
    requests: list[tuple[str, str | None, dict[str, object]]],
) -> None:
    with pushd(repo):
        client = TicketBoardWriteClient(base_url, "director")
        before = len(requests)
        try:
            client.submit_to_audit("PGU-122", commit_hash=local_only_hash, caller_role="ops")
        except write_client_module.TicketBoardWriteError as exc:
            message = str(exc)
        else:
            raise AssertionError("local-only commit unexpectedly submitted to audit")
        assert "is not pushed to origin" in message, message
        assert len(requests) == before, requests[before:]


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
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            assert client.start_work("PGU-120")["ticket"]["state"] == "in_progress"
        warning = stderr.getvalue()
        assert "WARNING: ticket board Unix socket" in warning, warning
        assert str(socket_path) in warning, warning
        assert "falling back to TCP" in warning, warning
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


def assert_socket_discovery_prefers_runtime_then_legacy(root: Path) -> None:
    runtime_socket = root / "run" / "pgu-ticket-board" / "ticket-board.sock"
    legacy_socket = root / "pgu-ticket-board.sock"
    runtime_socket.parent.mkdir(parents=True)
    old_default_url = write_client_module.DEFAULT_BOARD_URL
    old_default_socket = write_client_module.DEFAULT_BOARD_SOCKET
    old_legacy_socket = write_client_module.LEGACY_BOARD_SOCKET
    old_env_socket = os.environ.pop("PGU_TICKET_BOARD_SOCKET", None)
    try:
        write_client_module.DEFAULT_BOARD_URL = "http://127.0.0.1:8770"
        write_client_module.DEFAULT_BOARD_SOCKET = str(runtime_socket)
        write_client_module.LEGACY_BOARD_SOCKET = str(legacy_socket)
        assert TicketBoardWriteClient("http://127.0.0.1:8770", "ops").effective_socket_path is None
        legacy_socket.touch()
        assert TicketBoardWriteClient("http://127.0.0.1:8770", "ops").effective_socket_path == str(legacy_socket)
        runtime_socket.touch()
        assert TicketBoardWriteClient("http://127.0.0.1:8770", "ops").effective_socket_path == str(runtime_socket)
    finally:
        write_client_module.DEFAULT_BOARD_URL = old_default_url
        write_client_module.DEFAULT_BOARD_SOCKET = old_default_socket
        write_client_module.LEGACY_BOARD_SOCKET = old_legacy_socket
        if old_env_socket is not None:
            os.environ["PGU_TICKET_BOARD_SOCKET"] = old_env_socket


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-write-client.") as tmpdir:
        root = Path(tmpdir)
        repo, pushed_hash, local_only_hash = setup_git_repo(root)
        server = RecordingServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            exercise_client(base_url, repo, pushed_hash)
            assert_submit_rejects_unpushed_commit(base_url, repo, local_only_hash, server.requests)
            assert_socket_discovery_prefers_runtime_then_legacy(root)
            assert_auto_socket_connect_failure_falls_back_to_tcp(base_url, root / "blocked.sock")
            assert_action_requests(server.requests)
            assert ("/api/tickets/PGU-120/actions/start_work", "ops") in request_pairs(server.requests)
            assert all(path != "/api/tickets/PGU-122/actions/submit_to_audit" for path, _, _ in server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("ticket_board_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
