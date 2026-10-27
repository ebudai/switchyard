#!/usr/bin/env python3
"""SYRD-688: a launch starts idle pull workers only for work, and later work starts exactly what it needs.

MEFP declares fifteen detached ephemeral Luna implementers, and a launch with
no Luna work started all fifteen provider CLIs at once. These cases drive the
real decision (`worker_admission.plan`), the real admission child (`admit`)
against a launcher configuration of that shape with the board's reads stood
in for, its lock and record on disk, and the real worker-start phase of the
launch (`launch_phases._start_workers_and_present`), counting every provider
start it makes. The listener's half runs against a real board in
`demand_start_test`.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import worker_admission as wa  # noqa: E402

CHECKS = 0
ME = pwd.getpwuid(os.getuid()).pw_name
LUNAS = [f"luna-{n}" for n in range(1, 16)]
CORE = ["director", "main", "audit", "inspector"]


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def kinds(decisions):
    return {d.role: d.kind for d in decisions}


def started(decisions):
    return [d.role for d in decisions if d.kind in (wa.RECOVER, wa.DEMAND, wa.WARM)]


def ready(*pinned: str, unpinned: int = 0, blocked: int = 0):
    work = [{"id": f"MEFP-{n}", "claimable": True, "waiting_for": None} for n in range(unpinned)]
    work += [{"id": f"MEFP-P{n}", "claimable": True, "waiting_for": author} for n, author in enumerate(pinned)]
    work += [{"id": f"MEFP-B{n}", "claimable": False, "waiting_for": None} for n in range(blocked)]
    return work


# --- the decision ---------------------------------------------------------------


def test_the_plan() -> None:
    idle = wa.plan(LUNAS, ready_work=ready(blocked=3))
    check(started(idle) == [] and set(kinds(idle).values()) == {wa.IDLE} and len(idle) == 15,
          f"no claimable work: none of fifteen starts, and each says why: {kinds(idle)}")
    check([d.role for d in idle][:3] == ["luna-1", "luna-2", "luna-3"] and [d.role for d in idle][-1] == "luna-15",
          "in pool order, luna-2 before luna-10")
    one = wa.plan(LUNAS, ready_work=ready(unpinned=1))
    check(started(one) == ["luna-1"] and kinds(one)["luna-1"] == wa.DEMAND, f"one ready ticket: exactly one: {started(one)}")
    some = wa.plan(LUNAS, ready_work=ready(unpinned=3), running=["luna-9"])
    check(started(some) == ["luna-1", "luna-2"] and kinds(some)["luna-9"] == wa.RUNNING,
          f"three ready, one idle worker running: two more, not three: {started(some)}")
    starting = wa.plan(LUNAS, ready_work=ready(unpinned=2), starting=["luna-4"])
    check(started(starting) == ["luna-1"] and kinds(starting)["luna-4"] == wa.STARTING,
          f"a start under way is capacity, not started twice: {started(starting)}")
    work = wa.plan(LUNAS, ready_work=ready(), holders={"luna-3": "holds MEFP-7", "luna-11": "continues MEFP-5's conversation"})
    check(started(work) == ["luna-3", "luna-11"] and kinds(work)["luna-3"] == wa.RECOVER,
          f"active tickets and continuations recover, nothing else starts: {started(work)}")
    busy = wa.plan(LUNAS, ready_work=ready(unpinned=1), holders={"luna-1": "holds MEFP-7"}, running=["luna-1"])
    check(started(busy) == ["luna-2"] and kinds(busy)["luna-1"] == wa.RUNNING,
          f"a running worker holding a ticket is no idle capacity: {started(busy)}")
    pinned = wa.plan(LUNAS, ready_work=ready("luna-12"))
    check(started(pinned) == ["luna-12"] and "MEFP-P0" in [d for d in pinned if d.role == "luna-12"][0].detail,
          f"rework pinned to its author starts that author and no other: {started(pinned)}")
    respected = wa.plan(LUNAS[:4], ready_work=ready(unpinned=4), held=["luna-1"], operator_stopped=["luna-2"],
                        failed={"luna-3": "the pane command exited 1"})
    check(kinds(respected) == {"luna-1": wa.HELD, "luna-2": wa.OPERATOR, "luna-3": wa.FAILED, "luna-4": wa.DEMAND},
          f"quota holds, operator stops and failed starts are respected, and the work goes elsewhere: {kinds(respected)}")
    check(wa.plan(["luna-1"], ready_work=ready("luna-1"), held=["luna-1"])[0].kind == wa.HELD,
          "even rework pinned to a held worker does not start it")
    warm = wa.plan(LUNAS, ready_work=ready(), warm=2)
    check(started(warm) == ["luna-1", "luna-2"] and kinds(warm)["luna-1"] == wa.WARM,
          f"warm_workers keeps that many idle workers up: {started(warm)}")
    warm_busy = wa.plan(LUNAS, ready_work=ready(unpinned=1), warm=2, running=["luna-5"])
    check(started(warm_busy) == ["luna-1", "luna-2"] and kinds(warm_busy)["luna-1"] == wa.WARM
          and kinds(warm_busy)["luna-2"] == wa.WARM and kinds(warm_busy)["luna-5"] == wa.RUNNING,
          f"one ready ticket takes the running idle worker; two more are kept warm: {kinds(warm_busy)}")
    # Audit's case: a running worker the listener saw busy is no idle capacity.
    busy = wa.plan(["luna-1", "luna-2"], ready_work=ready(unpinned=1), running=["luna-1"], busy={"luna-1": "working"})
    check(busy == [wa.Decision("luna-1", wa.RUNNING, "busy: working"), wa.Decision("luna-2", wa.DEMAND, "1 ready ticket(s) unclaimed")],
          f"one ready ticket, luna-1 live but busy and holding nothing: luna-2 starts: {busy}")
    check(started(wa.plan(["luna-1", "luna-2"], ready_work=ready(unpinned=1), running=["luna-1"])) == [],
          "the same worker, idle: it is the capacity, and nothing starts")
    runtimes = {name: "claude" if int(name.split("-")[1]) <= 7 else "codex" for name in LUNAS}
    capped = wa.plan(LUNAS, ready_work=ready(unpinned=15), runtime_of=runtimes,
                     cohorts={"claude": {"target": 4, "ceiling": 4, "holding": 0},
                              "codex": {"target": 4, "ceiling": 4, "holding": 0}})
    check(started(capped) == ["luna-1", "luna-2", "luna-3", "luna-4", "luna-8", "luna-9", "luna-10", "luna-11"]
          and kinds(capped)["luna-15"] == wa.COHORT,
          f"fifteen ready tickets under MEFP's 4+4 cohorts: eight start, never more: {started(capped)}")
    running = wa.plan(LUNAS, ready_work=ready(unpinned=3), runtime_of=runtimes, running=["luna-1", "luna-2"],
                      holders={"luna-1": "x", "luna-2": "y"}, cohorts={"claude": {"target": 3, "ceiling": 9, "holding": 2}})
    check(started(running) == ["luna-3", "luna-8", "luna-9"],
          f"a cohort's running members count against its target: one more Claude, then uncohorted Codex: {started(running)}")
    ceiling = wa.plan(LUNAS, ready_work=ready("luna-2", unpinned=1), runtime_of=runtimes,
                      cohorts={"claude": {"target": 7, "ceiling": 1, "holding": 1}})
    check(kinds(ceiling)["luna-2"] == wa.COHORT and started(ceiling) == ["luna-8"],
          f"a cohort at its ceiling starts nobody to claim -- not even pinned rework's author: {kinds(ceiling)['luna-2']}")
    pins = wa.plan(LUNAS[:2], ready_work=ready(unpinned=1), off_pin={"luna-1": "effort high, the cohort wants xhigh"})
    check(kinds(pins) == {"luna-1": wa.OFF_PIN, "luna-2": wa.DEMAND}, f"an off-pin worker never starts: {kinds(pins)}")
    warm_capped = wa.plan(LUNAS, ready_work=ready(), warm=3, runtime_of=runtimes, cohorts={"claude": {"target": 1}},
                          running=["luna-1"])
    check(started(warm_capped) == ["luna-8", "luna-9"], f"warm workers respect a cohort's target too: {started(warm_capped)}")
    check(all(d.starts == (d.kind in (wa.RECOVER, wa.DEMAND, wa.WARM, wa.RUNNING)) for d in some + work + respected),
          "only recovery, demand, warm and running start a session")


# --- the child, on a MEFP-shaped configuration ------------------------------------


class Board:
    """The board's four reads, as a MEFP-shaped tenant's board answers them."""

    def __init__(self) -> None:
        self.ready: list = []
        self.holds: list = []
        self.recovery: dict[str, dict] = {}
        self.asked: list[str] = []
        self.fail = False
        self.cohorts: list = []  # declared in the workflow's pull policy
        self.cohort_status: list = []  # as the board reports them

    def __call__(self, url: str):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.asked.append("/" + path)
        if self.fail:
            raise OSError("connection refused")
        if path == "api/workflow":  # the board's own shape: the document under its revision
            return {"revision": 3, "document": {"scheduling": {"mode": "pull", "ready_stage": "ready", "release_after": "audit",
                                                              **({"cohorts": self.cohorts} if self.cohorts else {})},
                    "stages": [{"name": "in_progress", "kind": "implementation", "owners": ["main", *LUNAS]}],
                    "roles": [{"name": "main", "kind": "implementer", "active": True},
                              *({"name": name, "kind": "implementer", "active": True, "ephemeral": True,
                                 "runtime": "claude" if int(name.split("-")[1]) <= 7 else "codex"} for name in LUNAS)]}}
        if path == "api/reservations":
            return {"reservations": [], "pull_queue": {"enabled": True, "ready_work": self.ready}}
        if path == "api/provider-holds":
            return {"holds": self.holds}
        if path == "api/cohorts":
            return {"cohorts": self.cohort_status}
        role = path.removeprefix("api/context-recovery/")
        return self.recovery.get(role, {"action": "fresh", "ticket": None, "reason": f"{role} holds no active ticket"})


class Host:
    """The project account's tmux server and pane starts, counted."""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.core: set[str] = set(CORE)  # the project is up: its core roles have live sessions
        self.vanish_after: int | None = None  # the core sessions go after this many probes find them live
        self.starts: list[str] = []
        self.fail: set[str] = set()
        self.delay = 0.0
        self.killed: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, args, **kwargs):
        args = list(args)
        if "has-session" in args:
            session = args[args.index("-t") + 1].lstrip("=")
            if self.vanish_after is not None and any(session.endswith(f"-{role}") for role in self.core):
                self.vanish_after -= 1
                if self.vanish_after < 0:
                    self.core.clear()
            return subprocess.CompletedProcess(args, 0 if any(session.endswith(f"-{role}") for role in self.live | self.core)
                                               else 1)
        if "kill-session" in args:
            session = args[args.index("-t") + 1].lstrip("=")
            with self.lock:
                self.killed.append(session)
                self.live -= {role for role in self.live if session.endswith(f"-{role}")}
            return subprocess.CompletedProcess(args, 0)
        if "pane" in args and "attach-or-start" in args:
            role = args[args.index("attach-or-start") + 1]
            time.sleep(self.delay)
            with self.lock:
                self.starts.append(role)
                if role in self.fail:
                    return subprocess.CompletedProcess(args, 1)
                self.live.add(role)
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(f"unexpected command {args}")

    def observe(self, config, member, *, runner=None, watch=False):
        return ("running", f"its session is live at mefp-{member}:0.0") if member in self.live else \
            ("stopped", f"it has no live session mefp-{member}")


def mefp(tmp: Path) -> Path:
    roles = [{"role": name, "slot": n, "cli": ["claude"], "workdir": str(tmp / "work" / name)} for n, name in enumerate(CORE)]
    roles += [{"role": name, "detached": True, "ephemeral": True, "cli": ["codex"], "workdir": str(tmp / "work" / name)}
              for name in LUNAS]
    path = tmp / "mefp.json"
    path.write_text(json.dumps({"project": "mefp", "run_as_user": ME, "layout": str(tmp / "layout.json"),
                                "board_url": "http://127.0.0.1:9", "session_dir": str(tmp / "sessions"), "roles": roles}))
    return path


def admit(path: Path, board: Board, host: Host, mode: str, **kwargs):
    with patch("scripts.worker_pool.observe_worker", host.observe):
        return wa.admit("mefp", path, mode=mode, runner=host, getter=board, sleep=lambda _s: None, **kwargs)


def state(tmp: Path) -> dict:
    """The admission record; none written yet reads as an empty one."""
    path = tmp / "sessions" / wa.STATE_NAME
    return json.loads(path.read_text()) if path.exists() else {"starting": {}, "failed": {}, "operator_stopped": {}}


def test_the_admission_child() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd688.") as raw:
        tmp = Path(raw)
        path, board, host = mefp(tmp), Board(), Host()
        # A cold launch with no Luna work: nothing to start, and the reads that said so.
        answer = admit(path, board, host, "launch")
        check(answer["reason"] == "" and len(answer["decisions"]) == 15
              and all(d["kind"] == wa.IDLE for d in answer["decisions"]) and host.starts == [],
              f"no-work launch: fifteen left stopped, none started: {answer}")
        check(board.asked[:3] == ["/api/workflow", "/api/reservations", "/api/provider-holds"]
              and len([a for a in board.asked if a.startswith("/api/context-recovery/")]) == 15,
              f"the board's own reads, one recovery question per claimant: {board.asked[:5]}")
        # Recovery: an active ticket and a decided continuation.
        board.recovery = {"luna-3": {"action": "resume", "ticket": "MEFP-7", "session_id": "s7",
                                     "reason": "luna-3 holds MEFP-7"},
                          "luna-9": {"action": "resume", "ticket": "MEFP-4",
                                     "reason": "luna-9 continues MEFP-3's conversation, decided before the restart"},
                          "luna-10": {"action": "fresh", "ticket": "MEFP-8", "reason": "MEFP-8 has not been handed to luna-10 yet"}}
        answer = admit(path, board, host, "launch")
        kinds_now = {d["role"]: d["kind"] for d in answer["decisions"]}
        check([r for r, k in kinds_now.items() if k == wa.RECOVER] == ["luna-3", "luna-9", "luna-10"],
              f"active tickets, a continuation and a ticket not yet handed over recover: {kinds_now}")
        check(sorted(state(tmp)["starting"]) == ["luna-10", "luna-3", "luna-9"] and host.starts == [],
              "the launch records the starts it is about to make, and makes none itself")
        board.recovery = {}
        (tmp / "sessions" / wa.STATE_NAME).unlink()
        # Demand: one ticket arrives after launch.
        board.ready = ready(unpinned=1)
        answer = admit(path, board, host, "demand")
        check(host.starts == ["luna-1"] and answer["started"] == {"luna-1": ""} and state(tmp)["starting"] == {},
              f"one ready ticket: exactly one worker started, and its start no longer pending: {answer}")
        again = admit(path, board, host, "demand")
        check(host.starts == ["luna-1"] and again["started"] == {},
              "asked again before it claims: the running worker is the capacity; nothing more starts")
        # Concurrent demand: two passes at once, five ready tickets, never more than five workers.
        host.live.clear(), host.starts.clear()
        board.ready = ready(unpinned=3)
        host.delay = 0.3
        results = []
        # One patch around all three: a patch entered and left per thread restores the real probe under another.
        threads = [threading.Thread(target=lambda: results.append(wa.admit("mefp", path, mode="demand", runner=host,
                                                                           getter=board))) for _ in range(3)]
        with patch("scripts.worker_pool.observe_worker", host.observe):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        host.delay = 0.0
        check(sorted(host.starts) == ["luna-1", "luna-2", "luna-3"] and len(set(host.starts)) == 3,
              f"three concurrent asks for three tickets start three different workers once each: {host.starts}")
        check(all(len(r["started"]) <= wa.MAX_STARTS_PER_PASS for r in results), "no ask starts more than its limit")
        # The lock itself: an ask waits while another holds it, and then counts the other's start as capacity.
        host.live.clear(), host.starts.clear()
        board.ready = ready(unpinned=2)
        with patch("scripts.worker_pool.observe_worker", host.observe):
            with wa.locked_state(tmp / "sessions") as held_state:
                waiting = threading.Thread(target=lambda: results.append(
                    wa.admit("mefp", path, mode="demand", runner=host, getter=board)))
                waiting.start()
                time.sleep(0.5)
                check(waiting.is_alive() and host.starts == [], "an ask waits for the lock another holds, starting nothing")
                held_state["starting"]["luna-1"] = time.time()  # the holder's own start, in flight
            waiting.join()
        check(host.starts == ["luna-2"], f"then the other's start counts: two tickets, one more worker: {host.starts}")
        (tmp / "sessions" / wa.STATE_NAME).unlink()
        # A failed start: recorded, not retried during its backoff, and the work goes to another.
        host.live.clear(), host.starts.clear()
        board.ready = ready(unpinned=1)
        host.fail = {"luna-1"}
        failed = admit(path, board, host, "demand")
        check(failed["started"] == {"luna-1": "the pane command exited 1, and it has no live session mefp-luna-1"}
              and "luna-1" in state(tmp)["failed"], f"a failed start is reported and recorded: {failed}")
        retry = admit(path, board, host, "demand")
        check(host.starts == ["luna-1", "luna-2"] and {d["role"]: d["kind"] for d in retry["decisions"]}["luna-1"] == wa.FAILED,
              f"the next ask leaves the failed worker for its backoff and starts another: {host.starts}")
        later = admit(path, board, host, "demand", now=lambda: time.time() + wa.FAILURE_BACKOFF_SECONDS + 1)
        check({d["role"]: d["kind"] for d in later["decisions"]}["luna-1"] != wa.FAILED,
              "after the backoff it may be tried again")
        host.fail = set()
        # Quota holds and operator stops.
        host.live.clear(), host.starts.clear()
        board.holds = [{"role": "luna-1", "current": True}, {"role": "luna-2", "current": False}]
        wa.record("mefp", path, operator_stopped=["luna-2"])
        admit(path, board, host, "demand")
        check(host.starts == ["luna-3"], f"held and operator-stopped workers stay down; the next one starts: {host.starts}")
        wa.record("mefp", path, operator_started=["luna-2"])
        board.holds = []
        check("luna-2" not in state(tmp)["operator_stopped"], "an operator's start clears its stop")
        # A stopped project -- none of its core roles has a session -- starts nothing for demand.
        host.live.clear(), host.starts.clear()
        host.core.clear()
        stopped = admit(path, board, host, "demand")
        check(stopped["reason"] == "the project is stopped: none of its core roles has a live session" and host.starts == [],
              f"a stopped project starts nothing for demand: {stopped}")
        host.core.add("director")
        admit(path, board, host, "demand")
        check(len(host.starts) == 1, f"with its Director up again, one worker starts for the ticket: {host.starts}")
        host.live.clear(), host.starts.clear()
        (tmp / "sessions" / wa.STATE_NAME).unlink(missing_ok=True)
        admit(path, board, host, "launch")
        check(list(state(tmp)["starting"]) == ["luna-1"] and host.starts == [],
              f"a launch records the start it is about to make for the ready ticket, and makes none itself: {state(tmp)}")
        # A board that cannot answer, asked three times: every Luna left stopped, none started blind (Audit).
        board.fail = True
        board.asked.clear()
        failed = admit(path, board, host, "launch")
        check(board.asked.count("/api/workflow") == 3 and "could not say which pull workers have work" in failed["reason"]
              and len(failed["decisions"]) == 15 and all(d["kind"] == wa.UNDECIDED for d in failed["decisions"]),
              f"no answer after three asks: fifteen left stopped, undecided, and said why: {failed['reason']}")
        check(admit(path, board, host, "demand") == {"reason": failed["reason"], "decisions": []} and host.starts == [],
              "and a demand ask without the board starts nothing")
        board.fail = False
        # MEFP's cohorts: four Claude and four Codex at most, whatever the work.
        (tmp / "sessions" / wa.STATE_NAME).unlink(missing_ok=True)
        host.live.clear(), host.starts.clear()
        board.cohorts = [{"runtime": "claude", "target": 4, "ceiling": 4}, {"runtime": "codex", "target": 4, "ceiling": 4}]
        board.cohort_status = [{"runtime": "claude", "effective_ceiling": 4, "holding": []},
                               {"runtime": "codex", "effective_ceiling": 2, "holding": ["luna-14", "luna-15"]}]
        board.ready = ready(unpinned=12)
        answer = admit(path, board, host, "launch")
        kinds_now = {d["role"]: d["kind"] for d in answer["decisions"]}
        check([r for r, k in kinds_now.items() if k == wa.DEMAND] == ["luna-1", "luna-2", "luna-3", "luna-4"]
              and kinds_now["luna-5"] == wa.COHORT and kinds_now["luna-8"] == wa.COHORT,
              f"twelve tickets: four Claude workers (its target) and no Codex one, whose quota-cut ceiling is held: {kinds_now}")
        check("codex: holds 2 ticket(s), ceiling 2" in [d for d in answer["decisions"] if d["role"] == "luna-8"][0]["detail"]
              and "claude: 4 running, target 4" in [d for d in answer["decisions"] if d["role"] == "luna-5"][0]["detail"],
              "each says which limit holds it")
        board.cohorts, board.cohort_status, board.ready = [], [], ready()
        (tmp / "sessions" / wa.STATE_NAME).unlink(missing_ok=True)
        # A stopped holder -- a launch that could not decide, or a worker that died -- is recovered by demand, first.
        host.live.clear(), host.starts.clear()
        board.ready = ready(unpinned=2)
        board.recovery = {"luna-3": {"action": "resume", "ticket": "MEFP-7", "session_id": "s", "reason": "luna-3 holds MEFP-7"}}
        answer = admit(path, board, host, "demand")
        check(host.starts == ["luna-3", "luna-1"] and {d["role"]: d["kind"] for d in answer["decisions"]}["luna-3"] == wa.RECOVER,
              f"demand recovers the stopped holder before new capacity, within its two starts: {host.starts}")
        board.recovery = {}
        (tmp / "sessions" / wa.STATE_NAME).unlink(missing_ok=True)

        # Audit's race: an ask already deciding when the project is stopped starts nothing after the stop.
        host.live.clear(), host.starts.clear()
        board.ready = ready(unpinned=1)
        raced: list = []
        with patch("scripts.worker_pool.observe_worker", host.observe):
            with wa.locked_state(tmp / "sessions"):
                asking = threading.Thread(target=lambda: raced.append(
                    wa.admit("mefp", path, mode="demand", runner=host, getter=board, sleep=lambda _s: None)))
                asking.start()
                time.sleep(0.5)
                check(asking.is_alive(), "the ask is in flight, waiting on the lock")
                host.core.clear()  # the stop's sweep: every session gone, with no record written
            asking.join()
        check(host.starts == [] and raced[0]["reason"] == "the project is stopped: none of its core roles has a live session",
              f"after the sweep, the in-flight ask starts nothing: {host.starts} {raced}")
        host.core = set(CORE)
        # The same race through `stop`'s own record: written under the lock, so it wins over an ask that waited.
        with patch("scripts.worker_pool.observe_worker", host.observe):
            with wa.locked_state(tmp / "sessions") as held_state:
                asking = threading.Thread(target=lambda: raced.append(
                    wa.admit("mefp", path, mode="demand", runner=host, getter=board, sleep=lambda _s: None)))
                asking.start()
                time.sleep(0.5)
                held_state["stopped"] = time.time()  # what `stopped` writes once it holds the lock
            asking.join()
        check(host.starts == [] and raced[1]["reason"] == "the project was stopped", f"a recorded stop wins: {raced[1]}")
        # Between the decision and the start: the sessions go after the decision found the project up. The start,
        # asking again under its own lock, starts nothing.
        state_path = tmp / "sessions" / wa.STATE_NAME
        recorded = json.loads(state_path.read_text())
        recorded.pop("stopped", None)
        state_path.write_text(json.dumps(recorded))
        host.vanish_after = 1  # the decision's probe finds the Director; the next probe does not
        between = admit(path, board, host, "demand")
        host.vanish_after, host.core = None, set(CORE)
        check(host.starts == [] and list(between["started"].values()) == [
            "not started: the project is stopped: none of its core roles has a live session"],
              f"a stop between decision and start: nothing starts, and the ask says why: {between['started']}")
        # A start already under way when `stop` records: the record waits for it, so the sweep after ends it.
        del held_state
        (tmp / "sessions" / wa.STATE_NAME).unlink()
        host.delay = 1.0
        order: list[str] = []
        with patch("scripts.worker_pool.observe_worker", host.observe):
            asking = threading.Thread(target=lambda: raced.append(
                wa.admit("mefp", path, mode="demand", runner=host, getter=board, sleep=lambda _s: None)))
            asking.start()
            time.sleep(0.4)  # inside the pane start, holding the lock
            stopping = subprocess.run([sys.executable, str(Path(wa.__file__).resolve()), "stopped", "--session-dir",
                                       str(tmp / "sessions")], capture_output=True, text=True)
            order.append(f"stop recorded, starts {list(host.starts)}")
            asking.join()
        host.delay = 0.0
        check(stopping.returncode == 0 and order == ["stop recorded, starts ['luna-1']"] and state(tmp).get("stopped"),
              f"stop's record, from its command line, waited for the start under way: {order} {stopping.stderr[-200:]}")
        host.live.clear(), host.starts.clear()
        check(admit(path, board, host, "demand")["reason"] == "the project was stopped" and host.starts == [],
              "after it, demand starts nothing")
        admit(path, board, host, "launch")
        check("stopped" not in state(tmp), "and the next launch clears the stop")


def test_the_launch_starts_only_what_admission_admits() -> None:
    """The real worker-start phase, counting provider starts, with the admission child's answer from a MEFP board."""
    from scripts import launch_phases, team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix="syrd688-launch.") as raw:
        tmp = Path(raw)
        path, board, host = mefp(tmp), Board(), Host()
        config = launcher.load_project_config("mefp", path)
        config = launcher.replace(config, roles=[launcher.replace(role, detached=True) if role.role in CORE else role
                                                 for role in config.roles])
        children, starts, said = [], [], []

        def runner(args, **kwargs):
            args = list(args)
            if "admit" in args:  # the admission child, run here in-process as the project account
                children.append(args)
                with patch("scripts.worker_pool.observe_worker", host.observe):
                    answer = wa.admit("mefp", path, mode=args[args.index("--mode") + 1], runner=host, getter=board,
                                      sleep=lambda _s: None)
                return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")
            return subprocess.CompletedProcess(args, 0, "", "")

        def detached(role, **kwargs):
            starts.append(role.role)
            return 0

        def phase(**overrides):
            starts.clear(), said.clear()
            failed: dict[str, str] = {}
            with patch.object(launcher, "run_detached_role", detached), \
                    patch.object(launcher, "resolve_layout_mode", lambda *a, **k: "konsole"), \
                    patch("scripts.presentation_controller.presentation_enabled", lambda *a, **k: False), \
                    patch.object(launcher, "launch_konsole_layout", lambda *a, **k: 0, create=True):
                began = time.monotonic()
                try:
                    result = launch_phases._start_workers_and_present(
                        config, allow_stale_launcher=False, config_path=path, delegate_role_sessions_to_owner=False,
                        effective_pane_state_dir=tmp / "panes", failed_roles=failed, force_reload=False,
                        konsole_process_launcher=None, layout_environ={}, layout_mode="konsole", layout_output=None,
                        mode=overrides.get("mode", "start"), output_path=tmp / "layout.json",
                        pane_script_path=tmp / "team-launcher", print_func=said.append, role_process_runner=runner,
                        runner=runner, window_title="mefp")
                except Exception as exc:  # noqa: BLE001 - the presentation half is not this test's
                    result = exc
                return result, time.monotonic() - began

        result, seconds = phase()
        check(sorted(starts) == sorted(CORE) and not any(name in starts for name in LUNAS),
              f"a no-work MEFP launch starts the core roles and not one of fifteen Lunas: {starts}")
        check(sum(line.endswith(f"luna-{n}: {wa.IDLE}") for line in said for n in range(1, 16)) == 15,
              f"and says, once per Luna, that it is left stopped because no work needs it: {said[:3]}")
        check(len(children) == 1 and children[0][:2] == [sys.executable, str(Path(wa.__file__).resolve())]
              and seconds < 5, f"one admission child, as the project account, quickly ({seconds:.2f}s): {children}")
        deferred = getattr(result, "deferred_roles", None)
        check(isinstance(result, launch_phases.WorkerStartup) and sorted(deferred) == sorted(LUNAS),
              f"the Lunas left stopped are handed to the launch's report, which waits for no session of theirs: {deferred}")
        board.recovery = {"luna-4": {"action": "resume", "ticket": "MEFP-9", "session_id": "s9", "reason": "luna-4 holds MEFP-9"}}
        board.ready = ready("luna-8", unpinned=2)
        (tmp / "sessions" / wa.STATE_NAME).unlink(missing_ok=True)
        phase()
        check(sorted(set(starts) - set(CORE)) == ["luna-1", "luna-2", "luna-4", "luna-8"],
              f"recovery, pinned rework and two ready tickets: exactly four Lunas: {sorted(set(starts) - set(CORE))}")
        check(f"switchyard: luna-4: {wa.RECOVER} (luna-4 holds MEFP-9)" in said
              and any(line.startswith(f"switchyard: luna-1: {wa.DEMAND}") for line in said),
              f"each said plainly: {[line for line in said if 'luna-' in line][:4]}")
        board.fail = True
        phase()
        check(sorted(starts) == sorted(CORE) and any("left stopped rather than all started" in line for line in said),
              f"a board that cannot answer: the core starts and not one Luna, and the launch says why: {said[:2]}")
        board.fail = False
        children.clear()
        phase(mode="reload")
        check(children == [] and sorted(starts) == sorted(CORE + LUNAS), "a reload is not admission's: it asks nothing")


def test_the_child_runs_as_a_real_process() -> None:
    """for_launch runs the child for real -- its own interpreter, argv and JSON -- against a board over HTTP."""
    import http.server

    from scripts import team_launcher as launcher

    board = Board()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(board(f"http://board{self.path}")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="syrd688-child.") as raw:
            tmp = Path(raw)
            path = mefp(tmp)
            document = json.loads(path.read_text())
            document["board_url"] = f"http://127.0.0.1:{server.server_port}"
            # A private tmux socket directory: the child's has-session probes ask a server that is not running.
            path.write_text(json.dumps(document))
            config = launcher.load_project_config("mefp", path)
            board.recovery = {"luna-5": {"action": "resume", "ticket": "MEFP-2", "session_id": "s2", "reason": "luna-5 holds MEFP-2"}}
            board.ready = ready(unpinned=1)
            said: list[str] = []
            env = {**os.environ, "TMUX_TMPDIR": str(tmp / "tmux")}
            (tmp / "tmux").mkdir()
            decisions = wa.for_launch(config, config_path=path, mode="start", print_func=said.append,
                                      runner=lambda args, **kw: subprocess.run(args, env=env, **kw))
            check({name: d.kind for name, d in decisions.items() if d.starts} == {"luna-1": wa.DEMAND, "luna-5": wa.RECOVER}
                  and len(decisions) == 15 and "switchyard: luna-5: active-ticket recovery (luna-5 holds MEFP-2)" in said,
                  f"the real child answers the launch: {said[:3]}")
            check(sorted(state(tmp)["starting"]) == ["luna-1", "luna-5"], "and recorded the starts, as the project account")
            done = subprocess.run([sys.executable, str(Path(wa.__file__).resolve()), "record", "mefp", "--config", str(path),
                                   "--operator-stopped", "luna-9", "luna-10"], capture_output=True, text=True, env=env)
            check(done.returncode == 0 and sorted(state(tmp)["operator_stopped"]) == ["luna-10", "luna-9"],
                  f"record, from its command line: {done.stderr[-300:]}")
            server.shutdown()
            said.clear()
            decisions = wa.for_launch(config, config_path=path, mode="start", print_func=said.append,
                                      runner=lambda args, **kw: subprocess.run(args, env=env, **kw))
            check(len(decisions) == 15 and all(d.kind == wa.UNDECIDED for d in decisions.values())
                  and "Every detached ephemeral worker is left stopped rather than all started" in said[0]
                  and "switchyard mefp" in said[0], f"a board that is gone: every Luna left stopped, said: {said}")
            said.clear()
            broken = wa.for_launch(config, config_path=path, mode="start", print_func=said.append,
                                   runner=lambda args, **kw: subprocess.CompletedProcess(args, 1, "Traceback ...", ""))
            check(len(broken) == 15 and all(d.kind == wa.UNDECIDED for d in broken.values())
                  and "admission could not run" in said[0], f"a child that fails: the same bounded answer: {said}")
    finally:
        server.server_close()


def test_the_report_waits_for_no_worker_left_stopped() -> None:
    """P9: a Luna the launch left stopped has no session to wait for, and keeps its provider-state record."""
    from scripts import launch_phases, team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix="syrd688-report.") as raw:
        tmp = Path(raw)
        path = mefp(tmp)
        config = launcher.load_project_config("mefp", path)
        reported, recorded = [], []

        def session_records(config_, roles=None, **kwargs):
            reported.append([role.role for role in (roles or config_.roles)])
            return []

        with patch.object(launcher, "report_launch_session_records", session_records), \
                patch.object(launcher, "record_provider_state_generation", lambda config_, role, generation: recorded.append(role.role)), \
                patch.object(launcher, "provider_state_generation", lambda cli, owner_home: "g1"):
            for deferred in ((), tuple(LUNAS[1:])):
                reported.clear(), recorded.clear()
                launch_phases._report_launch(
                    config, config_path=path, effective_pane_state_dir=tmp / "panes", failed_roles={},
                    launch_started_at=0.0, launch_started_ns=0, mode="attach-or-start", print_func=lambda _l: None,
                    reconcile_home=tmp, report_session_records=True, resolved_layout_mode="konsole", running_roles=[],
                    session_record_poll=0.01, session_record_timeout=0.01, unreconciled_roles=set(),
                    worker_start_exit_code=0, deferred_roles=deferred)
                expected = [role.role for role in config.roles if role.role not in deferred]
                check(reported == [expected] and recorded == expected,
                      f"deferred {len(deferred)}: the session report and provider-state records cover exactly the "
                      f"roles that came up: {reported} {recorded}")


def test_stop_records_first_as_the_project_account() -> None:
    """`stop_project` writes admission's stop record before it touches a session, through the real child."""
    from scripts import presentation_controller, project_stop, team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix="syrd688-stop.") as raw:
        tmp = Path(raw)
        path = mefp(tmp)
        config = launcher.load_project_config("mefp", path)
        order: list[str] = []

        def runner(args, **kwargs):
            args = [str(part) for part in args]
            if len(args) > 2 and args[1] == str(Path(wa.__file__).resolve()):
                order.append("record " + " ".join(args[2:]))
                return subprocess.run(args, capture_output=True, text=True)
            order.append(args[1] if args[:1] == ["tmux"] else args[0])
            return subprocess.CompletedProcess(args, 1, "", "")

        with patch.object(presentation_controller, "stop_presentation", lambda *a, **k: order.append("presentation") or 0), \
                patch.object(launcher, "stop_role_sessions", lambda *a, **k: order.append("sessions") or 0):
            project_stop.stop_project(config, runner=runner, print_func=lambda _l: None)
        check(order[0] == f"record stopped --session-dir {tmp / 'sessions'}" and order[-1] == "sessions"
              and state(tmp).get("stopped"), f"the stop is recorded first, then every session is stopped: {order}")


def test_a_stop_that_cannot_record_itself() -> None:
    """Audit's case: the stop marker's child fails while a demand start is under way. The stop says so, nonzero,
    and no worker is left running after it."""
    from scripts import presentation_controller, project_stop, team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix="syrd688-unrecorded.") as raw:
        tmp = Path(raw)
        path = mefp(tmp)
        config = launcher.load_project_config("mefp", path)
        board, host = Board(), Host()
        board.ready = ready(unpinned=1)
        said: list[str] = []
        sweeps: list[str] = []

        def runner(args, **kwargs):
            args = [str(part) for part in args]
            if len(args) > 2 and args[1] == str(Path(wa.__file__).resolve()):
                return subprocess.CompletedProcess(args, 17, "", "stop marker refused")
            return subprocess.CompletedProcess(args, 1, "", "")

        def sweep(*_a, **_k):
            with host.lock:
                sweeps.append(f"sweep, live {sorted(host.live)}")
                host.core.clear(), host.live.clear()
            return 0

        host.delay = 1.0
        asked: list = []
        with patch("scripts.worker_pool.observe_worker", host.observe):
            asking = threading.Thread(target=lambda: asked.append(
                wa.admit("mefp", path, mode="demand", runner=host, getter=board, sleep=lambda _s: None)))
            asking.start()
            time.sleep(0.4)  # the demand child is inside its pane start, past every check before it
            with patch.object(presentation_controller, "stop_presentation", lambda *a, **k: 0), \
                    patch.object(launcher, "stop_role_sessions", sweep):
                answer = project_stop.stop_project(config, runner=runner, print_func=said.append)
            asking.join()
        check(answer != 0 and "stopped" not in state(tmp),
              f"the marker child failed: the stop answers nonzero and records nothing: {answer}")
        check(answer == 1 and any("could not record the stop for worker admission (exit 17: stop marker refused)" in line
                                  for line in said)
              and any("the stop is not clean" in line and "switchyard stop mefp" in line for line in said),
              f"and says plainly that the stop is not clean, and how to finish it: {said}")
        check(len(sweeps) == 2, f"every role session swept twice: {sweeps}")
        check(host.starts == ["luna-1"] and host.live == set() and host.killed == ["mefp-luna-1"]
              and asked[0]["started"]["luna-1"].startswith("not started: the project is stopped"),
              f"the start under way finished after the sweeps, found no core session and ended its worker: "
              f"{host.live} {host.killed} {asked[0]['started']}")

        # A start that lands mid-sweep, while a core session is still live: it keeps its worker, so the second
        # sweep is what ends it.
        host.core, host.starts, host.killed, sweeps[:] = set(CORE), [], [], []

        def slow_sweep(*_a, **_k):
            with host.lock:
                sweeps.append(f"sweep, live {sorted(host.live)}")
                host.live.clear()
            if len(sweeps) == 1:
                deadline = time.monotonic() + 5
                while "luna-1" not in host.live and time.monotonic() < deadline:
                    time.sleep(0.02)
                asking.join(5)  # its check after the start found the core up
                with host.lock:
                    host.core.clear()
            return 0

        host.delay = 0.6
        asked.clear()
        with patch("scripts.worker_pool.observe_worker", host.observe):
            asking = threading.Thread(target=lambda: asked.append(
                wa.admit("mefp", path, mode="demand", runner=host, getter=board, sleep=lambda _s: None)))
            asking.start()
            time.sleep(0.3)
            with patch.object(presentation_controller, "stop_presentation", lambda *a, **k: 0), \
                    patch.object(launcher, "stop_role_sessions", slow_sweep):
                answer = project_stop.stop_project(config, runner=runner, print_func=said.append)
            asking.join()
        check(answer == 1 and asked[0]["started"] == {"luna-1": ""} and sweeps == ["sweep, live []", "sweep, live ['luna-1']"]
              and host.live == set(), f"the worker that landed mid-sweep is ended by the second sweep: {sweeps} {asked}")

        # A marker child that cannot even be run: the same, not a crash that skips the sweep.
        def unrunnable(args, **kwargs):
            if len(args) > 2 and str(args[1]) == str(Path(wa.__file__).resolve()):
                raise OSError("no such account")
            return subprocess.CompletedProcess(args, 1, "", "")

        sweeps.clear(), said.clear()
        with patch.object(presentation_controller, "stop_presentation", lambda *a, **k: 0), \
                patch.object(launcher, "stop_role_sessions", sweep):
            try:
                answer = project_stop.stop_project(config, runner=unrunnable, print_func=said.append)
            except Exception as exc:  # noqa: BLE001 - a stop that raises is the failure being checked for
                answer = f"raised {type(exc).__name__}: {exc}"
        check(answer == 1 and len(sweeps) == 2 and any("(OSError: no such account)" in line for line in said),
              f"a marker child that cannot run: swept twice, said, and nonzero: {answer} {sweeps} {said}")


def test_operator_stops_are_recorded_and_cleared() -> None:
    """`worker-pool stop`/`start` and converge record the operator's choice, as the project account, before acting."""
    from scripts import team_launcher as launcher, worker_cohorts

    with tempfile.TemporaryDirectory(prefix="syrd688-operator.") as raw:
        tmp = Path(raw)
        path = mefp(tmp)
        config = launcher.load_project_config("mefp", path)
        order: list[str] = []

        def runner(args, **kwargs):
            args = [str(part) for part in args]
            check(args[1] == str(Path(wa.__file__).resolve()) and args[2] == "record", f"only the record runs here: {args}")
            order.append(" ".join(args[args.index("--config") + 2:]))
            return subprocess.CompletedProcess(args, wa.main(args[2:]), "", "")

        stopping, starting = worker_cohorts.operator_recorded(
            config, path, runner=runner, print_func=lambda _l: None,
            stop=lambda member: order.append(f"stop {member}"), start=lambda member: order.append(f"start {member}"))
        stopping("luna-2")
        check(order == ["--operator-stopped luna-2", "stop luna-2"] and "luna-2" in state(tmp)["operator_stopped"],
              f"a converge stop is recorded first, then made: {order}")
        starting("luna-2")
        check(order[2:] == ["--operator-started luna-2", "start luna-2"] and state(tmp)["operator_stopped"] == {},
              f"and its start clears it: {order}")

    # The worker-pool command itself, on the restart suite's sandbox with its workers ephemeral.
    sys.path.insert(0, str(ROOT / "tests"))
    import worker_pool_restart_status_test as restart

    class Host(restart.Host):
        def __call__(self, args, **kwargs):
            command = [str(part) for part in args]
            if len(command) > 2 and command[1] == str(Path(wa.__file__).resolve()):
                self.records.append(command[command.index("--config") + 2:])
                return subprocess.CompletedProcess(command, wa.main(command[2:]), "", "")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="syrd688-pool.") as raw:
        home = os.environ.get("HOME")
        os.environ["HOME"] = raw
        host = None
        try:
            host = Host(Path(raw))
            document = json.loads(host.config_path.read_text())
            for role in document["roles"]:
                role["ephemeral"] = role.get("detached", False)
            document["session_dir"] = str(Path(raw) / "sessions")
            host.config_path.write_text(json.dumps(document))
            host.records = []
            code, said = host.run("stop")
            recorded = state(Path(raw))
            check(code == 0 and host.records == [["--operator-stopped", "impl-1"]] and "impl-1" in recorded["operator_stopped"],
                  f"worker-pool stop records the operator's stop: {code} {host.records} {said}")
            code, said = host.run("start")
            recorded = state(Path(raw))
            check(host.records[1:] == [["--operator-started", "impl-1"]] and recorded["operator_stopped"] == {},
                  f"and worker-pool start clears it: {host.records} {said}")
        finally:
            if host is not None:
                host.close()
            if home is not None:
                os.environ["HOME"] = home


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"worker_admission_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
