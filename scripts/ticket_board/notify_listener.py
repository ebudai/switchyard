"""LISTEN shim for PostgreSQL ticket-board transition notifications."""

from __future__ import annotations

import argparse
import hashlib
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
DEFAULT_MAX_DEFER_SECONDS = 10.0
DEFAULT_DIRECTOR_RECENT_ACTIVITY_SECONDS = 10.0
DEFAULT_DIRECTOR_STABLE_IDLE_SECONDS = 0.0
AGENT_STATUS_REGION_LINES = 8
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


class PaneActivityGate:
    def __init__(
        self,
        directorctl_bin: str = DEFAULT_DIRECTORCTL,
        *,
        recent_activity_seconds: float = DEFAULT_DIRECTOR_RECENT_ACTIVITY_SECONDS,
        stable_idle_seconds: float = DEFAULT_DIRECTOR_STABLE_IDLE_SECONDS,
        capture_lines: int = 80,
        capture_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directorctl_bin = directorctl_bin
        self.recent_activity_seconds = recent_activity_seconds
        self.stable_idle_seconds = stable_idle_seconds
        self.capture_lines = capture_lines
        self.capture_runner = capture_runner
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._last_region_digest_by_target: dict[str, str] = {}
        self._last_changed_at_by_target: dict[str, float] = {}
        self._last_trace_by_target: dict[str, ActivityTrace] = {}

    def _record_trace(self, target: str, trace: ActivityTrace) -> bool:
        self._last_trace_by_target[target] = trace
        return trace.busy

    def last_trace(self, target: str) -> ActivityTrace | None:
        return self._last_trace_by_target.get(target)

    def _capture(self, target: str) -> str | None:
        try:
            proc = self.capture_runner(
                [self.directorctl_bin, "capture", target, str(self.capture_lines)],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout

    def _activity_region(self, target: str, text: str) -> str:
        if target == ROLE_TO_TARGET["director"]:
            return self._director_composer_region(text)
        return self._agent_status_region(text)

    def _agent_status_region(self, text: str) -> str:
        lines = [line for line in text.splitlines() if not self._is_volatile_composer_suggestion(line)]
        return "\n".join(lines[-AGENT_STATUS_REGION_LINES:])

    def _is_volatile_composer_suggestion(self, line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith(("> ", "› "))

    def _director_composer_region(self, text: str) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        horizontal_indices = [idx for idx, line in enumerate(lines) if line.count("─") >= 40]
        if len(horizontal_indices) < 2:
            return ""
        composer_lines = lines[horizontal_indices[-2] + 1 : horizontal_indices[-1]]
        return "\n".join(self._normalize_composer_line(line) for line in composer_lines).strip()

    def _normalize_composer_line(self, line: str) -> str:
        stripped = line.strip()
        for prompt in ("❯", ">", "$"):
            if stripped == prompt:
                return ""
            if stripped.startswith(f"{prompt} "):
                return stripped[len(prompt) + 1 :].strip()
        return stripped

    def _activity_digest(self, target: str, text: str) -> str:
        region = self._activity_region(target, text)
        return hashlib.sha256(region.encode("utf-8")).hexdigest()

    def _has_working_indicator(self, target: str, region: str) -> bool:
        if target == ROLE_TO_TARGET["director"]:
            return bool(region.strip())
        normalized = region.lower()
        return (
            "esc to interrupt" in normalized
            or "working" in normalized
            or "thinking" in normalized
            or "running" in normalized
        )

    def is_busy(self, target: str) -> bool:
        text = self._capture(target)
        if text is None:
            return self._record_trace(target, ActivityTrace(False, "capture_failed"))
        first_region = self._activity_region(target, text)
        first_digest = self._activity_digest(target, text)
        first_busy = self._has_working_indicator(target, first_region)
        if self.stable_idle_seconds > 0:
            self.sleeper(self.stable_idle_seconds)
            second_text = self._capture(target)
            if second_text is None:
                return self._record_trace(target, ActivityTrace(False, "capture_failed", first_digest))
            second_region = self._activity_region(target, second_text)
            second_digest = self._activity_digest(target, second_text)
            now = self.monotonic()
            second_busy = self._has_working_indicator(target, second_region)
            if second_busy:
                self._last_region_digest_by_target[target] = second_digest
                self._last_changed_at_by_target[target] = now
                return self._record_trace(target, ActivityTrace(True, "working_indicator", second_digest))
            if first_digest != second_digest:
                self._last_region_digest_by_target[target] = second_digest
                self._last_changed_at_by_target[target] = now
                return self._record_trace(target, ActivityTrace(True, "region_changed", second_digest))
            self._last_region_digest_by_target[target] = second_digest
            return self._record_trace(target, ActivityTrace(False, "stable_idle", second_digest))
        now = self.monotonic()
        previous = self._last_region_digest_by_target.get(target)
        self._last_region_digest_by_target[target] = first_digest
        if first_busy:
            self._last_changed_at_by_target[target] = now
            return self._record_trace(target, ActivityTrace(True, "working_indicator", first_digest))
        if previous is None:
            self._last_changed_at_by_target[target] = now
            return self._record_trace(target, ActivityTrace(False, "cold_start_idle", first_digest))
        if previous != first_digest:
            self._last_changed_at_by_target[target] = now
            return self._record_trace(target, ActivityTrace(True, "region_changed", first_digest))
        return self._record_trace(target, ActivityTrace(False, "region_stable", first_digest))

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
        max_defer_seconds: float = DEFAULT_MAX_DEFER_SECONDS,
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
        self.max_defer_seconds = max_defer_seconds
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

    def _requeue_notification(self, conn: Any, notification_id: int, attempts: int, error: str) -> None:
        delay_seconds = self._backoff_seconds(attempts)
        conn.execute(
            "SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
            (notification_id, f"{delay_seconds} seconds", error[:500]),
        )

    def _notification_created_at(self, conn: Any, notification_id: int) -> datetime | None:
        result = conn.execute(
            """
SELECT created_at
FROM ticket_board.ticket_notification_queue
WHERE id = %s
""",
            (notification_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        created_at = row["created_at"] if isinstance(row, dict) else row[0]
        if not isinstance(created_at, datetime):
            return None
        if created_at.tzinfo is None:
            return created_at.replace(tzinfo=timezone.utc)
        return created_at

    def _defer_age_seconds(self, conn: Any, notification_id: int) -> float:
        created_at = self._notification_created_at(conn, notification_id)
        if created_at is None:
            return 0.0
        now = datetime.now(created_at.tzinfo or timezone.utc)
        return max(0.0, (now - created_at).total_seconds())

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
        if kind == "nudge_analysis":
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
            defer_age = self._defer_age_seconds(conn, notification_id)
            pane_busy = self.activity_gate(target)
            activity_trace = self._activity_trace(target, pane_busy)
            if pane_busy and defer_age < self.max_defer_seconds:
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
                    detail={"target": target, "defer_age_seconds": defer_age, "attempts": attempts},
                )
                self._requeue_notification(conn, notification_id, attempts, "pane busy")
                continue
            if pane_busy:
                self.logger.warning(
                    "Pane %s still active after %.1fs; force-delivering notification %s for %s",
                    target,
                    defer_age,
                    notification_id,
                    ticket_id,
                )
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="max_defer_force_deliver",
                    pane_busy=True,
                    busy_reason=activity_trace.reason,
                    region_digest=activity_trace.region_digest,
                    detail={"target": target, "defer_age_seconds": defer_age, "attempts": attempts},
                )
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
        notifications = conn.notifies(timeout=self.poll_seconds, stop_after=1)
        return next(iter(notifications), None) is not None

    def listen_once(self, *, max_notifications: int | None = None) -> int:
        delivered_before = self.delivered_count
        with self.connector(self.conninfo, **self._connector_kwargs()) as conn:
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
            self.logger.info("LISTEN %s established", self.channel)
            while not self.stop_event.is_set():
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
    parser.add_argument("--max-defer-seconds", type=float, default=DEFAULT_MAX_DEFER_SECONDS)
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
        activity_gate=PaneActivityGate(args.directorctl).is_working,
        reconnect_seconds=args.reconnect_seconds,
        poll_seconds=args.poll_seconds,
        max_defer_seconds=args.max_defer_seconds,
    )
    listener.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
