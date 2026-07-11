#!/usr/bin/env python3
"""Regression test: board write client uses action routes with caller headers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board import write_client as write_client_module
from scripts.ticket_board.server import CALLER_ROLE_HEADER, TicketBoardEventHub, TicketBoardHandler
from scripts.ticket_board.write_client import TicketBoardWriteClient


class QuietNotifier:
    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return


class RecordingHandler(TicketBoardHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.server.requests.append((self.path, self.headers.get(CALLER_ROLE_HEADER)))  # type: ignore[attr-defined]
        super().do_POST()


class RecordingTicketBoardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: TicketBoardApp) -> None:
        self.app = app
        self.events = TicketBoardEventHub(app)
        self.director_notifier = QuietNotifier()
        self.requests: list[tuple[str, str | None]] = []
        super().__init__(address, RecordingHandler)

    def server_close(self) -> None:
        self.events.close()
        self.director_notifier.close()
        super().server_close()


def main_commit() -> str:
    return subprocess.run(
        ["git", "--git-dir=/data/git/pgu.git", "rev-parse", "refs/heads/main"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def seed_ticket(
    store: Path,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    audit_signoff: bool = False,
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
) -> None:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "blocked_by": [],
        "parent_id": "",
        "blocked_reason": "",
        "implementation": implementation,
        "audit_prompt": "",
        "audit_signoff": audit_signoff,
        "needs_eric_signoff": needs_eric_signoff,
        "eric_signoff": eric_signoff,
        "manually_controlled": False,
        "commit_hash": "",
        "commit_exempt": False,
        "created": "2026-07-11T00:00:00+00:00",
        "updated": "2026-07-11T00:00:00+00:00",
        "comments": [],
        "screenshots": [],
    }
    (store / f"{ticket_id}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_ticket(store: Path, ticket_id: str) -> dict[str, object]:
    return json.loads((store / f"{ticket_id}.json").read_text(encoding="utf-8"))


def seed_fixtures(store: Path) -> None:
    seed_ticket(store, "PGU-100", title="Route")
    seed_ticket(store, "PGU-101", title="Start submit", state="ready", assignee="ops", implementation="Ready.")
    seed_ticket(store, "PGU-102", title="Audit signoff", state="audit", assignee="audit", implementation="Done.")
    seed_ticket(store, "PGU-103", title="Audit kickback", state="audit", assignee="audit", implementation="Done.")
    seed_ticket(
        store,
        "PGU-104",
        title="Eric signoff",
        state="eric_review",
        implementation="Done.",
        audit_signoff=True,
        needs_eric_signoff=True,
    )
    seed_ticket(store, "PGU-105", title="Eric reopen", state="director_review", implementation="Done.")
    seed_ticket(store, "PGU-106", title="Done", state="director_review", implementation="Done.")
    seed_ticket(store, "PGU-107", title="Defer")
    seed_ticket(store, "PGU-108", title="Cancel")
    seed_ticket(store, "PGU-109", title="Manual")
    seed_ticket(store, "PGU-110", title="Blocker")
    seed_ticket(store, "PGU-111", title="Blocked target")
    seed_ticket(store, "PGU-112", title="Comment")


def assert_action_requests(requests: list[tuple[str, str | None]]) -> None:
    assert requests, "client should send requests"
    for path, caller_role in requests:
        assert caller_role, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)
    assert ("/api/tickets/actions/create_ticket", "director") in requests
    assert ("/api/tickets/actions/file_bug", "ops") in requests
    assert ("/api/tickets/PGU-100/actions/route", "director") in requests
    assert ("/api/tickets/PGU-101/actions/start_work", "ops") in requests
    assert ("/api/tickets/PGU-101/actions/submit_to_audit", "ops") in requests
    assert ("/api/tickets/PGU-102/actions/audit_sign_off", "audit") in requests
    assert ("/api/tickets/PGU-103/actions/audit_kick_back", "audit") in requests
    assert ("/api/tickets/PGU-104/actions/eric_sign_off", "eric") in requests
    assert ("/api/tickets/PGU-105/actions/eric_reopen", "eric") in requests
    assert ("/api/tickets/PGU-106/actions/mark_done", "director") in requests
    assert ("/api/tickets/PGU-107/actions/defer", "director") in requests
    assert ("/api/tickets/PGU-108/actions/cancel", "director") in requests
    assert ("/api/tickets/PGU-109/actions/set_manually_controlled", "director") in requests
    assert ("/api/tickets/PGU-111/actions/set_blockers", "director") in requests
    assert ("/api/tickets/PGU-112/actions/add_comment", "director") in requests


def exercise_client(base_url: str, store: Path, commit_hash: str) -> None:
    client = TicketBoardWriteClient(base_url, "director")
    created = client.create_ticket(
        title="Client create",
        body="Created through the write client.",
        assignee="director",
        state="ready",
        implementation="Ready from client.",
        comment_text="Created by director client.",
    )["ticket"]
    created_id = str(created["id"])
    assert load_ticket(store, created_id)["state"] == "ready"
    assert load_ticket(store, created_id)["comments"][0]["who"] == "director"  # type: ignore[index]

    filed = client.file_bug(
        title="Client file bug",
        body="Bug filed through the write client.",
        source_ticket_id=created_id,
        caller_role="ops",
    )["ticket"]
    assert load_ticket(store, str(filed["id"]))["parent_id"] == created_id

    assert client.route("PGU-100", state="backlog", assignee="ops")["ticket"]["state"] == "backlog"
    assert client.start_work("PGU-101", caller_role="ops")["ticket"]["state"] == "in_progress"
    assert client.submit_to_audit("PGU-101", commit_hash=commit_hash, caller_role="ops")["ticket"]["state"] == "audit"
    assert client.audit_sign_off("PGU-102", caller_role="audit")["ticket"]["state"] == "director_review"
    assert client.audit_kick_back("PGU-103", reason="Needs another pass.", caller_role="audit")["ticket"]["state"] == "analysis"
    assert client.eric_sign_off("PGU-104", caller_role="eric")["ticket"]["state"] == "director_review"
    assert client.eric_reopen("PGU-105", reason="Needs design revision.", caller_role="eric")["ticket"]["state"] == "analysis"
    assert client.mark_done("PGU-106", commit_hash=commit_hash)["ticket"]["state"] == "done"
    assert client.defer("PGU-107")["ticket"]["state"] == "backlog"
    assert client.cancel("PGU-108", reason="No longer needed.")["ticket"]["state"] == "cancelled"
    assert client.set_manually_controlled("PGU-109", True)["ticket"]["manually_controlled"] is True
    blockers = client.set_blockers("PGU-111", blocked_by=["PGU-110"], blocked_reason="Waiting on blocker.")["ticket"]
    assert blockers["blocked_by"] == ["PGU-110"]
    assert client.add_comment("PGU-112", text="Director note.")["ticket"]["comments"][-1]["who"] == "director"  # type: ignore[index]


def assert_auto_socket_connect_failure_falls_back_to_tcp(base_url: str, store: Path, socket_path: Path) -> None:
    seed_ticket(store, "PGU-120", title="Fallback start", state="ready", assignee="ops", implementation="Ready.")
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
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-write-client.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        blocked_socket = root / "blocked.sock"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        seed_fixtures(store)
        server = RecordingTicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            exercise_client(base_url, store, main_commit())
            assert_auto_socket_connect_failure_falls_back_to_tcp(base_url, store, blocked_socket)
            assert_action_requests(server.requests)
            assert ("/api/tickets/PGU-120/actions/start_work", "ops") in server.requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("ticket_board_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
