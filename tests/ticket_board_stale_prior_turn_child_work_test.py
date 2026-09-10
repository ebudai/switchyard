#!/usr/bin/env python3
"""SYRD-101: a turn's leftovers must not hold the pane's notifications forever.

SYRD-58 made a turn whose verification is still running count as busy, and it
was right to. The evidence it used for the strongest case was that a descendant
sits in a session of its own, because that survives a listener restart and needs
no history. It also needs no turn: a detached descendant held delivery whatever
turn had started it and however long ago.

The live stall on 2026-09-10 is what that costs. App's pane hook had said idle
since 07:19, from `claude.Notification.idle_prompt`, and Director's capture at
09:29 showed the pane at an empty composer still displaying the previous turn's
completion at 07:18. The listener claimed the SYRD-99 transition 23 times
through 09:26 and requeued every one as pane-active. Underneath were two
detached zsh descendants from the earlier turn, zero CPU, more than two hours
each spent looping on `sleep 15` and `sleep 20` waiting for a word that was
never going to appear in a stale output file. Routing SYRD-101 itself reproduced
it: notification 1106 was deferred on `pane_child_work` against an idle pane
with one surviving polling shell.

So the classification needs the turn back in it. Not a timeout -- the ticket is
explicit, and a timeout is exactly what would interrupt an hour-long sweep. The
evidence used here is lifecycle: when the runtime said its turn ended, when each
descendant started, and whether any of them is making progress.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board.notify_listener import (
    STALE_PRIOR_TURN_CHILD_WORK,
    WORK_EVIDENCE_REASONS,
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
)
from standalone_test_runner import run_module_tests
from ticket_board_notify_listener_test import (
    FakeConnection,
    TemporaryStateDir,
    constant_cursor_runner,
    queue_row,
    targeted_capture_runner,
)

PANE_PID = 4100
PANE_SESSION = PANE_PID
RUNTIME_PID = 4200
#: What App's pane was showing: the previous turn's last line, and nothing since.
QUIET_PANE = "SYRD-97 revision 6 is with Audit.\n"
#: The hook source in the incident. A trusted idle: the runtime saying, itself,
#: that it is back at its prompt.
CLAUDE_IDLE = "claude.Notification.idle_prompt"


class Descendant:
    def __init__(self, pid: int, started_at: float, *, detached: bool, burning: bool) -> None:
        self.pid = pid
        self.started_at = started_at
        self.detached = detached
        self.burning = burning
        self.cpu = 0


class TurnProcessTree:
    """A pane tree the test drives across a turn boundary, with start times.

    Start time is the whole point: it is what separates a shell this turn
    launched from one the last turn left behind, and it comes from the kernel
    rather than from anything the runtime reports.
    """

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.runtime_cpu = 500
        self.living: dict[int, Descendant] = {}
        self.pending: list[Any] = []

    def start(self, pid: int, *, detached: bool = True, burning: bool = False) -> None:
        self.living[pid] = Descendant(pid, self.clock[0], detached=detached, burning=burning)

    def exit(self, pid: int) -> None:
        self.living.pop(pid, None)

    def burn(self, pid: int) -> None:
        """A child that had been sleeping starts doing real work again."""
        self.living[pid].burning = True

    def on_next_read(self, action: Any) -> None:
        """Run `action` after the next sample, so the tree changes mid-probe.

        The gate takes two samples a moment apart. What happens between them is
        exactly what this ticket's review found unhandled, and a fixture that
        can only change between probes cannot express it.
        """
        self.pending.append(action)

    def read(self) -> tuple[tuple[int, int, int, int, float], ...]:
        rows: list[tuple[int, int, int, int, float]] = [
            (PANE_PID, 1, 0, PANE_SESSION, self.clock[0] - 86_400.0),
            # The runtime is itself a descendant of the pane shell, and it
            # started long before any of this. It holds nothing on its own.
            (RUNTIME_PID, PANE_PID, self.runtime_cpu, PANE_SESSION, self.clock[0] - 86_400.0),
        ]
        for child in self.living.values():
            if child.burning:
                child.cpu += 3
            rows.append(
                (
                    child.pid,
                    RUNTIME_PID,
                    child.cpu,
                    child.pid if child.detached else PANE_SESSION,
                    child.started_at,
                )
            )
        if self.pending:
            self.pending.pop(0)()
        return tuple(rows)


def pane_pid_runner(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=f"{PANE_PID}\n")


def build_gate(state_dir: Path, target: str, tree: TurnProcessTree) -> PaneActivityGate:
    return PaneActivityGate(
        state_store=PaneHookStateStore(state_dir),
        cursor_position_runner=constant_cursor_runner(),
        capture_pane_runner=targeted_capture_runner(target, QUIET_PANE),
        pane_pid_runner=pane_pid_runner,
        process_table_reader=tree.read,
        sleeper=lambda _seconds: None,
    )


class Pane:
    """One role's pane, its clock, and the turn it is in."""

    def __init__(self, state_dir: Path) -> None:
        probe = PaneActivityGate(state_store=PaneHookStateStore(state_dir))
        # A role this configuration runs Claude for, because the incident's hook
        # source is Claude's idle prompt and a source from another runtime is
        # judged by a different probe entirely. The director is skipped: its
        # startup hold is its own case. On the live board the role was App; which
        # role runs which CLI is per-tenant, so it is read rather than named.
        self.role = next(
            role
            for role in sorted(probe.role_targets)
            if probe.role_runtimes.get(role) == "claude"
            and probe.role_targets[role] != probe.director_target
        )
        self.target = probe.role_targets[self.role]
        self.clock = [1_800_000_000.0]
        self.tree = TurnProcessTree(self.clock)
        self.gate = build_gate(state_dir, self.target, self.tree)
        self.state_dir = state_dir

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds

    def turn_ended(self, source: str = CLAUDE_IDLE) -> None:
        """The runtime says, itself, that it is back at its prompt."""
        self.gate.state_store.write(self.target, "idle", source=source, now=self.clock[0])

    def restart_listener_gate(self) -> PaneActivityGate:
        """A fresh gate with no memory of anything, as a restart has."""
        self.gate = build_gate(self.state_dir, self.target, self.tree)
        return self.gate

    def verdict(self) -> tuple[bool, str]:
        busy = self.gate.pre_send_busy(self.target)
        trace = self.gate.last_trace(self.target)
        return busy, "" if trace is None else trace.reason


def the_incident(pane: Pane) -> None:
    """The live shape: a finished turn, and two polling shells still sleeping."""
    pane.tree.start(3351141, detached=True, burning=False)
    pane.tree.start(3351479, detached=True, burning=False)
    pane.advance(60.0)
    pane.turn_ended()
    # Two hours of the listener trying, which is what the trace recorded.
    pane.advance(7200.0)


def test_the_polling_shells_a_finished_turn_left_behind_do_not_hold_the_pane() -> None:
    """The exact regression, from the runtime's own idle hook onwards."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)

        busy, reason = pane.verdict()
        assert busy is False, reason
        # Its own reason. Reading `pane_child_work` here is what told an operator
        # for two hours that the pane was working when it was at its prompt.
        assert reason == STALE_PRIOR_TURN_CHILD_WORK, reason
        assert reason != "pane_child_work"
        # And it is not work evidence, so nothing downstream can treat it as a
        # reason to void a reminder.
        assert reason not in WORK_EVIDENCE_REASONS, reason

        # Nothing was killed to reach that verdict. The ticket is about
        # classification, and the shells are still exactly where they were.
        assert set(pane.tree.living) == {3351141, 3351479}, pane.tree.living


def test_the_transition_that_was_trapped_is_delivered_once() -> None:
    """The whole point: notification 1093 reaches the pane, and only once."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)

        conn = FakeConnection(
            [
                queue_row(
                    1093,
                    "SYRD-99",
                    kind="transition",
                    state="in_progress",
                    assignee=pane.role,
                    target_role=pane.role,
                    message="SYRD-99 -- Drop queued Ops escalations entered Implementation",
                )
            ]
        )
        sent: list[tuple[str, str]] = []
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda destination, message: sent.append((destination, message)),
            activity_gate=pane.gate.is_working,
            connector=lambda *_args, **_kwargs: conn,
            poll_seconds=0,
            pre_send_recheck_delay_seconds=0,
            target_exists=lambda _target: True,
        )

        listener.listen_once(max_notifications=1)

        assert len(sent) == 1, sent
        assert sent[0][0] == pane.target, sent
        assert "SYRD-99" in sent[0][1], sent
        # Delivered and acknowledged, not requeued again.
        assert conn.acked == [1093], conn.acked
        assert conn.requeued == [], conn.requeued


def test_work_this_turn_started_still_holds_delivery() -> None:
    """The SYRD-58 guarantee, unchanged: a running turn is not interrupted."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.turn_ended()
        # A detached shell the turn started after saying it was at its prompt --
        # a tool call that outlives the message, which is the ordinary case.
        pane.advance(5.0)
        pane.tree.start(5000, detached=True, burning=False)
        pane.advance(30.0)

        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_a_long_task_from_before_the_idle_hook_is_not_interrupted() -> None:
    """A sweep that is still running is work, whatever turn started it.

    This is why the rule is progress and lifecycle rather than age. The ticket
    forbids an arbitrary timeout precisely so an hour-long verification run
    started before a turn-end hook keeps its pane quiet.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(6000, detached=True, burning=True)
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(7200.0)

        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_a_sibling_leaving_cannot_hide_a_survivor_still_working() -> None:
    """SYRD-101 review: a total is not monotonic, and that made work look idle.

    Between the gate's two samples one old detached child exits carrying 100
    accumulated ticks while a surviving old detached child advances by 3. The
    tree's CPU total falls, so a check on the total sees no progress at all and
    calls the pane reachable -- with a child under it that is genuinely working.
    Progress has to be counted per surviving process.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(3351141, detached=True, burning=False)
        pane.tree.start(3351479, detached=True, burning=False)
        # The one that will leave has real CPU behind it, the way a finished
        # verification run does.
        pane.tree.living[3351141].cpu = 100
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(600.0)

        # The survivor starts working again, and the sibling exits between the
        # two samples the gate takes.
        pane.tree.burn(3351479)
        pane.tree.on_next_read(lambda: pane.tree.exit(3351141))

        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason
        # And the survivor is still there, doing the work it was doing.
        assert set(pane.tree.living) == {3351479}, pane.tree.living


def test_a_sibling_leaving_while_nothing_works_is_still_reachable() -> None:
    """The other half: a shrinking tree of idle leftovers is not work.

    Counting per survivor must not turn every exit into evidence, or the fix
    would trade one deadlock for another.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(3351141, detached=True, burning=False)
        pane.tree.start(3351479, detached=True, burning=False)
        pane.tree.living[3351141].cpu = 100
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(600.0)

        pane.tree.on_next_read(lambda: pane.tree.exit(3351141))

        busy, reason = pane.verdict()
        assert busy is False, reason
        assert reason == STALE_PRIOR_TURN_CHILD_WORK, reason


def test_a_stale_child_that_goes_back_to_work_is_busy_again() -> None:
    """Sleeping now is not a verdict about later."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(6100, detached=True, burning=False)
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(600.0)

        assert pane.verdict() == (False, STALE_PRIOR_TURN_CHILD_WORK), pane.verdict()

        # The same pid, from the same old turn, now doing real work.
        pane.tree.burn(6100)
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_a_restart_with_the_leftovers_already_there_reaches_the_same_verdict() -> None:
    """A restarted listener has no memory, and must not need one.

    The detached signal was chosen in SYRD-58 because it survives a restart. The
    replacement has to survive one too, or a restart in the middle of the stall
    would have restored the deadlock.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)

        # Never probed before: no remembered tree, no arrivals, nothing.
        pane.restart_listener_gate()
        busy, reason = pane.verdict()
        assert busy is False, reason
        assert reason == STALE_PRIOR_TURN_CHILD_WORK, reason

        # And a restart in the middle of real work still holds delivery.
        pane.advance(5.0)
        pane.tree.start(7000, detached=True, burning=False)
        pane.restart_listener_gate()
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_replacing_the_leftovers_makes_the_pane_busy_again() -> None:
    """New work under the pane is new work, including at a reused pid."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        assert pane.verdict() == (False, STALE_PRIOR_TURN_CHILD_WORK), pane.verdict()

        # The old shells go and the next turn starts its own.
        pane.tree.exit(3351141)
        pane.tree.exit(3351479)
        pane.advance(30.0)
        pane.tree.start(8000, detached=True, burning=False)
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason

        # Pid reuse: the same number, a later start. It is the new process that
        # counts, not the number it was given.
        pane.tree.exit(8000)
        pane.advance(30.0)
        pane.tree.start(3351141, detached=True, burning=False)
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_a_descendant_whose_start_cannot_be_read_still_holds_delivery() -> None:
    """Unknown is not old. Guessing the other way would dismiss real work."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        assert pane.verdict() == (False, STALE_PRIOR_TURN_CHILD_WORK), pane.verdict()

        # A table without the start-time column at all, as an older reader gives.
        def without_start_times() -> tuple[tuple[int, int, int, int], ...]:
            return tuple(row[:4] for row in pane.tree.read())

        pane.gate.process_table_reader = without_start_times
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_an_untrusted_idle_hook_reconciles_nothing() -> None:
    """Only the runtime's own statement that its turn ended draws the line.

    Without a trusted idle there is no turn boundary to compare a start time
    against, so the leftovers keep their old meaning rather than being dismissed
    on the strength of a hook that did not come from the runtime.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.tree.start(9000, detached=True, burning=False)
        pane.advance(60.0)
        pane.turn_ended(source="something.else")
        pane.advance(600.0)

        # Through the pane's real verdict, not the probe on its own: the
        # boundary is decided by the caller, and asking the probe directly would
        # never exercise that decision.
        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "pane_child_work", reason


def test_an_ordinary_idle_pane_still_reports_plain_hook_idle() -> None:
    """The runtime is a descendant too, and it holds nothing.

    A pane sitting at its prompt with only its runtime under it predates its own
    idle hook by definition. Calling that "stale prior-turn child work" would put
    the reason on every idle pane on the board and make it mean nothing.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.advance(60.0)
        pane.turn_ended()
        pane.advance(600.0)

        busy, reason = pane.verdict()
        assert busy is False, reason
        assert reason == "hook_idle", reason


def test_a_leftover_in_the_pane_s_own_session_is_reconciled_too() -> None:
    """Detachment is one way a leftover holds a pane; arriving is the other.

    SYRD-58 holds delivery for a child it watched appear, for as long as it is
    there. That is right during a turn and wrong after one: a helper the last
    turn started and left sleeping in the pane's own session would keep the pane
    unreachable exactly as the detached pollers did.
    """
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        pane.turn_ended()
        # Looked at once first, so what follows is genuinely an arrival: a tree
        # that was already there the first time the gate looked is the runtime's
        # own furniture, not this turn's work (SYRD-58).
        assert pane.verdict() == (False, "hook_idle"), pane.verdict()

        # Watched arriving during the turn, in the pane's own session.
        pane.advance(5.0)
        pane.tree.start(9500, detached=False, burning=False)
        assert pane.verdict() == (True, "pane_child_work"), pane.verdict()

        # The turn ends again, after that helper was started. It is now a
        # leftover, and it is not progressing.
        pane.advance(30.0)
        pane.turn_ended()
        pane.advance(600.0)
        busy, reason = pane.verdict()
        assert busy is False, reason
        assert reason == STALE_PRIOR_TURN_CHILD_WORK, reason


def test_a_human_at_the_composer_still_wins() -> None:
    """Distinct from the other reasons, and not a licence to type over anyone."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        # A cursor away from the prompt's home column: somebody is typing.
        pane.gate.cursor_position_runner = constant_cursor_runner("5 23 24")

        busy, reason = pane.verdict()
        assert busy is True, reason
        assert reason == "human_composing", reason
        assert reason != STALE_PRIOR_TURN_CHILD_WORK


def main() -> int:
    from standalone_test_runner import module_test_functions

    tests = module_test_functions(globals())
    run_module_tests(globals())
    print(f"ticket_board_stale_prior_turn_child_work_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
