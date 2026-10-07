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
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from scripts import team_launcher
from scripts.role_command import role_registers_runtime
from scripts.ticket_board.peer_identity import ProcessInfo, read_process


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
    last = "the board has no runtime assignment for it"
    while True:
        rows, problem = handover.assignments(config)
        row = rows.get(role.role)
        holder = row_holder(row)
        if problem:
            last = problem
        elif holder is None:
            last = "the board has no runtime assignment for it"
        elif not handover.live(holder):
            last = f"the process the board names (pid {holder[0]}) is no longer running"
        else:
            root = team_launcher.pane_pid_for_role(role, runner=runner)
            if root <= 0 or root == holder[0]:
                return
            last = f"the board names pid {holder[0]}, but {role.tmux_session}'s pane is pid {root}"
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
