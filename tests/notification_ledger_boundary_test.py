#!/usr/bin/env python3
"""SYRD-484: notification writes and deferral memory have one owner.

These cases use a recording connection. No database, pane, process, account,
host path or network service is needed.
"""

from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notification_ledger import NotificationLedger  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402


class Connection:
    def __init__(self, *, fail_trace: bool = False) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_trace = fail_trace

    def execute(self, statement: str, params: tuple[Any, ...]) -> None:
        self.statements.append((" ".join(statement.split()), params))
        if self.fail_trace and "record_notification_trace" in statement:
            raise RuntimeError("trace unavailable")


def fields() -> dict[str, Any]:
    return dict(ticket_id="SYRD-1", target_role="main", kind="route",
                event="gate_defer", pane_busy=True, busy_reason="busy", region_digest="abc")


def test_once_is_lazy_and_failed_trace_attempts_are_marked() -> None:
    warnings: list[str] = []

    class Logger:
        def warning(self, message: str, *args: Any) -> None:
            warnings.append(message % args)

    ledger = NotificationLedger(logger=Logger(),
                                requeue_base_seconds=1, requeue_max_seconds=300)
    conn = Connection(fail_trace=True)
    detail_calls = 0

    def detail() -> dict[str, Any]:
        nonlocal detail_calls
        detail_calls += 1
        return {"attempts": detail_calls}

    ledger.trace_deferral_once(conn, notification_id=7, detail=detail, **fields())
    ledger.trace_deferral_once(conn, notification_id=7, detail=detail, **fields())
    assert detail_calls == 1
    assert len(conn.statements) == 1
    assert len(warnings) == 1 and "trace unavailable" in warnings[0]
    assert ledger._traced_gate_defer_notifications == {7}
    ledger.forget(7)
    ledger.trace_deferral_once(conn, notification_id=7, detail=detail, **fields())
    assert detail_calls == 2
    assert len(conn.statements) == 2


def test_writes_keep_statement_parameters_and_disposition_order() -> None:
    ledger = NotificationLedger(logger=logging.getLogger("ledger-boundary"),
                                requeue_base_seconds=5, requeue_max_seconds=300)
    conn = Connection()
    ledger.trace_deferral_once(conn, notification_id=9, detail=lambda: {"x": 1}, **fields())
    statement, params = conn.statements[-1]
    assert "ticket_board.record_notification_trace(" in statement
    assert params == ("SYRD-1", 9, "main", "route", "gate_defer", "busy", "busy", "abc", '{"x": 1}')
    ledger.requeue(conn, 9, 3, "error")
    assert conn.statements[-1] == ("SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
                                   (9, "20 seconds", "error"))
    ledger.requeue(conn, 9, 3, "x" * 501, delay_seconds=2.5)
    assert conn.statements[-1][1] == (9, "2.5 seconds", "x" * 500)
    ledger.ack(conn, 9)
    assert conn.statements[-1] == ("SELECT ticket_board.ack_notification(%s::bigint)", (9,))
    ledger.discard(conn, 9, "stale")
    assert conn.statements[-1] == ("SELECT ticket_board.discard_notification(%s::bigint, %s::text)", (9, "stale"))
    ledger.dead_letter(conn, 9, "r" * 501, target="main:0.0", message="hello",
                       attempts=4, payload="broken{", error_output="e" * 1001)
    statement, params = conn.statements[-1]
    assert statement == "SELECT ticket_board.dead_letter_notification(%s::bigint, %s::text, %s::jsonb)"
    assert params[0:2] == (9, "r" * 500)
    assert json.loads(params[2]) == {"target": "main:0.0", "message": "hello", "attempts": 4,
                                     "payload": "broken{", "error_output": "e" * 1000}
    assert ledger._traced_gate_defer_notifications == set()


def test_backoff_and_late_timing_setters_reach_ledger() -> None:
    listener = TicketBoardNotifyListener(conninfo="", sender=lambda _target, _message: None,
                                         activity_gate=lambda _target: False,
                                         target_exists=lambda _target: True)
    assert isinstance(listener.ledger, NotificationLedger)
    assert "_traced_gate_defer_notifications" not in vars(listener)
    assert "requeue_base_seconds" not in vars(listener)
    assert "requeue_max_seconds" not in vars(listener)
    listener.requeue_base_seconds = 7
    listener.requeue_max_seconds = 21
    assert (listener.ledger.requeue_base_seconds, listener.ledger.requeue_max_seconds) == (7, 21)
    assert [listener.ledger.backoff_seconds(n) for n in range(1, 6)] == [7, 14, 21, 21, 21]
    conn = Connection()
    listener.ledger.requeue(conn, 2, 2, "busy")
    assert conn.statements[-1][1] == (2, "14 seconds", "busy")


def test_listener_has_no_write_method_or_set_owner() -> None:
    tree = ast.parse((ROOT / "scripts/ticket_board/notify_listener.py").read_text())
    listener = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TicketBoardNotifyListener")
    names = {n.name for n in listener.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert names.isdisjoint({"_trace_notification", "_ack_notification", "_discard_notification",
                             "_requeue_notification", "_dead_letter_notification", "_backoff_seconds",
                             "_safe_json_payload"})
    assert "_traced_gate_defer_notifications" not in (ROOT / "scripts/ticket_board/notify_listener.py").read_text()
    ledger_source = (ROOT / "scripts/ticket_board/notification_ledger.py").read_text()
    assert "notify_listener" not in ledger_source


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print("notification_ledger_boundary_test: 4 cases ok")
