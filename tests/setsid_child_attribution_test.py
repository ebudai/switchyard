#!/usr/bin/env python3
"""SYRD-794: a process the current ticket's work started is that work, whatever its session.

When main was handed SYRD-785 the Director was told twice that it had background work "left running from earlier
work": `postgres: io worker 0` and `io worker 1` of a throwaway cluster that main's current SYRD-785 sweep had started.
The postmaster was in the sweep's session; PostgreSQL 18 starts each io worker in a session of its own (pid = pgid =
sid), and the hand-over report judged each session alone: no worktree, no lease, so "earlier work".

Now a session whose parent chain reaches a session that is the role's current work -- its worktree, or its lease --
is that work too. Each parent must have started no later than its child, so a reused pid never counts; a child whose
parents have ended reaches nothing and is reported as before (SYRD-583, SYRD-636).

Real processes under a stand-in pane (a child subreaper, so an orphan stays in its tree as a provider's leftovers
do): a "sweep" in a session of its own in a real git worktree, its "postmaster", and the postmaster's child that calls
setsid. The real `handoff_children.leftovers` samples the real /proc through the gate's own sampler; only the board's
ticket answers are a stand-in. A reused parent pid is shown over a fake /proc.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board import handoff_children as hc  # noqa: E402
from scripts.ticket_board.pane_activity_gate import descendant_work_sample, read_process_table  # noqa: E402

CHECKS = 0
BOARD = {"OWN-1": ("OWN-1", "in_progress", "app"), "OLD-2": ("OLD-2", "done", "unassigned")}
TERMINAL = {"done", "cancelled"}

#: The pane: a child subreaper that starts the sweep in a session of its own, as a provider's tool does, and reaps
#: what ends. The sweep's postmaster forks a worker that calls setsid, as PostgreSQL 18's io workers do.
PANE = r'''
import ctypes, json, os, subprocess, sys, time
ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
cwd, out = sys.argv[1], sys.argv[2]
postmaster = (
    "import os, sys, time\n"
    "os.makedirs(sys.argv[1] + '.pgdata', exist_ok=True)\n"
    "os.chdir(sys.argv[1] + '.pgdata')  # a cluster's postmaster runs in its data directory, as do its workers\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    os.setsid()\n"
    "    time.sleep(3600)\n"
    "    os._exit(0)\n"
    "open(sys.argv[1], 'w').write(str(pid))\n"
    "time.sleep(3600)\n")
sweep = subprocess.Popen(["bash", "-c", f"{sys.executable} -c \"$1\" {out}.worker; exit 0", "sweep", postmaster],
                         cwd=cwd, start_new_session=True)
open(out, "w").write(json.dumps({"sweep": sweep.pid}))
while True:
    try:
        os.wait()
    except ChildProcessError:
        time.sleep(0.1)
'''


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


def worktrees(tmp: Path) -> dict[str, Path]:
    repo = tmp / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    for name in ("own-1-app", "old-2-app"):
        git("worktree", "add", "-q", "-b", name, str(tmp / name), cwd=repo)
    return {"current": tmp / "own-1-app", "earlier": tmp / "old-2-app"}


def wait_for(predicate, what: str, seconds: float = 20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"FAILED CHECK: timed out waiting for {what}")


def session_of(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rfind(")") + 2:].split()[3])


class Pane:
    def __init__(self, cwd: Path, out: Path):
        self.proc = subprocess.Popen([sys.executable, "-c", PANE, str(cwd), str(out)], start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.out = out
        self.sweep = json.loads(wait_for(lambda: out.exists() and out.read_text(), "the sweep"))["sweep"]
        worker_file = Path(f"{out}.worker")
        self.worker = int(wait_for(lambda: worker_file.exists() and worker_file.read_text(), "the worker"))
        wait_for(lambda: Path(f"/proc/{self.worker}").exists() and session_of(self.worker) == self.worker,
                 "the worker's own session")

    def gate(self):
        return SimpleNamespace(role_targets={"app": "pgu-app:0.0"}, proc_root="/proc", LAUNCHER_SEED_STARTUP_SECONDS=60.0,
                               _pane_pid=lambda target: self.proc.pid if target == "pgu-app:0.0" else None,
                               _child_work_sample=lambda pane: descendant_work_sample(pane, read_process_table()),
                               provider_started_at=lambda target: 0.0)

    def close(self) -> None:
        for pid in (self.worker, self.sweep):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.proc.kill()
        self.proc.wait(timeout=10)


def conn():
    return SimpleNamespace(execute=lambda _sql, params: SimpleNamespace(
        fetchall=lambda: [BOARD[ticket] for ticket in params[0] if ticket in BOARD]))


def reported(pane: Pane) -> dict[int, str | None]:
    sent_at = time.time() + 2.0  # handed over just now: what runs was running before it
    found = hc.leftovers(pane.gate(), conn(), "app", sent_at, TERMINAL, time.time())
    return {holder.pid: (origin or {}).get("id") for holder, origin in found}


def test_the_current_tickets_setsid_child_is_its_work() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd794.") as raw:
        trees = worktrees(Path(raw))
        pane = Pane(trees["current"], Path(raw) / "pids.json")
        try:
            check(session_of(pane.worker) == pane.worker != session_of(pane.sweep) == pane.sweep,
                  "the worker is a session of its own, apart from the sweep's")
            check(reported(pane) == {},
                  f"the sweep is the role's current ticket's (its worktree), and the worker it started is that work too: "
                  f"no report: {reported(pane)}")
            # The postmaster and its sweep end; the worker is orphaned, and the pane keeps it, as a leftover is kept.
            os.killpg(pane.sweep, signal.SIGKILL)
            wait_for(lambda: not Path(f"/proc/{pane.sweep}").exists(), "the sweep to end")
            wait_for(lambda: hc._parent_and_session(Path("/proc"), pane.worker)[0] == pane.proc.pid,
                     "the worker to be the pane's")
            check(reported(pane) == {pane.worker: None},
                  f"its parents ended: the worker reaches no current work, and is reported as before: {reported(pane)}")
        finally:
            pane.close()


def test_an_earlier_tickets_child_is_still_reported() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd794.") as raw:
        trees = worktrees(Path(raw))
        pane = Pane(trees["earlier"], Path(raw) / "pids.json")
        try:
            check(reported(pane) == {pane.sweep: "OLD-2", pane.worker: None},
                  f"a finished ticket's sweep, and the worker it started, are both reported: {reported(pane)}")
        finally:
            pane.close()


def test_a_reused_parent_pid_is_never_the_parent() -> None:
    sent_at, now = 1000.0, 2000.0
    with tempfile.TemporaryDirectory(prefix="syrd794f.") as raw:
        tmp = Path(raw)
        trees = worktrees(tmp)
        proc = tmp / "proc"
        # pid: (parent, session, start, start ticks, cwd, argv); the pane is 4, its own session 1.
        table = {
            30: (4, 30, 900.0, 3000, trees["current"], ["bash", "-c", "sweep"]),       # the current ticket's
            31: (30, 31, 950.0, 3100, Path("/tmp"), ["postgres: io worker 0"]),         # its child: not reported
            41: (30, 41, 950.0, 2000, Path("/tmp"), ["postgres: io worker 1"]),         # "30" started after it: reused
            51: (4, 51, 950.0, 3100, Path("/tmp"), ["postgres: io worker 2"]),          # the pane's own child
            72: (30, 72, 950.0, 3050, Path("/tmp"), ["launcher"]),                      # a setsid'd launcher of 30's,
            71: (72, 71, 960.0, 3060, Path("/tmp"), ["postgres: io worker 3"]),         # and its own setsid'd child
            91: (30, 91, 950.0, 3000, Path("/tmp"), ["postgres: io worker 4"]),         # forked in 30's own tick
        }
        for pid, (parent, session, _start, ticks, cwd, argv) in table.items():
            (proc / str(pid)).mkdir(parents=True)
            fields = ["S", str(parent), str(session), str(session)] + ["0"] * 15 + [str(ticks)]
            (proc / str(pid) / "stat").write_text(f"{pid} (x) " + " ".join(fields) + "\n")
            (proc / str(pid) / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
            (proc / str(pid) / "cwd").symlink_to(cwd)
        sample = SimpleNamespace(observed=True, pids=frozenset(table), detached=frozenset(table),
                                 starts=tuple((pid, row[2]) for pid, row in table.items()),
                                 sessions=tuple((pid, row[1]) for pid, row in table.items()))
        gate = SimpleNamespace(role_targets={"app": "pgu-app:0.0"}, proc_root=str(proc), LAUNCHER_SEED_STARTUP_SECONDS=60.0,
                               _pane_pid=lambda target: 4, _child_work_sample=lambda pane: sample,
                               provider_started_at=lambda target: 100.0)
        found = {holder.pid: (origin or {}).get("id") for holder, origin in hc.leftovers(gate, conn(), "app", sent_at, TERMINAL, now)}
        check(found == {41: None, 51: None},
              f"a child of the current work is that work, a grandchild through a session of its own too; one whose "
              f"'parent' started after it, or that is the pane's own, is reported: {found}")
        # SYRD-804: a child forked within its parent's clock tick has the same start ticks; it is still that parent's
        # (only a parent that started after its child is a reused pid). Here always, where a real fork lands in the
        # same tick only sometimes.
        check(91 not in found, f"a child started in its parent's own tick is the current work's, not reported: {found}")


def main() -> int:
    for test in (test_a_reused_parent_pid_is_never_the_parent, test_the_current_tickets_setsid_child_is_its_work,
                 test_an_earlier_tickets_child_is_still_reported):
        test()
    print(f"setsid_child_attribution_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
