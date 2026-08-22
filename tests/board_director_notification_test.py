#!/usr/bin/env python3
"""Regression tests for immediate director create notifications."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import ASSIGNEES, STATES, iso_now
from scripts.ticket_board.server import CALLER_ROLE_HEADER, WRITE_TOKEN_HEADER, DirectorNotifier, TicketBoardServer


class FakeNotifier:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        self.created.append({"id": str(ticket["id"]), "title": str(ticket["title"])})

    def close(self) -> None:
        return


class MemoryCreateApp:
    store_backend = "postgres"

    def __init__(self, frames: Path, assets: Path, *, persist: bool = True) -> None:
        self.frame_dir = frames.resolve()
        self.asset_dir = assets.resolve()
        self.persist = persist
        self.tickets: dict[str, dict[str, object]] = {}
        self.next_number = 1

    def store_signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((ticket_id, str(ticket["updated"])) for ticket_id, ticket in self.tickets.items()))

    def snapshot(self) -> dict[str, object]:
        return {
            "tickets": list(self.tickets.values()),
            "errors": [],
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": [],
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def create_ticket(self, **kwargs: object) -> dict[str, object]:
        return self.create_ticket_record(
            title=kwargs.get("title", ""),
            body=kwargs.get("body", ""),
            assignee=kwargs.get("assignee", "unassigned"),
            state="analysis",
            blocked_by=kwargs.get("blocked_by"),
            parent_id="",
            implementation="",
            audit_prompt="",
            audit_signoff=False,
            needs_inspection=kwargs.get("needs_inspection", False),
            inspector_signoff=False,
            needs_user_signoff=kwargs.get("needs_user_signoff", False),
            user_signoff=False,
            comments=[],
            blocked_reason=kwargs.get("blocked_reason", ""),
            commit_hash="",
            commit_exempt=False,
            caller_role=kwargs.get("caller_role"),
        )

    def create_ticket_record(self, **kwargs: object) -> dict[str, object]:
        ticket_id = f"PGU-{self.next_number}"
        self.next_number += 1
        now = iso_now()
        ticket = {
            "id": ticket_id,
            "title": str(kwargs["title"]).strip(),
            "body": str(kwargs["body"]),
            "assignee": str(kwargs["assignee"]),
            "state": str(kwargs["state"]),
            "blocked_by": list(kwargs.get("blocked_by") or []),
            "parent_id": str(kwargs.get("parent_id") or ""),
            "blocked_reason": str(kwargs.get("blocked_reason") or ""),
            "implementation": str(kwargs.get("implementation") or ""),
            "audit_prompt": str(kwargs.get("audit_prompt") or ""),
            "audit_signoff": bool(kwargs.get("audit_signoff")),
            "needs_user_signoff": bool(kwargs.get("needs_user_signoff")),
            "user_signoff": bool(kwargs.get("user_signoff")),
            "manually_controlled": False,
            "commit_hash": str(kwargs.get("commit_hash") or ""),
            "commit_exempt": bool(kwargs.get("commit_exempt")),
            "created": now,
            "updated": now,
            "comments": [],
            "screenshots": [],
            "screenshots_info": [],
            "screenshot": None,
            "screenshot_available": False,
        }
        if self.persist:
            self.tickets[ticket_id] = ticket
        return ticket

    def get_ticket(self, ticket_id: str) -> dict[str, object]:
        return self.tickets[ticket_id]

    def verify_created_ticket_persisted(
        self,
        created: dict[str, object],
        before_signature: tuple[tuple[object, ...], ...],
    ) -> tuple[tuple[object, ...], ...]:
        ticket_id = str(created["id"])
        if ticket_id not in self.tickets:
            raise ValueError(f"created ticket was not persisted: {ticket_id}")
        after = self.store_signature()
        if after == before_signature:
            raise ValueError(f"ticket create did not change the store: {ticket_id}")
        return after


def post_create(
    server: TicketBoardServer,
    title: str,
    *,
    caller: str,
    state: str = "analysis",
    assignee: str = "app",
    blocked_by: list[str] | None = None,
    blocked_reason: str = "",
) -> dict[str, object]:
    body = json.dumps(
        {
            "title": title,
            "body": "Created through API.",
            "assignee": assignee,
            "state": state,
            "blocked_by": blocked_by or [],
            "blocked_reason": blocked_reason,
            "needs_user_signoff": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/api/tickets/actions/create_ticket",
        data=body,
        headers={"Content-Type": "application/json", CALLER_ROLE_HEADER: caller, WRITE_TOKEN_HEADER: server.write_token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def post_file_bug(server: TicketBoardServer, title: str, *, caller: str) -> dict[str, object]:
    body = json.dumps(
        {
            "title": title,
            "body": "Filed through API.",
            "source_ticket_id": "PGU-999",
            "assignee": "unassigned",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/api/tickets/actions/file_bug",
        data=body,
        headers={"Content-Type": "application/json", CALLER_ROLE_HEADER: caller, WRITE_TOKEN_HEADER: server.write_token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_server_human_web_director_create_notifies_director() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-server.") as tmpdir:
        root = Path(tmpdir)
        notifier = FakeNotifier()
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryCreateApp(root / "frames", root / "assets"),
            director_notifier=notifier,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = post_create(
                server,
                "Director web create",
                caller="director",
                assignee="unassigned",
            )
            assert payload["ticket"]["title"] == "Director web create", payload
            assert notifier.created == [{"id": payload["ticket"]["id"], "title": "Director web create"}], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_non_director_file_bug_notifies_director() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-server.") as tmpdir:
        root = Path(tmpdir)
        notifier = FakeNotifier()
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryCreateApp(root / "frames", root / "assets"),
            director_notifier=notifier,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = post_file_bug(server, "Instant notify", caller="ops")
            assert notifier.created == [{"id": payload["ticket"]["id"], "title": "Instant notify"}], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_backlog_create_does_not_notify_director() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-backlog.") as tmpdir:
        root = Path(tmpdir)
        notifier = FakeNotifier()
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryCreateApp(root / "frames", root / "assets"),
            director_notifier=notifier,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = post_create(server, "Quiet backlog", caller="director", state="backlog")
            assert payload["ticket"]["state"] == "backlog", payload
            assert notifier.created == [], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_blocked_create_does_not_notify_director() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-blocked.") as tmpdir:
        root = Path(tmpdir)
        notifier = FakeNotifier()
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryCreateApp(root / "frames", root / "assets"),
            director_notifier=notifier,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = post_create(
                server,
                "Quiet blocked create",
                caller="director",
                blocked_by=["PGU-1"],
                blocked_reason="Waiting on PGU-1.",
            )
            assert payload["ticket"]["state"] == "analysis", payload
            assert payload["ticket"]["blocked_by"] == ["PGU-1"], payload
            assert notifier.created == [], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_server_create_does_not_notify_when_ticket_is_not_persisted() -> None:
    with tempfile.TemporaryDirectory(prefix="board-director-notify-no-persist.") as tmpdir:
        root = Path(tmpdir)
        notifier = FakeNotifier()
        server = TicketBoardServer(
            ("127.0.0.1", 0),
            MemoryCreateApp(root / "frames", root / "assets", persist=False),
            director_notifier=notifier,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                post_create(server, "Phantom create", caller="director")
            except HTTPError as exc:
                assert exc.code == 400
                assert "created ticket was not persisted" in exc.read().decode("utf-8")
            else:
                raise AssertionError("non-persisted create should fail")
            assert notifier.created == [], notifier.created
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_director_notifier_batches_quick_creates() -> None:
    sent: list[str] = []
    notifier = DirectorNotifier(sender=sent.append, batch_window_seconds=0.05)
    notifier.notify_ticket_created({"id": "PGU-75", "title": "Alpha"})
    notifier.notify_ticket_created({"id": "PGU-76", "title": "Beta"})
    time.sleep(0.2)
    notifier.close()
    assert sent == ["New tickets for you: PGU-75 -- Alpha; PGU-76 -- Beta"], sent


def main() -> int:
    test_server_human_web_director_create_notifies_director()
    test_server_non_director_file_bug_notifies_director()
    test_server_backlog_create_does_not_notify_director()
    test_server_blocked_create_does_not_notify_director()
    test_server_create_does_not_notify_when_ticket_is_not_persisted()
    test_director_notifier_batches_quick_creates()
    print("board_director_notification_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
