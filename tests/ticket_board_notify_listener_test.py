#!/usr/bin/env python3
"""Regression tests for the PostgreSQL ticket-board notification queue worker."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import (
    DEFAULT_BUSY_REQUEUE_SECONDS,
    DEFAULT_STABLE_IDLE_SECONDS,
    DirectorctlSender,
    PaneActivityGate,
    TicketBoardNotifyListener,
)


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
        ticket_rows: dict[str, tuple[str, str] | tuple[str, str, bool, bool]] | None = None,
        queue_created_at: dict[int, datetime] | None = None,
        notifications: list[str] | None = None,
        fail_on_notifies: bool = False,
        executed: list[Any] | None = None,
    ) -> None:
        self.queue_rows = queue_rows or []
        self.ticket_rows = ticket_rows if ticket_rows is not None else self._ticket_rows_for_queue(self.queue_rows)
        self.queue_created_at = (
            queue_created_at
            if queue_created_at is not None
            else {notification_id: datetime.now(timezone.utc) for notification_id, *_rest in self.queue_rows}
        )
        self.notifications = notifications or []
        self.fail_on_notifies = fail_on_notifies
        self.executed = executed if executed is not None else []
        self.acked: list[int] = []
        self.requeued: list[tuple[int, tuple[Any, ...] | None]] = []
        self.traces: list[tuple[Any, ...] | None] = []
        self.closed = False

    def _ticket_rows_for_queue(self, queue_rows: list[tuple[int, str, str, str, str, int]]) -> dict[str, tuple[str, str, bool, bool]]:
        rows: dict[str, tuple[str, str, bool, bool]] = {}
        for _notification_id, ticket_id, target_role, _message, payload, _attempts in queue_rows:
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {}
            state = str(parsed.get("state") or parsed.get("new_state") or "").strip()
            assignee = str(parsed.get("assignee") or "").strip()
            if not state:
                state = "audit" if target_role == "audit" else "director_review" if target_role == "director" else "in_progress"
            if not assignee:
                assignee = "director" if target_role == "director" else target_role
            rows[ticket_id] = (state, assignee, False, False)
        return rows

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        self.executed.append((statement, params))
        statement_text = str(statement)
        if "record_notification_trace" in statement_text:
            self.traces.append(params)
        if "claim_notification" in statement_text:
            if not self.queue_rows:
                return FakeResult([])
            return FakeResult([self.queue_rows.pop(0)])
        if "FROM ticket_board.tickets" in statement_text:
            assert params is not None
            row = self.ticket_rows.get(str(params[0]))
            if row is not None and len(row) == 2:
                row = (row[0], row[1], False, False)
            return FakeResult([] if row is None else [row])
        if "FROM ticket_board.ticket_notification_queue" in statement_text and "created_at" in statement_text:
            assert params is not None
            created_at = self.queue_created_at.get(int(params[0]))
            return FakeResult([] if created_at is None else [(created_at,)])
        if "ack_notification" in statement_text:
            assert params is not None
            self.acked.append(int(params[0]))
        if "requeue_notification" in statement_text:
            assert params is not None
            self.requeued.append((int(params[0]), params))
        return FakeResult([])

    def notifies(self, *, timeout: float | None = None, stop_after: int | None = None) -> Any:
        if self.fail_on_notifies:
            raise psycopg.OperationalError("simulated connection drop")
        queued = [FakeNotify(payload) for payload in self.notifications]
        self.notifications = []
        if stop_after is not None:
            queued = queued[:stop_after]
        yield from queued


class StopAfterWaitConnection(FakeConnection):
    def __init__(self, stop_event: Any) -> None:
        super().__init__()
        self.stop_event = stop_event
        self.claim_calls = 0
        self.notifies_advanced = 0

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        statement_text = str(statement)
        if "claim_notification" in statement_text:
            self.claim_calls += 1
            if self.claim_calls > 1:
                raise AssertionError("idle listener polled claim_notification again before waiting")
        return super().execute(statement, params)

    def notifies(self, *, timeout: float | None = None, stop_after: int | None = None) -> Any:
        self.notifies_advanced += 1
        self.stop_event.set()
        if False:
            yield FakeNotify("")


def queue_row(
    notification_id: int,
    ticket_id: str,
    *,
    kind: str = "transition",
    state: str | None = None,
    assignee: str | None = None,
    target_role: str = "ops",
    message: str | None = None,
    attempts: int = 1,
) -> tuple[int, str, str, str, str, int]:
    message = message or f"New ticket for you: {ticket_id} -- Queue"
    payload_fields = {"kind": kind, "id": ticket_id, "target_role": target_role, "message": message}
    if state is not None:
        payload_fields["state" if kind != "transition" else "new_state"] = state
    if assignee is not None:
        payload_fields["assignee"] = assignee
    payload = json.dumps(payload_fields)
    return (notification_id, ticket_id, target_role, message, payload, attempts)


def trace_events(conn: FakeConnection) -> list[str]:
    return [str(params[4]) for params in conn.traces if params is not None]


def transition_payload(
    ticket_id: str,
    *,
    title: str = "Ticket title",
    old_state: str = "analysis",
    new_state: str = "in_progress",
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
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-223 -- Queue")]
    assert conn.acked == [1]
    assert conn.requeued == []
    assert trace_events(conn) == ["listener_claim", "send", "listener_ack"]
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


def test_inspection_transition_delivers_to_inspector() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
    )

    delivered = listener.deliver_payload(
        transition_payload("PGU-248", old_state="in_progress", new_state="inspection", assignee="ops")
    )

    assert delivered is True
    assert sent == [("pgu-inspector:0.0", "PGU-248 -- Ticket title ready for inspection")]


def test_stale_inspection_queue_row_is_acked_without_delivery() -> None:
    sent: list[tuple[str, str]] = []
    payload = json.dumps(
        {
            "kind": "transition",
            "id": "PGU-248",
            "new_state": "inspection",
            "assignee": "ops",
            "target_role": "inspector",
            "message": "PGU-248 -- Ticket title ready for inspection",
        }
    )
    conn = FakeConnection(
        [(30, "PGU-248", "inspector", "PGU-248 -- Ticket title ready for inspection", payload, 1)],
        ticket_rows={"PGU-248": ("audit", "ops", False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 0
    assert sent == []
    assert conn.acked == [30]
    assert conn.requeued == []
    assert trace_events(conn) == ["listener_claim", "drop", "listener_ack"]
    assert conn.traces[1][6] == "stale_notification"


def test_reconnect_relistens_after_connection_drop() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    connections = [
        FakeConnection(fail_on_notifies=True, executed=executed),
        FakeConnection([queue_row(2, "PGU-215", target_role="app", message="New ticket for you: PGU-215 -- Reconnect")], executed=executed),
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

    assert sent == [("pgu-app:0.0", "New ticket for you: PGU-215 -- Reconnect")]
    assert sum("LISTEN" in str(statement) for statement, _params in executed) == 2


def test_idle_listener_consumes_notifies_generator_before_polling_again() -> None:
    stop_event = threading.Event()
    conn = StopAfterWaitConnection(stop_event)
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda _target, _message: None,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0.01,
        stop_event=stop_event,
    )

    assert listener.listen_once() == 0
    assert conn.claim_calls == 1
    assert conn.notifies_advanced == 1


def test_send_timeout_requeues_and_does_not_block_next_pane() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(10, "PGU-220", target_role="director", message="PGU-220 -- Director review ready for your review"),
            queue_row(11, "PGU-221", target_role="ops", message="New ticket for you: PGU-221 -- Ops work"),
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
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-221 -- Ops work")]
    assert [item[0] for item in conn.requeued] == [10]
    assert conn.requeued[0][1][1] == "5 seconds"
    assert conn.acked == [11]


def test_send_failure_uses_exponential_backoff() -> None:
    conn = FakeConnection(
        [
            queue_row(
                12,
                "PGU-292",
                target_role="ops",
                message="New ticket for you: PGU-292 -- Send failure",
                attempts=4,
            )
        ]
    )

    def sender(_target: str, _message: str) -> None:
        raise subprocess.TimeoutExpired(["directorctl", "send"], timeout=10)

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=sender,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert [item[0] for item in conn.requeued] == [12]
    assert conn.requeued[0][1][1] == "40 seconds"
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "send_failed"]


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
    assert busy_conn.requeued[0][1][1] == "3 seconds"
    assert busy_conn.acked == []
    assert trace_events(busy_conn) == ["listener_claim", "gate_defer"]
    assert busy_conn.traces[1][5] == "busy"

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
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-223 -- Queue")]
    assert idle_conn.acked == [20]
    assert trace_events(idle_conn) == ["listener_claim", "send", "listener_ack"]


def test_real_gate_busy_retry_is_fixed_then_delivers_after_stable_window() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    capture = "completed output\nstable prompt"

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )

    busy_conn = FakeConnection([queue_row(26, "PGU-292", attempts=20)])
    busy_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: busy_conn,
        poll_seconds=0,
    )

    assert busy_listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in busy_conn.requeued] == [26]
    assert busy_conn.requeued[0][1][1] == "3 seconds"
    assert busy_conn.acked == []
    assert trace_events(busy_conn) == ["listener_claim", "gate_defer"]
    assert busy_conn.traces[1][6] == "region_unproven"

    now[0] = 1.1
    idle_conn = FakeConnection([queue_row(26, "PGU-292", attempts=21)])
    idle_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: idle_conn,
        poll_seconds=0,
    )

    assert idle_listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-292 -- Queue")]
    assert idle_conn.acked == [26]
    assert idle_conn.requeued == []
    assert trace_events(idle_conn) == ["listener_claim", "send", "listener_ack"]


def test_region_must_remain_stable_for_full_idle_window() -> None:
    now = [0.0]
    captures = ["first frame\nprompt", "second frame\nprompt"]
    capture_index = [0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=captures[capture_index[0]])

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    target = "pgu-ops:0.0"

    assert gate.is_busy(target) is True
    assert gate.last_trace(target).reason == "region_unproven"

    now[0] = 0.1
    capture_index[0] = 1
    assert gate.is_busy(target) is True
    assert gate.last_trace(target).reason == "region_changed"

    now[0] = 0.6
    assert gate.is_busy(target) is True
    assert gate.last_trace(target).reason == "region_settling"

    now[0] = 1.2
    assert gate.is_busy(target) is False
    assert gate.last_trace(target).reason == "stable_idle"


def test_busy_pane_never_force_delivers_old_notification() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [queue_row(25, "PGU-252", attempts=9)],
        queue_created_at={25: datetime.now(timezone.utc) - timedelta(hours=1)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: True,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in conn.requeued] == [25]
    assert conn.requeued[0][1][1] == "3 seconds"
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "gate_defer"]
    assert "max_defer_force_deliver" not in trace_events(conn)


def test_stale_implementation_nudge_for_cancelled_ticket_is_acked_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(
                22,
                "PGU-226",
                kind="nudge",
                state="in_progress",
                assignee="ops",
                target_role="ops",
                attempts=2,
            )
        ],
        ticket_rows={"PGU-226": ("cancelled", "ops")},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert conn.acked == [22]
    assert conn.requeued == []


def test_stale_implementation_nudge_for_picked_up_ticket_is_acked_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(
                23,
                "PGU-226",
                kind="nudge",
                state="in_progress",
                assignee="ops",
                target_role="ops",
                attempts=2,
            )
        ],
        ticket_rows={"PGU-226": ("audit", "ops")},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert conn.acked == [23]
    assert conn.requeued == []


def test_escalation_for_still_stuck_implementation_ticket_delivers_to_director() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(
                24,
                "PGU-226",
                kind="escalation",
                target_role="director",
                message="PRIORITY PGU-226 -- Queue appears stuck for ops; check/reassign",
                attempts=1,
            )
        ],
        ticket_rows={"PGU-226": ("in_progress", "ops", False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", "PRIORITY PGU-226 -- Queue appears stuck for ops; check/reassign")]
    assert conn.acked == [24]
    assert conn.requeued == []


def test_nudge_analysis_copy_delivers_to_director() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(
                25,
                "PGU-256",
                kind="nudge_analysis",
                state="in_progress",
                assignee="ops",
                target_role="director",
                message="NUDGE-ANALYSIS: PGU-256 (target=ops, state=in_progress, 600s since transition, prior_nudges=0)",
                attempts=1,
            )
        ],
        ticket_rows={"PGU-256": ("in_progress", "ops", False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [
        (
            "pgu-director:0.0",
            "NUDGE-ANALYSIS: PGU-256 (target=ops, state=in_progress, 600s since transition, prior_nudges=0)",
        )
    ]
    assert conn.acked == [25]
    assert conn.requeued == []


def test_director_busy_requeues_then_idle_delivers_once() -> None:
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


def test_director_busy_never_force_delivers_old_notification_until_idle() -> None:
    sent: list[tuple[str, str]] = []
    old_created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    busy_conn = FakeConnection(
        [
            queue_row(
                22,
                "PGU-283",
                target_role="director",
                message="PGU-283 -- Director notification",
                attempts=4,
            )
        ],
        queue_created_at={22: old_created_at},
    )
    busy_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda target: target == "pgu-director:0.0",
        connector=lambda *args, **kwargs: busy_conn,
        poll_seconds=0,
    )

    assert busy_listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in busy_conn.requeued] == [22]
    assert busy_conn.acked == []
    assert trace_events(busy_conn) == ["listener_claim", "gate_defer"]
    assert "max_defer_force_deliver" not in trace_events(busy_conn)

    idle_conn = FakeConnection(
        [
            queue_row(
                22,
                "PGU-283",
                target_role="director",
                message="PGU-283 -- Director notification",
                attempts=5,
            )
        ],
        queue_created_at={22: old_created_at},
    )
    idle_listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: idle_conn,
        poll_seconds=0,
    )

    assert idle_listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", "PGU-283 -- Director notification")]
    assert idle_conn.acked == [22]
    assert idle_conn.requeued == []
    assert trace_events(idle_conn) == ["listener_claim", "send", "listener_ack"]


def test_stable_last_lines_queue_notification_delivers_immediately() -> None:
    capture = "\n".join(["prior output", "stable footer", "stable prompt"])
    sent: list[tuple[str, str]] = []
    now = [0.0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    assert gate.is_busy("pgu-director:0.0") is True
    now[0] = 1.1
    conn = FakeConnection(
        [
            queue_row(
                23,
                "PGU-283",
                target_role="director",
                message="PGU-283 -- Director notification",
                attempts=1,
            )
        ]
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_busy,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", "PGU-283 -- Director notification")]
    assert conn.acked == [23]
    assert conn.requeued == []


def test_changing_arbitrary_region_waits() -> None:
    sent: list[tuple[str, str]] = []
    tick = [46]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        tick[0] += 1
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"unchanged header\narbitrary changing line {tick[0]}\nunchanged footer",
        )

    gate = PaneActivityGate("/tmp/directorctl", stable_idle_seconds=1.0, capture_runner=capture_runner)
    assert gate.is_busy("pgu-director:0.0") is True

    conn = FakeConnection(
        [
            queue_row(
                34,
                "PGU-290",
                target_role="director",
                message="PGU-290 -- Director notification",
            )
        ]
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in conn.requeued] == [34]
    assert conn.requeued[0][1][1] == "3 seconds"
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "gate_defer"]
    assert conn.traces[1][5] == "busy"
    assert conn.traces[1][6] in {"region_changed", "region_unproven", "region_settling"}


def test_static_arbitrary_region_is_idle_and_delivers() -> None:
    capture = "unchanged header\narbitrary stable line\nunchanged footer"
    sent: list[tuple[str, str]] = []
    now = [0.0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    assert gate.is_busy("pgu-director:0.0") is True
    now[0] = 1.1

    conn = FakeConnection(
        [
            queue_row(
                39,
                "PGU-292",
                target_role="director",
                message="PGU-292 -- Director notification",
                attempts=4,
            )
        ],
        queue_created_at={39: datetime.now(timezone.utc) - timedelta(hours=1)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", "PGU-292 -- Director notification")]
    assert conn.acked == [39]
    assert conn.requeued == []
    assert trace_events(conn) == ["listener_claim", "send", "listener_ack"]
    assert conn.traces[1][5] == "idle"
    assert conn.traces[1][6] == "stable_idle"


def test_changing_director_region_defers_without_backstop() -> None:
    captures = iter(
        [
            "stable header\narbitrary draft line one\nstable footer",
            "stable header\narbitrary draft line two\nstable footer",
        ]
    )
    sent: list[tuple[str, str]] = []
    now = [0.0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=next(captures))

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    assert gate.is_busy("pgu-director:0.0") is True
    now[0] = 1.1
    conn = FakeConnection(
        [
            queue_row(
                35,
                "PGU-290",
                target_role="director",
                message="PGU-290 -- Director notification",
                attempts=7,
            )
        ],
        queue_created_at={35: datetime.now(timezone.utc) - timedelta(hours=1)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in conn.requeued] == [35]
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "gate_defer"]
    assert conn.traces[1][5] == "busy"
    assert conn.traces[1][6] == "region_changed"
    assert "max_defer_force_deliver" not in trace_events(conn)


def test_batch_idle_notifications_deliver_promptly_without_serial_sleep() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"stable pane output for {' '.join(args)}\nstable prompt",
        )

    rows = [
        queue_row(36, "PGU-2901", target_role="director", message="PGU-2901 -- Director notification"),
        queue_row(37, "PGU-2902", target_role="director", message="PGU-2902 -- Director notification"),
        queue_row(38, "PGU-2903", target_role="director", message="PGU-2903 -- Director notification"),
    ]
    expected_count = len(rows)
    conn = FakeConnection(rows)
    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    assert gate.is_busy("pgu-director:0.0") is True
    now[0] = 1.1
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    started = time.perf_counter()
    assert listener.listen_once(max_notifications=expected_count) == expected_count
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert len(sent) == expected_count
    assert conn.acked == [36, 37, 38]
    assert conn.requeued == []


def test_changing_last_lines_requeue_queue_notification() -> None:
    sent: list[tuple[str, str]] = []
    captures = iter(
        [
            "task output\nstatus tick 1\nplain footer",
            "task output\nstatus tick 2\nplain footer",
        ]
    )

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=next(captures))

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
    )
    assert gate.is_busy("pgu-app:0.0") is True
    conn = FakeConnection([queue_row(31, "PGU-283", target_role="app")])
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in conn.requeued] == [31]
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "gate_defer"]
    assert conn.traces[1][6] == "region_changed"


def test_stable_last_lines_queue_notification_delivers_for_agent() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    capture = "\n".join(
        [
            "previous answer",
            "",
            "stable prompt",
            "  gpt-5.5 high · ~/Projects/pgu",
        ]
    )

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=capture)

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
        monotonic=lambda: now[0],
    )
    assert gate.is_busy("pgu-app:0.0") is True
    now[0] = 1.1
    conn = FakeConnection([queue_row(32, "PGU-283", target_role="app")])
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-app:0.0", "New ticket for you: PGU-283 -- Queue")]
    assert conn.acked == [32]
    assert conn.requeued == []
    assert trace_events(conn) == ["listener_claim", "send", "listener_ack"]
    assert conn.traces[1][5] == "idle"
    assert conn.traces[1][6] == "stable_idle"


def test_director_changing_last_lines_requeues_without_force_delivery() -> None:
    sent: list[tuple[str, str]] = []
    captures = iter(
        [
            "static scrollback\narbitrary changing content one\nstable footer",
            "static scrollback\narbitrary changing content two\nstable footer",
        ]
    )

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=next(captures))

    gate = PaneActivityGate(
        "/tmp/directorctl",
        stable_idle_seconds=1.0,
        capture_runner=capture_runner,
    )
    assert gate.is_busy("pgu-director:0.0") is True
    conn = FakeConnection(
        [
            queue_row(
                33,
                "PGU-283",
                target_role="director",
                message="PGU-283 -- Director notification",
                attempts=9,
            )
        ],
        queue_created_at={33: datetime.now(timezone.utc) - timedelta(hours=1)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert [item[0] for item in conn.requeued] == [33]
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "gate_defer"]
    assert "max_defer_force_deliver" not in trace_events(conn)


def test_default_stable_idle_window_uses_no_sleep_async_path() -> None:
    assert DEFAULT_STABLE_IDLE_SECONDS == 3.0
    assert DEFAULT_BUSY_REQUEUE_SECONDS == 3.0


def test_default_gate_delivers_batch_without_serial_sleep() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    rows = [
        queue_row(40, "PGU-340", target_role="ops"),
        queue_row(41, "PGU-341", target_role="app"),
        queue_row(42, "PGU-342", target_role="main"),
        queue_row(43, "PGU-343", target_role="audit"),
        queue_row(44, "PGU-344", target_role="inspector"),
    ]
    expected_count = len(rows)

    def capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="stable pane output\nstable prompt")

    conn = FakeConnection(rows)
    gate = PaneActivityGate("/tmp/directorctl", capture_runner=capture_runner, monotonic=lambda: now[0])
    for target in ["pgu-ops:0.0", "pgu-app:0.0", "pgu-main:0.0", "pgu-audit:0.0", "pgu-inspector:0.0"]:
        assert gate.is_busy(target) is True
    now[0] = DEFAULT_STABLE_IDLE_SECONDS + 0.1
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    started = time.perf_counter()
    assert listener.listen_once(max_notifications=expected_count) == expected_count
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert len(sent) == expected_count
    assert conn.acked == [40, 41, 42, 43, 44]
    assert conn.requeued == []


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
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-223 -- Queue")]


def test_default_sender_delegates_to_directorctl_send() -> None:
    calls: list[tuple[list[str], bool]] = []
    original_run = subprocess.run

    def fake_run(args: list[str], *, check: bool = False, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        return subprocess.CompletedProcess(args, 0)

    try:
        subprocess.run = fake_run  # type: ignore[assignment]
        DirectorctlSender("/tmp/directorctl")("pgu-ops:0.0", "New ticket for you: PGU-215 -- Submit")
    finally:
        subprocess.run = original_run  # type: ignore[assignment]

    assert calls == [(["/tmp/directorctl", "send", "pgu-ops:0.0", "New ticket for you: PGU-215 -- Submit"], True)]


def main() -> int:
    test_claimed_queue_row_delivers_to_assignee_pane_and_acks()
    test_eric_review_is_not_delivered()
    test_reconnect_relistens_after_connection_drop()
    test_idle_listener_consumes_notifies_generator_before_polling_again()
    test_send_timeout_requeues_and_does_not_block_next_pane()
    test_send_failure_uses_exponential_backoff()
    test_busy_pane_requeues_then_idle_pane_delivers_once()
    test_real_gate_busy_retry_is_fixed_then_delivers_after_stable_window()
    test_region_must_remain_stable_for_full_idle_window()
    test_busy_pane_never_force_delivers_old_notification()
    test_stale_implementation_nudge_for_cancelled_ticket_is_acked_not_delivered()
    test_stale_implementation_nudge_for_picked_up_ticket_is_acked_not_delivered()
    test_escalation_for_still_stuck_implementation_ticket_delivers_to_director()
    test_nudge_analysis_copy_delivers_to_director()
    test_director_busy_requeues_then_idle_delivers_once()
    test_director_busy_never_force_delivers_old_notification_until_idle()
    test_stable_last_lines_queue_notification_delivers_immediately()
    test_changing_arbitrary_region_waits()
    test_static_arbitrary_region_is_idle_and_delivers()
    test_changing_director_region_defers_without_backstop()
    test_batch_idle_notifications_deliver_promptly_without_serial_sleep()
    test_changing_last_lines_requeue_queue_notification()
    test_stable_last_lines_queue_notification_delivers_for_agent()
    test_director_changing_last_lines_requeues_without_force_delivery()
    test_default_stable_idle_window_uses_no_sleep_async_path()
    test_default_gate_delivers_batch_without_serial_sleep()
    test_acked_notification_does_not_repeat_across_restart()
    test_default_sender_delegates_to_directorctl_send()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
