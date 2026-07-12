"""LISTEN shim for PostgreSQL ticket-board transition notifications."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import sql

CHANNEL = "ticket_board_state_transition"
DEFAULT_DATABASE_URL = os.environ.get("TICKET_BOARD_NOTIFY_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
DEFAULT_RECONNECT_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_KEEPALIVES_IDLE_SECONDS = 30
DEFAULT_KEEPALIVES_INTERVAL_SECONDS = 10
DEFAULT_KEEPALIVES_COUNT = 3
DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEUE_BASE_SECONDS = 5.0
DEFAULT_REQUEUE_MAX_SECONDS = 60.0
DEFAULT_BUSY_REQUEUE_SECONDS = 1.0
DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_IDLE_STALL_GRACE_SECONDS = 45.0
DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS = 5 * 60.0
DEFAULT_IDLE_STALL_ESCALATE_AFTER = 2
DEFAULT_PANE_STATE_DIR = (
    Path(os.environ["PGU_TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)
ROLE_TO_TARGET = {
    "director": "pgu-director:0.0",
    "main": "pgu-main:0.0",
    "app": "pgu-app:0.0",
    "perf": "pgu-perf:0.0",
    "research": "pgu-research:0.0",
    "ops": "pgu-ops:0.0",
    "audit": "pgu-audit:0.0",
    "inspector": "pgu-inspector:0.0",
}
STATE_RANK = {
    "backlog": 0,
    "analysis": 1,
    "in_progress": 3,
    "inspection": 4,
    "audit": 5,
    "eric_review": 6,
    "director_review": 7,
    "done": 8,
    "cancelled": 9,
}
TERMINAL_STATES = {"done", "cancelled"}
NUDGE_ELIGIBLE_STATES = {"in_progress", "inspection", "audit", "director_review", "analysis", "backlog"}

LOGGER = logging.getLogger(__name__)
DEFAULT_DIRECTORCTL = str(Path(__file__).resolve().parents[1] / "directorctl")


@dataclass(frozen=True)
class Transition:
    ticket_id: str
    title: str
    old_state: str
    new_state: str
    assignee: str
    message: str = ""
    target_role: str = ""
    kind: str = "transition"


@dataclass(frozen=True)
class ActivityTrace:
    busy: bool
    reason: str
    region_digest: str = ""


@dataclass(frozen=True)
class PaneHookState:
    target: str
    state: str
    updated_at: float
    source: str = ""


def parse_transition_payload(payload: str) -> Transition:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("notification payload must be a JSON object")
    return Transition(
        ticket_id=str(parsed.get("id", "")).strip().upper(),
        title=str(parsed.get("title", "")).strip(),
        old_state=str(parsed.get("old_state", "")).strip(),
        new_state=str(parsed.get("new_state", "")).strip(),
        assignee=str(parsed.get("assignee", "")).strip().lower(),
        message=str(parsed.get("message", "")).strip(),
        target_role=str(parsed.get("target_role", "")).strip().lower(),
        kind=str(parsed.get("kind", "transition")).strip().lower() or "transition",
    )


def target_for_transition(transition: Transition) -> str | None:
    if transition.target_role:
        return ROLE_TO_TARGET.get(transition.target_role)
    if transition.new_state == "analysis":
        return ROLE_TO_TARGET["director"]
    if transition.new_state == "in_progress":
        return ROLE_TO_TARGET.get(transition.assignee)
    if transition.new_state == "inspection":
        return ROLE_TO_TARGET["inspector"]
    if transition.new_state == "audit":
        return ROLE_TO_TARGET["audit"]
    if transition.new_state == "eric_review":
        return None
    if transition.new_state == "director_review":
        return ROLE_TO_TARGET["director"]
    return None


def message_for_transition(transition: Transition) -> str | None:
    if target_for_transition(transition) is None:
        return None
    if not transition.ticket_id:
        raise ValueError("notification payload missing ticket id")
    if transition.message:
        return transition.message
    title_suffix = f" -- {transition.title}" if transition.title else ""
    old_rank = STATE_RANK.get(transition.old_state)
    new_rank = STATE_RANK.get(transition.new_state)
    if old_rank is not None and new_rank is not None and new_rank < old_rank:
        return f"{transition.ticket_id}{title_suffix} kicked back to you"
    if transition.new_state == "analysis":
        return f"New ticket for you: {transition.ticket_id}{title_suffix}"
    if transition.new_state == "in_progress":
        return f"New ticket for you: {transition.ticket_id}{title_suffix}"
    if transition.new_state == "inspection":
        return f"{transition.ticket_id}{title_suffix} ready for inspection"
    if transition.new_state == "audit":
        return f"{transition.ticket_id}{title_suffix} ready for audit"
    if transition.new_state == "director_review":
        return f"{transition.ticket_id}{title_suffix} ready for your review"
    return None


class DirectorctlSender:
    def __init__(
        self,
        directorctl_bin: str = DEFAULT_DIRECTORCTL,
        *,
        timeout_seconds: float = DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS,
        director_typing_max_attempts: int = 0,
    ) -> None:
        self.directorctl_bin = directorctl_bin
        self.timeout_seconds = timeout_seconds
        self.director_typing_max_attempts = director_typing_max_attempts

    def __call__(self, target: str, message: str) -> None:
        env = os.environ.copy()
        env["PGU_DIRECTORCTL_DIRECTOR_TYPING_MAX_ATTEMPTS"] = str(self.director_typing_max_attempts)
        subprocess.run(
            [self.directorctl_bin, "send", target, message],
            check=True,
            timeout=self.timeout_seconds,
            env=env,
        )


class PaneHookStateStore:
    def __init__(self, state_dir: str | Path = DEFAULT_PANE_STATE_DIR) -> None:
        self.state_dir = Path(state_dir).expanduser()

    def _target_path(self, target: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
        return self.state_dir / f"{safe}.json"

    def read(self, target: str) -> PaneHookState | None:
        path = self._target_path(target)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        state = str(parsed.get("state") or "").strip().lower()
        if state not in {"idle", "busy", "blocked"}:
            return None
        try:
            updated_at = float(parsed.get("updated_at"))
        except (TypeError, ValueError):
            return None
        return PaneHookState(
            target=str(parsed.get("target") or target),
            state=state,
            updated_at=updated_at,
            source=str(parsed.get("source") or ""),
        )

    def write(self, target: str, state: str, *, source: str = "", now: float | None = None) -> Path:
        normalized_state = state.strip().lower()
        if normalized_state not in {"idle", "busy", "blocked"}:
            raise ValueError("pane hook state must be idle, busy, or blocked")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "target": target,
            "state": normalized_state,
            "updated_at": time.time() if now is None else now,
            "source": source,
        }
        path = self._target_path(target)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


class PaneActivityGate:
    def __init__(
        self,
        *,
        state_store: PaneHookStateStore | None = None,
        director_target: str = ROLE_TO_TARGET["director"],
        client_activity_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        director_composing_timeout_seconds: float = DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_store = state_store or PaneHookStateStore()
        self.director_target = director_target
        self.client_activity_runner = client_activity_runner
        self.director_composing_timeout_seconds = director_composing_timeout_seconds
        self.monotonic = monotonic
        self._last_trace_by_target: dict[str, ActivityTrace] = {}
        self._last_hook_state_by_target: dict[str, tuple[str, float]] = {}
        self._director_idle_snapshot_activity: int | None = None
        self._director_idle_snapshot_state_ts: float | None = None
        self._director_composing_latched = False
        self._director_composing_activity: int | None = None
        self._director_composing_started_at: float | None = None

    def _record_trace(self, target: str, trace: ActivityTrace) -> bool:
        self._last_trace_by_target[target] = trace
        return trace.busy

    def last_trace(self, target: str) -> ActivityTrace | None:
        return self._last_trace_by_target.get(target)

    def missing_hook_targets(self, targets: list[str] | None = None) -> list[str]:
        checked_targets = targets or sorted(set(ROLE_TO_TARGET.values()))
        return [target for target in checked_targets if self.state_store.read(target) is None]

    def idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        checked_roles = roles or sorted(ROLE_TO_TARGET)
        idle_since: dict[str, str] = {}
        for role in checked_roles:
            target = ROLE_TO_TARGET.get(role)
            if target is None:
                continue
            if self.is_busy(target):
                continue
            state = self.state_store.read(target)
            if state is None or state.state != "idle":
                continue
            idle_since[role] = datetime.fromtimestamp(state.updated_at, timezone.utc).isoformat()
        return idle_since

    def _tmux_session_for_target(self, target: str) -> str:
        return target.split(":", 1)[0]

    def _director_client_activity(self, target: str) -> int | None:
        session = self._tmux_session_for_target(target)
        try:
            proc = self.client_activity_runner(
                ["tmux", "list-clients", "-t", session, "-F", "#{client_activity}"],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        values: list[int] = []
        for line in proc.stdout.splitlines():
            try:
                values.append(int(line.strip()))
            except ValueError:
                continue
        return max(values) if values else None

    def _reset_director_latch(self) -> None:
        self._director_idle_snapshot_activity = None
        self._director_idle_snapshot_state_ts = None
        self._director_composing_latched = False
        self._director_composing_activity = None
        self._director_composing_started_at = None

    def _director_is_composing(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        current_activity = self._director_client_activity(target)
        if current_activity is None:
            return ActivityTrace(True, "client_activity_unavailable")
        if self._director_idle_snapshot_state_ts != state.updated_at:
            self._director_idle_snapshot_state_ts = state.updated_at
            self._director_idle_snapshot_activity = current_activity
            self._director_composing_latched = False
            self._director_composing_activity = None
            self._director_composing_started_at = None
            return None
        snapshot = self._director_idle_snapshot_activity
        if snapshot is None:
            self._director_idle_snapshot_activity = current_activity
            return None
        now = self.monotonic()
        if not self._director_composing_latched and current_activity > snapshot:
            self._director_composing_latched = True
            self._director_composing_activity = current_activity
            self._director_composing_started_at = now
        elif (
            self._director_composing_latched
            and self._director_composing_activity is not None
            and current_activity > self._director_composing_activity
        ):
            self._director_composing_activity = current_activity
            self._director_composing_started_at = now
        if not self._director_composing_latched:
            return None
        latched_activity = self._director_composing_activity
        started_at = self._director_composing_started_at if self._director_composing_started_at is not None else now
        if (
            latched_activity is not None
            and current_activity == latched_activity
            and now - started_at >= self.director_composing_timeout_seconds
        ):
            self._director_idle_snapshot_activity = current_activity
            self._director_composing_latched = False
            self._director_composing_activity = None
            self._director_composing_started_at = None
            return None
        return ActivityTrace(True, "human_composing")

    def is_busy(self, target: str) -> bool:
        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_latch()
            return self._record_trace(target, ActivityTrace(True, "no_hook_state"))
        previous = self._last_hook_state_by_target.get(target)
        current = (state.state, state.updated_at)
        self._last_hook_state_by_target[target] = current
        if state.state != "idle":
            if target == self.director_target:
                self._reset_director_latch()
            reason = "hook_blocked" if state.state == "blocked" else "hook_busy"
            return self._record_trace(target, ActivityTrace(True, reason))
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_latch()
        if target == self.director_target:
            composing_trace = self._director_is_composing(target, state)
            if composing_trace is not None:
                return self._record_trace(target, composing_trace)
        return self._record_trace(target, ActivityTrace(False, "hook_idle"))

    def is_working(self, target: str) -> bool:
        return self.is_busy(target)


class TicketBoardNotifyListener:
    def __init__(
        self,
        *,
        conninfo: str,
        channel: str = CHANNEL,
        sender: Callable[[str, str], None] | None = None,
        activity_gate: Callable[[str], bool] | None = None,
        connector: Callable[..., Any] = psycopg.connect,
        reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        requeue_base_seconds: float = DEFAULT_REQUEUE_BASE_SECONDS,
        requeue_max_seconds: float = DEFAULT_REQUEUE_MAX_SECONDS,
        busy_requeue_seconds: float = DEFAULT_BUSY_REQUEUE_SECONDS,
        idle_stall_grace_seconds: float = DEFAULT_IDLE_STALL_GRACE_SECONDS,
        idle_stall_nudge_cadence_seconds: float = DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS,
        idle_stall_escalate_after: int = DEFAULT_IDLE_STALL_ESCALATE_AFTER,
        stop_event: threading.Event | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.conninfo = conninfo
        self.channel = channel
        self.sender = sender or DirectorctlSender()
        self.activity_gate = activity_gate or PaneActivityGate().is_working
        self.connector = connector
        self.reconnect_seconds = reconnect_seconds
        self.poll_seconds = poll_seconds
        self.requeue_base_seconds = requeue_base_seconds
        self.requeue_max_seconds = requeue_max_seconds
        self.busy_requeue_seconds = busy_requeue_seconds
        self.idle_stall_grace_seconds = idle_stall_grace_seconds
        self.idle_stall_nudge_cadence_seconds = idle_stall_nudge_cadence_seconds
        self.idle_stall_escalate_after = idle_stall_escalate_after
        self.stop_event = stop_event or threading.Event()
        self.logger = logger
        self.delivered_count = 0

    def _connector_kwargs(self) -> dict[str, int | bool]:
        return {
            "autocommit": True,
            "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": DEFAULT_KEEPALIVES_IDLE_SECONDS,
            "keepalives_interval": DEFAULT_KEEPALIVES_INTERVAL_SECONDS,
            "keepalives_count": DEFAULT_KEEPALIVES_COUNT,
        }

    def _backoff_seconds(self, attempts: int) -> float:
        exponent = max(attempts - 1, 0)
        return min(self.requeue_max_seconds, self.requeue_base_seconds * (2**exponent))

    def deliver_payload(self, payload: str) -> bool:
        transition = parse_transition_payload(payload)
        target = target_for_transition(transition)
        message = message_for_transition(transition)
        if not target or not message:
            self.logger.debug("Skipping transition notification: %s", payload)
            return False
        if self.activity_gate(target):
            self.logger.info("Deferred notification for active pane %s", target)
            return False
        self.sender(target, message)
        self.delivered_count += 1
        self.logger.info("Delivered %s transition to %s", transition.ticket_id, target)
        return True

    def reconcile_pending_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
        return self.process_due_notifications(conn, max_notifications=max_notifications)

    def _claim_notification(self, conn: Any) -> tuple[int, str, str, str, str, int] | None:
        result = conn.execute(
            """
SELECT notification_id, ticket_id, target_role, message, payload::text, attempts
FROM ticket_board.claim_notification()
"""
        )
        row = result.fetchone()
        if row is None:
            return None
        notification_id, ticket_id, target_role, message, payload, attempts = row
        return (
            int(notification_id),
            self._decode_text(ticket_id),
            self._decode_text(target_role),
            self._decode_text(message),
            self._decode_text(payload),
            int(attempts),
        )

    def _decode_text(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def _payload_kind(self, payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get("kind") or "").strip().lower()

    def _safe_json_payload(self, payload: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    def _activity_trace(self, target: str, pane_busy: bool) -> ActivityTrace:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        last_trace = getattr(gate_owner, "last_trace", None)
        if callable(last_trace):
            trace = last_trace(target)
            if isinstance(trace, ActivityTrace):
                return trace
        return ActivityTrace(pane_busy, "busy" if pane_busy else "idle")

    def _trace_notification(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        event: str,
        pane_busy: bool | None = None,
        busy_reason: str | None = None,
        region_digest: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        pane_state = None if pane_busy is None else ("busy" if pane_busy else "idle")
        try:
            conn.execute(
                """
SELECT ticket_board.record_notification_trace(
    %s::text,
    %s::bigint,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::jsonb
)
""",
                (
                    ticket_id,
                    notification_id,
                    target_role,
                    kind,
                    event,
                    pane_state,
                    busy_reason,
                    region_digest,
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )
        except Exception as exc:  # Trace failures must not wedge delivery.
            self.logger.warning("Failed to record notification trace for %s/%s: %s", notification_id, event, exc)

    def _ack_notification(self, conn: Any, notification_id: int) -> None:
        conn.execute("SELECT ticket_board.ack_notification(%s::bigint)", (notification_id,))

    def _requeue_notification(
        self,
        conn: Any,
        notification_id: int,
        attempts: int,
        error: str,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        delay_seconds = self._backoff_seconds(attempts) if delay_seconds is None else delay_seconds
        conn.execute(
            "SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
            (notification_id, f"{delay_seconds:g} seconds", error[:500]),
        )

    def _next_notification_attempt_at(self, conn: Any) -> datetime | None:
        try:
            result = conn.execute("SELECT ticket_board.next_notification_attempt()")
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to read next ticket notification attempt: %s", exc)
            return None
        if row is None:
            return None
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return None

    def _wait_timeout_seconds(self, conn: Any) -> float:
        timeout = max(self.poll_seconds, 0.0)
        next_attempt_at = self._next_notification_attempt_at(conn)
        if next_attempt_at is None:
            return timeout
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
        seconds_until_due = (next_attempt_at - datetime.now(timezone.utc)).total_seconds()
        due_timeout = max(0.0, seconds_until_due)
        return min(timeout, due_timeout) if timeout > 0 else due_timeout

    def _idle_since_by_role(self) -> dict[str, str]:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        idle_since_by_role = getattr(gate_owner, "idle_since_by_role", None)
        if not callable(idle_since_by_role):
            return {}
        try:
            return dict(idle_since_by_role())
        except Exception as exc:
            self.logger.warning("Failed to read pane idle hook state for stall nudges: %s", exc)
            return {}

    def process_idle_stall_nudges(self, conn: Any) -> int:
        idle_since = self._idle_since_by_role()
        if not idle_since:
            return 0
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_stall_nudges(
    %s::jsonb,
    clock_timestamp(),
    %s::interval,
    %s::interval,
    %s::integer
)
""",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.idle_stall_grace_seconds:g} seconds",
                    f"{self.idle_stall_nudge_cadence_seconds:g} seconds",
                    self.idle_stall_escalate_after,
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue idle-stall nudges: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s idle-stall ticket nudges", enqueued)
        return enqueued

    def _current_ticket_state(self, conn: Any, ticket_id: str) -> tuple[str, str, bool, bool] | None:
        result = conn.execute(
            """
SELECT state,
       assignee,
       manually_controlled,
       coalesce((to_jsonb(t)->>'parked')::boolean, false) AS parked
FROM ticket_board.tickets t
WHERE id = %s
""",
            (ticket_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return (
                self._decode_text(row["state"]),
                self._decode_text(row["assignee"]),
                bool(row["manually_controlled"]),
                bool(row["parked"]),
            )
        return self._decode_text(row[0]), self._decode_text(row[1]), bool(row[2]), bool(row[3])

    def _current_target_role(self, kind: str, state: str, assignee: str) -> str | None:
        if kind == "transition":
            if state == "analysis":
                return "director"
            if state == "in_progress":
                return assignee if assignee != "unassigned" else None
            if state == "inspection":
                return "inspector"
            if state == "audit":
                return "audit"
            if state == "director_review":
                return "director"
            return None
        if kind == "escalation":
            return "director"
        if state == "in_progress":
            return assignee if assignee != "unassigned" else None
        if state == "inspection":
            return "inspector"
        if state in {"audit", "director_review", "analysis", "backlog"}:
            return "director" if state in {"analysis", "backlog", "director_review"} else "audit"
        return None

    def _notification_is_current(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> bool:
        current = self._current_ticket_state(conn, ticket_id)
        if current is None:
            return False
        current_state, current_assignee, manually_controlled, parked = current
        if current_state in TERMINAL_STATES:
            return False

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return True
        if not isinstance(parsed, dict):
            return True

        payload_ticket_id = str(parsed.get("id", ticket_id)).strip().upper()
        if payload_ticket_id and payload_ticket_id != ticket_id:
            return False

        expected_state = str(parsed.get("state") or parsed.get("new_state") or "").strip()
        if expected_state and current_state != expected_state:
            return False

        expected_assignee = str(parsed.get("assignee") or "").strip().lower()
        if expected_assignee and current_assignee != expected_assignee:
            return False

        kind = str(parsed.get("kind") or "").strip().lower()
        if kind == "escalation":
            return (
                target_role == "director"
                and current_state in NUDGE_ELIGIBLE_STATES
                and current_assignee != "unassigned"
                and not manually_controlled
                and not parked
            )

        current_target_role = self._current_target_role(kind, current_state, current_assignee)
        return current_target_role is None or current_target_role == target_role

    def process_due_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
        delivered = 0
        while not self.stop_event.is_set():
            if max_notifications is not None and self.delivered_count >= max_notifications:
                break
            row = self._claim_notification(conn)
            if row is None:
                break
            notification_id, ticket_id, target_role, message, payload, attempts = row
            kind = self._payload_kind(payload)
            self._trace_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="listener_claim",
                detail={"attempts": attempts, "payload": self._safe_json_payload(payload)},
            )
            target = ROLE_TO_TARGET.get(target_role)
            if self.stop_event.is_set():
                break
            if target is None:
                self.logger.warning("Acking notification %s with unknown target role %s", notification_id, target_role)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="unknown_target_role",
                    detail={"payload": self._safe_json_payload(payload)},
                )
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "unknown_target_role"},
                )
                self._ack_notification(conn, notification_id)
                continue
            if not self._notification_is_current(conn, ticket_id, target_role, payload):
                self.logger.info("Dropping stale notification %s for %s: %s", notification_id, ticket_id, payload)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="stale_notification",
                    detail={"payload": self._safe_json_payload(payload)},
                )
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "stale_notification"},
                )
                self._ack_notification(conn, notification_id)
                continue
            pane_busy = self.activity_gate(target)
            activity_trace = self._activity_trace(target, pane_busy)
            if pane_busy:
                self.logger.info("Pane %s is active; requeueing notification %s for %s", target, notification_id, ticket_id)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="gate_defer",
                    pane_busy=True,
                    busy_reason=activity_trace.reason,
                    region_digest=activity_trace.region_digest,
                    detail={"target": target, "attempts": attempts},
                )
                self._requeue_notification(
                    conn,
                    notification_id,
                    attempts,
                    "pane busy",
                    delay_seconds=self.busy_requeue_seconds,
                )
                continue
            try:
                self.sender(target, message)
            except (subprocess.SubprocessError, OSError) as exc:
                self.logger.warning("Failed to deliver queued ticket notification through directorctl: %s", exc)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="send_failed",
                    pane_busy=pane_busy,
                    busy_reason=str(exc),
                    region_digest=activity_trace.region_digest,
                    detail={"target": target, "message": message, "attempts": attempts},
                )
                self._requeue_notification(conn, notification_id, attempts, str(exc))
                continue
            self._trace_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="send",
                pane_busy=pane_busy,
                busy_reason=activity_trace.reason,
                region_digest=activity_trace.region_digest,
                detail={"target": target, "message": message, "attempts": attempts},
            )
            self._trace_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="listener_ack",
                pane_busy=pane_busy,
                busy_reason=activity_trace.reason,
                region_digest=activity_trace.region_digest,
                detail={"target": target},
            )
            self._ack_notification(conn, notification_id)
            self.delivered_count += 1
            delivered += 1
            self.logger.info("Delivered queued notification %s for %s to %s: %s", notification_id, ticket_id, target, payload)
        return delivered

    def _wait_for_notification(self, conn: Any) -> bool:
        notifications = conn.notifies(timeout=self._wait_timeout_seconds(conn), stop_after=1)
        return next(iter(notifications), None) is not None

    def _log_missing_hook_state(self) -> None:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        missing_hook_targets = getattr(gate_owner, "missing_hook_targets", None)
        if not callable(missing_hook_targets):
            return
        missing = missing_hook_targets()
        if missing:
            self.logger.warning(
                "Pane hook state missing for %s; notification delivery fails closed until hooks write state",
                ", ".join(missing),
            )
        else:
            self.logger.info("Pane hook state present for all notification targets")

    def listen_once(self, *, max_notifications: int | None = None) -> int:
        delivered_before = self.delivered_count
        with self.connector(self.conninfo, **self._connector_kwargs()) as conn:
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
            self.logger.info("LISTEN %s established", self.channel)
            self._log_missing_hook_state()
            while not self.stop_event.is_set():
                self.process_idle_stall_nudges(conn)
                delivered = self.process_due_notifications(conn, max_notifications=max_notifications)
                if max_notifications is not None and self.delivered_count >= max_notifications:
                    break
                if not delivered and max_notifications is not None and self.poll_seconds <= 0:
                    break
                notified = self._wait_for_notification(conn)
                if not notified and max_notifications is not None and self.poll_seconds <= 0:
                    break
        return self.delivered_count - delivered_before

    def run_forever(
        self,
        *,
        max_connections: int | None = None,
        max_notifications: int | None = None,
    ) -> None:
        attempts = 0
        while not self.stop_event.is_set():
            if max_connections is not None and attempts >= max_connections:
                return
            attempts += 1
            try:
                self.listen_once(max_notifications=max_notifications)
            except (psycopg.Error, OSError) as exc:
                self.logger.warning("ticket notify listener disconnected: %s", exc)
            if not self.stop_event.is_set():
                self.stop_event.wait(self.reconnect_seconds)


def database_url_from_environment() -> str:
    return DEFAULT_DATABASE_URL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver ticket-board pg_notify state transitions to tmux panes.")
    parser.add_argument("--database", default=database_url_from_environment(), help="PostgreSQL connection string. Defaults to TICKET_BOARD_NOTIFY_DATABASE_URL or DATABASE_URL, otherwise libpq environment.")
    parser.add_argument("--channel", default=CHANNEL, help=f"LISTEN channel (default: {CHANNEL})")
    parser.add_argument("--directorctl", default=DEFAULT_DIRECTORCTL, help=f"directorctl path (default: {DEFAULT_DIRECTORCTL})")
    parser.add_argument("--reconnect-seconds", type=float, default=DEFAULT_RECONNECT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--pane-state-dir", default=str(DEFAULT_PANE_STATE_DIR), help=f"per-pane hook state directory (default: {DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--director-composing-timeout-seconds", type=float, default=DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS)
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    listener = TicketBoardNotifyListener(
        conninfo=args.database,
        channel=args.channel,
        sender=DirectorctlSender(args.directorctl),
        activity_gate=PaneActivityGate(
            state_store=PaneHookStateStore(args.pane_state_dir),
            director_composing_timeout_seconds=args.director_composing_timeout_seconds,
        ).is_working,
        reconnect_seconds=args.reconnect_seconds,
        poll_seconds=args.poll_seconds,
    )
    listener.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
