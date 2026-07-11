"""LISTEN shim for PostgreSQL ticket-board transition notifications."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg import sql

CHANNEL = "ticket_board_state_transition"
DEFAULT_DATABASE_URL = os.environ.get("TICKET_BOARD_NOTIFY_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
DEFAULT_RECONNECT_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 5.0
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
    def __init__(self, directorctl_bin: str = DEFAULT_DIRECTORCTL) -> None:
        self.directorctl_bin = directorctl_bin

    def __call__(self, target: str, message: str) -> None:
        subprocess.run([self.directorctl_bin, "send", target, message], check=True)


class PaneActivityGate:
    def __init__(self, directorctl_bin: str = DEFAULT_DIRECTORCTL) -> None:
        self.directorctl_bin = directorctl_bin

    def is_working(self, target: str) -> bool:
        try:
            proc = subprocess.run(
                [self.directorctl_bin, "capture", target, "40"],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        text = proc.stdout
        return "Working" in text or "esc to interrupt" in text


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
        self.stop_event = stop_event or threading.Event()
        self.logger = logger
        self.delivered_count = 0

    def deliver_payload(self, payload: str) -> bool:
        transition = parse_transition_payload(payload)
        target = target_for_transition(transition)
        message = message_for_transition(transition)
        if not target or not message:
            self.logger.debug("Skipping transition notification: %s", payload)
            return False
        if message.startswith("NUDGE") and self.activity_gate(target):
            self.logger.info("Suppressed NUDGE for active pane %s", target)
            return False
        self.sender(target, message)
        self.delivered_count += 1
        self.logger.info("Delivered %s transition to %s", transition.ticket_id, target)
        return True

    def reconcile_pending_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
        delivered = 0
        rows = conn.execute("SELECT ticket_id, payload::text FROM ticket_board.pending_transition_notifications()").fetchall()
        for ticket_id, payload in rows:
            ticket_id_text = ticket_id.decode("utf-8") if isinstance(ticket_id, bytes) else str(ticket_id)
            if self.stop_event.is_set():
                break
            if max_notifications is not None and self.delivered_count >= max_notifications:
                break
            try:
                if self.deliver_payload(payload):
                    conn.execute("SELECT ticket_board.mark_transition_notified(%s::text)", (ticket_id_text,))
                    delivered += 1
            except ValueError as exc:
                self.logger.warning("Skipping malformed reconciled ticket notification payload: %s", exc)
            except (subprocess.CalledProcessError, OSError) as exc:
                self.logger.warning("Failed to deliver reconciled ticket notification through directorctl: %s", exc)
        return delivered

    def listen_once(self, *, max_notifications: int | None = None) -> int:
        delivered_before = self.delivered_count
        with self.connector(self.conninfo, autocommit=True) as conn:
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
            self.logger.info("LISTEN %s established", self.channel)
            self.reconcile_pending_notifications(conn, max_notifications=max_notifications)
            while not self.stop_event.is_set():
                for notify in conn.notifies(timeout=self.poll_seconds):
                    try:
                        delivered = self.deliver_payload(notify.payload)
                        transition = parse_transition_payload(notify.payload)
                        if delivered and transition.kind == "transition" and transition.ticket_id:
                            conn.execute("SELECT ticket_board.mark_transition_notified(%s::text)", (transition.ticket_id,))
                    except ValueError as exc:
                        self.logger.warning("Skipping malformed ticket notification payload: %s", exc)
                    except (subprocess.CalledProcessError, OSError) as exc:
                        self.logger.warning("Failed to deliver ticket notification through directorctl: %s", exc)
                    if max_notifications is not None and self.delivered_count >= max_notifications:
                        self.stop_event.set()
                        break
                if max_notifications is not None and self.delivered_count >= max_notifications:
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
