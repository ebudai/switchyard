#!/usr/bin/env python3
"""A worker-pool restart succeeds only over a live worker it has observed.

Live MEFP, 2026-09-29: luna-1 was idle and running, and

    switchyard worker-pool mefp restart luna-1

exited 0 after printing

    luna-1 stopped: session mefp-luna-1 is gone; its identity is not
    luna-1 already running: its session is live at mefp-luna-1:0.0

while `switchyard worker-pool mefp status` straight afterwards said
`luna-1 ... ready, stopped`. The start half decided "already running" from the
readiness read before the stop, so nothing was started, and both halves counted
as successes (SYRD-477).

These cases run the real `switchyard worker-pool` command over a sandbox project
whose owner home carries the real board skill. tmux is stood in for: a session
is a real process, a copy of `sleep` named after what the pane was asked to run,
so the product's own live-worker check reads a real process tree. No provider
CLI runs, no board is written, and every process started here is reaped.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher, worker_pool, worker_pool_command  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

import worker_pool_lifecycle_test as lifecycle  # noqa: E402

PROJECT = lifecycle.PROJECT
POOL = lifecycle.POOL
WORKERS = ["impl-1", "impl-2"]
SESSION = f"{PROJECT}-impl-1"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """A sandbox project, and a tmux whose sessions are real processes.

    `pane` decides what the next pane start does: "live" leaves the worker's
    runtime running, "exits" leaves a process that is gone at once (the pane
    command still exits 0), "fails" exits 1 and starts nothing, and "other"
    leaves something that is not the worker's runtime.
    """

    def __init__(self, tmp: Path, *, running: tuple[str, ...] = ("impl-1",), pane: str = "live") -> None:
        self.tmp = tmp
        self.config, self.config_path = lifecycle.config_with_pool(tmp, pool=POOL, workers=WORKERS)
        self.document, _ = worker_pool.expand_pool(
            lifecycle.base_document(), lifecycle.pool_of(POOL), project=PROJECT, worktree_base=tmp / "worktrees"
        )
        self.home = tmp / "home"
        board_skill.install_board_skill(
            home=self.home, source=board_skill.default_source(ROOT), source_commit="syrd477"
        )
        for member in WORKERS:
            (tmp / "worktrees" / member).mkdir(parents=True, exist_ok=True)
        self.bin = tmp / "standins"
        self.bin.mkdir()
        self.pane = pane
        self.kill_fails = False
        self.probe_raises = False
        self.board = None
        self.processes: dict[str, subprocess.Popen] = {}
        self.started_processes: list[subprocess.Popen] = []
        self.pane_starts: list[list[str]] = []
        self.kills: list[list[str]] = []
        for member in running:
            self._launch(f"{PROJECT}-{member}", POOL["runtime"])

    def _launch(self, session: str, name: str, *, seconds: str = "300") -> None:
        program = self.bin / name
        if not program.exists():
            shutil.copy2(shutil.which("sleep"), program)
        self.processes[session] = subprocess.Popen(
            [str(program), seconds], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.started_processes.append(self.processes[session])

    def alive(self, session: str) -> bool:
        proc = self.processes.get(session)
        if proc is None:
            return False
        if proc.poll() is None:
            return True
        return False

    def pid(self, session: str) -> int:
        return self.processes[session].pid if self.alive(session) else 0

    def __call__(self, args, **_kwargs):
        command = [str(part) for part in args]
        if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/hermes\n")
        if "config" in command and "check" in command:
            return subprocess.CompletedProcess(command, 0, stdout="\N{CHECK MARK} OPENROUTER_API_KEY\n")
        if "pane" in command and "attach-or-start" in command:
            self.pane_starts.append(command)
            session = f"{PROJECT}-{command[command.index('pane') + 2]}"
            if self.pane == "fails":
                return subprocess.CompletedProcess(command, 1)
            if self.pane == "exits":
                self._launch(session, POOL["runtime"], seconds="0")
                self.processes[session].wait(timeout=10)
            elif self.pane == "other":
                self._launch(session, "bash-login")
            else:
                self._launch(session, POOL["runtime"])
            return subprocess.CompletedProcess(command, 0)
        if command[:1] != ["tmux"]:
            raise AssertionError(f"an unexpected command reached the host: {command}")
        target = command[command.index("-t") + 1].lstrip("=") if "-t" in command else ""
        session = target.split(":", 1)[0]
        verb = command[1]
        if verb == "has-session":
            if self.probe_raises and self.pane_starts:
                raise OSError(13, "Permission denied", "tmux")
            return subprocess.CompletedProcess(command, 0 if self.alive(session) else 1)
        if verb == "kill-session":
            self.kills.append(command)
            if self.kill_fails:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no server\n")
            proc = self.processes.pop(session, None)
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if verb == "display-message":
            if command[-1] == "#{pane_pid}":
                pid = self.pid(session)
                return subprocess.CompletedProcess(command, 0 if pid else 1, stdout=f"{pid}\n")
            if command[-1] == "#{pane_current_command}":
                name = Path(self.processes[session].args[0]).name if self.alive(session) else ""
                return subprocess.CompletedProcess(command, 0 if name else 1, stdout=f"{name}\n")
        raise AssertionError(f"an unexpected tmux call reached the host: {command}")

    def run(self, action: str, member: str = "impl-1") -> tuple[int, list[str]]:
        said: list[str] = []
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda _owner: self.home
        try:
            code = team_launcher.switchyard_worker_pool_command(
                PROJECT, action=action, member=member,
                config_dir=self.tmp, registry_dir=self.tmp / "registry",
                board_reader=lambda _config: {"document": self.document},
                board_snapshot_reader=lambda _config: self.board,
                runner=self, print_func=said.append,
            )
        finally:
            team_launcher._owner_home_for_auth = original
        return code, said

    def close(self) -> None:
        for proc in self.started_processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def _sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd477.") as tmp:
            previous = os.environ.get("HOME")
            os.environ["HOME"] = tmp  # nothing here may reach the real home
            try:
                case(Path(tmp))
            finally:
                if previous is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


def _status_line(host: Host, member: str = "impl-1") -> str:
    code, said = host.run("status", "")
    check(code == 0, said)
    return next((line for line in said if line.strip().startswith(f"{member} (")), "\n".join(said))


def _recovery(said: list[str]) -> list[str]:
    """The one command a failure names, parsed by the command's real parser."""
    text = "\n".join(said)
    commands = [part.split("`", 1)[0] for part in text.split("`switchyard worker-pool ")[1:]]
    check(commands, f"no recovery command in: {text}")
    argv = shlex.split(commands[0])
    parsed = worker_pool_command._build_switchyard_worker_pool_parser().parse_args(argv)
    return [parsed.project, parsed.action, parsed.member]


@_sandbox
def test_the_live_sequence_restarts_the_worker_instead_of_calling_it_already_running(tmp: Path) -> None:
    """The MEFP run: an idle, running worker restarted."""
    host = Host(tmp)
    try:
        before = host.pid(SESSION)
        check(before and "ready, running" in _status_line(host), _status_line(host))
        code, said = host.run("restart")
        check(code == 0, (code, said))
        check(not any("already running" in line for line in said), f"the stale answer came back: {said}")
        check(any("impl-1 stopped" in line for line in said) and any("impl-1 started" in line for line in said),
              said)
        check(len(host.kills) == 1 and len(host.pane_starts) == 1, (host.kills, host.pane_starts))
        # A new worker, observed live -- and status agrees with what was said.
        check(host.alive(SESSION) and host.pid(SESSION) != before, "the session was not replaced")
        check("ready, running" in _status_line(host), _status_line(host))
        # Identity is not the session: the same session name and target.
        check(f"session {SESSION}" in "\n".join(said), said)
        check(host.pane_starts[0][host.pane_starts[0].index("pane") + 2] == "impl-1", host.pane_starts)
    finally:
        host.close()


@_sandbox
def test_a_restart_that_leaves_no_live_worker_fails_and_names_the_way_back(tmp: Path) -> None:
    host = Host(tmp, pane="exits")
    try:
        code, said = host.run("restart")
        check(code == 1, f"a restart that left the worker stopped exited {code}: {said}")
        text = "\n".join(said)
        check("impl-1 failed to start" in text and "stopped" in text, text)
        check("already running" not in text and "impl-1 started" not in text, text)
        # What the command said is what status says.
        check("ready, stopped" in _status_line(host), _status_line(host))
        # The printed way back is a command that parses, and it works.
        argv = _recovery(said)
        check(argv == [PROJECT, "start", "impl-1"], argv)
        host.pane = "live"
        code, said = host.run(argv[1], argv[2])
        check(code == 0 and any("impl-1 started" in line for line in said), (code, said))
        check("ready, running" in _status_line(host), _status_line(host))
    finally:
        host.close()


@_sandbox
def test_a_pane_command_that_fails_reports_the_state_it_left(tmp: Path) -> None:
    host = Host(tmp, pane="fails")
    try:
        code, said = host.run("restart")
        text = "\n".join(said)
        check(code == 1 and "the pane command exited 1" in text, (code, text))
        check("it is stopped" in text, text)
        check(_recovery(said) == [PROJECT, "start", "impl-1"], said)
    finally:
        host.close()


@_sandbox
def test_a_session_that_is_not_the_workers_runtime_is_not_a_running_worker(tmp: Path) -> None:
    # Started into something else: the pane command said 0, the check says no.
    host = Host(tmp, running=(), pane="other")
    try:
        code, said = host.run("start")
        text = "\n".join(said)
        check(code == 1 and "impl-1 failed to start" in text, (code, text))
        check("not running hermes" in text, text)
        # And a start over that session does not call it running either.
        code, said = host.run("start")
        text = "\n".join(said)
        check(code == 1 and "already running" not in text and "not running hermes" in text, (code, text))
        check(_recovery(said) == [PROJECT, "restart", "impl-1"], said)
        check(len(host.pane_starts) == 1, f"attach-or-start was not asked to adopt it: {host.pane_starts}")
    finally:
        host.close()


@_sandbox
def test_a_worker_whose_state_cannot_be_read_is_unknown_not_started(tmp: Path) -> None:
    host = Host(tmp)
    host.probe_raises = True  # every liveness probe after the pane start
    try:
        code, said = host.run("restart")
        text = "\n".join(said)
        check(code == 1, (code, text))
        check("unknown" in text and "Permission denied" in text, text)
        check("impl-1 started" not in text and "already running" not in text, text)
        check(_recovery(said) == [PROJECT, "status", ""], said)
    finally:
        host.close()


@_sandbox
def test_a_stop_that_failed_starts_nothing_and_says_so(tmp: Path) -> None:
    host = Host(tmp)
    host.kill_fails = True
    try:
        before = host.pid(SESSION)
        code, said = host.run("restart")
        text = "\n".join(said)
        check(code == 1 and "impl-1 failed to stop" in text, (code, text))
        check(host.pane_starts == [], f"a start ran over a worker that was never stopped: {host.pane_starts}")
        check("impl-1 not restarted" in text and "it is running" in text, text)
        check(host.pid(SESSION) == before, "the worker was touched")
    finally:
        host.close()


@_sandbox
def test_a_restart_keeps_what_the_worker_holds(tmp: Path) -> None:
    """Restart moves a session. The worker's identity and its tickets stay."""
    host = Host(tmp)
    host.board = {"tickets": [{"id": "STEL-7", "assignee": "impl-1", "state": "in_progress", "title": "t"}]}
    try:
        before = _status_line(host)
        check("holding STEL-7" in before, before)
        code, said = host.run("restart")
        check(code == 0, (code, said))
        after = _status_line(host)
        check("holding STEL-7" in after and "ready, running" in after, after)
        check(before.split(":", 1)[0] == after.split(":", 1)[0], (before, after))
    finally:
        host.close()


@_sandbox
def test_start_and_stop_still_answer_the_state_asked_for(tmp: Path) -> None:
    host = Host(tmp, running=())
    try:
        code, said = host.run("start")
        check(code == 0 and any("impl-1 started" in line for line in said), (code, said))
        code, said = host.run("start")
        check(code == 0 and any("impl-1 already running" in line for line in said), (code, said))
        check(len(host.pane_starts) == 1, host.pane_starts)
        code, said = host.run("stop")
        check(code == 0 and any("impl-1 stopped" in line for line in said), (code, said))
        code, said = host.run("stop")
        check(code == 0 and any("impl-1 already stopped" in line for line in said), (code, said))
    finally:
        host.close()


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"worker_pool_restart_status_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
