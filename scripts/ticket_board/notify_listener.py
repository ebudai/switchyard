"""LISTEN shim for PostgreSQL ticket-board transition notifications."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
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
DEFAULT_DIRECTOR_RECENT_ACTIVITY_SECONDS = 10.0
DEFAULT_DIRECTOR_STABLE_IDLE_SECONDS = 0.25
ROLE_TO_TARGET = {
    "director": "pgu-director:0.0",
    "main": "pgu-main:0.0",
    "app": "pgu-app:0.0",
    "perf": "pgu-perf:0.0",
    "research": "pgu-research:0.0",
    "ops": "pgu-ops:0.0",
    "audit": "pgu-audit:0.0",
}
STATE_RANK = {
    "backlog": 0,
    "analysis": 1,
    "ready": 2,
    "in_progress": 3,
    "audit": 4,
    "eric_review": 5,
    "director_review": 6,
    "done": 7,
    "cancelled": 8,
}
TERMINAL_STATES = {"done", "cancelled"}
NUDGE_ELIGIBLE_STATES = {"ready", "in_progress", "audit", "director_review", "analysis", "backlog"}

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
    if transition.new_state in {"ready", "in_progress"}:
        return ROLE_TO_TARGET.get(transition.assignee)
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
    if transition.new_state == "ready":
        return f"Ready ticket for you: {transition.ticket_id}{title_suffix}"
    if transition.new_state == "in_progress":
        return f"New ticket for you: {transition.ticket_id}{title_suffix}"
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
        self._last_capture_by_target: dict[str, str] = {}
        self._last_changed_at_by_target: dict[str, float] = {}

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

    def _has_working_indicator(self, text: str) -> bool:
        return "Working" in text or "esc to interrupt" in text

    def _director_composer_has_content(self, text: str) -> bool:
        if not text:
            return False
        lines = text.splitlines()
        horizontal_indices = [idx for idx, line in enumerate(lines) if line.count("─") >= 40]
        if len(horizontal_indices) < 2:
            return True
        composer_content = "\n".join(lines[horizontal_indices[-2] + 1 : horizontal_indices[-1]])
        cleaned = re.sub(r"^[❯\u276f\s>\-*]+", "", composer_content, flags=re.MULTILINE)
        return bool(cleaned.strip())

    def _mark_recent_activity(self, target: str, text: str, now: float) -> bool:
        previous = self._last_capture_by_target.get(target)
        if previous is None:
            self._last_capture_by_target[target] = text
            self._last_changed_at_by_target[target] = now
            return True
        if previous != text:
            self._last_capture_by_target[target] = text
            self._last_changed_at_by_target[target] = now
            return True
        changed_at = self._last_changed_at_by_target.get(target)
        return changed_at is not None and now - changed_at < self.recent_activity_seconds

    def is_busy(self, target: str) -> bool:
        text = self._capture(target)
        if text is None:
            return False
        if target != ROLE_TO_TARGET["director"]:
            return self._has_working_indicator(text)
        if self._has_working_indicator(text) or self._director_composer_has_content(text):
            self._mark_recent_activity(target, text, self.monotonic())
            return True
        now = self.monotonic()
        # The director pane is shared by a human and an agent. Status text can
        # flicker between tool calls, so require a recently unchanged pane
        # before injecting a notification into the composer.
        if self._mark_recent_activity(target, text, now):
            return True
        if self.stable_idle_seconds > 0:
            self.sleeper(self.stable_idle_seconds)
            second_text = self._capture(target)
            if second_text is None:
                return False
            if self._has_working_indicator(second_text) or self._director_composer_has_content(second_text):
                self._mark_recent_activity(target, second_text, self.monotonic())
                return True
            if second_text != text:
                self._mark_recent_activity(target, second_text, self.monotonic())
                return True
        return False

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

    def _ack_notification(self, conn: Any, notification_id: int) -> None:
        conn.execute("SELECT ticket_board.ack_notification(%s::bigint)", (notification_id,))

    def _requeue_notification(self, conn: Any, notification_id: int, attempts: int, error: str) -> None:
        delay_seconds = self._backoff_seconds(attempts)
        conn.execute(
            "SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
            (notification_id, f"{delay_seconds} seconds", error[:500]),
        )

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
            if state in {"ready", "in_progress"}:
                return assignee if assignee != "unassigned" else None
            if state == "audit":
                return "audit"
            if state == "director_review":
                return "director"
            return None
        if kind == "escalation":
            return "director"
        if state in {"ready", "in_progress"}:
            return assignee if assignee != "unassigned" else None
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
            target = ROLE_TO_TARGET.get(target_role)
            if self.stop_event.is_set():
                break
            if target is None:
                self.logger.warning("Acking notification %s with unknown target role %s", notification_id, target_role)
                self._ack_notification(conn, notification_id)
                continue
            if not self._notification_is_current(conn, ticket_id, target_role, payload):
                self.logger.info("Dropping stale notification %s for %s: %s", notification_id, ticket_id, payload)
                self._ack_notification(conn, notification_id)
                continue
            if self.activity_gate(target):
                self.logger.info("Pane %s is active; requeueing notification %s for %s", target, notification_id, ticket_id)
                self._requeue_notification(conn, notification_id, attempts, "pane busy")
                continue
            try:
                self.sender(target, message)
            except (subprocess.SubprocessError, OSError) as exc:
                self.logger.warning("Failed to deliver queued ticket notification through directorctl: %s", exc)
                self._requeue_notification(conn, notification_id, attempts, str(exc))
                continue
            self._ack_notification(conn, notification_id)
            self.delivered_count += 1
            delivered += 1
            self.logger.info("Delivered queued notification %s for %s to %s: %s", notification_id, ticket_id, target, payload)
        return delivered

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
                notifications = conn.notifies(timeout=self.poll_seconds)
                if not notifications and max_notifications is not None and self.poll_seconds <= 0:
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
    )
    listener.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
