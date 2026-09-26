"""Entering a role's pane: running it in the foreground or detached, making it visible to the viewer, and attaching it to a slot.

- `run_role_pane` is what a role's pane runs. It starts or resumes the role's
  session in the foreground and verifies the resume.
- `run_detached_role` starts it detached, and waits until it is stable.
- `ensure_visible_role_session_for_viewer` makes sure a role's session exists
  before the viewer shows it.
- `attach_role_to_slot` points a presentation slot at a role.
- `tmux_new_session_args` and `tmux_attach_args` build their tmux argv.
  `_ambient_session_conflict_for_role` refuses to start a role inside another
  role's session. `desktop_reload_is_safe` says whether reloading the desktop
  would interrupt a busy pane.

Resume decisions and verification come from `scripts/provider_resume.py`, and
the default pane-state directory from `scripts/launcher_env.py`; both are
imported directly. Starting sessions, pane commands, visibility records and
the rebound resume timings are read from `scripts/team_launcher.py` when a
function runs. The suites patch the three entry points there, and their
callers reach them there.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-313). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from scripts.launcher_env import DEFAULT_PANE_STATE_DIR
from scripts.provider_resume import (
    RESUME_LAUNCH_VERIFIED,
    _detached_launch_verified,
    _resume_launch_status,
    _resume_launch_verified,
    _resume_preflight_allows_attempt,
    clear_unverified_resume_for_role,
    prepare_hermes_home_for_role,
)

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig, RoleConfig


def _ambient_pane_session_ids(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for key in ("TICKET_BOARD_PANE_SESSION_ID", "PGU_PANE_SESSION_ID"):
        value = str(source.get(key) or "").strip()
        if value:
            result[key] = value
    return result


def _ambient_session_conflict_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    from scripts import team_launcher as launcher

    recorded_session_id = launcher.session_id_for_role(role, session_dir)
    if not recorded_session_id:
        return ""
    conflicts = [
        f"{key}={value}"
        for key, value in _ambient_pane_session_ids(environ).items()
        if value != recorded_session_id
    ]
    if not conflicts:
        return ""
    return (
        f"team-launcher: refusing to launch {role.role}; ambient pane session id "
        f"{', '.join(conflicts)} does not match recorded session {recorded_session_id} "
        f"for target {role.target}. Strip pane identity env or run from outside a pane."
    )


def tmux_new_session_args(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path | None = None,
    resume: bool = False,
    bin_user: str = "",
) -> list[str]:
    from scripts import team_launcher as launcher

    shell_command = launcher._quote_command(
        launcher.cli_command_for_role(role, session_dir=session_dir, pane_state_dir=pane_state_dir, resume=resume, bin_user=bin_user)
    )
    return ["tmux", "new-session", "-d", "-s", role.tmux_session, "-c", role.workdir, "-n", role.role, shell_command]


def tmux_attach_args(role: RoleConfig) -> list[str]:
    return ["tmux", "attach", "-t", role.tmux_session]


def desktop_reload_is_safe(role: RoleConfig, pane_state_dir: Path) -> bool:
    if not role.unset_env:
        return True
    from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore
    gate = PaneActivityGate(state_store=PaneHookStateStore(pane_state_dir))
    if gate.is_busy(role.target):
        print(f"switchyard: {role.target} is busy; wait for an idle checkpoint before reloading desktop environment", file=sys.stderr)
        return False
    return True


def run_role_pane(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    exists = runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return runner(tmux_attach_args(role)).returncode
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not launcher.live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(launcher.tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        start_result = launcher._start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            bin_user=bin_user,
            runner=runner,
        )
        if start_result != 0:
            return start_result
        return runner(tmux_attach_args(role)).returncode
    if mode in {"start", "attach-or-start"}:
        if not exists:
            start_result = launcher._start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _resume_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
            if start_result != 0:
                return start_result
        return runner(tmux_attach_args(role)).returncode
    raise SystemExit(f"unknown pane mode: {mode}")


def run_detached_role(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    exists = runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return 0
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not launcher.live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(launcher.tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        return launcher._start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            post_start_verifier=lambda: _detached_launch_verified(role, runner=runner),
            bin_user=bin_user,
            runner=runner,
        )
    if mode in {"start", "attach-or-start"}:
        if not exists:
            return launcher._start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _detached_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
        return 0
    raise SystemExit(f"unknown detached role mode: {mode}")


def ensure_visible_role_session_for_viewer(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    from scripts import team_launcher as launcher

    exists = runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return 0
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not launcher.live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(launcher.tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        return launcher._start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            bin_user=bin_user,
            runner=runner,
        )
    if mode in {"start", "attach-or-start"}:
        if not exists:
            return launcher._start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _resume_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
        launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        return 0
    raise SystemExit(f"unknown viewer role mode: {mode}")


def attach_role_to_slot(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    slot: int,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    if slot < 0:
        raise SystemExit("team-launcher: attach-role slot must be non-negative")
    slot_count = launcher._layout_slot_count(config)
    if slot >= slot_count:
        raise SystemExit(
            f"team-launcher: cannot attach {role_name} to slot {slot}; layout {config.layout} has {slot_count} slot(s)"
        )
    role = launcher._role_by_name(config, role_name)
    occupant = next(
        (
            candidate
            for candidate in config.roles
            if candidate.role != role.role and not candidate.detached and candidate.slot == slot
        ),
        None,
    )
    if occupant is not None:
        raise SystemExit(
            f"team-launcher: cannot attach {role.role} to slot {slot}; slot {slot} is occupied by {occupant.role}"
        )
    if not role.detached:
        if role.slot == slot:
            print_func(f"team-launcher: role {role.role} is already attached to slot {slot}")
            return 0
        raise SystemExit(f"team-launcher: role {role.role} is already attached to slot {role.slot}")
    visible_count = sum(1 for candidate in config.roles if not candidate.detached)
    if visible_count >= launcher.MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit(
            f"team-launcher: cannot attach {role.role}; at most {launcher.MAX_VISIBLE_PANES_PER_WINDOW} panes "
            "can be visible in one window; detach another role first"
        )
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    exists = runner(launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    session_id = launcher.session_id_for_role(role, session_dir)
    if not exists:
        if not session_id:
            print(f"team-launcher: cannot attach {role.role}; no live session or recorded resume id", file=sys.stderr)
            return 1
        preflight_ok, preflight_message = _resume_preflight_allows_attempt(role, session_id, session_dir=session_dir)
        if not preflight_ok:
            print(preflight_message, file=sys.stderr)
            return 1
    updated_config = launcher._write_role_visibility(
        config,
        config_path=config_path,
        role=role,
        detached=False,
        slot=slot,
        runner=runner,
    )
    updated_role = launcher._role_by_name(updated_config, role.role)
    if not exists:
        prepare_hermes_home_for_role(updated_role, session_dir=session_dir)
        start_proc = runner(
            tmux_new_session_args(updated_role, session_dir=session_dir, pane_state_dir=pane_state_dir, resume=True)
        )
        if start_proc.returncode != 0:
            launcher._write_role_visibility(
                updated_config,
                config_path=config_path,
                role=updated_role,
                detached=True,
                slot=None,
                runner=runner,
            )
            return int(start_proc.returncode)
        resume_status = _resume_launch_status(updated_role, runner=runner)
        if resume_status == RESUME_LAUNCH_VERIFIED:
            clear_unverified_resume_for_role(updated_role, session_dir)
            launcher.seed_initial_pane_idle_state(updated_role, pane_state_dir=pane_state_dir, source="team_launcher.attach_role")
        else:
            launcher._write_role_visibility(
                updated_config,
                config_path=config_path,
                role=updated_role,
                detached=True,
                slot=None,
                runner=runner,
            )
            print(
                f"team-launcher: resume for {updated_role.role} using session {session_id} was not verified; "
                "leaving tmux session and session record intact",
                file=sys.stderr,
            )
            return 1
    print_func(f"team-launcher: attached role {role.role} to slot {slot}; refusing to relayout other panes")
    return runner(tmux_attach_args(updated_role)).returncode
