#!/usr/bin/env python3
"""SYRD-882: long healthy test runs stop paging the Director; sweeps and mutation plans declare budgets scaled to them.

The User: "seems like those nudges are more bother than help". In one evening the Director was told eight times that
a command of main's had run past its lease, past the 15-minute default observation threshold: each a healthy
`tests/sweep.py` sweep or a mutation driver. None was a hang.

Now:
- **A sweep declares what its own guards allow:** ceil(suites / jobs) x timeout, twice for a retry, plus one timeout,
  for its session, as `switchyard-bounded --expect` does (the shared `execution_lease.SessionLease`), withdrawn when
  it ends.
- **A mutation plan runs under `switchyard-bounded --mutants N --clean DUR`:** N x max(60s, 10 x clean), SYRD-845's
  per-case bound for every mutant.
- **The board** then hears nothing while either is within its budget, and once when it outlives it. A budget over the
  board's cap is capped, and the run says so. An undeclared command still gets the default threshold.

Here, real processes, the real /proc, the real `tests/sweep.py` and `switchyard-bounded`, and the listener's real
lease pass with the board's few calls stood in (tests/execution_lease_nested_test.py's `Board`). The default
threshold is made tiny, so anything undeclared is past it at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts.ticket_board import execution_lease  # noqa: E402
from execution_lease_nested_test import Board, defaults, role_pass  # noqa: E402

SWEEP = ROOT / "tests" / "sweep.py"
WRAPPER = ROOT / "scripts" / "switchyard-bounded"
SUITE = "import time\ntime.sleep({seconds})\n"
#: What the cases start: `sleep` for these durations (the wrapped plans, the undeclared command, a refused plan), and
#: the fake suites of the sweeps, by their file names.
MARKERS = ("882.7", "884.9", "882.5", "885.1", "syrd882_sleep_")
CHECKS = 0


class OnceBoard(Board):
    """The stand-in board, telling once per lease and kind, as `note_execution_lease_alert` answers on a real board
    ("once per deadline: the database says whether this one was already told")."""

    def __init__(self) -> None:
        super().__init__()
        self.told_keys: set = set()

    def execute(self, sql: str, params: tuple = ()):
        if "note_execution_lease_alert" in sql:
            key = (params[0], "expired" if "'expired'" in sql else "stale")
            if key in self.told_keys:
                return super().execute("SELECT nothing", ())  # no row: not told again
            self.told_keys.add(key)
        return super().execute(sql, params)


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


#: The sessions this run's tool commands were started in: what `leftovers` may count, and nothing else on the host.
SESSIONS: set[int] = set()


def tool(argv: list[str], state_dir: Path, log: Path, environ: dict[str, str] | None = None) -> subprocess.Popen:
    """A command in a session of its own, as a provider's tool starts one, with the pane-state directory a pane has."""
    proc = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log.open("w"),
                            stderr=subprocess.STDOUT, preexec_fn=defaults,
                            env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state_dir), **(environ or {})})
    SESSIONS.add(proc.pid)  # start_new_session: the command leads its own session, and its orphans keep that id
    return proc


def declared(state_dir: Path, proc: subprocess.Popen, wait: float = 15.0) -> dict | None:
    """The raw declaration the command's session carries, once it has written one."""
    leader = execution_lease.start_ticks(proc.pid)
    path = execution_lease.lease_dir(state_dir) / (execution_lease.session_name(proc.pid, leader) + ".json")
    until = time.time() + wait
    while time.time() < until and proc.poll() is None:
        record = execution_lease._read_json(path)
        if record is not None:
            return record
        time.sleep(0.1)
    return None


def told(board: Board, proc: subprocess.Popen) -> int:
    """How many times the Director was told about `proc`'s session."""
    ids = {row["id"] for row in board.leases.values() if row["pid"] == proc.pid}
    return sum(1 for lease_id, _what in board.alerts if lease_id in ids)


def in_session(session: int) -> list[int]:
    """Live processes of a session: everything a tool command started, its own groups included."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if len(fields) > 3 and fields[0] != "Z" and fields[3] == str(session):
            found.append(int(entry.name))
    return found


def stop(proc: subprocess.Popen) -> None:
    """End a tool command and everything it started (SYRD-885). SIGTERM first: switchyard-bounded stops its command's
    own process group on it -- a SIGKILL of the wrapper's group alone left that group running. Then whatever is still
    in the tool's session, which takes in a sweep's suites too."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            pass
    for _ in range(50):
        left = in_session(proc.pid)
        if not left:
            break
        for pid in left:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        time.sleep(0.1)
    proc.poll() if proc.returncode is not None else proc.wait(10)


def leftovers(*markers: str) -> list[str]:
    """Live processes this run started and must not leave behind: in a session one of its tool commands began (an
    orphan keeps its session), a `sleep` of one of these durations, or a process running one of the fake suite files.
    Read from each argv, so no shell that merely mentions them counts; and only this run's sessions, so another run's
    copy of a marker elsewhere on the host never does (SYRD-885, Audit's kick-back)."""
    durations = {marker for marker in markers if marker[:1].isdigit()}
    prefixes = tuple(marker for marker in markers if not marker[:1].isdigit())
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            state = (entry / "stat").read_text()
        except OSError:
            continue
        fields = state[state.rfind(")") + 2:].split()
        if fields[:1] == ["Z"] or len(fields) < 4 or int(fields[3]) not in SESSIONS or not argv or not argv[0]:
            continue
        sleeper = Path(argv[0]).name == "sleep" and len(argv) > 1 and argv[1] in durations
        suite = prefixes and any(Path(arg).name.startswith(prefixes) for arg in argv[1:])
        if sleeper or suite:
            found.append(" ".join(argv)[:120])
    return found


def sweep_tree(root: Path, seconds: float) -> list[str]:
    (root / "tests").mkdir(parents=True)
    names = []
    for index in range(3):
        name = f"syrd882_sleep_{index}_test"
        (root / "tests" / f"{name}.py").write_text(SUITE.format(seconds=seconds))
        names.append(name)
    return names


def case(tmp: Path, label: str, argv: list[str], *, cap: str = "") -> dict:
    """Run one command as a tool would, beside an undeclared one, and pass the listener's lease pass over both."""
    state_dir = tmp / f"state-{label}"
    state_dir.mkdir()
    environ = {"TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS": cap} if cap else {}
    previous = os.environ.get("TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS")
    if cap:  # the listener reads the cap from its own environment
        os.environ["TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS"] = cap
    log = tmp / f"{label}.log"
    proc = tool(argv, state_dir, log, environ)
    plain = tool(["sleep", "882.5"], state_dir, tmp / f"{label}-plain.log")  # nobody bounded it
    board, seen = OnceBoard(), {}
    try:
        record = declared(state_dir, proc)
        first = []
        for _ in range(3):  # inside the budget: well past the default threshold, and nothing to say about it
            role_pass(board, state_dir, observe_seconds=0.5, every=0.0, seen=seen)
            first.append(told(board, proc))
            time.sleep(0.5)
        if cap:
            time.sleep(3.5)  # past the capped budget, the command still running
        later = []
        for _ in range(3):
            role_pass(board, state_dir, observe_seconds=0.5, every=0.0, seen=seen)
            later.append(told(board, proc))
            time.sleep(0.3)
        result = {"record": record, "within": first, "after": later, "plain": told(board, plain),
                  "running": proc.poll() is None}
    finally:
        stop(plain)
        stop(proc)
        if cap:
            if previous is None:
                os.environ.pop("TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS", None)
            else:
                os.environ["TICKET_BOARD_LEASE_MAX_DECLARED_SECONDS"] = previous
    result["log"] = log.read_text(errors="replace")
    result["withdrawn"] = not any(execution_lease.lease_dir(state_dir).glob(f"{proc.pid}-*.json"))
    result["leftovers"] = leftovers(*MARKERS)
    check(result["leftovers"] == [], f"{label}: nothing the case started outlives it: {result['leftovers']}")
    return result


def budget_of(record: dict | None) -> float:
    return round(float(record["deadline"]) - float(record["declared_at"])) if record else -1.0


def main() -> int:
    # A decoy: a marker process outside this run, in a session of its own, as another role's run or an older tree's
    # leak would be. It must neither fail the suite nor be stopped by it.
    decoy = subprocess.Popen(["sleep", "882.7"], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return run_suite(decoy)
    finally:
        if decoy.poll() is None:
            decoy.kill()
        decoy.wait(10)


def run_suite(decoy: subprocess.Popen) -> int:
    check(execution_lease.plan_budget(33, 90) == 33 * 900 and execution_lease.plan_budget(5, 2) == 5 * 60,
          "a plan's budget is each mutant up to ten times the clean run, at least 60s")
    with tempfile.TemporaryDirectory(prefix="syrd882.") as tmp_name:
        tmp = Path(tmp_name)
        tree = tmp / "tree"
        suites = sweep_tree(tree, seconds=8)
        sweep = [sys.executable, str(SWEEP), "--root", str(tree), "--jobs", "2", "--timeout", "20", "--no-sandbox",
                 *suites]
        got = case(tmp, "sweep", [*sweep, "--out", str(tmp / "out-sweep")])
        expected = 2 * 20 * 2 + 20  # ceil(3 / 2) rounds of 20s, twice for a retry, plus 20s
        check(got["record"] is not None and got["record"]["source"] == "declared" and not got["record"]["hard"]
              and budget_of(got["record"]) == expected and "3 suites" in got["record"]["label"],
              f"a sweep of 3 suites at 2 jobs and 20s declares {expected}s for its session, stopping nothing: {got}")
        check(got["within"] == [0, 0, 0] and got["after"] == [0, 0, 0] and got["running"] and got["plain"] == 1,
              f"while it runs within that, the Director hears nothing of it, though it is long past the default "
              f"threshold, which an undeclared command beside it is told about: {got}")
        check(f"expect up to {expected}s" in got["log"], f"the sweep says what it declared: {got['log'][-600:]}")
        capped = case(tmp, "sweep-capped", [*sweep, "--out", str(tmp / "out-capped")], cap="3s")
        check(budget_of(capped["record"]) == expected and "capped at its 3s" in capped["log"],
              f"over the board's cap, the sweep declares what its guards allow and says it is capped: {capped}")
        check(capped["within"] == [0, 0, 0] and capped["after"][-1] == 1 and capped["running"],
              f"past the (capped) budget, still running, the Director is told once: {capped}")
        plan = [sys.executable, str(WRAPPER), "--mutants", "3", "--clean", "2s", "--label", "syrd882 plan", "--",
                "sleep", "882.7"]
        got = case(tmp, "plan", plan)
        check(got["record"] is not None and got["record"]["source"] == "declared" and budget_of(got["record"]) == 180
              and got["record"]["label"] == "syrd882 plan",
              f"a plan of 3 mutants with a 2s clean run declares 3 x max(60s, 10 x 2s) = 180s: {got}")
        check(got["within"] == [0, 0, 0] and got["after"] == [0, 0, 0] and got["plain"] == 1,
              f"within it, the Director hears nothing of the plan, and is told of the undeclared command: {got}")
        capped = case(tmp, "plan-capped", plan, cap="3s")
        check(capped["within"] == [0, 0, 0] and capped["after"][-1] == 1 and "capped at the board's 3s" in capped["log"],
              f"past its capped budget, still running, the plan is reported once, and said to be capped: {capped}")
        # SYRD-884: above the floor, the factor decides. Through the real CLI, read from the lease record.
        for label, extra, expected in (("default factor", [], 2 * 10 * 9), ("--factor 20", ["--factor", "20"], 2 * 20 * 9)):
            state_dir = tmp / f"state-factor-{len(extra)}"
            state_dir.mkdir()
            proc = tool([sys.executable, str(WRAPPER), "--mutants", "2", "--clean", "9s", *extra, "--", "sleep", "884.9"],
                        state_dir, tmp / f"factor-{len(extra)}.log")
            try:
                record = declared(state_dir, proc)
            finally:
                stop(proc)
            check(leftovers(*MARKERS) == [], f"{label}: the wrapped plan does not outlive its case: {leftovers(*MARKERS)}")
            check(budget_of(record) == expected,
                  f"{label}: 2 mutants with a 9s clean run declare 2 x {expected // 2}s = {expected}s, the factor above "
                  f"the 60s floor: {record}")
        # SYRD-885: the plan mode's two refusals: exit 2, the reason, and no lease written.
        for label, flags, reason in (
                ("--expect with --mutants", ["--expect", "1h", "--mutants", "2", "--clean", "9s"],
                 "give --expect or --mutants with --clean, not both"),
                ("--mutants without --clean", ["--mutants", "2"], "--mutants and --clean go together"),
                ("--clean without --mutants", ["--clean", "9s"], "--mutants and --clean go together")):
            state_dir = tmp / f"state-refused-{len(flags)}-{flags[0]}"
            state_dir.mkdir()
            log = tmp / f"refused-{len(flags)}-{flags[0].strip('-')}.log"
            proc = tool([sys.executable, str(WRAPPER), *flags, "--", "sleep", "885.1"], state_dir, log)
            try:
                code = proc.wait(10)  # a refusal is immediate; an accepted plan would run its command
            except subprocess.TimeoutExpired:
                code = None
            leases = list(execution_lease.lease_dir(state_dir).glob("*.json"))
            stop(proc)
            said = log.read_text(errors="replace")
            check(code == 2 and reason in said and not leases and leftovers("885.1") == [],
                  f"{label} is refused: exit 2, saying {reason!r}, with no lease and nothing started: "
                  f"exit {code}, leases {[l.name for l in leases]}: {said[-300:]}")
        # A sweep inside an outer `switchyard-bounded --expect` (one session) leaves the wrapper's declaration standing.
        outer = case(tmp, "outer", [sys.executable, str(WRAPPER), "--expect", "7m", "--label", "outer", "--", *sweep,
                                    "--out", str(tmp / "out-outer")])
        check(budget_of(outer["record"]) == 420 and outer["record"]["label"] == "outer"
              and outer["within"] == [0, 0, 0],
              f"inside an outer switchyard-bounded --expect 7m, the wrapper's declaration stands, not the sweep's: {outer}")
        # A sweep that finishes withdraws its declaration: nothing is left for a later command of the session.
        short = tmp / "short"
        quick = sweep_tree(short, seconds=0.5)
        state_dir = tmp / "state-short"
        state_dir.mkdir()
        done = tool([sys.executable, str(SWEEP), "--root", str(short), "--jobs", "3", "--timeout", "20", "--no-sandbox",
                     "--out", str(tmp / "out-short"), *quick], state_dir, tmp / "short.log")
        record = declared(state_dir, done)
        done.wait(60)
        check(budget_of(record) == 1 * 20 * 2 + 20 and done.returncode == 0
              and not any(execution_lease.lease_dir(state_dir).glob("*.json")),
              f"a finished sweep withdraws its declaration: {record} {(tmp / 'short.log').read_text()[-400:]}")
    check(decoy.poll() is None and decoy.pid not in SESSIONS,
          "a marker process outside the run, running throughout, failed no case and was stopped by none")
    print(f"declared_budget_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
