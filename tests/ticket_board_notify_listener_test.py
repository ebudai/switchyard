#!/usr/bin/env python3
"""Regression tests for the PostgreSQL ticket-board notification queue worker."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import DirectorctlSender, PaneActivityGate, TicketBoardNotifyListener


@dataclass(frozen=True)
class FakeNotify:
    payload: str


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        if not self.rows:
            return None
        return self.rows[0]


class FakeConnection:
    def __init__(
        self,
        queue_rows: list[tuple[int, str, str, str, str, int]] | None = None,
        *,
        notifications: list[str] | None = None,
        fail_on_notifies: bool = False,
        executed: list[Any] | None = None,
    ) -> None:
        self.queue_rows = queue_rows or []
        self.notifications = notifications or []
        self.fail_on_notifies = fail_on_notifies
        self.executed = executed if executed is not None else []
        self.acked: list[int] = []
        self.requeued: list[tuple[int, tuple[Any, ...] | None]] = []
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        self.executed.append((statement, params))
        statement_text = str(statement)
        if "claim_notification" in statement_text:
            if not self.queue_rows:
                return FakeResult([])
            return FakeResult([self.queue_rows.pop(0)])
        if "ack_notification" in statement_text:
            assert params is not None
            self.acked.append(int(params[0]))
        if "requeue_notification" in statement_text:
            assert params is not None
            self.requeued.append((int(params[0]), params))
        return FakeResult([])

    def notifies(self, *, timeout: float | None = None) -> list[FakeNotify]:
        if self.fail_on_notifies:
            raise psycopg.OperationalError("simulated connection drop")
        queued = [FakeNotify(payload) for payload in self.notifications]
        self.notifications = []
        return queued


def queue_row(
    notification_id: int,
    ticket_id: str,
    *,
    target_role: str = "ops",
    message: str | None = None,
    attempts: int = 1,
) -> tuple[int, str, str, str, str, int]:
    message = message or f"Ready ticket for you: {ticket_id} -- Queue"
    payload = json.dumps({"kind": "transition", "id": ticket_id, "target_role": target_role, "message": message})
    return (notification_id, ticket_id, target_role, message, payload, attempts)


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


def test_claimed_queue_row_delivers_to_assignee_pane_and_acks() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    conn = FakeConnection([queue_row(1, "PGU-223")], executed=executed)
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-223 -- Queue")]
    assert conn.acked == [1]
    assert conn.requeued == []
    assert any("LISTEN" in str(statement) for statement, _params in executed)
    assert conn.closed is True


def test_eric_review_is_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
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
        FakeConnection([queue_row(2, "PGU-215", target_role="app", message="Ready ticket for you: PGU-215 -- Reconnect")], executed=executed),
    ]

    def connector(*args: Any, **kwargs: Any) -> FakeConnection:
        return connections.pop(0)

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=connector,
        reconnect_seconds=0,
        poll_seconds=0,
    )

    listener.run_forever(max_connections=2, max_notifications=1)

    assert sent == [("pgu-app:0.0", "Ready ticket for you: PGU-215 -- Reconnect")]
    assert sum("LISTEN" in str(statement) for statement, _params in executed) == 2


def test_send_timeout_requeues_and_does_not_block_next_pane() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(10, "PGU-220", target_role="director", message="PGU-220 -- Director review ready for your review"),
            queue_row(11, "PGU-221", target_role="ops", message="Ready ticket for you: PGU-221 -- Ops work"),
        ]
    )

    def sender(target: str, message: str) -> None:
        if target == "pgu-director:0.0":
            raise subprocess.TimeoutExpired(["directorctl", "send"], timeout=10)
        sent.append((target, message))

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=sender,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-221 -- Ops work")]
    assert [item[0] for item in conn.requeued] == [10]
    assert conn.acked == [11]


def test_busy_pane_requeues_then_idle_pane_delivers_once() -> None:
    sent: list[tuple[str, str]] = []
    busy_conn = FakeConnection([queue_row(20, "PGU-223", attempts=1)])
    busy_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: True,
        connector=lambda *args, **kwargs: busy_conn,
        poll_seconds=0,
    )

    busy_delivered = busy_listener.listen_once(max_notifications=1)

    assert busy_delivered == 0
    assert sent == []
    assert [item[0] for item in busy_conn.requeued] == [20]
    assert busy_conn.acked == []

    idle_conn = FakeConnection([queue_row(20, "PGU-223", attempts=2)])
    idle_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: idle_conn,
        poll_seconds=0,
    )

    idle_delivered = idle_listener.listen_once(max_notifications=1)

    assert idle_delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-223 -- Queue")]
    assert idle_conn.acked == [20]


def test_director_composer_busy_requeues_then_idle_delivers_once() -> None:
    sent: list[tuple[str, str]] = []
    gate_states = iter([True, False])

    busy_conn = FakeConnection(
        [
            queue_row(
                21,
                "PGU-231",
                target_role="director",
                message="PGU-231 -- Director review ready for your review",
                attempts=1,
            )
        ]
    )
    busy_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda target: target == "pgu-director:0.0" and next(gate_states),
        connector=lambda *args, **kwargs: busy_conn,
        poll_seconds=0,
    )

    assert busy_listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in busy_conn.requeued] == [21]
    assert busy_conn.acked == []

    idle_conn = FakeConnection(
        [
            queue_row(
                21,
                "PGU-231",
                target_role="director",
                message="PGU-231 -- Director review ready for your review",
                attempts=2,
            )
        ]
    )
    idle_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda target: target == "pgu-director:0.0" and next(gate_states),
        connector=lambda *args, **kwargs: idle_conn,
        poll_seconds=0,
    )

    assert idle_listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", "PGU-231 -- Director review ready for your review")]
    assert idle_conn.acked == [21]
    assert idle_conn.requeued == []


def test_director_gate_treats_composer_content_as_busy_only_for_director() -> None:
    horizontal = "─" * 48
    composer_busy_capture = f"""
assistant output
{horizontal}
❯ half typed message
{horizontal}
""".strip()

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=composer_busy_capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        recent_activity_seconds=0,
        stable_idle_seconds=0,
        capture_runner=capture_runner,
    )

    assert gate.is_busy("pgu-director:0.0") is True
    assert gate.is_busy("pgu-ops:0.0") is False


def test_director_gate_defers_recently_changed_idle_capture_then_delivers_when_stable() -> None:
    horizontal = "─" * 48
    idle_capture = f"""
assistant output
{horizontal}
❯
{horizontal}
""".strip()
    clock = [100.0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=idle_capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        recent_activity_seconds=3.0,
        stable_idle_seconds=0,
        capture_runner=capture_runner,
        monotonic=lambda: clock[0],
    )

    assert gate.is_busy("pgu-director:0.0") is True
    clock[0] += 4.0
    assert gate.is_busy("pgu-director:0.0") is False


def test_acked_notification_does_not_repeat_across_restart() -> None:
    sent: list[tuple[str, str]] = []
    first_conn = FakeConnection([queue_row(30, "PGU-223")])
    first_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: first_conn,
        poll_seconds=0,
    )
    assert first_listener.listen_once(max_notifications=1) == 1
    assert first_conn.acked == [30]

    second_conn = FakeConnection([])
    second_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: second_conn,
        poll_seconds=0,
    )
    assert second_listener.listen_once(max_notifications=1) == 0
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-223 -- Queue")]


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
    test_claimed_queue_row_delivers_to_assignee_pane_and_acks()
    test_eric_review_is_not_delivered()
    test_reconnect_relistens_after_connection_drop()
    test_send_timeout_requeues_and_does_not_block_next_pane()
    test_busy_pane_requeues_then_idle_pane_delivers_once()
    test_director_composer_busy_requeues_then_idle_delivers_once()
    test_director_gate_treats_composer_content_as_busy_only_for_director()
    test_director_gate_defers_recently_changed_idle_capture_then_delivers_when_stable()
    test_acked_notification_does_not_repeat_across_restart()
    test_default_sender_delegates_to_directorctl_send()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
