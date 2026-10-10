"""Handing a role's board registration from a stopped pane to its replacement (SYRD-559).

A runtime switch stops the role's pane and starts a new one. On a
process-authority board the new pane registers before its CLI starts, and the
board refuses that registration while the role's recorded holder is still a
live process (`server.handle_register_caller`). `tmux kill-session` returns
when tmux has hung the pane up, not when its process has gone, so starting the
replacement straight away loses that race every time.

- `RuntimeHandover` holds the bounds and the seams: how the board's runtime
  assignments, a process and a signal are read and sent.
- `board_holder` and `pane_holders` name who holds the role before the stop;
  `await_holders_gone` waits for them, then sends SIGTERM and SIGKILL, and
  raises -- having started nothing -- if one still stays.
- `await_registration` proves a started pane is up from the board's own row
  naming it, not from a tmux session that existed for a moment.

`scripts/role_runtime.py` drives these on the forward switch and on rollback.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from scripts import team_launcher
from scripts.role_command import role_registers_runtime
from scripts.ticket_board.peer_identity import ProcessInfo, read_process
from scripts.tmux_session_argv import expected_live_commands, tmux_current_command_args


def session_is_live(
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    return runner(
        team_launcher.tmux_has_session_args(role),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


#: A pane process, as the board identifies one: (pid, start time). The start
#: time is what keeps a reused pid from being taken for the process it replaced.
Holder = tuple[int, int]


@dataclass(frozen=True)
class RuntimeHandover:
    """How a replaced pane hands the role's board registration to its successor.

    `tmux kill-session` returns as soon as tmux has hung the pane up, not when
    the pane's process has gone. The board refuses a registration while the
    role's recorded holder is still a live process (`server.handle_register_
    caller`), so a replacement started straight after the kill registers
    against the process it was meant to replace, is refused, and exits -- after
    the start has already looked successful. Otto lost `main` and `uiux` this
    way, each time a fresh pane gone about 47 ms later (SYRD-559).

    So the stopped pane's process is waited for, within these bounds, before
    anything is started; one that will not go is sent SIGTERM and then SIGKILL,
    and one that outlives even that stops the switch before any replacement
    exists. After the start, success is the board's own row naming the new
    pane, not a tmux session that existed for a moment.
    """

    #: The board's runtime assignments, (rows by role, problem). None reads
    #: them through the launcher's own reader, from the tenant's board socket.
    read_assignments: Callable[[Any], tuple[dict[str, dict], str]] | None = None
    read_process: Callable[[int], ProcessInfo | None] = read_process
    #: Signals one holder. None signals through a pidfd opened and then
    #: re-identified by start time, so a pid reused since cannot be hit.
    signal_process: Callable[[int, int], None] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    #: Measured on otto: about three seconds for a CLI to leave after the
    #: hang-up. Five times that before it is asked more firmly.
    exit_timeout_seconds: float = 15.0
    term_grace_seconds: float = 5.0
    kill_grace_seconds: float = 3.0
    registration_timeout_seconds: float = 30.0
    poll_seconds: float = 0.1
    #: How long a started worker is watched before the switch calls it running.
    #: A provider that rejects its own arguments exits within about a second of
    #: registering (otto's Hermes, SYRD-560); a start observed only at its first
    #: instant cannot tell that from a worker that came up (SYRD-560).
    settle_seconds: float = 5.0

    def assignments(self, config: team_launcher.ProjectConfig) -> tuple[dict[str, dict], str]:
        reader = self.read_assignments or team_launcher.read_runtime_assignment_details
        return reader(config)

    def live(self, holder: Holder) -> bool:
        info = self.read_process(holder[0])
        return info is not None and info.start_time == holder[1]


def row_holder(row: Mapping[str, Any] | None) -> Holder | None:
    try:
        pid, started = int((row or {})["process_pid"]), int((row or {})["process_start_time"])
    except (KeyError, TypeError, ValueError):
        return None
    return (pid, started) if pid > 0 else None


def board_holder(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    handover: RuntimeHandover,
) -> Holder | None:
    """The process the board records as holding the role, when it can say.

    Read before anything changes the board's workflow: `/api/runtime-
    assignments` hides a row whose runtime the workflow no longer declares,
    while the registration check still counts it. An unreadable board is no
    holder here; the pane's own process, from tmux, is still waited for.
    """
    if not role_registers_runtime(role):
        return None
    try:
        rows, _problem = handover.assignments(config)
    except Exception:  # noqa: BLE001 - the tmux holder is still known
        return None
    return row_holder(rows.get(role.role))


def pane_holders(
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
    recorded: Holder | None = None,
) -> set[Holder]:
    """Every live process that holds, or may hold, the role's registration.

    The pane's root process is what a pane registers as (`peer_identity.
    session_identity`), so it is the holder whether or not the board could be
    read; the board's own record is added because that, not tmux, is what the
    refusal is decided against.
    """
    holders: set[Holder] = set()
    pid = team_launcher.pane_pid_for_role(role, runner=runner)
    if pid > 0:
        info = handover.read_process(pid)
        if info is not None:
            holders.add((pid, info.start_time))
    if recorded is not None and handover.live(recorded):
        holders.add(recorded)
    return holders


def signal_holder(holder: Holder, sig: int, handover: RuntimeHandover) -> None:
    if handover.signal_process is not None:
        if handover.live(holder):
            handover.signal_process(holder[0], sig)
        return
    # Opened first and identified second: a pidfd refers to the process that
    # had the pid when it was opened, so once its start time is confirmed the
    # signal cannot reach a newer process that happens to reuse the pid.
    try:
        fd = os.pidfd_open(holder[0])
    except ProcessLookupError:
        return
    try:
        if handover.live(holder):
            signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def await_holders_gone(
    holders: set[Holder],
    *,
    role: team_launcher.RoleConfig,
    replacement: str,
    handover: RuntimeHandover,
    print_func: Callable[[str], None],
) -> None:
    """Return once no holder is alive; raise, having started nothing, if one stays."""
    stages = (
        (None, handover.exit_timeout_seconds),
        (signal.SIGTERM, handover.term_grace_seconds),
        (signal.SIGKILL, handover.kill_grace_seconds),
    )
    announced = False
    problems: list[str] = []
    for sig, grace in stages:
        remaining = sorted(holder for holder in holders if handover.live(holder))
        if not remaining:
            return
        pids = ", ".join(str(pid) for pid, _started in remaining)
        if sig is None:
            if not announced:
                announced = True
                print_func(
                    f"switchyard: waiting up to {handover.exit_timeout_seconds:g}s for {role.tmux_session}'s "
                    f"stopped pane (pid {pids}) to exit before {replacement} registers for {role.role}"
                )
        else:
            print_func(
                f"switchyard: {role.tmux_session}'s stopped pane (pid {pids}) is still running; "
                f"sending {signal.Signals(sig).name}"
            )
            for holder in remaining:
                try:
                    signal_holder(holder, sig, handover)
                except OSError as exc:
                    problems.append(f"{signal.Signals(sig).name} to pid {holder[0]}: {exc}")
        deadline = handover.monotonic() + max(0.0, grace)
        while any(handover.live(holder) for holder in remaining) and handover.monotonic() < deadline:
            handover.sleep(handover.poll_seconds)
    remaining = sorted(holder for holder in holders if handover.live(holder))
    if not remaining:
        return
    waited = handover.exit_timeout_seconds + handover.term_grace_seconds + handover.kill_grace_seconds
    detail = f" ({'; '.join(problems)})" if problems else ""
    raise RuntimeError(
        f"{role.tmux_session}'s stopped pane process (pid "
        + ", ".join(str(pid) for pid, _started in remaining)
        + f") did not exit within {waited:g}s, even after SIGTERM and SIGKILL{detail}; it still holds "
        f"{role.role}'s board registration, so {replacement} was not started -- the board would "
        "refuse it and the pane would close"
    )


def await_registration(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
) -> None:
    """Return once the board's row for the role names the pane just started.

    The row has to name a live process, and that process has to be this
    session's pane: a row left by a process that has gone, or held by some
    other process, is not this pane's registration. Every replaced holder is
    already gone (`await_holders_gone`), so a row still naming one fails the
    liveness test. A session that has closed is the pane's own registrar
    having been refused, so that is said at once rather than after the full
    bound.
    """
    runtime = team_launcher._role_cli_name(role)
    deadline = handover.monotonic() + max(0.0, handover.registration_timeout_seconds)
    while True:
        last = registration_problem(config, role, runner=runner, handover=handover)
        if not last:
            return
        if not session_is_live(role, runner=runner):
            raise RuntimeError(
                f"the {runtime} pane for {role.role} closed before the board recorded its registration "
                f"({last}); its registrar prints why as `switchyard: runtime registration refused: ...`"
            )
        if handover.monotonic() >= deadline:
            raise RuntimeError(
                f"{role.role} started under {runtime} but did not register with the board within "
                f"{handover.registration_timeout_seconds:g}s ({last})"
            )
        handover.sleep(handover.poll_seconds)


def registration_problem(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
) -> str:
    """Why the board's row for the role is not this session's live pane, or ""."""
    rows, problem = handover.assignments(config)
    holder = row_holder(rows.get(role.role))
    if problem:
        return problem
    if holder is None:
        return "the board has no runtime assignment for it"
    if not handover.live(holder):
        return f"the process the board names (pid {holder[0]}) is no longer running"
    root = team_launcher.pane_pid_for_role(role, runner=runner)
    if root <= 0 or root == holder[0]:
        return ""
    return f"the board names pid {holder[0]}, but {role.tmux_session}'s pane is pid {root}"


def _liveness_problem(role: team_launcher.RoleConfig, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    """The direct observation: the session closed, or tmux kept a dead pane; "" while it runs."""
    runtime = team_launcher._role_cli_name(role)
    if not session_is_live(role, runner=runner):
        return f"the {runtime} pane for {role.role} exited and {role.tmux_session} closed"
    dead = runner(["tmux", "display-message", "-p", "-t", f"={role.tmux_session}:", "#{pane_dead}"],
                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if dead.returncode == 0 and str(dead.stdout or "").strip() == "1":
        return f"the {runtime} pane for {role.role} exited; {role.tmux_session} shows a dead pane"
    return ""


def _identity_problem(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
) -> str:
    """What the live pane runs and holds: its provider, readable, and its board registration."""
    runtime = team_launcher._role_cli_name(role)
    pane_pid = team_launcher.pane_pid_for_role(role, runner=runner)
    if pane_pid > 0:
        names = team_launcher.process_tree_command_names(pane_pid)
        if not names:
            return f"the processes in {role.tmux_session}'s pane (pid {pane_pid}) could not be read"
    else:
        # No pid from tmux: its own reading of the pane's command, as the
        # launcher's start check (`live_command_matches_role`) falls back to.
        shown = runner(tmux_current_command_args(role),
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        command = team_launcher._command_name(str(shown.stdout or "").strip()) if shown.returncode == 0 else ""
        if not command:
            return f"tmux reported neither a process nor a command for {role.tmux_session}, so its worker could not be read"
        names = {command}
    expected = expected_live_commands(role)
    if not names & expected:
        from scripts.ticket_board.runtime_catalog import RUNTIMES

        providers = sorted(names & {choice.value for choice in RUNTIMES})
        running = ", ".join(providers) if providers else "no supported provider"
        return f"{role.tmux_session} runs {running}, not {runtime}"
    if role_registers_runtime(role):
        return registration_problem(config, role, runner=runner, handover=handover)
    return ""


def worker_problem(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
) -> str:
    """Why the role's worker is not its declared provider, running and registered, or "".

    Each answer is a distinct finding: a session that has gone, a pane whose
    process has exited, a process table that could not be read -- "cannot
    say", never "fine" -- a pane running some other program, and a lost board
    registration.

    The readings are taken one after another, and a provider can exit between
    them: the pane read alive, and then its processes or its registration read
    only the consequence (SYRD-560 Audit). So when the identity readings find
    anything wrong, liveness is read again, and a worker that has died since is
    reported as what was directly seen -- its pane -- rather than as a symptom.
    """
    gone = _liveness_problem(role, runner=runner)
    if gone:
        return gone
    problem = _identity_problem(config, role, runner=runner, handover=handover)
    if problem:
        return _liveness_problem(role, runner=runner) or problem
    return ""


def provider_command(config: team_launcher.ProjectConfig, role: team_launcher.RoleConfig) -> str:
    """The provider command the pane runs, to start by hand and read its own error."""
    try:
        argv = team_launcher.cli_command_for_role(role, session_dir=team_launcher.role_session_dir(config, role))
        if "--" in argv:
            argv = argv[argv.index("--") + 1:]
        else:
            first = Path(str(role.cli[0])).name
            argv = argv[next(i for i, part in enumerate(argv) if Path(str(part)).name == first):]
    except Exception:  # noqa: BLE001 - the configured CLI is still a useful pointer
        argv = list(role.cli)
    return shlex.join(str(part) for part in argv)


def prove_worker(
    config: team_launcher.ProjectConfig,
    role: team_launcher.RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    handover: RuntimeHandover,
) -> None:
    """Watch the started worker through its settle window; raise on the first thing wrong."""
    deadline = handover.monotonic() + max(0.0, handover.settle_seconds)
    while True:
        try:
            problem = worker_problem(config, role, runner=runner, handover=handover)
        except Exception as exc:  # noqa: BLE001 - an observation that fails is a finding
            problem = f"its worker could not be observed ({type(exc).__name__}: {exc})"
        if problem:
            raise RuntimeError(
                f"{problem}. To see why, run its command in {role.workdir} as the project owner: "
                f"{provider_command(config, role)}"
            )
        if handover.monotonic() >= deadline:
            return
        handover.sleep(handover.poll_seconds)


def slot_binding_problem(
    config: team_launcher.ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    slots: tuple[int, ...],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """Why a reconnected slot does not show the role's live worker, as `present list` reads it, or ""."""
    if not slots:
        return ""
    from scripts import presentation_controller

    try:
        report = presentation_controller.presentation_report(config, config_path=config_path, runner=runner)
    except Exception as exc:  # noqa: BLE001
        return f"the presentation could not be read ({type(exc).__name__}: {exc})"
    by_slot = {int(item["slot"]): item for item in report.get("slots") or []}
    problems: list[str] = []
    failed: list[int] = []
    for slot in slots:
        item = by_slot.get(int(slot))
        worker = (item or {}).get("worker") or {}
        if item is not None and item.get("actual_role") == role_name and item.get("client_state") == "connected" \
                and worker.get("state") == "live":
            continue
        failed.append(int(slot))
        if item is None:
            problems.append(f"slot {slot} is not in the presentation")
        elif item.get("actual_role") != role_name or item.get("client_state") != "connected":
            problems.append(f"slot {slot} shows {item.get('actual_role') or 'nothing'} "
                            f"({item.get('client_state')}), not {role_name}")
        elif worker.get("state") != "live":
            problems.append(f"slot {slot} shows {role_name} with worker={worker.get('state') or 'unknown'}")
    if not problems:
        return ""
    remedies = "; ".join(f"`switchyard present {config.project} show {role_name} --slot {slot}`" for slot in failed)
    return ("; ".join(problems) + f". `switchyard present {config.project} list` shows the slots, and "
            f"{remedies} puts {role_name} back")
