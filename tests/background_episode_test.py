#!/usr/bin/env python3
"""SYRD-704: one Director alert per background episode after a delivery goes past it, and only for a real one.

Live syrd, 2026-10-29: notice 8160 for SYRD-703 went out to App past a finished
turn's waiter (process group 3030056, 28 min old), and the Director was told
about it in notice 8162. Then came eight more notices, 8163 to 8170, one for each
PostgreSQL process (`postgres: io worker 0`, `checkpointer`, ...) that a test
under that waiter had started three seconds earlier. PostgreSQL puts each child
in a session of its own, so each was a holder of its own. All of them had exited
before the Director could look.

Everything runs against a fake /proc tree built in a temporary directory, shaped
as the live one was: the pane's shell, the provider, the tool's shell in a
session of its own, and what runs under it. The listener and its activity hold
are the real ones; only the database is a stand-in that answers what this path
asks. No test sleeps: ages are start times written into the tree.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board import background_child, background_episode as be  # noqa: E402
from scripts.ticket_board import execution_lease as lease  # noqa: E402
from scripts.ticket_board import notification_activity_hold as ah  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402
from ticket_board_notify_listener_test import FakeConnection, FakeResult  # noqa: E402

CHECKS = 0
PANE, PROVIDER = 100, 101
WAITER = 3030056
POSTMASTER = 3345500
WORKERS = tuple(range(3345525, 3345533))
WORKER_NAMES = ("io worker 0", "io worker 1", "io worker 2", "checkpointer", "background writer", "walwriter",
                "autovacuum launcher", "logical replication launcher")


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Proc:
    """A fake /proc: `stat` with a boot time, and per process `stat`, `cmdline` and `cwd`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hz = float(os.sysconf("SC_CLK_TCK"))
        self.boot = float(int(time.time()) - 100_000)
        root.mkdir(parents=True, exist_ok=True)
        (root / "stat").write_text(f"cpu  1 2 3 4\nbtime {int(self.boot)}\n")
        self.sessions: dict[int, int] = {}

    def add(self, pid: int, *, ppid: int, session: int, pgid: int | None = None, age: float, argv: list[str],
            cwd: str = "/tmp", state: str = "S") -> None:
        directory = self.root / str(pid)
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir()
        ticks = round((time.time() - age - self.boot) * self.hz)
        fields = [state, ppid, pgid or session, session, 0, -1, 0, 0, 0, 0, 0, 5, 5, 0, 0, 20, 0, 1, 0, ticks]
        (directory / "stat").write_text(f"{pid} ({Path(argv[0]).name[:15]}) " + " ".join(map(str, fields + [0] * 10)))
        (directory / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
        os.symlink(cwd, directory / "cwd")
        self.sessions[pid] = session

    def remove(self, pid: int) -> None:
        shutil.rmtree(self.root / str(pid), ignore_errors=True)

    def started(self, pid: int) -> float:
        return be.read(self.root, pid, be.Clock(self.root)).started_at

    def holder(self, pid: int, *, finished_turn: bool = False) -> background_child.Holder:
        """What the activity gate's `describe` makes of a session's leader."""
        process = be.read(self.root, pid, be.Clock(self.root))
        argv = background_child._argv(self.root, pid)
        return background_child.Holder(
            session=process.session, pgid=process.pgid, pid=pid, started_at=process.started_at,
            command=" ".join(argv), cwd=background_child._cwd(self.root, pid), finished_turn=finished_turn,
            detached=process.session != PANE, argv=argv)


def pane(proc: Proc) -> None:
    proc.add(PANE, ppid=1, session=PANE, age=90_000, argv=["/usr/bin/zsh"])
    proc.add(PROVIDER, ppid=PANE, session=PANE, pgid=PROVIDER, age=89_000, argv=["claude"])


def live_tree(proc: Proc) -> None:
    """SYRD-703's: a finished turn's waiter, a test under it, and PostgreSQL's children in sessions of their own."""
    pane(proc)
    proc.add(WAITER, ppid=PROVIDER, session=WAITER, age=1736,
             argv=["/usr/sbin/zsh", "-c", "while pgrep -f sweep.sh >/dev/null; do sleep 20; done"])
    proc.add(WAITER + 40, ppid=WAITER, session=WAITER, age=4, argv=["python3", "tests/board_identity_test.py"])
    proc.add(POSTMASTER, ppid=WAITER + 40, session=WAITER, age=3,
             argv=["postgres", "-D", "/tmp/syrd113sb-sroqjpc5/pgdata"], cwd="/tmp/syrd113sb-sroqjpc5/pgdata")
    for pid, name in zip(WORKERS, WORKER_NAMES):
        proc.add(pid, ppid=POSTMASTER, session=pid, age=3, argv=[f"postgres: {name}"],
                 cwd="/tmp/syrd113sb-sroqjpc5/pgdata")


class Conn(FakeConnection):
    """The listener's connection, answering what SYRD-583's record and SYRD-616's leases are asked."""

    def __init__(self, *, deadlines: dict[str, float] | None = None, leases: bool = True,
                 tickets: dict[str, tuple[str, str, str]] | None = None) -> None:
        super().__init__()
        self.deadlines = deadlines or {}
        #: child key -> (ticket, its state, its assignee): the ticket a lease was recorded for (SYRD-616).
        self.tickets = tickets or {}
        self.leases = leases
        self.noted: list[tuple[int, str, str]] = []
        self.alerted: set[str] = set()

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
        text = str(statement)
        if "to_regprocedure('ticket_board.note_background_child" in text:
            return FakeResult([(True,)])
        if "ticket_board.note_background_child(" in text:
            notification_id, child, what = params
            self.noted.append((notification_id, child, what))
            new = child not in self.alerted
            self.alerted.add(child)
            return FakeResult([(new,)])
        if "to_regclass('ticket_board.execution_leases')" in text:
            return FakeResult([(self.leases,)])
        if "FROM ticket_board.execution_leases" in text and "deadline" in text:
            return Rows([(self.deadlines[key],) for key in params[0] if key in self.deadlines])
        if "SELECT child, ticket_id FROM ticket_board.execution_leases" in text:
            return Rows([(key, ticket) for key, (ticket, _s, _a) in self.tickets.items() if key in params[0]])
        if "FROM ticket_board.execution_leases" in text:
            return Rows([])
        if "SELECT id, state, assignee FROM ticket_board.tickets" in text:
            return Rows(sorted({row for row in self.tickets.values() if row[0] in params[0]}))
        return super().execute(statement, params)


class Rows(FakeResult):
    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Gate:
    def __init__(self, proc: Proc, trace: ah.ActivityTrace, state_dir: Path | None = None, pane_pid: int | None = PANE):
        self.proc_root = str(proc.root)
        self.trace = trace
        self.role_targets = {"app": "syrd-app:0.0"}
        self.role_identities = {"app": SimpleNamespace(pid=PROVIDER)}
        self.state_store = SimpleNamespace(state_dir=str(state_dir)) if state_dir else None
        self.pane = pane_pid

    def is_working(self, _target: str) -> bool:
        return self.trace.busy

    def last_trace(self, _target: str) -> ah.ActivityTrace:
        return self.trace

    def _pane_pid(self, _target: str) -> int | None:
        return self.pane


def deliver_past(proc: Proc, holders: list[background_child.Holder], conn: Conn, *,
                 state_dir: Path | None = None, pane_pid: int | None = PANE, notification_id: int = 8160,
                 lapsed: bool = False) -> str:
    """Hold notice `notification_id` behind a finished turn's waiter for the whole bound, then let it out."""
    clock = [0.0]
    trace = ah.ChildHoldTrace(True, ah.ORPHANED_PRIOR_TURN_WAITER, "", tuple(holders))
    gate = Gate(proc, trace, state_dir, pane_pid)
    listener = nl.TicketBoardNotifyListener(conninfo="offline", activity_gate=gate.is_working,
                                            prior_turn_hold_max_seconds=900.0, monotonic=lambda: clock[0])

    def release() -> str:
        return listener.activity_hold._release_prior_turn_hold(
            conn, notification_id=notification_id, ticket_id="SYRD-703", target_role="app", kind="ticket_update",
            activity_trace=trace)

    if lapsed:  # SYRD-583: a lapsed lease holds nothing, so the notice goes out at once
        return release()
    check(release() == "", "the notice waits behind the waiter until the bound")
    clock[0] = 900.0
    return release()


def test_the_live_episode_is_told_once(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    live_tree(proc)
    holders = [proc.holder(WAITER, finished_turn=True), *(proc.holder(pid) for pid in WORKERS)]
    conn = Conn()
    check(deliver_past(proc, holders, conn) == ah.PRIOR_TURN_HOLD_DELIVER, "SYRD-703's notice is delivered")
    keys = [child for _n, child, _w in conn.noted]
    check(keys == [f"{WAITER}@{int(proc.started(WAITER))}"],
          f"one alert, for the waiter's process group, not one per PostgreSQL process: {keys}")
    what = conn.noted[0][2]
    check(f"pid {WAITER} (process group {WAITER}), running for 28 min" in what, what)
    check("`zsh -c" in what and "left running by a finished turn" in what, f"the representative command: {what}")
    check(f"in process groups of their own: {WORKERS[0]} (`" in what, what)
    check("and 3 more" in what and f"{WORKERS[4]} (`" in what and f"{WORKERS[5]} (`" not in what,
          f"five named, the rest counted: {what}")
    check(f"ps -o pid,pgid,lstart,args -g {','.join(map(str, WORKERS))}" in what,
          f"every other group can be checked in one command: {what}")
    check(f"kill -TERM -- -{WAITER}" in what and "never the provider (pid 101)" in what, what)
    escalate = [trace for trace in conn.traces if trace and "gate_escalate" in trace]
    check(len(escalate) == 1, f"the release is recorded once: {conn.traces}")
    detail = json.loads(next(part for part in escalate[0] if isinstance(part, str) and '"holders"' in part))
    check(len(detail["holders"]) == 1 + len(WORKERS), "the record still lists every holder")

    # The next delivery past the same work tells nobody again, whichever of it holds then.
    later = Conn()
    later.alerted = set(conn.alerted)
    check(deliver_past(proc, [proc.holder(pid) for pid in WORKERS[:2]], later, notification_id=8171)
          == ah.PRIOR_TURN_HOLD_DELIVER, "the next notice is delivered")
    check([child for _n, child, _w in later.noted] == keys,
          f"PostgreSQL's processes alone still name the waiter's episode, already told: {later.noted}")


def test_short_siblings_tell_nothing(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    proc.add(5000, ppid=PROVIDER, session=5000, age=5, argv=["/usr/sbin/zsh", "-c", "python3 tests/x_test.py"])
    proc.add(5001, ppid=5000, session=5001, age=3, argv=["postgres: io worker 0"])
    proc.add(5002, ppid=5000, session=5002, age=3, argv=["postgres: io worker 1"])
    holders = [proc.holder(5001), proc.holder(5002)]

    # Exited between the sample and the release.
    proc.remove(5001)
    proc.remove(5002)
    conn = Conn()
    check(deliver_past(proc, holders, conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered past siblings that exited")
    check(conn.noted == [], f"siblings that exited are not told: {conn.noted}")

    # Still there, but three seconds old under a five-second command: nothing material.
    proc.add(5001, ppid=5000, session=5001, age=3, argv=["postgres: io worker 0"])
    proc.add(5002, ppid=5000, session=5002, age=3, argv=["postgres: io worker 1"])
    holders = [proc.holder(5001), proc.holder(5002)]
    conn = Conn()
    check(deliver_past(proc, holders, conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered past short siblings")
    check(conn.noted == [], f"a short command is not told because delivery went past it: {conn.noted}")

    # The same command, a minute on, is: once, under its shell.
    proc.add(5000, ppid=PROVIDER, session=5000, age=61, argv=["/usr/sbin/zsh", "-c", "python3 tests/x_test.py"])
    conn = Conn()
    check(deliver_past(proc, holders, conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered")
    check([child for _n, child, _w in conn.noted] == [f"5000@{int(proc.started(5000))}"],
          f"a command past SYRD-616's minute is told once, by its shell: {conn.noted}")


def test_identity_is_rechecked_before_recording(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    proc.add(6000, ppid=PROVIDER, session=6000, age=600, argv=["sleep", "3600"])
    proc.add(6100, ppid=PROVIDER, session=6100, age=600, argv=["sleep", "3600"])
    holders = [proc.holder(6000), proc.holder(6100)]
    proc.add(6000, ppid=PROVIDER, session=6000, age=1, argv=["vim"])  # the number reused
    proc.add(6100, ppid=PROVIDER, session=6100, age=600, argv=["sleep", "3600"], state="Z")  # exited, unreaped
    conn = Conn()
    check(deliver_past(proc, holders, conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered")
    check(conn.noted == [], f"a reused number and a zombie are not the processes sampled: {conn.noted}")

    # The same process, since moved to a group of its own: the group sampled has gone.
    proc.add(6200, ppid=PROVIDER, session=6200, age=600, argv=["sleep", "3600"])
    holder = proc.holder(6200)
    proc.add(6200, ppid=PROVIDER, session=6200, pgid=6201, age=600, argv=["sleep", "3600"])
    conn = Conn()
    deliver_past(proc, [holder], conn)
    check(conn.noted == [], f"a process now in another group is not the group sampled: {conn.noted}")


def test_an_unknown_start_is_not_a_short_one(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    proc.add(6300, ppid=PROVIDER, session=6300, age=5, argv=["sleep", "3600"])
    (proc.root / "stat").write_text("cpu  1 2 3 4\n")  # no boot time: no start time can be known
    holder = proc.holder(6300)
    check(holder.started_at == 0.0, f"the gate's start is unknown too: {holder}")
    conn = Conn()
    deliver_past(proc, [holder], conn)
    check([child for _n, child, _w in conn.noted] == ["6300@0"],
          f"work whose age cannot be read is told, not taken for short: {conn.noted}")


def test_distinct_groups_stay_distinct(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    for pid, vm in ((7000, "syrd627"), (8000, "syrd629")):
        proc.add(pid, ppid=PROVIDER, session=pid, age=7200, argv=["bash", "-c", f"./resume-vm.sh {vm} 8 16384"])
        proc.add(pid + 1, ppid=pid, session=pid, age=7200, argv=["qemu-system-x86_64", "-drive", f"file={vm}.qcow2"])
    conn = Conn()
    check(deliver_past(proc, [proc.holder(7000), proc.holder(8000)], conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered")
    keys = sorted(child for _n, child, _w in conn.noted)
    check(keys == sorted(f"{pid}@{int(proc.started(pid))}" for pid in (7000, 8000)),
          f"two commands the provider started are two alerts: {keys}")


def test_overdue_work_is_told_at_once(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    proc.add(9000, ppid=PROVIDER, session=9000, age=20, argv=["switchyard-bounded", "--deadline", "10s", "make"])
    holder = proc.holder(9000)

    conn = Conn(deadlines={holder.key: time.time() - 10})
    check(deliver_past(proc, [holder], conn) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered")
    check([child for _n, child, _w in conn.noted] == [holder.key],
          f"a command past its lease's deadline is told although it is young: {conn.noted}")

    conn = Conn(deadlines={holder.key: time.time() + 600})
    deliver_past(proc, [holder], conn)
    check(conn.noted == [], f"one inside its deadline is not: {conn.noted}")

    state = tmp / "pane-state"
    directory = lease.lease_dir(state)
    ticks = lease.start_ticks(9000, proc.root)
    lease.declare(directory, 9000, ticks, deadline=time.time() - 5, hard=True, command="make")
    conn = Conn(leases=False)
    deliver_past(proc, [holder], conn, state_dir=state)
    check([child for _n, child, _w in conn.noted] == [holder.key],
          f"a declaration whose deadline passed, not yet recorded as a lease, is told at once: {conn.noted}")
    lease.declare(directory, 9000, ticks, deadline=time.time() + 600, hard=True, command="make")
    conn = Conn(leases=False)
    deliver_past(proc, [holder], conn, state_dir=state)
    check(conn.noted == [], f"a declaration still ahead is not: {conn.noted}")


def test_a_finished_tickets_or_endless_work_is_told_at_once(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    pane(proc)
    proc.add(9100, ppid=PROVIDER, session=9100, age=11, argv=["bash", "-c", "./sweep.sh"])
    holder = proc.holder(9100, finished_turn=True)

    conn = Conn(tickets={holder.key: ("SYRD-579", "done", "app")})
    check(deliver_past(proc, [holder], conn, lapsed=True) == ah.PRIOR_TURN_HOLD_DELIVER,
          "a notice goes out past a done ticket's child at once")
    check([child for _n, child, _w in conn.noted] == [holder.key] and "started for SYRD-579, which is now done"
          in conn.noted[0][2], f"and its child is told although it is young, as SYRD-583 tells it: {conn.noted}")

    conn = Conn(tickets={holder.key: ("SYRD-579", "in_progress", "app")})
    deliver_past(proc, [holder], conn)
    check(conn.noted == [], f"work for a ticket still the role's is held to the minute: {conn.noted}")

    proc.add(9200, ppid=PROVIDER, session=9200, age=11,
             argv=["bash", "-c", "while pgrep -f sweep.sh >/dev/null; do sleep 20; done"])
    conn = Conn()
    deliver_past(proc, [proc.holder(9200, finished_turn=True)], conn)
    check([child for _n, child, _w in conn.noted] == [proc.holder(9200).key],
          f"a wait that finds itself can never end, so it is told at any age: {conn.noted}")


def test_without_the_pane_each_holder_is_its_own(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    live_tree(proc)
    holders = [proc.holder(WAITER, finished_turn=True), *(proc.holder(pid) for pid in WORKERS)]
    conn = Conn()
    check(deliver_past(proc, holders, conn, pane_pid=None) == ah.PRIOR_TURN_HOLD_DELIVER, "delivered")
    check([child for _n, child, _w in conn.noted] == [f"{WAITER}@{int(proc.started(WAITER))}"],
          f"with no pane to stop at, the short ones still are not told: {conn.noted}")


def test_a_failure_never_fails_the_delivery(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    live_tree(proc)

    class Broken(Conn):
        def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> FakeResult:
            if "note_background_child(" in str(statement) and "to_regprocedure" not in str(statement):
                raise RuntimeError("board went away")
            return super().execute(statement, params)

    check(deliver_past(proc, [proc.holder(WAITER, finished_turn=True)], Broken()) == ah.PRIOR_TURN_HOLD_DELIVER,
          "a record that fails leaves the delivery as it was")

    def broken(*_args: Any, **_kwargs: Any) -> list:
        raise RuntimeError("/proc went away")

    saved = be.episodes
    be.episodes = broken
    try:
        try:
            result = deliver_past(proc, [proc.holder(WAITER, finished_turn=True)], Conn())
        except Exception as exc:  # the defect this case exists to catch
            result = f"raised {exc!r}"
    finally:
        be.episodes = saved
    check(result == ah.PRIOR_TURN_HOLD_DELIVER, f"so does any failure in telling the Director: {result}")


def test_a_queued_alert_is_dropped_when_its_group_has_gone(tmp: Path) -> None:
    proc = Proc(tmp / "proc")
    live_tree(proc)
    started = int(proc.started(WAITER))

    def current(child: str) -> bool:
        return be.alert_is_current({"kind": "background_child", "child": child}, proc_root=proc.root)

    check(current(f"{WAITER}@{started}"), "a live leader that started when the alert says is current")
    check(current("garbage") and current(""), "an alert this cannot read is left to deliver")
    check(not current(f"{WORKERS[0]}@{started}"), "the same number, another start: not the one reported")
    proc.remove(WORKERS[1])
    check(not current(f"{WORKERS[1]}@{int(proc.started(WORKERS[0]))}"), "an exited group is not current")
    proc.add(WORKERS[2], ppid=POSTMASTER, session=WORKERS[2], age=3, argv=["x"], state="Z")
    check(not current(f"{WORKERS[2]}@{int(proc.started(WORKERS[2]))}"), "a zombie alone is not")
    # A holder that is not its group's leader, as one in the pane's own session is: the group's number names
    # another process, and the alert's start is its own.
    proc.add(120, ppid=PROVIDER, session=PANE, pgid=PROVIDER, age=700, argv=["node", "mcp-server.js"])
    member = f"{PROVIDER}@{int(proc.started(120))}"
    check(current(member), "a live member that started when the alert says is current, whoever leads its group")
    proc.remove(120)
    check(not current(member), "and once it has gone, its group's leader does not stand in for it")
    # The leader exits; a member of its group lives on, and the group's number cannot be reused while it does.
    proc.remove(WAITER)
    check(current(f"{WAITER}@{started}"), "a group outliving its leader is still there")
    proc.remove(WAITER + 40)
    proc.remove(POSTMASTER)
    check(not current(f"{WAITER}@{started}"), "once its last member has gone, it is not")
    # The number now leads a process in another group: the group reported has gone.
    proc.add(WAITER, ppid=PROVIDER, session=PANE, pgid=PROVIDER, age=1, argv=["git", "status"])
    check(not current(f"{WAITER}@{started}"), "a reused number in another group is not")

    eligibility = nl.TicketBoardNotifyListener(conninfo="offline").eligibility
    eligibility._current_ticket_state = lambda _conn, _ticket: ("in_progress", "app", False, False, False)
    saved = be.PROC_ROOT
    try:
        be.PROC_ROOT = proc.root
        gone = json.dumps({"kind": "background_child", "id": "SYRD-703", "target_role": "director", "role": "app",
                           "child": f"{WAITER}@{started}"})
        alive = json.dumps({"kind": "background_child", "id": "SYRD-703", "target_role": "director", "role": "app",
                            "child": f"{WORKERS[0]}@{int(proc.started(WORKERS[0]))}"})
        check(not eligibility._notification_is_current(Conn(), "SYRD-703", "director", gone),
              "the listener drops a queued alert whose group has gone")
        check(eligibility._notification_is_current(Conn(), "SYRD-703", "director", alive),
              "and delivers one whose group is still there")
        check(not eligibility._notification_is_current(Conn(), "SYRD-703", "app", alive),
              "only ever to the Director")
        conn = Conn()
        check(eligibility.drop_if_stale(conn, notification_id=8164, ticket_id="SYRD-703", target_role="director",
                                        kind="background_child", payload=gone, phase="pre_send_recheck"),
              "a stale alert is dropped at the recheck before its send")
        check(8164 in conn.acked, f"and acknowledged, as any stale notice is: {conn.acked}")
    finally:
        be.PROC_ROOT = saved


def main() -> int:
    tests = [test_the_live_episode_is_told_once, test_short_siblings_tell_nothing,
             test_identity_is_rechecked_before_recording, test_an_unknown_start_is_not_a_short_one,
             test_distinct_groups_stay_distinct,
             test_overdue_work_is_told_at_once, test_a_finished_tickets_or_endless_work_is_told_at_once,
             test_without_the_pane_each_holder_is_its_own,
             test_a_failure_never_fails_the_delivery, test_a_queued_alert_is_dropped_when_its_group_has_gone]
    for test in tests:
        with tempfile.TemporaryDirectory(prefix="s704-") as tmp:
            test(Path(tmp))
    print(f"background_episode_test: {len(tests)} cases, {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
