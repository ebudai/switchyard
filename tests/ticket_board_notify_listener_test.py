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
    DEFAULT_DIRECTORCTL,
    DirectorctlSender,
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
    display_message,
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
        ticket_rows: dict[str, tuple[str, str] | tuple[str, str, bool, bool] | tuple[str, str, bool, bool, bool]] | None = None,
        finish_current_blockers: dict[str, str] | None = None,
        in_progress_entries: dict[str, tuple[str, float | None]] | None = None,
        next_attempt_at: datetime | None = None,
        notifications: list[str] | None = None,
        fail_on_notifies: bool = False,
        executed: list[Any] | None = None,
        idle_turn_end_result: int = 0,
        idle_stall_result: int = 0,
    ) -> None:
        self.queue_rows = queue_rows or []
        self.ticket_rows = ticket_rows if ticket_rows is not None else self._ticket_rows_for_queue(self.queue_rows)
        self.finish_current_blockers = finish_current_blockers or {}
        self.in_progress_entries = in_progress_entries or {}
        self.next_attempt_at = next_attempt_at
        self.notifications = notifications or []
        self.fail_on_notifies = fail_on_notifies
        self.executed = executed if executed is not None else []
        self.idle_turn_end_result = idle_turn_end_result
        self.idle_stall_result = idle_stall_result
        self.acked: list[int] = []
        self.requeued: list[tuple[int, tuple[Any, ...] | None]] = []
        self.traces: list[tuple[Any, ...] | None] = []
        self.idle_turn_end_calls: list[tuple[Any, ...] | None] = []
        self.idle_stall_calls: list[tuple[Any, ...] | None] = []
        self.notify_timeouts: list[float | None] = []
        self.closed = False

    def _ticket_rows_for_queue(self, queue_rows: list[tuple[int, str, str, str, str, int]]) -> dict[str, tuple[str, str, bool, bool, bool]]:
        rows: dict[str, tuple[str, str, bool, bool, bool]] = {}
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
            rows[ticket_id] = (state, assignee, False, False, False)
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
        if "notify_idle_turn_end_nudges" in statement_text:
            self.idle_turn_end_calls.append(params)
            return FakeResult([(self.idle_turn_end_result,)])
        if "notify_idle_stall_nudges" in statement_text:
            self.idle_stall_calls.append(params)
            return FakeResult([(self.idle_stall_result,)])
        if "claim_notification" in statement_text:
            if not self.queue_rows:
                return FakeResult([])
            return FakeResult([self.queue_rows.pop(0)])
        if "finish_current_blocker" in statement_text:
            assert params is not None
            return FakeResult([(self._finish_current_blocker_for(str(params[0]), str(params[1])),)])
        if "current_ticket AS" in statement_text and "t.state = 'in_progress'" in statement_text:
            assert params is not None
            ticket_id = str(params[0])
            target_role = str(params[1])
            current_ticket_id = self._finish_current_blocker_for(ticket_id, target_role)
            if current_ticket_id:
                return FakeResult([(current_ticket_id,)])
            return FakeResult([])
        if "FROM ticket_board.tickets" in statement_text:
            assert params is not None
            row = self.ticket_rows.get(str(params[0]))
            if row is not None and len(row) == 2:
                row = (row[0], row[1], False, False)
            if row is not None and len(row) == 4:
                row = (row[0], row[1], row[2], row[3], False)
            return FakeResult([] if row is None else [row])
        if "ack_notification" in statement_text:
            assert params is not None
            self.acked.append(int(params[0]))
        if "requeue_notification" in statement_text:
            assert params is not None
            self.requeued.append((int(params[0]), params))
        return FakeResult([])

    def _ticket_number(self, ticket_id: str) -> int:
        try:
            return int(ticket_id.split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    def _finish_current_blocker_for(self, ticket_id: str, target_role: str) -> str:
        if self.in_progress_entries:
            current_entry = self.in_progress_entries.get(ticket_id)
            if current_entry is None or current_entry[0] != target_role:
                return ""
            current_entered_at = current_entry[1]
            current_number = self._ticket_number(ticket_id)
            older: list[tuple[float, int, str]] = []
            for candidate_id, (assignee, entered_at) in self.in_progress_entries.items():
                if candidate_id == ticket_id or assignee != target_role:
                    continue
                candidate_number = self._ticket_number(candidate_id)
                if entered_at is not None and current_entered_at is not None and entered_at < current_entered_at:
                    older.append((entered_at, candidate_number, candidate_id))
                elif (
                    entered_at is None
                    or current_entered_at is None
                    or entered_at == current_entered_at
                ) and candidate_number < current_number:
                    older.append((float("inf"), candidate_number, candidate_id))
            if not older:
                return ""
            return sorted(older)[0][2]
        return self.finish_current_blockers.get(ticket_id, self.finish_current_blockers.get(target_role, ""))

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


def constant_cursor_runner(output: str = "2 23 24") -> Any:
    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{output}\n")

    return runner


def sequenced_cursor_runner(*outputs: str) -> Any:
    values = list(outputs)
    fallback = values[-1] if values else "2 23 24"

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        value = values.pop(0) if values else fallback
        return subprocess.CompletedProcess(args, 0, stdout=f"{value}\n")

    return runner


def sequenced_capture_runner(*outputs: str) -> Any:
    values = list(outputs)
    fallback = values[-1] if values else ""

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        value = values.pop(0) if values else fallback
        return subprocess.CompletedProcess(args, 0, stdout=value)

    return runner


def targeted_capture_runner(target: str, *outputs: str) -> Any:
    values = list(outputs)
    fallback = values[-1] if values else ""

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        requested_target = str(args[args.index("-t") + 1]) if "-t" in args else ""
        if requested_target != target:
            return subprocess.CompletedProcess(args, 0, stdout="")
        value = values.pop(0) if values else fallback
        return subprocess.CompletedProcess(args, 0, stdout=value)

    return runner


def hook_gate(tmp_path: Path) -> tuple[PaneHookStateStore, PaneActivityGate]:
    store = PaneHookStateStore(tmp_path)
    return store, PaneActivityGate(
        state_store=store,
        cursor_position_runner=constant_cursor_runner(),
        capture_pane_runner=sequenced_capture_runner(""),
    )


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


def test_worker_cursor_advanced_holds_notification_delivery() -> None:
    sent: list[tuple[str, str]] = []
    cursor = ["5 23 24"]

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(78, "PGU-358A", target_role="ops", message="PGU-358A -- Ops notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 0

    assert sent == []
    assert len(conn.requeued) == 1
    assert conn.traces[1][6] == "human_composing"


def test_real_idle_cursor_position_delivers_instead_of_wedging() -> None:
    sent: list[tuple[str, str]] = []
    cursor = ["2 40 43"]

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(79, "PGU-365", target_role="ops", message="PGU-365 -- Ops notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-ops:0.0", "PGU-365 -- Ops notification")]
    assert conn.requeued == []
    assert conn.acked == [79]
    assert conn.traces[1][6] == "hook_idle"


def test_cursor_home_delivers_regardless_of_row() -> None:
    for cursor_value in ("2 40 43", "2 42 43"):
        sent: list[tuple[str, str]] = []

        def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{cursor_value}\n")

        with TemporaryStateDir() as tmp_path:
            store = PaneHookStateStore(tmp_path)
            gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
            store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
            conn = FakeConnection([queue_row(80, "PGU-365A", target_role="ops", message="PGU-365A -- Ops notification")])
            listener = TicketBoardNotifyListener(
                conninfo="dbname=test",
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working,
                connector=lambda *args, **kwargs: conn,
                poll_seconds=0,
            )

            assert listener.listen_once(max_notifications=1) == 1

        assert sent == [("pgu-ops:0.0", "PGU-365A -- Ops notification")]
        assert conn.acked == [80]
        assert conn.traces[1][6] == "hook_idle"


def test_pre_send_recheck_waits_and_holds_when_cursor_advances_on_second_read() -> None:
    sent: list[tuple[str, str]] = []
    sleep_calls: list[float] = []
    cursor_runner = sequenced_cursor_runner("2 23 24", "5 23 24")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(85, "PGU-364", target_role="ops", message="PGU-364 -- Ops notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            pre_send_recheck_delay_seconds=0.5,
            sleeper=lambda seconds: sleep_calls.append(seconds),
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 0

    assert sent == []
    assert sleep_calls == [0.5]
    assert conn.requeued == [(85, (85, "1 seconds", "pane busy"))]
    assert conn.traces[1][6] == "human_composing"
    assert json.loads(conn.traces[1][8])["phase"] == "pre_send_recheck"


def test_pre_send_recheck_delivers_when_cursor_stays_home() -> None:
    sent: list[tuple[str, str]] = []
    sleep_calls: list[float] = []
    cursor_runner = sequenced_cursor_runner("2 23 24", "2 23 24")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(86, "PGU-364A", target_role="ops", message="PGU-364A -- Ops notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            pre_send_recheck_delay_seconds=0.5,
            sleeper=lambda seconds: sleep_calls.append(seconds),
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 1

    assert sent == [("pgu-ops:0.0", "PGU-364A -- Ops notification")]
    assert sleep_calls == [0.5]
    assert conn.requeued == []
    assert conn.acked == [86]


def test_send_trace_records_composer_diagnostics_and_suspected_clobber() -> None:
    sent: list[tuple[str, str]] = []
    pane_before = """old output
──────────────────────────────────────────────────────────────────────────────────────────── Ops ──
❯ human draft
────────────────────────────────────────────────────────────────────────────────────────────────────────
"""
    pane_after = """old output
──────────────────────────────────────────────────────────────────────────────────────────── Ops ──
❯ human draft New ticket for you: PGU-482 -- Diagnostic
────────────────────────────────────────────────────────────────────────────────────────────────────────
"""

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-ops:0.0", pane_before, pane_before, pane_after),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(482, "PGU-482", target_role="ops", message="New ticket for you: PGU-482 -- Diagnostic")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: {
                "delivery_mode": "direct",
                "composer_before": {"active": True, "content_length": 11},
                "composer_after": {"active": True, "content_length": 48},
            } if not sent.append((target, message)) else {},
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            pre_send_recheck_delay_seconds=0,
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 1

    send_traces = [params for params in conn.traces if params is not None and params[4] == "send"]
    assert len(send_traces) == 1
    detail = json.loads(send_traces[0][8])
    assert detail["decision"] == "send"
    assert detail["composer_before"]["active"] is True
    assert detail["composer_before"]["content_length"] == len("human draft")
    assert detail["composer_before"]["content_sha256"]
    assert detail["composer_after"]["active"] is True
    assert detail["composer_changed"] is True
    assert detail["suspected_clobber"] is True
    assert detail["directorctl"]["delivery_mode"] == "direct"


def test_advanced_cursor_holds_immediately_without_second_read() -> None:
    sent: list[tuple[str, str]] = []
    sleep_calls: list[float] = []
    cursor_runner = sequenced_cursor_runner("5 23 24")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(87, "PGU-364B", target_role="ops", message="PGU-364B -- Ops notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            pre_send_recheck_delay_seconds=0.5,
            sleeper=lambda seconds: sleep_calls.append(seconds),
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 0

    assert sent == []
    assert sleep_calls == []
    assert conn.requeued == [(87, (87, "1 seconds", "pane busy"))]
    assert conn.traces[1][6] == "human_composing"


def test_listener_enqueues_idle_stall_nudges_from_hook_state() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        store.write("pgu-app:0.0", "busy", source="codex.PreToolUse", now=101.0)
        conn = FakeConnection(idle_stall_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 1

        assert len(conn.idle_stall_calls) == 1
        params = conn.idle_stall_calls[0]
        assert params is not None
        idle_since = json.loads(str(params[0]))
        assert set(idle_since) == {"ops"}
        assert idle_since["ops"].startswith("1970-01-01T00:01:40")
        assert params[1:] == ("45 seconds", "300 seconds", 2)


def test_advancing_working_timer_suppresses_idle_stall_nudge() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (12s · esc to interrupt)\n",
                "Working (13s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(idle_stall_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 0

    assert conn.idle_stall_calls == []
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer"  # type: ignore[union-attr]


def test_resetting_working_timer_suppresses_idle_stall_nudge() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (4s · esc to interrupt)\n",
                "Working (2s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(idle_stall_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 0

    assert conn.idle_stall_calls == []
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer"  # type: ignore[union-attr]


def test_static_working_timer_does_not_suppress_idle_stall_nudge() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (12s · esc to interrupt)\n",
                "Working (12s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(idle_stall_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 1

        assert len(conn.idle_stall_calls) == 1
        params = conn.idle_stall_calls[0]
        assert params is not None
        idle_since = json.loads(str(params[0]))
        assert set(idle_since) == {"ops"}
        assert gate.last_trace("pgu-ops:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_cached_static_working_timer_is_not_working_evidence() -> None:
    with TemporaryStateDir() as tmp_path:
        gate = PaneActivityGate(
            state_store=PaneHookStateStore(tmp_path),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (7s · esc to interrupt)\n",
            ),
            working_timer_sample_delay_seconds=0.0,
        )
        gate._last_working_timer_by_target["pgu-ops:0.0"] = 7

        assert gate._working_timer_trace("pgu-ops:0.0") is None

    assert gate._last_working_timer_by_target == {"pgu-ops:0.0": 7}


def test_cached_working_timer_increment_suppresses_next_idle_since_scan() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=sequenced_capture_runner(
                "Working (12s · esc to interrupt)\n",
                "Working (12s · esc to interrupt)\n",
                "Working (18s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)

        assert set(gate.idle_since_by_role(["ops"])) == {"ops"}
        assert gate.idle_since_by_role(["ops"]) == {}
        assert gate.last_trace("pgu-ops:0.0").reason == "working_timer"  # type: ignore[union-attr]


def test_listener_enqueues_idle_turn_end_nudges_on_each_turn_completion_idle() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        conn = FakeConnection(idle_turn_end_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
        )

        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)
        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert len(conn.idle_turn_end_calls) == 1
        startup_params = conn.idle_turn_end_calls[0]
        assert startup_params is not None
        startup_idle_since = json.loads(str(startup_params[0]))
        assert startup_idle_since == {"ops": "1970-01-01T00:01:40+00:00"}

        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=101.0)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=102.0)
        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert len(conn.idle_turn_end_calls) == 2
        first_params = conn.idle_turn_end_calls[1]
        assert first_params is not None
        first_idle_since = json.loads(str(first_params[0]))
        assert first_idle_since == {"ops": "1970-01-01T00:01:42+00:00"}

        assert listener.process_idle_turn_end_nudges(conn) == 0
        assert len(conn.idle_turn_end_calls) == 2

        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=103.0)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=104.0)
        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert len(conn.idle_turn_end_calls) == 3
        second_params = conn.idle_turn_end_calls[2]
        assert second_params is not None
        second_idle_since = json.loads(str(second_params[0]))
        assert second_idle_since == {"ops": "1970-01-01T00:01:44+00:00"}


def test_gemini_renamed_idle_hooks_count_as_turn_end_idle() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)

        store.write("pgu-inspector:0.0", "idle", source="gemini.SessionStart", now=100.0)
        assert gate.idle_since_by_role(["inspector"]) == {"inspector": "1970-01-01T00:01:40+00:00"}
        assert gate.turn_end_idle_since_by_role(["inspector"]) == {}

        store.write("pgu-inspector:0.0", "busy", source="gemini.PreInvocation", now=101.0)
        assert gate.turn_end_idle_since_by_role(["inspector"]) == {}

        store.write("pgu-inspector:0.0", "idle", source="gemini.PostInvocation", now=102.0)
        assert gate.turn_end_idle_since_by_role(["inspector"]) == {"inspector": "1970-01-01T00:01:42+00:00"}

        store.write("pgu-inspector:0.0", "idle", source="gemini.Stop", now=103.0)
        assert gate.turn_end_idle_since_by_role(["inspector"]) == {"inspector": "1970-01-01T00:01:43+00:00"}


def test_listener_enqueues_present_idle_fallback_once_per_idle_period() -> None:
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        now = time.time() - 60 * 60
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=now)
        conn = FakeConnection(idle_turn_end_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            present_idle_freshness_seconds=900,
        )

        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert listener.process_idle_turn_end_nudges(conn) == 0

        store.write("pgu-ops:0.0", "busy", source="test.UserPromptSubmit", now=now + 1)
        assert listener.process_idle_turn_end_nudges(conn) == 0
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=now + 2)
        assert listener.process_idle_turn_end_nudges(conn) == 1

    assert len(conn.idle_turn_end_calls) == 2
    first_params = conn.idle_turn_end_calls[0]
    second_params = conn.idle_turn_end_calls[1]
    assert first_params is not None
    assert second_params is not None
    assert json.loads(str(first_params[0])) == {"ops": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    assert json.loads(str(second_params[0])) == {"ops": datetime.fromtimestamp(now + 2, timezone.utc).isoformat()}


def test_present_idle_turn_end_fallback_does_not_probe_director() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("5 23 24"),
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=time.time())
        conn = FakeConnection(idle_turn_end_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            present_idle_freshness_seconds=900,
        )

        assert listener.process_idle_turn_end_nudges(conn) == 0

    assert conn.idle_turn_end_calls == []
    assert gate.last_trace("pgu-director:0.0") is None


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


def test_display_message_renames_analysis_stage_copy_without_touching_titles() -> None:
    assert (
        display_message("NUDGE PGU-328 -- Queue needs director triage in analysis")
        == "NUDGE PGU-328 -- Queue needs director triage in Triage"
    )
    assert (
        display_message("New ticket for you: PGU-29701 -- Analysis insert notify")
        == "New ticket for you: PGU-29701 -- Analysis insert notify"
    )


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


def test_stale_codex_busy_state_recovers_and_delivers() -> None:
    sent: list[tuple[str, str]] = []
    now = [1_800_000_300.0]
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=sequenced_capture_runner(""),
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: now[0],
        )
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=1_800_000_000.0)
        conn = FakeConnection([queue_row(89, "PGU-558")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 1
        recovered = store.read("pgu-ops:0.0")

    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-558 -- Queue")]
    assert conn.requeued == []
    assert conn.acked == [89]
    assert recovered is not None
    assert recovered.state == "idle"
    assert recovered.source == "listener.stale_codex_busy_recovery"


def test_stale_codex_busy_state_stays_busy_on_cold_cache_when_working_timer_resets() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (4s · esc to interrupt)\n",
                "Working (2s · esc to interrupt)\n",
            ),
            working_timer_sample_delay_seconds=0.0,
            idle_working_timer_sample_delay_seconds=1.1,
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_300.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=1_800_000_000.0)

        assert gate._last_working_timer_by_target == {}
        assert gate.is_busy("pgu-ops:0.0") is True
        state = store.read("pgu-ops:0.0")

    assert sleep_calls == [1.1]
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {"pgu-ops:0.0": 2}
    assert state is not None
    assert state.state == "busy"
    assert state.source == "codex.UserPromptSubmit"


def test_stale_codex_busy_state_recovers_on_cold_cache_when_working_timer_is_static() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (7s · esc to interrupt)\n",
                "Working (7s · esc to interrupt)\n",
            ),
            working_timer_sample_delay_seconds=0.0,
            idle_working_timer_sample_delay_seconds=1.1,
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_300.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=1_800_000_000.0)

        assert gate._last_working_timer_by_target == {}
        assert gate.is_busy("pgu-ops:0.0") is False
        state = store.read("pgu-ops:0.0")

    assert sleep_calls == [1.1]
    assert gate.last_trace("pgu-ops:0.0").reason == "stale_codex_busy_recovered"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {"pgu-ops:0.0": 7}
    assert state is not None
    assert state.state == "idle"
    assert state.source == "listener.stale_codex_busy_recovery"


def test_stale_codex_busy_state_stays_busy_when_human_is_composing() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("5 23 24"),
            capture_pane_runner=sequenced_capture_runner(""),
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_300.0,
        )
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True
        state = store.read("pgu-ops:0.0")

    assert gate.last_trace("pgu-ops:0.0").reason == "human_composing"  # type: ignore[union-attr]
    assert state is not None
    assert state.state == "busy"
    assert state.source == "codex.UserPromptSubmit"


def test_ticket_update_delivers_immediately_to_busy_worker_with_empty_composer() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "busy", source="codex.PreToolUse", now=100.0)
        conn = FakeConnection(
            [
                queue_row(
                    82,
                    "PGU-361",
                    kind="ticket_update",
                    state="in_progress",
                    assignee="ops",
                    target_role="ops",
                    message="PGU-361 (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
                )
            ],
            ticket_rows={"PGU-361": ("in_progress", "ops", False, False)},
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 1

    assert sent == [
        (
            "pgu-ops:0.0",
            "PGU-361 (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
        )
    ]
    assert conn.requeued == []
    assert conn.acked == [82]
    assert trace_events(conn) == ["listener_claim", "send", "listener_ack"]


def test_ticket_update_waits_for_worker_human_composing_to_clear() -> None:
    sent: list[tuple[str, str]] = []
    cursor = ["5 23 24"]

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)

        held_conn = FakeConnection(
            [
                queue_row(
                    83,
                    "PGU-361A",
                    kind="ticket_update",
                    state="in_progress",
                    assignee="ops",
                    target_role="ops",
                    message="PGU-361A (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
                )
            ],
            ticket_rows={"PGU-361A": ("in_progress", "ops", False, False)},
        )
        held_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: held_conn,
            poll_seconds=0,
        )
        assert held_listener.process_due_notifications(held_conn, max_notifications=1) == 0
        assert sent == []
        assert held_conn.requeued == [(83, (83, "1 seconds", "pane busy"))]
        assert held_conn.traces[1][6] == "human_composing"

        cursor[0] = "2 23 24"
        delivered_conn = FakeConnection(
            [
                queue_row(
                    84,
                    "PGU-361A",
                    kind="ticket_update",
                    state="in_progress",
                    assignee="ops",
                    target_role="ops",
                    message="PGU-361A (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
                )
            ],
            ticket_rows={"PGU-361A": ("in_progress", "ops", False, False)},
        )
        delivered_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: delivered_conn,
            poll_seconds=0,
        )
        assert delivered_listener.process_due_notifications(delivered_conn, max_notifications=1) == 1

    assert sent == [
        (
            "pgu-ops:0.0",
            "PGU-361A (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
        )
    ]
    assert delivered_conn.acked == [84]


def test_ticket_update_holds_busy_worker_with_advanced_cursor() -> None:
    sent: list[tuple[str, str]] = []
    cursor_runner = constant_cursor_runner("5 23 24")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(state_store=store, cursor_position_runner=cursor_runner)
        store.write("pgu-ops:0.0", "busy", source="codex.PreToolUse", now=100.0)
        conn = FakeConnection(
            [
                queue_row(
                    88,
                    "PGU-361B",
                    kind="ticket_update",
                    state="in_progress",
                    assignee="ops",
                    target_role="ops",
                    message="PGU-361B (in in_progress, that you own) was changed by director: spec updated -- re-read it before continuing.",
                )
            ],
            ticket_rows={"PGU-361B": ("in_progress", "ops", False, False)},
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.process_due_notifications(conn, max_notifications=1) == 0

    assert sent == []
    assert conn.requeued == [(88, (88, "1 seconds", "pane busy"))]
    assert conn.traces[1][6] == "human_composing"


def test_hook_busy_traces_only_first_gate_defer_per_notification() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=100.0)
        conn = FakeConnection(
            [
                queue_row(42, "PGU-318", attempts=1),
                queue_row(42, "PGU-318", attempts=2),
            ]
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=2) == 0

    assert sent == []
    assert len(conn.requeued) == 2
    assert trace_events(conn) == ["listener_claim", "gate_defer", "listener_claim"]


def test_new_assignment_waits_behind_existing_in_progress_ticket() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(
            [queue_row(43, "PGU-324", state="in_progress", assignee="ops", attempts=1)],
            finish_current_blockers={"ops": "PGU-321"},
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
    assert conn.acked == []
    assert len(conn.requeued) == 1
    assert conn.requeued[0][0] == 43
    assert conn.requeued[0][1][2] == "finish current"
    assert trace_events(conn) == ["listener_claim", "finish_current_defer"]


def test_two_in_progress_tickets_do_not_mutually_finish_current_deadlock() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(
            [
                queue_row(625, "PGU-336", state="in_progress", assignee="ops", attempts=1),
                queue_row(623, "PGU-334", state="in_progress", assignee="ops", attempts=1),
            ],
            in_progress_entries={
                "PGU-334": ("ops", 10.0),
                "PGU-336": ("ops", 20.0),
            },
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=2) == 1

    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-334 -- Queue")]
    assert conn.acked == [623]
    assert [row[0] for row in conn.requeued] == [625]
    assert conn.requeued[0][1][2] == "finish current"
    assert trace_events(conn) == [
        "listener_claim",
        "finish_current_defer",
        "listener_claim",
        "send",
        "listener_ack",
    ]


def test_finish_current_releases_next_oldest_after_oldest_leaves_in_progress() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(
            [queue_row(625, "PGU-336", state="in_progress", assignee="ops", attempts=2)],
            in_progress_entries={
                "PGU-336": ("ops", 20.0),
            },
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-336 -- Queue")]
    assert conn.acked == [625]
    assert conn.requeued == []


def test_three_in_progress_tickets_deliver_oldest_first_one_at_a_time() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(
            [
                queue_row(102, "PGU-102", state="in_progress", assignee="ops", attempts=1),
                queue_row(101, "PGU-101", state="in_progress", assignee="ops", attempts=1),
                queue_row(100, "PGU-100", state="in_progress", assignee="ops", attempts=1),
            ],
            in_progress_entries={
                "PGU-100": ("ops", 10.0),
                "PGU-101": ("ops", 20.0),
                "PGU-102": ("ops", 30.0),
            },
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=3) == 1

    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-100 -- Queue")]
    assert conn.acked == [100]
    assert [row[0] for row in conn.requeued] == [102, 101]


def test_finish_current_missing_entry_time_falls_back_to_lowest_ticket_number() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection(
            [queue_row(102, "PGU-102", state="in_progress", assignee="ops", attempts=1)],
            in_progress_entries={
                "PGU-100": ("ops", 30.0),
                "PGU-101": ("ops", 10.0),
                "PGU-102": ("ops", None),
            },
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
    assert conn.requeued[0][0] == 102
    finish_current_traces = [params for params in conn.traces if params is not None and params[4] == "finish_current_defer"]
    assert json.loads(finish_current_traces[0][8])["current_ticket_id"] == "PGU-100"


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
    cursor = ["2 23 24"]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["tmux", "list-clients", "-t", "pgu-director"]
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=60.0,
            monotonic=lambda: now[0],
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        activity[0] = 101
        cursor[0] = "5 23 24"
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

        store.write("pgu-director:0.0", "busy", source="claude.UserPromptSubmit", now=60.0)
        assert gate.is_busy("pgu-director:0.0") is True
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=61.0)
        cursor[0] = "2 23 24"
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


def test_director_restart_window_holds_stale_idle_until_busy_idle_cycle() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    wall = [50.0]
    activity = [100]
    cursor = ["2 23 24"]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["tmux", "list-clients", "-t", "pgu-director"]
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=60.0,
            monotonic=lambda: now[0],
            wall_time=lambda: wall[0],
        )

        startup_conn = FakeConnection(
            [queue_row(70, "PGU-353", target_role="director", message="PGU-353 -- Director notification")]
        )
        startup_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: startup_conn,
            poll_seconds=0,
        )
        assert startup_listener.listen_once(max_notifications=1) == 0
        assert sent == []
        assert startup_conn.traces[1][6] == "startup_latch_unestablished"

        now[0] = 1.0
        activity[0] = 101
        cursor[0] = "5 23 24"
        composing_conn = FakeConnection(
            [queue_row(71, "PGU-354", target_role="director", message="PGU-354 -- Director notification")]
        )
        composing_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: composing_conn,
            poll_seconds=0,
        )
        assert composing_listener.listen_once(max_notifications=1) == 0
        assert sent == []
        assert composing_conn.traces[1][6] == "human_composing"

        store.write("pgu-director:0.0", "busy", source="claude.UserPromptSubmit", now=60.0)
        assert gate.is_busy("pgu-director:0.0") is True
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=61.0)
        cursor[0] = "2 23 24"
        assert gate.is_busy("pgu-director:0.0") is False

        delivered_conn = FakeConnection(
            [queue_row(72, "PGU-355", target_role="director", message="PGU-355 -- Director notification")]
        )
        delivered_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: delivered_conn,
            poll_seconds=0,
        )
        assert delivered_listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-director:0.0", "PGU-355 -- Director notification")]


def test_director_restart_window_timeout_releases_flat_idle_without_busy_cycle() -> None:
    sent: list[tuple[str, str]] = []
    now = [0.0]
    wall = [50.0]
    activity = [100]
    cursor_runner = constant_cursor_runner()

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["tmux", "list-clients", "-t", "pgu-director"]
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=60.0,
            director_startup_hold_seconds=1.0,
            monotonic=lambda: now[0],
            wall_time=lambda: wall[0],
        )

        startup_conn = FakeConnection(
            [queue_row(74, "PGU-357", target_role="director", message="PGU-357 -- Director notification")]
        )
        startup_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: startup_conn,
            poll_seconds=0,
        )
        assert startup_listener.listen_once(max_notifications=1) == 0
        assert sent == []
        assert startup_conn.traces[1][6] == "startup_latch_unestablished"

        now[0] = 1.5
        released_conn = FakeConnection(
            [queue_row(75, "PGU-358", target_role="director", message="PGU-358 -- Director notification")]
        )
        released_listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: released_conn,
            poll_seconds=0,
        )
        assert released_listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-director:0.0", "PGU-358 -- Director notification")]
    assert released_conn.traces[1][6] == "hook_idle"


def test_director_pre_send_recheck_blocks_typing_that_starts_after_initial_idle_check() -> None:
    sent: list[tuple[str, str]] = []
    wall = [0.0]
    activity_values = [100, 100, 101]
    cursor_runner = sequenced_cursor_runner("2 23 24", "2 23 24", "5 23 24")

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["tmux", "list-clients", "-t", "pgu-director"]
        value = activity_values.pop(0) if activity_values else 101
        return subprocess.CompletedProcess(args, 0, stdout=f"{value}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=60.0,
            wall_time=lambda: wall[0],
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)

        conn = FakeConnection(
            [queue_row(73, "PGU-356", target_role="director", message="PGU-356 -- Director notification")]
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
    assert len(conn.requeued) == 1
    assert conn.traces[1][6] == "human_composing"
    assert json.loads(conn.traces[1][8])["phase"] == "pre_send_recheck"


def test_idle_nudge_enumeration_does_not_touch_director_composing_latch() -> None:
    sent: list[tuple[str, str]] = []
    wall = [0.0]

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            director_composing_timeout_seconds=60.0,
            wall_time=lambda: wall[0],
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)

        warm_conn = FakeConnection(idle_turn_end_result=0, idle_stall_result=0)
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: warm_conn,
            poll_seconds=0,
        )
        assert listener.process_idle_turn_end_nudges(warm_conn) == 0
        assert listener.process_idle_stall_nudges(warm_conn) == 0

        conn = FakeConnection(
            [queue_row(76, "PGU-359", target_role="director", message="PGU-359 -- Director notification")]
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )
        assert listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-director:0.0", "PGU-359 -- Director notification")]
    assert conn.acked == [76]
    assert conn.traces[1][6] == "hook_idle"


def test_director_paused_mid_compose_stays_held_until_busy_idle_cycle() -> None:
    now = [0.0]
    activity = [100]
    cursor = ["2 23 24"]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=5.0,
            monotonic=lambda: now[0],
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        activity[0] = 101
        cursor[0] = "5 23 24"
        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]

        now[0] = 30.0
        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]

        store.write("pgu-director:0.0", "busy", source="claude.UserPromptSubmit", now=60.0)
        assert gate.is_busy("pgu-director:0.0") is True

        store.write("pgu-director:0.0", "idle", source="claude.Stop", now=61.0)
        cursor[0] = "2 23 24"
        assert gate.is_busy("pgu-director:0.0") is False
        assert gate.last_trace("pgu-director:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_director_idle_without_detected_activity_does_not_false_latch() -> None:
    now = [0.0]
    activity = [100]
    cursor_runner = constant_cursor_runner()

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=5.0,
            monotonic=lambda: now[0],
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        now[0] = 600.0
        assert gate.is_busy("pgu-director:0.0") is False
        assert gate.last_trace("pgu-director:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_director_erased_draft_releases_without_submit() -> None:
    now = [0.0]
    activity = [100]
    cursor = ["2 23 24"]

    def client_activity_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{activity[0]}\n")

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            client_activity_runner=client_activity_runner,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=5.0,
            monotonic=lambda: now[0],
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)
        assert gate.is_busy("pgu-director:0.0") is False

        activity[0] = 101
        cursor[0] = "5 23 24"
        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]

        now[0] = 30.0
        cursor[0] = "2 23 24"
        assert gate.is_busy("pgu-director:0.0") is False
        assert gate.last_trace("pgu-director:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_cursor_advanced_never_releases_on_elapsed_time() -> None:
    now = [0.0]
    cursor = ["5 23 24"]

    def cursor_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=f"{cursor[0]}\n")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=cursor_runner,
            director_composing_timeout_seconds=0.01,
            monotonic=lambda: now[0],
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.Notification.idle_prompt", now=10.0)

        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]

        now[0] = 10_000.0
        assert gate.is_busy("pgu-director:0.0") is True
        assert gate.last_trace("pgu-director:0.0").reason == "human_composing"  # type: ignore[union-attr]


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


def test_terminal_transition_notification_is_delivered_to_prior_owner() -> None:
    sent: list[tuple[str, str]] = []
    message = "PGU-405 -- Terminal notify fixture cancelled"
    conn = FakeConnection(
        [
            queue_row(
                9,
                "PGU-405",
                state="cancelled",
                assignee="ops",
                target_role="ops",
                message=message,
            )
        ],
        ticket_rows={"PGU-405": ("cancelled", "ops", False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, text: sent.append((target, text)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-ops:0.0", message)]
    assert conn.acked == [9]


def test_queued_idle_reminder_drops_after_ticket_becomes_manually_controlled() -> None:
    sent: list[tuple[str, str]] = []
    conn = FakeConnection(
        [
            queue_row(
                90,
                "PGU-549",
                kind="idle_reminder",
                state="analysis",
                assignee="ops",
                target_role="director",
                message="PGU-549 is still in analysis and you haven't advanced it.",
            )
        ],
        ticket_rows={"PGU-549": ("analysis", "ops", True, False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, text: sent.append((target, text)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert conn.acked == [90]
    assert trace_events(conn) == ["listener_claim", "drop", "listener_ack"]


def test_queued_transition_drops_after_ticket_becomes_parked_or_blocked() -> None:
    for notification_id, ticket_row in (
        (91, ("analysis", "ops", False, True, False)),
        (92, ("analysis", "ops", False, False, True)),
    ):
        sent: list[tuple[str, str]] = []
        conn = FakeConnection(
            [
                queue_row(
                    notification_id,
                    f"PGU-{notification_id}",
                    kind="transition",
                    state="analysis",
                    assignee="ops",
                    target_role="director",
                    message=f"New ticket for you: PGU-{notification_id}",
                )
            ],
            ticket_rows={f"PGU-{notification_id}": ticket_row},
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, text: sent.append((target, text)),
            activity_gate=lambda _target: False,
            connector=lambda *args, conn=conn, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 0
        assert sent == []
        assert conn.acked == [notification_id]
        assert trace_events(conn) == ["listener_claim", "drop", "listener_ack"]


def test_idle_reminder_repeat_escalation_delivers_to_director() -> None:
    sent: list[tuple[str, str]] = []
    message = "ops was reminded about PGU-366 (in in_progress) and still hasn't advanced it -- may be stuck."
    conn = FakeConnection(
        [
            queue_row(
                81,
                "PGU-366",
                kind="escalation",
                state="in_progress",
                assignee="ops",
                target_role="director",
                message=message,
            )
        ],
        ticket_rows={"PGU-366": ("in_progress", "ops", False, False)},
    )
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, text: sent.append((target, text)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-director:0.0", message)]
    assert conn.acked == [81]


def test_eric_review_delivers_to_director_for_uat() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: FakeConnection([]),
        poll_seconds=0,
    )

    assert listener.deliver_payload(transition_payload("PGU-213", title="UAT gate", new_state="eric_review")) is True
    assert sent == [("pgu-director:0.0", "PGU-213 -- UAT gate ready for Eric UAT")]


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
    calls: list[tuple[list[str], bool, bool, bool]] = []
    original_run = subprocess.run

    def fake_run(args: list[str], *, check: bool = False, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, check, bool(kwargs.get("capture_output")), bool(kwargs.get("text"))))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout='directorctl: diagnostic {"delivery_mode":"direct","forced_delivery":false}\n',
        )

    try:
        subprocess.run = fake_run  # type: ignore[assignment]
        diagnostic = DirectorctlSender("/tmp/directorctl")("pgu-ops:0.0", "New ticket for you: PGU-215 -- Submit")
    finally:
        subprocess.run = original_run  # type: ignore[assignment]

    assert calls == [(["/tmp/directorctl", "send", "pgu-ops:0.0", "New ticket for you: PGU-215 -- Submit"], True, True, True)]
    assert diagnostic == {"delivery_mode": "direct", "forced_delivery": False}


def test_default_directorctl_path_is_canonical_runtime() -> None:
    assert DEFAULT_DIRECTORCTL == "/home/agent/bin/directorctl"


class TemporaryStateDir:
    def __enter__(self) -> Path:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pgu-pane-state.")
        return Path(self._tmp.name)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmp.cleanup()


def main() -> int:
    test_hook_state_writer_and_gate_idle_before_arrival_delivers_immediately()
    test_worker_cursor_advanced_holds_notification_delivery()
    test_real_idle_cursor_position_delivers_instead_of_wedging()
    test_cursor_home_delivers_regardless_of_row()
    test_pre_send_recheck_waits_and_holds_when_cursor_advances_on_second_read()
    test_pre_send_recheck_delivers_when_cursor_stays_home()
    test_send_trace_records_composer_diagnostics_and_suspected_clobber()
    test_advanced_cursor_holds_immediately_without_second_read()
    test_listener_enqueues_idle_stall_nudges_from_hook_state()
    test_advancing_working_timer_suppresses_idle_stall_nudge()
    test_resetting_working_timer_suppresses_idle_stall_nudge()
    test_static_working_timer_does_not_suppress_idle_stall_nudge()
    test_cached_static_working_timer_is_not_working_evidence()
    test_cached_working_timer_increment_suppresses_next_idle_since_scan()
    test_listener_enqueues_idle_turn_end_nudges_on_each_turn_completion_idle()
    test_gemini_renamed_idle_hooks_count_as_turn_end_idle()
    test_listener_enqueues_present_idle_fallback_once_per_idle_period()
    test_present_idle_turn_end_fallback_does_not_probe_director()
    test_external_hook_writer_records_state_file()
    test_display_message_renames_analysis_stage_copy_without_touching_titles()
    test_missing_hook_state_fails_safe_and_does_not_clobber()
    test_hook_busy_requeues_with_fixed_interval()
    test_stale_codex_busy_state_recovers_and_delivers()
    test_stale_codex_busy_state_stays_busy_on_cold_cache_when_working_timer_resets()
    test_stale_codex_busy_state_recovers_on_cold_cache_when_working_timer_is_static()
    test_stale_codex_busy_state_stays_busy_when_human_is_composing()
    test_ticket_update_delivers_immediately_to_busy_worker_with_empty_composer()
    test_ticket_update_waits_for_worker_human_composing_to_clear()
    test_ticket_update_holds_busy_worker_with_advanced_cursor()
    test_hook_busy_traces_only_first_gate_defer_per_notification()
    test_new_assignment_waits_behind_existing_in_progress_ticket()
    test_two_in_progress_tickets_do_not_mutually_finish_current_deadlock()
    test_finish_current_releases_next_oldest_after_oldest_leaves_in_progress()
    test_three_in_progress_tickets_deliver_oldest_first_one_at_a_time()
    test_finish_current_missing_entry_time_falls_back_to_lowest_ticket_number()
    test_hook_blocked_is_busy()
    test_director_client_activity_latches_no_clobber_until_busy_idle_cycle()
    test_director_restart_window_holds_stale_idle_until_busy_idle_cycle()
    test_director_restart_window_timeout_releases_flat_idle_without_busy_cycle()
    test_director_pre_send_recheck_blocks_typing_that_starts_after_initial_idle_check()
    test_idle_nudge_enumeration_does_not_touch_director_composing_latch()
    test_director_paused_mid_compose_stays_held_until_busy_idle_cycle()
    test_director_idle_without_detected_activity_does_not_false_latch()
    test_director_erased_draft_releases_without_submit()
    test_cursor_advanced_never_releases_on_elapsed_time()
    test_listener_logs_missing_hook_state_on_startup()
    test_send_failure_uses_exponential_backoff()
    test_stale_notification_for_cancelled_ticket_is_acked_not_delivered()
    test_terminal_transition_notification_is_delivered_to_prior_owner()
    test_queued_idle_reminder_drops_after_ticket_becomes_manually_controlled()
    test_queued_transition_drops_after_ticket_becomes_parked_or_blocked()
    test_idle_reminder_repeat_escalation_delivers_to_director()
    test_eric_review_delivers_to_director_for_uat()
    test_reconnect_relistens_after_connection_drop()
    test_idle_listener_consumes_notifies_generator_before_polling_again()
    test_wait_timeout_uses_next_attempt_instead_of_poll_ceiling()
    test_default_sender_delegates_to_directorctl_send()
    test_default_directorctl_path_is_canonical_runtime()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
