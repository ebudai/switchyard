#!/usr/bin/env python3
"""SYRD-403: a hung mutant or nested child cannot hold a run, and a timeout is never a result.

Every fixture is a temporary file and a child process this test spawns. The
only signals sent go to those children's own process groups; nothing reads
another process's `/proc`, and no host command runs.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import bounded_run as br  # noqa: E402

GRACE = 0.5
SLACK = 2.0  # scheduler and interpreter start-up allowance on top of the configured bound

#: The loop SYRD-402's hung mutant broke, reduced to its shape: a parent walk that must stop on a cycle.
FIXTURE_MODULE = textwrap.dedent('''
    def ancestry(pid, parents):
        seen = set()
        current = pid
        while current > 1 and current not in seen:
            seen.add(current)
            current = parents.get(current, 1)
        return seen
''').lstrip()

FIXTURE_SUITE = textwrap.dedent('''
    import sys
    sys.path.insert(0, ".")
    import walk
    assert walk.ancestry(5, {5: 4, 4: 3, 3: 1}) == {5, 4, 3}
    assert walk.ancestry(7, {7: 8, 8: 7}) == {7, 8}  # a cycle must stop
    print("fixture suite: ok")
''').lstrip()

#: A suite whose nested child never returns, waited on with no timeout: the shape of the call the ticket names.
NESTED_CHILD = (
    "import os, signal, sys, time\n"
    "if sys.argv[2] == 'ignore-term':\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "while True:\n"
    "    time.sleep(0.01)\n"
)
NESTED_SUITE = (
    "import subprocess, sys\n"
    f"CHILD = {NESTED_CHILD!r}\n"
    "subprocess.run([sys.executable, '-c', CHILD, sys.argv[1], sys.argv[2]])\n"
)


def gone(pid: int, within: float = 3.0) -> bool:
    """The process no longer exists (or is only a zombie awaiting its reaper)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            if Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def fixture(tmp: Path) -> None:
    (tmp / "walk.py").write_text(FIXTURE_MODULE)
    (tmp / "suite.py").write_text(FIXTURE_SUITE)
    (tmp / "nested.py").write_text(NESTED_SUITE)


def test_a_normal_run_and_a_passing_suite_keep_their_result() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        outcome = br.run_bounded([sys.executable, "suite.py"], timeout=10, cwd=tmp, grace=GRACE)
        assert (outcome.status, outcome.returncode, outcome.timed_out) == ("completed", 0, False), outcome
        assert "fixture suite: ok" in outcome.stdout and outcome.seconds < 10
        failing = br.run_bounded([sys.executable, "-c", "raise SystemExit(3)"], timeout=10, grace=GRACE)
        assert (failing.status, failing.returncode) == ("completed", 3), failing


def test_a_hanging_nested_child_is_stopped_with_its_group() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        for mode in ("plain", "ignore-term"):
            # Without the sweep the group signal alone must stop the nested child:
            # the sweep is a second layer, not the mechanism. That needs a fresh
            # process, since this one may already be a subreaper.
            pidfile = tmp / f"child-{mode}-nosweep.pid"
            probe = (f"import json, sys; sys.path.insert(0, {str(ROOT / 'tests')!r}); import bounded_run as br\n"
                     f"br._subreaper = False\n"
                     f"o = br.run_bounded([sys.executable, 'nested.py', {str(pidfile)!r}, {mode!r}], timeout=1.5, grace={GRACE})\n"
                     f"print(json.dumps([o.status, o.leftover_group, o.sweep_available]))")
            done = subprocess.run([sys.executable, "-c", probe], cwd=tmp, capture_output=True, text=True, timeout=30)
            # Only the status and the child's fate are asserted here: a killed member
            # may wait as a zombie for whichever ancestor reaps it, which keeps the
            # group nominally present without anything running.
            status, _leftover, sweep = json.loads(done.stdout)
            assert (status, sweep) == ("timeout", False), (mode, done.stdout, done.stderr)
            assert gone(int(pidfile.read_text())), f"{mode}: the group signal alone left the nested child alive"
        for mode in ("plain", "ignore-term"):
            pidfile = tmp / f"child-{mode}.pid"
            started = time.monotonic()
            outcome = br.run_bounded([sys.executable, "nested.py", str(pidfile), mode], timeout=1.5, cwd=tmp, grace=GRACE)
            took = time.monotonic() - started
            assert outcome.status == "timeout" and outcome.returncode is None, outcome
            assert took <= 1.5 + 2 * GRACE + SLACK, (mode, took)
            assert not outcome.leftover_group, outcome
            child = int(pidfile.read_text())
            assert gone(child), f"{mode}: nested child {child} is still alive"


def test_a_command_that_leaves_a_child_behind_does_not_leave_it_running() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        pidfile = tmp / "orphan.pid"
        orphan = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(600)"
        # The parent exits only once its child has written its pid, and leaves it running.
        script = (f"import os, subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {orphan!r}])\n"
                  f"while not os.path.exists({str(pidfile)!r}): time.sleep(0.01)")
        outcome = br.run_bounded([sys.executable, "-c", script], timeout=10, grace=GRACE)
        assert (outcome.status, outcome.returncode) == ("completed", 0), outcome
        assert gone(int(pidfile.read_text())), "a finished command left its child running"

        # The group stop after a normal exit must hold without the sweep behind it (a fresh process).
        pidfile.unlink()
        probe = (f"import json, sys; sys.path.insert(0, {str(ROOT / 'tests')!r}); import bounded_run as br\n"
                 f"br._subreaper = False\n"
                 f"o = br.run_bounded([sys.executable, '-c', {script!r}], timeout=10, grace={GRACE})\n"
                 f"print(json.dumps([o.status, o.returncode, o.sweep_available]))")
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=30)
        assert json.loads(done.stdout) == ["completed", 0, False], (done.stdout, done.stderr)
        assert gone(int(pidfile.read_text())), "without the sweep, a finished command left its child running"


def test_a_descendant_that_left_the_group_is_swept() -> None:
    """A nested harness puts its children in their own sessions; they must not outlive the run either."""
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        for ending in ("hangs", "exits"):
            pidfile = tmp / f"escaped-{ending}.pid"
            grandchild = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(600)"
            parent = (f"import os, subprocess, sys, time\n"
                      f"subprocess.Popen([sys.executable, '-c', {grandchild!r}], start_new_session=True)\n"
                      f"while not os.path.exists({str(pidfile)!r}): time.sleep(0.01)\n"
                      + ("time.sleep(600)\n" if ending == "hangs" else ""))
            outcome = br.run_bounded([sys.executable, "-c", parent], timeout=1.5 if ending == "hangs" else 10, grace=GRACE)
            assert outcome.status == ("timeout" if ending == "hangs" else "completed"), outcome
            assert outcome.sweep_available, "this host cannot sweep; the case proves nothing here"
            assert outcome.escaped >= 1, outcome
            assert gone(int(pidfile.read_text())), f"{ending}: a descendant in its own session outlived the run"


def test_mutants_keep_kill_and_survival_and_a_hang_is_inconclusive() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        original = (tmp / "walk.py").read_bytes()
        loop = "    while current > 1 and current not in seen:\n"
        mutants = [
            br.Mutant("the walk returns nothing", "walk.py", (("    return seen\n", "    return set()\n"),)),
            br.Mutant("a cycle not stopped", "walk.py", ((loop, "    while current > 1 and len(seen) < 100000:\n"),)),
            br.Mutant("a harmless rename", "walk.py", (("    seen = set()\n", "    seen = set()  # renamed nothing\n"),)),
            br.Mutant("an edit that does not apply", "walk.py", (("no such text", "x"),)),
        ]
        verdicts: list[tuple[str, str]] = []
        judged: list[str] = []

        def killed_when(outcome: br.Outcome) -> bool:
            judged.append(outcome.label)
            return outcome.returncode != 0

        # Bytecode allowed, so a mutant's compiled module is really written and must really be cleared.
        env = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
        started = time.monotonic()
        results = br.run_mutation_plan(mutants, [[sys.executable, "suite.py"]], root=tmp, case_seconds=2,
                                       total_seconds=60, killed_when=killed_when, grace=GRACE, env=env,
                                       report=lambda r: verdicts.append((r.name, r.result)))
        took = time.monotonic() - started
        assert verdicts == [
            ("the walk returns nothing", "killed"),
            ("a cycle not stopped", "inconclusive"),
            ("a harmless rename", "survived"),
            ("an edit that does not apply", "inconclusive"),
        ], verdicts
        hung = results[1]
        assert hung.reason.startswith("timeout:") and hung.suites[0]["status"] == "timeout", hung
        assert hung.suites[0]["returncode"] is None
        assert not any("a cycle not stopped" in label for label in judged), "a timeout reached the kill decision"
        assert results[3].reason.startswith("not applied:"), results[3]
        assert took <= 3 * 1 + 2 + 2 * GRACE + 3 * SLACK, took
        assert (tmp / "walk.py").read_bytes() == original, "the fixture module was not restored"
        assert not (tmp / "__pycache__").exists(), "bytecode compiled from a mutant was left behind"
        line, code = br.summarize(results)
        assert line.startswith("4 mutants: 1 killed, 1 survived, 2 inconclusive"), line
        assert code == 1, code
        assert br.summarize([results[0], results[1]])[1] == 2
        assert br.summarize([results[0]])[1] == 0


def test_the_whole_run_budget_bounds_every_later_mutant() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        hang = ("    while current > 1 and current not in seen:\n", "    while True:\n")
        mutants = [br.Mutant(f"hang {i}", "walk.py", (hang,)) for i in range(3)]
        started = time.monotonic()
        results = br.run_mutation_plan(mutants, [[sys.executable, "suite.py"]], root=tmp, case_seconds=1.0,
                                       total_seconds=1.6, grace=GRACE)
        took = time.monotonic() - started
        assert [r.result for r in results] == ["inconclusive"] * 3, results
        assert results[0].reason.startswith("timeout:") and results[1].reason.startswith("timeout:"), results
        assert results[1].suites[0]["seconds"] < 1.0, "the second mutant was not held to what the run had left"
        assert results[2].reason.startswith("not run: the whole-run budget"), results[2]
        assert took <= 1.6 + 2 * 2 * GRACE + 2 * SLACK, took


def test_output_is_bounded_and_the_command_line_reports_plainly() -> None:
    noisy = br.run_bounded([sys.executable, "-c", "print('x' * 200000)"], timeout=10, output_limit=1000, grace=GRACE)
    assert len(noisy.stdout) < 1200 and "bytes omitted" in noisy.stdout, len(noisy.stdout)

    tool = str(ROOT / "tests" / "bounded_run.py")
    hung = subprocess.run([sys.executable, tool, "run", "--case-seconds", "1", "--", sys.executable, "-c",
                           "import time; time.sleep(60)"], capture_output=True, text=True, timeout=30)
    assert hung.returncode == 124 and "BOUNDED-RUN TIMEOUT" in hung.stderr, (hung.returncode, hung.stderr)
    passed = subprocess.run([sys.executable, tool, "run", "--", sys.executable, "-c", "print('fine')"],
                            capture_output=True, text=True, timeout=30)
    assert (passed.returncode, passed.stdout) == (0, "fine\n"), passed

    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        plan = {"root": str(tmp), "suites": [[sys.executable, "suite.py"]], "mutants": [
            {"name": "killed", "path": "walk.py", "edits": [["    return seen\n", "    return set()\n"]]},
            {"name": "hangs", "path": "walk.py", "edits": [["    while current > 1 and current not in seen:\n", "    while True:\n"]]},
        ]}
        (tmp / "plan.json").write_text(json.dumps(plan))
        done = subprocess.run([sys.executable, tool, "mutate", str(tmp / "plan.json"), "--case-seconds", "1",
                               "--total-seconds", "30"], capture_output=True, text=True, timeout=60)
        lines = done.stdout.strip().splitlines()
        assert [json.loads(line)["result"] for line in lines[:-1]] == ["killed", "inconclusive"], lines
        assert lines[-1].startswith("2 mutants: 1 killed, 0 survived, 1 inconclusive"), lines[-1]
        assert done.returncode == 2, (done.returncode, done.stderr)


def sleeper(pidfile: Path) -> str:
    """A child that records its pid and then waits, reading nothing."""
    return f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(600)"


def wait_for(path: Path, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_unread_large_input_cannot_hold_the_runner() -> None:
    """The Director's reproduction: 2,000,000 characters to a child that never reads stdin."""
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        pidfile = Path(d) / "reader.pid"
        started = time.monotonic()
        outcome = br.run_bounded([sys.executable, "-c", sleeper(pidfile)], timeout=0.2, grace=0.1,
                                 input_text="x" * 2_000_000)
        took = time.monotonic() - started
        assert outcome.status == "timeout" and outcome.clean, outcome
        assert took <= 0.2 + 2 * 0.1 + SLACK, took
        assert gone(int(pidfile.read_text())), "the child that never read its input is still alive"
        echoed = br.run_bounded([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"], timeout=10,
                                grace=GRACE, input_text="y" * 2_000_000)
        assert (echoed.status, echoed.stdout.strip()) == ("completed", "2000000"), echoed


def test_start_and_wait_failures_still_clean_up() -> None:
    try:
        br.run_bounded(["/nonexistent/syrd403-no-such-program"], timeout=1, grace=GRACE)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a command that cannot start must raise, not report a result")

    class FailingWait(subprocess.Popen):
        def wait(self, timeout=None):
            if timeout is not None and not getattr(self, "_failed", False):
                self._failed = True
                raise RuntimeError("injected wait failure")
            return super().wait(timeout)

    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        pidfile = Path(d) / "waited.pid"
        real = br.subprocess.Popen
        br.subprocess.Popen = FailingWait
        started = time.monotonic()
        try:
            br.run_bounded([sys.executable, "-c", sleeper(pidfile)], timeout=30, grace=GRACE)
        except RuntimeError as exc:
            assert str(exc) == "injected wait failure", exc
        else:
            raise AssertionError("the injected wait failure was swallowed")
        finally:
            br.subprocess.Popen = real
        assert time.monotonic() - started <= 2 * GRACE + SLACK, "cleanup after a failed wait was not bounded"
        wait_for(pidfile)
        if pidfile.exists():
            assert gone(int(pidfile.read_text())), "a failed wait left the child running"


def test_many_escaped_children_share_one_cleanup_budget() -> None:
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        count = 12
        stubborn = ("import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(600)")
        parent = (f"import os, subprocess, sys, time\n"
                  f"for i in range({count}):\n"
                  f"    subprocess.Popen([sys.executable, '-c', {stubborn!r}, os.path.join({str(tmp)!r}, f'e{{i}}.pid')],"
                  f" start_new_session=True)\n"
                  f"while len([n for n in os.listdir({str(tmp)!r}) if n.endswith('.pid')]) < {count}: time.sleep(0.01)\n"
                  f"time.sleep(600)\n")
        started = time.monotonic()
        outcome = br.run_bounded([sys.executable, "-c", parent], timeout=1.0, grace=GRACE)
        took = time.monotonic() - started
        # One shared budget: not `count` graces one after another.
        assert took <= 1.0 + 2 * GRACE + SLACK, took
        assert outcome.status == "timeout" and outcome.escaped == count, outcome
        assert outcome.clean, outcome
        for pidfile in tmp.glob("e*.pid"):
            assert gone(int(pidfile.read_text())), pidfile


def test_output_is_read_within_its_bound() -> None:
    class Counting:
        def __init__(self, handle) -> None:
            self.handle, self.read_bytes = handle, 0

        def fileno(self) -> int:
            return self.handle.fileno()

        def seek(self, offset: int) -> int:
            return self.handle.seek(offset)

        def read(self, size: int = -1) -> bytes:
            data = self.handle.read(size)
            self.read_bytes += len(data)
            return data

    with tempfile.TemporaryFile() as big:
        big.write(b"a" * (10 * 1024 * 1024) + b"END")
        big.flush()
        counted = Counting(big)
        text = br._read_bounded(counted, 1000)
        assert counted.read_bytes <= 1000, counted.read_bytes
        assert text.startswith("a" * 500) and text.endswith("END") and "bytes omitted" in text, text[-60:]


def test_a_run_that_leaves_processes_is_never_a_result() -> None:
    # Cleanup that cannot finish returns within its budget and says so.
    real_alive = br._group_alive
    br._group_alive = lambda pgid: True
    started = time.monotonic()
    try:
        stuck = br.run_bounded([sys.executable, "-c", "pass"], timeout=5, grace=0.2)
    finally:
        br._group_alive = real_alive
    assert time.monotonic() - started <= 2 * 0.2 + SLACK, "an unfinishable cleanup was waited for"
    assert (stuck.status, stuck.leftover_group, stuck.clean) == ("completed", True, False), stuck
    assert "still present after cleanup" in stuck.reason, stuck.reason

    # A gained child that cannot be reaped in time is reported by pid, not waited for.
    ghost = 2 ** 22 + 12345  # above pid_max defaults: never a real process
    real_children = br._own_children
    seen = {"calls": 0}

    def children_with_ghost():
        seen["calls"] += 1
        found = real_children()
        return None if found is None else (found | {ghost} if seen["calls"] > 2 else found)

    br._own_children = children_with_ghost
    started = time.monotonic()
    try:
        haunted = br.run_bounded([sys.executable, "-c", "pass"], timeout=5, grace=0.2)
    finally:
        br._own_children = real_children
    assert time.monotonic() - started <= 2 * 0.2 + SLACK, "an unreapable child was waited for"
    assert haunted.unreaped == (ghost,) and not haunted.clean, haunted

    # In a plan an unclean run is inconclusive whatever its exit code, and the CLI exits 125.
    with tempfile.TemporaryDirectory(prefix="s403.") as d:
        tmp = Path(d)
        fixture(tmp)
        mutants = [br.Mutant("unclean failure", "walk.py", (("    return seen\n", "    return set()\n"),)),
                   br.Mutant("unclean pass", "walk.py", (("    seen = set()\n", "    seen = set()  # x\n"),))]
        real_run = br.run_bounded
        br.run_bounded = lambda argv, **k: br.Outcome("x", "completed", 1 if "failure" in k["label"] else 0, 0.1, 1.0,
                                                      "", "", "still present after cleanup", leftover_group=True)
        try:
            results = br.run_mutation_plan(mutants, [["unused"]], root=tmp, case_seconds=5, total_seconds=30)
            code = br._cli_run(type("A", (), {"case_seconds": 1, "command": ["unused"]})())
        finally:
            br.run_bounded = real_run
        assert [r.result for r in results] == ["inconclusive", "inconclusive"], results
        assert all(r.reason.startswith("processes outlived cleanup") for r in results), results
        assert code == 125, code


CASE_ALARM_SECONDS = 60


def _case_overran(_signum, _frame) -> None:
    raise AssertionError(f"a case ran past {CASE_ALARM_SECONDS}s: the bound it tests did not hold")


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    started = time.monotonic()
    # If run_bounded itself stops bounding, this suite fails instead of hanging.
    signal.signal(signal.SIGALRM, _case_overran)
    for test in tests:
        signal.alarm(CASE_ALARM_SECONDS)
        try:
            test()
        finally:
            signal.alarm(0)
    print(f"bounded_run_test: {len(tests)} cases ok in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
