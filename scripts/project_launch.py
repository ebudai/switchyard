"""Start or reload a project's panes, through the launch phases.

`launch_project` checks the running board's process authority, migrates the
director onboarding and the generated layout, prepares the desktop and the
pane launcher, then runs the launch phases -- runners and paths, preparation,
layout and plan, workers and presentation, and the report -- returning the
first answer that stops it. The phases themselves live in
`scripts/launch_phases.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-429). The launcher
imports this module and re-exports it; `main` and `switchyard_main` still
dispatch to the launcher's name, and `scripts/new_project_phases.py` and
`scripts/resume_provision_command.py` still read it there. Everything it reads
-- the phases, the worker-startup type, the config loader, the board-authority,
onboarding, layout-upgrade, desktop and pane-launcher checks -- is read through
the launcher at call time, so a suite that rebinds one there still intercepts
it. The definition-time defaults -- the session-record timeout and poll, the
layout mode and `subprocess.run` -- are imported from the very modules the
launcher imports them from, so they are the same objects; the config type is
imported under TYPE_CHECKING. This module imports `team_launcher` only inside
the function, when it runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from scripts.layout_modes import LAYOUT_MODE_AUTO
from scripts.session_records import LAUNCH_SESSION_RECORD_POLL_SECONDS, LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def launch_project(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    dry_run: bool = False,
    layout_output: Path | None = None,
    assign_layout_owner: bool | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    allow_stale_launcher: bool = False,
    no_launcher_self_deploy: bool = False,
    report_session_records: bool = False,
    #: Where the tenant's provider state lives, when the caller knows it. The
    #: reconciliation below compares each running role's runtime against it
    #: (SYRD-191).
    owner_home: Path | None = None,
    session_record_timeout: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    session_record_poll: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    layout_mode: str = LAYOUT_MODE_AUTO,
    layout_environ: dict[str, str] | None = None,
    konsole_process_launcher: Callable[..., Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import team_launcher as launcher

    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    if not dry_run and config.role_state_isolation:
        compatible, reason = launcher.process_authority_board_compatibility(config)
        if not compatible:
            print_func(
                f"team-launcher: refusing to launch {config.project} before changing local state: "
                f"its running board does not provide project-account process authority ({reason})"
            )
            return 1
    if mode == "reload" and not dry_run:
        # Reload re-projects the stored document, so an unmigrated tenant would reload
        # into the same missing director onboarding forever. Run the one-time backfill
        # first; it is idempotent and marked, so a migrated tenant pays nothing.
        if launcher.migrate_declarative_director_onboarding(
            config, config_path=config_path, print_func=print_func
        ):
            # The migration rewrote the projection on disk, so the config loaded before
            # it is now stale. Every later path -- session sync, detached restarts, the
            # viewer -- reads role env off this object, and would otherwise restart roles
            # without the prompt that was just projected.
            config = launcher.load_project_config(config.project, config_path)
    launch_setup = launcher._launch_runners_and_paths(
        config,
        config_path=config_path,
        runner=runner,
        pane_state_dir=pane_state_dir,
        layout_output=layout_output,
        assign_layout_owner=assign_layout_owner,
        script_path=script_path,
    )
    worktree_runner = launch_setup.worktree_runner
    role_process_runner = launch_setup.role_process_runner
    delegate_role_sessions_to_owner = launch_setup.delegate_role_sessions_to_owner
    effective_pane_state_dir = launch_setup.effective_pane_state_dir
    output_path = launch_setup.output_path
    window_title = launch_setup.window_title
    should_assign_layout_owner = launch_setup.should_assign_layout_owner
    pane_script_path = launch_setup.pane_script_path
    if not dry_run:
        upgrade_result = launcher.upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
        if upgrade_result.changed:
            print_func(upgrade_result.message)
            config = launcher.load_project_config(config.project, config_path)
    if not dry_run and mode != "attach":
        config = launcher.prepare_project_desktop(config, runner=runner)
    if not dry_run:
        pane_script_path = launcher._verify_pane_launcher_path(config, script_path=script_path, runner=worktree_runner)
    launch_preparation = launcher._prepare_launch(
        config,
        allow_stale_launcher=allow_stale_launcher,
        config_path=config_path,
        dry_run=dry_run,
        effective_pane_state_dir=effective_pane_state_dir,
        mode=mode,
        no_launcher_self_deploy=no_launcher_self_deploy,
        owner_home=owner_home,
        pane_script_path=pane_script_path,
        print_func=print_func,
        runner=runner,
        worktree_runner=worktree_runner,
    )
    if launch_preparation.exit_code is not None:
        return launch_preparation.exit_code
    config = launch_preparation.config
    failed_roles = launch_preparation.failed_roles
    running_roles = launch_preparation.running_roles
    reconcile_home = launch_preparation.reconcile_home
    unreconciled_roles = launch_preparation.unreconciled_roles
    layout_exit = launcher._write_layout_and_plan(
        config,
        config_path=config_path,
        dry_run=dry_run,
        failed_roles=failed_roles,
        force_reload=force_reload,
        layout_environ=layout_environ,
        layout_mode=layout_mode,
        mode=mode,
        output_path=output_path,
        pane_script_path=pane_script_path,
        pane_state_dir=pane_state_dir,
        runner=runner,
        should_assign_layout_owner=should_assign_layout_owner,
        window_title=window_title,
    )
    if layout_exit is not None:
        return layout_exit
    worker_startup = launcher._start_workers_and_present(
        config,
        allow_stale_launcher=allow_stale_launcher,
        config_path=config_path,
        delegate_role_sessions_to_owner=delegate_role_sessions_to_owner,
        effective_pane_state_dir=effective_pane_state_dir,
        failed_roles=failed_roles,
        force_reload=force_reload,
        konsole_process_launcher=konsole_process_launcher,
        layout_environ=layout_environ,
        layout_mode=layout_mode,
        layout_output=layout_output,
        mode=mode,
        output_path=output_path,
        pane_script_path=pane_script_path,
        print_func=print_func,
        role_process_runner=role_process_runner,
        runner=runner,
        window_title=window_title,
    )
    if not isinstance(worker_startup, launcher.WorkerStartup):
        return worker_startup
    worker_start_exit_code = worker_startup.worker_start_exit_code
    launch_started_at = worker_startup.launch_started_at
    launch_started_ns = worker_startup.launch_started_ns
    resolved_layout_mode = worker_startup.resolved_layout_mode
    # A window opened by an earlier release can still be running as root, and
    # the tenant cannot signal it. Saying the project is attached while that is
    # true would be the wrong report to act on (SYRD-43).
    return launcher._report_launch(
        config,
        config_path=config_path,
        effective_pane_state_dir=effective_pane_state_dir,
        failed_roles=failed_roles,
        launch_started_at=launch_started_at,
        launch_started_ns=launch_started_ns,
        mode=mode,
        print_func=print_func,
        reconcile_home=reconcile_home,
        report_session_records=report_session_records,
        resolved_layout_mode=resolved_layout_mode,
        running_roles=running_roles,
        session_record_poll=session_record_poll,
        session_record_timeout=session_record_timeout,
        unreconciled_roles=unreconciled_roles,
        worker_start_exit_code=worker_start_exit_code,
    )
