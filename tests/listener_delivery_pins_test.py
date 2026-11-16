#!/usr/bin/env python3
"""SYRD-809: two listener rules that ticket_board_notify_listener_test pinned in ways that hid them.

* A hand-over is delivered whoever holds the ticket: manual control alone holds none (SYRD-178). The final-review case
  in the notify suite was written (SYRD-21) when manual control dropped one; it now holds the carve-out against the
  holds that remain, parked and blocked, and this pins the delivery under manual control.
* The listener's directorctl is the release's own copy unless TICKET_BOARD_DIRECTORCTL names another. The notify suite
  compared the import-time default with the release's path, which every role pane's exported override broke.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_notify_listener_test as notify  # noqa: E402 - its fake board connection and queue rows
from scripts.ticket_board import notification_dispatch  # noqa: E402
from scripts.ticket_board.runtime_paths import directorctl_path  # noqa: E402
from submission_witness_fixture import taking_listener  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def test_manual_control_alone_holds_no_hand_over() -> None:
    sent: list[tuple[str, str]] = []
    conn = notify.FakeConnection(
        [notify.queue_row(61, "PGU-61", state="analysis", assignee="director", target_role="director")],
        ticket_rows={"PGU-61": ("analysis", "director", True, False, False)})  # manually controlled, nothing else
    listener = taking_listener(conninfo="dbname=test", sender=lambda target, text: sent.append((target, text)),
                               activity_gate=lambda _target: False, connector=lambda *args, **kwargs: conn,
                               poll_seconds=0, target_exists=lambda _target: True)
    delivered = listener.listen_once(max_notifications=1)
    check(delivered == 1 and len(sent) == 1 and conn.acked == [61],
          f"a manually controlled ticket's hand-over to the Director is delivered: {sent} {notify.trace_events(conn)}")


def test_the_release_directorctl_unless_overridden() -> None:
    with mock.patch.dict(os.environ, {"TICKET_BOARD_DIRECTORCTL": ""}):
        check(directorctl_path(notification_dispatch.__file__) == str(ROOT / "scripts" / "directorctl"),
              "with no override, the release's own directorctl")
    with mock.patch.dict(os.environ, {"TICKET_BOARD_DIRECTORCTL": "/opt/elsewhere/directorctl"}):
        check(directorctl_path(notification_dispatch.__file__) == "/opt/elsewhere/directorctl",
              "with one, the directorctl it names")
    check(notification_dispatch.DEFAULT_DIRECTORCTL == directorctl_path(notification_dispatch.__file__),
          "and the listener's default is that rule's answer in this environment")


def main() -> int:
    notify.notify_listener.tmux_target_exists = lambda _target: True  # type: ignore[assignment]
    for test in (test_manual_control_alone_holds_no_hand_over, test_the_release_directorctl_unless_overridden):
        test()
    print(f"listener_delivery_pins_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
