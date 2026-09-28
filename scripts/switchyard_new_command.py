"""`switchyard new`: gather a new project's answers and run the provisioning phases, in order.

`switchyard_new_command` is the front door's orchestration: it resolves the
choices (P0), checks the host (P1), prepares the accounts (P2) and the board
(P3), runs the provider sign-in (P4) and launches the panes (P5), handing each
phase what the earlier ones settled and returning a phase's status as it came.
The phases themselves live in `scripts/new_project_phases.py`.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-425). The launcher
imports this module and re-exports the command; `switchyard_main` still
dispatches `new` to the launcher's name. The six phases and their two
continuation types are read through the launcher at call time, so a suite that
rebinds one there still intercepts it. The definition-time defaults -- the port
and socket probes, the no-runner sentinel, the session-record timeout and poll,
the layout mode, `os.geteuid`, `input` and `print` -- are imported from the very
modules the launcher imports them from, so they are the same objects; the
sentinel's type is imported under TYPE_CHECKING. This module imports
`team_launcher` only inside the command, when it runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from scripts.first_run_auth import NO_RUNNER_INJECTED
from scripts.layout_modes import LAYOUT_MODE_AUTO
from scripts.new_project_precheck import _path_exists, _tcp_port_in_use
from scripts.session_records import LAUNCH_SESSION_RECORD_POLL_SECONDS, LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from scripts.first_run_auth import _NoRunnerInjected


def switchyard_new_command(
    *,
    slug: str | None = None,
    agent_name: str | None = None,
    project_name: str | None = None,
    project_path: Path | None = None,
    from_artifact: Path | None = None,
    source_repo: Path | None = None,
    workflow_config: Path | None = None,
    commit_git_dir: str | None = None,
    output_dir: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    role_clis: Sequence[tuple[str, str]] | None = None,
    yes: bool = False,
    desktop_policy: Path | None = None,
    headless: bool = False,
    desktop_gui_user: str | None = None,
    desktop_approval_settings_path: Path | None = None,
    allow_existing_owner_user: bool = False,
    agy_credential_source: str | None = None,
    no_agy_credential: bool = False,
    agy_credential_settings_path: Path | None = None,
    home_base: Path = Path("/home"),
    euid_getter: Callable[[], int] = os.geteuid,
    #: The sentinel, not `subprocess.run`: injecting nothing has to stay
    #: distinguishable from injecting the default, because that is what
    #: decides whether the person gets a watched setup window (SYRD-221).
    runner: Callable[..., subprocess.CompletedProcess[Any]] | _NoRunnerInjected = NO_RUNNER_INJECTED,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    session_dir_exists: Callable[[Path], bool] | None = None,
    pane_state_dir: Path | None = None,
    session_record_timeout: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    session_record_poll: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    layout_mode: str = LAYOUT_MODE_AUTO,
    layout_environ: dict[str, str] | None = None,
    konsole_process_launcher: Callable[..., Any] | None = None,
    git_init: bool = True,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    #: Whether there is somebody to ask. `None` reads the terminal, which is
    #: right in production and unanswerable in a test with no tty.
    interactive: bool | None = None,
    agent_cli_policy: str = "",
    agent_cli_sources: Sequence[str] | None = None,
) -> int:
    from scripts import team_launcher as launcher

    new_project_choices = launcher._resolve_new_project_choices(
        slug=slug,
        agent_name=agent_name,
        project_name=project_name,
        project_path=project_path,
        from_artifact=from_artifact,
        role_clis=role_clis,
        yes=yes,
        desktop_policy=desktop_policy,
        headless=headless,
        desktop_gui_user=desktop_gui_user,
        desktop_approval_settings_path=desktop_approval_settings_path,
        allow_existing_owner_user=allow_existing_owner_user,
        agy_credential_source=agy_credential_source,
        no_agy_credential=no_agy_credential,
        agy_credential_settings_path=agy_credential_settings_path,
        home_base=home_base,
        euid_getter=euid_getter,
        runner=runner,
        config_dir=config_dir,
        registry_dir=registry_dir,
        input_func=input_func,
        print_func=print_func,
    )
    agy_source_origin = new_project_choices.agy_source_origin
    artifact_path = new_project_choices.artifact_path
    design_document = new_project_choices.design_document
    director_onboarding = new_project_choices.director_onboarding
    first_run_runner = new_project_choices.first_run_runner
    include_audit = new_project_choices.include_audit
    include_designer = new_project_choices.include_designer
    owner_shell = new_project_choices.owner_shell
    owner_user = new_project_choices.owner_user
    project_dir = new_project_choices.project_dir
    resolved_agy_credential_source = new_project_choices.resolved_agy_credential_source
    resolved_project_name = new_project_choices.resolved_project_name
    resolved_slug = new_project_choices.resolved_slug
    runner = new_project_choices.runner
    selected_audit_roles = new_project_choices.selected_audit_roles
    selected_desktop_policy = new_project_choices.selected_desktop_policy
    selected_implementer_roles = new_project_choices.selected_implementer_roles
    selected_role_clis = new_project_choices.selected_role_clis
    selected_role_efforts = new_project_choices.selected_role_efforts
    selected_role_models = new_project_choices.selected_role_models
    stages = new_project_choices.stages
    new_project_preflight = launcher._check_new_project_preflight(
        from_artifact=from_artifact,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        port=port,
        database=database,
        yes=yes,
        home_base=home_base,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=config_dir,
        registry_dir=registry_dir,
        input_func=input_func,
        print_func=print_func,
        agent_cli_policy=agent_cli_policy,
        agent_cli_sources=agent_cli_sources,
        include_audit=include_audit,
        include_designer=include_designer,
        owner_user=owner_user,
        project_dir=project_dir,
        resolved_agy_credential_source=resolved_agy_credential_source,
        resolved_project_name=resolved_project_name,
        resolved_slug=resolved_slug,
        runner=runner,
        selected_audit_roles=selected_audit_roles,
        selected_implementer_roles=selected_implementer_roles,
        selected_role_clis=selected_role_clis,
        stages=stages,
    )
    effective_source_repo = new_project_preflight.effective_source_repo
    precheck_plan = new_project_preflight.precheck_plan
    selected_role_clis = new_project_preflight.selected_role_clis
    worktree_branch = new_project_preflight.worktree_branch
    new_project_accounts = launcher._prepare_new_project_accounts(
        from_artifact=from_artifact,
        output_dir=output_dir,
        home_base=home_base,
        git_init=git_init,
        print_func=print_func,
        agy_source_origin=agy_source_origin,
        artifact_path=artifact_path,
        design_document=design_document,
        director_onboarding=director_onboarding,
        include_audit=include_audit,
        include_designer=include_designer,
        owner_shell=owner_shell,
        owner_user=owner_user,
        project_dir=project_dir,
        resolved_agy_credential_source=resolved_agy_credential_source,
        resolved_project_name=resolved_project_name,
        resolved_slug=resolved_slug,
        runner=runner,
        selected_audit_roles=selected_audit_roles,
        selected_desktop_policy=selected_desktop_policy,
        selected_implementer_roles=selected_implementer_roles,
        selected_role_efforts=selected_role_efforts,
        selected_role_models=selected_role_models,
        stages=stages,
        effective_source_repo=effective_source_repo,
        precheck_plan=precheck_plan,
        selected_role_clis=selected_role_clis,
        worktree_branch=worktree_branch,
    )
    provision_dir = new_project_accounts.provision_dir
    new_project_board = launcher._prepare_new_project_board(
        source_repo=source_repo,
        workflow_config=workflow_config,
        commit_git_dir=commit_git_dir,
        port=port,
        database=database,
        home_base=home_base,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        registry_dir=registry_dir,
        print_func=print_func,
        artifact_path=artifact_path,
        director_onboarding=director_onboarding,
        owner_user=owner_user,
        project_dir=project_dir,
        resolved_slug=resolved_slug,
        runner=runner,
        selected_role_efforts=selected_role_efforts,
        selected_role_models=selected_role_models,
        stages=stages,
        provision_dir=provision_dir,
    )
    if not isinstance(new_project_board, launcher.NewProjectBoard):
        return new_project_board
    config = new_project_board.config
    config_path = new_project_board.config_path
    new_project_sign_in = launcher._run_new_project_sign_in(
        home_base=home_base,
        euid_getter=euid_getter,
        input_func=input_func,
        print_func=print_func,
        interactive=interactive,
        first_run_runner=first_run_runner,
        owner_user=owner_user,
        project_dir=project_dir,
        resolved_slug=resolved_slug,
        runner=runner,
        stages=stages,
        config=config,
        config_path=config_path,
    )
    if not isinstance(new_project_sign_in, launcher.NewProjectSignIn):
        return new_project_sign_in
    config = new_project_sign_in.config
    first_run_auth_report = new_project_sign_in.first_run_auth_report
    launch_deferred = new_project_sign_in.launch_deferred
    launch_runner = new_project_sign_in.launch_runner
    return launcher._launch_new_project_panes(
        home_base=home_base,
        pane_state_dir=pane_state_dir,
        session_record_timeout=session_record_timeout,
        session_record_poll=session_record_poll,
        layout_mode=layout_mode,
        layout_environ=layout_environ,
        konsole_process_launcher=konsole_process_launcher,
        print_func=print_func,
        include_designer=include_designer,
        owner_user=owner_user,
        resolved_slug=resolved_slug,
        runner=runner,
        stages=stages,
        config_path=config_path,
        config=config,
        first_run_auth_report=first_run_auth_report,
        launch_deferred=launch_deferred,
        launch_runner=launch_runner,
    )
