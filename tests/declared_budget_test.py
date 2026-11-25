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


def tool(argv: list[str], state_dir: Path, log: Path, environ: dict[str, str] | None = None) -> subprocess.Popen:
    """A command in a session of its own, as a provider's tool starts one, with the pane-state directory a pane has."""
    return subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=log.open("w"),
                            stderr=subprocess.STDOUT, preexec_fn=defaults,
                            env={**os.environ, "TICKET_BOARD_PANE_STATE_DIR": str(state_dir), **(environ or {})})


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


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, 9)
    proc.wait(10)


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
    return result


def budget_of(record: dict | None) -> float:
    return round(float(record["deadline"]) - float(record["declared_at"])) if record else -1.0


def main() -> int:
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
    print(f"declared_budget_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
