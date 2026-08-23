#!/usr/bin/env python3
"""Regression test: Codex panes write through the board API as their own roles."""

from __future__ import annotations

import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib import error as urllib_error
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.server import CALLER_ROLE_HEADER
from scripts.ticket_board.write_client import TicketBoardWriteClient, default_caller_role
from scripts.team_launcher import cli_command_for_role, load_project_config

LIVE_BOARD_URL = "http://127.0.0.1:8770"


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
        elif operation == "submit_to_inspection":
            ticket["state"] = "inspection"
            ticket["assignee"] = "inspector"
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


class UnixRecordingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        caller = self.headers.get(CALLER_ROLE_HEADER)
        self.server.requests.append((self.path, caller, payload))  # type: ignore[attr-defined]
        if self.path == "/api/register-caller":
            body = json.dumps({"ok": True}).encode("utf-8")
        else:
            body = json.dumps(
                {
                    "ticket": {
                        "id": "OTTO-NEW",
                        "title": payload.get("title", "fixture"),
                        "state": payload.get("state", "analysis"),
                        "assignee": payload.get("assignee", caller or "unassigned"),
                        "comments": [],
                    }
                }
            ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingUnixRecordingServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path) -> None:
        self.requests: list[tuple[str, str | None, dict[str, object]]] = []
        self.socket_path = socket_path
        socket_path.unlink(missing_ok=True)
        super().__init__(str(socket_path), UnixRecordingHandler)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if args and args[0] == "push":
        env["ALLOW_MAIN_PUSH"] = "director"
        env["PGU_ALLOW_MAIN_PUSH"] = "director"
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True, env=env)


def setup_git_repo(root: Path) -> tuple[Path, str, str]:
    origin = root / "origin.git"
    repo = root / "repo"
    run_git(root, "init", "--bare", str(origin))
    run_git(root, "clone", str(origin), str(repo))
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


def strip_board_endpoint_env(env: dict[str, str]) -> None:
    for key in list(env):
        if key.startswith("PGU_TICKET_BOARD_") or key.startswith("TICKET_BOARD_"):
            env.pop(key)


def dead_board_env(root: Path) -> dict[str, str]:
    return {
        "TICKET_BOARD_SOCKET": str(root / "missing-ticket-board.sock"),
        "PGU_TICKET_BOARD_SOCKET": str(root / "missing-legacy-ticket-board.sock"),
        "TICKET_BOARD_URL": "http://127.0.0.1:1",
        "PGU_TICKET_BOARD_URL": "http://127.0.0.1:1",
    }


def live_board_ticket_count() -> int | None:
    try:
        with request.urlopen(f"{LIVE_BOARD_URL}/api/board", timeout=2.0) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib_error.URLError):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    tickets = parsed.get("tickets")
    if not isinstance(tickets, list):
        return None
    return len(tickets)


def run_cli(base_url: str, caller_role: str, cwd: Path, *args: str) -> dict[str, object]:
    env = os.environ.copy()
    strip_board_endpoint_env(env)
    env["TICKET_BOARD_CALLER_ROLE"] = caller_role
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ticket-board-write"), "--board-url", base_url, *args],
        cwd=cwd,
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


def run_cli_error(base_url: str, caller_role: str, cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    strip_board_endpoint_env(env)
    env["TICKET_BOARD_CALLER_ROLE"] = caller_role
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ticket-board-write"), "--board-url", base_url, *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        raise AssertionError(proc.stdout)
    return proc.stderr or proc.stdout


def command_env(command: list[str]) -> dict[str, str]:
    assert command[0] == "env"
    result: dict[str, str] = {}
    for token in command[1:]:
        if "=" not in token:
            break
        key, value = token.split("=", 1)
        result[key] = value
    return result


def exercise_pane_cli(base_url: str, repo: Path, commit_hash: str, local_only_hash: str) -> None:
    assert default_caller_role({"TICKET_BOARD_CALLER_ROLE": " ops ", "PGU_TICKET_BOARD_CALLER_ROLE": " app "}) == "ops"
    assert default_caller_role({"PGU_TICKET_BOARD_CALLER_ROLE": " app "}) == "app"
    assert TicketBoardWriteClient(base_url, "director").for_caller("ops").caller_role == "ops"

    started = run_cli(base_url, "main", repo, "start-work", "PGU-2100")
    assert started["state"] == "in_progress", started
    submitted = run_cli(base_url, "main", repo, "submit-to-audit", "PGU-2100", "--commit-hash", commit_hash)
    assert submitted["state"] == "audit", submitted
    assert submitted["commit_hash"] == commit_hash, submitted
    submitted_to_inspection = run_cli(base_url, "main", repo, "submit-to-inspection", "PGU-2105")
    assert submitted_to_inspection["state"] == "inspection", submitted_to_inspection
    assert submitted_to_inspection["assignee"] == "inspector", submitted_to_inspection
    exempt_submitted = run_cli(base_url, "main", repo, "submit-to-audit", "PGU-2103")
    assert exempt_submitted["state"] == "audit", exempt_submitted
    assert exempt_submitted["commit_hash"] == "", exempt_submitted
    rejected = run_cli_error(base_url, "main", repo, "submit-to-audit", "PGU-2104", "--commit-hash", local_only_hash)
    assert "is not pushed to origin" in rejected, rejected

    filed = run_cli(
        base_url,
        "app",
        repo,
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

    commented = run_cli(base_url, "ops", repo, "add-comment", "PGU-2102", "--text", "Ops pane note.")
    assert commented["comments"][-1]["who"] == "ops", commented


def assert_second_project_pane_cli_uses_project_socket(root: Path) -> None:
    socket_path = root / "otto.sock"
    layout_path = root / "otto-layout.json"
    layout_path.write_text(json.dumps({"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}), encoding="utf-8")
    config_path = root / "otto.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "otto",
                "layout": str(layout_path),
                "repository": str(root),
                "board_url": "http://127.0.0.1:20740",
                "board_socket": str(socket_path),
                "roles": [{"role": "implementer", "slot": 0, "tmux_session": "otto-implementer", "cli": ["codex"]}],
            }
        ),
        encoding="utf-8",
    )
    config = load_project_config("otto", config_path)
    role = config.roles[0]
    pane_env = command_env(cli_command_for_role(role, session_dir=root / "sessions"))

    server = ThreadingUnixRecordingServer(socket_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        strip_board_endpoint_env(env)
        env.update(dead_board_env(root))
        env.update(pane_env)
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "ticket-board-write"),
                "create-ticket",
                "--title",
                "Otto pane write",
                "--body",
                "Created from an otto pane environment.",
                "--assignee",
                "implementer",
            ],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(proc.stderr or proc.stdout)
        parsed = json.loads(proc.stdout)
        assert parsed["id"] == "OTTO-NEW", parsed
        assert ("/api/register-caller", None, {"role": "implementer"}) in server.requests
        assert any(
            path == "/api/tickets/actions/create_ticket" and caller == "implementer"
            for path, caller, _payload in server.requests
        ), server.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def assert_second_project_socket_regression_fails_without_live_board(root: Path) -> None:
    layout_path = root / "broken-otto-layout.json"
    layout_path.write_text(json.dumps({"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}), encoding="utf-8")
    config_path = root / "broken-otto.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "otto",
                "layout": str(layout_path),
                "repository": str(root),
                "board_url": "http://127.0.0.1:20740",
                "board_socket": str(root / "otto.sock"),
                "roles": [{"role": "implementer", "slot": 0, "tmux_session": "otto-implementer", "cli": ["codex"]}],
            }
        ),
        encoding="utf-8",
    )
    config = load_project_config("otto", config_path)
    pane_env = command_env(cli_command_for_role(config.roles[0], session_dir=root / "sessions"))
    pane_env.pop("TICKET_BOARD_SOCKET", None)

    env = os.environ.copy()
    strip_board_endpoint_env(env)
    env.update(dead_board_env(root))
    env.update(pane_env)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "ticket-board-write"),
            "create-ticket",
            "--title",
            "Otto pane write",
            "--body",
            "Created from a broken otto pane environment.",
            "--assignee",
            "implementer",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode != 0, proc.stdout
    assert "invalid local caller role: implementer" not in combined, combined
    assert "FileNotFoundError" in combined, combined


def assert_pane_requests(requests: list[tuple[str, str | None, dict[str, object]]]) -> None:
    pairs = [(path, caller_role) for path, caller_role, _ in requests]
    assert ("/api/tickets/PGU-2100/actions/start_work", "main") in pairs
    assert ("/api/tickets/PGU-2100/actions/submit_to_audit", "main") in pairs
    assert ("/api/tickets/PGU-2105/actions/submit_to_inspection", "main") in pairs
    assert ("/api/tickets/PGU-2103/actions/submit_to_audit", "main") in pairs
    assert ("/api/tickets/actions/file_bug", "app") in pairs
    assert ("/api/tickets/PGU-2102/actions/add_comment", "ops") in pairs
    assert ("/api/tickets/PGU-2104/actions/submit_to_audit", "main") not in pairs
    for path, caller_role in pairs:
        assert caller_role in {"main", "app", "ops"}, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)
    empty_submit = next(payload for path, _, payload in requests if path == "/api/tickets/PGU-2103/actions/submit_to_audit")
    assert empty_submit == {"commit_hash": ""}, empty_submit
    inspection_submit = next(payload for path, _, payload in requests if path == "/api/tickets/PGU-2105/actions/submit_to_inspection")
    assert inspection_submit == {}, inspection_submit


def main() -> int:
    live_count_before = live_board_ticket_count()
    with tempfile.TemporaryDirectory(prefix="ticket-board-pane-write-client.") as tmpdir:
        root = Path(tmpdir)
        repo, pushed_hash, local_only_hash = setup_git_repo(root)
        server = RecordingServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise_pane_cli(f"http://127.0.0.1:{server.server_port}", repo, pushed_hash, local_only_hash)
            assert_second_project_pane_cli_uses_project_socket(root)
            assert_second_project_socket_regression_fails_without_live_board(root)
            assert_pane_requests(server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    live_count_after = live_board_ticket_count()
    assert live_count_after == live_count_before, (live_count_before, live_count_after)
    print("ticket_board_pane_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
