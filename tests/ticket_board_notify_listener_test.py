#!/usr/bin/env python3
"""Regression tests for the PostgreSQL ticket-board NOTIFY delivery shim."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import DirectorctlSender, TicketBoardNotifyListener


@dataclass(frozen=True)
class FakeNotify:
    payload: str


class FakeResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[str, str]]:
        return self.rows


class FakeConnection:
    def __init__(
        self,
        notifications: list[str] | None = None,
        *,
        pending_rows: list[tuple[str, str]] | None = None,
        fail_on_notifies: bool = False,
        block_on_notifies: bool = False,
        executed: list[Any] | None = None,
    ) -> None:
        self.notifications = notifications or []
        self.pending_rows = pending_rows or []
        self.fail_on_notifies = fail_on_notifies
        self.block_on_notifies = block_on_notifies
        self.executed = executed if executed is not None else []
        self.closed = False
        self.close_event = threading.Event()

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        self.close_event.set()

    def execute(self, statement: Any, params: object | None = None) -> FakeResult:
        self.executed.append((statement, params))
        if isinstance(statement, str) and "pending_transition_notifications" in statement:
            return FakeResult(self.pending_rows)
        return FakeResult([])

    def notifies(self, *, timeout: float | None = None) -> list[FakeNotify]:
        if self.fail_on_notifies:
            raise psycopg.OperationalError("simulated connection drop")
        if self.block_on_notifies:
            if self.close_event.wait(timeout=1.0):
                raise psycopg.OperationalError("simulated wedged connection closed")
            raise AssertionError("watchdog never force-closed the wedged connection")
        queued = [FakeNotify(payload) for payload in self.notifications]
        self.notifications = []
        return queued


def transition_payload(
    ticket_id: str,
    *,
    title: str = "Ticket title",
    old_state: str = "analysis",
    new_state: str = "ready",
    assignee: str = "ops",
) -> str:
    return json.dumps(
        {
            "id": ticket_id,
            "title": title,
            "old_state": old_state,
            "new_state": new_state,
            "assignee": assignee,
            "updated_at": "2026-07-11T00:00:00+00:00",
            "ticket_number": int(ticket_id.split("-", 1)[1]),
        }
    )


def test_simulated_pg_notify_delivers_to_assignee_pane() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    conn = FakeConnection([transition_payload("PGU-213", title="Restore notifications", assignee="ops")], executed=executed)
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-213 -- Restore notifications")]
    assert executed, "listener must issue LISTEN before consuming notifications"
    assert any("mark_transition_notified" in str(statement) for statement, _params in executed)
    assert conn.closed is True


def test_eric_review_is_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
    )

    delivered = listener.deliver_payload(
        transition_payload("PGU-214", old_state="audit", new_state="eric_review", assignee="director")
    )

    assert delivered is False
    assert sent == []
    assert listener.delivered_count == 0


def test_reconnect_relistens_after_connection_drop() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    connections = [
        FakeConnection(fail_on_notifies=True, executed=executed),
        FakeConnection(
            [transition_payload("PGU-215", title="Reconnect", old_state="analysis", new_state="ready", assignee="app")],
            executed=executed,
        ),
    ]

    def connector(*args: Any, **kwargs: Any) -> FakeConnection:
        return connections.pop(0)

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=connector,
        reconnect_seconds=0,
        poll_seconds=0,
    )

    listener.run_forever(max_connections=2, max_notifications=1)

    assert sent == [("pgu-app:0.0", "Ready ticket for you: PGU-215 -- Reconnect")]
    assert sum("LISTEN" in str(statement) for statement, _params in executed) == 2, "listener must re-issue LISTEN after reconnecting"


def test_director_send_timeout_does_not_block_other_pane_delivery() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    conn = FakeConnection(
        [
            transition_payload(
                "PGU-220",
                title="Director review",
                old_state="audit",
                new_state="director_review",
                assignee="director",
            ),
            transition_payload("PGU-221", title="Ops work", old_state="analysis", new_state="ready", assignee="ops"),
        ],
        executed=executed,
    )

    def sender(target: str, message: str) -> None:
        if target == "pgu-director:0.0":
            raise subprocess.TimeoutExpired(["directorctl", "send"], timeout=10)
        sent.append((target, message))

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=sender,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-221 -- Ops work")]
    mark_calls = [params for statement, params in executed if "mark_transition_notified" in str(statement)]
    assert mark_calls == [("PGU-221",)], mark_calls


def test_poll_watchdog_force_closes_wedged_connection_and_reconnects() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    wedged = FakeConnection(block_on_notifies=True, executed=executed)
    recovered = FakeConnection(
        [transition_payload("PGU-219", title="Wedge recovery", old_state="analysis", new_state="ready", assignee="ops")],
        executed=executed,
    )
    connections = [wedged, recovered]

    def connector(*args: Any, **kwargs: Any) -> FakeConnection:
        return connections.pop(0)

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=connector,
        reconnect_seconds=0,
        poll_seconds=0.01,
        watchdog_seconds=0.05,
    )

    listener.run_forever(max_connections=2, max_notifications=1)

    assert wedged.closed is True
    assert wedged.close_event.is_set()
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-219 -- Wedge recovery")]
    assert sum("LISTEN" in str(statement) for statement, _params in executed) == 2, "watchdog reconnect must re-issue LISTEN"


def test_reconcile_delivers_missed_transition_and_marks_notified() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    payload = transition_payload("PGU-207", title="Durable", old_state="", new_state="ready", assignee="ops")
    conn = FakeConnection(pending_rows=[("PGU-207", payload)], executed=executed)
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-207 -- Durable")]
    assert any("pending_transition_notifications" in str(statement) for statement, _params in executed)
    assert any("mark_transition_notified" in str(statement) for statement, _params in executed)


def test_nudge_is_suppressed_when_pane_is_working() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: True,
    )
    payload = json.dumps(
        {
            "kind": "nudge",
            "id": "PGU-207",
            "title": "Durable",
            "state": "ready",
            "assignee": "ops",
            "target_role": "ops",
            "message": "NUDGE Ready ticket for you: PGU-207 -- Durable",
        }
    )

    delivered = listener.deliver_payload(payload)

    assert delivered is False
    assert sent == []


def test_default_sender_delegates_to_directorctl_send() -> None:
    calls: list[tuple[list[str], bool]] = []
    original_run = subprocess.run

    def fake_run(args: list[str], *, check: bool = False, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0)

    try:
        subprocess.run = fake_run  # type: ignore[assignment]
        DirectorctlSender("/tmp/directorctl")("pgu-ops:0.0", "Ready ticket for you: PGU-215 -- Submit")
    finally:
        subprocess.run = original_run  # type: ignore[assignment]

    assert calls == [
        (["/tmp/directorctl", "send", "pgu-ops:0.0", "Ready ticket for you: PGU-215 -- Submit"], True)
    ]


def main() -> int:
    test_simulated_pg_notify_delivers_to_assignee_pane()
    test_eric_review_is_not_delivered()
    test_reconnect_relistens_after_connection_drop()
    test_director_send_timeout_does_not_block_other_pane_delivery()
    test_poll_watchdog_force_closes_wedged_connection_and_reconnects()
    test_reconcile_delivers_missed_transition_and_marks_notified()
    test_nudge_is_suppressed_when_pane_is_working()
    test_default_sender_delegates_to_directorctl_send()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
