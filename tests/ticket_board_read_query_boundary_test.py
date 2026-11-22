#!/usr/bin/env python3
"""SYRD-499: ticket read SQL and caller connection identity, without a database."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp  # noqa: E402
from scripts.ticket_board.ticket_read_query import select_ticket_rows  # noqa: E402


class Result:
    def __init__(self, *, one: Any = None, many: list[dict[str, str]] | None = None) -> None:
        self.one = one
        self.many = many if many is not None else []

    def fetchone(self) -> Any:
        return self.one

    def fetchall(self) -> list[dict[str, str]]:
        return self.many


class ReadOnlyConnection:
    def __init__(self, workflow: dict[str, Any] | None = None) -> None:
        self.workflow = workflow
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.enters = 0

    def __enter__(self) -> ReadOnlyConnection:
        self.enters += 1
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Result:
        assert sql.lstrip().startswith(("SELECT", "WITH")), sql
        self.calls.append((sql, tuple(params)))
        if "to_regprocedure" in sql:
            # SYRD-537: the reminder-snooze probe; this board has the function.
            return Result(one={"snooze": True, "size_review": True, "serial_queue": True})  # SYRD-541 size review, SYRD-568 queue
        if "to_regclass" in sql:
            relation = "ticket_board.workflow_configuration" if self.workflow else None
            return Result(one={"relation": relation})
        if "SELECT document FROM ticket_board.workflow_configuration" in sql:
            return Result(one={"document": self.workflow})
        if "notification_scope AS (" in sql:  # SYRD-568: after the serial_queue CTE it reads
            selected = params[0] if params else None
            row = {"id": "SYRD-1", "state": "in_progress", "updated": "2026-01-01"}
            return Result(many=[row] if selected in (None, "SYRD-1") else [])
        if "FROM ticket_board.publication_requests" in sql:
            return Result()
        raise AssertionError(sql)

    def ticket_queries(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [(sql, params) for sql, params in self.calls if "notification_scope AS (" in sql]


def configured_workflow() -> dict[str, Any]:
    return {
        "stages": [
            {"name": "director's review", "terminal": False, "kind": "review"},
            {"name": "done", "terminal": True, "kind": "terminal"},
        ],
        "transitions": [],
    }


def plan_gated_workflow() -> dict[str, Any]:
    """SYRD-850: the same workflow with the plan gate's policy, so the query reads each ticket's deliverable."""
    return {**configured_workflow(),
            "plan": {"owner": "main", "file": "docs/implementation-plan.md", "decision_log": "docs/decision-log.md",
                     "max_lines": 1500}}


def test_legacy_and_configured_sql_bytes_and_parameters() -> None:
    # SYRD-537 added the reminder_snooze column, SYRD-541 size_review, SYRD-568 the serial_queue CTE and
    # column, SYRD-113 privileged_jobs; hashes measured from the new SQL.
    # SYRD-851 re-pin: stale since SYRD-595 (e986c496), whose change and those of SYRD-599, 626, 629, 662, 673, 697
    # and 850 each altered this SQL on purpose without re-measuring it; measured again from main's e62f8efd.
    expected = {
        "legacy": "005f5e066bd7d8e52829ba23851e0ca5f56ecd3a1409321de6b225eb22803aff",
        "configured": "f6a00bc7b05f4d9f65d3aa4b4c484ece1056337952e64f7bc9974820a9935a75",
        "plan": "3e6dff37dd9e328555a0e71668daf8074fb622a011f36666d571d64bd73b6060",
    }
    for name, workflow in (("legacy", None), ("configured", configured_workflow()), ("plan", plan_gated_workflow())):
        conn = ReadOnlyConnection(workflow)
        assert select_ticket_rows(conn) == [
            {"id": "SYRD-1", "state": "in_progress", "updated": "2026-01-01"}
        ]
        sql, params = conn.ticket_queries()[0]
        assert params == ()
        assert hashlib.sha256(sql.encode()).hexdigest() == expected[name], (name, hashlib.sha256(sql.encode()).hexdigest())
        if workflow:
            assert "'director''s review'" in sql
            assert "'done'" not in sql
        else:
            assert "scoped.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review')" in sql
        # SYRD-850: the deliverable is read only on a board that runs the plan gate.
        assert ("FROM ticket_board.ticket_deliverables td WHERE td.ticket_id = t.id" in sql) == (name == "plan"), name
        assert select_ticket_rows(conn, "SYRD-1")
        assert conn.ticket_queries()[-1][1] == ("SYRD-1",)
        assert "WHERE t.id = %s" in conn.ticket_queries()[-1][0]


def test_list_and_detail_reuse_the_app_connection_without_writes() -> None:
    conn = ReadOnlyConnection(configured_workflow())
    app = TicketBoardApp.__new__(TicketBoardApp)
    # SYRD-572: a read takes the workflow's stage names first; a running app has them cached.
    app._workflow_states_cache = ("in_progress",)
    app._pg_connect = lambda: conn
    app._pg_row_to_ticket = lambda row, *_validation: dict(row)
    assert app.list_tickets() == ([
        {"id": "SYRD-1", "state": "in_progress", "updated": "2026-01-01"}
    ], [])
    assert conn.enters == 1

    def no_second_connection() -> None:
        raise AssertionError("detail query opened a new connection")

    app._pg_connect = no_second_connection
    detail = app._pg_get_ticket("SYRD-1", conn)
    assert detail["id"] == "SYRD-1"
    assert detail["workflow_actions"] == []
    assert detail["publication"] == {}
    assert conn.enters == 1
    assert conn.ticket_queries()[-1][1] == ("SYRD-1",)


def test_public_detail_normalizes_id_and_missing_id_refuses() -> None:
    conn = ReadOnlyConnection()
    app = TicketBoardApp.__new__(TicketBoardApp)
    # SYRD-572: a read takes the workflow's stage names first; a running app has them cached.
    app._workflow_states_cache = ("in_progress",)
    app._pg_connect = lambda: conn
    app._pg_row_to_ticket = lambda row, *_validation: dict(row)
    assert app.get_ticket(" syrd-1 ")["id"] == "SYRD-1"
    try:
        app.get_ticket("syrd-2")
    except FileNotFoundError as exc:
        assert str(exc) == "ticket not found: SYRD-2"
    else:
        raise AssertionError("missing ticket returned a row")
    assert [params for _, params in conn.ticket_queries()] == [("SYRD-1",), ("SYRD-2",)]
    assert conn.enters == 2


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_read_query_boundary_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
