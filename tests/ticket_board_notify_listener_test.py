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
    DirectorctlSender,
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
)


@dataclass(frozen=True)
class FakeNotify:
    payload: str


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

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
        next_attempt_at: datetime | None = None,
        notifications: list[str] | None = None,
        fail_on_notifies: bool = False,
        executed: list[Any] | None = None,
    ) -> None:
        self.queue_rows = queue_rows or []
        self.ticket_rows = ticket_rows if ticket_rows is not None else self._ticket_rows_for_queue(self.queue_rows)
        self.next_attempt_at = next_attempt_at
        self.notifications = notifications or []
        self.fail_on_notifies = fail_on_notifies
        self.executed = executed if executed is not None else []
        self.acked: list[int] = []
        self.requeued: list[tuple[int, tuple[Any, ...] | None]] = []
        self.traces: list[tuple[Any, ...] | None] = []
        self.notify_timeouts: list[float | None] = []
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
        if "next_notification_attempt" in statement_text:
            return FakeResult([(self.next_attempt_at,)])
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
        if "ack_notification" in statement_text:
            assert params is not None
            self.acked.append(int(params[0]))
        if "requeue_notification" in statement_text:
            assert params is not None
            self.requeued.append((int(params[0]), params))
        return FakeResult([])

    def notifies(self, *, timeout: float | None = None, stop_after: int | None = None) -> Any:
        self.notify_timeouts.append(timeout)
        if self.fail_on_notifies:
            raise psycopg.OperationalError("simulated connection drop")
        queued = [FakeNotify(payload) for payload in self.notifications]
        self.notifications = []
        if stop_after is not None:
            queued = queued[:stop_after]
        yield from queued


class StopAfterWaitConnection(FakeConnection):
    def __init__(self, stop_event: threading.Event) -> None:
        super().__init__()
        self.stop_event = stop_event
        self.claim_calls = 0
        self.notifies_advanced = 0

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        if "claim_notification" in str(statement):
            self.claim_calls += 1
            if self.claim_calls > 1:
                raise AssertionError("idle listener polled claim_notification again before waiting")
        return super().execute(statement, params)

    def notifies(self, *, timeout: float | None = None, stop_after: int | None = None) -> Any:
        self.notify_timeouts.append(timeout)
        self.notifies_advanced += 1
        self.stop_event.set()
        if False:
            yield FakeNotify("")


class FakeLogger:
    def __init__(self) -> None:
        self.infos: list[tuple[str, tuple[Any, ...]]] = []
        self.warnings: list[tuple[str, tuple[Any, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.infos.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append((message, args))

    def exception(self, message: str, *args: object) -> None:
        self.warning(message, *args)


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


def trace_events(conn: FakeConnection) -> list[str]:
    return [str(params[4]) for params in conn.traces if params is not None]


def hook_gate(tmp_path: Path) -> tuple[PaneHookStateStore, PaneActivityGate]:
    store = PaneHookStateStore(tmp_path)
    return store, PaneActivityGate(state_store=store)


def test_hook_state_writer_and_gate_idle_before_arrival_delivers_immediately() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(1, "PGU-304")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        started = time.perf_counter()
        delivered = listener.listen_once(max_notifications=1)
        elapsed = time.perf_counter() - started

    assert delivered == 1
    assert elapsed < 1.0
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-304 -- Queue")]
    assert conn.requeued == []
    assert conn.acked == [1]
    assert conn.traces[1][5] == "idle"
    assert conn.traces[1][6] == "hook_idle"


def test_external_hook_writer_records_state_file() -> None:
    with TemporaryStateDir() as tmp_path:
        subprocess.run(
            [
                str(ROOT / "scripts" / "ticket-board-pane-idle-hook"),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.Stop",
                "--state-dir",
                str(tmp_path),
            ],
            check=True,
        )
        state = PaneHookStateStore(tmp_path).read("pgu-ops:0.0")

    assert state is not None
    assert state.state == "idle"
    assert state.source == "codex.Stop"


def test_missing_hook_state_fails_safe_and_does_not_clobber() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        _store, gate = hook_gate(tmp_path)
        conn = FakeConnection([queue_row(2, "PGU-304")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 0

    assert sent == []
    assert conn.requeued[0][1][1] == f"{DEFAULT_BUSY_REQUEUE_SECONDS:g} seconds"
    assert conn.acked == []
    assert conn.traces[1][5] == "busy"
    assert conn.traces[1][6] == "no_hook_state"


def test_hook_busy_requeues_with_fixed_interval() -> None:
    sent: list[tuple[str, str]] = []
    delays: list[str] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=100.0)
        for attempts in (1, 2, 9):
            conn = FakeConnection([queue_row(3, "PGU-304", attempts=attempts)])
            listener = TicketBoardNotifyListener(
                conninfo="dbname=test",
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working,
                connector=lambda *args, conn=conn, **kwargs: conn,
                poll_seconds=0,
            )
            assert listener.listen_once(max_notifications=1) == 0
            delays.append(conn.requeued[0][1][1])

    assert sent == []
    assert delays == [f"{DEFAULT_BUSY_REQUEUE_SECONDS:g} seconds"] * 3


def test_hook_blocked_is_busy() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "blocked", source="codex.PermissionRequest", now=100.0)
        assert gate.is_busy("pgu-ops:0.0") is True
        assert gate.last_trace("pgu-ops:0.0").reason == "hook_blocked"  # type: ignore[union-attr]


def test_director_client_activity_latches_no_clobber_until_busy_idle_cycle() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    activity = [100]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["tmux", "list-clients", "-t", "pgu-director"]
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            director_composing_timeout_seconds=60.0,
            monotonic=lambda: now[0],
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        activity[0] = 101
        conn = FakeConnection(
            [queue_row(4, "PGU-304", target_role="director", message="PGU-304 -- Director notification")]
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
        assert conn.traces[1][6] == "human_composing"

        now[0] = 30.0
        held_conn = FakeConnection(
            [queue_row(5, "PGU-305", target_role="director", message="PGU-305 -- Director notification")]
        )
        held_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: held_conn,
            poll_seconds=0,
        )
        assert held_listener.listen_once(max_notifications=1) == 0
        assert sent == []
        assert held_conn.traces[1][6] == "human_composing"

        store.write("pgu-director:0.0", "busy", source="claude.UserPromptSubmit", now=20.0)
        assert gate.is_busy("pgu-director:0.0") is True
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=21.0)
        assert gate.is_busy("pgu-director:0.0") is False

        delivered_conn = FakeConnection(
            [queue_row(6, "PGU-306", target_role="director", message="PGU-306 -- Director notification")]
        )
        delivered_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: delivered_conn,
            poll_seconds=0,
        )
        assert delivered_listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-director:0.0", "PGU-306 -- Director notification")]


def test_director_abandoned_draft_timeout_releases_latch() -> None:
    now = [0.0]
    activity = [100]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            director_composing_timeout_seconds=5.0,
            monotonic=lambda: now[0],
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False
        activity[0] = 101
        assert gate.is_busy("pgu-director:0.0") is True
        now[0] = 5.1
        assert gate.is_busy("pgu-director:0.0") is False
        assert gate.last_trace("pgu-director:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_director_abandoned_draft_timeout_uses_latest_keystroke() -> None:
    now = [0.0]
    activity = [100]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            director_composing_timeout_seconds=5.0,
            monotonic=lambda: now[0],
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        now[0] = 1.0
        activity[0] = 101
        assert gate.is_busy("pgu-director:0.0") is True

        now[0] = 4.0
        activity[0] = 105
        assert gate.is_busy("pgu-director:0.0") is True

        now[0] = 8.5
        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]

        now[0] = 9.1
        assert gate.is_busy("pgu-director:0.0") is False
        assert gate.last_trace("pgu-director:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_listener_logs_missing_hook_state_on_startup() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        logger = FakeLogger()
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda _target, _message: None,
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: FakeConnection([]),
            logger=logger,  # type: ignore[arg-type]
        )

        listener._log_missing_hook_state()

    assert logger.warnings
    message, args = logger.warnings[0]
    assert "Pane hook state missing" in message
    assert "pgu-director:0.0" in args[0]
    assert "pgu-ops:0.0" not in args[0]


def test_send_failure_uses_exponential_backoff() -> None:
    conn = FakeConnection([queue_row(7, "PGU-304", attempts=4)])

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
    assert conn.requeued[0][1][1] == "40 seconds"
    assert conn.acked == []
    assert trace_events(conn) == ["listener_claim", "send_failed"]


def test_stale_notification_for_cancelled_ticket_is_acked_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [queue_row(8, "PGU-304", state="in_progress", assignee="ops")],
        ticket_rows={"PGU-304": ("cancelled", "ops", False, False)},
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
    assert conn.acked == [8]
    assert trace_events(conn) == ["listener_claim", "drop", "listener_ack"]


def test_eric_review_is_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: FakeConnection([]),
        poll_seconds=0,
    )

    assert listener.deliver_payload(transition_payload("PGU-213", new_state="eric_review")) is False
    assert sent == []


def test_reconnect_relistens_after_connection_drop() -> None:
    executed: list[Any] = []
    connections = [
        FakeConnection(fail_on_notifies=True, executed=executed),
        FakeConnection(executed=executed),
    ]
    stop_event = threading.Event()

    def connector(*args: object, **kwargs: object) -> FakeConnection:
        conn = connections.pop(0)
        if not connections:
            stop_event.set()
        return conn

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda _target, _message: None,
        activity_gate=lambda _target: False,
        connector=connector,
        reconnect_seconds=0,
        poll_seconds=0,
        stop_event=stop_event,
    )

    listener.run_forever(max_connections=2)

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

    listener.run_forever(max_connections=1)

    assert conn.claim_calls == 1
    assert conn.notifies_advanced == 1


def test_wait_timeout_uses_next_attempt_instead_of_poll_ceiling() -> None:
    conn = FakeConnection(next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda _target, _message: None,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=5,
    )

    timeout = listener._wait_timeout_seconds(conn)

    assert 0 <= timeout <= 1.1


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


class TemporaryStateDir:
    def __enter__(self) -> Path:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pgu-pane-state.")
        return Path(self._tmp.name)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmp.cleanup()


def main() -> int:
    test_hook_state_writer_and_gate_idle_before_arrival_delivers_immediately()
    test_external_hook_writer_records_state_file()
    test_missing_hook_state_fails_safe_and_does_not_clobber()
    test_hook_busy_requeues_with_fixed_interval()
    test_hook_blocked_is_busy()
    test_director_client_activity_latches_no_clobber_until_busy_idle_cycle()
    test_director_abandoned_draft_timeout_releases_latch()
    test_director_abandoned_draft_timeout_uses_latest_keystroke()
    test_listener_logs_missing_hook_state_on_startup()
    test_send_failure_uses_exponential_backoff()
    test_stale_notification_for_cancelled_ticket_is_acked_not_delivered()
    test_eric_review_is_not_delivered()
    test_reconnect_relistens_after_connection_drop()
    test_idle_listener_consumes_notifies_generator_before_polling_again()
    test_wait_timeout_uses_next_attempt_instead_of_poll_ceiling()
    test_default_sender_delegates_to_directorctl_send()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
