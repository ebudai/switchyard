"""Starting and stopping a role's tmux session.

- `_start_role_session` starts one role's session on its owner's tmux
  server. The pane runs the role's CLI either fresh for each ticket
  (`_uses_fresh_session_per_ticket`) or resuming its recorded provider
  session. Before the session starts it seeds the pane's idle state, clears
  the previous session record, and records a resume that has not yet been
  verified (`record_unverified_resume_for_role`).
- `_start_role_sessions_without_a_window` restarts every role's session with
  no viewer window, as the identities cutover does.
- `stop_role_sessions` stops them.
- `tmux_has_session_by_name_args` and `tmux_kill_session_by_name_args` build
  the argv that asks for a session by exact name, and kills it.

Everything a start needs is read from `scripts/team_launcher.py` when a
function runs: the resume checks and their timeouts, the tmux session argv,
the pane launcher and command, the role's process runner, session records,
paths and idle state. So the suites' patches on the launcher reach it. The
suites also patch `_start_role_sessions_without_a_window` and
`stop_role_sessions` there, and their callers reach them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-312). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def record_unverified_resume_for_role(role: RoleConfig, session_dir: Path, session_id: str) -> None:
    from scripts import team_launcher as launcher

    launcher._write_private_json_atomic(
        session_dir / f"{launcher.session_file_name(role.target)}.resume_timeout",
        {
            "reason": "resume_startup_timeout",
            "session_id": session_id,
            "target": role.target,
            "updated_at": time.time(),
        },
    )


def _uses_fresh_session_per_ticket(role: RoleConfig) -> bool:
    return role.fresh_session_per_ticket


def tmux_has_session_by_name_args(session: str) -> list[str]:
    return ["tmux", "has-session", "-t", session]


def tmux_kill_session_by_name_args(session: str) -> list[str]:
    return ["tmux", "kill-session", "-t", session]


def _start_role_session(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path,
    prefer_resume: bool,
    seed_source: str,
    post_start_verifier: Callable[[], bool] | None = None,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    session_id = launcher.session_id_for_role(role, session_dir) if prefer_resume else ""
    if prefer_resume and not session_id:
        print(
            f"team-launcher: no recorded session id for {role.role}; starting fresh session",
            file=sys.stderr,
        )
        prefer_resume = False
    if prefer_resume and _uses_fresh_session_per_ticket(role):
        print(
            f"team-launcher: {role.role} is configured for fresh sessions per ticket; "
            "starting a fresh session instead of inheriting the previous ticket context",
            file=sys.stderr,
        )
        prefer_resume = False
    if prefer_resume:
        preflight_ok, preflight_message = launcher._resume_preflight_allows_attempt(role, session_id, session_dir=session_dir)
        if not preflight_ok:
            print(preflight_message, file=sys.stderr)
            prefer_resume = False
    if not prefer_resume:
        launcher.clear_session_record_for_role(role, session_dir)
    launcher.prepare_hermes_home_for_role(role, session_dir=session_dir)
    start_proc = runner(
        launcher.tmux_new_session_args(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            resume=prefer_resume,
            bin_user=bin_user,
        )
    )
    if start_proc.returncode != 0:
        return int(start_proc.returncode)
    if not prefer_resume:
        if post_start_verifier is not None and not post_start_verifier():
            launcher.clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
            print(
                f"team-launcher: role {role.role} did not leave a live {role.tmux_session} session",
                file=sys.stderr,
            )
            return 1
        configure_result = launcher.configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        launcher.clear_unverified_resume_for_role(role, session_dir)
        launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    resume_status = launcher._resume_launch_status(role, runner=runner)
    if resume_status == launcher.RESUME_LAUNCH_VERIFIED and (post_start_verifier is None or post_start_verifier()):
        configure_result = launcher.configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        launcher.clear_unverified_resume_for_role(role, session_dir)
        launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    if resume_status == launcher.RESUME_LAUNCH_TIMEOUT:
        configure_result = launcher.configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        record_unverified_resume_for_role(role, session_dir, session_id)
        print(
            f"team-launcher: resume for {role.role} using session {session_id} was not verified within "
            f"{launcher.RESUME_STARTUP_TIMEOUT_SECONDS:g}s; leaving tmux session and session record intact",
            file=sys.stderr,
        )
        return 0
    print(
        f"team-launcher: resume failed for {role.role} using session {session_id}; falling back to fresh session",
        file=sys.stderr,
    )
    if runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        kill_proc = runner(launcher.tmux_kill_session_args(role))
        if kill_proc.returncode != 0:
            return int(kill_proc.returncode)
    launcher.clear_session_record_for_role(role, session_dir)
    launcher.prepare_hermes_home_for_role(role, session_dir=session_dir)
    fresh_proc = runner(
        launcher.tmux_new_session_args(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            resume=False,
            bin_user=bin_user,
        )
    )
    if fresh_proc.returncode != 0:
        return int(fresh_proc.returncode)
    if post_start_verifier is not None and not post_start_verifier():
        launcher.clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
        print(
            f"team-launcher: role {role.role} did not leave a live {role.tmux_session} session after resume fallback",
            file=sys.stderr,
        )
        return 1
    configure_result = launcher.configure_tmux_session_options(role.tmux_session, runner=runner)
    if configure_result != 0:
        return configure_result
    launcher.clear_unverified_resume_for_role(role, session_dir)
    launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=f"{seed_source}.fallback")
    print(
        f"team-launcher: started fresh session for {role.role} after resume fallback",
        file=sys.stderr,
    )
    return 0


def _start_role_sessions_without_a_window(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """Bring each role's session back under its own account, opening no window.

    The display slots already exist and are reconnected in place; relaunching
    the project instead opens a second six-pane window and stacks two status
    bars on the tenant that is mid-cutover (SYRD-45).
    """
    from scripts import team_launcher as launcher

    failures = 0
    for role in config.roles:
        result = runner(
            launcher.pane_command_args(
                config.project,
                role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=launcher.switchyard_pane_launcher_for(config),
                skip_launcher_check=True,
                no_attach=True,
                run_as_user=launcher.role_run_as_user(config, role),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            failures += 1
    return 1 if failures else 0


def stop_role_sessions(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Stop the workers and nothing else, leaving the presentation standing.

    A display slot is a frame around a worker, not the worker: its pane keeps
    running after the session it proxies goes away, says so, and is re-pointed
    when the worker comes back. So a transaction that only needs the workers
    quiescent must stop only the workers. Taking the whole presentation down
    with them destroys the window the tenant was actually looking at, and
    nothing in the restart path opens a replacement -- re-pointing slots that
    no terminal displays leaves every session attached to a viewer nobody can
    see (SYRD-65).
    """
    from scripts import team_launcher as launcher

    exit_code = 0
    for role in config.roles:
        role_runner = launcher.role_process_runner_for(config, role, runner=runner)
        exists = role_runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not exists:
            print_func(f"already stopped {role.role}: {role.tmux_session}")
            continue
        result = role_runner(launcher.tmux_kill_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            reason = launcher._proc_failure_reason(result, f"tmux kill-session failed with exit {result.returncode}")
            print_func(f"failed to stop {role.role}: {role.tmux_session}: {reason}")
            exit_code = exit_code or int(result.returncode)
            continue
        print_func(f"stopped {role.role}: {role.tmux_session}")
    return exit_code
