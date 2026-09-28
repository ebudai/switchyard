"""Reading the host's processes, and the runners that read them as a role or the owner.

`_process_snapshot` reads one `ps` snapshot (parents, children, command names
and argv by pid); `process_tree_command_names` names every command in a pane's
process tree; `_list_process_command_lines` lists every process's command line;
`_role_has_pane_process` says whether one of them carries a role's pane marker;
`process_uid` reads the uid a process runs as from the kernel.
`_owner_process_runner` wraps a runner to run as the project owner through
`sudo -u`, and `role_process_runner_for` picks that runner, or the plain one,
for a role's account.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-442), in their original
order. The launcher imports this module and re-exports all seven names; its own
callers and the modules that read them through it -- `launch_phases.py`,
`live_role_runtime.py`, `presentation_controller.py`, `project_stop.py`,
`role_identity_cutover.py`, `role_sessions.py`, `tmux_session_argv.py` and
`worker_pool.py` -- still reach the launcher's names. Everything they read when
they run -- each other, the current user, the role's account, the command-name
and failure-reason helpers, the kernel uid reader and `PROC_ROOT` -- is read
through the launcher, so a suite that rebinds one there still intercepts it.
The definition-time default `subprocess.run` is the same object. This module
imports `team_launcher` only inside the functions, when they run.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def role_process_runner_for(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """A runner for the project account's shared tmux server."""
    from scripts import team_launcher as launcher

    account = launcher.role_run_as_user(config, role)
    if account and launcher.current_user_name() != account:
        return launcher._owner_process_runner(owner_user=account, runner=runner)
    return runner


def _process_snapshot() -> tuple[dict[int, int], dict[int, list[int]], dict[int, set[str]], dict[int, list[str]]]:
    from scripts import team_launcher as launcher

    proc = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm=,args="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return {}, {}, {}, {}
    parents: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    names: dict[int, set[str]] = {}
    argv_by_pid: dict[int, list[str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        parents[pid] = ppid
        argv: list[str]
        try:
            argv = shlex.split(parts[3])
        except ValueError:
            argv = []
        argv_by_pid[pid] = argv
        command_names = {launcher._command_name(parts[2])}
        command_names.update(launcher._command_name(token) for token in argv if launcher._command_name(token))
        names[pid] = command_names
        children.setdefault(ppid, []).append(pid)
    return parents, children, names, argv_by_pid


def process_tree_command_names(pane_pid: int) -> set[str]:
    from scripts import team_launcher as launcher

    if pane_pid <= 0:
        return set()
    _parents_by_pid, children_by_parent, process_names, _argv_by_pid = launcher._process_snapshot()
    stack = [pane_pid]
    seen: set[int] = set()
    names: set[str] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        names.update(process_names.get(pid, set()))
        stack.extend(children_by_parent.get(pid, []))
    return names


def _owner_process_runner(
    *,
    owner_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return runner(["sudo", "-u", owner_user, "-H", *args], **kwargs)

    return wrapped


def _list_process_command_lines(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    from scripts import team_launcher as launcher

    proc = runner(
        ["ps", "-eo", "args=", "--no-headers"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        reason = launcher._proc_failure_reason(proc, f"ps failed with exit {proc.returncode}")
        raise SystemExit(f"switchyard: failed to inspect processes: {reason}")
    return [line for line in str(proc.stdout or "").splitlines() if line.strip()]


def _role_has_pane_process(role: RoleConfig, process_commands: Sequence[str]) -> bool:
    target_marker = f"TICKET_BOARD_PANE_TARGET={role.target}"
    legacy_target_marker = f"PGU_PANE_TARGET={role.target}"
    for command in process_commands:
        try:
            first = shlex.split(command)[0] if command.strip() else ""
        except ValueError:
            first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        if Path(first).name == "tmux":
            continue
        if target_marker in command or legacy_target_marker in command:
            return True
    return False


def process_uid(pid: int, *, proc_root: Path | None = None) -> int | None:
    """The uid a running process is actually executing as, from the kernel."""
    from scripts import team_launcher as launcher

    if pid <= 0:
        return None
    return launcher._proc_effective_uid(proc_root or launcher.PROC_ROOT, str(pid))
