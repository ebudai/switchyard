#!/usr/bin/env python3
"""A runtime switch hands registration over without racing the pane it stopped (SYRD-559).

`switchyard set-role-runtime otto main --cli codex …` stopped the old worker
and started the replacement within milliseconds. `tmux kill-session` returns
once tmux has signalled the pane, not once its process has gone, so the board
still saw the old pane's process alive, refused the new pane's registration
("role main is held by a live pane"), and the replacement exited about 47 ms
later. The command checked only that a tmux session existed, so it reported a
switch that left the role unregistered.

Every old pane here is a real child process, so "is it still alive" is answered
by the kernel exactly as the board answers it (`peer_identity.read_process`,
pid plus start time). The board is a stand-in that applies the server's own
rule from `server.handle_register_caller`: a registration is refused while the
role's recorded holder is still a live process. tmux is a stand-in too, and its
`kill-session` does what tmux does: it sends SIGHUP and returns at once.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import presentation_controller, role_runtime, team_launcher  # noqa: E402
from scripts.ticket_board.peer_identity import read_process  # noqa: E402

from role_runtime_test import DIRECTOR_ENV, FakeBoard, _ready  # noqa: E402

PROJECT = "porter"
ROLE = "research"
SESSION = f"{PROJECT}-{ROLE}"
TARGET = f"{SESSION}:0.0"
CHECKS = 0

# What each old pane does when tmux hangs it up. "fast" takes the default
# action; "delayed" is the measured otto case, a CLI that cleans up first.
PANE_SCRIPTS = {
    "fast": "import sys,time; print('ready', flush=True); time.sleep(120)",
    "delayed": (
        "import signal,sys,time\n"
        "def bye(*_a):\n"
        "    time.sleep(1.0); sys.exit(0)\n"
        "signal.signal(signal.SIGHUP, bye)\n"
        "print('ready', flush=True); time.sleep(120)\n"
    ),
    "ignores_hup": (
        "import signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(120)"
    ),
    "ignores_hup_and_term": (
        "import signal,time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)"
    ),
}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Processes:
    """Every child this test starts, so none outlives a case, pass or fail.

    It also reaps them the moment they exit, as the tmux server reaps its
    panes. Unreaped, an exited pane is a zombie whose /proc entry keeps its
    start time -- alive to the board's rule and to the switch alike -- which is
    true of an unreaped process and not of a tmux pane.
    """

    def __init__(self) -> None:
        self.started: list[subprocess.Popen] = []
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, daemon=True)
        self._reaper.start()

    def _reap(self) -> None:
        while not self._stop.is_set():
            for proc in list(self.started):
                proc.poll()
            time.sleep(0.005)

    def spawn(self, kind: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", PANE_SCRIPTS[kind]],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            # Its own session, so a signal meant for the pane never reaches us.
            start_new_session=True,
        )
        self.started.append(proc)
        # Handlers are installed before the pane is handed over, or a hang-up
        # sent too early would find the default action and prove nothing.
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        return proc

    def close(self) -> None:
        self._stop.set()
        self._reaper.join(timeout=5)
        for proc in self.started:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def identity(proc: subprocess.Popen) -> tuple[int, int]:
    info = read_process(proc.pid)
    assert info is not None, f"pid {proc.pid} has no /proc entry"
    return proc.pid, info.start_time


def alive(holder: tuple[int, int]) -> bool:
    info = read_process(holder[0])
    return info is not None and info.start_time == holder[1]


class RegistrationBoard:
    """The board's registration rule and its runtime-assignments read.

    `register` is `server.handle_register_caller`'s rule. `details` is
    `/api/runtime-assignments`, including its filter: a row whose runtime the
    workflow no longer declares is not shown (`app.runtime_targets`).
    """

    def __init__(self, workflow: FakeBoard) -> None:
        self.workflow = workflow
        self.rows: dict[str, dict[str, Any]] = {}
        self.refusals: list[str] = []
        self.refuse_runtime = ""
        self.refuse_runtimes: set[str] = set()
        # When set, the next accepted registration is recorded against this
        # other live process instead of the pane that registered.
        self.decoy: tuple[int, int] | None = None

    def register(self, role: str, runtime: str, holder: tuple[int, int]) -> bool:
        if runtime == self.refuse_runtime or runtime in self.refuse_runtimes:
            self.refusals.append(f"{role}: runtime {runtime} refused")
            return False
        previous = self.rows.get(role)
        if previous is not None:
            old = (int(previous["process_pid"]), int(previous["process_start_time"]))
            if alive(old) and old != holder:
                self.refusals.append(f"role {role} is held by a live pane")
                return False
        if self.decoy is not None:
            holder, self.decoy = self.decoy, None
        self.rows[role] = {
            "role": role, "runtime": runtime, "actual_target": TARGET,
            "process_pid": holder[0], "process_start_time": holder[1],
        }
        return True

    def details(self, _config: Any) -> tuple[dict[str, dict[str, Any]], str]:
        return {
            name: dict(row)
            for name, row in self.rows.items()
            if self.workflow.runtime_of(name) == row["runtime"]
        }, ""

    def holder(self, role: str = ROLE) -> tuple[int, int] | None:
        row = self.rows.get(role)
        return None if row is None else (int(row["process_pid"]), int(row["process_start_time"]))


class PaneHost:
    """tmux, as far as a runtime switch can see it, over real pane processes."""

    def __init__(self, processes: Processes, board: RegistrationBoard) -> None:
        self.processes = processes
        self.board = board
        self.sessions: dict[str, subprocess.Popen] = {}
        self.calls: list[list[str]] = []
        self.starts: list[tuple[str, str]] = []
        self.next_kind = "fast"
        # tmux answering nothing for #{pane_pid}, as it can for a pane that is
        # still being created.
        self.pane_pid_unreadable = False

    def adopt(self, session: str, proc: subprocess.Popen, runtime: str) -> None:
        self.sessions[session] = proc
        assert self.board.register(ROLE, runtime, identity(proc))

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = [str(part) for part in args]
        self.calls.append(command)
        if command[:2] == ["tmux", "has-session"]:
            session = command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0]
            proc = self.sessions.get(session)
            return subprocess.CompletedProcess(command, 0 if proc is not None and proc.poll() is None else 1)
        if command[:2] == ["tmux", "kill-session"]:
            session = command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0]
            proc = self.sessions.pop(session, None)
            if proc is not None and proc.poll() is None:
                # What tmux does: hang the pane up, and return without waiting.
                os.kill(proc.pid, signal.SIGHUP)
            return subprocess.CompletedProcess(command, 0)
        if command[:2] == ["tmux", "display-message"] and command[-1] == "#{pane_pid}":
            if self.pane_pid_unreadable:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no pane")
            session = command[command.index("-t") + 1].removeprefix("=").split(":", 1)[0]
            proc = self.sessions.get(session)
            if proc is None or proc.poll() is not None:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="no such session")
            return subprocess.CompletedProcess(command, 0, stdout=f"{proc.pid}\n")
        return subprocess.CompletedProcess(command, 0, stdout="")

    def start(self, role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
        """The launcher's start: a pane whose first act is to register.

        Refused, it exits a moment later, as the real registrar does
        (`ticket-board-register-runtime` returns 1 and the pane closes) -- after
        the start has already reported success.
        """
        runtime = team_launcher._role_cli_name(role)
        self.starts.append((role.tmux_session, runtime))
        proc = self.processes.spawn(self.next_kind)
        self.sessions[role.tmux_session] = proc
        if not self.board.register(role.role, runtime, identity(proc)):
            timer = threading.Timer(0.047, lambda: proc.poll() is None and proc.kill())
            timer.daemon = True
            timer.start()
        return 0


def _write_config(root: Path) -> Path:
    provision = root / ".switchyard" / "provision"
    provision.mkdir(parents=True)
    layout = provision / f"{PROJECT}-konsole-layout.json"
    layout.write_text(json.dumps(team_launcher._new_project_layout_payload(2)) + "\n", encoding="utf-8")
    roles = []
    for slot, (name, cli, detached) in enumerate(
        (("director", "claude", False), ("audit", "claude", False), (ROLE, "codex", True))
    ):
        workdir = root / name
        workdir.mkdir()
        roles.append({
            "role": name,
            "slot": None if detached else slot,
            "detached": detached,
            "target": f"{PROJECT}-{name}:0.0",
            "tmux_session": f"{PROJECT}-{name}",
            "workdir": str(workdir),
            "cli": [cli],
            "live_commands": [cli],
            # A process-authority pane: it registers with the board before its
            # CLI starts. The socket is never opened here; the board is faked.
            "env": {
                "TICKET_BOARD_SOCKET": str(root / "no-board.sock"),
                "TICKET_BOARD_PROCESS_AUTHORITY": "1",
            },
        })
    config_path = provision / f"{PROJECT}.json"
    config_path.write_text(json.dumps({
        "project": PROJECT,
        "layout": str(layout),
        "session_dir": str(root / "sessions"),
        "board_url": "http://127.0.0.1:65535",
        "presentation": {"slot_count": 2},
        "roles": roles,
    }, indent=2) + "\n", encoding="utf-8")
    return config_path


def _handover(board: RegistrationBoard, **overrides: Any) -> dict[str, Any]:
    """The handover seam where it exists; nothing on a release without one.

    On the base release this leaves the switch exactly as it ships, so the
    cases fail there for the reason the ticket describes, not for a missing
    keyword.
    """
    factory = getattr(role_runtime, "RuntimeHandover", None)
    if factory is None:
        return {}
    settings = {
        "read_assignments": board.details,
        "exit_timeout_seconds": 5.0,
        "term_grace_seconds": 3.0,
        "kill_grace_seconds": 3.0,
        "registration_timeout_seconds": 5.0,
        "poll_seconds": 0.02,
    }
    settings.update(overrides)
    return {"handover": factory(**settings)}


class Case:
    """One tenant: config, workflow board, registration board, panes."""

    def __init__(self, old_kind: str) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="syrd559-")
        self.root = Path(self.tmp.name)
        self.config_path = _write_config(self.root)
        (self.config_path.parent / "pane-state").mkdir()
        self.workflow = FakeBoard()
        self.board = RegistrationBoard(self.workflow)
        self.processes = Processes()
        self.host = PaneHost(self.processes, self.board)
        self.old = self.processes.spawn(old_kind)
        self.old_holder = identity(self.old)
        self.host.adopt(SESSION, self.old, "codex")
        self.output: list[str] = []
        self.saved_readiness = role_runtime._readiness_blockers
        role_runtime._readiness_blockers = _ready

    def switch(self, runtime: str = "claude", **handover: Any):
        config = team_launcher.load_project_config(PROJECT, self.config_path)
        return role_runtime.switch_role_runtime(
            config,
            config_path=self.config_path,
            role_name=ROLE,
            runtime=runtime,
            environ=DIRECTOR_ENV,
            runner=self.host,
            client=self.workflow,
            workflow_reader=self.workflow.reader,
            pane_state_dir=self.config_path.parent / "pane-state",
            print_func=self.output.append,
            start=self.host.start,
            busy_check=lambda *a, **k: False,
            **_handover(self.board, **handover),
        )

    def live_pane(self) -> subprocess.Popen | None:
        proc = self.host.sessions.get(SESSION)
        return proc if proc is not None and proc.poll() is None else None

    def close(self) -> None:
        role_runtime._readiness_blockers = self.saved_readiness
        self.processes.close()
        self.tmp.cleanup()


def _assert_handed_over(case: Case, result: Any, runtime: str) -> None:
    check(result.live_session_changed, f"the live session was replaced: {result}")
    # Settle past the 47 ms a refused pane takes to exit, so a pane that only
    # looked live at the instant of the check cannot pass.
    time.sleep(0.3)
    pane = case.live_pane()
    check(pane is not None, f"the replacement pane is still running: {case.board.refusals}")
    check(case.board.holder() == identity(pane),
          f"the board's holder for {ROLE} is the replacement pane: {case.board.holder()} vs {identity(pane)}")
    check(case.board.rows[ROLE]["runtime"] == runtime, case.board.rows[ROLE])
    check(case.board.refusals == [], f"no registration was refused: {case.board.refusals}")
    check(not alive(case.old_holder), f"the old pane process is gone: {case.old_holder}")
    check(case.workflow.runtime_of(ROLE) == runtime, case.workflow.document)


def _switched(case: Case, **handover: Any) -> Any:
    try:
        return case.switch(**handover)
    except role_runtime.RoleRuntimeRefusal as exc:
        raise AssertionError(f"the switch was refused: {exc}") from exc


def _run(old_kind: str, **handover: Any) -> None:
    case = Case(old_kind)
    try:
        result = _switched(case, **handover)
        _assert_handed_over(case, result, "claude")
    finally:
        case.close()


def test_an_old_pane_that_exits_at_once_hands_over() -> None:
    _run("fast")


def test_an_old_pane_that_takes_a_second_to_exit_is_waited_for() -> None:
    """The otto case: the CLI cleans up for about a second after the hang-up."""
    _run("delayed")


def test_an_old_pane_that_ignores_the_hangup_is_terminated_then_replaced() -> None:
    _run("ignores_hup", exit_timeout_seconds=0.3)


def test_an_old_pane_that_ignores_hangup_and_terminate_is_killed_then_replaced() -> None:
    _run("ignores_hup_and_term", exit_timeout_seconds=0.3, term_grace_seconds=0.3)


def test_a_holder_the_board_records_outside_the_pane_is_waited_for_too() -> None:
    """The board, not tmux, decides the refusal.

    Here the role's recorded holder is a live process outside the pane -- an
    old CLI that left its session -- so the pane's own exit proves nothing. It
    is read from the board before the workflow changes (afterwards its row is
    hidden), waited for, and, since no hang-up reaches it, terminated.
    """
    case = Case("fast")
    orphan = case.processes.spawn("ignores_hup")
    orphan_holder = identity(orphan)
    case.board.rows[ROLE].update(process_pid=orphan_holder[0], process_start_time=orphan_holder[1])
    try:
        result = _switched(case, exit_timeout_seconds=0.3)
        _assert_handed_over(case, result, "claude")
        check(not alive(orphan_holder), f"the recorded holder outside the pane is gone: {orphan_holder}")
    finally:
        case.close()


def test_a_row_naming_another_live_process_is_not_this_panes_registration() -> None:
    """A live row is not enough: it has to name the pane that was started."""
    case = Case("fast")
    decoy = case.processes.spawn("fast")
    case.board.decoy = identity(decoy)
    try:
        try:
            case.switch(registration_timeout_seconds=0.5, exit_timeout_seconds=0.3)
            refused = ""
        except role_runtime.RoleRuntimeRefusal as exc:
            refused = str(exc)
        check("did not register with the board within 0.5s" in refused
              and f"the board names pid {decoy.pid}, but {SESSION}'s pane is pid" in refused,
              f"the switch named the mismatch: {refused}")
        check("still runs codex" in refused and "the switch was undone" in refused, refused)
        time.sleep(0.3)
        pane = case.live_pane()
        check(pane is not None and case.board.holder() == identity(pane)
              and case.board.rows[ROLE]["runtime"] == "codex",
              f"the restored codex pane holds the role: {case.board.rows}")
    finally:
        case.close()


def test_a_reused_pid_is_never_signalled() -> None:
    """The shipped signalling opens a pidfd and re-identifies it by start time.

    A holder whose pid now belongs to a different process -- a different start
    time -- is left alone, however firmly the holder is being stopped.
    """
    import importlib

    handover_module = importlib.import_module("scripts.runtime_handover")
    processes = Processes()
    try:
        bystander = processes.spawn("ignores_hup_and_term")
        pid, started = identity(bystander)
        handover_module.signal_holder((pid, started + 1), signal.SIGKILL, handover_module.RuntimeHandover())
        time.sleep(0.2)
        check(bystander.poll() is None, "a process with the holder's pid but another start time was not killed")
        handover_module.signal_holder((pid, started), signal.SIGKILL, handover_module.RuntimeHandover())
        deadline = time.monotonic() + 5
        while bystander.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        check(bystander.poll() is not None, "while the real holder, identified exactly, was")
    finally:
        processes.close()


def test_a_holder_the_board_does_not_show_is_still_waited_for() -> None:
    """The board declares another runtime than the launcher runs (SYRD-525).

    Its read then hides the old pane's row even before the switch -- the
    runtime differs -- while its refusal still counts that pane. The pane's
    own process, from tmux, is what is waited for.
    """
    case = Case("delayed")
    for entry in case.workflow.document["roles"]:
        if entry["name"] == ROLE:
            entry["runtime"] = "agy"
    try:
        check(case.board.details(None)[0].get(ROLE) is None, "the board's read hides the old pane's row")
        result = _switched(case)
        _assert_handed_over(case, result, "claude")
    finally:
        case.close()


def test_a_stale_row_is_not_taken_for_a_pane_tmux_cannot_name() -> None:
    """Undoing a switch whose restored pane is refused too.

    The board's row for the old runtime is visible again and names the
    original pane, which has gone; tmux gives no pane pid to compare with. A
    row naming a dead process is not a registration, so the undo is reported
    as unfinished, not as clean.
    """
    case = Case("fast")
    case.board.refuse_runtimes = {"claude", "codex"}
    case.host.pane_pid_unreadable = True
    try:
        try:
            case.switch(registration_timeout_seconds=2.0)
            refused = ""
        except role_runtime.RoleRuntimeRefusal as exc:
            refused = str(exc)
        check("is left between runtimes and needs an operator" in refused
              and "the codex pane for research closed before the board recorded its registration" in refused,
              f"the undo is not called clean over a dead row: {refused}")
        check("the switch was undone" not in refused, refused)
    finally:
        case.close()


def test_the_shipped_default_reads_the_tenants_board_through_the_launcher() -> None:
    """No injected reader: the board is read the way production reads it.

    Every other case injects the assignments reader, which would leave the
    default untested. Here it is left unset, and the launcher's own reader --
    the one that opens the tenant's board socket -- answers, recorded.
    """
    case = Case("delayed")
    reads: list[str] = []
    original = team_launcher.read_runtime_assignment_details

    def recorded(config: Any) -> tuple[dict[str, dict[str, Any]], str]:
        reads.append(config.project)
        return case.board.details(config)

    team_launcher.read_runtime_assignment_details = recorded
    try:
        result = _switched(case, read_assignments=None)
        _assert_handed_over(case, result, "claude")
        check(reads and set(reads) == {PROJECT}, f"the default read the tenant's board: {reads}")
    finally:
        team_launcher.read_runtime_assignment_details = original
        case.close()


def test_a_holder_that_cannot_be_removed_stops_before_any_replacement_starts() -> None:
    """A process that will not die (here: signals are swallowed) is never raced.

    The replacement is not started at all, the command exits non-zero, and the
    message names the process that is still holding the role.
    """
    case = Case("ignores_hup_and_term")
    sent: list[tuple[int, int]] = []
    try:
        try:
            case.switch(
                exit_timeout_seconds=0.2, term_grace_seconds=0.2, kill_grace_seconds=0.2,
                signal_process=lambda pid, sig: sent.append((pid, sig)),
            )
            refused = ""
        except role_runtime.RoleRuntimeRefusal as exc:
            refused = str(exc)
        check(refused, "a switch whose old pane never left did not fail")
        check(str(case.old.pid) in refused and "did not exit" in refused,
              f"the refusal names the process still holding {ROLE}: {refused}")
        check(not any(runtime == "claude" for _session, runtime in case.host.starts),
              f"no claude pane was started against a live holder: {case.host.starts}")
        check((case.old.pid, signal.SIGTERM) in sent and (case.old.pid, signal.SIGKILL) in sent,
              f"it escalated to TERM then KILL before giving up: {sent}")
        check(case.board.refusals == [], f"nothing raced the holder: {case.board.refusals}")
        check(case.workflow.runtime_of(ROLE) == "codex", "the board's declaration was rolled back")
    finally:
        case.close()


def test_a_replacement_that_cannot_register_is_rolled_back_to_a_registered_old_runtime() -> None:
    """The new runtime's pane is refused for its own reason and exits.

    That is caught from the board's own row, not from a tmux session that
    briefly existed, and the rollback brings the old runtime back registered.
    """
    case = Case("delayed")
    case.board.refuse_runtime = "claude"
    try:
        try:
            case.switch()
            refused = ""
        except role_runtime.RoleRuntimeRefusal as exc:
            refused = str(exc)
        check(refused, "a replacement that never registered was reported as a switch")
        check("still runs codex" in refused and "the switch was undone" in refused, refused)
        check("the claude pane for research closed before the board recorded its registration" in refused,
              f"the reason is the replacement's own registration, from the board: {refused}")
        time.sleep(0.3)
        pane = case.live_pane()
        check(pane is not None, "the rolled-back codex pane is running")
        check(case.board.holder() == identity(pane) and case.board.rows[ROLE]["runtime"] == "codex",
              f"and holds the role: {case.board.rows}")
        check(case.board.refusals == ["research: runtime claude refused"],
              f"only the injected refusal happened: {case.board.refusals}")
    finally:
        case.close()


def test_a_rollback_waits_for_the_replacement_it_stops() -> None:
    """The reverse race: the switch fails after the new pane registered.

    Undoing it stops that pane and starts the old runtime again; the old pane
    must not be refused by the new one's still-live registration.
    """
    case = Case("fast")
    case.host.next_kind = "delayed"
    original = presentation_controller.reconnect_role_slots

    def failing_reconnect(*_a: Any, **_k: Any):
        raise RuntimeError("injected slot reconnect failure")

    presentation_controller.reconnect_role_slots = failing_reconnect
    try:
        try:
            case.switch()
            refused = ""
        except role_runtime.RoleRuntimeRefusal as exc:
            refused = str(exc)
        check("still runs codex" in refused and "the switch was undone" in refused,
              f"the rollback restored the old runtime: {refused}")
        time.sleep(0.3)
        pane = case.live_pane()
        check(pane is not None, "the restored pane is running")
        check(case.board.holder() == identity(pane) and case.board.rows[ROLE]["runtime"] == "codex",
              f"and holds the role: {case.board.rows}")
        check(case.board.refusals == [], f"the restored pane was not refused: {case.board.refusals}")
    finally:
        presentation_controller.reconnect_role_slots = original
        case.close()


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
            except BaseException as exc:  # noqa: BLE001 - a refusal is a SystemExit; report and keep going
                failures += 1
                print(f"FAILED {name}: {type(exc).__name__}: {exc}", flush=True)
    print(f"role_runtime_handover_test: {CHECKS} checks, {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
