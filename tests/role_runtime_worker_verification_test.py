#!/usr/bin/env python3
"""A runtime switch is a success only once the new worker is observed running (SYRD-560).

On 2026-10-05 an otto Hermes switch printed "restarted its session; reconnected
slot 5" while `switchyard present otto list` showed `worker=missing`. The
success text was keyed on `live_session_changed`: nothing looked at the worker
after the start. A provider that registers and then exits on its first breath
-- otto's Hermes, rejecting a flag -- passes every check made at the instant
of the start.

This reuses SYRD-559's harness: every pane is a real child process, started
through a symlink named for its provider, so the kernel and `ps` name it
exactly as they name a real `claude` or `codex`. tmux and the board are
stand-ins that apply the real rules.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import presentation_controller, role_runtime, team_launcher  # noqa: E402

import role_runtime_handover_test as h  # noqa: E402

CHECKS = 0

#: Panes keyed by what they do once started. Every one prints `ready` first.
SCRIPTS = {
    "steady": "import time; print('ready', flush=True); time.sleep(120)",
    # Registers, then dies on its first breath: otto's Hermes rejecting `--reasoning`.
    "exits": "import sys, time; print('ready', flush=True); time.sleep(0.3); sys.exit(2)",
}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class NamedProcesses(h.Processes):
    """Panes started as `<provider>`: a symlink to Python, so ps and the kernel see that name."""

    def __init__(self, bin_dir: Path) -> None:
        super().__init__()
        self.bin_dir = bin_dir
        for name in ("claude", "codex", "hermes"):
            link = bin_dir / name
            if not link.exists():
                link.symlink_to(sys.executable)

    def spawn_as(self, program: str, kind: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [str(self.bin_dir / program), "-c", SCRIPTS[kind]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )
        self.started.append(proc)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        return proc


class ProviderHost(h.PaneHost):
    """The launcher's start: the pane runs the provider the config names, registered first."""

    def __init__(self, processes: NamedProcesses, board: h.RegistrationBoard) -> None:
        super().__init__(processes, board)
        self.behaviour: dict[str, str] = {}   # runtime -> pane kind
        self.program: dict[str, str] = {}     # runtime -> what actually runs
        self.remain_on_exit = False           # tmux keeps a dead pane's session
        self.pid_hidden = False               # tmux gives no #{pane_pid}, only #{pane_current_command}
        self.after_start: Any = None          # called once a pane has registered
        # Answer one `#{pane_dead}` with 0, then kill the pane's process before
        # any further read: the provider dies mid-observation, every time.
        self.die_after_dead_probe = False

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in args]
        session = command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0] if "-t" in command else ""
        proc = self.sessions.get(session)
        if self.remain_on_exit and proc is not None:
            if command[:2] == ["tmux", "has-session"]:
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 0)
            if command[:2] == ["tmux", "display-message"] and command[-1] == "#{pane_dead}":
                self.calls.append(command)
                if self.die_after_dead_probe and proc.poll() is None:
                    self.die_after_dead_probe = False
                    proc.kill()
                    proc.wait(timeout=10)  # reaped, as tmux reaps it: /proc and the board agree it is gone
                    return subprocess.CompletedProcess(command, 0, stdout="0\n")
                return subprocess.CompletedProcess(command, 0, stdout="1\n" if proc.poll() is not None else "0\n")
        if command[:2] == ["tmux", "display-message"] and proc is not None and proc.poll() is None:
            if self.pid_hidden and command[-1] == "#{pane_pid}":
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no pid")
            if command[-1] == "#{pane_current_command}":
                self.calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout=f"{Path(proc.args[0]).name}\n")
        return super().__call__(args, **kwargs)

    def start(self, role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
        runtime = team_launcher._role_cli_name(role)
        self.starts.append((role.tmux_session, runtime))
        proc = self.processes.spawn_as(self.program.get(runtime, runtime), self.behaviour.get(runtime, "steady"))
        self.sessions[role.tmux_session] = proc
        self.board.register(role.role, runtime, h.identity(proc))
        if self.after_start is not None:
            hook, self.after_start = self.after_start, None
            hook()
        return 0


class Case(h.Case):
    def __init__(self) -> None:
        self.bin_tmp = tempfile.TemporaryDirectory(prefix="syrd560-bin-")
        super().__init__("fast")
        # The original pane is a real codex-named process, like the rest.
        self.processes.close()
        self.processes = NamedProcesses(Path(self.bin_tmp.name))
        self.host = ProviderHost(self.processes, self.board)
        self.board.rows.clear()
        self.old = self.processes.spawn_as("codex", "steady")
        self.old_holder = h.identity(self.old)
        self.host.adopt(h.SESSION, self.old, "codex")

    def switch(self, runtime: str = "claude", **handover: Any):
        fields = {f.name for f in dataclasses.fields(role_runtime.RuntimeHandover)}
        settings = {"read_assignments": self.board.details, "exit_timeout_seconds": 3.0,
                    "term_grace_seconds": 2.0, "kill_grace_seconds": 2.0,
                    "registration_timeout_seconds": 3.0, "poll_seconds": 0.02, "settle_seconds": 1.0}
        settings.update(handover)
        settings = {k: v for k, v in settings.items() if k in fields}
        config = team_launcher.load_project_config(h.PROJECT, self.config_path)
        return role_runtime.switch_role_runtime(
            config, config_path=self.config_path, role_name=h.ROLE, runtime=runtime,
            environ=h.DIRECTOR_ENV, runner=self.host, client=self.workflow,
            workflow_reader=self.workflow.reader, pane_state_dir=self.config_path.parent / "pane-state",
            print_func=self.output.append, start=self.host.start, busy_check=lambda *a, **k: False,
            handover=role_runtime.RuntimeHandover(**settings),
        )

    def close(self) -> None:
        super().close()
        self.bin_tmp.cleanup()


def refused(case: Case, **handover: Any) -> str:
    try:
        result = case.switch(**handover)
    except role_runtime.RoleRuntimeRefusal as exc:
        return str(exc)
    return f"NOT REFUSED: {result.describe()}"


def restored_codex(case: Case) -> None:
    """After a refused switch the old runtime is running and registered again."""
    h.time.sleep(0.4)
    pane = case.live_pane()
    check(pane is not None and case.board.holder() == h.identity(pane)
          and case.board.rows[h.ROLE]["runtime"] == "codex",
          f"codex is running and registered again: {case.board.rows}")
    check(case.workflow.runtime_of(h.ROLE) == "codex", "and the board declares it again")


def test_a_healthy_replacement_is_reported_as_one() -> None:
    """The control: a claude that starts and stays up is a success, and says so."""
    case = Case()
    try:
        result = case.switch()
        check(result.live_session_changed and "now runs claude" in result.describe(), result.describe())
        h.time.sleep(0.3)
        pane = case.live_pane()
        check(pane is not None and case.board.holder() == h.identity(pane), "the claude pane holds the role")
    finally:
        case.close()


def test_a_provider_that_exits_after_registering_is_not_a_success() -> None:
    """The otto case: the pane command succeeded, registered, and the provider exited."""
    case = Case()
    case.host.behaviour["claude"] = "exits"
    try:
        message = refused(case)
        check(not message.startswith("NOT REFUSED"), f"a worker that exited was reported as a switch: {message}")
        check("still runs codex" in message and "the switch was undone" in message, message)
        check("claude pane for research exited" in message or "is no longer running" in message
              or "closed" in message, f"the reason says the claude worker is gone: {message}")
        restored_codex(case)
    finally:
        case.close()


def test_a_pane_running_the_wrong_provider_is_not_a_success() -> None:
    """The pane started, registered and stayed up -- running codex, not the claude asked for."""
    case = Case()
    case.host.program["claude"] = "codex"
    try:
        message = refused(case)
        check(not message.startswith("NOT REFUSED"), f"a wrong provider was reported as a switch: {message}")
        check("runs codex, not claude" in message, f"the reason names what runs instead: {message}")
        restored_codex(case)
    finally:
        case.close()


def test_a_worker_that_cannot_be_observed_is_not_a_success() -> None:
    """No process table to read is "cannot say", never "fine"."""
    case = Case()
    original = team_launcher._process_snapshot
    team_launcher._process_snapshot = lambda: ({}, {}, {}, {})
    try:
        message = refused(case)
        check(not message.startswith("NOT REFUSED"), f"an unobservable worker was reported as a switch: {message}")
        check("could not be read" in message, f"the reason is that it could not be observed: {message}")
    finally:
        team_launcher._process_snapshot = original
        case.close()


def test_a_slot_that_does_not_show_the_worker_is_not_a_success() -> None:
    """Reconnected slot 1 must, by `present list`'s own reading, show the role, connected."""
    case = Case()
    original_reconnect = presentation_controller.reconnect_role_slots
    original_report = presentation_controller.presentation_report
    original_enabled = presentation_controller.presentation_enabled
    presentation_controller.presentation_enabled = lambda *a, **k: True
    presentation_controller.reconnect_role_slots = lambda *a, **k: (1,)
    presentation_controller.presentation_report = lambda *a, **k: {"slots": [
        {"slot": 1, "desired_role": h.ROLE, "actual_role": None, "client_state": "disconnected",
         "worker": {"role": h.ROLE, "state": "live", "live": True}},
    ]}
    try:
        message = refused(case)
        check(not message.startswith("NOT REFUSED"), f"an unbound slot was reported as reconnected: {message}")
        check("slot 1" in message and "disconnected" in message, f"the reason names the slot and its state: {message}")
    finally:
        presentation_controller.reconnect_role_slots = original_reconnect
        presentation_controller.presentation_report = original_report
        presentation_controller.presentation_enabled = original_enabled
        case.close()


def test_a_dead_pane_tmux_keeps_is_not_a_success() -> None:
    """With remain-on-exit the session outlives its provider: the pane, not the session, is the worker."""
    case = Case()
    case.host.behaviour["claude"] = "exits"
    case.host.remain_on_exit = True
    try:
        message = refused(case)
        check("shows a dead pane" in message, f"the reason is the dead pane: {message}")
        check("still runs codex" in message, message)
    finally:
        case.close()


def test_a_provider_dying_mid_observation_is_reported_as_the_dead_pane() -> None:
    """Audit's flake made deterministic: the pane read alive, then the provider died.

    The later reads -- its processes, its board registration -- then see only
    the consequence. The dead pane is the direct observation and is what is
    reported, not "could not be read" or a stale registration.
    """
    case = Case()
    case.host.remain_on_exit = True
    case.host.die_after_dead_probe = True
    try:
        message = refused(case)
        check("research exited; porter-research shows a dead pane" in message,
              f"the dead pane, not its consequence, is the reason: {message}")
        check("still runs codex" in message, message)
    finally:
        case.close()


def test_a_registration_taken_during_the_settle_is_not_a_success() -> None:
    """The worker stays up, but another live process takes the role's board row before it settles."""
    case = Case()
    decoy = case.processes.spawn_as("claude", "steady")

    def take_the_row() -> None:
        case.board.rows[h.ROLE].update(process_pid=decoy.pid, process_start_time=h.identity(decoy)[1])

    def later() -> None:
        h.threading.Timer(0.3, take_the_row).start()

    case.host.after_start = later
    try:
        message = refused(case)
        check(f"the board names pid {decoy.pid}" in message, f"the reason is the lost registration: {message}")
    finally:
        case.close()


def test_a_slot_showing_the_role_with_its_worker_missing_is_not_a_success() -> None:
    """The otto report itself: connected to the role, and `worker=missing`."""
    case = Case()
    saved = (presentation_controller.reconnect_role_slots, presentation_controller.presentation_report,
             presentation_controller.presentation_enabled)
    presentation_controller.presentation_enabled = lambda *a, **k: True
    presentation_controller.reconnect_role_slots = lambda *a, **k: (5,)
    presentation_controller.presentation_report = lambda *a, **k: {"slots": [
        {"slot": 5, "desired_role": h.ROLE, "actual_role": h.ROLE, "client_state": "connected",
         "worker": {"role": h.ROLE, "state": "missing", "live": False}},
    ]}
    try:
        message = refused(case)
        check("slot 5 shows research with worker=missing" in message, f"the reason is the missing worker: {message}")
        check(f"switchyard present {h.PROJECT} show research --slot 5" in message, f"with the exact remedy: {message}")
    finally:
        (presentation_controller.reconnect_role_slots, presentation_controller.presentation_report,
         presentation_controller.presentation_enabled) = saved
        case.close()


def test_without_a_pane_pid_the_command_tmux_reports_decides() -> None:
    """No pid from tmux: its own `#{pane_current_command}`, as the launcher's start check reads it."""
    good = Case()
    good.host.pid_hidden = True
    try:
        outcome = refused(good)
        check(outcome.startswith("NOT REFUSED") and "now runs claude" in outcome,
              f"the right provider by tmux's command is a success: {outcome}")
    finally:
        good.close()
    wrong = Case()
    wrong.host.pid_hidden = True
    wrong.host.program["claude"] = "codex"
    try:
        message = refused(wrong)
        check("runs codex, not claude" in message, f"and the wrong one is not: {message}")
    finally:
        wrong.close()


def main() -> int:
    failures = 0
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            try:
                case()
                print(f"PASS {name}", flush=True)
            except AssertionError as exc:
                failures += 1
                print(f"FAILED {name}: AssertionError: {exc}", flush=True)
            except BaseException as exc:  # noqa: BLE001
                failures += 1
                print(f"FAILED {name}: {type(exc).__name__}: {exc}", flush=True)
    print(f"role_runtime_worker_verification_test: {CHECKS} checks, {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
