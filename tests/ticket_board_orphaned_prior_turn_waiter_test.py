#!/usr/bin/env python3
"""SYRD-212: a finished turn's poll loop may not starve a later handoff.

Routing SYRD-211 to App on 2026-09-18. The transition was enqueued at 21:56 as
notification 2842 and was still undelivered 38 minutes later. App's trusted hook
had said idle at 21:02 and its composer was empty; the only descendant was a
SYRD-210 shell started at 20:49, polling a task-output file every ten seconds
for a marker that was never going to appear.

SYRD-101 had already taught the gate to discount a finished turn's leftovers,
and made one exception: a descendant still making progress is real work whatever
turn started it, so an hour-long sweep keeps its pane quiet. The exception was
tested by asking whether ANY survivor's CPU had risen -- and a poll loop's does.
So the exception swallowed the rule, `pane_child_work` was returned before the
prior-turn classification could run, and the handoff could be starved for ever.
The Director ended it by stopping the process group by hand.

Two things are needed and neither is a command-name allowlist. The probe has to
say WHICH turn a hold comes from, so that a wait behind a finished turn is
visible as such. And the wait itself has to be bounded, because a probe cannot
know how long a build should be allowed to run while the handoff can know how
long it has been waiting.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board.notify_listener import (
    ORPHANED_PRIOR_TURN_WAITER,
    OWNER_ALREADY_ACTED,
    PRIOR_TURN_CHILD_WORK,
    STALE_PRIOR_TURN_CHILD_WORK,
    TicketBoardNotifyListener,
)
from standalone_test_runner import run_module_tests
from ticket_board_notify_listener_test import FakeConnection, queue_row
from ticket_board_stale_prior_turn_child_work_test import (
    Pane,
    TemporaryStateDir,
    build_gate,
)

#: The pids in the incident, kept so the fixture reads as the report does.
POLLING_CHILD = 3351141
SWEEP_CHILD = 6000


def polling_pane(state_dir: Path) -> Pane:
    """App's pane as it stood: turn over, one poll loop still waking.

    The child starts before the turn-end hook and wakes between the gate's two
    samples often enough to spend CPU -- which is exactly what made it look like
    work. Nothing about it is named; it is described by when it started and how
    it spends time.
    """
    pane = Pane(state_dir)
    pane.tree.start(POLLING_CHILD, detached=True, burning=False)
    child = pane.tree.living[POLLING_CHILD]
    pane.advance(60.0)
    pane.turn_ended()
    pane.advance(4200.0)

    reads = {"n": 0}
    underlying = pane.tree.read

    def waking_read():
        reads["n"] += 1
        if reads["n"] % 2 == 0:
            child.cpu += 3
        return underlying()

    pane.tree.read = waking_read
    pane.gate = build_gate(state_dir, pane.target, pane.tree)
    return pane


def test_a_waking_poll_loop_is_no_longer_read_as_this_turns_work() -> None:
    """The regression itself, at the probe.

    Before SYRD-212 this returned `pane_child_work`: the tree-wide CPU sum could
    not say whose ticks it had counted, so a waiter's and a worker's were the
    same number. The hold may still be there -- that is the bound's problem --
    but it must never again be recorded as the CURRENT turn working.
    """
    with TemporaryStateDir() as state_dir:
        pane = polling_pane(state_dir)
        busy, reason = pane.verdict()

    assert reason != "pane_child_work", (
        "a turn that ended an hour ago is not this turn's work: " + reason
    )
    assert reason in {
        PRIOR_TURN_CHILD_WORK,
        ORPHANED_PRIOR_TURN_WAITER,
        STALE_PRIOR_TURN_CHILD_WORK,
    }, reason
    del busy


def test_a_sweep_from_before_the_turn_end_still_holds_and_says_which_turn() -> None:
    """The protection SYRD-101 bought, kept, and now legible.

    A verification run started before the turn-end hook is real work and keeps
    holding. What changed is only that the record says it came from a turn that
    has already finished, which is what lets the wait behind it be bounded
    without interrupting it.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(SWEEP_CHILD, detached=True, burning=True)
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(7200.0)
        busy, reason = pane.verdict()

    assert busy is True, reason
    assert reason == PRIOR_TURN_CHILD_WORK, reason


def test_work_the_current_turn_started_is_still_named_as_such() -> None:
    """The distinction has to cut both ways, or it names nothing."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.turn_ended()
        pane.advance(10.0)
        pane.tree.start(7000, detached=True, burning=True)
        busy, reason = pane.verdict()

    assert busy is True, reason
    assert reason == "pane_child_work", reason


class HeldPane:
    """A listener whose pane reports one reason, with a clock a test can move."""

    def __init__(self, reason: str, *, busy: bool = True) -> None:
        self.reason = reason
        self.busy = busy
        self.now = 1_000.0
        self.sent: list[tuple[str, str]] = []

    def is_working(self, target: str) -> bool:
        """The seam the listener calls: a bool, with the reason read separately."""
        return self.busy

    def last_trace(self, target: str):
        """Where the listener gets the reason from -- the gate object itself."""
        from scripts.ticket_board.notify_listener import ActivityTrace

        return ActivityTrace(self.busy, self.reason)

    def listener(self, conn: FakeConnection, **kwargs) -> TicketBoardNotifyListener:
        return TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: self.sent.append((target, message)),
            activity_gate=self.is_working,
            connector=lambda *args, **more: conn,
            poll_seconds=0,
            monotonic=lambda: self.now,
            **kwargs,
        )


def test_a_handoff_waits_behind_a_finished_turn_only_for_the_bound() -> None:
    """The starvation, ended.

    The pane keeps reporting the same finished-turn hold. Within the bound the
    handoff is requeued, exactly as before. Past it the handoff goes out anyway,
    and the wait is recorded against it rather than disappearing into another
    retry.
    """
    pane = HeldPane(PRIOR_TURN_CHILD_WORK)
    conn = FakeConnection([queue_row(2842, "SYRD-211", target_role="app")])
    listener = pane.listener(conn, prior_turn_hold_max_seconds=900.0)

    listener.listen_once(max_notifications=1)
    assert pane.sent == [], f"inside the bound it still waits: {pane.sent}"
    assert conn.requeued, "and is requeued"

    # A requeued notification is offered again on a later poll; the fake queue
    # hands out each row once, so the next attempt is staged explicitly.
    pane.now += 901.0
    conn.queue_rows = [queue_row(2842, "SYRD-211", target_role="app")]
    listener.listen_once(max_notifications=1)

    assert pane.sent, "past the bound the handoff is delivered rather than retried again"
    escalations = [
        trace for trace in conn.traces
        if trace is not None and ORPHANED_PRIOR_TURN_WAITER in [str(item) for item in trace]
    ]
    assert escalations, f"and the wait is recorded, not silent: {conn.traces}"


def test_the_current_turns_work_is_never_bounded() -> None:
    """A clock may not interrupt the turn that is actually working.

    This is the half SYRD-101 was right about, and the bound must not reach it:
    however long a current turn takes, its own work keeps its pane.
    """
    pane = HeldPane("pane_child_work")
    conn = FakeConnection([queue_row(2843, "SYRD-211", target_role="app")])
    listener = pane.listener(conn, prior_turn_hold_max_seconds=900.0)

    listener.listen_once(max_notifications=1)
    pane.now += 86_400.0
    conn.queue_rows = [queue_row(2843, "SYRD-211", target_role="app")]
    listener.listen_once(max_notifications=1)

    assert pane.sent == [], f"a day of the current turn working still holds: {pane.sent}"


def test_a_handoff_the_owner_already_answered_is_discarded_not_delivered() -> None:
    """What actually happened next: App found SYRD-211 by itself.

    The original notification was still queued. Delivering it once the pane
    freed up would hand the same assignment over a second time, so it is dropped
    with the reason recorded.
    """
    pane = HeldPane(PRIOR_TURN_CHILD_WORK)

    class OwnerActed(FakeConnection):
        def execute(self, statement, params=None):
            text = statement if isinstance(statement, str) else str(statement)
            if "notification_trace" in text and "last_sent_at" in text:
                return _Row("2026-09-18T22:34:00+00:00")
            return super().execute(statement, params)

    class _Row:
        def __init__(self, value: str) -> None:
            self.value = value

        def fetchone(self):
            return (self.value,)

    conn = OwnerActed([queue_row(2842, "SYRD-211", target_role="app")])
    listener = pane.listener(conn, prior_turn_hold_max_seconds=900.0)

    listener.listen_once(max_notifications=1)
    pane.now += 901.0
    conn.queue_rows = [queue_row(2842, "SYRD-211", target_role="app")]
    listener.listen_once(max_notifications=1)

    assert pane.sent == [], f"the duplicate was not delivered: {pane.sent}"
    assert any(reason == OWNER_ALREADY_ACTED for _id, reason in conn.discarded), (
        f"it was discarded, saying why: {conn.discarded}"
    )


def main() -> int:
    # The listener dead-letters a notification whose tmux target is absent, long
    # before the activity gate is consulted. These cases are about the gate and
    # the bound, so the pane is asserted to exist, exactly as the listener's own
    # suite does.
    from scripts.ticket_board import notify_listener

    real_exists = notify_listener.tmux_target_exists
    notify_listener.tmux_target_exists = lambda _target: True  # type: ignore[assignment]
    try:
        run_module_tests(globals())
    finally:
        notify_listener.tmux_target_exists = real_exists  # type: ignore[assignment]
    print("ticket_board_orphaned_prior_turn_waiter_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
