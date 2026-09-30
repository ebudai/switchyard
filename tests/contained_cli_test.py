#!/usr/bin/env python3
"""Containment for real provider CLIs, proved on a stand-in that daemonises the way Codex does.

SYRD-524: the real-Codex folder-trust cases left Codex app-server daemons
running from their deleted temporary homes, and Codex's plugin clone raced the
homes' removal. The stand-in here does what Codex did -- its daemon setsid()s
and double-forks out of the process group, then keeps writing into
`$HOME/plugins-clone` -- and never touches anything outside a directory this
test created. Every case is bounded, and nothing is chosen by name or path:
what is stopped is what descends from the run.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import bounded_run  # noqa: E402
import contained_cli  # noqa: E402

CHECKS = 0
GRACE = 1.0
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}

#: The stand-in CLI: prints its daemon's pid, then exits, hangs or crashes.
STANDIN = r'''
import os, signal, sys, time
home, mode = sys.argv[1], sys.argv[2]
ignore_term = "--ignore-term" in sys.argv
r, w = os.pipe()
middle = os.fork()
if middle == 0:
    os.setsid()
    if os.fork() == 0:
        if ignore_term:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.close(r)
        # As a daemon does: nothing of its caller's stdio is held open.
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        clone = os.path.join(home, "plugins-clone")
        os.makedirs(clone, exist_ok=True)
        os.write(w, str(os.getpid()).encode())
        os.close(w)
        n, end = 0, time.time() + 300
        while time.time() < end:
            with open(os.path.join(clone, "f%d" % (n % 50)), "w") as handle:
                handle.write("x")
            n += 1
            time.sleep(0.005)
        os._exit(0)
    os._exit(0)
os.close(w)
os.waitpid(middle, 0)
print(os.read(r, 32).decode(), flush=True)
if mode == "hang":
    time.sleep(300)
if mode == "crash":
    raise SystemExit("the stand-in crashed after starting its daemon")
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def standin(tmp: Path) -> Path:
    path = tmp / "standin.py"
    path.write_text(STANDIN, encoding="utf-8")
    return path


def daemon_of(outcome: bounded_run.Outcome) -> int:
    lines = [line for line in outcome.stdout.splitlines() if line.strip().isdigit()]
    check(lines, f"the stand-in never reported its daemon: {outcome}")
    return int(lines[0])


def _bench(case):
    """A scratch directory for the stand-in script itself, outside every contained home."""
    def run() -> None:
        bench = Path(tempfile.mkdtemp(prefix="syrd524-bench."))
        try:
            case(bench)
        finally:
            shutil.rmtree(bench, ignore_errors=True)
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


@_bench
def test_a_daemon_that_left_its_group_is_stopped_before_the_home_goes(bench: Path) -> None:
    script = standin(bench)
    with contained_cli.ContainedHome("syrd524.", grace=GRACE) as home:
        root = home.root
        outcome = home.run([sys.executable, str(script), str(root), "exit"], env=ENV, timeout=30, label="exit")
        daemon = daemon_of(outcome)
        check(outcome.status == "completed" and outcome.returncode == 0, outcome)
        check(outcome.escaped >= 1 and outcome.clean, f"the daemon was not caught: {outcome}")
        check(not alive(daemon), f"daemon {daemon} outlived its run")
        check(contained_cli.processes_under(root) == [], contained_cli.processes_under(root))
        check((root / "plugins-clone").is_dir(), "the stand-in never wrote into its home")
    # Leaving the block removed the home -- with nothing left writing into it,
    # so without the "Directory not empty" race.
    check(not root.exists(), f"{root} was kept")


@_bench
def test_a_daemon_that_ignores_sigterm_is_still_stopped(bench: Path) -> None:
    script = standin(bench)
    with contained_cli.ContainedHome("syrd524.", grace=GRACE) as home:
        outcome = home.run([sys.executable, str(script), str(home.root), "exit", "--ignore-term"],
                           env=ENV, timeout=30, label="ignore-term")
        daemon = daemon_of(outcome)
        check(outcome.clean and not alive(daemon), (outcome, alive(daemon)))
        root = home.root
    check(not root.exists(), root)


@_bench
def test_a_hung_cli_is_bounded_and_still_leaves_nothing(bench: Path) -> None:
    script = standin(bench)
    started = time.monotonic()
    with contained_cli.ContainedHome("syrd524.", grace=GRACE) as home:
        outcome = home.run([sys.executable, str(script), str(home.root), "hang"], env=ENV, timeout=3, label="hang")
        daemon = daemon_of(outcome)
        check(outcome.timed_out, f"a hang read as an answer: {outcome}")
        check(outcome.clean and not alive(daemon), (outcome, alive(daemon)))
        root = home.root
    check(time.monotonic() - started < 3 + 4 * GRACE + 5, "the bounded run was not bounded")
    check(not root.exists(), root)


@_bench
def test_a_crashing_cli_leaves_nothing_either(bench: Path) -> None:
    script = standin(bench)
    with contained_cli.ContainedHome("syrd524.", grace=GRACE) as home:
        outcome = home.run([sys.executable, str(script), str(home.root), "crash"], env=ENV, timeout=30, label="crash")
        daemon = daemon_of(outcome)
        check(outcome.returncode not in (0, None) and "crashed" in outcome.stderr, outcome)
        check(outcome.clean and not alive(daemon), (outcome, alive(daemon)))
        root = home.root
    check(not root.exists(), root)


@_bench
def test_what_this_process_already_had_is_not_touched(bench: Path) -> None:
    """Ownership is ancestry within the run: an earlier child, and an earlier orphan, are left alone."""
    script = standin(bench)
    check(contained_cli.containment_available(), "this host cannot contain a CLI at all")
    earlier = subprocess.Popen(["sleep", "60"])
    # An orphan that was reparented here BEFORE the run: a daemon started outside any contained run.
    first = subprocess.run([sys.executable, str(script), str(bench), "exit"], capture_output=True, text=True,
                           timeout=30)
    orphan = int(first.stdout.split()[0])
    try:
        deadline = time.monotonic() + 5
        while orphan not in (contained_cli.bounded_run._own_children() or set()):
            check(time.monotonic() < deadline, f"the earlier daemon {orphan} was never reparented here")
            time.sleep(0.02)
        with contained_cli.ContainedHome("syrd524.", grace=GRACE) as home:
            outcome = home.run([sys.executable, str(script), str(home.root), "exit"], env=ENV, timeout=30,
                               label="exit")
            check(outcome.clean and not alive(daemon_of(outcome)), outcome)
        check(earlier.poll() is None, "a child this process already had was stopped")
        check(alive(orphan), "an orphan this process already had was stopped")
    finally:
        earlier.kill()
        earlier.wait(timeout=10)
        try:
            os.kill(orphan, signal.SIGKILL)
            os.waitpid(orphan, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


@_bench
def test_something_left_in_the_home_is_reported_and_the_home_kept(bench: Path) -> None:
    home = contained_cli.ContainedHome("syrd524.", grace=GRACE)
    # Started outside any contained run, so no sweep reaches it: the check must.
    stray = subprocess.Popen(["sleep", "60"], cwd=home.root)
    try:
        try:
            home.close()
            raised = ""
        except contained_cli.Unclean as exc:
            raised = str(exc)
        check(f"({stray.pid}, " in raised and "kept" in raised, f"a live process in the home went unreported: {raised!r}")
        check(home.root.exists(), "the home was removed under a live process")
    finally:
        stray.kill()
        stray.wait(timeout=10)
        shutil.rmtree(home.root, ignore_errors=True)


@_bench
def test_a_run_the_sweep_could_not_finish_keeps_the_home(bench: Path) -> None:
    # The runner's own report of a process it could not reap: the home stays.
    home = contained_cli.ContainedHome("syrd524.", grace=GRACE)
    original = contained_cli.bounded_run.run_bounded
    contained_cli.bounded_run.run_bounded = lambda argv, **kw: bounded_run.Outcome(
        label=kw.get("label", ""), status="completed", returncode=0, seconds=0.1, timeout=kw["timeout"],
        stdout="", stderr="", reason="still present after cleanup: group=no unreaped=[424242]",
        unreaped=(424242,))
    try:
        home.run(["true"], env=ENV, timeout=10, label="stuck")
        try:
            home.close()
            raised = ""
        except contained_cli.Unclean as exc:
            raised = str(exc)
        check("unreaped=[424242]" in raised and "kept" in raised, raised)
        check(home.root.exists(), "the home was removed although the sweep said something was left")
    finally:
        contained_cli.bounded_run.run_bounded = original
        shutil.rmtree(home.root, ignore_errors=True)


@_bench
def test_nothing_runs_where_nothing_could_be_contained(bench: Path) -> None:
    home = contained_cli.ContainedHome("syrd524.", grace=GRACE)
    marker = home.root / "ran"
    original = contained_cli.containment_available
    contained_cli.containment_available = lambda: False
    try:
        try:
            home.run(["touch", str(marker)], env=ENV, timeout=10, label="touch")
            raised = ""
        except contained_cli.Unclean as exc:
            raised = str(exc)
        check("cannot contain" in raised and not marker.exists(), (raised, marker.exists()))
    finally:
        contained_cli.containment_available = original
        home.close()


@_bench
def test_the_old_pattern_leaks_the_daemon_and_containment_is_what_stops_it(bench: Path) -> None:
    """The reproduction, itself contained: the SYRD-279 shape leaves the daemon; only the outer sweep stops it."""
    script = standin(bench)
    old = (
        "import subprocess, sys, tempfile\n"
        f"with tempfile.TemporaryDirectory(prefix='syrd524-old.', dir={str(bench)!r}) as tmp:\n"
        f"    run = subprocess.run([sys.executable, {str(script)!r}, tmp, 'exit'], capture_output=True, text=True)\n"
        "    print(run.stdout.strip(), flush=True)\n"
    )
    outcome = bounded_run.run_bounded([sys.executable, "-c", old], timeout=30, env=ENV, grace=GRACE,
                                      label="the old pattern")
    daemon = daemon_of(outcome)
    # The old pattern returned with its daemon still running: the outer runner
    # had to catch it after the fact. That is the leak.
    check(outcome.escaped >= 1, f"the old pattern left nothing behind: {outcome}")
    check(outcome.clean and not alive(daemon), outcome)


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"contained_cli_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
