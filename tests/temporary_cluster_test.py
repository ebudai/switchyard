#!/usr/bin/env python3
"""A test cluster must not survive the test process (SYRD-54).

An orphaned cluster was found in /tmp, idle for 3.6 hours, holding a port and its
shared buffers after the run that started it was gone. `pg_ctl start` daemonises,
so the only thing that ever stopped one was reaching the `finally` -- which an
interrupted run never does.

Each case here kills a real run in a different way and then asks the kernel
whether the postmaster is still there.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from standalone_test_runner import run_module_tests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import temporary_cluster as tc  # noqa: E402

HELPER = ROOT / "tests" / "temporary_cluster.py"


def _await_gone(pid: int, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not tc._running(pid):
            return True
        time.sleep(0.05)
    return False


def _await_absent(path: Path, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return True
        time.sleep(0.05)
    return False


def _start_helper() -> tuple[subprocess.Popen[str], int, Path]:
    """Run a cluster in another process and wait until it says where it is.

    Through the shared spawner: a helper started any other way outlives this
    runner and takes its cluster with it (SYRD-57).
    """
    child = tc.spawn_tied(
        [sys.executable, str(HELPER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT / "tests"),
    )
    line = child.stdout.readline().split()
    assert len(line) == 3, (line, child.poll())
    return child, int(line[0]), Path(line[1])


# --- the interruptions that used to leak ------------------------------------


def test_a_kill_nine_still_takes_the_cluster_down() -> None:
    """The case that leaked: nothing in the process runs, so the kernel does it."""
    child, postmaster, root = _start_helper()
    try:
        assert tc._running(postmaster)
        child.kill()
        child.wait(timeout=20)
        assert _await_gone(postmaster), f"postmaster {postmaster} outlived a SIGKILLed run"
    finally:
        subprocess.run(["rm", "-rf", str(root)], check=False)


def test_a_terminated_run_stops_the_cluster_and_removes_its_tree() -> None:
    """A harness timeout sends SIGTERM; the handler cleans up and re-raises it."""
    child, postmaster, root = _start_helper()
    child.terminate()
    child.wait(timeout=30)
    assert _await_gone(postmaster), f"postmaster {postmaster} outlived a terminated run"
    assert not root.exists(), root
    # Re-raised rather than swallowed: the harness still sees the signal it sent.
    assert child.returncode == -signal.SIGTERM, child.returncode


def test_an_interrupted_run_stops_the_cluster_and_removes_its_tree() -> None:
    """Ctrl-C is the same path, and must not be reported as a clean exit."""
    child, postmaster, root = _start_helper()
    child.send_signal(signal.SIGINT)
    child.wait(timeout=30)
    assert _await_gone(postmaster), f"postmaster {postmaster} outlived an interrupted run"
    assert not root.exists(), root
    assert child.returncode == -signal.SIGINT, child.returncode


# --- the paths that already worked, pinned so they keep working --------------


def test_the_body_gets_a_usable_cluster_and_leaves_nothing_after_it() -> None:
    with tc.temporary_cluster(prefix="temporary-cluster-success.") as cluster:
        answer = subprocess.run(
            ["psql", "-X", "-tA", cluster.conninfo("postgres"), "-c", "select 42"],
            capture_output=True,
            text=True,
        )
        assert answer.returncode == 0, answer.stderr
        assert answer.stdout.strip() == "42"
        postmaster, root = cluster.pid, cluster.root
    assert _await_gone(postmaster)
    assert not root.exists(), root


def test_an_assertion_failure_inside_the_body_still_cleans_up() -> None:
    seen: dict[str, object] = {}
    try:
        with tc.temporary_cluster(prefix="temporary-cluster-failure.") as cluster:
            seen["pid"] = cluster.pid
            seen["root"] = cluster.root
            raise AssertionError("the scenario failed, as scenarios do")
    except AssertionError as exc:
        assert "the scenario failed" in str(exc)
    else:
        raise AssertionError("the failure was swallowed")
    assert _await_gone(int(seen["pid"]))
    assert not Path(str(seen["root"])).exists(), seen["root"]


# --- the sweep that collects what a kill -9 could not ------------------------


def _fake_tree(base: Path, name: str, *, pid: int | None, age: float) -> Path:
    tree = base / name
    (tree / "pgdata").mkdir(parents=True)
    (tree / "pgdata" / "PG_VERSION").write_text("17\n", encoding="utf-8")
    if pid is not None:
        (tree / "pgdata" / "postmaster.pid").write_text(f"{pid}\n/data\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(tree, (stamp, stamp))
    return tree


def test_the_sweep_removes_only_an_abandoned_tree() -> None:
    with tempfile.TemporaryDirectory(prefix="temporary-cluster-sweep.") as tmp:
        base = Path(tmp)
        # 4 billion is above any pid_max in use, so it names nothing running.
        abandoned = _fake_tree(base, "live-sync.abandoned", pid=4_000_000_000, age=7200)
        no_pidfile = _fake_tree(base, "live-sync.crashed", pid=None, age=7200)
        running = _fake_tree(base, "live-sync.running", pid=os.getpid(), age=7200)
        recent = _fake_tree(base, "live-sync.recent", pid=4_000_000_000, age=5)
        other = _fake_tree(base, "something-else.tree", pid=4_000_000_000, age=7200)
        not_a_cluster = base / "live-sync.empty"
        not_a_cluster.mkdir()
        os.utime(not_a_cluster, (time.time() - 7200, time.time() - 7200))

        removed = tc.sweep_stale_trees("live-sync.", base=base)

        assert sorted(path.name for path in removed) == ["live-sync.abandoned", "live-sync.crashed"]
        assert not abandoned.exists() and not no_pidfile.exists()
        # A live cluster, a run that may have started seconds ago, another
        # prefix, and a directory that is not one of ours are all left alone.
        for kept in (running, recent, other, not_a_cluster):
            assert kept.exists(), kept


# --- the fixture that leaked --------------------------------------------------


def test_the_live_sync_fixture_no_longer_daemonises_its_cluster() -> None:
    source = (ROOT / "tests" / "ticket_board_live_sync_test.py").read_text(encoding="utf-8")
    assert 'temporary_cluster(prefix="ticket-board-live-sync.")' in source
    # Read what the fixture runs, not what it says about itself: the comment
    # explaining this change names `pg_ctl` too.
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "pg_ctl" not in code, "the fixture still starts a cluster it cannot take with it"
    assert "initdb" not in code


# --- the guard that keeps this from coming back -------------------------------


CLUSTER_TOOLS = ("initdb", "pg_ctl", "postgres")
# The helper is the one place allowed to start a cluster, and this module names
# the tools in order to look for them.
EXEMPT = {"temporary_cluster.py", Path(__file__).name}


def _cluster_starts(path: Path) -> list[str]:
    """Direct uses of the cluster tools in one test module, argv or shell string."""
    source = path.read_text(encoding="utf-8")
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # One line naming the command and the action, so prose that happens to
            # contain both words -- a docstring explaining this rule, for one --
            # is not read as an invocation.
            for line in node.value.splitlines():
                if re.search(r"\bpg_ctl\b[^\n]*\bstart\b", line):
                    found.append(f"shell string: {line.strip()[:60]}")
            continue
        if not isinstance(node, ast.Call) or not node.args:
            continue
        argv = node.args[0]
        if not isinstance(argv, ast.List) or not argv.elts:
            continue
        first = argv.elts[0]
        if isinstance(first, ast.Constant) and first.value in CLUSTER_TOOLS:
            found.append(f"argv: {first.value}")
    return found


def test_no_test_module_starts_a_cluster_of_its_own() -> None:
    """A cluster a test starts itself is one nothing takes down when it is killed.

    Only test modules are scanned. The board's own runtime uses of `pg_ctl` and
    `initdb` -- provisioning a tenant, deploying a release -- are production
    behaviour on a cluster nobody is disposing of, and are deliberately outside
    this guard.
    """
    offenders = {
        path.name: starts
        for path in sorted((ROOT / "tests").glob("*.py"))
        if path.name not in EXEMPT and (starts := _cluster_starts(path))
    }
    assert offenders == {}, offenders


def test_the_guard_would_catch_a_fixture_that_starts_its_own_cluster() -> None:
    """A guard that finds nothing anywhere would pass forever."""
    with tempfile.TemporaryDirectory(prefix="cluster-guard-sample.") as tmp:
        sample = Path(tmp) / "sample_test.py"
        sample.write_text(
            "import subprocess\n"
            "def test_x():\n"
            "    subprocess.run(['initdb', '-D', 'data'])\n"
            "    subprocess.run(['pg_ctl', '-D', 'data', '-w', 'start'])\n",
            encoding="utf-8",
        )
        assert _cluster_starts(sample) == ["argv: initdb", "argv: pg_ctl"]

        shell = Path(tmp) / "shell_test.py"
        shell.write_text('CMD = "pg_ctl -D data -w start"\n', encoding="utf-8")
        assert _cluster_starts(shell), "a shell string form must be caught too"

        clean = Path(tmp) / "clean_test.py"
        clean.write_text(
            "from temporary_cluster import temporary_cluster\n"
            "def test_y():\n"
            "    with temporary_cluster(prefix='x.') as cluster:\n"
            "        assert cluster.port\n",
            encoding="utf-8",
        )
        assert _cluster_starts(clean) == []


def test_a_cluster_that_will_not_start_says_what_postgres_said() -> None:
    """`pg_ctl -w start` printed the refusal itself; nothing else may swallow it."""
    with tempfile.TemporaryDirectory(prefix="cluster-log.") as tmp:
        log = Path(tmp) / "postgres.log"
        log.write_text(
            'FATAL:  data directory "/nowhere" has invalid permissions\n', encoding="utf-8"
        )
        dead = subprocess.Popen(["false"])
        dead.wait()
        try:
            tc._await_ready(Path(tmp), 1, dead, time.monotonic() + 1.0, log)
        except AssertionError as exc:
            assert "has invalid permissions" in str(exc), exc
        else:
            raise AssertionError("a dead postmaster was reported as ready")

        # And a missing log is reported as missing rather than crashing the report.
        assert "no postgres log" in tc._log_tail(Path(tmp) / "absent.log")


def test_the_running_cluster_keeps_its_log_where_a_fixture_can_read_it() -> None:
    with tc.temporary_cluster(prefix="cluster-log-live.") as cluster:
        assert cluster.log_path.is_file(), cluster.log_path
        assert "database system is ready" in cluster.log_path.read_text(encoding="utf-8")


def test_the_named_shutdown_is_the_signal_the_cluster_gets() -> None:
    """A fixture that asked `pg_ctl -m immediate` must not quietly get fast."""
    for mode, expected in (("fast", signal.SIGINT), ("immediate", signal.SIGQUIT)):
        child = subprocess.Popen(["sleep", "60"])
        tc._shut_down(child, shutdown=mode)
        assert child.returncode == -expected, (mode, child.returncode)
    # An unknown mode is not a crash and not silence: it takes the safe default.
    child = subprocess.Popen(["sleep", "60"])
    tc._shut_down(child, shutdown="nonsense")
    assert child.returncode == -signal.SIGINT, child.returncode


# --- the helper itself, not just its cluster ---------------------------------


def test_killing_the_outer_runner_takes_the_helper_and_its_cluster() -> None:
    """The gap SYRD-54 and SYRD-56 left: a helper adopted by init keeps its cluster.

    The runner here is a separate process that starts the helper exactly as a
    test does. SIGKILL leaves it no chance to clean up, so what happens next is
    the kernel's doing and nothing else's.
    """
    runner = subprocess.Popen(
        [sys.executable, str(HELPER), "--run-a-helper"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT / "tests"),
    )
    line = runner.stdout.readline().split()
    assert len(line) == 3, (line, runner.poll(), runner.stderr.read() if runner.poll() else "")
    helper_pid, postmaster, root = int(line[0]), int(line[1]), Path(line[2])
    try:
        assert tc._running(helper_pid) and tc._running(postmaster)

        runner.kill()
        runner.wait(timeout=20)

        assert _await_gone(helper_pid), f"helper {helper_pid} outlived the runner"
        assert _await_gone(postmaster), f"postmaster {postmaster} outlived the runner"
        # The helper had time to leave through its own cleanup, so the tree goes
        # with it rather than waiting for the next run's sweep.
        assert _await_absent(root), root
    finally:
        for pid in (postmaster, helper_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)


def test_a_helper_that_inherited_an_ignored_sigterm_still_dies() -> None:
    """The stale helper absorbed the signal it was told to die from.

    A process started with SIGTERM ignored passes that on to its children. The
    cleanup handler installed itself over the inherited disposition, tidied up
    on SIGTERM, restored the ignore and re-raised -- so the signal did nothing
    and the helper stayed asleep. SIGKILL was the only thing left.
    """
    child = tc.spawn_tied(
        [sys.executable, str(HELPER)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=str(ROOT / "tests"),
        preexec_fn=lambda: (
            tc.die_with_parent(),
            signal.signal(signal.SIGTERM, signal.SIG_IGN),
        ),
    )
    line = child.stdout.readline().split()
    assert len(line) == 3, (line, child.poll())
    postmaster, root = int(line[0]), Path(line[1])
    try:
        child.terminate()
        child.wait(timeout=30)
        assert _await_gone(postmaster), postmaster
        assert not root.exists(), root
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(child.pid, signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)


# The launcher goes through the shared spawner, so the identity it passes is the
# one the real path passes -- and then takes both signal protections away: its own
# preexec_fn replaces the parent-death signal with nothing and ignores SIGTERM. It
# exits without waiting for readiness, so the helper is orphaned while its cluster
# is still being created.
ORPHANING_LAUNCHER = """
import os, signal, subprocess, sys
sys.path.insert(0, os.path.dirname(sys.argv[1]))
import temporary_cluster as tc

log = open(sys.argv[2], "w")
child = tc.spawn_tied(
    [sys.executable, sys.argv[1]],
    stdout=log,
    stderr=subprocess.DEVNULL,
    text=True,
    preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),
)
print(child.pid, flush=True)
"""


def test_an_orphaned_helper_leaves_even_if_no_signal_reaches_it() -> None:
    """The last line of defence: no parent, no reason to be running.

    The launcher exits before the helper is ready, so the orphaning lands in the
    middle of cluster creation rather than wherever the scheduler happens to put
    it -- and it is started with the parent-death signal and SIGTERM both taken
    away, so nothing but the helper's own check can end it.
    """
    with tempfile.TemporaryDirectory(prefix="orphaned-helper.") as tmp:
        announced = Path(tmp) / "announced.txt"
        launcher = subprocess.Popen(
            [sys.executable, "-c", ORPHANING_LAUNCHER, str(HELPER), str(announced)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT / "tests"),
        )
        first = launcher.stdout.readline().split()
        assert len(first) == 1, (first, launcher.stderr.read())
        helper_pid = int(first[0])
        launcher.wait(timeout=20)
        # Proved, not assumed: the launcher is gone before the cluster is up.
        assert launcher.returncode == 0, launcher.stderr.read()
        assert not announced.read_text(encoding="utf-8").strip(), "the helper was ready too soon"

        deadline = time.monotonic() + 60.0
        line: list[str] = []
        while time.monotonic() < deadline and len(line) != 3:
            line = announced.read_text(encoding="utf-8").split()
            time.sleep(0.05)
        assert len(line) == 3, f"the orphaned helper never announced its cluster: {line}"
        postmaster, root = int(line[0]), Path(line[1])

        try:
            assert _await_gone(helper_pid, timeout=30), f"orphaned helper {helper_pid} stayed"
            assert _await_gone(postmaster, timeout=30), postmaster
            assert _await_absent(root), root
        finally:
            for pid in (postmaster, helper_pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            shutil.rmtree(root, ignore_errors=True)


REPORT_PDEATHSIG = """
import ctypes
buf = ctypes.c_int(0)
# PR_GET_PDEATHSIG is 2; it writes the signal number the kernel will send.
ctypes.CDLL("libc.so.6", use_errno=True).prctl(2, ctypes.byref(buf), 0, 0, 0)
print(buf.value, flush=True)
"""


def test_the_shared_spawner_arms_the_parent_death_signal() -> None:
    """Pinned on its own, because the orphan check would otherwise cover for it.

    Both protections end a helper whose runner has died, so removing either one
    leaves the other passing. This asks the kernel directly what it will send.
    """
    tied = tc.spawn_tied(
        [sys.executable, "-c", REPORT_PDEATHSIG], stdout=subprocess.PIPE, text=True
    )
    armed, _ = tied.communicate(timeout=20)
    assert int(armed.strip()) == int(signal.SIGTERM), armed

    untied = subprocess.Popen(
        [sys.executable, "-c", REPORT_PDEATHSIG], stdout=subprocess.PIPE, text=True
    )
    default, _ = untied.communicate(timeout=20)
    assert int(default.strip()) == 0, f"a plain Popen should arm nothing, got {default}"


def main() -> int:
    run_module_tests(globals())
    print("temporary_cluster_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
