#!/usr/bin/env python3
"""SYRD-681: no fixture process outlives its test, however the test ends.

A harness owns a run (`fixture_processes.owned`) and starts what SYRD-616's suites start, with their own fixture code:
the fake tmux server, a provider in its pane, the provider's app-server daemon in a session of its own, a tool command
spinning `while :; do :; done # stale-681` in a session of its own, and a tool command whose own child (`sleep`) no
parent-death signal reaches. It records each one's pid and start time, then ends:

* normally; with a failed assertion;
* by SIGTERM, as a timeout ends it; by SIGINT; by SIGHUP, as a closed session does;
* by SIGKILL, which runs nothing at all, as a killed parent or an exhausted budget does.

After each, every one of those identities is gone within a bound -- checked by pid AND start time -- and so is the
run's guardian. A decoy outside the run, with exactly the busy loop's command line, is still running: nothing is
stopped by its name.

The suite owns what it starts itself -- the decoy, each harness, the identity test's sleeper -- in a run of its own,
so a SIGKILLed suite leaves none of them either: `--suite-probe` is the suite as an external timeout finds it, and
`test_a_killed_suite_leaves_nothing` kills it and checks every outer and inner identity (Audit on 8f59b613).
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture_processes as fp  # noqa: E402

CHECKS = 0
BUSY = "while :; do :; done # stale-681"
GONE_WITHIN = 15.0

HARNESS = """
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import fixture_processes as fp
work, mode = Path(sys.argv[2]), sys.argv[3]
try:
  with fp.owned(work) as run:
      ctl = work / "ctl"
      ctl.mkdir()
      tmux = run.popen([sys.executable, "-c", fp.TMUX_SERVER,
                        json.dumps([[sys.executable, "-c", fp.PROVIDER, str(ctl), "app-server-daemon-681"],
                                    ["sleep", "600"]]),  # a pane that sets nothing itself: only its start reaches it
                        str(work / "panes.json")], stdin=subprocess.DEVNULL)
      # One of the test's own, as SYRD-616's suite starts a service: only the run's own start reaches it.
      service = run.popen(["sleep", "601"], start_new_session=True, stdin=subprocess.DEVNULL)
      def wait_for(path):
          for _ in range(400):
              if path.exists() and path.read_text().strip():
                  return path.read_text()
              time.sleep(0.025)
          raise AssertionError(f"never written: {path}")
      provider, bare_pane = json.loads(wait_for(work / "panes.json"))
      def spawn(n, command):
          (ctl / f"spawn-{n:03d}.json").write_text(json.dumps({"cwd": str(work), "command": command}))
          return int(wait_for(ctl / f"spawn-{n:03d}.pid"))
      busy = spawn(0, sys.argv[4])
      # Its own child ignores TERM, as a stubborn tool's child may: only the KILL that follows stops it.
      nested = spawn(1, "(trap '' TERM; exec sleep 600) & wait")
      # A command that drops its environment, and with it the run's mark: only the parent-death signal reaches it.
      unmarked = spawn(2, "exec env -i bash -c 'while :; do sleep 0.2; done # unmarked-681'")
      def children(parent, needle):
          for _ in range(400):
              for entry in Path("/proc").iterdir():
                  try:
                      stat = (entry / "stat").read_text()
                      cmd = (entry / "cmdline").read_bytes()
                  except OSError:
                      continue
                  if entry.name.isdigit() and int(stat.rsplit(")", 1)[1].split()[1]) == parent and needle in cmd:
                      return int(entry.name)
              time.sleep(0.025)
          raise AssertionError(f"no child of {parent} with {needle}")
      daemon = children(provider, b"app-server-daemon-681")
      grandchild = children(nested, b"sleep")
      ids = {name: [pid, fp.start_time(pid)] for name, pid in
             {"tmux": tmux.pid, "provider": provider, "daemon": daemon, "busy": busy, "nested": nested,
              "grandchild": grandchild, "unmarked": unmarked, "bare_pane": bare_pane, "service": service.pid,
              "guardian": run.guardian.pid}.items()}
      (work / "ids.json").write_text(json.dumps(ids))
      if mode == "ok":
          pass
      elif mode == "fail":
          raise AssertionError("FAILED CHECK: a test that fails")
      else:
          time.sleep(600)
finally:
    # What is still alive the moment the run's own cleanup has finished, before this process exits and the
    # parent-death signal or the guardian could do it instead.
    left = run.sentinel() if "run" in dir() else ["no run"]
    (work / "finally").write_text(json.dumps(left))
"""


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def gone(ids: dict, within: float = GONE_WITHIN) -> list[str]:
    """Which identities are still alive after `within` seconds."""
    deadline = time.monotonic() + within
    while True:
        alive = [name for name, (pid, start) in ids.items() if fp.start_time(pid) == start]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


def start_harness(work: Path, mode: str, log) -> subprocess.Popen:
    """A harness, owned by this suite's own run (Audit on 8f59b613): it dies with the suite, and the suite's guardian
    stops it after a SIGKILLed suite. Its own run, inside, has its own mark."""
    return fp.current.popen([sys.executable, "-c", HARNESS, str(HERE), str(work), mode, BUSY],
                            start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)


def start_decoy() -> subprocess.Popen:
    """The busy loop's exact command line (sleeping, not spinning), outside every harness's run -- but this suite's:
    a harness's cleanup must leave it alone, and the suite's own end must not."""
    return fp.current.popen(["bash", "-c", BUSY.replace(":; done", "sleep 0.2; done")], start_new_session=True)


def run_harness(mode: str, end: int | None, guardian_too: bool = False,
                then: int | None = None) -> tuple[dict, int | None, str, list | None]:
    # A harness's guardian may still be writing its ledger here after the harness exits: the directory outlives it
    # until its processes are gone, and a straggling write never fails the case -- the identity checks do that.
    with tempfile.TemporaryDirectory(prefix=f"syrd681-{mode}.", ignore_cleanup_errors=True) as raw:
        work = Path(raw)
        # Files, not pipes: a process that wrongly survives must not hold the harness's output open.
        log = open(work / "harness.log", "w+")
        harness = start_harness(work, mode, log)
        deadline = time.monotonic() + 30
        while not (work / "ids.json").exists() and harness.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        ids = json.loads((work / "ids.json").read_text()) if (work / "ids.json").exists() else {}
        if guardian_too and ids:
            fp.signal_identity(*ids["guardian"], signal.SIGKILL)
        if end is not None and harness.poll() is None:
            os.kill(harness.pid, end)
        if then is not None:
            time.sleep(0.4)  # inside the cleanup's TERM grace: the TERM-proof grandchild keeps it waiting
            if harness.poll() is None:
                os.kill(harness.pid, then)
        try:
            harness.wait(timeout=30)
        except subprocess.TimeoutExpired:
            harness.kill()
            harness.wait(timeout=10)
        log.seek(0)
        err = log.read()
        log.close()
        gone({name: ident for name, ident in ids.items() if not (guardian_too and name == "grandchild")})
        cleaned = json.loads((work / "finally").read_text()) if (work / "finally").exists() else None
        return ids, harness.returncode, err[-600:], cleaned


def test_every_way_out_leaves_nothing() -> None:
    # A decoy outside the run, with exactly the busy loop's command line (sleeping, not spinning): it must survive.
    decoy = start_decoy()
    try:
        decoy_start = fp.start_time(decoy.pid)
        for mode, end, label in (("ok", None, "a test that passes"), ("fail", None, "a test whose check fails"),
                                 ("hang", signal.SIGTERM, "a test its timeout TERMs"),
                                 ("hang", signal.SIGINT, "a test interrupted"),
                                 ("hang", signal.SIGHUP, "a test whose session closes"),
                                 ("hang", signal.SIGKILL, "a test SIGKILLed, which runs no cleanup of its own"),
                                 # Audit on 4044ec42: a second signal while the cleanup waits out its TERM grace.
                                 ("hang", (signal.SIGTERM, signal.SIGHUP), "a TERMed test HUPed during its cleanup"),
                                 ("hang", (signal.SIGTERM, signal.SIGINT), "a TERMed test interrupted during its cleanup"),
                                 ("hang", (signal.SIGTERM, signal.SIGTERM), "a TERMed test TERMed again during its cleanup"),
                                 # ... and one while a test that passed is cleaning up: honoured once it is done.
                                 ("ok", (None, signal.SIGTERM), "a passing test TERMed during its cleanup")):
            end, then = end if isinstance(end, tuple) else (end, None)
            ids, code, err, cleaned = run_harness(mode, end, then=then)
            check(set(ids) == {"tmux", "provider", "daemon", "busy", "nested", "grandchild", "unmarked", "bare_pane",
                               "service", "guardian"}
                  and all(start for _pid, start in ids.values()),
                  f"{label}: the harness started every fixture process, each known by pid and start time: {ids} {err}")
            left = gone(ids)
            check(left == [], f"{label} (exit {code}): every fixture process is gone, by identity: still alive {left}")
            check(mode != "fail" or "a test that fails" in err, f"{label}: the failure itself is reported: {err}")
            check((cleaned == []) if end != signal.SIGKILL else cleaned is None,
                  f"{label}: the test's own cleanup ran, and left nothing, unless it was SIGKILLed: {cleaned}")
            want = 128 + end if end not in (None, signal.SIGKILL) else (128 + then if then else None)
            check(want is None or code == want, f"{label}: it exits as the first signal says ({want}): {code}")
            check(fp.start_time(decoy.pid) == decoy_start,
                  f"{label}: the decoy with the same command line, outside the run, still runs")
        # The guardian killed too, then the test: only the parent-death chain is left, and it ends every fixture
        # process but a tool command's own child, which only the guardian reaches -- that one is stopped here.
        ids, code, err, cleaned = run_harness("hang", signal.SIGKILL, guardian_too=True)
        chain = {name: ident for name, ident in ids.items() if name not in ("grandchild", "guardian")}
        check(len(chain) == 8 and gone(chain) == [],
              f"with the guardian gone too, the parent-death signal alone ends the server, the provider, its daemon and "
              f"its commands: still alive {gone(chain, 0.0)}")
        if "grandchild" in ids:
            fp.signal_identity(*ids["grandchild"], signal.SIGKILL)
        check(fp.start_time(decoy.pid) == decoy_start, "and the decoy still runs")
    finally:
        fp.signal_identity(decoy.pid, fp.start_time(decoy.pid) or -1, signal.SIGKILL)
        decoy.wait(timeout=10)


def test_the_sentinel_names_a_marked_process_the_ledger_missed() -> None:
    """A process the run started without recording it -- a tool's own child -- is named by its mark, and stopped."""
    with tempfile.TemporaryDirectory(prefix="syrd681-sentinel.") as raw:
        with fp.owned(raw) as run:
            stray = subprocess.Popen(["sleep", "30"], start_new_session=True)  # not run.popen: not in the ledger
            start = fp.start_time(stray.pid)
            named = run.sentinel()
        check(any(line.startswith(f"{stray.pid} {start} ") for line in named),
              f"the sentinel names a marked process the ledger never recorded: {named}")
        stray.wait(timeout=10)
        check(fp.start_time(stray.pid) is None and run.sentinel() == [], "and the run's end stopped it")


def test_a_ledger_that_cannot_be_written_never_stops_a_cleanup() -> None:
    """A guardian whose test's directory is already gone still stops what carries the mark."""
    run_id = "syrd681-noledger-" + os.urandom(4).hex()
    marked = fp.current.popen(["sleep", "30"], start_new_session=True, env={**os.environ, fp.RUN_ENV: run_id})
    start = fp.start_time(marked.pid)
    try:
        left = fp.stop_marked(run_id, "/nonexistent-syrd681/fixture-processes.ledger", grace=1.0)
    except Exception as exc:  # the failure is this check's, not a crash
        left = {"raised": repr(exc)}
    try:
        marked.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # still running: the check below says so, and the run's own end stops it
    check(left == {} and fp.start_time(marked.pid) != start,
          f"with its ledger unwritable, the cleanup still stops the run's marked process: {left}")


def test_identity_not_name() -> None:
    """A pid whose start time does not match is never signalled: a reused number is another process."""
    sleeper = fp.current.popen(["sleep", "30"])
    try:
        start = fp.start_time(sleeper.pid)
        check(fp.signal_identity(sleeper.pid, start + 1, signal.SIGKILL) is False and fp.start_time(sleeper.pid) == start,
              "a recorded identity whose start time is another's sends nothing")
        check(fp.signal_identity(sleeper.pid, start, signal.SIGKILL) is True, "the exact identity is signalled")
        sleeper.wait(timeout=10)
        check(fp.signal_identity(sleeper.pid, start, signal.SIGKILL) is False, "and once gone, nothing is sent again")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


def suite_probe(work: Path) -> int:
    """`--suite-probe DIR`: this suite as an external timeout finds it -- its own run, a decoy, and a harness
    mid-run -- recorded, then waiting to be killed."""
    with fp.owned(work) as run:
        decoy = start_decoy()
        inner = work / "inner"
        inner.mkdir()
        log = open(work / "harness.log", "w")
        harness = start_harness(inner, "hang", log)
        for _ in range(600):
            if (inner / "ids.json").exists():
                break
            time.sleep(0.05)
        ids = json.loads((inner / "ids.json").read_text())
        outer = {"decoy": decoy.pid, "harness": harness.pid, "suite_guardian": run.guardian.pid}
        (work / "probe.json").write_text(json.dumps(
            {**{name: [pid, fp.start_time(pid)] for name, pid in outer.items()}, **{f"inner_{k}": v for k, v in ids.items()}}))
        time.sleep(600)
    return 0


def test_a_killed_suite_leaves_nothing() -> None:
    """Audit on 8f59b613: the suite itself SIGKILLed -- an external timeout -- leaves none of its own processes: not
    its decoy, not a harness mid-run, not anything that harness started, not the suite's guardian."""
    with tempfile.TemporaryDirectory(prefix="syrd681-probe.", ignore_cleanup_errors=True) as raw:
        work = Path(raw)
        probe = fp.current.popen([sys.executable, str(Path(__file__).resolve()), "--suite-probe", str(work)],
                                 start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not (work / "probe.json").exists() and probe.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        ids = json.loads((work / "probe.json").read_text()) if (work / "probe.json").exists() else {}
        check({"decoy", "harness", "suite_guardian", "inner_busy", "inner_daemon", "inner_grandchild"} <= set(ids)
              and all(start for _pid, start in ids.values()),
              f"the probe suite started its decoy, a harness, and the harness's fixtures, each by identity: {sorted(ids)}")
        os.kill(probe.pid, signal.SIGKILL)
        probe.wait(timeout=10)
        left = gone(ids)
        check(left == [], f"the suite SIGKILLed: every process it owned, outer and inner, is gone: still alive {left}")


def main() -> int:
    if sys.argv[1:2] == ["--suite-probe"]:
        return suite_probe(Path(sys.argv[2]))
    # This suite owns what it starts, like any other (Audit on 8f59b613): its decoys, harnesses and sleepers die with
    # it however it ends, and are proved gone at the end.
    with tempfile.TemporaryDirectory(prefix="syrd681-suite.") as own:
        with fp.owned(own) as run:
            _run_cases()
        left = run.sentinel()
    check(left == [], f"every process this suite started is gone, by pid and start time: {left}")
    print(f"fixture_processes_test: {CHECKS} checks ok")
    return 0


def _run_cases() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()


if __name__ == "__main__":
    raise SystemExit(main())
