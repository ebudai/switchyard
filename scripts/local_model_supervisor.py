"""The local model service's main process: llama-server, stopped once it has been idle (SYRD-691).

`switchyard local-model install` copies this file, root-owned, beside the engine, and the unit runs it as
`python3 -I -B <this> ... -- <llama-server argv>`. It uses only the standard library, so it imports nothing the
service identity could have written.

While llama-server runs it holds about 8.3 GiB of the GPU and about 3 GiB of RAM, whether or not anyone is using
it. A qualification run, a test or a role that went away cannot be trusted to stop it, so the service stops
itself: once no request has run, waited or produced a token for `--idle-seconds`, it ends llama-server and exits
with IDLE_EXIT, which the unit counts as a clean stop and does not restart.

Idle is read from what the server itself counts on `/metrics`, not from anyone's say-so: a request running or
waiting (`requests_processing`, `requests_deferred`) is activity, and so is any change in its work counters
(every `llamacpp:*_total`, such as `prompt_tokens_total` and `n_decode_total`) between two readings. A consumer
that is still working keeps it running; one that stopped, crashed or was abandoned stops making requests, and
the service stops itself one idle window later. Reads that do no inference -- `/health`, `/v1/models`, `/props`
and `/metrics` itself -- move no counter, so status checks and the Hermes start check never keep it alive.

It is never stopped for being idle while it reports a request running or waiting. When the window has passed
it reads the metrics once more immediately before stopping, and stays up if anything moved. An endpoint that does
not answer is not activity, so a server that never comes up, hangs or stops answering is stopped one window
after anything was last seen moving.

`systemctl stop` (SIGTERM to this process alone, `KillMode=mixed`) is passed on to llama-server once, and this
process exits 0. If llama-server exits on its own, this process exits with its status (128 + the signal for a
signal), so the unit's restart limit and `switchyard local-model status` see exactly what it did.

While it runs it keeps a small JSON state file (`--state`, in the unit's RuntimeDirectory) saying how long it
has been idle and when it will stop itself; `status` shows it. systemd removes it when the unit stops.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence

#: The exit status of a stop for being idle; the unit lists it in SuccessExitStatus, so it is not a failure.
IDLE_EXIT = 75
STATE_SCHEMA = "switchyard.local-model.supervisor.v1"
#: Gauges that are non-zero while a request is running or waiting for a slot.
BUSY_GAUGES = ("llamacpp:requests_processing", "llamacpp:requests_deferred")
#: How long llama-server has to end after SIGTERM before it is killed; inside the unit's TimeoutStopSec=30.
STOP_GRACE_SECONDS = 20.0


def reading(text: str) -> tuple[int, tuple[tuple[str, float], ...]]:
    """(requests running or waiting, the work counters) from llama-server's Prometheus `/metrics` text."""
    busy, work = 0, []
    for line in text.splitlines():
        if not line.startswith("llamacpp:"):
            continue
        name, _, value = line.partition(" ")
        try:
            number = float(value.split()[0])
        except (IndexError, ValueError):
            continue
        if name in BUSY_GAUGES:
            busy += int(number)
        elif name.endswith("_total"):
            work.append((name, number))
    return busy, tuple(sorted(work))


def read_metrics(url: str, *, timeout: float = 10.0) -> tuple[int, tuple] | None:
    """The server's reading now, or None when it does not answer one (still loading, hung, or gone)."""
    try:
        with urllib.request.urlopen(f"{url}/metrics", timeout=timeout) as response:  # noqa: S310 - loopback only
            if response.status != 200:
                return None
            return reading(response.read(1 << 20).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


class IdleClock:
    """How long the server has done nothing, from successive readings."""

    def __init__(self, idle_seconds: float, now: float) -> None:
        self.idle_seconds = idle_seconds
        self.since = now
        self.busy = 0
        self.work: tuple | None = None

    def observe(self, now: float, seen: tuple[int, tuple] | None) -> bool:
        """Take one reading (None: unanswered); True once the server has been idle for the whole window."""
        if seen is None:
            # Unanswered is not activity: the window runs on from the last time anything was seen moving.
            self.busy = 0
            return now - self.since >= self.idle_seconds
        busy, work = seen
        # The first answer is the baseline; after it, any counter that moved is a request that ran.
        if busy or (self.work is not None and work != self.work):
            self.since = now
        self.busy, self.work = busy, work
        return now - self.since >= self.idle_seconds

    def idle_for(self, now: float) -> float:
        return 0.0 if self.busy else max(0.0, now - self.since)


def write_state(path: str, state: dict[str, Any]) -> None:
    """Replace the state file atomically; a state that cannot be written never stops the service."""
    if not path:
        return
    staged = f"{path}.{os.getpid()}.new"
    try:
        with open(staged, "w", encoding="utf-8") as handle:
            json.dump({"schema": STATE_SCHEMA, **state}, handle, sort_keys=True)
        os.chmod(staged, 0o644)
        os.replace(staged, path)
    except OSError:
        pass


def exit_status(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 - returncode


def supervise(argv: Sequence[str], *, url: str, idle_seconds: float, poll_seconds: float, state: str = "",
              popen: Callable[..., Any] = subprocess.Popen,
              metrics: Callable[[str], tuple[int, tuple] | None] = read_metrics,
              clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
              log: Callable[[str], None] = lambda line: print(line, flush=True),
              install_signals: bool = True) -> int:
    """Run `argv` (llama-server) until it exits, systemd stops it, or it has been idle for `idle_seconds`."""
    child = popen(list(argv))
    stopping: list[int] = []

    def forward(signum: int, _frame: Any) -> None:
        # systemctl stop: pass it on once, then wait for the server to finish.
        if not stopping:
            stopping.append(signum)
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    if install_signals:
        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
    clock_now = clock()
    idle = IdleClock(idle_seconds, clock_now)
    started = wall()

    def record(**extra: Any) -> None:
        now = clock()
        remaining = max(0.0, idle_seconds - idle.idle_for(now))
        write_state(state, {"server_pid": child.pid, "started_at": round(started), "updated_at": round(wall()),
                            "idle_seconds": idle_seconds, "idle_for": round(idle.idle_for(now)), "busy": idle.busy,
                            "stops_at": None if idle.busy else round(wall() + remaining), **extra})

    log(f"switchyard-local-model: serving; stops itself after {idle_seconds / 60:g} idle minutes "
        f"(no request running or waiting, no tokens processed)")
    record()
    while True:
        try:
            returncode = child.wait(timeout=poll_seconds)
        except subprocess.TimeoutExpired:
            returncode = None
        if returncode is not None:
            break
        if stopping:
            continue
        if not idle.observe(clock(), metrics(url)):
            record()
            continue
        # The window has passed. Look once more, just before stopping: a request that came in since is never cut.
        if not idle.observe(clock(), metrics(url)) or stopping:
            record()
            continue
        log(f"switchyard-local-model: idle for {idle.idle_for(clock()) / 60:.0f} minutes with no request running "
            "or waiting; stopping llama-server to release its GPU and host memory")
        record(stopped="idle")
        child.terminate()
        try:
            child.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return IDLE_EXIT
    if stopping:
        return 0
    log(f"switchyard-local-model: llama-server exited with status {returncode}")
    return exit_status(returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="local-model-supervisor", description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="the server's own loopback address, e.g. http://127.0.0.1:17431")
    parser.add_argument("--idle-seconds", type=float, required=True)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--state", default="")
    parser.add_argument("server", nargs=argparse.REMAINDER, help="-- llama-server and its arguments")
    args = parser.parse_args(argv)
    server = args.server[1:] if args.server[:1] == ["--"] else args.server
    if not server:
        parser.error("no server command after --")
    return supervise(server, url=args.url, idle_seconds=args.idle_seconds, poll_seconds=args.poll_seconds,
                     state=args.state)


if __name__ == "__main__":
    sys.exit(main())
