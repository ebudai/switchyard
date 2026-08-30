#!/usr/bin/env python3
"""Regression tests for the PostgreSQL ticket-board notification queue worker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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

from ticket_board_pane_env import (
    candidate_live_pane_state_paths,
    pane_state_record_anomalies,
    pane_state_record_anomaly_diff,
    snapshot_diff,
    snapshot_file_set_diff,
    snapshot_paths_file_set,
    snapshot_paths,
    strip_ticket_board_pane_env,
)
from ticket_board_live_pane_guard import (
    LivePaneShelloutGuard,
    assert_capture_pane_guard_blocks_without_shelling_out,
    run_module_tests_with_live_pane_guard,
)

LIVE_PANE_STATE_PATHS = candidate_live_pane_state_paths()
strip_ticket_board_pane_env(os.environ)

from scripts.ticket_board import notify_listener
from scripts.ticket_board.notify_listener import (
    DEFAULT_DIRECTORCTL,
    DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS,
    DEFAULT_REQUEUE_BASE_SECONDS,
    DEFAULT_REQUEUE_MAX_SECONDS,
    DirectorctlSender,
    FINISH_CURRENT_REQUEUE_ERROR,
    PaneActivityGate as RealPaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
    delivery_failure_reason,
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
        self.dead_lettered: list[tuple[int, tuple[Any, ...] | None]] = []
        self.traces: list[tuple[Any, ...] | None] = []
        self.reset_backoff_calls: list[tuple[Any, ...] | None] = []
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
        if "dead_letter_notification" in statement_text:
            assert params is not None
            self.dead_lettered.append((int(params[0]), params))
        if "requeue_notification" in statement_text:
            assert params is not None
            self.requeued.append((int(params[0]), params))
        if "reset_notification_backoff_for_idle_roles" in statement_text:
            self.reset_backoff_calls.append(params)
            return FakeResult([(0,)])
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


def targeted_alternating_capture_runner(target: str, *prefix_outputs: str) -> Any:
    values = list(prefix_outputs)
    index = 0

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal index
        requested_target = str(args[args.index("-t") + 1]) if "-t" in args else ""
        if requested_target != target:
            return subprocess.CompletedProcess(args, 0, stdout="")
        if values:
            value = values.pop(0)
        else:
            value = f"frame {index % 2}\n"
            index += 1
        return subprocess.CompletedProcess(args, 0, stdout=value)

    return runner


def spinner_aliasing_capture_runner(clock: dict[str, float]) -> Any:
    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        phase = round(clock["elapsed"], 2)
        frame = "same" if phase in {0.0, 1.2} else "changed"
        return subprocess.CompletedProcess(args, 0, stdout=f"Roosting... {frame}\n")

    return runner


def periodic_spinner_capture_runner(clock: dict[str, float], *, frames: int, frame_interval: float) -> Any:
    period = frames * frame_interval

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        phase = (clock["phase"] + clock["elapsed"]) % period
        frame = int(phase / frame_interval)
        return subprocess.CompletedProcess(args, 0, stdout=f"Roosting... frame-{frame}\n")

    return runner


def assert_uneven_probe_sleeps(sleep_calls: list[float], total_delay: float, *, probes: int = 1) -> None:
    first_gap = total_delay * (23.0 / 120.0)
    second_gap = total_delay * (1.0 / 15.0)
    final_gap = total_delay - first_gap - second_gap
    expected = [first_gap, second_gap, final_gap] * probes
    assert [round(value, 6) for value in sleep_calls] == [round(value, 6) for value in expected]


def failing_capture_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise subprocess.SubprocessError("capture failed")


def PaneActivityGate(*args: Any, **kwargs: Any) -> RealPaneActivityGate:
    kwargs.setdefault("capture_pane_runner", sequenced_capture_runner("ordinary idle content\n> \n"))
    return RealPaneActivityGate(*args, **kwargs)


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


def test_hermes_turn_end_idle_delivers_without_foreign_runtime_trace() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=sequenced_capture_runner("Hermes idle prompt\n"),
            role_runtimes={"inspector": "hermes"},
        )
        store.write("pgu-inspector:0.0", "idle", source="hermes.post_llm_call", now=100.0)
        conn = FakeConnection([queue_row(774, "PGU-774", target_role="inspector", message="PGU-774 -- Hermes notification")])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-inspector:0.0", "PGU-774 -- Hermes notification")]
    assert conn.requeued == []
    assert conn.acked == [774]
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
    assert conn.requeued == [(85, (85, f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds", "pane busy"))]
    assert conn.traces[1][6] == "human_composing"
    detail = json.loads(conn.traces[1][8])
    assert detail["decision_reason"] == "human_composing"
    assert detail["anti_clobber"]["reason"] == "human_composing"


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
    assert conn.requeued == [(87, (87, f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds", "pane busy"))]
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
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 1

        assert len(conn.reset_backoff_calls) == 1
        reset_params = conn.reset_backoff_calls[0]
        assert reset_params is not None
        assert json.loads(str(reset_params[0])) == {"ops": "1970-01-01T00:01:40+00:00"}
        assert len(conn.idle_stall_calls) == 1
        params = conn.idle_stall_calls[0]
        assert params is not None
        idle_since = json.loads(str(params[0]))
        assert set(idle_since) == {"ops"}
        assert idle_since["ops"].startswith("1970-01-01T00:01:40")
        assert DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS == 30 * 60.0
        assert params[1:4] == ("45 seconds", "1800 seconds", 2)
        assert set(json.loads(str(params[4]))) == {"app"}


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
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)
        conn = FakeConnection(idle_stall_result=0)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 0

    assert len(conn.idle_stall_calls) == 1
    params = conn.idle_stall_calls[0]
    assert params is not None
    assert json.loads(str(params[0])) == {}
    assert set(json.loads(str(params[4]))) == {"ops"}
    assert conn.reset_backoff_calls == []


def test_idle_role_resets_busy_backoff_before_claiming_due_notifications() -> None:
    sent: list[tuple[str, str]] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        conn = FakeConnection([queue_row(32, "PGU-627", target_role="ops", attempts=20)])
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        assert listener.listen_once(max_notifications=1) == 1

    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-627 -- Queue")]
    assert conn.requeued == []
    assert conn.acked == [32]
    executed_sql = [str(statement) for statement, _params in conn.executed]
    reset_index = next(index for index, statement in enumerate(executed_sql) if "reset_notification_backoff_for_idle_roles" in statement)
    claim_index = next(index for index, statement in enumerate(executed_sql) if "claim_notification" in statement)
    assert reset_index < claim_index
    assert gate.last_trace("pgu-ops:0.0").reason == "hook_idle"  # type: ignore[union-attr]


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
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)
        conn = FakeConnection(idle_stall_result=0)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
            idle_stall_grace_seconds=45,
            idle_stall_nudge_cadence_seconds=300,
            idle_stall_escalate_after=2,
        )

        assert listener.process_idle_stall_nudges(conn) == 0

    assert len(conn.idle_stall_calls) == 1
    params = conn.idle_stall_calls[0]
    assert params is not None
    assert json.loads(str(params[0])) == {}
    assert set(json.loads(str(params[4]))) == {"ops"}
    assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]


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
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)
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
        assert json.loads(str(params[4])) == {}
        assert gate.last_trace("pgu-ops:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]


def test_spinner_aliasing_during_startup_does_not_false_idle() -> None:
    sleeps: list[float] = []
    clock = {"elapsed": 0.0}

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["elapsed"] += seconds

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=spinner_aliasing_capture_runner(clock),
            idle_working_timer_sample_delay_seconds=1.2,
            sleeper=sleeper,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)

        assert gate.idle_since_by_role(["ops"]) == {}
        trace = gate.last_trace("pgu-ops:0.0")
        assert trace is not None
        assert trace.busy is True
        assert trace.reason == "pane_content_changed"

    assert_uneven_probe_sleeps(sleeps, 1.2)


def test_startup_spinner_period_sweep_does_not_false_idle() -> None:
    periods = [
        (4, 0.1),
        (2, 0.1),
        (2, 0.3),
        (3, 0.13),
        (4, 0.15),
        (4, 0.3),
    ]
    failures: list[str] = []
    probes = 0

    for frames, frame_interval in periods:
        period = frames * frame_interval
        for round_index in range(20):
            sleeps: list[float] = []
            clock = {
                "elapsed": 0.0,
                "phase": period * ((round_index + 0.37) / 20.0),
            }

            def sleeper(seconds: float, *, clock: dict[str, float] = clock) -> None:
                sleeps.append(seconds)
                clock["elapsed"] += seconds

            with TemporaryStateDir() as tmp_path:
                store = PaneHookStateStore(tmp_path)
                gate = PaneActivityGate(
                    state_store=store,
                    cursor_position_runner=constant_cursor_runner(),
                    capture_pane_runner=periodic_spinner_capture_runner(
                        clock,
                        frames=frames,
                        frame_interval=frame_interval,
                    ),
                    idle_working_timer_sample_delay_seconds=1.2,
                    sleeper=sleeper,
                )
                store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)

                if gate.idle_since_by_role(["ops"]):
                    failures.append(f"{frames}x{frame_interval}@{clock['phase']:.6f}")
            assert_uneven_probe_sleeps(sleeps, 1.2)
            probes += 1

    assert failures == []
    assert probes == 120


def test_multi_sample_working_timer_probe_still_reports_genuine_idle() -> None:
    sleeps: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "ordinary idle content\n",
                "ordinary idle content\n",
                "ordinary idle content\n",
            ),
            idle_working_timer_sample_delay_seconds=1.2,
            sleeper=sleeps.append,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)

        idle_since = gate.idle_since_by_role(["ops"])
        assert set(idle_since) == {"ops"}
        trace = gate.last_trace("pgu-ops:0.0")
        assert trace is not None
        assert trace.busy is False
        assert trace.reason == "working_timer_idle"

    assert_uneven_probe_sleeps(sleeps, 1.2)


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


def test_changed_pane_content_suppresses_idle_since_scan() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=sequenced_capture_runner("stable screen\n", "changed screen\n"),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)

        assert gate.idle_since_by_role(["ops"]) == {}
        assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]


def test_listener_enqueues_idle_turn_end_nudges_on_each_turn_completion_idle() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=failing_capture_runner,
        )
        conn = FakeConnection(idle_turn_end_result=1)
        listener = TicketBoardNotifyListener(
            conninfo="",
            activity_gate=gate.is_working,
            sender=lambda _target, _message: None,
        )

        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=100.0)
        assert listener.process_idle_turn_end_nudges(conn) == 0
        assert conn.idle_turn_end_calls == []

        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=101.0)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=102.0)
        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert len(conn.idle_turn_end_calls) == 1
        first_params = conn.idle_turn_end_calls[0]
        assert first_params is not None
        first_idle_since = json.loads(str(first_params[0]))
        assert first_idle_since == {"ops": "1970-01-01T00:01:42+00:00"}

        assert listener.process_idle_turn_end_nudges(conn) == 0
        assert len(conn.idle_turn_end_calls) == 1

        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=103.0)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=104.0)
        assert listener.process_idle_turn_end_nudges(conn) == 1
        assert len(conn.idle_turn_end_calls) == 2
        second_params = conn.idle_turn_end_calls[1]
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
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner(),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (7s · esc to interrupt)\n",
            ),
            sleeper=lambda _seconds: None,
        )
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
    assert conn.requeued[0][1][1] == f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds"
    assert conn.acked == []
    assert conn.traces[1][5] == "busy"
    assert conn.traces[1][6] == "no_hook_state"


def test_hook_busy_requeues_with_exponential_backoff() -> None:
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
    assert delays == [
        f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds",
        f"{DEFAULT_REQUEUE_BASE_SECONDS * 2:g} seconds",
        f"{DEFAULT_REQUEUE_MAX_SECONDS:g} seconds",
    ]


def test_backoff_sequence_is_pinned_to_five_minute_cap() -> None:
    listener = TicketBoardNotifyListener(conninfo="", poll_seconds=0)

    assert [listener._backoff_seconds(attempts) for attempts in range(1, 9)] == [
        5.0,
        10.0,
        20.0,
        40.0,
        80.0,
        160.0,
        300.0,
        300.0,
    ]


def test_busy_backoff_attempts_stay_below_ten_in_five_minutes() -> None:
    elapsed = 0.0
    attempts = 0
    listener = TicketBoardNotifyListener(conninfo="", poll_seconds=0)
    while elapsed < 5 * 60:
        attempts += 1
        elapsed += listener._backoff_seconds(attempts)

    assert attempts < 10


def test_finish_current_backoff_attempts_stay_below_ten_in_five_minutes() -> None:
    elapsed = 0.0
    attempts = 0
    listener = TicketBoardNotifyListener(conninfo="", poll_seconds=0)
    while elapsed < 5 * 60:
        attempts += 1
        elapsed += listener._backoff_seconds(attempts)

    assert attempts < 10


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

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}
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

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "stale_codex_busy_recovered"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}
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


def test_stale_codex_session_start_idle_stays_busy_when_working_timer_resets() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (18s · esc to interrupt)\n",
                "Working (2s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_030.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True
        state = store.read("pgu-ops:0.0")

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}
    assert state is not None
    assert state.state == "idle"
    assert state.source == "codex.SessionStart"


def test_codex_session_start_idle_without_timer_delivers() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-ops:0.0", ""),
            idle_working_timer_sample_delay_seconds=1.1,
            wall_time=lambda: 1_800_000_030.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is False

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_codex_probe_ignores_pgu626_timer_prose_above_idle_prompt() -> None:
    pgu626_text_before = """\
PGU-626 -- Working-timer probe parses pane PROSE as a timer.
The idle pane was displaying analysis of PGU-618 itself:
    "...WORKING_TIMER_RE matches Working (23s cleanly."
    "Working (3s) -> Working (4s) across 1.2s."
>
"""
    sleep_calls: list[float] = []

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-ops:0.0", pgu626_text_before, pgu626_text_before),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is False

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_codex_probe_detects_working_timer_in_real_footer_region() -> None:
    codex_layout_before = """\
Some ordinary output above the footer.
────────────────────────────────────────────────────────────────────────────────
• Working (1s • esc to interrupt)
› Ask Codex to do anything
  gpt-5.5 high · ~/Projects/pgu
"""
    codex_layout_after = codex_layout_before.replace("Working (1s", "Working (2s")
    sleep_calls: list[float] = []

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-ops:0.0", codex_layout_before, codex_layout_after),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_claude_session_start_idle_with_working_render_stays_busy() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-audit:0.0",
                "✶ Harmonizing… (2m 22s · ↓ 8.6k tokens)\n",
                "✻ Harmonizing… (2m 23s · ↓ 8.6k tokens)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-audit:0.0", "idle", source="claude.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-audit:0.0") is True

    assert gate.last_trace("pgu-audit:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_static_claude_interrupt_hint_is_not_working_evidence() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-director:0.0",
                "Press esc to interrupt\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-director:0.0", "idle", source="claude.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-director:0.0") is False

    assert gate.last_trace("pgu-director:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_claude_probe_ignores_pgu626_interrupt_prose_above_idle_prompt() -> None:
    pgu626_text = """\
PGU-626 -- Working-timer probe parses pane PROSE as a timer.
Fix options include matching only when the pattern co-occurs with the interrupt hint:
    Press esc to interrupt
That sentence is ticket prose, not the CLI status line.
>
"""

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-director:0.0", pgu626_text),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-director:0.0", "idle", source="claude.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-director:0.0") is False

    assert gate.last_trace("pgu-director:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_idle_probe_capture_failure_is_inconclusive_and_withheld() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=failing_capture_runner,
            idle_working_timer_sample_delay_seconds=1.1,
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True

    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer_unobservable"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_unknown_runtime_idle_probe_uses_content_hash_when_capture_succeeds() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-main:0.0", ""),
            idle_working_timer_sample_delay_seconds=1.1,
            role_runtimes={"main": "newcli"},
        )
        store.write("pgu-main:0.0", "idle", source="newcli.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-main:0.0") is False

    assert gate.last_trace("pgu-main:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_codex_session_start_idle_frozen_timer_samples_once_then_delivers() -> None:
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
            idle_working_timer_sample_delay_seconds=1.1,
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_030.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.SessionStart", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is False
        assert gate.is_busy("pgu-ops:0.0") is False

    assert_uneven_probe_sleeps(sleep_calls, 1.1, probes=2)
    assert gate.last_trace("pgu-ops:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_foreign_runtime_idle_with_advancing_working_timer_is_busy() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (22s · esc to interrupt)\n",
                "Working (23s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="gemini.Stop", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "foreign_runtime_pane_content_changed"  # type: ignore[union-attr]


def test_foreign_runtime_idle_without_timer_delivers() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-ops:0.0", ""),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-ops:0.0", "idle", source="gemini.Stop", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is False

    assert gate.last_trace("pgu-ops:0.0").reason == "foreign_runtime_working_timer_idle"  # type: ignore[union-attr]


def test_hermes_runtime_turn_end_idle_is_known_runtime_not_foreign_fallback() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-inspector:0.0", "idle hermes prompt\n"),
            role_runtimes={"inspector": "hermes"},
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-inspector:0.0", "idle", source="hermes.post_llm_call", now=1_800_000_000.0)

        assert gate.is_busy("pgu-inspector:0.0") is False

    assert gate.last_trace("pgu-inspector:0.0").reason == "hook_idle"  # type: ignore[union-attr]


def test_hermes_runtime_session_start_idle_still_requires_probe() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-inspector:0.0",
                "Hermes working\n",
                "Hermes working.\n",
            ),
            role_runtimes={"inspector": "hermes"},
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda _seconds: None,
        )
        store.write("pgu-inspector:0.0", "idle", source="hermes.on_session_start", now=1_800_000_000.0)

        assert gate.is_busy("pgu-inspector:0.0") is True

    assert gate.last_trace("pgu-inspector:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]


def test_hermes_busy_hook_blocks_delivery_for_known_runtime() -> None:
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            role_runtimes={"inspector": "hermes"},
        )
        store.write("pgu-inspector:0.0", "busy", source="hermes.pre_llm_call", now=1_800_000_000.0)

        assert gate.is_busy("pgu-inspector:0.0") is True

    assert gate.last_trace("pgu-inspector:0.0").reason == "hook_busy"  # type: ignore[union-attr]


def test_team_launcher_start_idle_without_timer_delivers() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner("pgu-main:0.0", ""),
            idle_working_timer_sample_delay_seconds=1.1,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-main:0.0", "idle", source="team_launcher.start", now=1_800_000_000.0)

        assert gate.is_busy("pgu-main:0.0") is False

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-main:0.0").reason == "working_timer_idle"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


def test_stale_turn_end_idle_uses_content_hash_probe() -> None:
    sleep_calls: list[float] = []
    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=constant_cursor_runner("2 23 24"),
            capture_pane_runner=targeted_capture_runner(
                "pgu-ops:0.0",
                "Working (18s · esc to interrupt)\n",
                "Working (2s · esc to interrupt)\n",
            ),
            idle_working_timer_sample_delay_seconds=1.1,
            stale_codex_busy_hook_seconds=120,
            wall_time=lambda: 1_800_000_236.0,
            sleeper=lambda seconds: sleep_calls.append(seconds),
        )
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=1_800_000_000.0)

        assert gate.is_busy("pgu-ops:0.0") is True

    assert_uneven_probe_sleeps(sleep_calls, 1.1)
    assert gate.last_trace("pgu-ops:0.0").reason == "pane_content_changed"  # type: ignore[union-attr]
    assert gate._last_working_timer_by_target == {}


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
        assert held_conn.requeued == [(83, (83, f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds", "pane busy"))]
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
    assert conn.requeued == [(88, (88, f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds", "pane busy"))]
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
    assert conn.requeued[0][1][1] == f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds"
    assert conn.requeued[0][1][2] == FINISH_CURRENT_REQUEUE_ERROR
    assert trace_events(conn) == ["listener_claim", "finish_current_defer"]


def test_finish_current_requeues_with_exponential_backoff() -> None:
    sent: list[tuple[str, str]] = []
    delays: list[str] = []
    with TemporaryStateDir() as tmp_path:
        store, gate = hook_gate(tmp_path)
        store.write("pgu-ops:0.0", "idle", source="codex.Stop", now=100.0)
        for attempts in (1, 2, 4, 9):
            conn = FakeConnection(
                [queue_row(43, "PGU-324", state="in_progress", assignee="ops", attempts=attempts)],
                finish_current_blockers={"ops": "PGU-321"},
            )
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
    assert delays == [
        f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds",
        f"{DEFAULT_REQUEUE_BASE_SECONDS * 2:g} seconds",
        f"{DEFAULT_REQUEUE_BASE_SECONDS * 8:g} seconds",
        f"{DEFAULT_REQUEUE_MAX_SECONDS:g} seconds",
    ]


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
    assert conn.requeued[0][1][2] == FINISH_CURRENT_REQUEUE_ERROR
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
            capture_pane_runner=targeted_capture_runner("pgu-director:0.0", "Press esc to interrupt\n"),
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
    detail = json.loads(conn.traces[1][8])
    assert detail["decision_reason"] == "human_composing"
    assert detail["anti_clobber"]["reason"] == "human_composing"


def test_director_pre_send_recheck_blocks_claude_working_probe_after_initial_idle_check() -> None:
    sent: list[tuple[str, str]] = []
    cursor_runner = sequenced_cursor_runner("2 23 24", "2 23 24")
    capture_runner = targeted_alternating_capture_runner(
        "pgu-director:0.0",
        "",
    )

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=cursor_runner,
            capture_pane_runner=capture_runner,
            idle_working_timer_sample_delay_seconds=0.0,
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source="claude.SessionStart", now=10.0)

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

        assert listener.listen_once(max_notifications=1) == 0

    assert sent == []
    assert len(conn.requeued) == 1
    assert conn.traces[1][6] == "pane_content_changed"
    detail = json.loads(conn.traces[1][8])
    assert detail["decision_reason"] == "pane_content_changed"
    assert detail["anti_clobber"]["reason"] == "pane_content_changed"


def director_delivery_case(
    source: str,
    *pane_texts: str,
    capture_pane_runner: Any | None = None,
) -> tuple[list[tuple[str, str]], FakeConnection]:
    sent: list[tuple[str, str]] = []
    cursor_runner = sequenced_cursor_runner("2 23 24", "2 23 24", "2 23 24")

    with TemporaryStateDir() as tmp_path:
        store = PaneHookStateStore(tmp_path)
        gate = PaneActivityGate(
            state_store=store,
            cursor_position_runner=cursor_runner,
            capture_pane_runner=capture_pane_runner
            or targeted_capture_runner("pgu-director:0.0", *pane_texts),
            idle_working_timer_sample_delay_seconds=0.0,
            wall_time=lambda: 0.0,
        )
        store.write("pgu-director:0.0", "idle", source=source, now=10.0)

        conn = FakeConnection(
            [queue_row(77, "PGU-360", target_role="director", message="PGU-360 -- Director notification")]
        )
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )

        listener.listen_once(max_notifications=1)
        return sent, conn


def test_director_delivery_matrix_for_trusted_and_untrusted_claude_idle_sources() -> None:
    trusted_idle_sent, trusted_idle_conn = director_delivery_case("claude.Notification.idle_prompt", "")
    trusted_static_interrupt_sent, trusted_static_interrupt_conn = director_delivery_case(
        "claude.Notification.idle_prompt",
        "Press esc to interrupt\n",
    )
    untrusted_idle_sent, untrusted_idle_conn = director_delivery_case("claude.SessionStart", "")
    untrusted_changed_sent, untrusted_changed_conn = director_delivery_case(
        "claude.SessionStart",
        capture_pane_runner=targeted_alternating_capture_runner("pgu-director:0.0", ""),
    )

    assert trusted_idle_sent == [("pgu-director:0.0", "PGU-360 -- Director notification")]
    assert trace_events(trusted_idle_conn) == ["listener_claim", "send", "listener_ack"]
    assert trusted_idle_conn.traces[1][6] == "hook_idle"

    assert trusted_static_interrupt_sent == [("pgu-director:0.0", "PGU-360 -- Director notification")]
    assert trace_events(trusted_static_interrupt_conn) == ["listener_claim", "send", "listener_ack"]
    assert trusted_static_interrupt_conn.traces[1][6] == "hook_idle"

    assert untrusted_idle_sent == [("pgu-director:0.0", "PGU-360 -- Director notification")]
    assert trace_events(untrusted_idle_conn) == ["listener_claim", "send", "listener_ack"]
    assert untrusted_idle_conn.traces[1][6] == "working_timer_idle"

    assert untrusted_changed_sent == []
    assert len(untrusted_changed_conn.requeued) == 1
    assert untrusted_changed_conn.traces[1][6] == "pane_content_changed"


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


def test_send_failure_marks_missing_tmux_target_as_structural_fault() -> None:
    conn = FakeConnection([queue_row(17, "PGU-637", attempts=2, assignee="research", target_role="research")])
    error = subprocess.CalledProcessError(
        1,
        ["/home/agent/bin/directorctl", "send", "pgu-research:0.0", "message"],
        stderr="can't find pane: pgu-research:0.0\n",
    )

    def sender(_target: str, _message: str) -> None:
        raise error

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=sender,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        target_exists=lambda _target: None,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert delivery_failure_reason(error, "pgu-research:0.0") == "tmux_target_missing"
    assert trace_events(conn) == ["listener_claim", "send_failed"]
    assert conn.traces[1][6] == "tmux_target_missing"
    detail = json.loads(conn.traces[1][8])
    assert detail["decision_reason"] == "tmux_target_missing"
    assert conn.requeued == []
    assert len(conn.dead_lettered) == 1
    notification_id, params = conn.dead_lettered[0]
    assert notification_id == 17
    assert params is not None
    assert params[1] == "tmux_target_missing"
    detail = json.loads(params[2])
    assert detail["attempts"] == 2
    assert detail["target"] == "pgu-research:0.0"
    assert detail["message"] == "New ticket for you: PGU-637 -- Queue"


def test_tmux_target_exists_uses_loud_probe_on_isolated_tmux_socket() -> None:
    if shutil.which("tmux") is None:
        return
    session = f"pgu776-probe-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="ticket-board-tmux-probe.") as tmpdir:
        socket_name = Path(tmpdir).name

        def tmux_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            assert args[0] == "tmux", args
            return subprocess.run(["tmux", "-L", socket_name] + args[1:], **kwargs)

        subprocess.run(
            ["tmux", "-L", socket_name, "new-session", "-d", "-s", session, "-n", "main", "sleep 60"],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        try:
            assert notify_listener.tmux_target_exists(f"{session}:0.0", runner=tmux_run) is True
            assert notify_listener.tmux_target_exists(f"{session}-missing:0.0", runner=tmux_run) is False
            assert notify_listener.tmux_target_exists(f"{session}:9.0", runner=tmux_run) is False
            assert notify_listener.tmux_target_exists("%999", runner=tmux_run) is False
        finally:
            subprocess.run(
                ["tmux", "-L", socket_name, "kill-session", "-t", session],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )


def test_tmux_target_exists_treats_inconclusive_probe_as_unknown() -> None:
    def timeout_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    def os_error_runner(_args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("tmux unavailable")

    assert notify_listener.tmux_target_exists("pgu-ops:0.0", runner=timeout_runner) is None
    assert notify_listener.tmux_target_exists("pgu-ops:0.0", runner=os_error_runner) is None


def test_missing_tmux_target_dead_letters_before_busy_gate() -> None:
    gate_calls: list[str] = []
    send_calls: list[tuple[str, str]] = []
    conn = FakeConnection([queue_row(49, "PGU-772", attempts=49, assignee="perf", target_role="perf")])
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: send_calls.append((target, message)),
        activity_gate=lambda target: gate_calls.append(target) or True,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        target_exists=lambda target: False if target == "pgu-perf:0.0" else True,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert send_calls == []
    assert gate_calls == []
    assert conn.requeued == []
    assert conn.acked == []
    assert len(conn.dead_lettered) == 1
    notification_id, params = conn.dead_lettered[0]
    assert notification_id == 49
    assert params is not None
    assert params[1] == "tmux_target_missing"
    detail = json.loads(params[2])
    assert detail["target"] == "pgu-perf:0.0"
    assert detail["attempts"] == 49


def test_busy_existing_pane_holds_then_delivers_when_idle() -> None:
    sent: list[tuple[str, str]] = []
    gate_busy = [True]
    row = queue_row(50, "PGU-776", attempts=1, assignee="ops", target_role="ops")
    conn = FakeConnection([row])
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: gate_busy[0],
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        target_exists=lambda _target: True,
    )

    assert listener.listen_once(max_notifications=1) == 0
    assert sent == []
    assert conn.requeued == [(50, (50, f"{DEFAULT_REQUEUE_BASE_SECONDS:g} seconds", "pane busy"))]
    assert conn.dead_lettered == []

    gate_busy[0] = False
    conn.queue_rows.append(queue_row(50, "PGU-776", attempts=2, assignee="ops", target_role="ops"))
    assert listener.listen_once(max_notifications=1) == 1
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-776 -- Queue")]
    assert conn.acked == [50]
    assert conn.dead_lettered == []


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


def test_user_review_does_not_deliver_to_a_pane_for_uat() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: FakeConnection([]),
        poll_seconds=0,
    )

    assert listener.deliver_payload(transition_payload("PGU-213", title="UAT gate", new_state="user_review")) is False
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


def test_live_pane_shellout_guard_rejects_capture_pane_without_calling_tmux() -> None:
    assert_capture_pane_guard_blocks_without_shelling_out(
        "test_live_pane_shellout_guard_rejects_capture_pane_without_calling_tmux"
    )


def test_live_pane_shellout_guard_rejects_real_gate_default_capture_runner() -> None:
    test_name = "test_live_pane_shellout_guard_rejects_real_gate_default_capture_runner"
    guard = LivePaneShelloutGuard(patch_bound_run_defaults=(RealPaneActivityGate.__init__,))
    guard.current_test = test_name
    try:
        with guard:
            RealPaneActivityGate().composer_snapshot("pgu-ops:0.0")
    except AssertionError as exc:
        message = str(exc)
    else:
        raise AssertionError("real gate default capture runner reached no live-pane guard")

    assert test_name in message
    assert guard.escapes == [["tmux", "capture-pane", "-p", "-J", "-t", "pgu-ops:0.0"]], guard.escapes


class TemporaryStateDir:
    def __enter__(self) -> Path:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="pgu-pane-state.")
        return Path(self._tmp.name)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._tmp.cleanup()


def test_pane_state_live_guard_ignores_content_churn_but_detects_file_set_changes() -> None:
    with TemporaryStateDir() as tmp_path:
        ops_path = tmp_path / "pgu-ops_0.0.json"
        audit_path = tmp_path / "pgu-audit_0.0.json"
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")

        content_before_modify = snapshot_paths([tmp_path])
        before_modify = snapshot_paths_file_set([tmp_path])
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "busy"}) + "\n", encoding="utf-8")
        assert snapshot_diff(content_before_modify, snapshot_paths([tmp_path])) == [f"{tmp_path}/pgu-ops_0.0.json"]
        assert snapshot_file_set_diff(before_modify, snapshot_paths_file_set([tmp_path])) == []

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_delete = snapshot_paths_file_set([tmp_path])
        ops_path.unlink()
        assert snapshot_file_set_diff(before_delete, snapshot_paths_file_set([tmp_path])) == [f"{tmp_path}/pgu-ops_0.0.json"]

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_add = snapshot_paths_file_set([tmp_path])
        audit_path.write_text(json.dumps({"target": "pgu-audit:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        assert snapshot_file_set_diff(before_add, snapshot_paths_file_set([tmp_path])) == [f"{tmp_path}/pgu-audit_0.0.json"]


def test_pane_state_live_guard_detects_foreign_and_source_mismatched_records() -> None:
    with TemporaryStateDir() as tmp_path:
        ops_path = tmp_path / "pgu-ops_0.0.json"
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle", "source": "codex.Stop"}) + "\n", encoding="utf-8")
        before = pane_state_record_anomalies([tmp_path])

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle", "source": "gemini.Stop"}) + "\n", encoding="utf-8")
        changed = pane_state_record_anomaly_diff(before, pane_state_record_anomalies([tmp_path]))
        assert changed == [f"{ops_path}: source 'gemini.Stop' is not valid for 'pgu-ops:0.0'"]

        foreign_path = tmp_path / "pgu-probe_0.0.json"
        foreign_path.write_text(json.dumps({"target": "pgu-probe:0.0", "state": "idle", "source": "codex.Stop"}) + "\n", encoding="utf-8")
        changed = pane_state_record_anomaly_diff(before, pane_state_record_anomalies([tmp_path]))
        assert f"{foreign_path}: target 'pgu-probe:0.0' is not a configured live pgu pane" in changed


def test_expected_runtime_strips_known_hyphenated_project_slug() -> None:
    previous_project = notify_listener.DEFAULT_PROJECT
    try:
        notify_listener.DEFAULT_PROJECT = "my-cool-thing"
        gate = RealPaneActivityGate(role_runtimes={"code-review": "codex", "review": "gemini"})
        assert gate._expected_runtime_for_target("my-cool-thing-code-review:0.0") == "codex"
    finally:
        notify_listener.DEFAULT_PROJECT = previous_project


def test_notify_listener_no_env_target_fallback_splits_first_dash_for_hyphenated_role() -> None:
    previous_project = notify_listener.DEFAULT_PROJECT
    try:
        notify_listener.DEFAULT_PROJECT = "mycoolthing"
        gate = RealPaneActivityGate(role_runtimes={"t-ops": "codex"})
        assert gate._role_for_target("other-project-ops:0.0") == "project-ops"
    finally:
        notify_listener.DEFAULT_PROJECT = previous_project


def main() -> int:
    live_snapshot = snapshot_paths_file_set(LIVE_PANE_STATE_PATHS)
    live_anomalies = pane_state_record_anomalies(LIVE_PANE_STATE_PATHS)
    try:
        run_module_tests_with_live_pane_guard(
            globals(),
            patch_bound_run_defaults=(RealPaneActivityGate.__init__,),
        )
    finally:
        changed = [
            *snapshot_file_set_diff(live_snapshot, snapshot_paths_file_set(LIVE_PANE_STATE_PATHS)),
            *pane_state_record_anomaly_diff(live_anomalies, pane_state_record_anomalies(LIVE_PANE_STATE_PATHS)),
        ]
        assert not changed, changed
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
