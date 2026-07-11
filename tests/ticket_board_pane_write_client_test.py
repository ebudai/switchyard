#!/usr/bin/env python3
"""Regression test: Codex panes write through the board API as their own roles."""

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
from scripts.ticket_board.server import CALLER_ROLE_HEADER, TicketBoardEventHub, TicketBoardHandler
from scripts.ticket_board.write_client import TicketBoardWriteClient, default_caller_role


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
        "audit_signoff": False,
        "needs_eric_signoff": False,
        "eric_signoff": False,
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


def exercise_pane_cli(base_url: str, store: Path, commit_hash: str) -> None:
    assert default_caller_role({"PGU_TICKET_BOARD_CALLER_ROLE": " app "}) == "app"
    assert TicketBoardWriteClient(base_url, "director").for_caller("ops").caller_role == "ops"

    started = run_cli(base_url, "main", "start-work", "PGU-2100")
    assert started["state"] == "in_progress", started
    submitted = run_cli(base_url, "main", "submit-to-audit", "PGU-2100", "--commit-hash", commit_hash)
    assert submitted["state"] == "audit", submitted
    assert submitted["commit_hash"] == commit_hash, submitted

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
    filed_id = str(filed["id"])
    assert load_ticket(store, filed_id)["parent_id"] == "PGU-2101"

    commented = run_cli(base_url, "ops", "add-comment", "PGU-2102", "--text", "Ops pane note.")
    assert commented["comments"][-1]["who"] == "ops", commented  # type: ignore[index]
    assert load_ticket(store, "PGU-2102")["comments"][-1]["text"] == "Ops pane note."  # type: ignore[index]


def assert_pane_requests(requests: list[tuple[str, str | None]]) -> None:
    assert ("/api/tickets/PGU-2100/actions/start_work", "main") in requests
    assert ("/api/tickets/PGU-2100/actions/submit_to_audit", "main") in requests
    assert ("/api/tickets/actions/file_bug", "app") in requests
    assert ("/api/tickets/PGU-2102/actions/add_comment", "ops") in requests
    for path, caller_role in requests:
        assert caller_role in {"main", "app", "ops"}, (path, caller_role)
        assert "/actions/" in path, (path, caller_role)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-pane-write-client.") as tmpdir:
        root = Path(tmpdir)
        store = root / "store"
        frames = root / "frames"
        assets = root / "assets"
        store.mkdir()
        frames.mkdir()
        assets.mkdir()
        seed_ticket(store, "PGU-2100", title="Main ready", state="ready", assignee="main", implementation="Ready.")
        seed_ticket(store, "PGU-2101", title="App bug source")
        seed_ticket(store, "PGU-2102", title="Ops comment target")
        server = RecordingTicketBoardServer(("127.0.0.1", 0), TicketBoardApp(store, frames, assets))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise_pane_cli(f"http://127.0.0.1:{server.server_port}", store, main_commit())
            assert_pane_requests(server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("ticket_board_pane_write_client_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
