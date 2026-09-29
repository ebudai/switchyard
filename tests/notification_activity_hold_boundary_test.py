#!/usr/bin/env python3
"""SYRD-492: one owner for activity policy and a handoff's finished-turn timer."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board import notification_activity_hold as ah  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402
from ticket_board_notify_listener_test import FakeConnection  # noqa: E402


class Gate:
    def __init__(self, trace: ah.ActivityTrace) -> None:
        self.trace = trace

    def is_working(self, _target: str) -> bool:
        return self.trace.busy

    def pre_send_busy(self, target: str) -> bool:
        return self.is_working(target)

    def last_trace(self, _target: str) -> ah.ActivityTrace:
        return self.trace


def test_late_gate_and_work_reason_rebinding() -> None:
    first = Gate(ah.ActivityTrace(False, "idle"))
    listener = nl.TicketBoardNotifyListener(conninfo="offline", activity_gate=first.is_working)
    assert listener.activity_hold._activity_state_for_notification("transition", "s492-main:0.0") == (False, first.trace)

    second = Gate(ah.ActivityTrace(True, "patched_work"))
    listener.activity_gate = second.is_working
    old_reasons = nl.WORK_EVIDENCE_REASONS
    try:
        nl.WORK_EVIDENCE_REASONS = frozenset({"patched_work"})
        assert listener.activity_hold._activity_state_for_notification("transition", "s492-main:0.0", pre_send_recheck=True) == (True, second.trace)
        assert listener.activity_hold._reminder_is_stale_for_activity("nudge", second.trace)
        assert not listener.activity_hold._reminder_is_stale_for_activity("escalation", second.trace)
    finally:
        nl.WORK_EVIDENCE_REASONS = old_reasons


def test_one_notification_timer_and_current_turn_reset() -> None:
    clock = [0.0]
    listener = nl.TicketBoardNotifyListener(
        conninfo="offline", activity_gate=Gate(ah.ActivityTrace(False, "idle")).is_working,
        prior_turn_hold_max_seconds=900.0, monotonic=lambda: clock[0],
    )
    owner = listener.activity_hold
    assert not hasattr(listener, "_prior_turn_hold_started_at")
    prior = ah.ActivityTrace(True, ah.PRIOR_TURN_CHILD_WORK)
    current = ah.ActivityTrace(True, "pane_child_work")
    conn = FakeConnection()

    def release(notification_id: int, trace: ah.ActivityTrace) -> str:
        return owner._release_prior_turn_hold(
            conn, notification_id=notification_id, ticket_id="SYRD-492",
            target_role="main", kind="transition", activity_trace=trace,
        )

    assert release(1, prior) == ""
    clock[0] = 899.0
    assert release(1, prior) == ""
    assert release(2, prior) == ""
    assert owner._prior_turn_hold_started_at == {1: 0.0, 2: 899.0}
    assert release(1, current) == ""
    assert owner._prior_turn_hold_started_at == {2: 899.0}
    clock[0] = 1800.0
    assert release(2, prior) == ah.PRIOR_TURN_HOLD_DELIVER
    assert owner._prior_turn_hold_started_at == {}
    assert [trace[4] for trace in conn.traces] == ["gate_escalate"]


def test_public_aliases_and_bounded_source_owner() -> None:
    for name in (
        "ActivityTrace", "SELF_REMINDER_KINDS", "DEFAULT_PRIOR_TURN_HOLD_MAX_SECONDS",
        "ORPHANED_PRIOR_TURN_WAITER", "PRIOR_TURN_CHILD_WORK",
        "PRIOR_TURN_HOLD_REASONS", "OWNER_ALREADY_ACTED",
        "PRIOR_TURN_HOLD_DELIVER", "PRIOR_TURN_HOLD_DISCARD",
    ):
        assert getattr(nl, name) is getattr(ah, name), name
    owner_source = (ROOT / "scripts/ticket_board/notification_activity_hold.py").read_text()
    listener_source = (ROOT / "scripts/ticket_board/notify_listener.py").read_text()
    assert "notify_listener" not in owner_source
    owner_tree = ast.parse(owner_source)
    listener_tree = ast.parse(listener_source)
    owner_class = next(node for node in owner_tree.body if isinstance(node, ast.ClassDef) and node.name == "NotificationActivityHold")
    listener_class = next(node for node in listener_tree.body if isinstance(node, ast.ClassDef) and node.name == "TicketBoardNotifyListener")
    moved = {
        "_activity_trace", "_activity_state_for_notification", "_should_defer_for_activity",
        "_reminder_is_stale_for_activity", "_drop_stale_reminder",
        "_release_prior_turn_hold", "_owner_already_notified_at",
    }
    owner_methods = {node.name for node in owner_class.body if isinstance(node, ast.FunctionDef)}
    listener_methods = {node.name for node in listener_class.body if isinstance(node, ast.FunctionDef)}
    assert moved <= owner_methods
    assert not moved & listener_methods
    assert "_prior_turn_hold_started_at" in owner_source
    assert "_prior_turn_hold_started_at" not in listener_source


def main() -> int:
    test_late_gate_and_work_reason_rebinding()
    test_one_notification_timer_and_current_turn_reset()
    test_public_aliases_and_bounded_source_owner()
    print("notification_activity_hold_boundary_test: 3 cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
