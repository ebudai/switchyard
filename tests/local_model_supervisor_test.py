#!/usr/bin/env python3
"""SYRD-691: the local model service stops itself once idle, and never while a request runs.

The supervisor is checked three ways: its reading of llama-server's `/metrics` and its idle clock on their own;
its loop against a stand-in child and a stand-in clock, for each way a run can end; and as the unit runs it --
`python3 -I -B scripts/local_model_supervisor.py ... -- <server>` -- around a real loopback server that answers
`/metrics` from a file the test writes, so a request can be made to run, finish or never come.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(detail)


def metrics(*, running: int = 0, waiting: int = 0, prompt: int = 0, decoded: int = 0) -> str:
    """`/metrics` as llama-server writes it."""
    return (f"# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.\n"
            f"# TYPE llamacpp:prompt_tokens_total counter\nllamacpp:prompt_tokens_total {prompt}\n"
            f"llamacpp:prompt_seconds_total {prompt / 200}\nllamacpp:tokens_predicted_total {decoded}\n"
            f"llamacpp:tokens_predicted_seconds_total {decoded / 4}\nllamacpp:n_decode_total {decoded}\n"
            f"llamacpp:n_tokens_max 4096\n"
            f"# HELP llamacpp:requests_processing Number of requests processing.\n"
            f"# TYPE llamacpp:requests_processing gauge\nllamacpp:requests_processing {running}\n"
            f"llamacpp:requests_deferred {waiting}\n")


# ------------------------------------------------------------------ the reading and the clock


def test_the_reading_is_what_the_server_counts() -> None:
    from scripts import local_model_supervisor as s

    busy, work = s.reading(metrics(running=1, waiting=2, prompt=30, decoded=7))
    check(busy == 3, f"running and waiting requests are both busy: {busy}")
    check(dict(work) == {"llamacpp:prompt_tokens_total": 30.0, "llamacpp:prompt_seconds_total": 0.15,
                         "llamacpp:tokens_predicted_total": 7.0, "llamacpp:tokens_predicted_seconds_total": 1.75,
                         "llamacpp:n_decode_total": 7.0},
          f"every work counter, and no gauge such as n_tokens_max: {work}")
    check(s.reading("# nothing\nother_metric 5\nllamacpp:requests_processing x\n") == (0, ()),
          "anything else, or a value that is not a number, is ignored")


def test_the_idle_clock_runs_only_while_nothing_happens() -> None:
    from scripts import local_model_supervisor as s

    clock = s.IdleClock(60, now=0)
    idle = s.reading(metrics(prompt=10, decoded=5))
    check(not clock.observe(10, idle) and clock.since == 0, "the first answer is the baseline, not activity")
    check(not clock.observe(59, idle) and clock.observe(60, idle), "idle for the whole window, then due")
    clock = s.IdleClock(60, now=0)
    clock.observe(10, idle)
    check(not clock.observe(50, s.reading(metrics(prompt=11, decoded=5)))
          and clock.since == 50 and not clock.observe(100, s.reading(metrics(prompt=11, decoded=5)))
          and clock.observe(110, s.reading(metrics(prompt=11, decoded=5))),
          "a request that ran between two readings, with nothing running at either, restarts the window")
    clock = s.IdleClock(60, now=0)
    for now in range(0, 600, 30):
        due = clock.observe(now, s.reading(metrics(running=1, prompt=10, decoded=5)))
        check(not due, f"a request running at {now}s -- one long thinking turn -- is never idle")
    check(clock.idle_for(600) == 0 and clock.since == 570, "and the window starts again from the last busy reading")
    check(not clock.observe(620, None) and clock.busy == 0 and clock.observe(630, None),
          "unanswered is not activity: the window runs on from the last time anything was seen moving")
    check(not s.IdleClock(60, now=0).observe(30, s.reading(metrics(waiting=1))), "a waiting request is busy too")


# ------------------------------------------------------------------ the loop, with a stand-in child and clock


class Child:
    """A stand-in llama-server: each wait() lets the clock run on by the poll, until it is ended or exits."""

    def __init__(self, clock: list[float], *, exits_at: float | None = None, code: int = 0,
                 ignores_term: bool = False) -> None:
        self.clock, self.exits_at, self.code, self.ignores_term = clock, exits_at, code, ignores_term
        self.pid, self.returncode, self.signals = 4242, None, []

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if self.exits_at is not None and self.clock[0] + (timeout or 0) >= self.exits_at:
            self.clock[0] = self.exits_at
            self.returncode = self.code
            return self.code
        self.clock[0] += timeout or 0
        raise subprocess.TimeoutExpired("llama-server", timeout)

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)
        if not self.ignores_term:
            self.returncode = 0

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.signals.append(signal.SIGKILL)
        self.returncode = -9


def run(script, *, idle: float = 300, poll: float = 30, child_kwargs=None, until: float = 7200, state: str = ""):
    """Run supervise() against `script(now) -> reading text or None`; what it returned, the child, what it read."""
    from scripts import local_model_supervisor as s

    clock = [0.0]
    child = Child(clock, **(child_kwargs or {}))
    reads, logged = [], []

    def read(_url):
        if clock[0] > until:
            raise AssertionError(f"still running at {clock[0]}s")
        text = script(clock[0])
        reads.append((clock[0], text))
        return None if text is None else s.reading(text)

    result = s.supervise(["llama-server"], url="http://127.0.0.1:1", idle_seconds=idle, poll_seconds=poll,
                         state=state, popen=lambda _argv: child, metrics=read, clock=lambda: clock[0],
                         wall=lambda: 1_800_000_000 + clock[0], log=logged.append, install_signals=False)
    return result, child, reads, logged, clock[0]


def test_an_abandoned_service_stops_itself_and_releases_the_gpu() -> None:
    from scripts import local_model_supervisor as s

    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        state = str(Path(raw) / "state.json")
        # A consumer asked twice and went away at 600s; nothing has come since.
        result, child, _reads, logged, ended = run(
            lambda now: metrics(prompt=40 if now >= 300 else 10, decoded=9 if now >= 600 else 3), state=state)
        check(result == s.IDLE_EXIT == 75 and child.signals == [signal.SIGTERM],
              f"stopped once for being idle, with the exit the unit counts as clean: {result} {child.signals}")
        check(900 <= ended <= 960, f"one window after the last request, give or take a poll: {ended}")
        check(any("idle for 5 minutes" in line and "release its GPU" in line for line in logged), f"and says so: {logged}")
        recorded = json.loads(Path(state).read_text())
        check(recorded["schema"] == s.STATE_SCHEMA and recorded["stopped"] == "idle" and recorded["server_pid"] == 4242
              and recorded["idle_seconds"] == 300 and recorded["busy"] == 0, f"its state says why: {recorded}")


def test_a_running_request_is_never_cut() -> None:
    from scripts import local_model_supervisor as s

    # One thinking turn from 100s to 2000s: running throughout, no counter moves until it ends.
    result, child, reads, _logged, ended = run(
        lambda now: metrics(running=1 if 100 <= now < 2000 else 0, prompt=10, decoded=500 if now >= 2000 else 0))
    check(result == s.IDLE_EXIT and 2000 + 300 <= ended <= 2000 + 360,
          f"not stopped while it ran; stopped one window after it finished: {ended}")
    check(not any(now < 2300 for now, _ in reads[-2:]), f"its last two readings came after the window: {reads[-2:]}")
    # The same, with a request only waiting for the slot.
    result, _child, _reads, _logged, ended = run(lambda now: metrics(waiting=1 if now < 1000 else 0))
    check(result == s.IDLE_EXIT and ended == 990 + 300, f"a waiting request holds it too, to its last reading: {ended}")


def test_a_request_that_arrives_at_the_last_moment_is_not_cut() -> None:
    from scripts import local_model_supervisor as s

    seen = []

    def busy_at_recheck(now):
        seen.append(now)
        if len(seen) > 1 and seen[-2] == now and not getattr(busy_at_recheck, "done", False):
            busy_at_recheck.done = True
            return metrics(running=1)
        return metrics()

    result, child, reads, _logged, ended = run(busy_at_recheck)
    rechecks = [now for (now, _), (then, _) in zip(reads, reads[1:]) if now == then]
    check(rechecks and result == s.IDLE_EXIT and ended >= rechecks[0] + 300,
          f"the reading just before the stop saw a request, so it waited a whole window more: {rechecks} {ended}")
    check(child.signals == [signal.SIGTERM], f"and stopped once, later: {child.signals}")


def test_an_endpoint_that_never_answers_is_stopped_once_bounded() -> None:
    from scripts import local_model_supervisor as s

    result, child, _reads, _logged, ended = run(lambda _now: None)
    check(result == s.IDLE_EXIT and 300 <= ended <= 330 and child.signals == [signal.SIGTERM],
          f"a server that never comes up is stopped one window after start, once: {ended} {child.signals}")
    result, _child, _reads, _logged, ended = run(lambda now: metrics(running=1) if now < 600 else None)
    check(result == s.IDLE_EXIT and ended == 570 + 300,
          f"one that hangs mid-request and stops answering is stopped one window after it was last seen busy: {ended}")


def test_a_server_that_exits_is_reported_as_it_exited() -> None:
    from scripts import local_model_supervisor as s

    for code, wanted in ((3, 3), (-9, 137), (0, 0)):
        result, child, _reads, logged, _ended = run(lambda _now: metrics(), child_kwargs=dict(exits_at=100, code=code))
        check(result == wanted and child.signals == [] and any(f"status {code}" in line for line in logged),
              f"llama-server's exit {code} is the unit's {wanted}, so its restart limit sees it: {result} {logged}")
    original = s.STOP_GRACE_SECONDS
    try:
        s.STOP_GRACE_SECONDS = 1
        result, child, _reads, _logged, _ended = run(lambda _now: metrics(), child_kwargs=dict(ignores_term=True))
    finally:
        s.STOP_GRACE_SECONDS = original
    check(result == s.IDLE_EXIT and child.signals == [signal.SIGTERM, signal.SIGKILL],
          f"a server that ignores the stop is killed after the grace: {child.signals}")


# ------------------------------------------------------------------ as the unit runs it, around a real server


FAKE_SERVER = r'''
import http.server, os, signal, sys, time
port, metrics, log = int(sys.argv[1]), sys.argv[2], sys.argv[3]

def ended(signum, _frame):
    with open(log, "a") as out:
        out.write(f"signal {signum}\n")
    time.sleep(0.5)  # long enough for a second signal to be seen
    os._exit(0)

signal.signal(signal.SIGTERM, ended)
signal.signal(signal.SIGINT, ended)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = open(metrics, "rb").read()
        elif self.path in ("/v1/models", "/props", "/health"):
            body = b'{"data": [{"id": "Qwen/Qwen3.8-27B", "meta": {"n_ctx": 65536}}], "status": "ok"}'
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

with open(log, "a") as out:
    out.write(f"pid {os.getpid()}\n")
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


class Service:
    """`python3 -I -B local_model_supervisor.py ... -- python3 fake-server.py`, as the unit's ExecStart."""

    def __init__(self, tmp: Path, *, idle: float, server: list[str] | None = None, text: str | None = None) -> None:
        self.tmp = tmp
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.metrics, self.log, self.state = tmp / "metrics", tmp / "server.log", tmp / "state.json"
        self.set(text if text is not None else metrics())
        (tmp / "fake-server.py").write_text(FAKE_SERVER)
        command = server or [sys.executable, "-I", str(tmp / "fake-server.py"), str(self.port), str(self.metrics),
                             str(self.log)]
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(ROOT / "scripts/local_model_supervisor.py"),
             "--url", f"http://127.0.0.1:{self.port}", "--idle-seconds", str(idle), "--poll-seconds", "0.2",
             "--state", str(self.state), "--", *command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def set(self, text: str) -> None:
        staged = self.metrics.with_suffix(".new")
        staged.write_text(text)
        os.replace(staged, self.metrics)

    def server_pid(self) -> int | None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.log.exists():
                for line in self.log.read_text().splitlines():
                    if line.startswith("pid "):
                        return int(line.split()[1])
            time.sleep(0.05)
        return None

    def signals(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith("signal")] if self.log.exists() else []

    def wait(self, timeout: float) -> int | None:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def output(self) -> str:
        return self.process.stdout.read() if self.process.poll() is not None else ""

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()


def alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it has released everything.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except (OSError, IndexError):
        return False


def test_as_the_unit_runs_it_idle_stops_and_the_server_is_gone() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        service = Service(Path(raw), idle=1.5)
        try:
            pid = service.server_pid()
            check(alive(pid), "the server runs under the supervisor")
            result = service.wait(15)
            output = service.output()
            check(result == 75, f"it stopped itself for being idle, exit 75: {result} {output}")
            check(not alive(pid) and service.signals() == [f"signal {signal.SIGTERM}"],
                  f"the server got one SIGTERM and is gone, GPU and all: {service.signals()}")
            state = json.loads(service.state.read_text())
            check(state["stopped"] == "idle" and state["server_pid"] == pid, f"its state says so: {state}")
            check("stops itself after 0.025 idle minutes" in output and "stopping llama-server" in output,
                  f"and its journal says what it does and why: {output}")
        finally:
            service.close()


def test_as_the_unit_runs_it_a_long_request_holds_it_and_its_end_releases_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        service = Service(Path(raw), idle=1.5, text=metrics(running=1, prompt=12))
        try:
            pid = service.server_pid()
            check(service.wait(4.5) is None and alive(pid), "a running request outlasts three idle windows")
            state = json.loads(service.state.read_text())
            check(state["busy"] == 1 and state["stops_at"] is None, f"and its state says it is busy: {state}")
            ended = time.monotonic()
            service.set(metrics(running=0, prompt=12, decoded=900))
            result = service.wait(15)
            took = time.monotonic() - ended
            check(result == 75 and 1.4 <= took <= 6 and not alive(pid),
                  f"once it finishes, the service stops one window later: {result} after {took:.1f}s")
        finally:
            service.close()


def test_as_the_unit_runs_it_status_reads_never_keep_it_alive() -> None:
    from scripts import hermes_local_profile as hermes

    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        service = Service(Path(raw), idle=2.0)
        try:
            pid = service.server_pid()
            profile = hermes.LocalProfile(model="Qwen/Qwen3.8-27B", base_url=f"http://127.0.0.1:{service.port}/v1",
                                          server="llama.cpp b11803", revision="r", artifact="a", quantization="Q4_K_M",
                                          context=65536)
            started, served = time.monotonic(), []
            while service.process.poll() is None and time.monotonic() - started < 15:
                served.append(hermes.served(profile, timeout=1)[0] == "")
                time.sleep(0.1)
            took = time.monotonic() - started
            check(service.process.returncode == 75 and 1.8 <= took <= 6 and len(served) > 10 and served[0],
                  f"a Hermes start check every 0.1s is no request: it stopped on time: {service.process.returncode} "
                  f"after {took:.1f}s and {len(served)} checks")
            check(not alive(pid), "and the server is gone")
        finally:
            service.close()


def test_as_the_unit_runs_it_systemctl_stop_is_passed_on_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        service = Service(Path(raw), idle=600)
        try:
            pid = service.server_pid()
            check(alive(pid), "running")
            # KillMode=mixed: systemctl stop sends SIGTERM to the main process alone. A second one -- an operator's
            # kill, or Ctrl-C after it -- lands while the server is ending; llama-server takes a second signal as
            # "terminate now", so it is not passed on.
            os.kill(service.process.pid, signal.SIGTERM)
            time.sleep(0.1)
            os.kill(service.process.pid, signal.SIGTERM)
            result = service.wait(10)
            check(result == 0 and not alive(pid) and service.signals() == [f"signal {signal.SIGTERM}"],
                  f"a stop is a clean exit, and the server got exactly one SIGTERM: {result} {service.signals()}")
        finally:
            service.close()


def test_as_the_unit_runs_it_a_crash_is_the_unit_s_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd691s.") as raw:
        for script, wanted in (("import sys; sys.exit(3)", 3), ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 137)):
            service = Service(Path(raw), idle=600, server=[sys.executable, "-I", "-c", script])
            try:
                result = service.wait(10)
                check(result == wanted, f"{script!r}: the unit sees {wanted}, so Restart=on-failure and its limit "
                                        f"apply: {result} {service.output()}")
            finally:
                service.close()


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"local_model_supervisor_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    raise SystemExit(main())
