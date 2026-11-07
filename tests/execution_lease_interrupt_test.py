#!/usr/bin/env python3
"""SYRD-740: the Director's interrupt is carried out from the lease's own record, promptly, and reconciled.

MEFP, 2026-11-07: luna-5's lease 33 tracked process group 2681865, a broad `git grep`, after MEFP-902 moved to
Audit. The Director ran `ticket-board-write interrupt-lease 33`. The board answered `requested` and cleared the
alert, but the group was still alive 75 seconds later, and the Director stopped it by hand. The lease then closed
"no signal; it ended". The listener's lease pass was the only thing that carried an interrupt out:
- it ran once an iteration, and MEFP's iterations were minutes apart (the lease was observed at 08:34:47, 08:36:16
  and 08:38:19, and next at 08:39:57);
- it signalled only a session it found under the role's pane;
- a lease that left the pane's tree was closed as ended, alive or not.

`execution_lease_pass.carry_out_interrupts` now works from the lease row. `interrupt_watch` runs it every second on a
thread and connection of its own for as long as the listener listens, whatever pass the listener is in, and the
listener runs it again before every delivery. tests/execution_lease_test.py covers it on a production board: a pane
tmux cannot answer for, and a first producer that runs for seconds while one interrupt is pending and another is asked
for. Here, the step itself runs over real processes, real /proc and real signals, with the board's few calls stood in:
- a session whose leader exited but whose members run on;
- a reused pid, which gets no signal;
- the escalation past ignored SIGINT and SIGTERM, a grace apart;
- a request made while a long run of deliveries is under way, acted on before the run ends;
- a lease that left its pane's tree, which is not ended while its process runs;
- the watch, which carries a request out while the listener is busy elsewhere, opens its connection again when it
  cannot get one, ends with the listen, and starts nothing without a real activity gate.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import execution_lease, execution_lease_pass as lp  # noqa: E402
from scripts.ticket_board.pane_activity_gate import read_process_table  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def iso(epoch: float) -> str:
    """A timestamp as the board's jsonb holds one."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Result:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.description = rows, None

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Board:
    """The board's side of an interrupt: the open leases, and what the listener records of each step."""

    def __init__(self, *leases: dict) -> None:
        self.leases = {row["id"]: row for row in leases}
        self.recorded: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> Result:
        if "to_regprocedure" in sql:
            return Result([{"present": True}])
        if "FROM ticket_board.execution_leases WHERE ended_at IS NULL AND interrupt IS NOT NULL" in sql:
            return Result([dict(row) for row in sorted(self.leases.values(), key=lambda r: r["id"])
                           if row["ended_at"] is None and row["interrupt"]])
        if "record_execution_lease_interrupt" in sql:
            # The step names its stage as a parameter; the end, as the literal 'done'.
            lease_id, stage, detail = (params[0], "done", params[1]) if "'done'" in sql else (params[0], params[1], params[2])
            self.recorded.append((lease_id, stage, detail, time.time()))
            row = self.leases[lease_id]
            row["interrupt"] = {**row["interrupt"], "stage": stage, "stage_at": iso(time.time())}
            if stage == "done":
                row["interrupt"]["result"] = detail
                row["ended_at"] = time.time()
            return Result([{"r": True}])
        if "end_execution_lease" in sql:
            self.leases[params[0]]["ended_at"] = time.time()
            self.leases[params[0]]["end_reason"] = params[1]
            return Result([{"r": True}])
        return Result([])  # the role's work and its last hand-over: none, here

    def stage(self, lease_id: int) -> str:
        return str(self.leases[lease_id]["interrupt"].get("stage") or "")


class Gate:
    def __init__(self, state_dir: Path) -> None:
        self.process_table_reader = read_process_table
        self.proc_root = "/proc"
        self.state_store = SimpleNamespace(state_dir=str(state_dir))

    def is_working(self, *_args, **_kwargs):
        return False


def listener(state_dir: Path) -> SimpleNamespace:
    gate = Gate(state_dir)
    return SimpleNamespace(activity_gate=gate.is_working, activity_hold=SimpleNamespace(_terminal_states=lambda: {"done"}),
                           logger=SimpleNamespace(warning=lambda *a: (_ for _ in ()).throw(AssertionError(a[0] % a[1:]))))


def spawn(script: str) -> subprocess.Popen:
    """A command in a session of its own, as a provider's tool starts one: with the default dispositions, whatever
    this suite's own parent ignores."""
    def defaults() -> None:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    return subprocess.Popen(["bash", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=defaults)


def lease_of(lease_id: int, pid: int, *, ticks: int | None = None, stage: str = "requested") -> dict:
    return {"id": lease_id, "role": "luna-5", "child": f"{pid}@0", "pid": pid, "pgid": pid,
            "start_ticks": execution_lease.start_ticks(pid) if ticks is None else ticks, "ticket_id": "MEFP-902",
            "command": "bash -c [redacted]", "ended_at": None,
            "interrupt": {"stage": stage, "requested_by": "director", "reason": "outlived its ticket",
                          "stage_at": None if stage == "requested" else iso(time.time())}}


def session_alive(session: int) -> bool:
    """A member of the session still runs. An orphan killed but not yet reaped by whatever adopted it runs nothing."""
    return any(row[3] == session and not lp._exited(Path("/proc"), int(row[0])) for row in read_process_table())


def step(fake: SimpleNamespace, board: Board) -> int:
    fake.__dict__.pop("_execution_interrupts_looked", None)  # every call a look, here
    return lp.carry_out_interrupts(fake, board)


def wait_until(condition, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_an_interrupt_signals_the_recorded_group_at_once_and_ends_the_lease() -> None:
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("while :; do sleep 0.2; done # plain-740")
        try:
            time.sleep(0.3)
            board, fake = Board(lease_of(33, proc.pid)), listener(Path(tmp))
            moved = step(fake, board)
            check(moved == 1 and board.stage(33) == "int" and board.recorded[0][2] == "SIGINT",
                  f"the first look sends SIGINT and records it: {board.recorded}")
            check(wait_until(lambda: proc.poll() is not None and not session_alive(proc.pid), 3), "and the group stops")
            marker = execution_lease.interrupt_marker(execution_lease.lease_dir(Path(tmp)), proc.pid,
                                                      board.leases[33]["start_ticks"])
            check(marker and marker["reason"] == "outlived its ticket" and marker["actor"] == "director",
                  f"the wrapper's marker says who asked and why: {marker}")
            step(fake, board)
            check(board.leases[33]["ended_at"] is not None
                  and board.leases[33]["interrupt"].get("result") == "SIGINT; it ended",
                  f"the next look finds nothing left and ends the lease, saying what it was sent: {board.recorded}")
        finally:
            proc.kill()


def test_a_session_whose_leader_exited_is_still_found_by_its_number() -> None:
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        # The leader starts a member that ignores SIGINT and then exits; the member keeps the session's number.
        proc = spawn("(trap '' INT; exec sleep 740.1) & sleep 0.5; exit 0")
        try:
            time.sleep(0.2)
            row = lease_of(34, proc.pid)
            proc.wait(timeout=5)
            time.sleep(0.2)
            check(session_alive(proc.pid) and not Path(f"/proc/{proc.pid}").exists(),
                  "the leader is gone and its session runs on")
            board, fake = Board(row), listener(Path(tmp))
            step(fake, board)
            check(board.stage(34) == "int", f"the session is still the lease's, and is signalled: {board.recorded}")
            time.sleep(0.3)
            check(session_alive(proc.pid), "its member ignored SIGINT")
            board.leases[34]["interrupt"]["stage_at"] = iso(time.time() - lp.INTERRUPT_GRACE_SECONDS - 1)
            step(fake, board)
            check(board.stage(34) == "term" and wait_until(lambda: not session_alive(proc.pid), 3),
                  f"a grace later, SIGTERM ends it: {board.recorded}")
            step(fake, board)
            check(board.leases[34]["interrupt"].get("result") == "SIGINT, then SIGTERM; it ended",
                  f"and the lease is reconciled: {board.leases[34]['interrupt']}")
        finally:
            subprocess.run(["pkill", "-f", "sleep 740.1"], check=False)


def test_a_reused_number_gets_no_signal() -> None:
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("sleep 740.2")
        try:
            time.sleep(0.2)
            board, fake = Board(lease_of(35, proc.pid, ticks=execution_lease.start_ticks(proc.pid) + 1)), listener(Path(tmp))
            step(fake, board)
            check(proc.poll() is None and board.leases[35]["interrupt"].get("result") ==
                  "no signal; its leader was gone before any signal",
                  f"a process with the recorded number but another start tick is not the lease's: {board.recorded}")
        finally:
            proc.kill()


def test_the_escalation_waits_a_grace_between_steps_and_ends_in_sigkill() -> None:
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("trap '' INT TERM; while :; do sleep 0.2; done # stubborn-740")
        try:
            time.sleep(0.3)
            board, fake = Board(lease_of(36, proc.pid)), listener(Path(tmp))
            step(fake, board)
            step(fake, board)
            check([r[1] for r in board.recorded] == ["int"], f"inside the grace, nothing more: {board.recorded}")
            for expected in ("term", "kill"):
                board.leases[36]["interrupt"]["stage_at"] = iso(time.time() - lp.INTERRUPT_GRACE_SECONDS - 1)
                step(fake, board)
                check(board.stage(36) == expected, f"a grace later, {expected}: {board.recorded}")
            check(wait_until(lambda: proc.poll() is not None and not session_alive(proc.pid), 3),
                  "SIGKILL ends what ignored the rest")
            step(fake, board)
            check(board.leases[36]["interrupt"].get("result") == "SIGINT, SIGTERM, then SIGKILL; it ended",
                  f"and the lease says so: {board.leases[36]['interrupt']}")
        finally:
            proc.kill()


def test_a_request_is_acted_on_between_deliveries() -> None:
    """MEFP's iterations were minutes long: the request must not wait for the next one."""
    from scripts.ticket_board import notify_listener as nl

    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("while :; do sleep 0.2; done # mid-delivery-740")
        try:
            time.sleep(0.3)
            board, fake = Board(), listener(Path(tmp))
            claims: list[str] = []

            def claim(_self, _conn):
                claims.append(board.stage(37) if 37 in board.leases else "")
                if len(claims) == 1:  # the Director asks while the first delivery is under way, which takes a while
                    board.leases[37] = lease_of(37, proc.pid)
                    time.sleep(lp.INTERRUPT_LOOK_EVERY_SECONDS + 0.1)
                return None if len(claims) > 2 else (len(claims), "MEFP-1", "luna-1", "m", {}, 0)

            # Each claimed notice is for a role with no live runtime, so it is requeued: the shortest delivery there is.
            this = SimpleNamespace(**vars(fake), stop_event=SimpleNamespace(is_set=lambda: False), delivered_count=0,
                                   refresh_workflow=lambda conn: None, _claim_notification=lambda conn: claim(this, conn),
                                   _payload_kind=lambda payload: "transition", role_targets={}, workflow=None,
                                   active_runtime_roles={"luna-1"},
                                   ledger=SimpleNamespace(trace=lambda *a, **k: None, requeue=lambda *a, **k: None,
                                                          safe_json_payload=lambda payload: payload))
            this.logger = SimpleNamespace(info=lambda *a: None, warning=fake.logger.warning)
            nl.TicketBoardNotifyListener.process_due_notifications(this, board)
            check(claims == ["", "int", "int"],
                  f"the request made during one delivery is carried out before the next is claimed: {claims}")
        finally:
            proc.kill()


def test_a_lease_that_left_its_panes_tree_is_not_ended_while_it_runs() -> None:
    proc = spawn("sleep 740.3")
    try:
        time.sleep(0.2)
        row = lease_of(38, proc.pid)
        table = list(read_process_table())
        check(lp.lease_session(row, table, Path("/proc")) == proc.pid,
              "alive, it is still the lease's session, wherever it is now")
        proc.kill()
        check(wait_until(lambda: Path(f"/proc/{proc.pid}/stat").read_text().split(") ")[1][0] == "Z", 3),
              "killed and not yet reaped, it is a zombie")
        check(lp.lease_session(row, list(read_process_table()), Path("/proc")) is None,
              "an exited process waiting to be reaped runs nothing: nothing of the lease is left")
        proc.wait(timeout=5)
        check(lp.lease_session(row, list(read_process_table()), Path("/proc")) is None, "gone, nothing of it is left")
    finally:
        proc.kill()


def test_the_watch_carries_out_a_request_while_the_listener_is_busy_elsewhere() -> None:
    """Audit of fef749a1: a request must not wait for the producer passes that run before any delivery."""
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("while :; do sleep 0.2; done # watched-740")
        try:
            time.sleep(0.3)
            board, warned, connects = Board(lease_of(39, proc.pid)), [], []

            def connector(conninfo, **kwargs):
                connects.append((conninfo, kwargs.get("autocommit")))
                if len(connects) == 1:
                    raise OSError("the board is restarting")  # the first connection cannot be had
                return board

            fake = listener(Path(tmp))
            fake.logger = SimpleNamespace(warning=lambda *a: warned.append(a[0] % a[1:]))
            fake.connector, fake.conninfo, fake.stop_event = connector, "dbname=pgu", threading.Event()
            fake._connector_kwargs = lambda: {"autocommit": True}
            began = time.time()
            with lp.interrupt_watch(fake) as thread:
                # The listener is in a producer pass that takes seconds; it never reaches a delivery.
                stopped = wait_until(lambda: proc.poll() is not None, 6)
                stopped_after = time.time() - began
                running_inside = thread is not None and thread.is_alive()
            check(stopped and stopped_after < 4 and board.stage(39) == "int" and running_inside,
                  f"the watch signals a pending request within seconds while the listener is busy: "
                  f"{stopped_after:.1f}s {board.recorded}")
            check(connects[:2] == [("dbname=pgu", True), ("dbname=pgu", True)]
                  and warned and "the board is restarting" in warned[0],
                  f"a connection it cannot get is said, and opened again at the next look: {connects} {warned}")
            check(not thread.is_alive(), "the watch ends with the listen")
            # Without a real activity gate there is nothing to watch, and nothing is connected.
            idle = SimpleNamespace(activity_gate=lambda *a, **k: False, connector=connector, stop_event=threading.Event())
            before = len(connects)
            with lp.interrupt_watch(idle) as none:
                time.sleep(0.2)
            check(none is None and len(connects) == before, "no gate, no watch and no connection")
        finally:
            proc.kill()


def test_one_thread_carries_the_interrupts_out_at_a_time() -> None:
    """The watch and the listener's look before a delivery never step one lease twice, and never wait for each other."""
    with tempfile.TemporaryDirectory(prefix="s740.") as tmp:
        proc = spawn("while :; do sleep 0.2; done # one-at-a-time-740")
        try:
            time.sleep(0.3)
            board, fake = Board(lease_of(40, proc.pid)), listener(Path(tmp))
            busy = fake.__dict__.setdefault("_execution_interrupts_lock", threading.Lock())
            busy.acquire()  # the other thread is carrying them out now
            try:
                answered: list[int] = []
                other = threading.Thread(target=lambda: answered.append(step(fake, board)), daemon=True)
                other.start()
                other.join(3)
                check(answered == [0] and board.recorded == [] and proc.poll() is None,
                      f"while another thread carries the interrupts out, a look leaves them to it, at once: "
                      f"{answered} {board.recorded}")
            finally:
                busy.release()
            other.join(3)
            check(step(fake, board) == 1 and board.stage(40) == "int", f"the next look carries them on: {board.recorded}")
        finally:
            proc.kill()


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"execution_lease_interrupt_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
