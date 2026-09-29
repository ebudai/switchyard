#!/usr/bin/env python3
"""SYRD-491: dispatch owns one send, its receipt, and the resulting disposition."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board import notification_dispatch as d  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402


class Ledger:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events

    def trace(self, _conn: object, **kwargs: Any) -> None:
        self.events.append(("trace", kwargs["event"], kwargs.get("busy_reason")))

    def ack(self, _conn: object, notification_id: int) -> None:
        self.events.append(("ack", notification_id))

    def forget(self, notification_id: int) -> None:
        self.events.append(("forget", notification_id))

    def requeue(self, _conn: object, notification_id: int, attempts: int, reason: str) -> None:
        self.events.append(("requeue", notification_id, attempts, reason))

    def dead_letter(self, _conn: object, notification_id: int, reason: str, **_kwargs: Any) -> None:
        self.events.append(("dead_letter", notification_id, reason))


class Gate:
    def __init__(self, events: list[tuple[Any, ...]], label: str) -> None:
        self.events = events
        self.label = label

    def is_working(self, _target: str) -> bool:
        return False

    def composer_snapshot(self, _target: str) -> d.ComposerSnapshot:
        self.events.append(("snapshot", self.label))
        return d.ComposerSnapshot(True, active=False, content_sha256=self.label)


def test_send_outcomes_and_late_providers() -> None:
    events: list[tuple[Any, ...]] = []
    current: dict[str, Any] = {
        "gate": Gate(events, "old"),
        "sender": lambda *_args: (_ for _ in ()).throw(AssertionError("stale sender")),
        "witness": lambda *_args: False,
        "confirm": 0.0,
        "poll": 0.01,
    }
    dispatch = d.NotificationDispatch(
        logger=logging.getLogger("notification-dispatch-boundary"),
        ledger=Ledger(events),  # type: ignore[arg-type] -- recorded ledger boundary
        sender=lambda: current["sender"],
        activity_gate=lambda: current["gate"].is_working,
        submission_witness=lambda: current["witness"],
        wall_clock=lambda: 100.0,
        monotonic=lambda: 200.0,
        sleeper=lambda _seconds: events.append(("sleep",)),
        submission_confirm_seconds=lambda: current["confirm"],
        submission_poll_seconds=lambda: current["poll"],
    )
    conn = object()
    trace = SimpleNamespace(busy=False, reason="hook_idle", region_digest="digest")
    before = d.ComposerSnapshot(True, active=False, content_sha256="before")

    def attempt(notification_id: int) -> bool:
        return dispatch.send(
            conn, notification_id=notification_id, ticket_id="SYRD-491",
            target_role="main", kind="transition", target="s491-main:0.0",
            message="SYRD-491 is yours", payload='{"kind":"transition"}',
            attempts=2, pane_busy=False, activity_trace=trace,
            composer_before=before,
        )

    current["gate"] = Gate(events, "rebound")
    current["sender"] = lambda _target, _message: events.append(("send",)) or {"delivery_mode": "direct"}
    current["witness"] = lambda _target, since: events.append(("witness", since)) or True
    assert attempt(1) is True
    assert events == [
        ("send",), ("snapshot", "rebound"), ("witness", 100.0),
        ("trace", "send", "hook_idle"), ("trace", "listener_ack", "hook_idle"),
        ("ack", 1), ("forget", 1),
    ], events

    events.clear()
    current["witness"] = lambda _target, _since: None
    assert attempt(2) is False
    assert events == [
        ("send",), ("snapshot", "rebound"),
        ("trace", "send_unconfirmed", "no_hook_state"),
        ("ack", 2), ("forget", 2),
    ], events

    events.clear()
    current["sender"] = lambda *_args: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["directorctl", "send"], stderr="cannot resolve runtime assignment for main")
    )
    assert attempt(3) is False
    assert events == [
        ("snapshot", "rebound"),
        ("trace", "send_failed", "runtime_assignment_unresolved"),
        ("requeue", 3, 2, "runtime_assignment_unresolved"),
    ], events

    events.clear()
    current["sender"] = lambda *_args: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["directorctl", "send"], stderr="can't find pane: s491-main:0.0")
    )
    assert attempt(4) is False
    assert events == [
        ("snapshot", "rebound"),
        ("trace", "send_failed", "tmux_target_missing"),
        ("dead_letter", 4, "tmux_target_missing"),
    ], events


def test_public_imports_and_module_boundary() -> None:
    for name in (
        "ComposerSnapshot", "DirectorctlSender", "display_message",
        "parse_directorctl_diagnostic", "delivery_error_output",
        "delivery_failure_reason", "tmux_target_exists",
        "DEFAULT_DIRECTORCTL", "DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS",
        "DEFAULT_SUBMISSION_CONFIRM_SECONDS", "DEFAULT_SUBMISSION_POLL_SECONDS",
        "SEND_UNCONFIRMED_EVENT", "NO_SUBMISSION_WITNESSED",
        "NO_HOOK_STATE", "NO_SUBMISSION_WITNESS",
        "RUNTIME_ASSIGNMENT_UNRESOLVED", "RUNTIME_ASSIGNMENT_MARKERS",
    ):
        assert getattr(nl, name) is getattr(d, name), name
    assert "notify_listener" not in (ROOT / "scripts/ticket_board/notification_dispatch.py").read_text()


def main() -> int:
    test_send_outcomes_and_late_providers()
    test_public_imports_and_module_boundary()
    print("notification_dispatch_boundary_test: 2 cases, 4 dispositions ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
