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
            return Result(one={"snooze": True, "size_review": True})  # and SYRD-541's size review
        if "to_regclass" in sql:
            relation = "ticket_board.workflow_configuration" if self.workflow else None
            return Result(one={"relation": relation})
        if "SELECT document FROM ticket_board.workflow_configuration" in sql:
            return Result(one={"document": self.workflow})
        if "WITH notification_scope" in sql:
            selected = params[0] if params else None
            row = {"id": "SYRD-1", "state": "in_progress", "updated": "2026-01-01"}
            return Result(many=[row] if selected in (None, "SYRD-1") else [])
        if "FROM ticket_board.publication_requests" in sql:
            return Result()
        raise AssertionError(sql)

    def ticket_queries(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [(sql, params) for sql, params in self.calls if "WITH notification_scope" in sql]


def configured_workflow() -> dict[str, Any]:
    return {
        "stages": [
            {"name": "director's review", "terminal": False, "kind": "review"},
            {"name": "done", "terminal": True, "kind": "terminal"},
        ],
        "transitions": [],
    }


def test_legacy_and_configured_sql_bytes_and_parameters() -> None:
    # SYRD-537 added the reminder_snooze column, SYRD-541 size_review; hashes measured from the new SQL.
    expected = {
        False: "dba04e7674be8e02aa26ad6a100836a4457888a907d3ac820a417c831263d29d",
        True: "0f3b03bac4e3a3c634e34a917e4a20f0b6153858b2709eaf007f955d8b45abb7",
    }
    for workflow in (None, configured_workflow()):
        conn = ReadOnlyConnection(workflow)
        assert select_ticket_rows(conn) == [
            {"id": "SYRD-1", "state": "in_progress", "updated": "2026-01-01"}
        ]
        sql, params = conn.ticket_queries()[0]
        assert params == ()
        assert hashlib.sha256(sql.encode()).hexdigest() == expected[bool(workflow)]
        if workflow:
            assert "'director''s review'" in sql
            assert "'done'" not in sql
        else:
            assert "scoped.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review')" in sql
        assert select_ticket_rows(conn, "SYRD-1")
        assert conn.ticket_queries()[-1][1] == ("SYRD-1",)
        assert "WHERE t.id = %s" in conn.ticket_queries()[-1][0]


def test_list_and_detail_reuse_the_app_connection_without_writes() -> None:
    conn = ReadOnlyConnection(configured_workflow())
    app = TicketBoardApp.__new__(TicketBoardApp)
    app._pg_connect = lambda: conn
    app._pg_row_to_ticket = lambda row: dict(row)
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
    app._pg_connect = lambda: conn
    app._pg_row_to_ticket = lambda row: dict(row)
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
