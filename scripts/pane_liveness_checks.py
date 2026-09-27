"""Whether a role's pane is really running, established from tmux and /proc.

- `PaneLiveness` is one role's answer: live or not, and how that was
  established.
- `process_start_ticks` and `process_owner_uid` read a process's start time and
  owner from /proc.
- `owner_tmux_targets` asks the TENANT OWNER's tmux server which panes it holds,
  as that account, and without prompting when asked non-interactively
  (SYRD-169, SYRD-170).
- `pane_liveness` decides whether a role's pane is live: the owner's tmux holds
  its target and, when the board has an assignment, that assignment names this
  target and a process that is still the one that registered it (SYRD-169).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-364). The launcher
imports this module at its top and re-exports every name, so resume-provision's
readiness check and `project_status`, which reads them through the launcher,
reach the same objects, the class included. The caller's account, every name
defined here that another definition here reads, and the result class are read
from `team_launcher` when they run, as they were, so a patch on the launcher
still intercepts. The standard-library names are this module's own imports, the
same objects. This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


@dataclass(frozen=True)
class PaneLiveness:
    """What root could establish about one role's pane, and how."""

    role: str
    live: bool
    why: str


def process_start_ticks(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    """Field 22 of /proc/<pid>/stat: when this process began, in clock ticks.

    Read from the field after the comm, which is parenthesised and may itself
    contain spaces and parentheses, so the split is on the LAST `)` rather than
    on whitespace.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _pid, _sep, rest = raw.partition(" ")
    tail = rest[rest.rfind(")") + 1 :].split() if ")" in rest else rest.split()
    # After the comm and the state character, field 22 overall is index 19 here.
    try:
        return int(tail[19])
    except (IndexError, ValueError):
        return None


def process_owner_uid(pid: int, *, proc_root: Path = Path("/proc")) -> int | None:
    try:
        return (proc_root / str(pid)).stat().st_uid
    except OSError:
        return None


def owner_tmux_targets(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    timeout_seconds: float | None = None,
    interactive: bool = True,
) -> tuple[set[str], str]:
    """Every pane the TENANT OWNER's tmux server holds, asked as that account.

    Root has its own tmux server and it is not the one the project runs in, so
    asking tmux as root answers about the wrong server -- confidently, and with
    an empty list. The question is dispatched to the owner the way every other
    owner-side step here is dispatched (SYRD-169).

    `switchyard list` asks the same question about several tenants in a row, as
    whoever happened to type it, so it asks with `interactive=False` and a
    deadline: `sudo -n` fails instead of prompting for a password nobody is
    there to type, and a tenant whose tmux server does not answer becomes one
    unknown row rather than a hung listing (SYRD-170).
    """
    from scripts import team_launcher as launcher

    args = ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"]
    if launcher.current_user_name() != config.run_as_user:
        args = ["sudo", *([] if interactive else ["-n"]), "-u", config.run_as_user, "-H", *args]
    try:
        done = runner(
            args, capture_output=True, text=True, check=False,
            **({"timeout": timeout_seconds} if timeout_seconds else {}),
        )
    except subprocess.TimeoutExpired:
        return set(), (
            f"the owner's tmux server did not answer within {timeout_seconds:g}s"
        )
    except OSError as exc:
        return set(), f"the owner's tmux server could not be asked: {exc}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        return set(), (
            "the owner's tmux server could not be asked: "
            + (detail[-1] if detail else f"exit {done.returncode}")
        )
    return {line.strip() for line in (done.stdout or "").splitlines() if line.strip()}, ""


def pane_liveness(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    tmux_targets: set[str],
    assignments: dict[str, dict],
    owner_uid: int | None,
    proc_root: Path = Path("/proc"),
) -> PaneLiveness:
    """Whether this role's pane is really running, without reading its argv.

    The old proof searched `ps -eo args` for `TICKET_BOARD_PANE_TARGET=<target>`.
    That marker is in the argv of the ENV WRAPPER that started the pane, and a
    long-running CLI has exec'd past it -- so six live panes, with six live tmux
    sessions and six process-bound board registrations naming their original
    pids, were reported absent (journal 0074).

    What is durable instead: the owner's own tmux server holds the target, and
    -- when the board has an assignment for the role -- that assignment names
    THIS target and a process that is still the one that registered it, checked
    by pid, start time and uid against /proc rather than by trusting the row.

    Fail-closed in every direction that matters. No tmux target is not live. An
    assignment for another target, a pid that is gone, a pid that has been
    reused (start time differs), or a process running as somebody other than the
    tenant owner all mean not live, even though a tmux pane exists -- because
    then the board's record and the machine disagree, and a recovery must not
    call that finished. A role with NO assignment yet is live on the tmux
    evidence alone: that is a pane which has started and not registered, which
    is exactly what the registration wait after this exists to find out about.
    """
    from scripts import team_launcher as launcher

    if role.target not in tmux_targets:
        return launcher.PaneLiveness(role.role, False, f"no pane {role.target} in {config.run_as_user}'s tmux server")
    assignment = assignments.get(role.role)
    if not isinstance(assignment, dict):
        return launcher.PaneLiveness(role.role, True, f"tmux holds {role.target}; the board has no assignment yet")
    recorded_target = str(assignment.get("actual_target") or "").strip()
    if recorded_target != role.target:
        return launcher.PaneLiveness(
            role.role, False,
            f"the board assigns {role.role} to {recorded_target or 'nothing'}, not {role.target}",
        )
    try:
        pid = int(assignment.get("process_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return launcher.PaneLiveness(role.role, False, f"the board's assignment for {role.role} names no process")
    started = launcher.process_start_ticks(pid, proc_root=proc_root)
    if started is None:
        return launcher.PaneLiveness(role.role, False, f"the process {pid} the board assigned {role.role} is gone")
    recorded_start = assignment.get("process_start_time")
    if recorded_start is not None and int(recorded_start) != started:
        return launcher.PaneLiveness(
            role.role, False,
            f"process {pid} started at {started}, not {int(recorded_start)}: the pid has been reused",
        )
    actual_uid = launcher.process_owner_uid(pid, proc_root=proc_root)
    expected_uid = owner_uid if owner_uid is not None else assignment.get("process_uid")
    if expected_uid is not None and actual_uid is not None and int(expected_uid) != int(actual_uid):
        return launcher.PaneLiveness(
            role.role, False,
            f"process {pid} runs as uid {actual_uid}, not {config.run_as_user}'s {int(expected_uid)}",
        )
    return launcher.PaneLiveness(role.role, True, f"tmux holds {role.target} and pid {pid} still registered it")
