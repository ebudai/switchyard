"""The tmux argv a role's session is driven with, the command its pane runs, and whether a live pane is still running it.

- **Session argv:** `tmux_has_session_args`, `tmux_kill_session_args` and
  `tmux_current_command_args` ask a role's tmux server about its session,
  kill it, or read what its pane is running.
- **Pane commands:** `pane_command_args` and `pane_command` build what a
  role's pane runs -- the pane launcher wrapping the role's CLI, as its
  owner.
- **Live matching:** `live_command_matches_role` decides whether the process
  tree under a role's pane is still running what the role should run
  (`expected_live_commands`, `process_tree_contains_command`).

Reading a pane's pid and a process tree, quoting and naming commands, and the
current user stay in `scripts/team_launcher.py`. This module reads them from
there when a function runs. The suites patch the session argv builders on the
launcher, and every caller -- here, in the launcher and in the modules
already moved out -- reaches them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-314). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import RoleConfig


def tmux_has_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "has-session", "-t", role.tmux_session]


def tmux_kill_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "kill-session", "-t", role.tmux_session]


def tmux_current_command_args(role: RoleConfig) -> list[str]:
    return ["tmux", "display-message", "-p", "-t", role.target, "#{pane_current_command}"]


def expected_live_commands(role: RoleConfig) -> set[str]:
    from scripts import team_launcher as launcher

    configured = role.live_commands or [launcher._command_name(role.cli[0])]
    return {launcher._command_name(command) for command in configured if launcher._command_name(command)}


def process_tree_contains_command(pane_pid: int, expected_commands: set[str]) -> bool:
    from scripts import team_launcher as launcher

    return bool(launcher.process_tree_command_names(pane_pid) & expected_commands)


def live_command_matches_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    from scripts import team_launcher as launcher

    expected = expected_live_commands(role)
    pane_pid = launcher.pane_pid_for_role(role, runner=runner)
    if pane_pid > 0:
        return process_tree_contains_command(pane_pid, expected)

    proc = runner(tmux_current_command_args(role), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return False
    actual = launcher._command_name(str(proc.stdout).strip())
    return actual in expected


def pane_command_args(
    project: str,
    role: RoleConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    slot: int | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    skip_launcher_check: bool = False,
    allow_stale_launcher: bool = False,
    no_attach: bool = False,
    run_as_user: str = "",
) -> list[str]:
    from scripts import team_launcher as launcher

    args = [
        str(script_path),
        project,
        "pane",
        mode,
        role.role,
        "--config",
        str(config_path),
    ]
    if mode == "reload" and force_reload:
        args.append("--force")
    if slot is not None:
        args.extend(["--slot", str(slot)])
    if skip_launcher_check:
        args.append("--skip-launcher-check")
    if allow_stale_launcher:
        args.append("--allow-stale-launcher")
    if no_attach:
        args.append("--no-attach")
    if pane_state_dir is not None:
        args.extend(["--pane-state-dir", str(pane_state_dir)])
    if run_as_user and launcher.current_user_name() != run_as_user:
        args = ["sudo", "-u", run_as_user, "-H", *args]
    return args


def pane_command(
    project: str,
    role: RoleConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    slot: int | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    skip_launcher_check: bool = False,
    allow_stale_launcher: bool = False,
    no_attach: bool = False,
    run_as_user: str = "",
) -> str:
    from scripts import team_launcher as launcher

    args = pane_command_args(
        project,
        role,
        config_path=config_path,
        mode=mode,
        script_path=script_path,
        slot=slot,
        pane_state_dir=pane_state_dir,
        force_reload=force_reload,
        skip_launcher_check=skip_launcher_check,
        allow_stale_launcher=allow_stale_launcher,
        no_attach=no_attach,
        run_as_user=run_as_user,
    )
    return launcher._quote_command(args)
