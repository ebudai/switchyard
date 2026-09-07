#!/usr/bin/env python3
"""SYRD-58: a turn whose verification is still running is not idle.

SYRD-57's notification trace caught the shape. At 20:38:39 a turn-end hook wrote
idle; eleven seconds later a reminder was minted with that timestamp, and seven
seconds after that it was delivered with decision reason `hook_idle`. The pane
was in an active turn the whole time, with four verification shells and a
monitor running a sweep that deliberately took minutes and printed nothing. Two
director escalations followed, about a minute apart, while the same work ran.

Every probe the gate had was blind to it: the hook said the runtime's own turn
was over, and the visible region did not change because the sweep was quiet. The
missing evidence was the pane's own process tree, and this is the case for it.

The cases are driven from the gate's configured roles rather than named ones, so
an implementer, an auditor, an inspector and a declaratively registered role are
all covered by construction.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board.notify_listener import (
    WORK_EVIDENCE_REASONS,
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
    descendant_work_sample,
    read_process_table,
)
from standalone_test_runner import run_module_tests
from ticket_board_notify_listener_test import (
    FakeConnection,
    TemporaryStateDir,
    constant_cursor_runner,
    queue_row,
    targeted_capture_runner,
)

#: The pane's own shell. Its descendants are what a turn runs.
PANE_PID = 4100
#: A quiet screen: the sweep prints nothing for minutes.
QUIET_PANE = "the last thing this turn printed\n"
#: One turn-end source per runtime, so no case depends on which CLI a role runs.
TURN_END_SOURCE_BY_RUNTIME = {
    "codex": "codex.Stop",
    "claude": "claude.Stop",
    "gemini": "gemini.Stop",
}


class ProcessTree:
    """A pane process tree the test moves through a turn and out the other side."""

    def __init__(self) -> None:
        self.runtime_cpu = 500
        self.child_cpu = 100
        self.children: list[int] = []

    def working(self) -> None:
        """A turn with verification running: shells that keep using CPU."""
        self.children = [4300, 4301, 4302, 4303, 4304]

    def finished(self) -> None:
        """The turn is over: the runtime is at a prompt with nothing under it."""
        self.children = []

    def read(self) -> tuple[tuple[int, int, int], ...]:
        # Each read advances the running children's CPU, and only theirs, the
        # way a sweep does between two samples a fraction of a second apart.
        rows = [(PANE_PID, 1, 0), (4200, PANE_PID, self.runtime_cpu)]
        for pid in self.children:
            self.child_cpu += 3
            rows.append((pid, 4200, self.child_cpu))
        return tuple(rows)


def pane_pid_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=f"{PANE_PID}\n")


def build_gate(state_dir: Path, target: str, tree: ProcessTree) -> PaneActivityGate:
    return PaneActivityGate(
        state_store=PaneHookStateStore(state_dir),
        cursor_position_runner=constant_cursor_runner(),
        capture_pane_runner=targeted_capture_runner(target, QUIET_PANE),
        pane_pid_runner=pane_pid_runner,
        process_table_reader=tree.read,
        sleeper=lambda _seconds: None,
    )


def roles_under_test(gate: PaneActivityGate) -> list[str]:
    """An implementer, an auditor, an inspector and the rest, from configuration."""
    return sorted(gate.role_targets)


def test_the_process_tree_sample_is_the_pane_s_descendants() -> None:
    table = ((10, 1, 0), (20, 10, 7), (30, 20, 11), (40, 1, 99))
    sample = descendant_work_sample(10, table)
    assert sample.observed
    assert sample.pids == frozenset({20, 30}), sample.pids
    assert sample.cpu_ticks == 18, sample.cpu_ticks
    # A pane with nothing under it is genuinely idle, not unobservable.
    empty = descendant_work_sample(40, table)
    assert empty.observed and empty.pids == frozenset(), empty


def test_the_real_process_table_parses() -> None:
    """The parser runs against this host's own /proc, not a fixture of it."""
    table = read_process_table()
    assert table, "no processes were readable"
    pids = {pid for pid, _ppid, _cpu in table}
    import os

    assert os.getpid() in pids, "this process is missing from its own table"
    for _pid, _ppid, cpu_ticks in table:
        assert cpu_ticks >= 0


def test_a_running_turn_is_busy_for_every_configured_role() -> None:
    """Transient hook idle, unchanged screen, work still running underneath."""
    with TemporaryStateDir() as state_dir:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        for role in roles_under_test(probe):
            target = probe.role_targets[role]
            tree = ProcessTree()
            tree.working()
            gate = build_gate(state_dir, target, tree)
            source = TURN_END_SOURCE_BY_RUNTIME.get(gate.role_runtimes.get(role, ""), "codex.Stop")
            gate.state_store.write(target, "idle", source=source, now=time.time())

            assert gate.pre_send_busy(target) is True, role
            trace = gate.last_trace(target)
            assert trace is not None and trace.reason == "pane_child_work", (role, trace)
            assert trace.reason in WORK_EVIDENCE_REASONS, trace
            # And no reminder is minted for it in the first place.
            assert gate.idle_since_by_role([role]) == {}, role
            assert gate.turn_end_idle_since_by_role([role]) == {}, role


def test_the_turn_ending_releases_one_reminder_dated_from_the_work() -> None:
    """The clock starts when the work stopped, not when the hook said idle."""
    with TemporaryStateDir() as state_dir:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        for role in roles_under_test(probe):
            target = probe.role_targets[role]
            tree = ProcessTree()
            tree.working()
            clock = [1_800_000_000.0]
            gate = build_gate(state_dir, target, tree)
            gate.wall_time = lambda: clock[0]
            hook_idle_at = clock[0]
            source = TURN_END_SOURCE_BY_RUNTIME.get(gate.role_runtimes.get(role, ""), "codex.Stop")
            gate.state_store.write(target, "idle", source=source, now=hook_idle_at)

            assert gate.idle_since_by_role([role]) == {}, role
            # The sweep runs for five minutes with the same hook state.
            clock[0] = hook_idle_at + 300.0
            assert gate.idle_since_by_role([role]) == {}, role

            # The turn ends. One reminder becomes eligible, dated from the last
            # observed work rather than from the hook five minutes earlier.
            tree.finished()
            clock[0] = hook_idle_at + 301.0
            eligible = gate.idle_since_by_role([role])
            assert list(eligible) == [role], (role, eligible)
            from datetime import datetime, timezone

            reported = datetime.fromisoformat(eligible[role]).timestamp()
            assert reported == hook_idle_at + 300.0, (role, reported - hook_idle_at)


def test_a_pane_with_no_descendants_is_reachable() -> None:
    """Durable retries for a genuinely idle pane are not what this suppresses."""
    with TemporaryStateDir() as state_dir:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        role = roles_under_test(probe)[0]
        target = probe.role_targets[role]
        tree = ProcessTree()
        tree.finished()
        gate = build_gate(state_dir, target, tree)
        gate.state_store.write(target, "idle", source="codex.Stop", now=1_800_000_000.0)

        assert gate.pre_send_busy(target) is False
        trace = gate.last_trace(target)
        assert trace is not None and trace.reason == "hook_idle", trace
        assert gate.idle_since_by_role([role]) == {
            role: "2027-01-15T08:00:00+00:00"
        }, gate.idle_since_by_role([role])


def test_an_unreadable_process_tree_does_not_change_the_verdict() -> None:
    """No pane pid, or no table: the probe declines rather than guessing."""
    with TemporaryStateDir() as state_dir:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        role = roles_under_test(probe)[0]
        target = probe.role_targets[role]
        tree = ProcessTree()
        tree.working()
        gate = build_gate(state_dir, target, tree)
        gate.state_store.write(target, "idle", source="codex.Stop", now=1_800_000_000.0)

        gate.pane_pid_runner = lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="not a pid\n")
        assert gate.child_work_trace(target) is None
        gate.pane_pid_runner = pane_pid_runner
        gate.process_table_reader = lambda: ()
        assert gate.child_work_trace(target) is None

        def refuses() -> Any:
            raise OSError("proc is unreadable")

        gate.process_table_reader = refuses
        assert gate.child_work_trace(target) is None


def test_a_queued_reminder_is_held_and_no_escalation_is_earned() -> None:
    """The delivery half: the reminder waits, and its counter does not advance."""
    with TemporaryStateDir() as state_dir:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        role = "ops" if "ops" in probe.role_targets else roles_under_test(probe)[0]
        target = probe.role_targets[role]
        tree = ProcessTree()
        tree.working()
        gate = build_gate(state_dir, target, tree)
        gate.state_store.write(target, "idle", source="codex.Stop", now=time.time())

        conn = FakeConnection(
            [
                queue_row(
                    581,
                    "SYRD-57",
                    kind="idle_reminder",
                    state="in_progress",
                    assignee=role,
                    target_role=role,
                    message=f"{role} appears idle on SYRD-57.",
                )
            ]
        )
        sent: list[tuple[str, str]] = []
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda destination, message: sent.append((destination, message)),
            activity_gate=gate.is_working,
            connector=lambda *_args, **_kwargs: conn,
            poll_seconds=0,
            pre_send_recheck_delay_seconds=0,
            # The pane exists as far as this case is concerned; whether a tmux
            # session is present on the host running the suite is not what is
            # under test, and asking would consult it.
            target_exists=lambda _target: True,
        )

        listener.listen_once(max_notifications=1)

        assert sent == [], sent
        # Voided, not acked. Its whole claim is "you appear idle on this
        # ticket", and the pane was working, so the claim is false rather than
        # early. Crucially it is a discard: an ack is what increments
        # idle_reminder_count, and that count is what mints the escalation to
        # the director. No ack, no escalation earned.
        assert conn.acked == [], conn.acked
        assert conn.discarded == [(581, "stale_reminder_work_observed")], conn.discarded
        dropped = [
            (str(params[5]), str(params[6]))
            for params in conn.traces
            if params is not None and str(params[4]) == "drop"
        ]
        assert dropped == [("busy", "stale_reminder_work_observed")], dropped
        # And the evidence that made it busy was the process tree, not the
        # screen and not the hook, both of which said idle.
        trace = gate.last_trace(target)
        assert trace is not None and trace.reason == "pane_child_work", trace

        # And the generator is told work was observed, so the stall side agrees.
        listener.process_idle_stall_nudges(conn)
        assert conn.idle_stall_calls, conn.idle_stall_calls
        params = conn.idle_stall_calls[-1]
        assert params is not None
        assert json.loads(str(params[0])) == {}, params[0]
        assert role in json.loads(str(params[-1])), params[-1]


def main() -> int:
    run_module_tests(globals())
    print("ticket_board_active_turn_suppression_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
