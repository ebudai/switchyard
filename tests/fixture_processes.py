#!/usr/bin/env python3
"""Fixture processes that cannot outlive their test (SYRD-681).

SYRD-616's suites start a fake tmux server, providers that run "tool" commands each in a session of its own, and an
app-server daemon each provider starts in a session of its own. A session of its own is the point -- it is what the
board must cope with -- and it is also why nothing reached them: a timeout's TERM to the test's process group missed
them, the default TERM ended the test without its `finally`, and the daemon was never tracked at all. On 2026-10-25 a
`while :; do :; done # stale-616` ran for 25 hours, reparented into the user session.

`owned()` gives a test run four boundaries, every one by process identity -- pid AND start time, signalled through a
pidfd -- never by command name:

* **Signals.** While a run is owned, SIGTERM, SIGINT and SIGHUP raise SystemExit in the main thread, so a timeout, an
  interrupt or a closed session runs every `finally` -- this module's cleanup among them. The cleanup itself is not
  re-entrant: once it begins, a further signal is recorded and honoured after it, never raised into it, and the
  guardian is retired only once the run's sentinel is empty.
* **The parent's death.** Each fixture process is started with PR_SET_PDEATHSIG (SIGKILL) against the process that
  started it (`DIE_WITH_PARENT` in fixture scripts, `preexec()` for the test's own), so the kernel ends the chain
  when its parent dies, even when the test itself is SIGKILLed and runs nothing.
* **The run's mark.** Every process the run starts inherits `RUN_ENV=<run id>`. A unique id per run, so another run's
  processes, or anything else with the same command line, never match. Cleanup finds every process carrying it -- a
  tool command's own children too -- and records its identity in the run's ledger.
* **A guardian.** A small process in a session of its own waits for the test process to end, by pidfd, then stops
  whatever still carries the mark, and exits. It is what cleans up after a test that was SIGKILLed.

At the end, `sentinel()` answers which recorded identities -- and which marked processes -- are still alive.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

RUN_ENV = "SWITCHYARD_TEST_FIXTURE_RUN"
LEDGER_ENV = "SWITCHYARD_TEST_FIXTURE_LEDGER"
STOP_GRACE_SECONDS = 2.0
#: The run `owned()` is in, for a suite's own Popen calls; None outside one.
current: "Run | None" = None

#: For fixture scripts: die with the process that started this one, checked against it, so a parent already gone
#: when this starts is not missed. Paste at the top of a `python -c` script; call `die_with_parent(os.getppid())`
#: first, and pass `preexec_fn=child_dies_with_me()` to each child it starts.
DIE_WITH_PARENT = """
import ctypes as _ct, os as _os, signal as _sg
def die_with_parent(parent):
    _ct.CDLL(None, use_errno=True).prctl(1, int(_sg.SIGKILL), 0, 0, 0)  # PR_SET_PDEATHSIG
    if _os.getppid() != parent:
        _os._exit(1)
def child_dies_with_me():
    me = _os.getpid()
    return lambda: die_with_parent(me)
"""

#: SYRD-616's fake tmux server: a process named `tmux: server` whose children are the panes, each in a session of
#: its own, as tmux starts them. It and its panes die with their parent.
TMUX_SERVER = DIE_WITH_PARENT + """
import json, os, signal, subprocess, sys, time
die_with_parent(int(os.environ["SWITCHYARD_TEST_FIXTURE_PARENT"]))
open('/proc/self/comm', 'w').write('tmux: server')
panes = [subprocess.Popen(argv, start_new_session=True, preexec_fn=child_dies_with_me()) for argv in json.loads(sys.argv[1])]
open(sys.argv[2], 'w').write(json.dumps([p.pid for p in panes]))
def stop(*_):
    for p in panes:
        try: p.terminate()
        except OSError: pass
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.1)
"""

#: SYRD-616's provider: it runs each `spawn-*.json` request's command in a session of its own, as a CLI's tool does,
#: and with a second argument starts a daemon of its own in a session of its own, as Codex starts its app-server
#: (SYRD-589). Each dies with the provider, which dies with its pane.
PROVIDER = DIE_WITH_PARENT + """
import json, os, subprocess, sys, time
from pathlib import Path
die_with_parent(os.getppid())
ctl = Path(sys.argv[1])
if len(sys.argv) > 2:
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(86400)', sys.argv[2]], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     preexec_fn=child_dies_with_me())
while True:
    for request in sorted(ctl.glob('spawn-*.json')):
        job = json.loads(request.read_text())
        child = subprocess.Popen(['bash', '-c', job['command']], cwd=job['cwd'], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 preexec_fn=child_dies_with_me())
        request.with_suffix('.pid').write_text(str(child.pid))
        request.unlink()
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.05)
"""

#: The guardian: waits for the test process (argv: pid, start time, run id, ledger) to end, then stops the run's
#: marked processes and exits. Its own lifetime is the test's, plus one bounded stop.
GUARDIAN = """
import os, select, sys, time
sys.path.insert(0, os.environ["SWITCHYARD_TEST_FIXTURE_MODULE_DIR"])
import fixture_processes as fp
pid, start, run, ledger = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    fd = os.pidfd_open(pid)
except OSError:
    fd = None
if fd is not None and fp.start_time(pid) == start:
    select.select([fd], [], [])
fp.stop_marked(run, ledger, spare={os.getpid()})
"""


def start_time(pid: int) -> int | None:
    """The process's start time in clock ticks since boot, or None when it is gone (or a zombie)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split()
    return None if fields[0] in ("Z", "X") else int(fields[19])


def marked(run: str) -> dict[int, int]:
    """Every live process carrying this run's mark: {pid: start time}. Another account's are not readable, and skipped."""
    needle = f"{RUN_ENV}={run}".encode()
    found: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            start = start_time(int(entry.name))
            if start is not None:
                found[int(entry.name)] = start
    return found


def record(ledger: str | os.PathLike, identities: dict[int, int]) -> None:
    """Add identities to the ledger. A ledger that cannot be written -- its directory removed with a test that ended --
    never stops a cleanup: the mark still finds what carries it."""
    try:
        with open(ledger, "a", encoding="utf-8") as handle:
            for pid, start in identities.items():
                handle.write(f"{pid} {start}\n")
    except OSError:
        pass


def recorded(ledger: str | os.PathLike) -> dict[int, int]:
    try:
        lines = Path(ledger).read_text(encoding="utf-8").split()
    except OSError:
        return {}
    return {int(pid): int(start) for pid, start in zip(lines[::2], lines[1::2])}


def signal_identity(pid: int, start: int, sig: int) -> bool:
    """Send `sig` to exactly that process: through a pidfd, opened and then checked against the start time, so a pid
    reused since cannot be hit. False when it is already gone."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        if start_time(pid) != start:
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def stop_marked(run: str, ledger: str | os.PathLike, *, spare: set[int] = frozenset(),
                grace: float = STOP_GRACE_SECONDS) -> dict[int, int]:
    """TERM, then KILL after `grace`, every process carrying the mark or in the ledger; returns what is still alive."""
    def living() -> dict[int, int]:
        found = {pid: start for pid, start in {**recorded(ledger), **marked(run)}.items()
                 if pid not in spare and start_time(pid) == start}
        record(ledger, {pid: start for pid, start in found.items() if recorded(ledger).get(pid) != start})
        return found

    for sig in (signal.SIGTERM, signal.SIGKILL):
        targets = living()
        for pid, start in targets.items():
            signal_identity(pid, start, sig)
        deadline = time.monotonic() + grace
        while living() and time.monotonic() < deadline:
            time.sleep(0.05)
    return living()


class Run:
    """One owned run: its mark, its ledger, its guardian."""

    def __init__(self, directory: Path) -> None:
        self.id = uuid.uuid4().hex
        self.ledger = directory / "fixture-processes.ledger"
        self.guardian: subprocess.Popen | None = None

    def preexec(self):
        """For the test's own Popen calls: the child dies with this process."""
        parent = os.getpid()

        def die_with_test() -> None:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0)
            if os.getppid() != parent:
                os._exit(1)
        return die_with_test

    def popen(self, argv: list[str], **kwargs: Any) -> subprocess.Popen:
        """A fixture process of the test's own, recorded by identity the moment it exists."""
        process = subprocess.Popen(argv, preexec_fn=self.preexec(), **kwargs)
        self.track(process.pid)
        return process

    def track(self, *pids: int) -> None:
        record(self.ledger, {pid: start for pid in pids if (start := start_time(pid)) is not None})

    def sweep(self) -> None:
        """Record every marked process alive now, so the sentinel knows each one by identity."""
        found = marked(self.id)
        found.pop(os.getpid(), None)
        if self.guardian is not None:
            found.pop(self.guardian.pid, None)
        record(self.ledger, {pid: start for pid, start in found.items() if recorded(self.ledger).get(pid) != start})

    def sentinel(self) -> list[str]:
        """Each recorded identity, or marked process, still alive: "pid start command". Empty when all are gone."""
        spare = {os.getpid()} | ({self.guardian.pid} if self.guardian else set())
        alive = {pid: start for pid, start in {**recorded(self.ledger), **marked(self.id)}.items()
                 if pid not in spare and start_time(pid) == start}
        named = []
        for pid, start in sorted(alive.items()):
            try:
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:120]
            except OSError:
                command = "?"
            named.append(f"{pid} {start} {command}")
        return named


@contextlib.contextmanager
def owned(directory: str | os.PathLike) -> Iterator[Run]:
    """Own every process this test starts from here on; stop them all, by identity, on every way out.

    Each run has its own mark: a run inside another's process (a suite's "before" child) owns its own descendants,
    and its cleanup never reaches its parent's. The child itself still carries the parent's mark, as it was started.
    """
    global current
    run, outer = Run(Path(directory)), current
    current = run
    previous_env = {key: os.environ.get(key) for key in (RUN_ENV, LEDGER_ENV, "SWITCHYARD_TEST_FIXTURE_PARENT",
                                                         "SWITCHYARD_TEST_FIXTURE_MODULE_DIR")}
    os.environ.update({RUN_ENV: run.id, LEDGER_ENV: str(run.ledger), "SWITCHYARD_TEST_FIXTURE_PARENT": str(os.getpid()),
                       "SWITCHYARD_TEST_FIXTURE_MODULE_DIR": str(Path(__file__).resolve().parent)})
    handlers = {}

    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, leave)
    except ValueError:  # not the main thread: the parent-death signal and the guardian still hold
        pass
    run.guardian = subprocess.Popen([sys.executable, "-c", GUARDIAN, str(os.getpid()), str(start_time(os.getpid())),
                                     run.id, str(run.ledger)], start_new_session=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield run
    finally:
        # Cleanup is not re-entrant (Audit on 4044ec42): a second TERM, INT or HUP while it waits out the TERM grace
        # is recorded, not raised, so it can never abort the sweep. First, before anything else here.
        deferred: list[int] = []
        for sig in handlers:
            signal.signal(sig, lambda signum, _frame: deferred.append(signum))
        leaving = sys.exc_info()[0] is not None
        try:
            run.sweep()
            stop_marked(run.id, run.ledger, spare={os.getpid(), run.guardian.pid})
            left = run.sentinel()
        except BaseException:
            left = ["the cleanup failed"]
            raise
        finally:
            current = outer
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            # The guardian is retired only once nothing of the run is alive. Otherwise it stays, and finishes the
            # job when this process ends -- it is bounded by this process's life, and by one stop after it.
            if not left:
                signal_identity(run.guardian.pid, start_time(run.guardian.pid) or -1, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    run.guardian.wait(timeout=5)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        if deferred and not leaving:
            raise SystemExit(128 + deferred[0])  # a signal that came during cleanup, honoured once it is done
