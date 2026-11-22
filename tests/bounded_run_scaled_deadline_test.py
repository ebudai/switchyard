#!/usr/bin/env python3
"""SYRD-845: bounded_run has no default whole-run budget, and a case's hang deadline scales from its clean run.

The User: "I don't want time limits on the test runs, sometimes a really long run is reasonable." Main's
`tests/bounded_run.py` still ended every mutation plan at 540s (`DEFAULT_TOTAL_SECONDS`), recording the rest as
inconclusive, and gave every case a fixed 300s, so a suite whose clean run is longer could never be mutation-tested
without a flag. Now:

* no whole-run budget unless the caller gives one (`total_seconds=`, `--total-seconds`,
  `SWITCHYARD_BOUNDED_TOTAL_SECONDS`), which still applies;
* each suite's cases get `max(60s, 10 x its clean runtime)`, measured on the clean tree before the first mutant or
  passed in as `clean_seconds`; an explicit case deadline (argument or environment) overrides it;
* a mutant that loops forever is still stopped at its scaled deadline and recorded inconclusive, never killed;
* a suite that fails or hangs on the clean tree gives no baseline: every mutant is inconclusive.

SYRD-846 dropped the last two fixed caps and pinned two branches that mutants survived:
* the clean run has no cap unless the caller gives one (`clean_run_seconds=`): it is the unmodified candidate, so a
  hang there is a defect in the code under review, and the board's long-command lease reports it;
* `run` with nothing measured has no deadline; with `--clean-seconds` it keeps the scaled one;
* a measured plan gives each suite the deadline of its own clean run, not the first suite's;
* a clean run that leaves processes behind gives no baseline, so no mutant runs.

Long plans run against a fake clock with a stubbed `run_bounded`, so hours of simulated suite time take
milliseconds. The looping mutant and the command line run real child processes, in temporary directories. Main is
checked first, loaded from git.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types
from contextlib import contextmanager
from pathlib import Path

sys.dont_write_bytecode = True  # main's module is loaded from a temporary file: leave nothing compiled behind

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import bounded_run as br  # noqa: E402

#: Main before SYRD-845 and before SYRD-846, inserted from `git rev-parse`.
BEFORE = "7b85764e9c845e5b6b3d80be6458b2a5fe68ba7e"
BEFORE_846 = "410f9359dd1fad73e81e95bfa33b21e6af631113"
GRACE = 0.5

MODULE = "def answer():\n    return 42\n"
SUITE = textwrap.dedent('''
    import sys
    sys.path.insert(0, ".")
    import walk
    assert walk.answer() == 42, "the answer changed"
    print("suite: ok")
''').lstrip()
#: Four mutants: three the suite catches, one it cannot see.
MUTANTS = (
    br.Mutant("returns 41", "walk.py", (("    return 42\n", "    return 41\n"),)),
    br.Mutant("returns None", "walk.py", (("    return 42\n", "    return None\n"),)),
    br.Mutant("returns a string", "walk.py", (("    return 42\n", "    return '42'\n"),)),
    br.Mutant("a comment", "walk.py", (("    return 42\n", "    return 42  # same\n"),)),
)
SURVIVOR = "a comment"


def check(condition: object, detail: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def main_module(before: str = BEFORE) -> types.ModuleType:
    """Main's bounded_run.py at `before`, from git, as a module of its own."""
    source = subprocess.run(["git", "-C", str(ROOT), "show", f"{before}:tests/bounded_run.py"], check=True,
                            capture_output=True, text=True).stdout
    tmp = Path(tempfile.mkdtemp(prefix="s845-main."))
    path = tmp / "bounded_run_main.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("bounded_run_main", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bounded_run_main"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        shutil.rmtree(tmp)
    return module


class Fake:
    """A clock and a `run_bounded` stand-in: each run advances the clock by its simulated duration."""

    def __init__(self, module, durations: dict, failing_clean: bool = False, leaves_processes: bool = False) -> None:
        self.module = module
        self.now = 1000.0
        #: "clean" and "mutant" seconds: one number for every suite, or a dict by the suite's last argument
        self.durations = durations
        self.failing_clean = failing_clean
        self.leaves_processes = leaves_processes
        self.calls: list[tuple[str, float | None]] = []

    def monotonic(self) -> float:
        return self.now

    def run_bounded(self, argv, *, timeout, cwd=None, env=None, label="", grace=0.0, **_):
        self.calls.append((label, timeout))
        clean = label.startswith("clean ::")
        duration = self.durations["clean" if clean else "mutant"]
        if isinstance(duration, dict):
            duration = duration[argv[-1]]
        if timeout is not None and duration > timeout:
            self.now += timeout
            return self.module.Outcome(label, "timeout", None, timeout, timeout, "", "",
                                       reason=f"no result within {timeout:g}s")
        self.now += duration
        text = (Path(cwd) / "walk.py").read_text()
        caught = not clean and not any(line.split("#")[0].strip() == "return 42" for line in text.splitlines())
        code = 1 if (caught or (clean and self.failing_clean)) else 0
        if clean and self.leaves_processes:  # what run_bounded reports for a group it could not stop
            return self.module.Outcome(label, "completed", code, duration, timeout, "", "", leftover_group=True,
                                       reason="still present after cleanup: group=yes unreaped=[]")
        return self.module.Outcome(label, "completed", code, duration, timeout, "", "")

    def mutant_timeouts(self) -> list[float]:
        return [timeout for label, timeout in self.calls if not label.startswith("clean ::")]

    def clean_runs(self) -> int:
        return sum(label.startswith("clean ::") for label, _ in self.calls)


@contextmanager
def faked(module, durations: dict, *, failing_clean: bool = False, leaves_processes: bool = False,
          environ: dict[str, str] | None = None):
    fake = Fake(module, durations, failing_clean, leaves_processes)
    saved_time, saved_run = module.time, module.run_bounded
    saved_env = {name: os.environ.get(name) for name in ("SWITCHYARD_BOUNDED_TOTAL_SECONDS",
                                                         "SWITCHYARD_BOUNDED_CASE_SECONDS")}
    module.time = types.SimpleNamespace(monotonic=fake.monotonic, sleep=lambda _s: None)
    module.run_bounded = fake.run_bounded
    for name in saved_env:
        os.environ.pop(name, None)
    os.environ.update(environ or {})
    try:
        with tempfile.TemporaryDirectory(prefix="s845.") as d:
            fake.root = Path(d)
            (fake.root / "walk.py").write_text(MODULE)
            (fake.root / "suite.py").write_text(SUITE)
            yield fake
    finally:
        module.time, module.run_bounded = saved_time, saved_run
        for name, value in saved_env.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value


def plan(module, fake: Fake, **kwargs):
    mutants = [module.Mutant(m.name, m.path, m.edits) for m in MUTANTS]
    return module.run_mutation_plan(mutants, [[sys.executable, "suite.py"]], root=fake.root, grace=0.0, **kwargs)


def verdicts(results) -> dict[str, str]:
    return {r.name: r.result for r in results}


EXPECTED = {m.name: ("survived" if m.name == SURVIVOR else "killed") for m in MUTANTS}


# --- main first -------------------------------------------------------------------------


def test_main_caps_the_run_at_540s_and_every_case_at_300s() -> None:
    """The bug: four 200s cases end at main's 540s budget, and a 400s suite can never pass its fixed 300s."""
    main = main_module()
    check(main.DEFAULT_TOTAL_SECONDS == 540.0, f"main's default budget: {main.DEFAULT_TOTAL_SECONDS}")
    with faked(main, {"clean": 200.0, "mutant": 200.0}) as fake:
        results = plan(main, fake)
        check(verdicts(results)["a comment"] == "inconclusive"
              and "whole-run budget" in results[-1].reason, f"main runs every case: {verdicts(results)}")
        check(fake.mutant_timeouts()[0] == 300.0 and max(fake.mutant_timeouts()) == 300.0,
              f"main's case deadlines: {fake.mutant_timeouts()}")
    with faked(main, {"clean": 400.0, "mutant": 400.0}) as fake:
        results = plan(main, fake, total_seconds=100000)
        check(all(r.result == "inconclusive" and r.reason.startswith("timeout:") for r in results),
              f"main gives a 400s suite room: {[(r.name, r.result) for r in results]}")


def test_main_caps_the_clean_run_at_an_hour_and_an_unmeasured_run_at_300s() -> None:
    """SYRD-846's bug: a two-hour clean suite is refused at main's hour, and `run` alone stops at 300s."""
    import argparse

    main = main_module(BEFORE_846)
    check((main.CLEAN_RUN_SECONDS, main.DEFAULT_CASE_SECONDS) == (3600.0, 300.0),
          f"main's caps: {(main.CLEAN_RUN_SECONDS, main.DEFAULT_CASE_SECONDS)}")
    with faked(main, {"clean": 7200.0, "mutant": 7200.0}) as fake:
        results = plan(main, fake)
        check(all(r.result == "inconclusive" and "did not finish" in r.reason for r in results),
              f"main lets a two-hour clean run finish: {[(r.result, r.reason[:60]) for r in results]}")
        check(fake.calls[0][1] == 3600.0, f"main's clean-run bound: {fake.calls[0]}")
    seen: list = []
    saved = main.run_bounded
    main.run_bounded = lambda argv, *, timeout, **_: (seen.append(timeout)
                                                      or main.Outcome("x", "completed", 0, 0.0, timeout, "", ""))
    try:
        main._cli_run(argparse.Namespace(command=["true"], case_seconds=None, clean_seconds=None))
    finally:
        main.run_bounded = saved
    check(seen == [300.0], f"main's unmeasured run: {seen}")


# --- no default whole-run budget ------------------------------------------------------


def test_an_uncapped_case_has_no_deadline_unless_the_run_has_a_budget() -> None:
    """Deadline.case_timeout(None): no cap. Run before any plan, so a regression is this assertion, not a crash."""
    import math

    try:
        unlimited, budgeted = br.Deadline(None).case_timeout(None), br.Deadline(100.0).case_timeout(None)
    except TypeError as exc:
        unlimited = budgeted = f"refused: {exc}"
    check(unlimited == math.inf, f"no cap and no budget is no deadline: {unlimited}")
    check(isinstance(budgeted, float) and 99.0 < budgeted <= 100.0, f"a budget still bounds an uncapped case: {budgeted}")



def test_a_plan_longer_than_540s_finishes_without_a_budget() -> None:
    check(br.DEFAULT_TOTAL_SECONDS is None, f"a default whole-run budget: {br.DEFAULT_TOTAL_SECONDS}")
    with faked(br, {"clean": 200.0, "mutant": 200.0}) as fake:
        started = fake.now
        results = plan(br, fake)
        check(verdicts(results) == EXPECTED, f"every mutant has its result: {verdicts(results)}")
        check(fake.now - started >= 4 * 200.0 > 540.0, f"simulated {fake.now - started}s")
        check(not any(r.result == "inconclusive" for r in results), "an inconclusive case without a budget")


def test_a_given_budget_still_applies() -> None:
    for how, kwargs, environ in (("argument", {"total_seconds": 540.0}, {}),
                                 ("environment", {}, {"SWITCHYARD_BOUNDED_TOTAL_SECONDS": "540"})):
        with faked(br, {"clean": 100.0, "mutant": 200.0}, environ=environ) as fake:
            results = plan(br, fake, **kwargs)
            check([r.result for r in results][:2] == ["killed", "killed"], f"{how}: {verdicts(results)}")
            late = results[2:]
            check(late and all(r.result == "inconclusive" for r in late), f"{how}: the budget did not apply")
            check(any("whole-run budget" in r.reason for r in late), f"{how}: {[r.reason for r in late]}")
            check(fake.now - 1000.0 <= 540.0 + 1e-6, f"{how}: ran {fake.now - 1000.0}s past a 540s budget")


# --- the scaled hang deadline ---------------------------------------------------------


def test_the_deadline_follows_the_clean_runtime_with_a_floor() -> None:
    check((br.CASE_TIMEOUT_FACTOR, br.CASE_TIMEOUT_FLOOR_SECONDS) == (10.0, 60.0), "k = 10, floor = 60s")
    check(br.scaled_case_seconds(219.0) == 2190.0, "a 219s suite gets ten times it")
    check(br.scaled_case_seconds(0.2) == 60.0 and br.scaled_case_seconds(6.0) == 60.0, "the floor")
    for clean, deadline in ((30.0, 300.0), (219.0, 2190.0), (400.0, 4000.0), (0.3, 60.0)):
        with faked(br, {"clean": clean, "mutant": clean}) as fake:
            results = plan(br, fake)
            check(fake.clean_runs() == 1, f"clean {clean}s: measured once, not {fake.clean_runs()} times")
            check(set(fake.mutant_timeouts()) == {deadline}, f"clean {clean}s: deadlines {fake.mutant_timeouts()}")
            check(verdicts(results) == EXPECTED, f"clean {clean}s: {verdicts(results)}")
            check(all(record.get("timeout") == deadline for r in results for record in r.suites),
                  f"clean {clean}s: the record does not say its deadline")


def test_each_suite_gets_its_own_deadline() -> None:
    with faked(br, {"clean": 1.0, "mutant": 1.0}) as fake:
        mutants = [br.Mutant(SURVIVOR, "walk.py", MUTANTS[3].edits)]
        br.run_mutation_plan(mutants, [[sys.executable, "suite.py"], [sys.executable, "suite.py", "again"]],
                             root=fake.root, grace=0.0, clean_seconds=[2.0, 30.0])
        check(fake.clean_runs() == 0, "measured clean runs were given, so none is run")
        check(fake.mutant_timeouts() == [60.0, 300.0], f"per-suite deadlines: {fake.mutant_timeouts()}")


def test_an_explicit_deadline_overrides_the_scaling() -> None:
    for how, kwargs, environ, deadline in (("argument", {"case_seconds": 7.0}, {}, 7.0),
                                           ("environment", {}, {"SWITCHYARD_BOUNDED_CASE_SECONDS": "9"}, 9.0)):
        with faked(br, {"clean": 1.0, "mutant": 1.0}, environ=environ) as fake:
            results = plan(br, fake, **kwargs)
            check(fake.clean_runs() == 0, f"{how}: no clean run is needed")
            check(set(fake.mutant_timeouts()) == {deadline}, f"{how}: {fake.mutant_timeouts()}")
            check(verdicts(results) == EXPECTED, f"{how}: {verdicts(results)}")


def test_a_clean_tree_that_fails_gives_no_baseline() -> None:
    with faked(br, {"clean": 1.0, "mutant": 1.0}, failing_clean=True) as fake:
        results = plan(br, fake)
        check(all(r.result == "inconclusive" and "fails on the clean tree" in r.reason for r in results),
              f"a red suite killed mutants: {[(r.result, r.reason) for r in results]}")
        check(fake.mutant_timeouts() == [], "a mutant ran against a suite that fails on the clean tree")


def test_a_clean_run_that_leaves_processes_gives_no_baseline() -> None:
    """Processes left by the clean run could make every mutant's run fail: a false kill. No mutant runs."""
    with faked(br, {"clean": 1.0, "mutant": 1.0}, leaves_processes=True) as fake:
        results = plan(br, fake)
        check(fake.mutant_timeouts() == [], f"a mutant ran after the clean run left processes: {fake.calls}")
        check(all(r.result == "inconclusive" and "left processes behind" in r.reason for r in results),
              f"verdicts after a clean run left processes: {[(r.result, r.reason[:70]) for r in results]}")


def test_the_clean_run_has_no_cap_unless_one_is_given() -> None:
    check(br.CLEAN_RUN_SECONDS is None, f"a default clean-run cap: {br.CLEAN_RUN_SECONDS}")
    with faked(br, {"clean": 5 * 3600.0, "mutant": 1.0}) as fake:
        results = plan(br, fake)
        check(fake.calls[0][1] is None, f"the clean run ran under a deadline: {fake.calls[0]}")
        check(verdicts(results) == EXPECTED, f"a five-hour clean run: {verdicts(results)}")
        check(set(fake.mutant_timeouts()) == {50 * 3600.0}, f"its cases' deadlines: {fake.mutant_timeouts()}")
    with faked(br, {"clean": 10 ** 6, "mutant": 1.0}) as fake:
        results = plan(br, fake, clean_run_seconds=3600.0)
        check(all(r.result == "inconclusive" and "did not finish" in r.reason for r in results),
              f"a given cap: {[(r.result, r.reason[:60]) for r in results]}")
        check(fake.calls == [(fake.calls[0][0], 3600.0)], f"the given cap's bound: {fake.calls}")
    with faked(br, {"clean": 10 ** 6, "mutant": 1.0}) as fake:
        results = plan(br, fake, total_seconds=900.0)
        check(fake.calls[0][1] == 900.0 and all(r.result == "inconclusive" for r in results),
              f"a whole-run budget bounds the clean run: {fake.calls}")


def test_run_bounded_with_no_deadline_waits_and_records_none() -> None:
    """Real processes: None or an infinite timeout is no deadline, recorded as None (JSON null), never Infinity."""
    import math

    for timeout in (None, math.inf):
        outcome = br.run_bounded([sys.executable, "-c", "import time; time.sleep(0.3); raise SystemExit(4)"],
                                 timeout=timeout, grace=GRACE)
        check((outcome.status, outcome.returncode, outcome.timeout) == ("completed", 4, None),
              f"timeout={timeout}: {(outcome.status, outcome.returncode, outcome.timeout)}")
        check(outcome.clean and json.dumps({"timeout": outcome.timeout}) == '{"timeout": null}',
              f"timeout={timeout}: the record")


def test_a_measured_plan_gives_each_suite_its_own_deadline() -> None:
    """A fast first suite must not hand a slow later suite its deadline: that would be a false timeout."""
    suites = [[sys.executable, "suite.py", "fast"], [sys.executable, "suite.py", "slow"],
              [sys.executable, "suite.py", "middling"]]
    with faked(br, {"clean": {"fast": 2.0, "slow": 30.0, "middling": 9.0},
                    "mutant": {"fast": 2.0, "slow": 200.0, "middling": 9.0}}) as fake:
        mutants = [br.Mutant(SURVIVOR, "walk.py", MUTANTS[3].edits)]
        results = br.run_mutation_plan(mutants, suites, root=fake.root, grace=0.0)
        check(fake.clean_runs() == 3, f"each suite measured once: {fake.calls}")
        recorded = [(record["argv"][-1], record["timeout"], record["status"]) for record in results[0].suites]
        check(recorded == [("fast", 60.0, "completed"), ("slow", 300.0, "completed"), ("middling", 90.0, "completed")],
              f"per-suite measured deadlines: {recorded}")
        check(results[0].result == "survived", f"the slow suite was cut off: {results[0]}")


def test_a_looping_mutant_is_stopped_at_its_scaled_deadline() -> None:
    """Real processes: a clean run well under the floor, so the case gets the floor and the loop is stopped there."""
    with tempfile.TemporaryDirectory(prefix="s845.") as d:
        root = Path(d)
        (root / "walk.py").write_text(MODULE)
        (root / "suite.py").write_text(SUITE)
        original = (root / "walk.py").read_bytes()
        judged: list[str] = []

        def killed_when(outcome: br.Outcome) -> bool:
            judged.append(outcome.label)
            return outcome.returncode != 0

        mutants = [br.Mutant("loops forever", "walk.py", (("    return 42\n", "    while True:\n        pass\n"),)),
                   br.Mutant("returns 41", "walk.py", (("    return 42\n", "    return 41\n"),))]
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        started = time.monotonic()
        results = br.run_mutation_plan(mutants, [[sys.executable, "suite.py"]], root=root, env=env, grace=GRACE,
                                       killed_when=killed_when, floor_seconds=1.5)
        took = time.monotonic() - started
        check([r.result for r in results] == ["inconclusive", "killed"], f"verdicts: {verdicts(results)}")
        hung = results[0]
        check(hung.reason.startswith("timeout:") and hung.suites[0]["status"] == "timeout", f"hung: {hung}")
        check(hung.suites[0]["timeout"] == 1.5, f"the scaled deadline: {hung.suites[0]}")
        check(results[1].suites[0]["timeout"] == 1.5, "the killed mutant's deadline")
        check(not any("loops forever" in label for label in judged), "a timeout reached the kill decision")
        check(any(label.startswith("clean ::") for label in judged), "the clean run was judged as a baseline")
        check(took < 1.5 + 2 * GRACE + 10.0, f"the plan took {took:.1f}s")
        check((root / "walk.py").read_bytes() == original, "the module was not restored")


def test_run_takes_its_deadline_from_clean_seconds_or_the_override() -> None:
    """`run` with nothing measured has no deadline; --clean-seconds scales; --case-seconds (or the environment) wins."""
    import argparse

    seen: list[float] = []
    saved = br.run_bounded
    saved_env = os.environ.pop("SWITCHYARD_BOUNDED_CASE_SECONDS", None)
    br.run_bounded = lambda argv, *, timeout, **_: (seen.append(timeout)
                                                    or br.Outcome("x", "completed", 0, 0.0, timeout, "", ""))
    try:
        for clean, case, expected in ((None, None, None), (12.0, None, 120.0), (2.0, None, 60.0), (12.0, 5.0, 5.0)):
            seen.clear()
            br._cli_run(argparse.Namespace(command=["true"], case_seconds=case, clean_seconds=clean))
            check(seen == [expected], f"run clean={clean} case={case}: deadline {seen}, not {expected}")
        os.environ["SWITCHYARD_BOUNDED_CASE_SECONDS"] = "7"
        seen.clear()
        br._cli_run(argparse.Namespace(command=["true"], case_seconds=None, clean_seconds=12.0))
        check(seen == [7.0], f"the environment's deadline: {seen}")
    finally:
        br.run_bounded = saved
        os.environ.pop("SWITCHYARD_BOUNDED_CASE_SECONDS", None)
        if saved_env is not None:
            os.environ["SWITCHYARD_BOUNDED_CASE_SECONDS"] = saved_env


def test_the_command_line_scales_and_takes_a_plan_without_a_budget() -> None:
    tool = str(ROOT / "tests" / "bounded_run.py")
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
    nap = [sys.executable, "-c", "import time; time.sleep(1.2)"]
    scaled = subprocess.run([sys.executable, tool, "run", "--clean-seconds", "0.05", "--", *nap],
                            capture_output=True, text=True, timeout=120, env=env)
    check(scaled.returncode == 0, f"run --clean-seconds gives the floor: {scaled.returncode} {scaled.stderr[-300:]}")
    fixed = subprocess.run([sys.executable, tool, "run", "--case-seconds", "0.5", "--", *nap],
                           capture_output=True, text=True, timeout=120, env=env)
    check(fixed.returncode == 124, f"run --case-seconds is the deadline itself: {fixed.returncode}")
    alone = subprocess.run([sys.executable, tool, "run", "--", sys.executable, "-c", "raise SystemExit(3)"],
                           capture_output=True, text=True, timeout=120, env=env)
    check(alone.returncode == 3, f"run with nothing measured keeps the command's result: {alone.returncode}")
    run_usage = subprocess.run([sys.executable, tool, "run", "--help"], capture_output=True, text=True, timeout=60,
                               env=env).stdout
    flat_usage = " ".join(run_usage.split())
    check("automatic hang protection" in flat_usage and "no deadline" in flat_usage and "lease" in flat_usage,
          f"run --help: {run_usage[-500:]}")
    with tempfile.TemporaryDirectory(prefix="s845.") as d:
        root = Path(d)
        (root / "walk.py").write_text(MODULE)
        (root / "suite.py").write_text(SUITE)
        plan_file = root / "plan.json"
        plan_file.write_text(json.dumps({
            "root": str(root), "suites": [[sys.executable, "suite.py"]], "clean_seconds": [12.0],
            "mutants": [{"name": "returns 41", "path": "walk.py", "edits": [["    return 42\n", "    return 41\n"]]}],
        }))
        mutate = subprocess.run([sys.executable, tool, "mutate", str(plan_file)], capture_output=True, text=True,
                                timeout=120, env=env)
        lines = [json.loads(line) for line in mutate.stdout.splitlines() if line.startswith("{")]
        check(mutate.returncode == 0 and [line["result"] for line in lines] == ["killed"], mutate.stdout[-500:])
        check(lines[0]["suites"][0]["timeout"] == 120.0, f"the plan's clean_seconds scaled: {lines[0]['suites']}")
    usage = subprocess.run([sys.executable, tool, "mutate", "--help"], capture_output=True, text=True, timeout=60,
                           env=env).stdout
    check("default: none" in usage and "ten times" in usage, f"mutate --help: {usage[-400:]}")


# --- the text that stated the old numbers --------------------------------------------


def test_the_old_numbers_are_gone_and_long_plans_are_explained() -> None:
    doc = br.__doc__ or ""
    methodology = (ROOT / "docs/onboarding/adversarial-collaborative-methodology.md").read_text(encoding="utf-8")
    for name, text in (("bounded_run.py", doc), ("the methodology", methodology)):
        flat = " ".join(text.split())
        check("540" not in flat, f"{name} still states 540s")
        check("no whole-run budget" in flat.lower(), f"{name} does not say there is no default budget")
        check("ten times" in flat and "60s" in flat, f"{name} does not give the scaled deadline")
        check("background" in flat and "switchyard-bounded --expect" in flat,
              f"{name} does not say how to run a long plan")
        check("--clean-seconds" in flat and "automatic hang protection" in flat and "lease" in flat,
              f"{name} does not say how an unmeasured run is protected")
        for stale in ("(an hour)", "(300s)", "3600", "300s by default"):
            check(stale not in flat, f"{name} still states a fixed cap: {stale!r}")


def main() -> int:
    tests = [(name, value) for name, value in globals().items() if name.startswith("test_") and callable(value)]
    first = ("test_main_caps_the_run_at_540s_and_every_case_at_300s",
             "test_main_caps_the_clean_run_at_an_hour_and_an_unmeasured_run_at_300s")
    tests.sort(key=lambda item: item[0] not in first)
    for _name, test in tests:
        test()
    print(f"bounded_run_scaled_deadline_test: {len(tests)} cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
