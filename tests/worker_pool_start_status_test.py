#!/usr/bin/env python3
"""A worker that was not started is not a success, and preflight says so first.

Live MEFP, 2026-09-26: after applying a two-role worker pool, `switchyard
worker-pool mefp preflight` reported zero blockers, and `switchyard
worker-pool mefp start luna-1` exited 0 while printing

    luna-1 not started: its worktree is missing

No session started. `apply` declares a worker -- identity, route, board
registration -- and puts nothing on disk; `start` moves a tmux session and
nothing else; the worktree comes from the per-role preparation, which nothing
named (SYRD-278).

These cases run the real `switchyard worker-pool` command over a sandbox
project whose owner home carries the real board skill, with a runner that
answers the read-only probes and records any pane it is asked to start.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402

import worker_pool_lifecycle_test as lifecycle  # noqa: E402

PROJECT = lifecycle.PROJECT
POOL = lifecycle.POOL
WORKERS = ["impl-1", "impl-2"]
PREPARE = ROOT / "scripts" / "ticket-board-workflow"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """A sandbox project, its owner home, and a runner that records what it is asked."""

    def __init__(self, tmp: Path, *, running: set[str] = frozenset(), pane_exit: int = 0) -> None:
        self.tmp = tmp
        self.config, self.config_path = lifecycle.config_with_pool(tmp, pool=POOL, workers=WORKERS)
        pool = lifecycle.pool_of(POOL)
        self.document, _ = worker_pool.expand_pool(
            lifecycle.base_document(), pool, project=PROJECT, worktree_base=tmp / "worktrees"
        )
        self.home = tmp / "home"
        board_skill.install_board_skill(
            home=self.home, source=board_skill.default_source(ROOT), source_commit="syrd278"
        )
        self.running = set(running)
        self.pane_exit = pane_exit
        self.started: list[list[str]] = []
        self.killed: list[list[str]] = []

    def worktree(self, member: str) -> Path:
        return self.tmp / "worktrees" / member

    def prepare(self, member: str) -> None:
        """Where the supported preparation leaves a worker's worktree."""
        self.worktree(member).mkdir(parents=True)

    def runner(self, args, **_kwargs):
        command = [str(part) for part in args]
        if "has-session" in command:
            session = command[command.index("-t") + 1].lstrip("=") if "-t" in command else ""
            return subprocess.CompletedProcess(command, 0 if session in self.running else 1)
        if "kill-session" in command:
            self.killed.append(command)
            session = command[command.index("-t") + 1].lstrip("=")
            self.running.discard(session)
            return subprocess.CompletedProcess(command, 0)
        if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/hermes\n")
        if "config" in command and "check" in command:
            return subprocess.CompletedProcess(command, 0, stdout="\N{CHECK MARK} OPENROUTER_API_KEY\n")
        # Anything else is the pane start itself.
        self.started.append(command)
        return subprocess.CompletedProcess(command, self.pane_exit)

    def run(self, action: str, member: str = "") -> tuple[int, str]:
        said: list[str] = []
        original = team_launcher._owner_home_for_auth
        team_launcher._owner_home_for_auth = lambda _owner: self.home
        try:
            code = team_launcher.switchyard_worker_pool_command(
                PROJECT, action=action, member=member,
                config_dir=self.tmp, registry_dir=self.tmp / "registry",
                board_reader=lambda _config: {"document": self.document},
                board_snapshot_reader=lambda _config: None,
                runner=self.runner, print_func=said.append,
            )
        finally:
            team_launcher._owner_home_for_auth = original
        return code, "\n".join(said)


def _in_sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd278.") as tmp:
            case(Path(tmp))
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


@_in_sandbox
def test_a_worker_with_no_worktree_is_refused_and_the_command_fails(tmp: Path) -> None:
    """The live case: refused, and the exit status says so."""
    host = Host(tmp)
    code, said = host.run("start", "impl-1")
    check(code == 1, f"a worker that did not start exited {code}: {said}")
    check("impl-1 not started" in said and "its worktree is missing" in said, said)
    check(host.started == [], f"a worker that is not ready must not be started: {host.started}")
    # The refusal names the supported preparation, as a command that runs.
    check("Prepare it first, then start it again: " in said, f"the refusal names no preparation: {said}")
    remedy = said.split("then start it again: ", 1)[1].strip()
    argv = shlex.split(remedy)
    check(argv[:2] == [str(PREPARE), "prepare-role"], argv)
    check(argv[argv.index("--config") + 1] == str(host.config_path), argv)
    check(argv[argv.index("--role") + 1] == "impl-1", argv)
    dry = subprocess.run(argv + ["--dry-run"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"})
    check(dry.returncode == 0 and "Would prepare only impl-1" in dry.stdout, (dry.returncode, dry.stdout, dry.stderr))


@_in_sandbox
def test_preflight_names_every_unprepared_worker_before_anybody_starts_one(tmp: Path) -> None:
    host = Host(tmp)
    code, said = host.run("preflight")
    check(code == 1, f"preflight passed over workers that start would refuse: {said}")
    line = next((line for line in said.splitlines() if "worktrees" in line), "")
    check("impl-1, impl-2 are declared but have no worktree yet" in line, said)
    for member in WORKERS:
        check(f"--role {member}" in line and str(PREPARE) in line, line)
    # Read-only: it probed and started nothing, and made nothing on disk.
    check(host.started == [] and host.killed == [], (host.started, host.killed))
    check(not (tmp / "worktrees" / "impl-1").exists(), "preflight created a worktree")

    host.prepare("impl-1")
    code, said = host.run("preflight")
    line = next((line for line in said.splitlines() if "worktrees" in line), "")
    check("impl-2 is declared but has no worktree yet" in line and "impl-1" not in line, line)

    host.prepare("impl-2")
    code, said = host.run("preflight")
    check(not any("worktrees" in line for line in said.splitlines()), said)


@_in_sandbox
def test_after_preparation_the_same_start_succeeds(tmp: Path) -> None:
    host = Host(tmp)
    check(host.run("start", "impl-1")[0] == 1, "refused before preparation")
    host.prepare("impl-1")
    code, said = host.run("start", "impl-1")
    check(code == 0, said)
    check("impl-1 started" in said, said)
    check(len(host.started) == 1, f"exactly one pane command: {host.started}")


@_in_sandbox
def test_an_already_running_worker_is_what_start_asked_for(tmp: Path) -> None:
    host = Host(tmp, running={f"{PROJECT}-impl-1"})
    host.prepare("impl-1")
    code, said = host.run("start", "impl-1")
    check(code == 0 and "impl-1 already running" in said, (code, said))
    check(host.started == [], host.started)


@_in_sandbox
def test_mixed_outcomes_fail_the_command_that_did_not_get_what_it_asked_for(tmp: Path) -> None:
    host = Host(tmp, running={f"{PROJECT}-impl-2"})
    host.prepare("impl-1")
    # Across workers: one comes up, the unprepared one is refused.
    check(host.run("start", "impl-1")[0] == 0, "the prepared worker starts")
    code, said = host.run("start", "impl-2")
    check(code == 1 and "impl-2 not started" in said, (code, said))
    # Within one command: a restart whose stop worked and whose start was
    # refused left a stopped worker; both halves are reported, and it fails.
    code, said = host.run("restart", "impl-2")
    check(code == 1, (code, said))
    check("impl-2 stopped" in said and "impl-2 not started" in said, said)
    check(host.killed, "the stop really ran")
    # Stopping a worker that is not running is what stop asked for.
    code, said = host.run("stop", "impl-2")
    check(code == 0 and "impl-2 already stopped" in said, (code, said))


@_in_sandbox
def test_a_pane_command_that_fails_fails_the_start(tmp: Path) -> None:
    host = Host(tmp, pane_exit=1)
    host.prepare("impl-1")
    code, said = host.run("start", "impl-1")
    check(code == 1 and "impl-1 failed to start" in said, (code, said))


def test_the_plan_attributes_the_worktree_blocker_to_preparation() -> None:
    check(worker_pool.BLOCKER_CLEARED_BY.get("worktrees") == "worker preparation",
          worker_pool.BLOCKER_CLEARED_BY)


def test_the_docs_say_preparation_is_its_own_step() -> None:
    life = (ROOT / "docs" / "syrd-37-worker-pools.md").read_text(encoding="utf-8")
    check("prepare-role" in life and "start` does not prepare" in life, "worker-pools life cycle")
    packet = (ROOT / "docs" / "syrd-37-mefp-upgrade-packet.md").read_text(encoding="utf-8")
    check("prepare-role" in packet, "MEFP packet")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"worker_pool_start_status_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
