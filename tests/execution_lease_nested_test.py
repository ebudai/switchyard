#!/usr/bin/env python3
"""SYRD-840: one bounded command is one lease, and an exited child holds none.

sapi, 2026-11-22 (SAPI-128, release 2cab070): ops ran ONE command in its pane,
`switchyard-bounded --deadline 50m --expect 40m -- python3 -u measure-baseline.py`,
which ran scripts/check three times, about 40 minutes in all. The board opened a
lease for every session the run started, 200 of them (7182 to 7385), each on
the default 15-minute threshold, many with an empty command. The first two
alerts named exited git children of the wrapper, pids 3831914 and 3831933, ppid
3828235 = switchyard-bounded. The wrapper and the worker were healthy. The
Director extended all 200 by hand.

Two things were wrong:
- the lease pass gave each detached session under the pane its own lease, even
  one started from inside another leased session: a test stage's
  `start_new_session`, git's detached maintenance, an orphan the wrapper adopted;
- the wrapper is the subreaper of its command's orphans, but it reaped them only
  when the command ended, so each one that exited waited as a zombie under the
  pane, and a zombie's session counted as a running command.

Here, real processes, the real /proc, the real `switchyard-bounded` and real
signals, with the board's few calls stood in, as tests/execution_lease_interrupt_test.py
does. The pane is this suite's own process: what it starts in a session of its
own is a provider tool's command.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import execution_lease, execution_lease_pass as lp, notify_listener as nl  # noqa: E402
from scripts.ticket_board.pane_activity_gate import read_process_table  # noqa: E402

WRAPPER = ROOT / "scripts" / "switchyard-bounded"
PROC = Path("/proc")
CHECKS = 0

#: The bounded command: a stage in a session of its own (still its child), a detached orphan the wrapper adopts, and
#: four detached orphans that exit at once, as git's maintenance does. It says it is ready in the file it is given.
COMMAND = r"""
import os, subprocess, sys, time
stage = subprocess.Popen(["sleep", "840.2"], start_new_session=True)
def orphan(argv):
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            if argv:
                os.execvp(argv[0], argv)
            os._exit(0)
        os._exit(0)
    os.wait()
orphan(["sleep", "840.1"])
for _ in range(4):
    orphan(None)
open(sys.argv[1], "w").write(str(stage.pid))
time.sleep(840)
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


class Result:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.description = rows, None

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Board:
    """The board's side of a lease pass: the open leases, what is observed, ended and alerted."""

    def __init__(self, *leases: dict) -> None:
        self.leases = {row["child"]: row for row in leases}
        self.observed: list[str] = []
        self.alerts: list[tuple[int, str]] = []
        self.ended: list[tuple[int, str]] = []
        self.recorded: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> Result:
        if "to_regprocedure" in sql:
            return Result([{"present": True}])
        if "FROM ticket_board.execution_leases WHERE role = %s AND ended_at IS NULL" in sql:
            return Result([dict(row) for row in self.leases.values() if row["ended_at"] is None])
        if "FROM ticket_board.execution_leases WHERE ended_at IS NULL AND interrupt IS NOT NULL" in sql:
            return Result([dict(row) for row in self.leases.values() if row["ended_at"] is None and row["interrupt"]])
        if "observe_execution_lease(" in sql:
            role, child, pid, pgid, ticks, started, command, ticket, turn, deadline, source, hard, look = params
            self.observed.append(child)
            row = self.leases.setdefault(child, {
                "id": 7000 + len(self.leases), "role": role, "child": child, "pid": pid, "pgid": pgid,
                "start_ticks": ticks, "command": command, "ticket_id": ticket, "turn_started_at": None,
                "deadline": datetime.fromtimestamp(deadline, timezone.utc), "deadline_source": source, "hard": hard,
                "state": "running", "evidence": [], "interrupt": None, "ended_at": None})
            row["evidence"] = row["evidence"] + [look.obj]
            return Result([{"observe_execution_lease": dict(row)}])
        if "notification_trace" in sql:
            return Result([{"ticket_id": "SYRD-840"}])
        if "note_execution_lease_alert" in sql:
            self.alerts.append((params[0], params[2]))
            return Result([{"r": True}])
        if "end_execution_lease" in sql:
            row = next(row for row in self.leases.values() if row["id"] == params[0])
            row["ended_at"], row["end_reason"] = time.time(), params[1]
            self.ended.append((params[0], params[1]))
            return Result([{"r": True}])
        if "record_execution_lease_interrupt" in sql:
            row = next(row for row in self.leases.values() if row["id"] == params[0])
            stage = "done" if "'done'" in sql else params[1]
            self.recorded.append((params[0], stage))
            row["interrupt"] = {**row["interrupt"], "stage": stage, "stage_at": datetime.now(timezone.utc).isoformat()}
            return Result([{"r": True}])
        return Result([])  # the role's work: none, here


class Gate:
    #: Everything this suite starts is a tool's command, not the provider's start-up furniture.
    LAUNCHER_SEED_SETTLE_SECONDS = -1e12

    def __init__(self, state_dir: Path) -> None:
        self.process_table_reader = read_process_table
        self.proc_root = str(PROC)
        self.state_store = SimpleNamespace(state_dir=str(state_dir), read=lambda _target: None)

    def is_working(self, *_args, **_kwargs):
        return False


def role_pass(board: Board, state_dir: Path, *, observe_seconds: float = 0.0, every: float = 0.0,
              seen: dict | None = None) -> int:
    """One lease pass over this suite's own process as the pane, every holder past the default threshold at once.

    `every` is how often a recorded lease is looked at again, against the looks kept in `seen`."""
    lp.MIN_AGE_SECONDS, lp.OBSERVE_EVERY_SECONDS = 0.0, every
    fake = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: (_ for _ in ()).throw(AssertionError(a[0] % a[1:]))))
    return lp._role_pass(fake, board, Gate(state_dir), nl, "ops", "syrd-ops:0.0", os.getpid(), list(read_process_table()),
                         PROC, execution_lease.lease_dir(state_dir), {"done"}, observe_seconds,
                         {} if seen is None else seen, time.time())


def defaults() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def tool_command(argv: list[str], state_dir: Path) -> subprocess.Popen:
    """A command in a session of its own, as a provider's tool starts one, with the pane-state directory a pane has."""
    return subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, preexec_fn=defaults,
                            env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state_dir)})


def stat_of(pid: int) -> list[str]:
    try:
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return []
    return stat[stat.rfind(")") + 2:].split()


def children(pid: int) -> set[int]:
    found: set[int] = set()
    for entry in (PROC / str(pid) / "task").glob("*/children"):
        try:
            found |= {int(child) for child in entry.read_text().split()}
        except OSError:
            continue
    return found


def zombies_of(pid: int) -> list[int]:
    return sorted(child for child in children(pid) if stat_of(child)[:1] == ["Z"])


def sleeping(marker: str) -> list[int]:
    """Live processes whose command line is `sleep <marker>`."""
    found = []
    for entry in PROC.iterdir():
        if entry.name.isdigit():
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if argv[:2] == [b"sleep", marker.encode()] and stat_of(int(entry.name))[:1] != ["Z"]:
                found.append(int(entry.name))
    return found


def wait_until(condition, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def stop(proc: subprocess.Popen, *markers: str) -> None:
    """End a command and what it started, by its group and by the unique sleep lengths it ran."""
    for pid in [proc.pid] + [pid for marker in markers for pid in sleeping(marker)]:
        for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                kill()
            except (ProcessLookupError, PermissionError):
                pass
    proc.wait(timeout=10)


def test_one_bounded_command_is_one_declared_lease_and_its_sessions_are_folded_into_it() -> None:
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state, ready = Path(tmp) / "state", Path(tmp) / "ready"
        state.mkdir()
        proc = tool_command([sys.executable, str(WRAPPER), "--deadline", "50m", "--expect", "40m", "--label", "SAPI-128",
                             "--", sys.executable, "-c", COMMAND, str(ready)], state)
        try:
            check(wait_until(lambda: ready.exists() and ready.read_text().strip(), 20),
                  "the bounded command started its stage and its orphans")
            check(wait_until(lambda: sleeping("840.1") and sleeping("840.2"), 10), "both detached sleeps run")
            orphan, stage = sleeping("840.1")[0], sleeping("840.2")[0]
            check(orphan in children(proc.pid), f"the detached orphan was adopted by the wrapper: {children(proc.pid)}")
            sessions = {stat_of(orphan)[3], stat_of(stage)[3]}
            check(len(sessions) == 2 and not sessions & {str(proc.pid), str(os.getsid(0))},
                  f"each detached sleep is in a session of its own, apart from the wrapper's: {sessions}")
            check(wait_until(lambda: not zombies_of(proc.pid), 3),
                  f"the wrapper reaps the orphans that exited while its command runs: {zombies_of(proc.pid)}")

            board = Board()
            told = role_pass(board, state)
            key = next((child for child in board.observed if child.startswith(f"{proc.pid}@")), "")
            check(board.observed == [key] and key,
                  f"one bounded command is one lease, the wrapper's session (pid {proc.pid}): {board.observed}")
            row = board.leases[key]
            check(row["deadline_source"] == "hard" and row["hard"] is True,
                  f"the lease carries the command's own declared deadline, not the default: {row['deadline_source']}")
            check(told == 0 and not board.alerts,
                  f"no session of it is past the default threshold on a lease of its own: {board.alerts}")
            table = list(read_process_table())
            kin = lp.family(table, proc.pid)
            check({orphan, stage} <= kin, f"the stage and the adopted orphan are the command's: {sorted(kin)}")
            check(row["evidence"][-1]["members"] == len(kin) >= 4,
                  f"the lease's evidence counts what its sessions run: {row['evidence'][-1]} vs {sorted(kin)}")
            groups = lp._session_groups(table, proc.pid, PROC)
            check({int(stat_of(orphan)[2]), int(stat_of(stage)[2])} <= groups,
                  f"an interrupt of the lease reaches the folded sessions' process groups: {sorted(groups)}")

            again = role_pass(board, state)
            check(again == 0 and board.observed == [key, key] and len(board.leases) == 1,
                  f"the next pass looks at the same one lease: {board.observed}")
        finally:
            stop(proc, "840.1", "840.2")


def test_a_session_left_only_as_a_zombie_holds_no_lease_and_its_lease_ends() -> None:
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state = Path(tmp) / "state"
        state.mkdir()
        gone = tool_command(["true"], state)  # its parent, this suite, has not reaped it yet
        live = tool_command(["sleep", "840.3"], state)
        # Its leader exits at once, but a member it left runs on, adopted elsewhere: the command is not over.
        left = tool_command(["bash", "-c", "sleep 840.4 & exit 0"], state)
        try:
            check(wait_until(lambda: stat_of(gone.pid)[:1] == ["Z"], 5), "the exited command waits to be reaped")
            check(wait_until(lambda: stat_of(left.pid)[:1] == ["Z"] and sleeping("840.4"), 5),
                  "the other command's leader exited, and its member runs on")
            started = next(row[4] for row in read_process_table() if row[0] == gone.pid)  # as the pass keys it
            board = Board({"id": 6999, "role": "ops", "child": f"{gone.pid}@{int(started)}", "pid": gone.pid,
                           "pgid": gone.pid, "start_ticks": 1, "command": "", "ticket_id": None, "turn_started_at": None,
                           "deadline": datetime.now(timezone.utc), "deadline_source": "default", "hard": False,
                           "state": "running", "evidence": [], "interrupt": None, "ended_at": None})
            told = role_pass(board, state)
            check(not any(child.startswith(f"{gone.pid}@") for child in board.observed),
                  f"an exited command in a session of its own is not leased: {board.observed}")
            check(any(child.startswith(f"{live.pid}@") for child in board.observed),
                  f"a running one beside it still is: {board.observed}")
            check(any(child.startswith(f"{left.pid}@") for child in board.observed),
                  f"so is one whose leader exited while a member of its session still runs: {board.observed}")
            check(board.ended == [(6999, "the process exited")],
                  f"the zombie's open lease ends as exited, its alert with it: {board.ended}")
            check(told == 2 and 6999 not in [lease for lease, _ in board.alerts],
                  f"only the running commands, past the threshold, are alerted: {board.alerts}")
        finally:
            stop(live, "840.3")
            stop(left, "840.4")
            gone.wait(timeout=5)


def test_an_interrupt_reaches_a_session_the_command_started_and_nothing_beside_it() -> None:
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state, ready = Path(tmp) / "state", Path(tmp) / "ready"
        state.mkdir()
        script = ("import subprocess, sys, time\n"
                  "subprocess.Popen(['sleep', '840.6'], start_new_session=True)\n"
                  "open(sys.argv[1], 'w').write('1')\n"
                  "time.sleep(840)\n")
        proc = tool_command([sys.executable, "-c", script, str(ready)], state)
        beside = tool_command(["sleep", "840.8"], state)
        try:
            check(wait_until(lambda: ready.exists() and sleeping("840.6"), 20), "the command started its stage")
            stage = sleeping("840.6")[0]
            board = Board({"id": 7100, "role": "ops", "child": f"{proc.pid}@0", "pid": proc.pid, "pgid": proc.pid,
                           "start_ticks": execution_lease.start_ticks(proc.pid), "command": "python3 -c [redacted]",
                           "ticket_id": "SYRD-840", "ended_at": None,
                           "interrupt": {"stage": "requested", "requested_by": "director", "reason": "SYRD-840",
                                         "stage_at": None}})
            fake = SimpleNamespace(activity_gate=Gate(state).is_working,
                                   activity_hold=SimpleNamespace(_terminal_states=lambda: {"done"}),
                                   logger=SimpleNamespace(warning=lambda *a: (_ for _ in ()).throw(
                                       AssertionError(a[0] % a[1:]))))
            check(lp.carry_out_interrupts(fake, board) == 1 and board.recorded == [(7100, "int")],
                  f"the first step is SIGINT: {board.recorded}")
            check(wait_until(lambda: not sleeping("840.6"), 3),
                  f"the stage in a session of its own was sent it too, as the command's (pid {stage})")
            check(sleeping("840.8") == [beside.pid], "a command beside it, not started by it, was sent nothing")
        finally:
            stop(proc, "840.6")
            stop(beside, "840.8")


def test_a_folded_stage_that_outlives_its_command_keeps_the_lease_open_and_interruptible() -> None:
    """Audit's case: the outer command dies, and its stage runs on, reparented out of the pane's tree."""
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state = Path(tmp) / "state"
        state.mkdir()
        script = ("import subprocess, sys, time\n"
                  "subprocess.Popen(['sleep', sys.argv[2]], start_new_session=True)\n"
                  "open(sys.argv[1], 'w').write('1')\n"
                  "time.sleep(840)\n")
        commands = {marker: (tool_command([sys.executable, "-c", script, str(Path(tmp) / marker), marker], state),
                             Path(tmp) / marker) for marker in ("840.7", "840.9")}
        board = Board()
        try:
            for marker, (proc, ready) in commands.items():
                check(wait_until(lambda: ready.exists() and sleeping(marker), 20), f"the command started its stage {marker}")
            stages = {marker: sleeping(marker)[0] for marker in commands}
            role_pass(board, state, observe_seconds=3600)
            check(len(board.leases) == 2 and not board.alerts,
                  f"each command is one lease, its stage folded into it: {sorted(board.leases)}")
            rows = {marker: next(row for row in board.leases.values() if row["pid"] == proc.pid)
                    for marker, (proc, _) in commands.items()}
            for marker, row in rows.items():
                check(row["evidence"][-1].get("folded") == [[stages[marker], execution_lease.start_ticks(stages[marker])]],
                      f"the lease records its folded stage by identity: {row['evidence'][-1]}")

            for proc, _ in commands.values():
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            under_pane = nl.descendant_work_sample(os.getpid(), list(read_process_table())).pids
            check(all(stage not in under_pane and not lp._exited(PROC, stage) for stage in stages.values()),
                  f"both stages run on, reparented out of the pane's tree: {stages}")
            role_pass(board, state, observe_seconds=3600)
            check(not board.ended and all(row["ended_at"] is None for row in rows.values()),
                  f"the outer leases stay open while their folded stages run: {board.ended}")
            check(lp.lease_sessions(rows["840.7"], list(read_process_table()), PROC) == [stages["840.7"]],
                  "what the lease still holds is its escaped stage")

            rows["840.9"]["interrupt"] = {"stage": "requested", "requested_by": "director", "reason": "SYRD-840",
                                          "stage_at": None}
            fake = SimpleNamespace(activity_gate=Gate(state).is_working,
                                   activity_hold=SimpleNamespace(_terminal_states=lambda: {"done"}),
                                   logger=SimpleNamespace(warning=lambda *a: (_ for _ in ()).throw(
                                       AssertionError(a[0] % a[1:]))))
            check(lp.carry_out_interrupts(fake, board) == 1 and board.recorded == [(rows["840.9"]["id"], "int")],
                  f"an interrupt of the dead command's lease still has something to signal: {board.recorded}")
            check(wait_until(lambda: not sleeping("840.9"), 3), "its escaped stage was sent SIGINT and ended")
            check(sleeping("840.7") == [stages["840.7"]], "the other command's stage was sent nothing")

            os.kill(stages["840.7"], signal.SIGKILL)
            check(wait_until(lambda: not sleeping("840.7"), 3), "the last stage is gone")
            role_pass(board, state, observe_seconds=3600)
            check(board.ended == [(rows["840.7"]["id"], "the process exited")],
                  f"once nothing it holds runs, the lease ends as exited: {board.ended}")
        finally:
            for marker, (proc, _) in commands.items():
                stop(proc, marker)


def test_a_stage_started_between_looks_is_recorded_at_once() -> None:
    """A lease is looked at again only every 30 s; a stage folded in between is recorded on the pass that sees it, so an
    outer command that dies before the next look cannot take it out of the lease system unrecorded."""
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state, go, ready = Path(tmp) / "state", Path(tmp) / "go", Path(tmp) / "ready"
        state.mkdir()
        script = ("import os, subprocess, sys, time\n"
                  "while not os.path.exists(sys.argv[1]):\n"
                  "    time.sleep(0.05)\n"
                  "subprocess.Popen(['sleep', '840.5'], start_new_session=True)\n"
                  "open(sys.argv[2], 'w').write('1')\n"
                  "time.sleep(840)\n")
        proc = tool_command([sys.executable, "-c", script, str(go), str(ready)], state)
        board, seen = Board(), {}
        try:
            check(wait_until(lambda: proc.pid in children(os.getpid()), 5), "the command runs")
            role_pass(board, state, observe_seconds=3600, every=3600, seen=seen)
            check(len(board.observed) == 1 and not board.leases[board.observed[0]]["evidence"][-1].get("folded"),
                  f"first look: one lease, nothing folded yet: {board.observed}")
            role_pass(board, state, observe_seconds=3600, every=3600, seen=seen)
            check(len(board.observed) == 1, f"nothing changed, and the next look is not due: {board.observed}")
            go.write_text("1")
            check(wait_until(lambda: ready.exists() and sleeping("840.5"), 20), "the command started its stage")
            role_pass(board, state, observe_seconds=3600, every=3600, seen=seen)
            stage = sleeping("840.5")[0]
            row = board.leases[board.observed[0]]
            check(len(board.observed) == 2 and row["evidence"][-1].get("folded") == [[stage, execution_lease.start_ticks(stage)]],
                  f"the pass that first sees the folded stage records it, before the look is due: {row['evidence'][-1]}")
        finally:
            stop(proc, "840.5")


def test_a_stage_that_escapes_while_its_command_runs_stays_recorded() -> None:
    """The stage's own parent, not the outer command, exits: the stage leaves the pane's tree while the lease is still
    looked at. The next look keeps it, so the lease still holds it and an interrupt still reaches it."""
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state, release, ready = Path(tmp) / "state", Path(tmp) / "release", Path(tmp) / "ready"
        state.mkdir()
        middle = ("import os, subprocess, sys, time\n"
                  "subprocess.Popen(['sleep', '840.4'], start_new_session=True)\n"
                  "open(sys.argv[2], 'w').write('1')\n"
                  "while not os.path.exists(sys.argv[1]):\n"
                  "    time.sleep(0.05)\n")
        script = ("import subprocess, sys, time\n"
                  f"subprocess.Popen([sys.executable, '-c', {middle!r}, sys.argv[1], sys.argv[2]])\n"
                  "time.sleep(840)\n")
        proc = tool_command([sys.executable, "-c", script, str(release), str(ready)], state)
        board = Board()
        try:
            check(wait_until(lambda: ready.exists() and sleeping("840.4"), 20), "the command's child started the stage")
            stage = sleeping("840.4")[0]
            role_pass(board, state, observe_seconds=3600)
            row = next(iter(board.leases.values()))
            check(len(board.leases) == 1 and row["evidence"][-1].get("folded") == [[stage, execution_lease.start_ticks(stage)]],
                  f"one lease, its stage folded into it: {row['evidence'][-1]}")
            release.write_text("1")
            check(wait_until(lambda: stage not in nl.descendant_work_sample(os.getpid(), list(read_process_table())).pids
                             and proc.poll() is None, 10),
                  "the stage's parent exited: the stage left the pane's tree while the command runs on")
            role_pass(board, state, observe_seconds=3600)
            check(row["evidence"][-1].get("folded") == [[stage, execution_lease.start_ticks(stage)]] and len(board.leases) == 1,
                  f"the next look keeps the escaped stage on the lease, by identity: {row['evidence'][-1]}")
            check(lp.lease_sessions(row, list(read_process_table()), PROC) == [proc.pid, stage],
                  "the lease holds its command and its escaped stage")
        finally:
            stop(proc, "840.4")


def test_a_stage_two_levels_deep_is_recorded_on_the_top_lease_and_kept_when_both_above_it_die() -> None:
    """SAPI-128's shape (SYRD-841): the bounded command A started a check stage B in a session of its own, and B
    started git's detached maintenance C in another. C is folded into the top lease, A's, not into B, which holds no
    lease of its own: once A and B die and C runs on out of the pane's tree, A's lease still holds C."""
    with tempfile.TemporaryDirectory(prefix="s841.") as tmp:
        state, ready = Path(tmp) / "state", Path(tmp) / "ready"
        state.mkdir()
        b_script = ("import os, subprocess, sys, time\n"
                    "subprocess.Popen(['sleep', '841.3'], start_new_session=True)\n"
                    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                    "time.sleep(840)\n")
        a_script = ("import subprocess, sys, time\n"
                    f"subprocess.Popen([sys.executable, '-c', {b_script!r}, sys.argv[1]], start_new_session=True)\n"
                    "time.sleep(840)\n")
        proc = tool_command([sys.executable, "-c", a_script, str(ready)], state)
        board, b = Board(), 0
        try:
            check(wait_until(lambda: ready.exists() and ready.read_text().strip() and sleeping("841.3"), 20),
                  "A started B, and B started C")
            b, c = int(ready.read_text()), sleeping("841.3")[0]
            sessions = {who: stat_of(pid)[3] for who, pid in (("A", proc.pid), ("B", b), ("C", c))}
            check(len(set(sessions.values())) == 3 and stat_of(c)[1] == str(b) and stat_of(b)[1] == str(proc.pid),
                  f"three sessions, each started from inside the one above: {sessions}")
            role_pass(board, state, observe_seconds=3600)
            check(len(board.leases) == 1, f"one lease for the whole command: {sorted(board.leases)}")
            row = next(iter(board.leases.values()))
            check(row["pid"] == proc.pid, f"the lease is the top command's, A's: {row['pid']} vs {proc.pid}")
            recorded = row["evidence"][-1].get("folded") or []
            check(sorted(recorded) == sorted([[b, execution_lease.start_ticks(b)], [c, execution_lease.start_ticks(c)]]),
                  f"A's lease records both B and the two-deep C by identity: {recorded}")

            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            os.kill(b, signal.SIGKILL)
            under_pane = nl.descendant_work_sample(os.getpid(), list(read_process_table())).pids
            check(wait_until(lambda: lp._exited(PROC, b), 3) and not lp._exited(PROC, c) and c not in under_pane,
                  "A and B are dead, and C runs on, reparented out of the pane's tree")
            role_pass(board, state, observe_seconds=3600)
            check(not board.ended and row["ended_at"] is None,
                  f"A's lease stays open while the two-deep C runs: {board.ended}")
            check(lp.lease_sessions(row, list(read_process_table()), PROC) == [c],
                  f"what A's lease still holds is C: {lp.lease_sessions(row, list(read_process_table()), PROC)}")

            row["interrupt"] = {"stage": "requested", "requested_by": "director", "reason": "SYRD-841", "stage_at": None}
            fake = SimpleNamespace(activity_gate=Gate(state).is_working,
                                   activity_hold=SimpleNamespace(_terminal_states=lambda: {"done"}),
                                   logger=SimpleNamespace(warning=lambda *a: (_ for _ in ()).throw(
                                       AssertionError(a[0] % a[1:]))))
            check(lp.carry_out_interrupts(fake, board) == 1 and board.recorded == [(row["id"], "int")],
                  f"interrupt-lease on A's lease still has something to signal: {board.recorded}")
            check(wait_until(lambda: not sleeping("841.3"), 3), "the two-deep C was sent SIGINT and ended")
        finally:
            if b:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(b, signal.SIGKILL)
            stop(proc, "841.3")


def test_the_wrapper_reaps_an_adopted_orphan_while_its_command_still_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="s840.") as tmp:
        state, ready = Path(tmp) / "state", Path(tmp) / "ready"
        state.mkdir()
        script = ("import os, sys, time\n"
                  "for _ in range(3):\n"
                  "    if os.fork() == 0:\n"
                  "        os.setsid()\n"
                  "        os.fork()\n"
                  "        os._exit(0)\n"
                  "    os.wait()\n"
                  "open(sys.argv[1], 'w').write('1')\n"
                  "time.sleep(840)\n")
        proc = tool_command([sys.executable, str(WRAPPER), "--expect", "40m", "--", sys.executable, "-c", script,
                             str(ready)], state)
        try:
            check(wait_until(ready.exists, 20), "the command left its orphans")
            check(wait_until(lambda: not zombies_of(proc.pid), 3),
                  f"no exited orphan waits under the wrapper while the command runs: {zombies_of(proc.pid)}")
            check(proc.poll() is None, "the command is still running")
        finally:
            stop(proc)


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"execution_lease_nested_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
