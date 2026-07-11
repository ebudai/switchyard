#!/usr/bin/env python3
"""Regression tests for the PostgreSQL ticket-board NOTIFY delivery shim."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import TicketBoardNotifyListener


@dataclass(frozen=True)
class FakeNotify:
    payload: str


class FakeConnection:
    def __init__(
        self,
        notifications: list[str] | None = None,
        *,
        fail_on_notifies: bool = False,
        executed: list[Any] | None = None,
    ) -> None:
        self.notifications = notifications or []
        self.fail_on_notifies = fail_on_notifies
        self.executed = executed if executed is not None else []
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.closed = True

    def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    def notifies(self, *, timeout: float | None = None) -> list[FakeNotify]:
        if self.fail_on_notifies:
            raise psycopg.OperationalError("simulated connection drop")
        queued = [FakeNotify(payload) for payload in self.notifications]
        self.notifications = []
        return queued


def transition_payload(
    ticket_id: str,
    *,
    title: str = "Ticket title",
    old_state: str = "analysis",
    new_state: str = "ready",
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


def test_simulated_pg_notify_delivers_to_assignee_pane() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    conn = FakeConnection([transition_payload("PGU-213", title="Restore notifications", assignee="ops")], executed=executed)
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )

    delivered = listener.listen_once(max_notifications=1)

    assert delivered == 1
    assert sent == [("pgu-ops:0.0", "Ready ticket for you: PGU-213 -- Restore notifications")]
    assert executed, "listener must issue LISTEN before consuming notifications"
    assert conn.closed is True


def test_eric_review_is_not_delivered() -> None:
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
    )

    delivered = listener.deliver_payload(
        transition_payload("PGU-214", old_state="audit", new_state="eric_review", assignee="director")
    )

    assert delivered is False
    assert sent == []
    assert listener.delivered_count == 0


def test_reconnect_relistens_after_connection_drop() -> None:
    sent: list[tuple[str, str]] = []
    executed: list[Any] = []
    connections = [
        FakeConnection(fail_on_notifies=True, executed=executed),
        FakeConnection(
            [transition_payload("PGU-215", title="Reconnect", old_state="analysis", new_state="ready", assignee="app")],
            executed=executed,
        ),
    ]

    def connector(*args: Any, **kwargs: Any) -> FakeConnection:
        return connections.pop(0)

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: sent.append((target, message)),
        connector=connector,
        reconnect_seconds=0,
        poll_seconds=0,
    )

    listener.run_forever(max_connections=2, max_notifications=1)

    assert sent == [("pgu-app:0.0", "Ready ticket for you: PGU-215 -- Reconnect")]
    assert len(executed) == 2, "listener must re-issue LISTEN after reconnecting"


def main() -> int:
    test_simulated_pg_notify_delivers_to_assignee_pane()
    test_eric_review_is_not_delivered()
    test_reconnect_relistens_after_connection_drop()
    print("ticket_board_notify_listener_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
