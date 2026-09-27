"""JSON-driven project team launcher for tmux-backed CLI panes."""

from __future__ import annotations

import argparse
import copy
import errno
import grp
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import shutil
import signal
import select
import shlex
import struct
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
import tomllib
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

from scripts.ticket_board import runtime_catalog
from scripts.ticket_board import terminal_select
from scripts.ticket_board.prompt_schema import (
    KIND_MULTI,
    KIND_SINGLE,
    Choice,
    Field,
    Schema,
    review_lines,
    with_existing_value,
)
from scripts.ticket_board.project_provision import (
    DEFAULT_PRIVILEGED_PROVISION_ROOT,
    DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    DEFAULT_PG_IDENT_MAP,
    ProjectBoardProvision,
    ROLE_RE,
    NON_PROCESS_ROLES,
    TENANT_ARTIFACT_NAMES,
    build_plan,
    migrate_plan_document,
    plan_field_names,
    privileged_artifact_names,
    privileged_provision_dir,
    render_add_role_sql,
    render_board_unit,
    render_canary_unit,
    render_vcs_close_role_sql,
    role_account_migration_name,
    ROLE_STAGED_EXECUTABLES,
    role_tooling_staging_commands,
    role_tooling_staging_dir,
    staged_role_tooling_problems,
    untrusted_root_executable_reasons,
    display_attach_helper_path,
    invoking_human,
    resolve_control_user,
    sql_identifier,
    tenant_control_helper_path,
    validate_ticket_prefix,
    write_artifacts,
)
# One bound on a stored role prompt, in the module that owns it. A pool's
# declared prompt becomes exactly that, so restating the limit here would let a
# pool declare something the document refuses at apply time (SYRD-37).
from scripts.ticket_board.workflow_config import ONBOARDING_PROMPT_MAX_CHARS
from scripts.ticket_board.codex_hook_trust import (
    codex_command_hook_trust_entries as _codex_command_hook_trust_entries,
    codex_hook_current_hash as _codex_hook_current_hash,
    codex_hook_event_key as _codex_hook_event_key,
    codex_hook_timeout as _codex_hook_timeout,
    codex_trusted_hashes as _codex_trusted_hashes,
)
from scripts.ticket_board.commit_repos import commit_git_dir_env_for_project
# The worker-pool declaration and verb (SYRD-37), moved out whole (SYRD-286).
# Named here because `ProjectConfig`, `load_project_config` and
# `switchyard_main` use them, and because `team_launcher.<name>` is how callers
# and tests have always reached them.
from scripts.worker_pool_command import (
    WORKER_POOL_ACTIONS,
    WORKER_POOL_MAX_SIZE,
    WORKER_POOL_MEMBER_SEPARATOR,
    WORKER_POOL_ONBOARDING_PROMPT_MAX_CHARS,
    WorkerPool,
    WorkerPoolFinding,
    _build_switchyard_worker_pool_parser,
    format_worker_pool_preflight,
    parse_worker_pool,
    switchyard_worker_pool_command,
    worker_pool_member_role,
    worker_pool_preflight,
)
# Agent credential sourcing and role seeding (SYRD-287), moved out whole.
# Named here because the launcher's own `new`, provider-state and dispatch code
# use them, and because `team_launcher.<name>` is how callers and tests have
# always reached them.
from scripts.role_credentials import (
    AGY_CREDENTIAL_DIR_NAME,
    AGY_CREDENTIAL_TOKEN_NAME,
    HERMES_OWNER_CREDENTIAL_DIR,
    HERMES_PROVIDER_ENV_KEYS,
    ROLE_CREDENTIAL_ARTIFACTS,
    RoleCredentialArtifact,
    _require_owner_home_traversable,
    _require_owner_traversable,
    _role_credential_target,
    hermes_credential_target,
    role_credential_artifacts,
    role_credential_manifest,
    seed_role_credential,
    select_hermes_provider_env,
    switchyard_seed_role_credentials_command,
)
from scripts.agy_credential import (
    AGY_CREDENTIAL_ABSENT,
    AGY_CREDENTIAL_INSTALLED,
    AGY_CREDENTIAL_UNUSABLE,
    AGY_SOURCE_FROM_HOST,
    AGY_SOURCE_FROM_OPT_OUT,
    AGY_SOURCE_FROM_OVERRIDE,
    AGY_SOURCE_UNSET,
    DEFAULT_AGY_CREDENTIAL_SETTING_PATH,
    _agy_credential_state,
    _open_owner_credential_dir,
    _resolve_agy_credential_source,
    _seed_agy_credential_for_owner,
    _validate_agy_credential_source,
    read_host_agy_credential_source,
    switchyard_agy_credential_command,
    write_host_agy_credential_source,
)
# A dependency-free host lookup (SYRD-288), imported rather than defined here
# because `scripts.upstream_report` binds it as a default argument at import.
from scripts.host_accounts import home_dir_for_user, local_account_exists, uid_for_user
# The upstream report link and its credential (SYRD-288), moved out whole.
# Named here because `upgrade_project_command` calls record/refresh -- and the
# suites patch them here -- and because `team_launcher.<name>` is how callers
# and tests have always reached them.
from scripts.upstream_report import (
    UPSTREAM_REPORT_CREDENTIAL_NAME,
    UPSTREAM_REPORT_TOKEN_KEY,
    _board_env_report_token,
    _write_owner_private_file,
    record_upstream_report_link,
    refresh_upstream_report_credential,
    upstream_report_board,
    upstream_report_credential_path,
)
# Onboarding documents, director onboarding and the generated board skill
# (SYRD-289), moved out whole. Named here because `new`, `upgrade`,
# `finish-upgrade` and launch call them -- and the suites patch some of them
# here -- and because `team_launcher.<name>` is how callers and tests have
# always reached them.
from scripts.project_onboarding import (
    BOARD_SKILL_INSTALLER_NAME,
    BOARD_SKILL_NAME,
    ONBOARDING_SNAPSHOT_RE,
    SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME,
    SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME,
    SWITCHYARD_ONBOARDING_DOC_NAMES,
    DirectorSeedResult,
    _install_switchyard_onboarding_docs,
    _stamp_onboarding_doc,
    _switchyard_source_commit,
    _write_switchyard_onboarding_files,
    board_skill_installer_path,
    director_onboarding_seed_text,
    director_onboarding_state,
    ensure_generated_project_board_skill,
    install_generated_project_board_skill_args,
    migrate_declarative_director_onboarding,
    seed_director_onboarding,
    upgrade_switchyard_onboarding_docs,
)
# A role's CLI command, and checking its model (SYRD-290), moved out whole.
# Named here because pane launch, config loading, `new`, first-run auth and
# role-runtime changes call them, and because `team_launcher.<name>` is how
# callers and tests have always reached them.
from scripts.role_command import (
    DEFAULT_MODEL_ARG_BY_CLI,
    DEFAULT_RESUME_FLAG_BY_CLI,
    DEFAULT_RESUME_MODE_BY_CLI,
    DEFAULT_RESUME_SUBCOMMAND_BY_CLI,
    EFFORT_STYLE_BY_CLI,
    STARTUP_ARGS_BY_CLI,
    YOLO_ARGS_BY_CLI,
    cli_command_for_role,
    effort_args_for_role,
    hermes_env_for_role,
    startup_args_for_role,
    yolo_args_for_role,
)
from scripts.model_validation import (
    MODEL_PROBE_EVIDENCE_CHARS,
    MODEL_PROBE_FILENAME,
    MODEL_PROBE_NO_TOOL_CALL_REASON,
    MODEL_PROBE_TOOL_CALL_ATTEMPTS,
    MODEL_VALIDATION_PROMPT,
    ModelProbeAttempt,
    ModelValidationFailure,
    _effort_field,
    _model_failure_suggestion,
    _model_field,
    _model_probe_called_a_tool,
    _model_probe_evidence,
    _model_validation_command,
    _model_validation_passed,
    _ModelProbeWorkspace,
    confirm_unknown_models_with_owner,
    record_role_model,
    report_models_were_not_probed,
    stop_before_launch_for_unknown_models,
    validate_role_models,
)
# A project's worktrees and control repository (SYRD-291), moved out whole.
# Named here because config loading, launch, first-run auth and add-role call
# them -- and the suites patch `_control_repository_owner_home` and
# `ensure_project_worktrees` here -- and because `team_launcher.<name>` is how
# callers and tests have always reached them.
from scripts.project_worktrees import (
    CONTROL_REPOSITORY_EMPTY,
    CONTROL_REPOSITORY_MISSING,
    CONTROL_REPOSITORY_OCCUPIED,
    CONTROL_REPOSITORY_READY,
    CONTROL_REPOSITORY_UNREADABLE,
    WorktreeProvisionResult,
    _config_git_owner_rules,
    _control_repository_boundary_error,
    _control_repository_owner_home,
    _prepare_project_worktrees_for_launch,
    chown_control_repository_args,
    control_repository_refspec,
    control_repository_state,
    ensure_control_repository,
    ensure_control_role_worktrees,
    ensure_project_worktrees,
    fetch_project_worktree_ref,
    git_checkout_shared_ref_args,
    git_clean_role_worktree_args,
    git_clean_role_worktree_dry_run_args,
    git_clean_shared_checkout_args,
    git_clean_shared_checkout_dry_run_args,
    git_clone_control_repository_args,
    git_control_fetch_refspec_args,
    git_control_remote_rename_args,
    git_control_worktree_add_args,
    git_fetch_control_ref_args,
    git_fetch_worktree_ref_args,
    git_role_worktree_check_args,
    git_role_worktree_reset_args,
    git_role_worktree_status_porcelain_args,
    git_shared_checkout_check_args,
    git_shared_checkout_status_porcelain_args,
    mkdir_p_args,
    repair_control_repository_ownership,
    warn_before_role_worktree_refresh,
    warn_before_shared_checkout_refresh,
)
# Keeping a launcher checkout current (SYRD-292), moved out whole. Named here
# because launch, `main` (deploy), upgrade and the release-status formatter
# call them -- and the suites patch `ensure_launcher_checkout_current` here --
# and because `team_launcher.<name>` is how callers and tests reach them.
from scripts.launcher_checkout import (
    ALLOW_STALE_LAUNCHER_ENV,
    LauncherCheckoutProbe,
    LEGACY_ALLOW_STALE_LAUNCHER_ENV,
    _format_behind_count,
    deploy_launcher_checkout,
    ensure_launcher_checkout_current,
    git_checkout_launcher_branch_args,
    git_clean_launcher_checkout_args,
    git_fast_forward_launcher_ref_args,
    git_fetch_launcher_ref_args,
    git_launcher_ahead_behind_args,
    git_launcher_checkout_check_args,
    git_launcher_commit_exists_args,
    git_launcher_current_branch_args,
    git_launcher_head_args,
    git_launcher_head_short_args,
    git_launcher_ls_remote_ref_args,
    git_launcher_status_porcelain_args,
    launcher_checkout_status,
    probe_checkout_against_worktree_ref,
    probe_launcher_checkout,
    warn_if_artifact_source_checkout_is_stale,
)
# Owner-correct git execution and project git helpers (SYRD-293), moved out
# whole. `run_owner_correct_git` stays reachable -- and patchable -- here: the
# other git modules and owner_git's own helpers call it through the launcher.
# Named here too because `new`, launch, add-role, deploy and config loading call
# them, and because `team_launcher.<name>` is how callers and tests reach them.
from scripts.owner_git import (
    GitOwnerRule,
    _commit_project_git_changes,
    _ensure_project_git_repository,
    _git_owner_failure,
    _git_owner_for_target,
    _git_status_porcelain,
    _git_target_path_from_args,
    _owner_project_git_runner,
    _path_is_under,
    _path_owner_user,
    _require_existing_project_git_repository,
    run_owner_correct_git,
    _run_owner_git,
)
# Pane hooks and Codex hook trust (SYRD-294), moved out whole. Named here
# because launch, upgrade and first-run setup call them -- and the suites patch
# `ensure_generated_project_pane_hooks` and `refresh_role_pane_hooks` here --
# and because `team_launcher.<name>` is how callers and tests reach them.
from scripts.pane_hooks import (
    CodexHookTrustMismatch,
    ensure_generated_project_pane_hooks,
    _format_codex_hook_trust_report,
    install_generated_project_pane_hooks_args,
    refresh_role_pane_hooks,
    stale_codex_hook_trust_for_roles,
    tenant_hook_accounts,
)
# Agent CLI discovery and host-wide promotion (SYRD-295), moved out whole.
# Named here because `new`, launch, registration and upgrade call them, and
# because `team_launcher.<name>` is how callers and tests have always reached
# them. The vendor install table and its text formatters stay in this file.
from scripts.agent_cli_discovery import (
    AGENT_CLI_SCOPE_ABSENT,
    AGENT_CLI_SCOPE_CALLER_ONLY,
    AGENT_CLI_SCOPE_HOST_WIDE,
    AgentCliAvailability,
    AgentCliUnavailable,
    CALLER_LOCAL_BIN_GLOBS,
    CALLER_LOCAL_BIN_SUBDIRS,
    CALLER_PROCESS_TREE_HOPS,
    InvokingAccount,
    account_can_execute,
    agent_cli_binary,
    agent_cli_scope_explanation,
    caller_aware_which,
    caller_command_search_path,
    caller_executable,
    classify_agent_cli,
    classify_selected_agent_clis,
    invoking_account,
)
from scripts.agent_cli_promotion import (
    AGENT_CLI_HOST_WIDE_BIN,
    AGENT_CLI_POLICIES,
    AGENT_CLI_POLICY_PROMOTE_LOCAL,
    AGENT_CLI_POLICY_REQUIRE_HOST_WIDE,
    AGENT_CLI_PROMOTER_NAME,
    AGENT_CLI_PROMOTION_LABEL,
    AGENT_CLI_SCRIPT_SAMPLE_BYTES,
    AgentCliSourceRejected,
    OwnerCliVerification,
    agent_cli_detected_path_problems,
    agent_cli_promoter_path,
    agent_cli_source_is_self_contained,
    agent_cli_unreachable_dependencies,
    _configured_agent_clis,
    offer_host_wide_promotion_before_launch,
    _parse_agent_cli_sources,
    promote_agent_cli_host_wide,
    promote_agent_cli_through_sudo,
    refresh_registered_agent_clis,
    registered_tenant_agent_clis,
    require_agent_clis_for_new_tenant,
    resolvable_agent_cli_promotions,
    resolve_agent_cli_source,
    verify_agent_clis_for_owner,
)
# First-run setup manifest and workdir trust (SYRD-296), moved out whole.
# Named here because the first-run auth phase (still in this file) builds and
# prints the manifest -- and the suites patch `_workdir_is_trusted` here -- and
# because `team_launcher.<name>` is how callers and tests have always reached them.
from scripts.first_run_setup import (
    FIRST_RUN_TRUST_CLIS,
    FirstRunAuthLoginStep,
    FirstRunFolderTrustStep,
    FirstRunProviderSetupStep,
    FirstRunSetupManifest,
    build_first_run_setup_manifest,
    _format_first_run_setup_manifest,
    _git_common_dir_for,
    print_first_run_setup_manifest,
    _role_names,
    _workdir_is_trusted,
)
# Provider auth-status probing (SYRD-297), moved out whole. Every name is kept
# here: the suites patch several of them on the launcher, and the launcher's
# own first-run and launch code, and the modules already moved out, reach
# them as `team_launcher.<name>`.
from scripts.provider_auth_status import (
    FIRST_RUN_AUTH_STATUS_COMMANDS,
    OWNER_CLI_PROBE_TIMEOUT_SECONDS,
    PROBE_TIMED_OUT_STATUS,
    _claude_account_setup_complete,
    _cli_auth_probe_passed,
    _cli_auth_status,
    _owner_cli_is_installed,
    _owner_home_for_auth,
    _provider_account_setup_complete,
    _run_owner_cli_probe,
)
# Pure provider screen classifiers (SYRD-298), moved out whole. Named here
# because `team_launcher.<name>` is how tests reach them; the foreground
# first-run session imports them itself (scripts/provider_session.py).
from scripts.provider_screen import (
    PROVIDER_PENDING_ANSWER_MARKERS,
    PROVIDER_SELECTION_CURSORS,
    PROVIDER_WAITING_ON_SIGN_IN_MARKERS,
    _draws_something,
    provider_is_waiting_for_an_answer,
    _provider_screen_offers_a_choice,
    _replaced_frame_starts_at,
    _screen_is_settled,
    _TerminalStream,
    _visible_text,
)
# The foreground first-run session and terminal ownership (SYRD-299), moved
# out whole. Named here because the auth phase below still starts sessions
# through them and defaults to their timeout and purposes, and because
# `team_launcher.<name>` is how tests reach them.
from scripts.provider_session import (
    FOREGROUND_COMPLETION_POLL_SECONDS,
    FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
    PROVIDER_READY_QUIET_SECONDS,
    SETUP_PURPOSE_FIRST_RUN,
    SETUP_PURPOSE_FOLDER_TRUST,
    SETUP_PURPOSE_SIGN_IN,
    SETUP_STEP_CLEAR_SCREEN,
    SETUP_WINDOW_TITLE_DONE,
    PtyForegroundSession,
    _countdown,
    _RawTerminal,
    _run_provider_first_run,
    run_provider_first_run_session,
    set_terminal_title,
    _TerminalModeLedger,
)
# The first-run authentication phase and its report (SYRD-300), moved out
# whole. Named here because `switchyard new`, `switchyard <slug>` and
# `validate-models` below run the phase, read its report and default to its
# runner sentinel -- one object, so both defaults mean the same thing -- and
# because `team_launcher.<name>` is how tests and the modules already moved out
# reach them. The suites patch the phase, the warning report and
# `_run_owner_cli_until` here, and the moved code calls them through here.
from scripts.first_run_auth import (
    NO_RUNNER_INJECTED,
    FirstRunAuthReport,
    foreground_runner_for,
    _NoRunnerInjected,
    OwnerShellIssue,
    report_first_run_auth_warnings,
    _run_owner_cli_until,
    run_first_run_auth_phase,
    stop_before_launch_for_unauthenticated_providers,
)
# `switchyard status` and `switchyard release-status` (SYRD-301), moved out
# whole. Named here because `switchyard_main` below dispatches to them -- a
# suite patches `switchyard_status_command` here and that dispatch is what it
# reaches -- and because `team_launcher.<name>` is how tests reach them.
from scripts.project_status import (
    SwitchyardProjectStatus,
    _build_switchyard_release_status_parser,
    _build_switchyard_status_parser,
    close_release_phase,
    format_release_alignment,
    root_recorded_tenant_facts,
    _runtime_release_copy_status,
    switchyard_project_statuses,
    switchyard_release_status_command,
    switchyard_runtime_copy_statuses,
    switchyard_status_command,
)
# Board and listener service control (SYRD-302), moved out whole. Named here
# because suspending, resuming, cutting over, upgrading and creating a tenant
# below call them, and because `team_launcher.<name>` is how tests reach them.
# The suites patch `capture_listener_state`, `board_system_unit_is_active`,
# `_ensure_board_service_peer_auth` and (by rebinding)
# `OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS` here; the moved code reads them
# through here as well.
from scripts.board_services import (
    _board_system_unit,
    _board_system_unit_action,
    _canary_system_unit,
    _default_board_service_user,
    _ensure_board_service_peer_auth,
    _ensure_board_service_user,
    _listener_user_unit,
    _non_login_shell_path,
    activate_board_authority,
    authority_unit_installs,
    board_service_user,
    board_system_unit_is_active,
    capture_listener_state,
    managed_unit_names,
    MANAGER_RESPONDING,
    MANAGER_WEDGED,
    OWNER_USER_MANAGER_RESTART_TIMEOUT_SECONDS,
    owner_user_manager_state,
    repair_owner_user_manager,
    restore_installed_units,
    start_owner_listener,
    stop_owner_listener,
)
# Workflow adoption, migration and the root-vouched handoff (SYRD-303), moved
# out whole. Named here because `switchyard_main` dispatches to the verbs,
# `finish-upgrade` below installs a handed-off workflow -- the suites patch
# `install_handed_off_workflow` here, and that call is what they reach -- and
# because `team_launcher.<name>` is how tests reach them.
from scripts.workflow_adoption import (
    _build_switchyard_adopt_workflow_parser,
    _build_switchyard_migrate_workflow_parser,
    effective_workflow_document,
    install_handed_off_workflow,
    plan_workflow_migration,
    propose_legacy_workflow_adoption,
    publish_workflow_handoff,
    read_workflow_handoff,
    switchyard_adopt_workflow_command,
    switchyard_migrate_workflow_command,
    workflow_handoff_path,
    WorkflowMigration,
    write_workflow_record,
)
# Rebinding panes to a migrated workflow (SYRD-304), moved out whole. Named
# here because `switchyard_main` dispatches to the verb and because
# `team_launcher.<name>` is how tests reach them.
from scripts.pane_rebind import (
    _build_switchyard_rebind_workflow_panes_parser,
    _reconciled_presentation_section,
    root_verified_tenant,
    switchyard_rebind_workflow_panes_command,
)
# Role-account migration (SYRD-305), moved out whole. Named here because
# `switchyard new`, `upgrade` and the identities cutover below publish,
# verify, instruct and run it, and because `team_launcher.<name>` is how tests
# reach them.
from scripts.role_account_migration import (
    account_existence_guard,
    director_control_access_commands_for,
    _privileged_directory_is_closed,
    publish_role_account_migration,
    remove_untrusted_role_account_migration,
    render_role_account_migration,
    role_account_migration_instruction,
    role_path_access_commands,
    trusted_role_account_migration_path,
    upgrade_role_accounts_in_config,
)
# The privilege drop (SYRD-306), a leaf: several modules take
# `_drop_to_account` as a default argument, so there is one object, and
# `_report_new_project_to_caller` below runs as the caller through it.
from scripts.account_drop import _drop_to_account, _run_as_account
# Provider-state generation and runtime-registration reads (SYRD-306), moved
# out whole. Named here because launching, recovery, resume and the identities
# cutover below read them -- a suite patches `await_runtime_registration`
# here, and the recovery check that calls it is what it reaches -- because
# `presentation_controller` and `project_status` read two of them through
# the launcher, and because `team_launcher.<name>` is how tests reach them.
from scripts.provider_runtime_state import (
    await_runtime_registration,
    provider_state_generation,
    _provider_state_record_path,
    provider_state_store_problem,
    read_runtime_assignment_details,
    record_provider_state_generation,
    recorded_provider_state_generation,
    RUNTIME_REGISTRATION_POLL_SECONDS,
    RUNTIME_REGISTRATION_TIMEOUT_SECONDS,
    RuntimeRegistrationWait,
    unreadable_provider_state_roles,
)
# The default release ref (SYRD-308), a leaf: the release functions below and
# the identities cutover both take it as a default argument, so it is one object.
from scripts.release_refs import DEFAULT_TENANT_RELEASE_DEPLOY_REF
# The role-identity cutover (SYRD-308), moved out whole. Named here because
# `upgrade`, `finish-upgrade` and interrupted-state recovery below call it --
# the suites patch `cutover_role_identities_command` and
# `_interrupted_provider_state_roles` here, and those calls are what they
# reach -- and because `team_launcher.<name>` is how tests reach them.
from scripts.role_identity_cutover import (
    canonical_role_identities,
    cutover_role_identities_command,
    _finish_interrupted_provider_state,
    _interrupted_provider_state_roles,
    repatriate_role_runtime_state,
    revert_incomplete_role_account_cutover,
    role_account_cutover,
    running_role_identities,
    _worktree_ownership,
)
# The tmux viewer (SYRD-309), moved out whole. Named here because launching a
# project and starting a role's session below build and configure it, and
# because `presentation_controller`, `switchyard-viewer-layout` and the tests
# reach these as `team_launcher.<name>`.
from scripts.tmux_viewer import (
    configure_tmux_session_options,
    DEFAULT_VIEWER_COLUMNS,
    DEFAULT_VIEWER_ROWS,
    install_viewer_relayout_hook,
    launch_tmux_viewer_session,
    tmux_set_history_limit_args,
    tmux_set_mouse_args,
    tmux_viewer_observer_hook_args,
    tmux_viewer_pin_size_args,
    tmux_viewer_relayout_hook_args,
    tmux_viewer_select_layout_args,
    tmux_viewer_set_titles_args,
    tmux_viewer_set_titles_string_args,
    tmux_viewer_unpin_size_args,
    viewer_grid,
    viewer_layout_helper_path,
    viewer_layout_string,
    VIEWER_RELAYOUT_UNAVAILABLE_NOTE,
)
# Session records and seeding (SYRD-310), moved out whole. Named here because
# launching, starting a role's session and resume preflight below read them --
# a suite patches `report_launch_session_records` here, and the launch calls it
# by this name -- because `role_credentials`, `role_identity_cutover`,
# `presentation_controller`, `role_command` and `role_runtime` read four of
# them through the launcher, and because `team_launcher.<name>` is how tests
# reach them.
from scripts.session_records import (
    clear_session_record_for_role,
    LAUNCH_SESSION_RECORD_POLL_SECONDS,
    launch_session_record_statuses,
    LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    LaunchSessionRecordStatus,
    pane_launch_outcome_source_for_role,
    pane_runtime_hook_source_for_role,
    pane_state_file_name,
    report_launch_session_records,
    seed_default_session_dir_from_legacy_sources,
    seed_session_dir_from_legacy_sources,
    session_file_name,
    session_id_for_role,
    _session_payload_model_for_role,
    _session_record_for_role,
    superseded_session_id_for_role,
)
# Session and pane-state paths and the initial pane idle state (SYRD-311),
# moved out whole. Named here because launching, config loading and the role
# session code below resolve them -- the suites patch `account_session_dir`,
# `role_session_dir` and `seed_initial_pane_idle_state` here -- because
# several modules read them through the launcher, and because
# `team_launcher.<name>` is how tests reach them. `DEFAULT_SESSION_DIR` and
# `DEFAULT_PANE_STATE_DIR` stay defined in this file: the suites rebind them
# here, and the moved code reads them here.
from scripts.session_paths import (
    account_session_dir,
    clear_pane_idle_state_for_role,
    default_pane_state_dir_for_user,
    default_session_dir_for_user,
    role_pane_state_dir,
    role_session_dir,
    seed_initial_pane_idle_state,
    session_dir_uses_user_runtime,
    shared_pane_state_dir,
)
# Starting and stopping role sessions (SYRD-312), moved out whole. Named here
# because launching, the viewer and stopping a project below call them -- the
# suites patch `_start_role_sessions_without_a_window` and
# `stop_role_sessions` here -- because `role_command`, `tmux_viewer` and
# `role_identity_cutover` read five of them through the launcher, and because
# `team_launcher.<name>` is how tests reach them.
from scripts.role_sessions import (
    record_unverified_resume_for_role,
    _start_role_session,
    _start_role_sessions_without_a_window,
    stop_role_sessions,
    tmux_has_session_by_name_args,
    tmux_kill_session_by_name_args,
    _uses_fresh_session_per_ticket,
)
# Environment lookups (SYRD-313), a leaf: the pane entry points below and in
# scripts/role_pane_entry.py take `DEFAULT_PANE_STATE_DIR` as a default
# argument, so it is one object; the suites rebind both names here, and
# `session_paths` reads them here.
from scripts.launcher_env import (
    DEFAULT_PANE_STATE_DIR,
    _env_first,
)
# Provider resume stores, Hermes homes and resume verification (SYRD-313),
# moved out whole. Named here because `role_sessions`, `role_command` and
# `role_identity_cutover` read them through the launcher, and because
# `team_launcher.<name>` is how tests reach them.
from scripts.provider_resume import (
    _claude_project_dir_for_workdir,
    clear_unverified_resume_for_role,
    CODEX_SESSIONS_DIR_NAME,
    hermes_home_for_role,
    HERMES_PRIVATE_HOME_ENTRIES,
    HERMES_SHARED_HOME_ENTRIES,
    _home_from_session_dir,
    prepare_hermes_home_for_role,
    _resume_launch_status,
    RESUME_LAUNCH_TIMEOUT,
    RESUME_LAUNCH_VERIFIED,
    _resume_preflight_allows_attempt,
    _uses_hermes,
)
# Role pane entry (SYRD-313), moved out whole. Named here because `main` and
# `launch_project` below run it -- the suites patch `run_role_pane`,
# `run_detached_role` and `ensure_visible_role_session_for_viewer` here -- and
# because `presentation_controller`, `role_runtime` and `role_sessions` read
# it through the launcher.
from scripts.role_pane_entry import (
    attach_role_to_slot,
    ensure_visible_role_session_for_viewer,
    run_detached_role,
    run_role_pane,
    tmux_new_session_args,
)
# The tmux session argv, pane commands and live pane-command matching
# (SYRD-314), moved out whole. Named here because launching, the role session
# code and the modules already moved out read them through the launcher -- the
# suites patch `tmux_has_session_args` and `tmux_kill_session_args` here -- and
# because `team_launcher.<name>` is how tests reach them.
from scripts.tmux_session_argv import (
    live_command_matches_role,
    pane_command,
    pane_command_args,
    tmux_has_session_args,
    tmux_kill_session_args,
)
from scripts.layout_modes import (
    LAYOUT_MODE_AUTO,
    LAYOUT_MODE_CHOICES,
    LAYOUT_MODE_SEPARATE,
    LAYOUT_MODE_VIEWER,
)
from scripts.presentation_reconnect import (
    PRESENTATION_HANDOFF_FD_ENV,
    hand_presentation_back_to_the_caller,
    presentation_is_attached,
    presentation_slot_titles,
    reconnect_presentation,
    render_presentation_handoff,
)
from scripts.desktop_layout_writer import (
    _open_owned_directory_chain,
    _write_crossing_desktop_layout,
    write_desktop_layout,
)
from scripts.desktop_presentation import (
    PRESENTATION_HANDOFF_SCHEMA,
    PRESENTATION_TERMINALS,
    PRESENTATION_TITLE_MAX_LENGTH,
    PRESENTATION_TITLE_REJECTED,
    TERMINAL_RETURNS,
    TERMINAL_STAYS,
    available_presentation_terminal,
    complete_desktop_presentation,
    launch_presentation_terminal,
    missing_terminal_refusal,
    presentation_handoff_path,
    presentation_pane_program_problem,
    presentation_title_problem,
    _registered_project_name,
    _tenant_has_desktop_access,
    terminal_launch_args,
    validated_presentation_handoff,
)
from scripts.gui_window_launch import (
    FALLBACK_XDG_CONFIG_DIRS,
    GUI_ENVIRONMENT_ALLOWLIST,
    GUI_WAYLAND_ENV,
    HOST_WAYLAND_ENV,
    KONSOLE_DEFAULTS_NAME,
    KONSOLE_WINDOW_TITLE_DEFAULTS,
    LEGACY_GUI_WAYLAND_ENV,
    LEGACY_HOST_WAYLAND_ENV,
    gui_environment_args,
    _gui_launch_prefix,
    gui_privilege_drop_args,
    gui_program_path,
    _gui_runtime_dir,
    konsole_launch_args,
    launch_konsole_window,
    _make_konsole_log_readable,
    normalize_wayland_display,
    _print_konsole_early_exit,
    _refusal_command,
    write_konsole_config_defaults,
)
from scripts.presentation_layout_files import (
    _KONSOLE_SAFE_CHARACTER,
    _KONSOLE_SAFE_WORD,
    _konsole_command,
    _konsole_quote,
    chown_layout_output_args,
    default_layout_output_path,
    desktop_layout_destination_problem,
    desktop_presentation_layout_path,
    desktop_state_dir,
    ensure_layout_output_owner,
    failed_role_command,
    inert_pane_command,
    materialize_layout,
    pane_split_title,
    role_display_name,
)
from scripts.desktop_detection import (
    GUI_USER_ENV,
    LEGACY_GUI_USER_ENV,
    default_gui_user,
    _desktop_from_loginctl_output,
    _desktop_is_kde,
    detected_invoking_desktop,
    pinned_presentation_gui_user,
    presentation_gui_user,
    resolve_layout_mode,
)
from scripts.presentation_windows import (
    PRESENTATION_PROGRAM_NAMES,
    close_desktop_presentation,
    close_presentation_window,
    desktop_presentation_windows,
    presentation_layout_markers,
    presentation_window_processes,
    PresentationWindowProcess,
    _proc_cmdline,
    unsafe_presentation_report,
    unsafe_root_presentation_windows,
    UnsafePresentationWindow,
)
from scripts.display_bridge import (
    SUDOERS_RULE_MODE,
    display_bridge_launch_problem,
    display_bridge_state,
    DisplayBridgeState,
    ensure_display_bridge,
    sudoers_rule_state,
)
from scripts.desktop_approval import (
    DEFAULT_DESKTOP_APPROVAL_SETTING_PATH,
    DESKTOP_APPROVAL_REVOKED,
    _desktop_approval_lines,
    _desktop_approval_operator,
    _desktop_approval_setting_path,
    read_desktop_approval_record,
    read_host_desktop_approval,
    switchyard_approve_desktop_command,
    write_desktop_approval_record,
    write_host_desktop_approval,
)
from scripts.desktop_policy import (
    DESKTOP_FROM_CHOSEN_HEADLESS,
    DESKTOP_FROM_HEADLESS_OPTION,
    DESKTOP_FROM_HOST_APPROVAL,
    DESKTOP_FROM_NEW_APPROVAL,
    DESKTOP_FROM_POLICY_FILE,
    approved_desktop_policy,
    _desktop_host_is_headless,
    install_recovered_desktop_access,
    _prompt_choice,
    _resolve_desktop_policy,
    upgrade_desktop_policy_decision,
)
from scripts.project_desktop import (
    DESKTOP_ENV_KEYS,
    configure_project_desktop,
    prepare_project_desktop,
)
from scripts.legacy_presentation import (
    _desktop_state_root_problem,
    legacy_presentation_migration,
    legacy_presentation_refusal,
    legacy_presentation_section,
    LegacyPresentationMigration,
    migrate_legacy_presentation,
    presentation_controller_enabled,
    presentation_section_for_roles,
)
from scripts.presentation_window_replacement import (
    replace_presentation_window_command,
)
from scripts.live_role_runtime import (
    _drop_roles_with_stale_provider_runtime,
    _model_from_argv,
    live_cli_for_role,
    live_model_for_role,
    process_tree_argvs,
    roles_with_stale_provider_runtime,
    sync_reload_config_to_live_sessions,
)
from scripts.owner_state_dirs import (
    _is_owner_state_path,
    _owner_state_roots,
    ensure_owner_state_dirs,
    install_owner_state_dir_args,
)
from scripts.board_authority_preflight import (
    process_authority_board_compatibility,
)
from scripts.pane_launcher_preflight import (
    _verify_pane_launcher_path,
)
from scripts.launch_phases import (
    LaunchPreparation,
    LaunchSetup,
    WorkerStartup,
    _launch_runners_and_paths,
    _prepare_launch,
    _report_launch,
    _start_workers_and_present,
    _write_layout_and_plan,
)
from scripts.upgrade_phases import (
    UpgradeIdentitiesDone,
    UpgradeSourcePinned,
    UpgradeStateReady,
    UpgradeToolingStaged,
    _finish_upgrade,
    _pin_upgrade_source,
    _recover_upgrade_state,
    _refresh_upgrade_artifacts,
    _stage_upgrade_tooling,
    _upgrade_identities_and_accounts,
)
from scripts.director_upgrade import finish_upgrade_command
from scripts.privileged_runtime_plan import (
    authoritative_refresh_plan,
    close_privileged_artifacts,
    legacy_owner_from_host_records,
    plan_for_current_identities,
    _privileged_baseline_plan,
    privileged_provision_privacy_problems,
    _provision_owner,
    reconstruct_privileged_baseline,
    repair_legacy_provision_ownership,
    _root_controlled_record,
    SYSTEMD_SYSTEM_UNIT_DIR,
    _tenant_runs_on_project_account,
    _unit_environment,
)
from scripts.runtime_artifact_refresh import (
    _path_containment_error,
    _plan_replacements,
    _tenant_copy_is_current,
    installed_controller,
    refresh_generated_project_runtime_artifacts,
)
from scripts.project_role_plan_support import (
    _commit_git_dir_from_plan_data,
    _configured_audit_roles,
    _configured_implementer_roles,
    _install_and_restart_board_unit,
    _loaded_plan_field,
    _owner_home_from_plan_data,
    _regenerated_control_user,
)
from scripts.project_role_add import (
    add_project_role_command,
    _add_role_payload,
    _apply_add_role_board_sql,
    _is_recognized_generated_project_layout,
    _next_visible_role_slot,
    _project_plan_for_added_role,
    _update_project_design_artifact_for_role,
    _vcs_close_role_from_plan_data,
    _write_added_role_config,
    _write_updated_project_plan_artifacts,
)
from scripts.project_vcs_close_role import (
    _apply_vcs_close_role_board_sql,
    _project_plan_for_vcs_close_role,
    _write_vcs_close_role_artifacts,
    set_project_vcs_close_role_command,
)
from scripts.project_role_runtime import _role_named, set_project_role_runtime_command
from scripts.repository_boundary_repair import switchyard_repair_boundary_command
from scripts.root_plan_reconstruction import (
    REGENERATED_PLAN_FIELDS,
    _regenerated_field_divergence,
    _resume_plan_from_record,
    _resume_source_release,
    _validated_role_names,
    plan_workflow_from_root,
)
from scripts.tenant_release_target import (
    _deploy_ref_remote_branch,
    _parse_ls_remote_head,
    _resolve_deploy_ref_from_bare_repo,
    _resolve_deploy_ref_readonly,
    explicit_source_caches,
    git_deploy_ref_ls_remote_args,
    git_deploy_ref_rev_parse_args,
    installed_release_deploy_target,
    switchyard_bare_repo,
    tenant_release_status,
)
from scripts.tenant_config_records import (
    TENANT_CONFIG_RECORD_NAME,
    _tenant_config_candidates,
    board_declared_role_names,
    normalize_tenant_config_mode,
    record_tenant_config_path,
    recorded_tenant_config_path,
    registered_tenant_config_path,
    tenant_config_conflicts,
    tenant_config_record_path,
    verified_tenant_config,
)
from scripts.packet_completion import (
    PacketCompletion,
    _board_answers,
    _owner_unit_is_active,
    _owner_user_unit_args,
    privileged_packet_completion,
)
from scripts.pane_liveness_checks import (
    PaneLiveness,
    owner_tmux_targets,
    pane_liveness,
    process_owner_uid,
    process_start_ticks,
)
from scripts.recovery_readiness import (
    _acl_entries,
    recovery_readiness_problems,
    repository_boundary_problems,
)
from scripts.resume_provision_command import (
    _finish_provision_after_packet,
    plan_with_tenant_checkout,
    switchyard_resume_provision_command,
)
from scripts.project_teardown import (
    SwitchyardTeardownAction,
    SwitchyardTeardownPlan,
    TEARDOWN_MINIMUM_OWNER_UID,
    TEARDOWN_PROTECTED_USERS,
    _bash_action,
    _confirm_teardown_project,
    _drop_database_command,
    _owner_removal_actions,
    _port_from_board_url,
    _print_teardown_plan,
    _run_teardown_actions,
    _switchyard_teardown_actions,
    _teardown_project_context,
    _ticket_board_existing_ticket_count,
    owner_removal_refusal,
    owner_removal_residue,
    switchyard_teardown_command,
)
from scripts.owner_preparation import (
    ExistingOwnerUser,
    OwnerUserProvisionResult,
    _confirm_existing_owner_user,
    _enable_owner_linger_args,
    _ensure_owner_user_and_project_dir,
    _existing_owner_user,
    _existing_project_path_is_usable,
    _owner_linger_is_enabled,
    _owner_linger_show_args,
    _owner_project_install_args,
    _owner_project_install_command,
    _owner_project_install_commands,
    _owner_user_verbatim,
    _precheck_project_path_before_mutating,
    _resolve_owner_shell_path,
    _verify_project_path_writable_by_owner,
)
from scripts.new_project_support import (
    NEW_PROJECT_FIXED_ROLE_NAMES,
    NEW_PROJECT_REQUIRED_ROLES,
    NEW_PROJECT_STAGES,
    NEW_RESULT_FILE_ENV,
    ProvisioningStages,
    SWITCHYARD_DESIGN_FILE_NAME,
    _agent_owner_user,
    _chown_switchyard_project_files,
    _confirm_switchyard_new,
    _dedupe_role_cli_pairs,
    _prepare_first_run_auth_worktrees,
    _project_dir,
    _report_new_project_to_caller,
    _require_new_project_roles,
    _resolve_project_path,
    _write_initial_switchyard_project_artifact,
    announce_new_project_presentation,
    print_role_plan_review,
)
from scripts.upgrade_records import (
    RoleAccountCutover,
    UPGRADE_JOURNAL_OBSERVATIONS,
    UPGRADE_JOURNAL_SCHEMA,
    UPGRADE_PHASES,
    UPGRADE_PHASE_OWNERS,
    UPGRADE_SOURCE_SCHEMA,
    _read_journal_file,
    _record_upgrade_observation,
    _write_privileged_json,
    director_phase_required,
    outstanding_release_phase_report,
    privileged_upgrade_source_path,
    publish_tenant_journal_projection,
    read_upgrade_journal,
    read_upgrade_source,
    record_release_phase_from_status,
    record_upgrade_phase,
    record_upgrade_source,
    resolve_pinned_upgrade_source,
    upgrade_journal_path,
    upgrade_phase_observation,
    upgrade_phase_report,
    upgrade_phase_state,
    upgrade_source_unavailable_reason,
)
from scripts.new_project_phases import (
    NewProjectAccounts,
    NewProjectBoard,
    NewProjectChoices,
    NewProjectPreflight,
    NewProjectSignIn,
    _check_new_project_preflight,
    _launch_new_project_panes,
    _prepare_new_project_accounts,
    _prepare_new_project_board,
    _resolve_new_project_choices,
    _run_new_project_sign_in,
)
from scripts.github_identity import (
    GITHUB_IDENTITY_TIMEOUT_SECONDS,
    _plan_with_selection,
    clear_owner_github_identity_command,
    github_identity_status,
    selected_key_problems,
    set_owner_github_identity_command,
    write_plan_no_follow,
)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "team-launcher"
DEFAULT_SWITCHYARD_REGISTRY_DIR = Path("/etc/switchyard/projects")
# The shell helpers already honour this; the Python entrypoint now does too, so
# a role account's invocation can be exercised against a real registry that is
# not the host's (SYRD-49).
SWITCHYARD_REGISTRY_DIR_ENV = "SWITCHYARD_PROJECT_REGISTRY_DIR"


def switchyard_registry_dir() -> Path:
    configured = os.environ.get(SWITCHYARD_REGISTRY_DIR_ENV, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_SWITCHYARD_REGISTRY_DIR
SWITCHYARD_REGISTRY_SCHEMA = "switchyard.project-registry.v1"
#: The agent CLIs a registered tenant's roles are configured with. Optional, so
#: a record written before it existed still reads; absent means "not recorded",
#: never "none" (SYRD-220).
SWITCHYARD_REGISTRY_AGENT_CLIS_KEY = "agent_clis"
SWITCHYARD_NAME = "switchyard"
TEAM_LAUNCHER_NAME = "team-launcher"
# SYRD-43: the program Konsole runs for a pane, so a detach never lands on a shell.
PANE_WINDOW_NAME = "switchyard-pane-window"
GENERIC_DEFAULT_STATE_DIR_NAME = "ticket-board"
LIVE_PGU_STATE_DIR_NAME = "pgu-ticket-board"
REMOVED_CONFIG_FREE_COMMANDS = frozenset({"bootstrap"})
BOOTSTRAP_REPLACEMENT_COMMAND = "switchyard new"
PROJECT_DESIGN_ARTIFACT_SCHEMA = "switchyard.project.v1"
PROJECT_DESIGN_DEFAULT_GATES = {
    "audit_signoff": True,
    "needs_inspection": False,
    "needs_user_signoff": False,
}
PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS = {
    "board_service_traversal": True,
    "supplementary_groups": [],
    "linger": True,
    "shell": "fish",
    # Empty means no credential sharing. A username here grants every role pane of
    # this project the ability to act as that user's Google account in agy; see
    # _seed_agy_credential_for_owner. The origin records how that value was chosen, so a
    # host default is distinguishable from a per-project override or an explicit opt-out.
    "agy_credential_source": "",
    "agy_credential_source_origin": "unset",
}
AGY_SOURCE_ORIGINS = frozenset({"unset", "host_default", "project_override", "opt_out"})
PROJECT_DESIGN_FORBIDDEN_KEYS = frozenset(
    {
        "stages",
        "workflow",
        "workflow_stages",
        "workflow_transitions",
        "extra_implementer_roles",
    }
)
WORKTREE_POLICIES = frozenset({"shared", "isolated"})
SWITCHYARD_PROJECT_DIR_NAME = ".switchyard"
DEFAULT_PANE_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT = Path("/opt/switchyard")
SWITCHYARD_RELEASE_MARKER_NAME = ".switchyard-release.json"
#: Root's note of what a host was running before an upgrade replaced it.
RELEASE_ROLLBACK_SCHEMA = "switchyard.release-rollback.v1"
#: The workflow seed of a tenant that keeps schema.sql's own workflow and
#: declares no per-project document. Such a tenant is not a legacy tenant: it
#: has nothing to adopt and no scaffold onboarding to migrate away from, so the
#: no-workflow detection has to leave it alone (SYRD-240).
NON_DECLARATIVE_WORKFLOW_SEED = "pgu-full"


DEFAULT_SESSION_DIR = (
    Path(_env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")).expanduser()
    if _env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
    else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / LIVE_PGU_STATE_DIR_NAME
    / "pane-sessions"
)
USER_BIN_ENV = "TEAM_LAUNCHER_BIN_DIR"
LEGACY_USER_BIN_ENV = "PGU_TEAM_LAUNCHER_BIN_DIR"
#: The human the passwordless lifecycle bridge resolved from the kernel before
#: it dropped to the owner. The bridge rebuilds the environment, so SUDO_USER
#: does not survive it and the desktop identity would otherwise be lost --
#: which is what made a bridged launch open its window against the owner's own
#: runtime directory, where no compositor is listening (SYRD-65).
TENANT_CONTROL_CALLER_ENV = "SWITCHYARD_TENANT_CONTROL_CALLER"
MAX_VISIBLE_PANES_PER_WINDOW = 6
SWITCHYARD_VERSION = "dev"
SWITCHYARD_COMMANDS = (
    "board-skill",
    "new",
    "register",
    "upgrade",
    # Applies the reviewed repository boundary to a tenant that is already
    # running, and nothing else in the packet (SYRD-175).
    "repair-boundary",
    # Records, shows or withdraws this host's standing desktop approval. The
    # record existed and had one writer, behind an interactive prompt; this is
    # the front door onto it (SYRD-174).
    "approve-desktop",
    # Records an existing tenant's declared workflow as root's own, once, with
    # an operator authorizing it through Polkit and the whole decision in the
    # rollout journal (SYRD-166).
    "adopt-workflow",
    # Installs root's declared workflow onto a tenant whose board is running
    # none. A legacy tenant keeps stages and roles as table rows with no
    # workflow document, so /api/workflow answers null and its Director keeps
    # receiving provisioning-scaffold onboarding (SYRD-240).
    "migrate-workflow",
    # Rebinds a declared workflow's pane roles to the tenant's own panes, when
    # the declaration names runtimes or targets its panes do not register with
    # and so hides them, the Director's authority included (SYRD-262).
    "rebind-workflow-panes",
    # Finishes a project whose `switchyard new` stopped before it was
    # registered, so the installation that already exists can be completed
    # instead of started again: root's artifacts first (SYRD-147), then the
    # registration and role startup the interrupted process never reached
    # (SYRD-155).
    "resume-provision",
    "finish-upgrade",
    "cutover-roles",
    # Reports the publication-key cutover, and on request checks the one thing
    # no push can establish. Its only possible write is the root-owned,
    # non-secret evidence file (SYRD-116).
    "publication-status",
    # Reads the root-owned record of a privileged provisioning or upgrade run.
    # A role reads it directly rather than the User pasting output (SYRD-128).
    "rollout-log",
    # Recovers a disconnected Director display slot from the desktop session
    # that owns the screen. It runs through the tenant-control bridge, which
    # authenticates the operator from SUDO_UID against the tenant's root-owned
    # grant -- the ordinary recovery is gated to the Director's own pane, and
    # that pane is what has gone away (SYRD-239).
    "recover-display",
    # The one front door onto a bounded privileged operation: a catalogued
    # action name and typed values, pre-flown against the installed policy and
    # then asked for through pkexec. It replaces handing a sudo command to the
    # User through chat (SYRD-112).
    "privileged-action",
    # Repoints /opt/switchyard/current at an already-built release, by commit
    # alone: the bounded version of the bare `ln -sfn` the operator packet used
    # to carry. Records the previous target before it moves anything, verifies
    # what landed, and puts the previous target back if it does not (SYRD-112).
    "install-shared-release",
    # Compares the shared release, the tenant's deployed board, the live build
    # and both upgrade journals, and -- only as root, and only after re-proving
    # the deployment from the running board -- closes the release phase. Its
    # only write is root's own journal entry (SYRD-117).
    "release-status",
    "add-role",
    # Reports what a declared pool of interchangeable workers would change, and
    # what would stop it. Reads only (SYRD-37).
    "worker-pool",
    # Records which of the owner's existing keys a tenant publishes with, and
    # rewrites the managed ssh_config block. Both are root's writes (SYRD-100).
    "set-owner-identity",
    "present",
    "attach",
    "replace-window",
    "set-vcs-close-role",
    "set-role-runtime",
    "agy-credential",
    "seed-role-credentials",
    "role-prompt",
    "onboarding-readiness",
    "stop",
    # The other half of `stop`: bring a suspended tenant back in dependency
    # order, and stop at the boundary that fails rather than claiming a start
    # over a board that never came up (SYRD-193).
    "start",
    "teardown",
    "status",
    "validate-models",
)
# `present` and `board-skill` act on the caller's own runtime -- display slots
# and the caller's CLI skill trees -- so neither needs to escalate. `role-prompt`
# writes the tenant's own configuration through the board's workflow API as the
# invoking user, which is the point: the director sets a role's remit without root
# and without hand-editing a generated artifact.
SWITCHYARD_UNPRIVILEGED_COMMANDS = frozenset(
    # `finish-upgrade` is the director's own phase and refuses to run as root by
    # design; classifying it privileged made the wrapper escalate it into the
    # refusal, leaving the director no way to run it at all (SYRD-49).
    #
    # `attach` must never escalate either, and for a sharper reason: the whole
    # point of it is to hand an operator a terminal on a worker without a
    # privileged parent shell behind it. A wrapper that ran it through sudo
    # would put exactly that shell there, and every key the operator pressed
    # would have it as an ancestor (SYRD-76).
    #
    # `worker-pool` for the same reason as `role-prompt` and `attach` together.
    # Everything it writes goes through the board's own workflow API as the
    # invoking role, and everything else it does is a tmux session on the
    # project account's own server. Escalating it would put a privileged parent
    # shell behind `worker-pool <project> attach <worker>`, which is precisely
    # the thing SYRD-76 exists to keep out from behind an operator's terminal
    # (SYRD-37).
    #
    # `privileged-action` is the sharpest case of all. It escalates itself,
    # through pkexec, for one catalogued action at a time -- and the root-owned
    # helper then proves the CALLER is the control role's registered pane by
    # walking its own ancestry up to a tmux parent. A wrapper that ran this
    # under sudo would put a privileged shell in the middle of that walk and
    # change the identity being proved, so the boundary would be asked about
    # the wrong process. It runs unprivileged, exactly as typed (SYRD-112).
    #
    # `status` reads and prints; it changes nothing. Escalating it asked an
    # operator to cross a privileged mutation boundary to look at their own
    # host, and after a legacy cutover -- when the tenant's configuration moved
    # into an account the desktop operator is not -- that ask became a password
    # prompt for a read. The root-owned registry, release and journal records
    # this reports are world-readable by design; anything it cannot read is
    # named as unavailable instead (SYRD-241).
    {
        "present", "attach", "board-skill", "role-prompt", "set-role-runtime",
        "finish-upgrade", "worker-pool", "privileged-action", "status",
    }
)
SWITCHYARD_PRIVILEGED_COMMANDS = frozenset(
    command for command in SWITCHYARD_COMMANDS if command not in SWITCHYARD_UNPRIVILEGED_COMMANDS
)
SUPPORTED_CONFIG_CLI_NAMES = ("agy", "claude", "codex", "hermes")


def viewer_session_for_project(project: str) -> str:
    return f"{project}-viewer"


def project_window_title(config: ProjectConfig) -> str:
    return config.project_name.strip() or config.project


KNOWN_LIVE_CLI_NAMES = set(SUPPORTED_CONFIG_CLI_NAMES)
AGY_CONVERSATION_ROOT = Path.home() / ".gemini" / "antigravity-cli"
RESUME_STARTUP_TIMEOUT_SECONDS = 1.5
RESUME_STARTUP_POLL_SECONDS = 0.1
DETACHED_SESSION_STABILITY_SECONDS = 2.0
RUNTIME_READY_ATTEMPTS = 50
RUNTIME_READY_POLL_SECONDS = 0.1
NO_LAUNCHER_SELF_DEPLOY_ENV = "TEAM_LAUNCHER_NO_SELF_DEPLOY"
LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV = "PGU_TEAM_LAUNCHER_NO_SELF_DEPLOY"
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
# A credential-source user name is joined onto a home base to locate a token, so it is
# constrained to a plain useradd-style name: no separators, no traversal.
OWNER_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUPPORTED_NEW_PROJECT_CLIS = ("claude", "codex", "agy", "hermes")
NEW_PROJECT_ROLE_CLI_DEFAULTS = {
    "designer": "claude",
    "director": "claude",
    "audit": "claude",
}
SWITCHYARD_PROMPT_MAX_ATTEMPTS = 5
NEW_PROJECT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "audit", "user", "unassigned"})
NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "user", "unassigned"})
NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES = ("main", "ops")
NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES = (
    ("main", "core/domain implementation and integration"),
    ("ops", "environment, services, tooling, and infrastructure"),
    ("app", "application/UI work"),
    ("research", "investigation, design support, and unknowns"),
    ("perf", "measurement and performance work"),
)


@dataclass(frozen=True)
class RoleConfig:
    role: str
    slot: int | None
    detached: bool
    tmux_session: str
    target: str
    workdir: str
    cli: list[str]
    model: str
    model_arg: str
    effort: str
    yolo: bool
    extra_args: list[str]
    resume_mode: str
    resume_flag: str
    resume_subcommand: str
    fresh_session_per_ticket: bool
    live_commands: list[str]
    env: dict[str, str]
    # Read only for upgrade compatibility. SYRD-69 runs every role as the
    # project account; authority is the registered live process, not this UID.
    run_as_user: str = ""
    unset_env: tuple[str, ...] = ()
    # SYRD-135: declared on the role, projected from the workflow document, and
    # acted on by the notify listener rather than here -- the reset happens at
    # the ticket boundary in a running pane, not at launch. It is carried in the
    # generated config so the two descriptions of a role cannot disagree.
    ephemeral: bool = False
    # SYRD-141: what this role's presentation pane is called. Projected from
    # the workflow document, where the implementer default and any per-role
    # override are decided; empty here means a config generated before that
    # existed, and the role's own name is the answer it had then.
    presentation_label: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    project: str
    project_name: str
    ticket_prefix: str
    layout: Path
    session_dir: Path
    board_url: str
    board_socket: str
    upstream_report_url: str
    upstream_report_token_file: str
    run_as_user: str
    pane_launcher: Path | None
    repository: Path | None
    control_repository: Path | None
    worktree_base: Path | None
    worktree_remote: str
    worktree_branch: str
    roles: list[RoleConfig]
    desktop_access: dict[str, Any] | None = None
    role_state_isolation: bool = False
    #: A pool of interchangeable workers this project may run, declared once
    #: rather than written out as N roles. None means the project has none,
    #: which is every project that has not asked for one (SYRD-37).
    worker_pool: "WorkerPool | None" = None


@dataclass(frozen=True)
class LauncherUpgradeResult:
    changed: bool
    message: str


@dataclass(frozen=True)
class TenantReleaseStatus:
    board_root: Path
    owner_user: str
    owner_home: Path
    provisioned_system_unit: Path | None
    commit_git_dir: str
    current_release: Path | None
    current_sha: str
    target_sha: str
    deploy_ref: str
    source_repo: Path
    resolve_error: str = ""
    clone_source_repo: Path | None = None
    #: Where this tenant's board actually listens. Carried because the deploy
    #: script's own defaults are a different tenant's board on a shared host
    #: (SYRD-87 R6).
    board_port: str = ""
    board_socket: str = ""

    @property
    def unchanged(self) -> bool:
        return bool(self.current_sha and self.target_sha and self.current_sha == self.target_sha)


@dataclass(frozen=True)
class ProjectDesignArtifact:
    project: str
    project_name: str
    ticket_prefix: str
    owner_user: str
    repository: Path
    remote: str
    default_branch: str
    worktree_policy: str
    design_document: Path
    implementer_roles: tuple[str, ...]
    audit_roles: tuple[str, ...]
    role_clis: tuple[tuple[str, str], ...]
    include_designer: bool
    include_audit: bool
    push_policy: str
    gates: dict[str, bool]
    capability_grants: dict[str, object]
    #: What each role was chosen to run on, by stable identifier. Optional, so
    #: every artifact written before these existed still loads, and absent means
    #: "not chosen" rather than "no model" -- the launcher reads a missing model
    #: as the runtime's own default (SYRD-115).
    role_models: tuple[tuple[str, str], ...] = ()
    role_efforts: tuple[tuple[str, str], ...] = ()
    #: Which version of the recorded option catalog those identifiers were
    #: chosen from. A label may be reworded and a catalog may gain entries
    #: without changing what this project runs; this says what it was read off.
    catalog_version: int = 0


@dataclass(frozen=True)
class SharedSwitchyardRelease:
    root: Path
    marker_commit: str = ""
    marker_error: str = ""

    @property
    def active(self) -> bool:
        return bool(self.marker_commit)

    @property
    def undeterminable(self) -> bool:
        return bool(self.error)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def switchyard_shared_install_root() -> Path:
    return Path(os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT)).expanduser()


def switchyard_shared_target(root: Path | None = None) -> Path:
    return (root or switchyard_shared_install_root()) / "current" / SWITCHYARD_NAME


def switchyard_shared_pane_launcher(root: Path | None = None) -> Path:
    return (root or switchyard_shared_install_root()) / "current" / "scripts" / TEAM_LAUNCHER_NAME


def _read_switchyard_release_marker(path: Path) -> SharedSwitchyardRelease | None:
    marker = path / SWITCHYARD_RELEASE_MARKER_NAME
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SharedSwitchyardRelease(root=path, marker_error=str(exc))
    commit = str(payload.get("commit") or "").strip()
    if not commit:
        return SharedSwitchyardRelease(root=path, marker_error=f"{marker} has no commit")
    return SharedSwitchyardRelease(root=path, marker_commit=commit)


def shared_switchyard_release_for_path(path: Path, *, install_root: Path | None = None) -> SharedSwitchyardRelease | None:
    root = (install_root or switchyard_shared_install_root()).expanduser().resolve(strict=False)
    candidate = path.expanduser().resolve(strict=False)
    if not _path_is_under(candidate, root):
        return None
    for probe in (candidate, *candidate.parents):
        if not _path_is_under(probe, root):
            break
        marker = _read_switchyard_release_marker(probe)
        if marker is not None:
            return marker
    if _path_is_under(candidate, root / "current") or _path_is_under(candidate, root / "releases"):
        return SharedSwitchyardRelease(root=candidate)
    return None


def switchyard_version_text(repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    release = _read_switchyard_release_marker(root)
    if release is not None and release.marker_commit:
        return f"switchyard {release.marker_commit}"
    return f"switchyard {SWITCHYARD_VERSION}"


def _allocated_board_port(project: str, *, base: int = 18_770, span: int = 10_000) -> int:
    if project == "pgu":
        return 8770
    digest = hashlib.blake2s(project.encode("utf-8"), digest_size=2).digest()
    return base + (int.from_bytes(digest, "big") % span)


def _default_board_url(project: str) -> str:
    return f"http://127.0.0.1:{_allocated_board_port(project)}"


def _default_board_socket(project: str) -> str:
    return f"/run/{project}-ticket-board/ticket-board.sock"


def current_user_name() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def runtime_dir_for_uid(uid: int) -> Path:
    return Path(f"/run/user/{uid}")


def loginctl_enable_linger_args(user_name: str) -> list[str]:
    return ["loginctl", "enable-linger", user_name]


def ensure_user_linger_runtime(
    user_name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
    poll_seconds: float = RUNTIME_READY_POLL_SECONDS,
) -> Path:
    user = user_name.strip()
    if not user:
        raise SystemExit("team-launcher: cannot provision runtime for an empty user name")
    uid = uid_for_user(user)
    if uid is None:
        raise SystemExit(f"team-launcher: cannot provision runtime for unknown user {user!r}")
    runtime_dir = runtime_dir_for_uid(uid)
    exists = runtime_exists or (lambda path: path.is_dir())
    result = runner(
        loginctl_enable_linger_args(user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            f"team-launcher: failed to enable linger for {user!r}{detail}; "
            f"run `sudo loginctl enable-linger {user}` and retry"
        )
    for _ in range(max(1, attempts)):
        if exists(runtime_dir):
            return runtime_dir
        sleeper(poll_seconds)
    raise SystemExit(
        f"team-launcher: linger is enabled for {user!r}, but {runtime_dir} is still missing; "
        "start or restart that user's systemd user manager and retry"
    )


def ensure_configured_runtime_user(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
) -> Path | None:
    user = config.run_as_user or current_user_name()
    if not user or not session_dir_uses_user_runtime(config.session_dir, user):
        return None
    return ensure_user_linger_runtime(
        user,
        runner=runner,
        runtime_exists=runtime_exists,
        sleeper=sleeper,
        attempts=attempts,
    )


def default_user_bin(user_name: str = "") -> str:
    return default_user_bin_dirs(user_name)[0]


def default_user_bin_dirs(user_name: str = "") -> list[str]:
    configured = _env_first(USER_BIN_ENV, LEGACY_USER_BIN_ENV)
    if configured:
        return [str(Path(configured).expanduser())]
    owner_home = home_dir_for_user(user_name) if user_name.strip() else None
    if owner_home is not None:
        return [str(owner_home / "bin"), str(owner_home / ".local" / "bin")]
    home = Path.home()
    return [str(home / "bin"), str(home / ".local" / "bin")]


def _owner_home_bin_dirs(owner_home: Path) -> list[str]:
    configured = _env_first(USER_BIN_ENV, LEGACY_USER_BIN_ENV)
    if configured:
        return [str(Path(configured).expanduser())]
    home = owner_home.expanduser()
    return [str(home / "bin"), str(home / ".local" / "bin")]


def default_pane_base_path(user_name: str = "") -> str:
    user = user_name.strip()
    if user and current_user_name() != user:
        return DEFAULT_PANE_BASE_PATH
    return os.environ.get("PATH", "")


def _gui_home(gui_user: str) -> str:
    try:
        return pwd.getpwnam(gui_user).pw_dir
    except KeyError:
        return f"/home/{gui_user}"


def visible_roles_for_viewer(config: ProjectConfig) -> list[RoleConfig]:
    return sorted(
        (role for role in config.roles if not role.detached and role.slot is not None),
        key=lambda role: (role.slot if role.slot is not None else 0, role.role),
    )


def running_through_tenant_control() -> bool:
    """Is this launcher the owner half of a bridged invocation?

    The bridge names the human who crossed it in the environment it builds, and
    that is the only thing here that distinguishes "the person is at this
    screen" from "this is the owner account, which has no screen at all".
    """
    return bool(os.environ.get(TENANT_CONTROL_CALLER_ENV, "").strip())


def _int_env(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _expand_path(value: str, *, base: Path) -> Path:
    expanded = os.path.expandvars(value)
    path = Path(expanded).expanduser()
    return path if path.is_absolute() else base / path


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed


def _artifact_forbidden_keys(raw: dict[str, Any]) -> list[str]:
    found = [key for key in raw if key in PROJECT_DESIGN_FORBIDDEN_KEYS]
    project_raw = raw.get("project")
    if isinstance(project_raw, dict):
        found.extend(f"project.{key}" for key in project_raw if key in PROJECT_DESIGN_FORBIDDEN_KEYS)
    return sorted(found)


def _artifact_string(raw: dict[str, Any], key: str, *, path: Path, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise SystemExit(f"{path} field {key!r} must be a JSON string")
    value = value.strip()
    if not value:
        raise SystemExit(f"{path} field {key!r} must be non-empty")
    return value


def _artifact_optional_string(raw: dict[str, Any], key: str, *, path: Path, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SystemExit(f"{path} field {key!r} must be a JSON string")
    return value.strip() or default


def _artifact_role_list(raw: Any, *, path: Path, field: str, defaults: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(defaults)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} field {field!r} must be a JSON string list")
    result: list[str] = []
    for item in raw:
        role = item.strip().lower()
        if role in {"designer", "director", "audit", "user", "unassigned"}:
            raise SystemExit(f"{path} field {field!r} contains reserved role {role!r}")
        if role not in result:
            result.append(role)
    if not result:
        raise SystemExit(f"{path} field {field!r} must contain at least one implementer role")
    return tuple(result)


def _artifact_audit_role_list(raw: Any, *, path: Path, defaults: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(defaults)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} field 'project.audit_roles' must be a JSON string list")
    result: list[str] = []
    for item in raw:
        role = item.strip().lower()
        if not ROLE_RE.fullmatch(role):
            raise SystemExit(f"{path} field 'project.audit_roles' role {role!r} must match ^[a-z][a-z0-9_-]{{0,63}}$")
        if role in NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
            raise SystemExit(f"{path} field 'project.audit_roles' contains reserved role {role!r}")
        if role not in result:
            result.append(role)
    return tuple(result)


def _validate_new_project_cli(value: str, *, context: str = "CLI") -> str:
    cli = value.strip().lower()
    if cli not in SUPPORTED_NEW_PROJECT_CLIS:
        raise SystemExit(f"{context} must be one of {', '.join(SUPPORTED_NEW_PROJECT_CLIS)}")
    return cli


def _validate_new_project_implementer_role(value: str, *, context: str = "role") -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in NEW_PROJECT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _validate_new_project_audit_role(value: str, *, context: str = "audit role") -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _dedupe_role_names(roles: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for role in roles:
        if role not in result:
            result.append(role)
    return tuple(result)


def _default_role_cli_pairs(
    implementer_roles: Sequence[str],
    *,
    include_designer: bool,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    resolved_audit_roles = tuple(audit_roles) if audit_roles is not None else (("audit",) if include_audit else ())
    if include_designer:
        pairs.append(("designer", NEW_PROJECT_ROLE_CLI_DEFAULTS["designer"]))
    pairs.append(("director", NEW_PROJECT_ROLE_CLI_DEFAULTS["director"]))
    pairs.extend((role, NEW_PROJECT_ROLE_CLI_DEFAULTS["audit"]) for role in resolved_audit_roles)
    pairs.extend((role, "codex") for role in implementer_roles)
    return tuple(pairs)


def _role_cli_map(role_clis: Sequence[tuple[str, str]]) -> dict[str, str]:
    return {role: cli for role, cli in role_clis}


def _artifact_role_value_pairs(
    raw: object, *, path: Path, field: str
) -> tuple[tuple[str, str], ...]:
    """A role -> value map out of an artifact, or nothing.

    Absent is the ordinary case: every artifact written before these fields
    existed has no such key, and that must load rather than fail. What is
    refused is a key that is present and is not a map of strings, because a
    half-understood one would be written back out as though it were read.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} {field} must be a mapping of role to value")
    pairs: list[tuple[str, str]] = []
    for role, value in raw.items():
        if not isinstance(role, str) or not isinstance(value, str):
            raise SystemExit(f"{path} {field} must map role names to strings")
        if value.strip():
            pairs.append((role, value.strip()))
    return tuple(pairs)


def _artifact_role_cli_pairs(
    raw: Any,
    *,
    path: Path,
    implementer_roles: Sequence[str],
    include_designer: bool,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    defaults = _default_role_cli_pairs(
        implementer_roles,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
    )
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'project.role_clis' must be a JSON object")
    allowed_roles = {role for role, _cli in defaults}
    pairs_by_role = dict(defaults)
    for raw_role, raw_cli in raw.items():
        if not isinstance(raw_role, str) or not isinstance(raw_cli, str):
            raise SystemExit(f"{path} field 'project.role_clis' must map role strings to CLI strings")
        role = raw_role.strip().lower()
        if role not in allowed_roles:
            raise SystemExit(f"{path} field 'project.role_clis' contains unknown role {role!r}")
        pairs_by_role[role] = _validate_new_project_cli(raw_cli, context=f"CLI for {role}")
    return tuple((role, pairs_by_role[role]) for role, _cli in defaults)


def _artifact_bool_mapping(raw: Any, *, path: Path, field: str, defaults: dict[str, bool]) -> dict[str, bool]:
    if raw is None:
        return dict(defaults)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field {field!r} must be a JSON object")
    result = dict(defaults)
    for key, value in raw.items():
        if key not in defaults:
            raise SystemExit(f"{path} field {field!r} contains unknown key {key!r}")
        if not isinstance(value, bool):
            raise SystemExit(f"{path} field {field}.{key} must be a JSON boolean")
        result[key] = value
    return result


def _artifact_capability_grants(raw: Any, *, path: Path) -> dict[str, object]:
    if raw is None:
        return dict(PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'capability_grants' must be a JSON object")
    result = dict(PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
    allowed = set(result)
    for key, value in raw.items():
        if key not in allowed:
            raise SystemExit(f"{path} field 'capability_grants' contains unknown key {key!r}")
        if key == "supplementary_groups":
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise SystemExit(f"{path} field 'capability_grants.supplementary_groups' must be a JSON string list")
            result[key] = [item.strip() for item in value]
        elif key == "shell":
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"{path} field 'capability_grants.shell' must be a non-empty JSON string")
            result[key] = value.strip()
        elif key == "agy_credential_source":
            if not isinstance(value, str):
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source' must be a JSON string"
                )
            candidate = value.strip()
            if candidate and not _is_valid_owner_user_name(candidate):
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source' must be a plain Unix "
                    f"user name, not {candidate!r}"
                )
            result[key] = candidate
        elif key == "agy_credential_source_origin":
            if not isinstance(value, str) or value.strip() not in AGY_SOURCE_ORIGINS:
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source_origin' must be "
                    f"one of {', '.join(sorted(AGY_SOURCE_ORIGINS))}"
                )
            result[key] = value.strip()
        elif not isinstance(value, bool):
            raise SystemExit(f"{path} field 'capability_grants.{key}' must be a JSON boolean")
        else:
            result[key] = value
    return result


def load_project_design_artifact(path: Path, *, expected_project: str | None = None) -> ProjectDesignArtifact:
    artifact_path = path.expanduser().resolve(strict=False)
    raw = _load_json(artifact_path)
    forbidden = _artifact_forbidden_keys(raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    schema = _artifact_string(raw, "schema", path=artifact_path)
    if schema != PROJECT_DESIGN_ARTIFACT_SCHEMA:
        raise SystemExit(
            f"{artifact_path} schema must be {PROJECT_DESIGN_ARTIFACT_SCHEMA!r}, got {schema!r}"
        )
    project_raw = raw.get("project")
    if not isinstance(project_raw, dict):
        raise SystemExit(f"{artifact_path} field 'project' must be a JSON object")
    forbidden = _artifact_forbidden_keys(project_raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    slug = _validate_project_slug(_artifact_string(project_raw, "slug", path=artifact_path))
    if expected_project is not None and slug != expected_project:
        raise SystemExit(f"{artifact_path} project slug {slug!r} does not match requested project {expected_project!r}")
    project_name = _artifact_optional_string(project_raw, "name", path=artifact_path, default=slug)
    ticket_prefix = validate_ticket_prefix(_artifact_string(project_raw, "ticket_prefix", path=artifact_path))
    owner_user = _artifact_string(project_raw, "owner_user", path=artifact_path)
    repository = _expand_path(_artifact_string(project_raw, "repository", path=artifact_path), base=artifact_path.parent)
    remote = _artifact_string(project_raw, "remote", path=artifact_path, default="origin")
    default_branch = _artifact_string(project_raw, "default_branch", path=artifact_path, default="main")
    worktree_policy = _artifact_string(project_raw, "worktree_policy", path=artifact_path, default="shared")
    if worktree_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"{artifact_path} field 'project.worktree_policy' must be one of {sorted(WORKTREE_POLICIES)}")
    design_document = _expand_path(_artifact_string(raw, "design_document", path=artifact_path), base=artifact_path.parent)
    implementer_roles = _artifact_role_list(
        project_raw.get("roles", project_raw.get("implementer_roles")),
        path=artifact_path,
        field="project.roles",
        defaults=DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    )
    include_designer_raw = project_raw.get("include_designer", True)
    if not isinstance(include_designer_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_designer' must be a JSON boolean")
    include_audit_raw = project_raw.get("include_audit", True)
    if not isinstance(include_audit_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_audit' must be a JSON boolean")
    audit_roles = _artifact_audit_role_list(
        project_raw.get("audit_roles"),
        path=artifact_path,
        defaults=("audit",) if include_audit_raw else (),
    )
    role_overlap = set(implementer_roles) & set(audit_roles)
    if role_overlap:
        raise SystemExit(
            f"{artifact_path} roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}"
        )
    role_clis = _artifact_role_cli_pairs(
        project_raw.get("role_clis"),
        path=artifact_path,
        implementer_roles=implementer_roles,
        include_designer=include_designer_raw,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
    )
    role_models = _artifact_role_value_pairs(
        project_raw.get("role_models"), path=artifact_path, field="project.role_models"
    )
    role_efforts = _artifact_role_value_pairs(
        project_raw.get("role_efforts"), path=artifact_path, field="project.role_efforts"
    )
    catalog_version = project_raw.get("catalog_version")
    push_policy = _artifact_string(project_raw, "push_policy", path=artifact_path, default="director-main-only")
    gates = _artifact_bool_mapping(project_raw.get("gates"), path=artifact_path, field="project.gates", defaults=PROJECT_DESIGN_DEFAULT_GATES)
    capability_grants = _artifact_capability_grants(project_raw.get("capability_grants"), path=artifact_path)
    return ProjectDesignArtifact(
        project=slug,
        project_name=project_name,
        ticket_prefix=ticket_prefix,
        owner_user=owner_user,
        repository=repository,
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
        design_document=design_document,
        implementer_roles=implementer_roles,
        audit_roles=audit_roles,
        role_clis=role_clis,
        role_models=role_models,
        role_efforts=role_efforts,
        catalog_version=int(catalog_version) if isinstance(catalog_version, int) else 0,
        include_designer=include_designer_raw,
        include_audit=bool(audit_roles),
        push_policy=push_policy,
        gates=gates,
        capability_grants=capability_grants,
    )


def project_design_artifact_payload(artifact: ProjectDesignArtifact) -> dict[str, Any]:
    return {
        "schema": PROJECT_DESIGN_ARTIFACT_SCHEMA,
        "design_document": str(artifact.design_document),
        "project": {
            "slug": artifact.project,
            "name": artifact.project_name,
            "ticket_prefix": artifact.ticket_prefix,
            "owner_user": artifact.owner_user,
            "repository": str(artifact.repository),
            "remote": artifact.remote,
            "default_branch": artifact.default_branch,
            "worktree_policy": artifact.worktree_policy,
            "roles": list(artifact.implementer_roles),
            "audit_roles": list(artifact.audit_roles),
            "include_designer": artifact.include_designer,
            "include_audit": artifact.include_audit,
            "role_clis": _role_cli_map(artifact.role_clis),
            **({"role_models": dict(artifact.role_models)} if artifact.role_models else {}),
            **({"role_efforts": dict(artifact.role_efforts)} if artifact.role_efforts else {}),
            **({"catalog_version": artifact.catalog_version} if artifact.catalog_version else {}),
            "push_policy": artifact.push_policy,
            "gates": artifact.gates,
            "capability_grants": artifact.capability_grants,
        },
    }


def _resolve_launcher_project_config(
    project: str,
    *,
    explicit_config: Path | None = None,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> SwitchyardProjectEntry:
    if explicit_config is not None:
        return SwitchyardProjectEntry(slug=project, name=project, config_path=explicit_config)
    effective_config_dir = config_dir or DEFAULT_CONFIG_DIR
    effective_registry_dir = registry_dir or switchyard_registry_dir()
    try:
        return _resolve_switchyard_project(
            project,
            config_dir=effective_config_dir,
            registry_dir=effective_registry_dir,
        )
    except SystemExit as exc:
        message = str(exc)
        if message.startswith("switchyard: unknown project"):
            raise SystemExit(
                f"team-launcher: unknown project {project!r}; "
                f"searched config dir {effective_config_dir} and registry dir {effective_registry_dir}"
            ) from exc
        raise


def _write_json_atomic(
    path: Path, payload: dict[str, Any], *, owner_user: str | None = None,
) -> None:
    owner = pwd.getpwnam(owner_user) if owner_user and os.geteuid() == 0 else None
    mode = 0o600
    if owner_user and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode) & 0o777
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # Publish tenant controls with the correct owner already attached;
            # never leave a root-owned replacement for a later repair step.
            if owner is not None:
                os.fchown(handle.fileno(), owner.pw_uid, owner.pw_gid)
            if owner_user:
                os.fchmod(handle.fileno(), mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _string_list(value: Any, *, field: str, role: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"role {role} {field} must be a JSON string list")
    return list(value)


def _bool_value(value: Any, *, field: str, role: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SystemExit(f"role {role} {field} must be a JSON boolean")
    return value


def _role_from_json(project: str, raw: dict[str, Any], *, base: Path, default_workdir: Path | None) -> RoleConfig:
    role = str(raw.get("role") or "").strip()
    if not role:
        raise SystemExit("team launcher role entry missing role")
    tmux_session = str(raw.get("tmux_session") or f"{project}-{role}").strip()
    slot_raw = raw.get("slot")
    slot = None if slot_raw is None else int(slot_raw)
    cli_raw = raw.get("cli")
    if isinstance(cli_raw, str):
        cli = shlex.split(cli_raw)
    elif isinstance(cli_raw, list) and all(isinstance(item, str) for item in cli_raw):
        cli = list(cli_raw)
    else:
        raise SystemExit(f"role {role} must define cli as a string or string list")
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise SystemExit(f"role {role} env must be a JSON object")
    workdir_raw = raw.get("workdir")
    if workdir_raw is None:
        workdir = str(default_workdir or Path.cwd())
    else:
        workdir = str(_expand_path(str(workdir_raw), base=base))
    cli_name = _command_name(cli[0])
    if cli_name not in SUPPORTED_CONFIG_CLI_NAMES:
        raise SystemExit(
            f"role {role} cli {cli_name!r} is not supported; "
            f"supported clis: {', '.join(SUPPORTED_CONFIG_CLI_NAMES)}"
        )
    resume_mode = str(raw.get("resume_mode") or DEFAULT_RESUME_MODE_BY_CLI.get(cli_name, "flag")).strip()
    resume_flag = str(raw.get("resume_flag") or DEFAULT_RESUME_FLAG_BY_CLI.get(cli_name, "--resume")).strip()
    resume_subcommand = str(raw.get("resume_subcommand") or DEFAULT_RESUME_SUBCOMMAND_BY_CLI.get(cli_name, "resume")).strip()
    return RoleConfig(
        role=role,
        slot=slot,
        detached=_bool_value(raw.get("detached"), field="detached", role=role),
        tmux_session=tmux_session,
        target=str(raw.get("target") or f"{tmux_session}:0.0").strip(),
        workdir=workdir,
        cli=cli,
        model=str(raw.get("model") or "").strip(),
        model_arg=str(raw.get("model_arg") or DEFAULT_MODEL_ARG_BY_CLI.get(cli_name, "--model")).strip(),
        effort=str(raw.get("effort") or "").strip(),
        yolo=_bool_value(raw.get("yolo"), field="yolo", role=role),
        extra_args=_string_list(raw.get("extra_args"), field="extra_args", role=role),
        resume_mode=resume_mode,
        resume_flag=resume_flag,
        resume_subcommand=resume_subcommand,
        fresh_session_per_ticket=_bool_value(
            raw.get("fresh_session_per_ticket"),
            field="fresh_session_per_ticket",
            role=role,
        ),
        live_commands=_string_list(raw.get("live_commands"), field="live_commands", role=role),
        env={str(key): str(value) for key, value in env_raw.items()},
        run_as_user=str(raw.get("run_as_user") or "").strip(),
        ephemeral=_bool_value(raw.get("ephemeral"), field="ephemeral", role=role),
        presentation_label=str(raw.get("presentation_label") or "").strip(),
    )


def role_account_name(project: str, role: str) -> str:
    """The Unix account a role runs as. Defined once, in provisioning."""
    from scripts.ticket_board.project_provision import role_account_name as _name

    return _name(project, role)


def role_run_as_user(config: ProjectConfig, role: RoleConfig) -> str:
    """The project identity after repatriation, or the legacy owner before it.

    Reading an old config as though migration had already succeeded can strand
    its resumable state.  The atomic ``role_state_isolation`` flip is therefore
    the compatibility boundary: only fresh/repatriated configs ignore the
    historical role binding.
    """
    if config.role_state_isolation:
        return config.run_as_user or current_user_name()
    return role.run_as_user or config.run_as_user


def role_process_runner_for(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """A runner for the project account's shared tmux server."""
    account = role_run_as_user(config, role)
    if account and current_user_name() != account:
        return _owner_process_runner(owner_user=account, runner=runner)
    return runner


def _role_board_env(config: ProjectConfig, role: RoleConfig, session_role_map: dict[str, str]) -> dict[str, str]:
    env = {
        "TICKET_BOARD_PROJECT": config.project,
        "TICKET_BOARD_PROJECT_NAME": config.project_name,
        "TICKET_BOARD_TICKET_PREFIX": config.ticket_prefix,
        "TICKET_BOARD_URL": config.board_url,
        "TICKET_BOARD_SOCKET": config.board_socket,
        "TICKET_BOARD_CALLER_ROLE": role.role,
        "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps(session_role_map, sort_keys=True, separators=(",", ":")),
    }
    if config.role_state_isolation:
        env["TICKET_BOARD_PROCESS_AUTHORITY"] = "1"
    else:
        # Runtime routing follows only explicit legacy bindings.  The
        # migration renderer may synthesize canonical account names before it
        # creates them; doing that here would misclassify a shared-account PGU
        # config as partially migrated and make ordinary startup require
        # artifacts it never installed (SYRD-66).
        owner = config.run_as_user or current_user_name()
        accounts = tuple(
            (candidate.role, candidate.run_as_user)
            for candidate in config.roles
            if candidate.run_as_user and candidate.run_as_user != owner
        )
        if accounts:
            env["TICKET_BOARD_ROLE_ACCOUNTS"] = ",".join(
                f"{name}={account}" for name, account in accounts
            )
    if config.run_as_user:
        # directorctl needs the owner's name to reach the display and viewer
        # sessions, which stay in the owner's tmux server (SYRD-39).
        env["SWITCHYARD_PROJECT_OWNER"] = config.run_as_user
    if config.upstream_report_url:
        env["TICKET_BOARD_REPORT_URL"] = config.upstream_report_url
        env["TICKET_BOARD_REPORT_ORIGIN_PROJECT"] = config.project
    if config.upstream_report_token_file:
        env["TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"] = config.upstream_report_token_file
    return env


def role_git_template_env(project: str) -> dict[str, str]:
    """The Git template a role's new repositories are created from, once staged.

    Implementers make their own source checkouts from their panes, with a plain
    `git clone`, and nothing Switchyard installs reached them: those clones had
    no pre-commit hook, so the size warning never fired (SYRD-257). Naming the
    root-staged template here gives every clone or init made from a role pane
    the warning-only policy before its first commit, and every worktree linked
    to it shares that clone's hooks. Only new repositories are affected, and
    only those a role makes -- this is not a global hooksPath. Unset until the
    template is staged, so a pane never points Git at a directory that is not
    there.
    """
    from scripts.ticket_board.project_provision import GIT_TEMPLATE_DIR_NAME

    template = Path(role_tooling_staging_dir(project)) / GIT_TEMPLATE_DIR_NAME
    if (template / "hooks" / "pre-commit").is_file():
        return {"GIT_TEMPLATE_DIR": str(template)}
    return {}


def _with_project_board_env(config: ProjectConfig, roles: list[RoleConfig]) -> list[RoleConfig]:
    session_role_map = {role.tmux_session: role.role for role in roles}
    git_env = role_git_template_env(config.project)
    return [
        replace(
            role,
            env={
                # Beneath the role's own env: a template a role's config names
                # on purpose is not overridden.
                **git_env,
                **role.env,
                **_role_board_env(config, role, session_role_map),
            },
        )
        for role in roles
    ]


def load_project_config(
    project: str,
    config_path: Path | None = None,
    *,
    document: Mapping[str, Any] | None = None,
) -> ProjectConfig:
    project_slug = _validate_project_slug(project)
    path = config_path or DEFAULT_CONFIG_DIR / f"{project_slug}.json"
    if document is None and os.geteuid() == 0:
        # Root reads a document in a directory the tenant owns, and the upgrade
        # re-reads it at every phase. `Path.read_text` follows whatever is
        # there, so one planted symlink sent root to read a file of the
        # tenant's choosing -- and a file that was not JSON came back as a
        # traceback rather than a refusal (SYRD-228). Unprivileged callers read
        # their own file as before.
        document, problem = read_tenant_document_no_follow(
            path, what=f"{project_slug}'s generated configuration"
        )
        if problem:
            raise SystemExit(f"switchyard: {problem}. Nothing was changed.")
    config = dict(document) if document is not None else _load_json(path)
    config_project = _validate_project_slug(str(config.get("project") or project_slug))
    if config_project != project_slug:
        raise SystemExit(f"config project {config_project!r} does not match requested project {project_slug!r}")
    project_name = str(config.get("project_name") or config.get("name") or config_project).strip() or config_project
    roles_raw = config.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise SystemExit(f"{path} must define a non-empty roles list")
    base = path.parent
    layout = _expand_path(str(config.get("layout") or f"{config_project}-konsole-layout.json"), base=base)
    run_as_user = str(config.get("run_as_user") or "").strip()
    session_dir = _expand_path(str(config.get("session_dir") or str(default_session_dir_for_user(run_as_user))), base=base)
    ticket_prefix = validate_ticket_prefix(str(config.get("ticket_prefix") or config_project))
    board_url = str(config.get("board_url") or _default_board_url(config_project)).strip()
    board_socket = str(config.get("board_socket") or _default_board_socket(config_project)).strip()
    upstream_report_url = str(config.get("upstream_report_url") or config.get("report_board_url") or "").strip()
    inline_report_token_keys = [
        key for key in ("upstream_report_token", "tenant_report_token") if str(config.get(key) or "").strip()
    ]
    if inline_report_token_keys:
        raise SystemExit(
            f"{path} stores an upstream report token value in {', '.join(inline_report_token_keys)}; "
            "use upstream_report_token_file instead"
        )
    upstream_report_token_file = str(
        config.get("upstream_report_token_file") or config.get("tenant_report_token_file") or ""
    ).strip()
    pane_launcher = None
    pane_launcher_raw = config.get("pane_launcher")
    if pane_launcher_raw is not None:
        pane_launcher = _expand_path(str(pane_launcher_raw), base=base)
    repository = None
    repository_raw = config.get("repository")
    if repository_raw is not None:
        repository = _expand_path(str(repository_raw), base=base)
    control_repository = None
    control_repository_raw = config.get("control_repository")
    if control_repository_raw is not None:
        control_repository = _expand_path(str(control_repository_raw), base=base)
    worktree_base = None
    worktree_base_raw = config.get("worktree_base")
    if worktree_base_raw is not None:
        worktree_base = _expand_path(str(worktree_base_raw), base=base)
    if repository is None and worktree_base is not None:
        raise SystemExit(f"{path} must not define worktree_base without repository")
    if control_repository is not None and repository is None:
        raise SystemExit(f"{path} must not define control_repository without repository")
    if control_repository is not None and worktree_base is None:
        raise SystemExit(f"{path} must define worktree_base when control_repository is set")
    worktree_remote = str(config.get("worktree_remote") or "origin").strip()
    worktree_branch = str(config.get("worktree_branch") or "main").strip()
    if not worktree_remote or not worktree_branch:
        raise SystemExit(f"{path} worktree_remote and worktree_branch must be non-empty")
    roles: list[RoleConfig] = []
    for raw in roles_raw:
        if not isinstance(raw, dict):
            continue
        default_workdir = repository
        if control_repository is not None and raw.get("workdir") is None and worktree_base is not None:
            role_name = str(raw.get("role") or "").strip()
            if role_name:
                default_workdir = worktree_base / role_name
        roles.append(_role_from_json(config_project, raw, base=base, default_workdir=default_workdir))
    if len(roles) != len(roles_raw):
        raise SystemExit(f"{path} roles must all be JSON objects")
    slots = [role.slot for role in roles if role.slot is not None]
    if len(set(slots)) != len(slots):
        raise SystemExit(f"{path} contains duplicate role slot assignments")
    detached_with_slots = [role.role for role in roles if role.detached and role.slot is not None]
    if detached_with_slots:
        raise SystemExit(f"{path} detached roles must not define layout slots: {', '.join(detached_with_slots)}")
    visible_without_slots = [role.role for role in roles if not role.detached and role.slot is None]
    if visible_without_slots:
        raise SystemExit(f"{path} visible roles must define layout slots: {', '.join(visible_without_slots)}")
    if repository is not None:
        repo_path = _normalized_path(repository)
    else:
        repo_path = ""
    seen_workdirs: dict[str, str] = {}
    duplicate_non_shared: list[str] = []
    for role in roles:
        normalized_workdir = _normalized_path(Path(role.workdir))
        previous_role = seen_workdirs.setdefault(normalized_workdir, role.role)
        if previous_role != role.role and normalized_workdir != repo_path:
            duplicate_non_shared.append(f"{previous_role}/{role.role}:{normalized_workdir}")
    if duplicate_non_shared:
        raise SystemExit(f"{path} contains duplicate non-shared role workdir assignments: {', '.join(duplicate_non_shared)}")
    parsed_config = ProjectConfig(
        project=config_project,
        project_name=project_name,
        ticket_prefix=ticket_prefix,
        layout=layout,
        session_dir=session_dir,
        board_url=board_url,
        board_socket=board_socket,
        upstream_report_url=upstream_report_url,
        upstream_report_token_file=upstream_report_token_file,
        run_as_user=run_as_user,
        pane_launcher=pane_launcher,
        repository=repository,
        control_repository=control_repository,
        worktree_base=worktree_base,
        worktree_remote=worktree_remote,
        worktree_branch=worktree_branch,
        roles=roles,
        desktop_access=config.get("desktop_access"),
        role_state_isolation=bool(config.get("role_state_isolation", False)),
        worker_pool=parse_worker_pool(config.get("worker_pool"), path=path),
    )
    boundary_error = _control_repository_boundary_error(parsed_config, require_existing_user=False)
    if boundary_error is not None:
        raise SystemExit(f"{path} control_repository {boundary_error}")
    return replace(parsed_config, roles=_with_project_board_env(parsed_config, roles))


def _normalized_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _quote_command(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _env_prefix(env: dict[str, str]) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in sorted(env.items(), key=lambda item: (item[0] != "TICKET_BOARD_PANE_TARGET", item[0]))
    ]


PANE_TARGET_ENV_KEYS = (
    "PGU_PANE_SESSION_ID",
    "TICKET_BOARD_PANE_SESSION_ID",
    "PGU_TICKET_BOARD_PANE_SESSION_DIR",
    "PGU_TICKET_BOARD_PANE_STATE_DIR",
    "TICKET_BOARD_PANE_SESSION_DIR",
    "TICKET_BOARD_PANE_STATE_DIR",
    "TICKET_BOARD_PANE_TARGET",
    "PGU_PANE_TARGET",
    "TICKET_BOARD_CALLER_ROLE",
)
PROBE_IDENTITY_ENV_KEYS = ("TMUX", "TMUX_PANE", *PANE_TARGET_ENV_KEYS)


def _env_unset_prefix(keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        result.extend(["-u", key])
    return result


def _prepend_path(path_value: str, directory: str) -> str:
    return _prepend_paths(path_value, [directory])


def _prepend_paths(path_value: str, directories: Sequence[str]) -> str:
    parts = [part for part in path_value.split(":") if part]
    for directory in reversed([item for item in directories if item]):
        parts = [part for part in parts if part != directory]
        parts.insert(0, directory)
    return ":".join(parts)


def _command_name(value: str) -> str:
    return Path(value.strip()).name


def role_runtime_binding(role: "RoleConfig") -> tuple[str, str]:
    """The (runtime, target) a role's pane registers with the board.

    One function for both sides of the match the board makes: the pane
    registers with this, and a declared workflow composed for a tenant names
    this, so the two cannot disagree (SYRD-262).
    """
    return _command_name(role.cli[0]), role.target


def role_pane_declaration(role: "RoleConfig") -> dict[str, Any]:
    """How a declared workflow describes this tenant's pane for `role`."""
    runtime, target = role_runtime_binding(role)
    return {"runtime": runtime, "target": target, "slot": role.slot}


def tmux_detach_clients_args(role: RoleConfig) -> list[str]:
    return ["tmux", "detach-client", "-s", role.tmux_session]


def tmux_pane_pid_args(role: RoleConfig) -> list[str]:
    return ["tmux", "display-message", "-p", "-t", role.target, "#{pane_pid}"]


def pane_pid_for_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    proc = runner(tmux_pane_pid_args(role), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return 0
    try:
        return int(str(proc.stdout).strip())
    except ValueError:
        return 0


def worktree_ref(config: ProjectConfig) -> str:
    return f"{config.worktree_remote}/{config.worktree_branch}"


def _proc_failure_reason(proc: subprocess.CompletedProcess[Any], fallback: str) -> str:
    stderr = getattr(proc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str) and stderr.strip():
        # All of it, on one line. Git explains a failure across several lines
        # and the last is often the least of it: a refused fetch ends "and the
        # repository exists.", which is all an operator was shown of "Could not
        # read from remote repository" (SYRD-255).
        return " ".join(line.strip() for line in stderr.strip().splitlines() if line.strip())
    return fallback


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_truthy_any(*names: str) -> bool:
    return any(_env_truthy(name) for name in names)


def _path_owner_label(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        return f"unreadable ({exc})"
    try:
        user = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        user = f"uid {info.st_uid}"
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = f"gid {info.st_gid}"
    return f"{user}:{group}"


def _path_owner_ids(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_uid, info.st_gid


def _process_snapshot() -> tuple[dict[int, int], dict[int, list[int]], dict[int, set[str]], dict[int, list[str]]]:
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
        command_names = {_command_name(parts[2])}
        command_names.update(_command_name(token) for token in argv if _command_name(token))
        names[pid] = command_names
        children.setdefault(ppid, []).append(pid)
    return parents, children, names, argv_by_pid


def process_tree_command_names(pane_pid: int) -> set[str]:
    if pane_pid <= 0:
        return set()
    _parents_by_pid, children_by_parent, process_names, _argv_by_pid = _process_snapshot()
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


def _layout_leaves(node: Any) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    if isinstance(node, dict):
        widgets = node.get("Widgets")
        if isinstance(widgets, list):
            for child in widgets:
                leaves.extend(_layout_leaves(child))
        elif "Command" in node:
            leaves.append(node)
    return leaves


def _running_project_roles(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[RoleConfig]:
    """Which roles have a live session, asked of each role's own tmux server.

    Probing them all through the project owner's server cannot see a role that
    runs as its own account, so restart and liveness both misreport (SYRD-39).
    """
    running_roles: list[RoleConfig] = []
    for role in config.roles:
        role_runner = role_process_runner_for(config, role, runner=runner)
        result = role_runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and live_command_matches_role(role, runner=role_runner):
            running_roles.append(role)
    return running_roles


# The process table this scan reads. A module attribute so the callers that must
# perform it -- start, status, upgrade, replace-window -- do not each need a
# parameter threaded through them, and so tests can point it at a real tree they
# built rather than at a stub of the scan itself.
PROC_ROOT = Path("/proc")


def _proc_effective_uid(proc_root: Path, pid: str) -> int | None:
    """The uid a process is actually running with.

    The effective uid is the one that decides what a shell in that process can
    do, and a process that started as root and kept its privileges reports it
    here. /proc is kernel-provided, so this is not something a process can
    claim about itself (SYRD-43).
    """
    try:
        for line in (proc_root / pid / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                if len(fields) >= 3:
                    return int(fields[2])
    except (OSError, ValueError):
        pass
    try:
        return os.stat(proc_root / pid).st_uid
    except OSError:
        return None


def switchyard_pane_launcher_for(config: ProjectConfig) -> Path:
    """The launcher this project's panes run; the pane window ships beside it."""
    if config.pane_launcher is not None:
        return config.pane_launcher.expanduser()
    return Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME)


def pane_window_program(script_path: Path) -> Path:
    """The inert pane program that ships beside the launcher this pane runs."""
    return Path(script_path).expanduser().resolve(strict=False).with_name(PANE_WINDOW_NAME)


def _owner_state_layout_output_path(project: str, *, owner_home: Path) -> Path:
    return owner_home / ".local" / "state" / "switchyard" / "projects" / project / f"{project}-team-layout.json"


def chown_owner_file_args(config: ProjectConfig, path: Path) -> list[str]:
    if not config.run_as_user:
        raise ValueError("file ownership repair requires run_as_user")
    return ["chown", f"{config.run_as_user}:{config.run_as_user}", str(path)]


def ensure_owner_file(
    config: ProjectConfig,
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    if not config.run_as_user or current_user_name() == config.run_as_user or os.geteuid() != 0:
        return
    result = runner(chown_owner_file_args(config, path))
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"chown failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to assign generated file {path} to {config.run_as_user}: {reason}")


def _legacy_new_project_stacked_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]
    if role_count <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": [
            leaves[0],
            {
                "Orientation": "Vertical",
                "Widgets": leaves[1:],
            },
        ],
    }


def _legacy_new_project_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return _single_row_layout_payload(leaves)
    return _legacy_new_project_sqrt_column_major_layout_payload(role_count)


def _legacy_new_project_sqrt_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    if role_count <= 1:
        return _single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(role_count))
    row_count = math.ceil(role_count / column_count)
    columns: list[dict[str, Any]] = []
    for start in range(0, role_count, row_count):
        column = leaves[start : start + row_count]
        if len(column) == 1:
            columns.append(column[0])
        else:
            columns.append(
                {
                    "Orientation": "Vertical",
                    "Widgets": column,
                }
            )
    return {
        "Orientation": "Horizontal",
        "Widgets": columns,
    }


def _legacy_new_project_chunked_row_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return _single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(len(leaves)))
    rows: list[dict[str, Any]] = []
    for start in range(0, len(leaves), column_count):
        row = leaves[start : start + column_count]
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _known_generated_project_layout_payloads(role_count: int) -> tuple[dict[str, Any], ...]:
    return (
        _new_project_layout_payload(role_count),
        _legacy_new_project_stacked_layout_payload(role_count),
        _legacy_new_project_column_major_layout_payload(role_count),
        _legacy_new_project_sqrt_column_major_layout_payload(role_count),
        _legacy_new_project_chunked_row_major_layout_payload(role_count),
    )


def _is_generated_project_layout_template(config: ProjectConfig, *, config_path: Path) -> bool:
    config_file = config_path.expanduser().resolve(strict=False)
    layout_path = config.layout.expanduser().resolve(strict=False)
    provision_dir = config_file.parent
    return (
        provision_dir.name == "provision"
        and layout_path.parent == provision_dir
        and layout_path.name == f"{config.project}-konsole-layout.json"
    )


def _generated_project_durable_session_dir(config: ProjectConfig) -> Path | None:
    if not config.run_as_user:
        return None
    return Path(_new_project_session_dir(config.project, config.run_as_user))


def upgrade_generated_project_config(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherUpgradeResult:
    if not _is_generated_project_layout_template(config, config_path=config_path):
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout is hand-maintained or outside a provision directory; leaving it unchanged",
    )
    changed_messages: list[str] = []
    durable_session_dir = _generated_project_durable_session_dir(config)
    session_dir_upgrade: Path | None = None
    shared_pane_launcher = switchyard_shared_pane_launcher()
    pane_launcher_upgrade: Path | None = None
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    directorctl_upgrade = str(board_root / "current" / "scripts" / "directorctl") if board_root is not None else ""
    roles_need_directorctl_upgrade = bool(directorctl_upgrade) and any(
        role.env.get("TICKET_BOARD_DIRECTORCTL") != directorctl_upgrade for role in config.roles
    )
    if roles_need_directorctl_upgrade:
        if dry_run:
            changed_messages.append(f"pane directorctl can be pinned to {directorctl_upgrade}")
        else:
            changed_messages.append(f"pinned pane directorctl to {directorctl_upgrade}")
    if (
        config.pane_launcher is not None
        and config.pane_launcher.expanduser().resolve(strict=False)
        != shared_pane_launcher.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"pane launcher can be upgraded from {config.pane_launcher} to {shared_pane_launcher}"
            )
        else:
            pane_launcher_upgrade = shared_pane_launcher
            changed_messages.append(f"upgraded pane launcher to {shared_pane_launcher}")
    if (
        durable_session_dir is not None
        and session_dir_uses_user_runtime(config.session_dir, config.run_as_user)
        and config.session_dir.expanduser().resolve(strict=False) != durable_session_dir.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"session dir can be upgraded from {config.session_dir} to {durable_session_dir}"
            )
        else:
            session_dir_upgrade = durable_session_dir
            changed_messages.append(f"upgraded session dir to {durable_session_dir}")
    role_count = sum(1 for role in config.roles if not role.detached)
    current_layout = _new_project_layout_payload(role_count)
    known_layouts = _known_generated_project_layout_payloads(role_count)
    try:
        existing_layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: cannot read generated layout template {config.layout}: {exc}",
        )
    except json.JSONDecodeError as exc:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: generated layout template {config.layout} is not valid JSON: {exc}",
        )
    if existing_layout != current_layout:
        if existing_layout not in known_layouts:
            if not changed_messages:
                return LauncherUpgradeResult(
                    changed=False,
                    message=f"switchyard: {config.project} layout template differs from the known generated legacy shapes; leaving it unchanged",
                )
            changed_messages.append("layout template differs from the known generated legacy shapes; leaving it unchanged")
        elif dry_run:
            changed_messages.append(f"layout template can be upgraded: {config.layout}")
        else:
            config.layout.write_text(json.dumps(current_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            ensure_owner_file(config, config.layout, runner=runner)
            changed_messages.append(f"upgraded generated layout template: {config.layout}")
    elif not changed_messages:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout template is already current",
        )
    if session_dir_upgrade is not None or pane_launcher_upgrade is not None or roles_need_directorctl_upgrade:
        # Root rewrites this document, so it must be the tenant's own and not a
        # link to somebody else's file (SYRD-228).
        raw_config, config_problem = read_tenant_document_no_follow(
            config_path, what=f"{config.project}'s generated configuration"
        )
        if config_problem:
            return LauncherUpgradeResult(
                changed=False, message=f"switchyard: {config_problem}. Nothing was changed."
            )
        if session_dir_upgrade is not None:
            raw_config["session_dir"] = str(session_dir_upgrade)
        if pane_launcher_upgrade is not None:
            raw_config["pane_launcher"] = str(pane_launcher_upgrade)
        if roles_need_directorctl_upgrade:
            for raw_role in raw_config.get("roles", []):
                if not isinstance(raw_role, dict):
                    continue
                raw_env = raw_role.get("env")
                if not isinstance(raw_env, dict):
                    raw_env = {}
                    raw_role["env"] = raw_env
                raw_env["TICKET_BOARD_DIRECTORCTL"] = directorctl_upgrade
        _write_json_atomic(config_path, raw_config)
        ensure_owner_file(config, config_path, runner=runner)
    return LauncherUpgradeResult(
        changed=not dry_run,
        message=f"switchyard: upgraded generated project config for {config.project}: {'; '.join(changed_messages)}",
    )


def upgrade_generated_project_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherUpgradeResult:
    return upgrade_generated_project_config(config, config_path=config_path, dry_run=dry_run, runner=runner)


def publish_tenant_artifact(
    config: ProjectConfig, provision_dir: Path, name: str, body: bytes
) -> tuple[bool, str]:
    """Write the tenant's copy of a generated file without following anything.

    These are untrusted output: root produces them for the tenant's own tooling
    and never reads them back to decide what root installs. The write still runs
    as root, so every path component is opened with O_NOFOLLOW and the file is
    replaced by rename rather than truncated in place -- a symlink left at the
    destination is replaced, never followed to its referent (SYRD-39).
    """
    destination = provision_dir / name
    relative = Path(str(provision_dir).lstrip("/")) / name
    dir_fd, problem = _walk_no_follow(Path(provision_dir.anchor or "/"), relative)
    if dir_fd < 0:
        return False, f"switchyard: refusing to write {destination}: {problem}"
    mode = 0o755 if name.endswith(".sh") else 0o644
    staged = f".{name}.new"
    try:
        descriptor = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode, dir_fd=dir_fd
        )
        try:
            os.write(descriptor, body)
            os.fchmod(descriptor, mode)
            if os.geteuid() == 0 and config.run_as_user:
                try:
                    owner = pwd.getpwnam(config.run_as_user)
                except KeyError:
                    owner = None
                if owner is not None:
                    os.fchown(descriptor, owner.pw_uid, owner.pw_gid)
        finally:
            os.close(descriptor)
        os.rename(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        return False, f"switchyard: could not write {destination}: {exc}"
    finally:
        os.close(dir_fd)
    return True, ""


PRIVILEGED_PROVISION_ROOT_ENV = "SWITCHYARD_PRIVILEGED_PROVISION_ROOT"


def switchyard_privileged_provision_root() -> Path:
    """Where root keeps the copies of generated artifacts it installs from.

    Overridable so tests can exercise the real root branch without writing to
    the host's /etc (SYRD-39).
    """
    configured = os.environ.get(PRIVILEGED_PROVISION_ROOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PRIVILEGED_PROVISION_ROOT


def privileged_baseline_plan_path(project: str) -> Path:
    """Root's own copy of the plan, the only one it renders privileged artifacts from."""
    return privileged_provision_dir(project, root=switchyard_privileged_provision_root()) / "plan.json"


def workflow_record_path(project: str) -> Path:
    from scripts.ticket_board.project_provision import WORKFLOW_RECORD_NAME

    return privileged_baseline_plan_path(project).with_name(WORKFLOW_RECORD_NAME)


def recorded_declared_workflow(project: str) -> tuple[dict | None, str]:
    """The declared workflow root holds for this project, or why it holds none.

    Root's own copy, read the way root reads its other authorities: by fd,
    refusing a symlink at every component, and required to belong to root and
    to be unwritable by anybody else. The tenant's configuration carries this
    document too, and that copy is not consulted here -- it decides which roles
    exist and what each of them may call, so a copy the account every role runs
    as can write is a copy that account could grant itself with (SYRD-165).
    """
    from scripts.ticket_board.project_provision import workflow_record_document

    path = workflow_record_path(project)
    try:
        # Absence and unusability are different answers, and asked separately
        # rather than read out of the wording of a refusal.
        path.lstat()
    except FileNotFoundError:
        return None, f"root holds no recorded workflow for {project}"
    except OSError as exc:
        return None, f"root's workflow record for {project} could not be read: {exc}"
    document, problem = read_plan_no_follow(path, require_root_owned=True)
    if document is None:
        return None, problem
    return workflow_record_document(document.data, project=project)


def render_privileged_artifacts(
    plan: ProjectBoardProvision, *, enable_owner_linger: bool = False
) -> dict[str, bytes]:
    """Render everything root installs, in a directory only root can reach.

    Rendering into root's own temporary directory rather than reading the
    tenant's copies back means the bytes root publishes are the bytes root
    generated, with no window in which they could be replaced (SYRD-39).
    """
    with tempfile.TemporaryDirectory(prefix=f"switchyard-{plan.project}-privileged.") as tmp:
        staged = Path(tmp)
        os.chmod(staged, 0o700)
        write_artifacts(plan, staged, enable_owner_linger=enable_owner_linger)
        return {
            name: (staged / name).read_bytes()
            for name in (*privileged_artifact_names(plan), "plan.json")
            if (staged / name).is_file()
        }


#: A tenant's root-owned provisioning directory, and what is in it. Together
#: the plan, the operator packet, the database and workflow SQL, the workflow
#: record and the publication remote describe the whole authority model of a
#: tenant -- which accounts exist, what each may call, where the board's socket
#: and database are. None of that is a secret root shares with the accounts it
#: is about, so the directory is root's alone and the files inside it are too:
#: a mode that leans on the directory is one chmod away from being nothing
#: (SYRD-176, syrd rollout journal 0084).
PRIVILEGED_PROVISION_DIR_MODE = 0o700
PRIVILEGED_ARTIFACT_MODE = 0o600
PRIVILEGED_EXECUTABLE_ARTIFACT_MODE = 0o700


def privileged_artifact_mode(name: str) -> int:
    """The least a root-owned artifact needs to be what its consumer runs.

    Root executes the packets and the migration script and reads everything
    else; nobody else does either, so nothing here is readable beyond root.
    """
    return PRIVILEGED_EXECUTABLE_ARTIFACT_MODE if name.endswith(".sh") else PRIVILEGED_ARTIFACT_MODE


def ensure_privileged_provision_dir(target: Path, *, root: Path | None = None) -> list[str]:
    """Create or repair a tenant's root-only provisioning directory.

    Returns what it had to repair, so a caller can tell an operator what
    changed. Every writer under this directory goes through here, because the
    invariant is only as good as the last thing that created the directory --
    it was documented as root-only while one of these writers chmod'd it 0755
    on every run (SYRD-176).

    Refusals are loud. A directory that is a symlink, or that belongs to
    somebody other than root, is not one root may publish a plan into: closing
    it would be closing whatever it points at, and writing to it would be
    writing where its owner can read and replace what root then installs.
    """
    base = Path(root) if root is not None else switchyard_privileged_provision_root()
    owner = expected_privileged_uid()
    repaired: list[str] = []
    for directory in (*reversed(target.parents), target):
        private = directory == target
        if not (private or directory.is_relative_to(base)):
            continue
        try:
            info = directory.lstat()
        except FileNotFoundError:
            directory.mkdir(mode=PRIVILEGED_PROVISION_DIR_MODE if private else 0o755)
            info = directory.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SystemExit(
                f"switchyard: {directory} is a symlink, so it is not a directory root will "
                f"publish {target.name}'s provisioning artifacts into. Nothing was written."
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit(
                f"switchyard: {directory} is not a directory, so root's provisioning artifacts "
                "have nowhere to go. Nothing was written."
            )
        if info.st_uid != owner:
            raise SystemExit(
                f"switchyard: {directory} is owned by uid {info.st_uid} rather than by root, so "
                "what root publishes there would be its owner's to read and replace. Nothing "
                "was written."
            )
        wanted = PRIVILEGED_PROVISION_DIR_MODE if private else 0o755
        if stat.S_IMODE(info.st_mode) != wanted:
            if private:
                repaired.append(
                    f"closed {directory} to root only (was mode "
                    f"{stat.S_IMODE(info.st_mode):04o})"
                )
            directory.chmod(wanted)
        try:
            os.chown(directory, 0, 0)
        except OSError:
            # Attempted, not relied on. What decides whether root publishes here
            # is the ownership check above, which reads the directory back --
            # and through the documented provision-root seam "root" is the
            # caller's own uid, which it cannot chown away from itself.
            pass
    return repaired


def install_privileged_artifacts(
    plan: ProjectBoardProvision,
    rendered: dict[str, bytes],
    *,
    privileged_root: Path | None = None,
) -> Path:
    """Publish rendered bytes into a directory only root can reach.

    The provision directory belongs to the tenant, and a directory's owner can
    replace what is inside it, so root neither writes there nor reads back from
    there: these bytes come straight from the render above. Each file is written
    beside its target and renamed, so an operator never reads a half-written
    unit (SYRD-39).

    "Only root can reach" is enforced here rather than described: the directory
    is closed to root alone and each artifact is written at the least its
    consumer needs, which for all of them is root and nobody else (SYRD-176).
    """
    base = privileged_root or switchyard_privileged_provision_root()
    target = privileged_provision_dir(plan.project, root=base)
    ensure_privileged_provision_dir(target, root=base)
    for name, body in rendered.items():
        staged = target / f".{name}.new"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, body)
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, privileged_artifact_mode(name))
        finally:
            os.close(descriptor)
        staged.replace(target / name)
    return target


def _tenant_board_root_from_config(config: ProjectConfig) -> Path | None:
    if config.pane_launcher is None:
        return None
    launcher = config.pane_launcher.expanduser()
    if shared_switchyard_release_for_path(launcher) is not None:
        return None
    for parent in launcher.parents:
        if parent.name == "current":
            return parent.parent
    return None


def _tenant_board_root_from_config_or_plan(config: ProjectConfig, config_path: Path | None = None) -> Path | None:
    if config_path is not None:
        plan_data = _plan_data_from_config(config, config_path)
        board_root = plan_data.get("board_root")
        if board_root:
            return Path(str(board_root)).expanduser()
    return _tenant_board_root_from_config(config)


def _read_deploy_sha_marker(path: Path) -> str:
    try:
        return (path / ".pgu-deploy-sha").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_tenant_release(board_root: Path) -> tuple[Path | None, str]:
    current_link = board_root / "current"
    if not current_link.exists() and not current_link.is_symlink():
        return None, ""
    current_release = current_link.resolve(strict=False)
    current_sha = _read_deploy_sha_marker(current_link)
    if not current_sha:
        current_sha = _read_deploy_sha_marker(current_release)
    if not current_sha and current_release.name:
        current_sha = current_release.name
    return current_release, current_sha


def tenant_release_deploy_command(status: TenantReleaseStatus, project: str) -> str:
    deploy_env = [
        f"TICKET_BOARD_OWNER_HOME={status.owner_home}",
        f"TICKET_BOARD_PROJECT={project}",
        f"BOARD_ROOT={status.board_root}",
        f"DEPLOY_REF={status.deploy_ref}",
    ]
    # Where this tenant's board listens, named rather than defaulted.
    #
    # ticket-board-service.sh falls back to BOARD_PORT=8770, which on a
    # multi-tenant host is another tenant's board and is listening. The deploy's
    # own health gates then probed that board: the HTTP smoke passed against
    # http://127.0.0.1:8770/api/board, and the build-id check spent its whole
    # ten-second budget comparing a foreign board's build to this release's
    # before rolling back a syrd process that had started correctly. A gate that
    # can pass by reaching somebody else's service is not a check on this one
    # (SYRD-87 R6).
    if status.board_port:
        deploy_env.append(f"BOARD_PORT={status.board_port}")
    if status.board_socket:
        deploy_env.append(f"BOARD_UNIX_SOCKET={status.board_socket}")
    if status.provisioned_system_unit is not None:
        # The readable copy, never root's own. The deployer runs as the project
        # account and root's copy lives in the privileged provision directory,
        # which is root-only: handing over that path made a present, correct,
        # byte-identical unit read as ABSENT and refused the release (SYRD-126).
        # The copy is published by the artifacts phase from exactly that file,
        # so the comparison it feeds is still release-against-installed rather
        # than a file against itself (SYRD-127).
        from scripts.ticket_board.project_provision import readable_system_unit_path

        deploy_env.append(
            "TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="
            + readable_system_unit_path(project)
        )
    if status.commit_git_dir:
        deploy_env.append(f"TICKET_BOARD_COMMIT_GIT_DIR={status.commit_git_dir}")
    if status.clone_source_repo is not None:
        marker = json.dumps({"commit": status.target_sha}, separators=(",", ":"))
        script = (
            'tmpdir="$(mktemp -d)"'
            f" && git --git-dir={shlex.quote(str(status.clone_source_repo))} archive {shlex.quote(status.target_sha)}"
            ' | tar -x -C "$tmpdir"'
            f" && printf '%s\\n' {shlex.quote(marker)} >\"$tmpdir/{SWITCHYARD_RELEASE_MARKER_NAME}\""
            " && env "
            + " ".join(shlex.quote(value) for value in deploy_env)
            + ' SOURCE_REPO="$tmpdir" "$tmpdir/scripts/ticket-board-service.sh" deploy-restart'
        )
        return _quote_command(_owner_boundary_env_args(status.owner_user, status.owner_home, ["sh", "-c", script]))
    service_script = status.source_repo / "scripts" / "ticket-board-service.sh"
    return _quote_command(
        _owner_boundary_env_args(
            status.owner_user,
            status.owner_home,
            ["env", *deploy_env, f"SOURCE_REPO={status.source_repo}", str(service_script), "deploy-restart"],
        )
    )


def tenant_release_listener_command(status: TenantReleaseStatus, project: str, action: str) -> str:
    if action not in {"stop", "start"}:
        raise ValueError(f"unsupported listener action: {action}")
    listener_unit = f"{project}-ticket-board-notify-listener.service"
    operation = f"systemctl --user {action} {shlex.quote(listener_unit)}"
    if action == "start":
        operation = f"systemctl --user daemon-reload && {operation}"
    # Exported: `start` reloads first, and a prefix would leave the start
    # itself without a bus to talk to (SYRD-61).
    script = (
        'runtime="/run/user/$(id -u)"; '
        'export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
        + operation
    )
    # The same boundary as the deploy: an operator pastes this line into the
    # same shell, and a `systemctl --user` that lands on root's manager stops
    # nothing and starts nothing (SYRD-138).
    command = _owner_boundary_env_args(
        status.owner_user,
        status.owner_home,
        ["sh", "-c", script],
    )
    return _quote_command(command)


def _rollout_recorder_path() -> Path | None:
    """The recorder root should execute, or None when there is not one to run."""
    installed = switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    if installed.is_file():
        return installed
    checkout = _repo_root() / "scripts" / "switchyard-record-rollout"
    return checkout if checkout.is_file() else None


def recorded_provisioning_command(project: str, script_name: str) -> str:
    """How an operator runs the provisioning packet so it records itself.

    `script_name` is a path, and callers pass an absolute one: the packet is run
    from a journal, a Polkit transaction or whatever directory the operator was
    in, and none of those is a promise about the cwd (SYRD-149).
    """
    recorder = _rollout_recorder_path()
    if recorder is None:
        return f"bash {shlex.quote(script_name)}"
    return " ".join(
        [
            "sudo",
            shlex.quote(str(recorder)),
            shlex.quote(project),
            "--label",
            "provisioning",
            "--",
            "bash",
            shlex.quote(script_name),
        ]
    )


def recorded_rollout_command(
    status: TenantReleaseStatus, project: str, command: str, *, label: str = ""
) -> str:
    """One privileged step, run so that it leaves a record root owns.

    The step itself is unchanged -- it is handed to the recorder rather than
    rewritten -- so what an operator runs is still exactly what was reviewed,
    and what survives it is a root-owned attempt directory instead of a log
    under the account every role shares (SYRD-128).
    """
    recorder = str(_repo_root() / "scripts" / "switchyard-record-rollout")
    installed = switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    if installed.is_file():
        # The installed release's copy, not this checkout's: the operator is
        # running a release, and root should execute root-owned bytes.
        recorder = str(installed)
    prefix = ["sudo", recorder, project]
    if status.target_sha:
        prefix += ["--target-commit", status.target_sha]
    if label:
        prefix += ["--label", label]
    # The step goes in whole, through one `bash -c`, because it is a command
    # LINE: it carries its own quoting and its own `&&` chain, and leaving the
    # chain outside the recorder would record the first command and run the
    # rest unrecorded -- which is the opposite of the point.
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(command)]
    )


def tenant_release_unit_install_command(status: TenantReleaseStatus, project: str) -> str:
    from scripts.ticket_board.project_provision import system_unit_proof_chain

    if status.provisioned_system_unit is None:
        return ""
    provision_dir = status.provisioned_system_unit.parent
    board_unit = status.provisioned_system_unit
    canary_unit = provision_dir / f"{project}-ticket-board-canary.service"
    listener_unit = provision_dir / f"{project}-ticket-board-notify-listener.service"
    listener_target = status.owner_home / ".config" / "systemd" / "user" / listener_unit.name
    return " && ".join(
        [
            _quote_command(["sudo", "install", "-m", "0644", str(board_unit), f"/etc/systemd/system/{board_unit.name}"]),
            _quote_command(["sudo", "install", "-m", "0644", str(canary_unit), f"/etc/systemd/system/{canary_unit.name}"]),
            _quote_command(
                [
                    "sudo",
                    "install",
                    "-m",
                    "0644",
                    "-o",
                    status.owner_user,
                    "-g",
                    status.owner_user,
                    str(listener_unit),
                    str(listener_target),
                ]
            ),
            # The same reviewed bytes, published where the unprivileged
            # deployer can read them. In this chain rather than a step of its
            # own so the copy cannot exist without the install having happened,
            # and cannot be stale relative to it (SYRD-127).
            *system_unit_proof_chain(project, _quote_command([str(board_unit)])),
            _quote_command(["sudo", "systemctl", "daemon-reload"]),
        ]
    )


def release_update_blocked(status: "TenantReleaseStatus | None") -> str:
    """Why no safe release update can be produced, or "" when one can.

    One reading, used both by the report an operator sees and by the exit status
    the caller returns. Printing `cannot produce a safe release update` and then
    exiting 0 is what let a bounded wrapper announce REAL UPGRADE COMPLETE over a
    board that had not moved (SYRD-100 review).
    """
    if status is None:
        return ""
    if not status.target_sha:
        return status.resolve_error or f"{status.deploy_ref} did not resolve"
    if not status.unchanged and status.provisioned_system_unit is None:
        return "generated board, canary, and listener units are incomplete"
    return ""


def _format_release_sha(sha: str) -> str:
    return sha if sha else "(none)"


def _format_release_path(path: Path | None) -> str:
    return str(path) if path is not None else "(none)"


def report_tenant_release_upgrade(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> TenantReleaseStatus | None:
    """Report the tenant's release, and return what was found.

    The status is returned because the caller has to record a phase from the same
    reading: the identities transaction now switches the release itself, so whether
    a deploy is still owed is exactly `status.unchanged`, and asking twice would be
    two readings of a tree that can move between them (SYRD-48).
    """
    status = tenant_release_status(
        config,
        config_path=config_path,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
    )
    if status is None:
        return None
    print_func(
        f"switchyard: {config.project} deployed board release old: "
        f"{_format_release_sha(status.current_sha)} at {_format_release_path(status.current_release)}"
    )
    if status.target_sha:
        print_func(f"switchyard: {config.project} deployed board release new: {status.target_sha} from {status.deploy_ref}")
    else:
        print_func(
            f"switchyard: {config.project} deployed board release new: "
            f"(unresolved {status.deploy_ref}: {status.resolve_error})"
        )
    blocked = release_update_blocked(status)
    if not status.target_sha:
        print_func(
            f"switchyard: cannot produce a safe release update for {config.project}: {blocked}"
        )
    elif status.unchanged:
        print_func(f"switchyard: {config.project} deployed board release unchanged; no release deploy needed")
    else:
        if status.provisioned_system_unit is None:
            print_func(
                f"switchyard: cannot produce a safe release update for {config.project}: {blocked}"
            )
            return status
        print_func("switchyard: matching-release deployment sequence (keep the listener stopped through migrations):")
        print_func(f"  {tenant_release_listener_command(status, config.project, 'stop')}")
        unit_install = tenant_release_unit_install_command(status, config.project)
        if unit_install:
            privileged_root = switchyard_privileged_provision_root()
            if status.provisioned_system_unit.is_relative_to(privileged_root):
                print_func(f"  {recorded_rollout_command(status, config.project, unit_install, label='install units')}")
            else:
                # Not printed at all, rather than printed with a warning above
                # it. This step is executed by root through the recorder, and
                # its source is a directory the tenant can write: printing it
                # asks an operator to install a unit file that anyone with the
                # tenant account could have rewritten between the render and
                # the run. The warning was already here and was not enough --
                # on SYRD-137 the step had to be recognised as unsafe and
                # skipped by hand, against a copy four days stale that would
                # have stripped the live board's socket-group confinement
                # (SYRD-138).
                print_func(
                    f"switchyard: omitting the unit-install step for {config.project}: it would "
                    f"install from {status.provisioned_system_unit.parent}, which the tenant owns, "
                    f"and the step runs as root. Run `switchyard upgrade {config.project}` as root "
                    "to stage a root-owned copy, then re-render this sequence."
                )
        print_func(
            f"  {recorded_rollout_command(status, config.project, tenant_release_deploy_command(status, config.project), label='deploy-restart')}"
        )
        print_func(f"  {tenant_release_listener_command(status, config.project, 'start')}")
        # The step that closes the phase, in the sequence, recorded like the
        # rest. It runs last because it re-proves the end state -- the listener
        # is back, the board is serving the release its own link names -- and a
        # close that ran before the listener was restored would be closing over
        # a system the deploy had not finished putting back (SYRD-117).
        print_func(
            f"  {recorded_rollout_command(status, config.project, _quote_command(['switchyard', 'release-status', config.project, '--close']), label='close release')}"
        )
        # What to do when a step fails, said before anybody needs it. On mefp the
        # deploy failed with the listener already stopped, and nothing said
        # whether bringing it back was safe (SYRD-231).
        print_func(
            f"switchyard: if a step fails, stop there. `switchyard release-status {config.project}` "
            "says which release the board is serving: if it is still the one it had, the deploy "
            "stopped before activation and the listener can be brought back with "
            f"`{tenant_release_listener_command(status, config.project, 'start')}`; then run "
            f"`sudo switchyard upgrade {config.project}` to repair what stopped it and re-render "
            "this sequence."
        )
        print_func(
            f"switchyard: each recorded step prints where its record is; read them with "
            f"`switchyard rollout-log {config.project}` -- the journal is root-owned and every "
            "role may read it without sudo, so nobody has to paste output (SYRD-128)."
        )
        print_func(
            "switchyard: panes must be restarted after the release update to pick up hook installer, "
            "hook binary, or pane launcher changes; this command does not restart panes"
        )
    return status


def _privileged_upgrade_check_command(project: str, deploy_ref: str | None) -> str:
    """The boundary command that runs this upgrade's checks as root, read-only."""
    commit = deploy_ref if deploy_ref and re.fullmatch(r"[0-9a-f]{40}", deploy_ref) else "<release commit>"
    return f"switchyard privileged-action {project} preview-upgrade commit={commit}"


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
    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    if not dry_run and config.role_state_isolation:
        compatible, reason = process_authority_board_compatibility(config)
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
        if migrate_declarative_director_onboarding(
            config, config_path=config_path, print_func=print_func
        ):
            # The migration rewrote the projection on disk, so the config loaded before
            # it is now stale. Every later path -- session sync, detached restarts, the
            # viewer -- reads role env off this object, and would otherwise restart roles
            # without the prompt that was just projected.
            config = load_project_config(config.project, config_path)
    launch_setup = _launch_runners_and_paths(
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
        upgrade_result = upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
        if upgrade_result.changed:
            print_func(upgrade_result.message)
            config = load_project_config(config.project, config_path)
    if not dry_run and mode != "attach":
        config = prepare_project_desktop(config, runner=runner)
    if not dry_run:
        pane_script_path = _verify_pane_launcher_path(config, script_path=script_path, runner=worktree_runner)
    launch_preparation = _prepare_launch(
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
    layout_exit = _write_layout_and_plan(
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
    worker_startup = _start_workers_and_present(
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
    if not isinstance(worker_startup, WorkerStartup):
        return worker_startup
    worker_start_exit_code = worker_startup.worker_start_exit_code
    launch_started_at = worker_startup.launch_started_at
    launch_started_ns = worker_startup.launch_started_ns
    resolved_layout_mode = worker_startup.resolved_layout_mode
    # A window opened by an earlier release can still be running as root, and
    # the tenant cannot signal it. Saying the project is attached while that is
    # true would be the wrong report to act on (SYRD-43).
    return _report_launch(
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


def _default_new_project_owner(project: str) -> str:
    return f"{project}-agent"


def _read_prompt(
    prompt: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    try:
        return input_func(prompt)
    except EOFError:
        raise SystemExit("switchyard: no input available") from None


def _prompt_text(
    label: str,
    *,
    default: str = "",
    input_func: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    value = _read_prompt(f"{label}{suffix}: ", input_func=input_func).strip()
    return value or default


def _prompt_bool(
    label: str,
    *,
    default: bool,
    input_func: Callable[[str], str] = input,
) -> bool:
    default_text = "Y/n" if default else "y/N"
    for _attempt in range(SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = _read_prompt(f"{label} [{default_text}]: ", input_func=input_func).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print("answer yes or no")
    raise SystemExit(f"switchyard: too many invalid answers for {label}")


def _runtime_choices() -> tuple[Choice, ...]:
    """The runtimes, in the order `switchyard new` offers them.

    Taken from the catalog and narrowed to what `switchyard new` supports, so
    the list an operator sees cannot drift from the list the validator accepts.
    """
    described = {choice.value: choice for choice in runtime_catalog.RUNTIMES}
    return tuple(
        described.get(name, Choice(name)) for name in SUPPORTED_NEW_PROJECT_CLIS
    )


def _runtime_field(role: str, *, default: str, configured: str = "") -> Field:
    choices = _runtime_choices()
    if configured:
        choices = with_existing_value(choices, configured)
    return Field(
        name="runtime",
        kind=KIND_SINGLE,
        title=f"{role} runtime",
        choices=choices,
        default=configured or default,
    )


def _prompt_cli(
    role: str,
    *,
    default: str,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> str:
    """Choose a role's runtime from the list, rather than recall one.

    This used to render the alternatives into the prompt text -- `director CLI
    (claude/codex/agy/hermes)` -- and read back whatever was typed. The set was
    always finite and always known; it just was not shown as a set (SYRD-115).
    """
    default_cli = _validate_new_project_cli(default, context=f"default CLI for {role}")
    try:
        return terminal_select.select_one(
            _runtime_field(role, default=default_cli),
            input_func=input_func,
            print_func=print_func,
        )
    except terminal_select.Cancelled:
        raise SystemExit(f"switchyard: too many invalid answers for {role} CLI") from None


@dataclass(frozen=True)
class RoleSelection:
    """One role, fully chosen: what runs it, on which model, at what effort."""

    role: str
    cli: str
    model: str = ""
    effort: str = ""


def _implementer_roles_field() -> Field:
    """The conventional roles as things to pick, not a string to compose.

    The list was already printed -- and then the answer was read as one
    comma-separated line, so a typo in the middle of it was a role nobody asked
    for and a role nobody noticed was missing. The same names, selectable, with
    the conventional pair as the default and a deliberate path to a role of
    one's own (SYRD-115).
    """
    def validate(value: str) -> str:
        return _validate_new_project_implementer_role(value, context="implementer role")

    return Field(
        name="roles",
        kind=KIND_MULTI,
        title="Implementer roles",
        choices=tuple(
            Choice(role, role, description)
            for role, description in NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES
        ),
        default=tuple(NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES),
        allow_custom=True,
        custom_title="A role of your own",
        validate=validate,
    )


def _prompt_role_runtime_plan(
    role: str,
    *,
    default_cli: str,
    configured: RoleSelection | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    owner_args: Sequence[str] = (),
    unverified_because: str = "",
    interactive: bool = True,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> RoleSelection:
    """One guided path for a role: runtime, then model, then effort.

    In that order because each one narrows the next. The models offered are the
    chosen runtime's, and the effort question is not asked at all for a runtime
    that discards it -- `agy` drops an effort level before it reaches the
    command line, and a question whose answer is thrown away should not be
    asked (SYRD-115).
    """
    held = configured or RoleSelection(role=role, cli="")
    schema = Schema(
        (
            _runtime_field(role, default=default_cli, configured=held.cli),
            _model_field(
                role, configured=held.model, runner=runner,
                owner_args=owner_args, unverified_because=unverified_because,
                print_func=print_func,
            ),
            _effort_field(role, configured=held.effort),
        )
    )
    answers: dict[str, Any] = {}
    runtime_field, model_field, effort_field = schema.fields
    answers["runtime"] = terminal_select.select_one(
        runtime_field, answers, interactive=interactive,
        input_func=input_func, print_func=print_func,
    )
    model = ""
    if model_field.choices_for(answers) or model_field.allow_custom:
        model = terminal_select.select_one(
            model_field, answers, interactive=interactive,
            input_func=input_func, print_func=print_func,
        )
    answers["model"] = model
    effort = ""
    if runtime_catalog.runtime_takes_effort(answers["runtime"]):
        effort = terminal_select.select_one(
            effort_field, answers, interactive=interactive,
            input_func=input_func, print_func=print_func,
        )
    return RoleSelection(role=role, cli=answers["runtime"], model=model, effort=effort)


def _prompt_switchyard_role_plan(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    #: Whose CLI context the model lists come from. A catalog is a property of
    #: an ACCOUNT, not of a host: `agy models` on the operator's login and on
    #: the tenant owner's are different lists, and the one that matters is the
    #: owner's, because that is the account the role will run as. Asking the
    #: wrong one is how `test2` was configured with a slug its own owner does
    #: not recognise (SYRD-250).
    owner_user: str = "",
    owner_home: Path | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[RoleSelection, ...]:
    """Every role of a new project, chosen rather than typed."""
    # The account usually does not exist yet: `switchyard new` chooses its
    # roles before it creates anybody, and it must keep choosing before it
    # creates anybody -- nothing may be mutated before the plan review. So the
    # owner-scoped list is simply not available here, and the honest thing is
    # to say which account could not be asked and that the choice will be
    # confirmed once it can be. Pretending otherwise is what shipped the first
    # time: the recorded fallback offered `gemini-3.7-flash-high`, the very
    # slug test2 was misconfigured with (SYRD-250 DAT).
    owner_exists = _owner_account_exists(owner_user)
    owner_args = (
        _owner_command_env_args(owner_user, owner_home, [])
        if owner_user and owner_home is not None
        else ()
    )
    # One guard, and it is this one. When the owner cannot be asked, NOBODY is
    # asked: leaving the runner in place would enumerate whoever is TYPING and
    # label their models "listed in this account" -- the original defect, moved
    # into the code meant to fix it. Withholding the runner is what makes that
    # impossible; withholding the prefix as well would only look careful.
    catalog_runner = runner if owner_exists else None
    unverified_because = (
        f"the {owner_user} account does not exist yet, so its own list could not be read; "
        f"this choice is confirmed against it after the account is created"
        if owner_user and not owner_exists
        else ""
    )
    include_designer = _prompt_bool("Include designer role", default=True, input_func=input_func)
    include_audit = _prompt_bool("Include audit role", default=True, input_func=input_func)
    fixed: list[str] = []
    if include_designer:
        fixed.append("designer")
    fixed.append("director")
    if include_audit:
        fixed.append("audit")

    try:
        implementers = terminal_select.select_many(
            _implementer_roles_field(), input_func=input_func, print_func=print_func
        )
    except terminal_select.Cancelled:
        raise SystemExit("switchyard: too many invalid answers for implementer roles") from None
    if not implementers:
        raise SystemExit("switchyard: at least one implementer role is required")

    plan: list[RoleSelection] = []
    for role in fixed:
        plan.append(
            _prompt_role_runtime_plan(
                role,
                default_cli=NEW_PROJECT_ROLE_CLI_DEFAULTS.get(role, "claude"),
                runner=catalog_runner, owner_args=owner_args,
                unverified_because=unverified_because,
                input_func=input_func, print_func=print_func,
            )
        )
    for role in implementers:
        plan.append(
            _prompt_role_runtime_plan(
                role, default_cli="codex", runner=catalog_runner, owner_args=owner_args,
                unverified_because=unverified_because,
                input_func=input_func, print_func=print_func,
            )
        )
    return tuple(plan)


def _prompt_switchyard_role_choices(
    *,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[tuple[str, str], ...]:
    """The role/runtime pairs, for callers that want only those."""
    return tuple(
        (selection.role, selection.cli)
        for selection in _prompt_switchyard_role_plan(
            input_func=input_func, print_func=print_func
        )
    )


def _comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_project_artifact_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}.project.json"


def _default_project_design_document_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}-design.md"


def _project_design_markdown(project: str, *, title: str, body: str) -> str:
    heading = title.strip() or f"{project} design"
    body_text = body.strip() or "TBD."
    return f"# {heading}\n\n{body_text}\n"


def design_project_command(
    project: str,
    *,
    output_dir: Path | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    project_name: str | None = None,
    design_title: str | None = None,
    design_body: str | None = None,
    repository: Path | None = None,
    remote: str | None = None,
    default_branch: str | None = None,
    worktree_policy: str | None = None,
    owner_user: str | None = None,
    ticket_prefix: str | None = None,
    implementer_roles: Sequence[str] | None = None,
    push_policy: str | None = None,
    audit_signoff: bool | None = None,
    audit_roles: Sequence[str] | None = None,
    needs_inspection: bool | None = None,
    needs_user_signoff: bool | None = None,
    board_service_traversal: bool | None = None,
    supplementary_groups: Sequence[str] | None = None,
    linger: bool | None = None,
    owner_shell: str | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    project_slug = _validate_project_slug(project)
    base_dir = (output_dir or Path.cwd()).expanduser().resolve(strict=False)
    target_artifact = (artifact_path or _default_project_artifact_path(project_slug, base_dir)).expanduser().resolve(strict=False)
    target_design_document = (
        design_document or _default_project_design_document_path(project_slug, target_artifact.parent)
    ).expanduser().resolve(strict=False)

    title = design_title if design_title is not None else _prompt_text("Design document title", default=f"{project_slug} design", input_func=input_func)
    resolved_project_name = (project_name or title or project_slug).strip()
    body = design_body if design_body is not None else _prompt_text("Design summary", input_func=input_func)
    repo = repository or Path(_prompt_text("Code location", input_func=input_func))
    resolved_remote = remote or _prompt_text("Remote", default="origin", input_func=input_func)
    resolved_branch = default_branch or _prompt_text("Default branch", default="main", input_func=input_func)
    # A declared set, so it is shown as one. Push policy and owner shell are
    # left as text on purpose: neither has a vocabulary anywhere in this
    # codebase, and a selector whose options somebody invented to fill the list
    # out is worse than a text field -- it looks authoritative (SYRD-115).
    resolved_policy = worktree_policy or terminal_select.select_one(
        Field(
            name="worktree_policy",
            kind=KIND_SINGLE,
            title="Worktree policy",
            choices=(
                Choice("shared", "shared", "every role works in one checkout"),
                Choice("isolated", "isolated", "a worktree per role"),
            ),
            default="shared",
        ),
        input_func=input_func,
        print_func=print_func,
    )
    if resolved_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"worktree policy must be one of {sorted(WORKTREE_POLICIES)}")
    resolved_owner = _owner_user_verbatim(
        owner_user if owner_user is not None else _prompt_text("Owner user", default=_default_new_project_owner(project_slug), input_func=input_func)
    )
    resolved_prefix = validate_ticket_prefix(
        ticket_prefix or _prompt_text("Ticket prefix", default=project_slug.upper(), input_func=input_func)
    )
    resolved_push_policy = push_policy or _prompt_text("Push policy", default="director-main-only", input_func=input_func)
    resolved_audit_roles = _dedupe_role_names(
        tuple(_validate_new_project_audit_role(role) for role in (audit_roles or ("audit",)))
    )
    resolved_implementer_roles = tuple(implementer_roles or DEFAULT_PROJECT_IMPLEMENTER_ROLES)
    role_overlap = set(resolved_implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    gates = {
        "audit_signoff": audit_signoff
        if audit_signoff is not None
        else _prompt_bool("Require audit signoff gate", default=PROJECT_DESIGN_DEFAULT_GATES["audit_signoff"], input_func=input_func),
        "needs_inspection": needs_inspection
        if needs_inspection is not None
        else _prompt_bool("Enable inspection gate by default", default=PROJECT_DESIGN_DEFAULT_GATES["needs_inspection"], input_func=input_func),
        "needs_user_signoff": needs_user_signoff
        if needs_user_signoff is not None
        else _prompt_bool("Enable user signoff gate by default", default=PROJECT_DESIGN_DEFAULT_GATES["needs_user_signoff"], input_func=input_func),
    }
    grants = {
        "board_service_traversal": board_service_traversal
        if board_service_traversal is not None
        else _prompt_bool("Grant board-service traversal into the owner home", default=True, input_func=input_func),
        "supplementary_groups": list(supplementary_groups)
        if supplementary_groups is not None
        else _comma_list(_prompt_text("Supplementary groups (comma-separated, blank for none)", input_func=input_func)),
        "linger": linger
        if linger is not None
        else _prompt_bool("Enable linger for the owner user", default=True, input_func=input_func),
        "shell": owner_shell or _prompt_text("Owner shell", default="fish", input_func=input_func),
    }
    artifact = ProjectDesignArtifact(
        project=project_slug,
        project_name=resolved_project_name,
        ticket_prefix=resolved_prefix,
        owner_user=resolved_owner,
        repository=repo.expanduser().resolve(strict=False),
        remote=resolved_remote,
        default_branch=resolved_branch,
        worktree_policy=resolved_policy,
        design_document=target_design_document,
        implementer_roles=resolved_implementer_roles,
        audit_roles=resolved_audit_roles,
        role_clis=_default_role_cli_pairs(
            resolved_implementer_roles,
            include_designer=True,
            include_audit=True,
            audit_roles=resolved_audit_roles,
        ),
        include_designer=True,
        include_audit=bool(resolved_audit_roles),
        push_policy=resolved_push_policy,
        gates=gates,
        capability_grants=grants,
    )
    target_design_document.parent.mkdir(parents=True, exist_ok=True)
    target_design_document.write_text(_project_design_markdown(project_slug, title=title, body=body), encoding="utf-8")
    _write_json_atomic(target_artifact, project_design_artifact_payload(artifact))
    print_func(f"team-launcher: wrote design document {target_design_document}")
    print_func(f"team-launcher: wrote project artifact {target_artifact}")
    return 0


def _new_project_artifact_dir(project: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{project}-team-launcher-new."))


def _new_project_session_dir(project: str, owner_user: str) -> str:
    owner_home = home_dir_for_user(owner_user) or Path("/home") / owner_user
    return str(owner_home / ".local" / "state" / f"{project}-ticket-board" / "pane-sessions")


def _new_project_control_repository(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / ".local" / "state" / "switchyard" / "projects" / project / "control.git"


def _new_project_worktree_base(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / f"{project}-worktrees"


NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES = 3
NEW_PROJECT_GRID_PANES_PER_ROW = 3


def _new_project_layout_leaves(role_count: int) -> list[dict[str, Any]]:
    return [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]


def _single_row_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    if len(leaves) <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": leaves,
    }


def _row_major_grid_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = math.ceil(len(leaves) / NEW_PROJECT_GRID_PANES_PER_ROW)
    base_row_size, extra = divmod(len(leaves), row_count)
    rows: list[dict[str, Any]] = []
    start = 0
    for row_index in range(row_count):
        row_size = base_row_size + (1 if row_index < extra else 0)
        row = leaves[start : start + row_size]
        start += row_size
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    if len(rows) == 1:
        return rows[0]
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _new_project_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    if role_count <= NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES:
        return _single_row_layout_payload(leaves)
    return _row_major_grid_layout_payload(leaves)


def _new_project_launcher_config_payload(
    plan: ProjectBoardProvision,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    #: What each role was chosen to run on, when it was chosen rather than
    #: defaulted. An absent model is left out of the payload rather than written
    #: as an empty string: the launcher reads a missing `model` as "the
    #: runtime's own default", and an empty one would be a value (SYRD-115).
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
) -> dict[str, Any]:
    layout_name = f"{plan.project}-konsole-layout.json"
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    worktree_base = _new_project_worktree_base(plan.project, plan.owner_user)
    control_repository = _new_project_control_repository(plan.project, plan.owner_user)
    roles = []
    for index, (role, cli) in enumerate(role_defs):
        role_payload: dict[str, Any] = {
            "cli": [cli],
            "env": _new_project_role_env(
                role,
                plan,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
            ),
            "live_commands": [cli],
            "role": role,
            "target": f"{plan.project}-{role}:0.0",
            "tmux_session": f"{plan.project}-{role}",
            "workdir": str(worktree_base / role),
            "yolo": True,
        }
        chosen_model = str((role_models or {}).get(role, "")).strip()
        if chosen_model:
            role_payload["model"] = chosen_model
        chosen_effort = str((role_efforts or {}).get(role, "")).strip()
        if chosen_effort:
            role_payload["effort"] = chosen_effort
        if index < MAX_VISIBLE_PANES_PER_WINDOW:
            role_payload["slot"] = index
        else:
            role_payload["detached"] = True
        roles.append(role_payload)
    payload = {
        "project": plan.project,
        **({"project_name": project_name} if project_name else {}),
        "ticket_prefix": plan.ticket_prefix,
        "layout": layout_name,
        "repository": str(repository),
        "control_repository": str(control_repository),
        "run_as_user": plan.owner_user,
        "worktree_branch": default_branch,
        "worktree_remote": remote,
        "worktree_base": str(worktree_base),
        "board_url": f"http://127.0.0.1:{plan.port}",
        "board_socket": plan.socket_path,
        "session_dir": _new_project_session_dir(plan.project, plan.owner_user),
        "role_state_isolation": True,
        "pane_launcher": str(switchyard_shared_pane_launcher()),
        "presentation": {
            "slot_count": min(len(role_defs), MAX_VISIBLE_PANES_PER_WINDOW),
            "layouts": {
                "default": {
                    str(index): role
                    for index, (role, _cli) in enumerate(role_defs[:MAX_VISIBLE_PANES_PER_WINDOW])
                }
            },
        },
        "roles": roles,
    }
    if upstream_report_url:
        payload["upstream_report_url"] = upstream_report_url
    if upstream_report_token_file:
        payload["upstream_report_token_file"] = upstream_report_token_file
    if plan.workflow is not None:
        from scripts.workflow_launcher import project_roles
        payload = project_roles(payload, plan.workflow)
    return payload


def _dedupe_role_defs(role_defs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, cli in role_defs:
        if role in seen:
            continue
        seen.add(role)
        result.append((role, cli))
    return result


def _new_project_role_env(
    role: str,
    plan: ProjectBoardProvision,
    *,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "TICKET_BOARD_DIRECTORCTL": f"{plan.board_current}/scripts/directorctl",
    }
    if role == "designer" and design_document is not None:
        project_dir = design_document.parent
        env.update(
            {
                "SWITCHYARD_PROJECT_SLUG": plan.project,
                "SWITCHYARD_PROJECT_NAME": project_name or plan.project,
                "SWITCHYARD_PROJECT_ARTIFACT": str(artifact_path or (_switchyard_dir(project_dir) / f"{plan.project}.project.json")),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
                "SWITCHYARD_DESIGNER_ONBOARDING": str(
                    _switchyard_dir(project_dir) / SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME
                ),
            }
        )
    if role == "director" and director_onboarding is not None and design_document is not None:
        env.update(
            {
                "SWITCHYARD_DIRECTOR_ONBOARDING": str(director_onboarding),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
            }
        )
    if role == "director":
        # Generic role data, read by the hook exactly as any other role's prompt is.
        # Seeded here because a project with no declarative workflow document has no
        # document to carry it, and the director's remit must not depend on one: the
        # hook no longer has a director-only branch to fall back to. A declarative
        # project carries the same field in its document, and the projection governs it
        # there, so this value is replaced rather than layered on top.
        seed_root = _director_seed_project_dir(design_document, director_onboarding)
        if seed_root is not None:
            env.setdefault(
                "TICKET_BOARD_ROLE_ONBOARDING_PROMPT",
                director_onboarding_seed_text(seed_root),
            )
    return env


def _director_seed_project_dir(
    design_document: Path | None, director_onboarding: Path | None
) -> Path | None:
    if design_document is not None:
        return design_document.parent
    if director_onboarding is not None:
        parent = director_onboarding.parent
        return parent.parent if parent.name == SWITCHYARD_PROJECT_DIR_NAME else parent
    return None


def write_new_project_launcher_artifacts(
    plan: ProjectBoardProvision,
    output_dir: Path,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    print_func: Callable[[str], None] = print,
) -> Path:
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    visible_role_count = min(len(role_defs), MAX_VISIBLE_PANES_PER_WINDOW)
    detached_roles = [role for role, _cli in role_defs[MAX_VISIBLE_PANES_PER_WINDOW:]]
    if detached_roles:
        print_func(
            f"team-launcher: auto-detached roles beyond the {MAX_VISIBLE_PANES_PER_WINDOW}-pane window cap: "
            f"{', '.join(detached_roles)}; use attach-role to surface one later or detach another role first"
        )
    config_path = output_dir / f"{plan.project}.json"
    layout_path = output_dir / f"{plan.project}-konsole-layout.json"
    layout_path.write_text(
        json.dumps(_new_project_layout_payload(visible_role_count), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            _new_project_launcher_config_payload(
                plan,
                repository=repository,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
                implementer_roles=implementer_roles,
                role_clis=role_defs,
                role_models=role_models,
                role_efforts=role_efforts,
                include_designer=include_designer,
                include_audit=include_audit,
                audit_roles=audit_roles,
                remote=remote,
                default_branch=default_branch,
                worktree_policy=worktree_policy,
                upstream_report_url=upstream_report_url,
                upstream_report_token_file=upstream_report_token_file,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Not group-writable, because the configuration decides which account every
    # role runs as and which board they talk to, and root refuses to register a
    # document anybody in its group could rewrite. Some were written 0660, and
    # on the tenant this was written for that made the registered configuration
    # unadoptable by the ordinary command (SYRD-167).
    config_path.chmod(config_path.stat().st_mode & ~(stat.S_IWGRP | stat.S_IWOTH))
    if plan.workflow:
        projected = _load_json(config_path)
        visible_role_count = max(1, 1 + max((role.get("slot", -1) for role in projected["roles"]), default=-1))
        _write_json_atomic(layout_path, _new_project_layout_payload(visible_role_count))
        if artifact_path and artifact_path.exists():
            artifact_data = _load_json(artifact_path)
            artifact_data["workflow"] = plan.workflow
            _write_json_atomic(artifact_path, artifact_data)
    policy_file = output_dir / "desktop-policy.json"
    if policy_file.exists():
        from scripts.desktop_access import validate_policy
        payload = _load_json(config_path)
        payload["desktop_access"] = validate_policy(_load_json(policy_file), project=plan.project, tenant=plan.owner_user)
        _write_json_atomic(config_path, payload)
    return config_path


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _tcp_port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _looks_like_switchyard_release_tree(path: Path) -> bool:
    return (
        (path / "switchyard").is_file()
        and (path / "scripts" / TEAM_LAUNCHER_NAME).is_file()
        and (path / "scripts" / "ticket-board-service.sh").is_file()
    )


def _switchyard_release_source_error(source_repo: Path) -> str:
    direct_marker = _read_switchyard_release_marker(source_repo)
    if direct_marker is not None:
        if direct_marker.marker_error:
            return (
                f"deploy source {source_repo} has an invalid {SWITCHYARD_RELEASE_MARKER_NAME}: "
                f"{direct_marker.marker_error}"
            )
        if _looks_like_switchyard_release_tree(source_repo):
            return ""
        return (
            f"deploy source {source_repo} has {SWITCHYARD_RELEASE_MARKER_NAME}, but is missing "
            "the expected exported launcher files"
        )

    shared_release = shared_switchyard_release_for_path(source_repo)
    if shared_release is None:
        return "not-release"
    if shared_release.marker_error:
        return (
            f"deploy source {source_repo} has an invalid shared release marker "
            f"at {shared_release.root}: {shared_release.marker_error}"
        )
    if shared_release.marker_commit or _looks_like_switchyard_release_tree(source_repo):
        return ""
    return (
        f"deploy source {source_repo} is under the Switchyard shared install, but is missing "
        f"{SWITCHYARD_RELEASE_MARKER_NAME} and the expected exported launcher files"
    )


def _precheck_deploy_source(
    source_repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    if not _path_exists(source_repo):
        return [f"deploy source {source_repo} does not exist"]

    release_error = _switchyard_release_source_error(source_repo)
    if release_error == "":
        return []
    if release_error != "not-release":
        return [release_error]

    has_git_metadata = (source_repo / ".git").exists()
    try:
        status = _git_status_porcelain(source_repo, runner=runner)
    except SystemExit:
        if has_git_metadata:
            raise
        return [
            f"deploy source {source_repo} is neither a git checkout nor a Switchyard release; "
            f"expected a clean Switchyard source checkout, or an exported release with "
            f"{SWITCHYARD_RELEASE_MARKER_NAME}"
        ]
    if status.strip():
        return [f"deploy checkout {source_repo} has uncommitted changes"]
    return []


def _system_unit_file_exists(unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    try:
        result = runner(
            ["systemctl", "list-unit-files", "--no-legend", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return _path_exists(Path("/etc/systemd/system") / unit)
    if result.returncode != 0:
        return _path_exists(Path("/etc/systemd/system") / unit)
    stdout = str(getattr(result, "stdout", "") or "").strip()
    return bool(stdout and not stdout.startswith("0 unit files listed"))


#: The unit every generated board unit already declares `Wants=`, and the
#: socket directory the generated connection strings use.
POSTGRES_SERVICE_UNIT = "postgresql.service"
POSTGRES_ADMIN_SOCKET_DIR = "/var/run/postgresql"


def postgres_cluster_script() -> Path:
    """The helper that initializes or starts the local cluster, in this tree."""
    return Path(__file__).resolve().parent / "ensure-postgres-cluster"


def postgres_availability_remedy(
    *, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
) -> str:
    """Why the local cluster did not answer, and the command that repairs it.

    A fresh Arch-family host installs the PostgreSQL package without a cluster:
    the service cannot start, and the first thing to notice was this preflight,
    which printed only psql's `No such file or directory` for the socket after
    every provisioning answer had been collected (SYRD-235). The raw error says
    what failed and nothing about what to do, so the state is read here --
    package, service, socket -- and the matching repair is named.
    """
    script = postgres_cluster_script()
    lines: list[str] = []
    if not _system_unit_file_exists(POSTGRES_SERVICE_UNIT, runner=runner):
        lines.append(
            f"this host has no {POSTGRES_SERVICE_UNIT}, so no PostgreSQL server is installed."
        )
        lines.append("  install the host packages first: sudo scripts/install-switchyard-prereqs")
        return "\n".join(lines)
    if not _system_unit_is_active(POSTGRES_SERVICE_UNIT, runner=runner):
        lines.append(
            f"{POSTGRES_SERVICE_UNIT} is installed but not running, so nothing is serving "
            f"{POSTGRES_ADMIN_SOCKET_DIR}."
        )
        lines.append(
            f"  initialize the cluster if this host has none, then start the service: sudo {script}"
        )
        lines.append(
            f"  it is idempotent, never re-initializes an existing cluster, and verifies the same "
            f"socket this check uses."
        )
        return "\n".join(lines)
    lines.append(
        f"{POSTGRES_SERVICE_UNIT} is active, but the admin connection over "
        f"{POSTGRES_ADMIN_SOCKET_DIR} did not answer."
    )
    lines.append(f"  read why: sudo systemctl status {POSTGRES_SERVICE_UNIT} --no-pager")
    lines.append(f"  and: sudo journalctl -u {POSTGRES_SERVICE_UNIT} -n 50 --no-pager")
    lines.append(f"  then re-verify the socket: sudo {script}")
    return "\n".join(lines)


def _database_exists(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    escaped = database.replace("'", "''")
    command = [
        "psql",
        "-XAt",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped}'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(
            f"team-launcher: cannot verify PostgreSQL database availability: {exc}\n"
            f"team-launcher: {postgres_availability_remedy(runner=runner)}\n"
            "team-launcher: nothing was created; re-run `switchyard new` once that answers."
        ) from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            f"team-launcher: cannot verify PostgreSQL database availability{detail}\n"
            f"team-launcher: {postgres_availability_remedy(runner=runner)}\n"
            "team-launcher: nothing was created; re-run `switchyard new` once that answers."
        )
    return str(getattr(result, "stdout", "") or "").strip() == "1"


def _ticket_board_table_count(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> int:
    command = [
        "psql",
        "-XAt",
        f"postgresql:///{database}?host=/var/run/postgresql",
        "-c",
        "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}{detail}")
    raw_count = str(getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw_count)
    except ValueError as exc:
        raise SystemExit(f"team-launcher: cannot parse ticket_board table count for database {database!r}: {raw_count!r}") from exc


def _usable_switchyard_entry_for_project(
    project: str,
    *,
    config_dir: Path | None,
    registry_dir: Path | None,
) -> tuple[SwitchyardProjectEntry | None, list[str]]:
    broken_entries: list[str] = []
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        if entry.slug.casefold() != project.casefold():
            continue
        try:
            load_project_config(entry.slug, entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit) as exc:
            broken_entries.append(f"{entry.config_path}: {exc}")
            continue
        return entry, broken_entries
    return None, broken_entries


def _recorded_owner_home(raw: Mapping[str, Any]) -> str:
    """The owner home a plan records, or the one its board root discloses.

    Every other generated path is anchored on this, so it is established before
    a reference plan can be built and is never guessed at.
    """
    recorded = str(raw.get("owner_home") or "").strip()
    if recorded:
        return recorded
    project = str(raw.get("project") or "").strip()
    board_root = Path(str(raw.get("board_root") or "")).expanduser()
    if project and board_root.name == f"{project}-ticketboard-live":
        return str(board_root.parent)
    return ""


def _plan_migration_reference(raw: Mapping[str, Any]) -> ProjectBoardProvision | None:
    """A current plan for the same project, to take generated names from.

    Built by the same `build_plan` provisioning uses, from the identity and the
    tenant-specific choices the document already records, so a field added
    after the document was written gets the value provisioning would have given
    it rather than an invented one. Nothing here decides what root installs:
    root renders from its own baseline, and a value taken from this reference
    is one the document was missing entirely (SYRD-52).
    """
    project = str(raw.get("project") or "").strip()
    owner_user = str(raw.get("owner_user") or "").strip()
    owner_home = _recorded_owner_home(raw)
    if not project or not owner_user or not owner_home:
        return None
    recorded: dict[str, Any] = {}
    for name in ("project_name", "ticket_prefix", "database"):
        value = str(raw.get(name) or "").strip()
        if value:
            recorded[name] = value
    port = raw.get("port")
    if isinstance(port, int) and not isinstance(port, bool):
        recorded["port"] = port
    try:
        return build_plan(
            project=project,
            owner_user=owner_user,
            owner_home=Path(owner_home),
            **recorded,
        )
    except SystemExit:
        return None


def _project_board_provision_from_json(
    path: Path,
    *,
    migrated: list[str] | None = None,
    supplied: Mapping[str, str] | None = None,
    document: Mapping[str, Any] | None = None,
) -> ProjectBoardProvision:
    """Parse a plan written by this release, or by an older one.

    A plan gains fields as the product does, and parsing straight into the
    current strict shape made every already provisioned tenant unupgradable the
    moment one was added. Fields the document predates are filled in first, and
    the caller is told which ones so it can report the migration (SYRD-52).

    `supplied` is what the operator gave on the command line -- `--commit-git-dir`,
    `--source-repo`. It fills a field the document is MISSING, before anything
    is judged unresolved, and never replaces one the document records: the
    tenant's recorded choices stand, and the caller applies the operator's
    values over the parsed plan afterwards exactly as before. Without it, the
    documented repair `switchyard upgrade <project> --commit-git-dir <path>`
    was refused for the one plan it exists to repair: mefp's plan predates
    `commit_git_dir`, its reference plan could not be built, and the parse gave
    up before the operator's value was ever consulted (SYRD-226).
    """
    # `document` is a plan a privileged caller already read without following
    # anything; `path` then names it for diagnostics only (SYRD-228).
    raw = dict(document) if document is not None else _load_json(path)
    document = dict(raw)
    owner_home = _recorded_owner_home(document)
    if owner_home:
        document.setdefault("owner_home", owner_home)
    from_operator: list[str] = []
    for name, value in (supplied or {}).items():
        if name in plan_field_names() and document.get(name) in (None, "") and value:
            document[name] = value
            from_operator.append(name)
    fields, added, unresolved = migrate_plan_document(
        document, reference=_plan_migration_reference(document)
    )
    if "owner_home" in unresolved:
        raise SystemExit(
            f"switchyard: {path} is missing provision field 'owner_home' and it cannot be "
            "derived from board_root"
        )
    if "commit_git_dir" in unresolved:
        # Never regenerated, so no reference plan could ever supply it: it names
        # where this tenant's commits are verified. Advising a re-provision --
        # which would discard the tenant, its tickets and its resumable state --
        # sent operators away from the one supported repair (SYRD-226).
        raise SystemExit(
            f"switchyard: {path} is missing provision field 'commit_git_dir', which no reference "
            "plan can supply: give it with `switchyard upgrade <project> --commit-git-dir <path>`"
        )
    if unresolved:
        raise SystemExit(
            f"switchyard: {path} is missing provision field {unresolved[0]!r} and no reference "
            "plan can be built for it; re-provision the project"
        )
    if migrated is not None:
        migrated.extend(added)
        migrated.extend(f"{name} (from the command line)" for name in from_operator)
    return ProjectBoardProvision(**{name: fields[name] for name in plan_field_names()})


#: Where provisioning installs the board's deploy rule. Its absence is how a
#: host without polkit shows itself, halfway through the packet (SYRD-261).
POLKIT_RULES_DIR = Path("/etc/polkit-1/rules.d")


#: What a polkit authority answers with: authorized, not authorized, needs
#: authentication, dismissed. Any of them means the service answered over the
#: system bus; pkcheck says 127 when it could not ask at all (pkcheck(1)).
POLKIT_ANSWERED_EXIT_CODES = frozenset({0, 1, 2, 3})
#: Brings back a stopped or masked authority. `restart` alone refuses a masked
#: unit, which is one way a host ends up with polkit present and unusable;
#: `unmask` is a no-op on a unit that is not masked (SYRD-261 review).
POLKIT_RESTART_COMMAND = "sudo systemctl unmask polkit.service && sudo systemctl restart polkit.service"
#: Bounded, and long enough for the system bus to activate polkitd on a host
#: where nothing has asked it anything yet -- which is normal, not broken.
POLKIT_QUERY_TIMEOUT_SECONDS = 15.0


def _apt_archive_has(package: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    """The installer's own question, asked the same way (install-switchyard-prereqs)."""
    try:
        result = runner(
            ["apt-cache", "show", "--no-all-versions", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def polkit_install_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """One command an operator can paste, or "" when there is none to give.

    Command text only, never prose around it: a remedy the operator has to
    edit before it runs is not a remedy (SYRD-261 review). On apt the package
    names depend on the release, so the archive is asked, as the installer asks.
    """
    if which("pacman"):
        return "sudo pacman -S --needed polkit"
    if which("apt-get"):
        if _apt_archive_has("pkexec", runner=runner):
            return "sudo apt-get install -y polkitd pkexec"
        return "sudo apt-get install -y policykit-1"
    return ""


def polkit_service_problem(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    timeout_seconds: float = POLKIT_QUERY_TIMEOUT_SECONDS,
) -> str:
    """Why the polkit authority cannot answer an authorization question, or "".

    Presence is not readiness: the rules directory and pkexec can both be there
    while polkitd cannot start or the system bus cannot reach it, and the board
    unit's deploy rule is then never consulted. So it is asked one real
    question, about this very process, through polkit's own client. Whatever
    the answer -- this account may not even be authorized -- an answer is what
    proves the service works; the system bus starts polkitd on demand, so a
    service that is simply not running yet is not a failure.
    """
    pkcheck = which("pkcheck")
    if pkcheck is None:
        return "pkcheck, polkit's own client, is not on PATH"
    try:
        result = runner(
            [pkcheck, "--action-id", "org.freedesktop.policykit.exec", "--process", str(os.getpid())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"the polkit authority did not answer within {timeout_seconds:g}s"
    except OSError as exc:
        return f"pkcheck could not be run: {exc}"
    if result.returncode in POLKIT_ANSWERED_EXIT_CODES:
        return ""
    detail = " ".join(str(result.stderr or "").split()) or f"exit {result.returncode}"
    return f"the polkit authority could not answer an authorization query (pkcheck: {detail})"


def polkit_readiness_problems(
    *,
    rules_dir: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    """Why this host cannot take a tenant's polkit rule or answer for it, if it cannot.

    Asked before anything is created. On a minimal host `switchyard new` used
    to create the accounts, the project and the board's units, then fail
    installing the deploy rule into a directory that did not exist -- and a
    re-run was refused, because by then a unit was installed with no database
    beside it (SYRD-261).
    """
    rules_dir = rules_dir if rules_dir is not None else POLKIT_RULES_DIR
    missing: list[str] = []
    if not rules_dir.is_dir():
        missing.append(f"{rules_dir} does not exist")
    if which("pkexec") is None:
        missing.append("pkexec is not on PATH")
    if missing:
        install = polkit_install_command(which=which, runner=runner)
        return [
            f"polkit is not installed ({'; '.join(missing)}). Provisioning installs a polkit "
            "rule so the project account can restart its own board, and privileged commands "
            "run through pkexec. "
            + (
                f"Install it, then run this again:\n    {install}"
                if install
                else "Install your distribution's polkit package, then run this again."
            )
        ]
    service = polkit_service_problem(which=which, runner=runner)
    if service:
        return [
            f"polkit is installed but not working: {service}. The board's deploy rule is "
            "enforced by that service. Start it, then run this again:\n"
            f"    {POLKIT_RESTART_COMMAND}"
        ]
    return []


def _installed_unit_is_this_plans(plan: ProjectBoardProvision) -> bool:
    """Whether the installed board unit is exactly the one this plan renders.

    The packet installs the units before it creates the database, so a run that
    stopped between the two leaves precisely this unit and no database. That is
    this provisioning, half done, and running it again finishes it. A unit
    that says anything else is somebody else's state, and stays refused.
    """
    try:
        installed = _installed_unit_path(plan.board_unit).read_text(encoding="utf-8")
    except OSError:
        return False
    return installed == render_board_unit(plan)


def precheck_new_project(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    require_owner_user: bool = True,
    require_repository: bool = True,
    polkit_problems: Callable[..., list[str]] = polkit_readiness_problems,
) -> None:
    errors: list[str] = list(polkit_problems(runner=runner))
    if require_owner_user and uid_for_user(plan.owner_user) is None:
        errors.append(f"target user {plan.owner_user!r} does not exist")
    if require_repository and not _path_exists(repository):
        errors.append(f"project repository {repository} does not exist")
    errors.extend(_precheck_deploy_source(source_repo, runner=runner))
    unit_exists = _system_unit_file_exists(plan.board_unit, runner=runner)
    database_exists = _database_exists(plan.database, runner=runner)
    socket_path = Path(plan.socket_path)
    socket_is_present = socket_exists(socket_path)
    port_is_live = port_in_use(plan.port)
    if not unit_exists:
        if database_exists:
            errors.append(f"database {plan.database!r} already exists but {plan.board_unit} is not installed")
        if socket_is_present:
            errors.append(f"socket {plan.socket_path} already exists but {plan.board_unit} is not installed")
        if port_is_live:
            errors.append(f"port {plan.port} is already in use but {plan.board_unit} is not installed")
    elif not database_exists:
        # Exactly this plan's unit and no database is this provisioning, stopped
        # between installing its units and creating its database; running it
        # again completes it. Anything else is not ours to build over (SYRD-261).
        if not _installed_unit_is_this_plans(plan):
            errors.append(
                f"{plan.board_unit} is installed but database {plan.database!r} does not exist, "
                "and the installed unit is not the one this provisioning would install.\n"
                f"  to inspect recovery: switchyard teardown {plan.project} --dry-run"
            )
    else:
        table_count = _ticket_board_table_count(plan.database, runner=runner)
        if table_count > 0:
            launch_entry, broken_entries = _usable_switchyard_entry_for_project(
                plan.project,
                config_dir=config_dir,
                registry_dir=registry_dir,
            )
            if launch_entry is not None:
                errors.append(
                    f"project {plan.project!r} is already provisioned "
                    f"(database {plan.database} has {table_count} ticket_board tables, {plan.board_unit} is installed).\n"
                    f"  to launch it:      switchyard {plan.project}\n"
                    f"  to start over:     switchyard teardown {plan.project} --dry-run"
                )
            else:
                effective_config_dir = config_dir or DEFAULT_CONFIG_DIR
                effective_registry_dir = registry_dir or switchyard_registry_dir()
                broken_entry_detail = f"  unusable launch entry: {broken_entries[0]}\n" if broken_entries else ""
                errors.append(
                    f"project {plan.project!r} is partially provisioned but not registered "
                    f"(database {plan.database} has {table_count} ticket_board tables, "
                    f"{plan.board_unit} is installed, but no usable launch entry exists in "
                    f"{effective_config_dir} or {effective_registry_dir}).\n"
                    f"{broken_entry_detail}"
                    f"  to inspect recovery: switchyard teardown {plan.project} --dry-run"
                )
    if errors:
        raise SystemExit("team-launcher: new project precheck failed:\n- " + "\n- ".join(errors))


def new_project_command(
    project: str,
    *,
    from_artifact: Path | None = None,
    owner_user: str | None = None,
    owner_home: Path | None = None,
    desktop_policy: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    source_repo: Path | None = None,
    workflow_config: Path | None = None,
    commit_git_dir: str | None = None,
    repository: Path | None = None,
    output_dir: Path | None = None,
    director_onboarding: Path | None = None,
    execute: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    require_owner_user: bool | None = None,
    enable_owner_linger: bool = True,
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    #: Chosen per role by the selectors, when this came from an interactive
    #: `switchyard new`. A scripted run passes neither and the generated
    #: configuration carries no model or effort, exactly as before (SYRD-115).
    role_models: Mapping[str, str] | None = None,
    role_efforts: Mapping[str, str] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    if execute and dry_run:
        raise SystemExit("team-launcher: --execute and --dry-run are mutually exclusive")
    effective_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    design_artifact = load_project_design_artifact(from_artifact, expected_project=project) if from_artifact else None
    if design_artifact is not None:
        if owner_user is not None or repository is not None:
            raise SystemExit("team-launcher: --from owns owner/repository answers; do not also pass --owner-user or --repository")
        effective_owner = design_artifact.owner_user
        effective_repository = design_artifact.repository.expanduser().resolve(strict=False)
        remote = design_artifact.remote
        default_branch = design_artifact.default_branch
        worktree_policy = design_artifact.worktree_policy
        ticket_prefix = design_artifact.ticket_prefix
        project_name = design_artifact.project_name
        design_document = design_artifact.design_document if design_artifact.include_designer else None
        implementer_roles = design_artifact.implementer_roles
        role_clis = design_artifact.role_clis
        # A checked-in artifact reproduces the whole choice, not just the
        # runtime: an explicit argument still wins, so nothing a caller passes
        # is overridden by the file (SYRD-115).
        role_models = role_models or dict(design_artifact.role_models)
        role_efforts = role_efforts or dict(design_artifact.role_efforts)
        include_designer = design_artifact.include_designer
        include_audit = design_artifact.include_audit
        audit_roles = design_artifact.audit_roles
        board_service_traversal = bool(design_artifact.capability_grants.get("board_service_traversal", True))
    else:
        if repository is None:
            raise SystemExit("team-launcher: new project requires --repository for the project's working checkout")
        effective_owner = (owner_user or _default_new_project_owner(project)).strip()
        effective_repository = repository.expanduser().resolve(strict=False)
        remote = "origin"
        default_branch = "main"
        worktree_policy = "shared"
        ticket_prefix = None
        project_name = project
        design_document = None
        implementer_roles = DEFAULT_PROJECT_IMPLEMENTER_ROLES
        role_clis = _default_role_cli_pairs(implementer_roles, include_designer=True, include_audit=True)
        include_designer = True
        include_audit = True
        audit_roles = ("audit",)
        board_service_traversal = True
    if worktree_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"team-launcher: worktree policy must be one of {sorted(WORKTREE_POLICIES)}")
    plan = build_plan(
        project=project,
        project_name=project_name,
        workflow=seed_director_onboarding(
            json.loads(workflow_config.read_text())
            if workflow_config
            else _load_json(from_artifact).get("workflow")
            if from_artifact
            else None,
            effective_repository,
        )[0],
        owner_user=effective_owner,
        owner_home=owner_home,
        # The human at the keyboard, so they can run `switchyard <project>`
        # afterwards without sudo and without becoming the owner account.
        control_user=resolve_control_user(
            project, invoking_user=invoking_human(), owner_user=effective_owner
        ),
        port=port,
        database=database,
        source_repo=effective_source_repo,
        # The tenant's own checkout, which is not the release the artifacts are
        # rendered from. Passing the release where this was meant is what made
        # the packet confine nothing (SYRD-156).
        project_repository=effective_repository,
        commit_git_dir=commit_git_dir,
        ticket_prefix=ticket_prefix,
        implementer_roles=implementer_roles,
        board_service_traversal=board_service_traversal,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
    )
    precheck_new_project(
        plan,
        source_repo=effective_source_repo,
        repository=effective_repository,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=None,
        registry_dir=None,
        require_owner_user=execute if require_owner_user is None else require_owner_user,
    )
    artifact_dir = (output_dir or _new_project_artifact_dir(plan.project)).expanduser().resolve(strict=False)
    if desktop_policy is not None:
        from scripts.desktop_access import validate_policy
        selected = {"mode": "headless"} if str(desktop_policy) == "headless" else _load_json(desktop_policy)
        selected = validate_policy(selected, project=plan.project, tenant=plan.owner_user)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(artifact_dir / "desktop-policy.json", selected)
    # The rollout must hand each role the tree it works in, so record those
    # paths on the plan before the operator artifact is rendered (SYRD-39).
    _role_worktree_base = _new_project_worktree_base(plan.project, plan.owner_user)
    plan = replace(
        plan,
        role_worktrees=tuple(
            (role, str(_role_worktree_base / role)) for role, _account in plan.role_accounts
        ),
    )
    write_artifacts(plan, artifact_dir, enable_owner_linger=enable_owner_linger)
    if execute and os.geteuid() == 0:
        # The provision directory is handed to the tenant, so root publishes its
        # own copy of everything it later installs or executes -- rendered here
        # from the plan root just computed, never copied back out of the tenant's
        # directory -- and installs from that instead (SYRD-39).
        rendered_privileged = render_privileged_artifacts(plan, enable_owner_linger=enable_owner_linger)
        try:
            mirrored_privileged = install_privileged_artifacts(plan, rendered_privileged)
        except OSError as exc:
            mirrored_privileged = None
            print_func(
                f"switchyard: could not stage {plan.project} privileged artifacts for root: {exc}. "
                f"Run `switchyard upgrade {plan.project}` as root before installing its units."
            )
        if mirrored_privileged is not None:
            print_func(
                f"switchyard: root installs {plan.project} units, grants and SQL from {mirrored_privileged}"
            )
    config_path = write_new_project_launcher_artifacts(
        plan,
        artifact_dir,
        repository=effective_repository,
        project_name=project_name,
        artifact_path=from_artifact,
        design_document=design_document,
        director_onboarding=director_onboarding,
        implementer_roles=implementer_roles,
        role_clis=role_clis,
        role_models=role_models,
        role_efforts=role_efforts,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
        upstream_report_url=upstream_report_url.strip(),
        upstream_report_token_file=upstream_report_token_file.strip(),
        print_func=print_func,
    )
    commands_path = artifact_dir / "operator-commands.sh"
    # The complete role-isolation handoff -- accounts, ownership, runtime,
    # tooling AND credential seeding -- is written beside the other artifacts, so
    # an operator who follows the printed instruction once has everything. It
    # used to be produced only by a later failed start, which meant doing exactly
    # what provisioning said still left the credential gaps open (SYRD-39).
    try:
        handoff_config = load_project_config(plan.project, config_path)
    except SystemExit:
        handoff_config = None
    if handoff_config is not None and role_isolation_gaps(handoff_config):
        handoff_path, handoff_problems = publish_role_account_migration(
            handoff_config, config_path=config_path, print_func=print_func
        )
        if handoff_path is not None:
            print_func(
                f"team-launcher: roles are not isolated yet; run {handoff_path} as an operator "
                "(safe to re-run) before starting them"
            )
        else:
            print_func(
                "team-launcher: roles are not isolated yet, and the migration script was not "
                "published where root can run it: " + "; ".join(handoff_problems)
            )
    if not execute:
        print_func(f"team-launcher: dry-run for {plan.project}; artifacts in {artifact_dir}")
        print_func(f"team-launcher: launcher config {config_path}")
        print_func("team-launcher: execution plan:")
        print_func("  sudo -v")
        # By absolute path, and with no `cd` in front of it. The packet resolves
        # the artifacts beside it from its own location, so the directory an
        # operator happens to be in is not part of the instruction -- and an
        # instruction that told them to change directory first is what taught
        # everyone the packet needed one (SYRD-149).
        print_func(f"  {recorded_provisioning_command(plan.project, str(commands_path))}")
        print_func(
            f"team-launcher: that leaves a root-owned record of the run; read it with "
            f"`switchyard rollout-log {plan.project}` (SYRD-128)"
        )
        print_func("")
        print_func(commands_path.read_text(encoding="utf-8").rstrip("\n"))
        return 0
    print_func(f"team-launcher: provisioning {plan.project}; artifacts in {artifact_dir}")
    sudo_result = runner(["sudo", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if sudo_result.returncode != 0:
        stderr = str(getattr(sudo_result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: sudo authentication failed{detail}")
    # Recorded when the recorder is reachable, plain otherwise: provisioning a
    # project must not depend on the journal, but when it can be recorded it
    # should be, because this is the run whose evidence matters most and the
    # one an operator is least likely to still have a terminal for (SYRD-128).
    recorder = _rollout_recorder_path()
    if recorder is not None:
        result = runner(
            ["sudo", str(recorder), plan.project, "--label", "provisioning",
             "--", "bash", str(commands_path)],
        )
        print_func(
            f"team-launcher: the run is recorded; read it with "
            f"`switchyard rollout-log {plan.project}`"
        )
    else:
        result = runner(["bash", str(commands_path)])
    if result.returncode != 0:
        raise SystemExit(f"team-launcher: provisioning failed with exit status {result.returncode}")
    config = load_project_config(plan.project, config_path)
    if config.desktop_access is not None:
        configure_project_desktop(config, config_path=config_path,
            helper=effective_source_repo / "scripts/desktop_access.py", runner=runner)
    from scripts.workflow_launcher import assign_projection_owner
    assign_projection_owner(config, config_path, runner=runner)
    print_func(f"team-launcher: provisioned {plan.project}; launcher config {config_path}")
    return 0


@dataclass(frozen=True)
class SwitchyardProjectEntry:
    slug: str
    name: str
    config_path: Path


@dataclass(frozen=True)
class GithubIdentityStatus:
    """Whether the project owner can actually publish, and what is missing.

    Never carries private key material. The public half is here because that is
    what an operator has to paste into GitHub, and the fingerprint because that
    is what they can compare against what GitHub already lists (SYRD-74).
    """

    owner_user: str
    key_path: Path
    problems: tuple[str, ...] = ()
    authenticated: bool = False
    detail: str = ""
    public_key: str = ""
    fingerprint: str = ""
    checked: bool = False

    @property
    def ready(self) -> bool:
        return self.checked and not self.problems and self.authenticated


@dataclass(frozen=True)
class TrustedOwnerIdentity:
    """Who a tenant's owner is, taken from root's own record and the kernel."""

    owner_user: str
    owner_home: Path
    owner_uid: int
    owner_gid: int
    problems: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return not self.problems


def trusted_owner_identity(project: str) -> TrustedOwnerIdentity:
    """The owner root will act for, derived from things the tenant cannot write.

    The tenant's configuration and its plan are both writable by the account
    every role runs as, so neither can say whose SSH state a root command
    modifies: a role could point `run_as_user` or `owner_home` somewhere else
    between the operator deciding to run this and the command reading it. Root's
    own baseline plan lives in a directory only root can write, and passwd is the
    kernel's. Both are consulted, and they have to agree (SYRD-100 review).
    """
    baseline = privileged_baseline_plan_path(project)
    problems: list[str] = []
    walk = root_controlled_problems_for(str(baseline))
    if walk:
        return TrustedOwnerIdentity("", Path(), -1, -1, tuple(
            [f"{baseline} is not root-controlled, so it cannot say who this tenant's owner is"] + walk
        ))
    try:
        recorded = json.loads(baseline.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return TrustedOwnerIdentity("", Path(), -1, -1, (f"{baseline} could not be read: {exc}",))
    if not isinstance(recorded, dict):
        return TrustedOwnerIdentity("", Path(), -1, -1, (f"{baseline} is not a plan document",))
    owner_user = str(recorded.get("owner_user") or "").strip()
    recorded_home = str(recorded.get("owner_home") or "").strip()
    if not owner_user:
        problems.append(f"{baseline} records no owner_user")
    if not recorded_home:
        problems.append(f"{baseline} records no owner_home")
    if problems:
        return TrustedOwnerIdentity("", Path(), -1, -1, tuple(problems))
    owner_uid = uid_for_user(owner_user)
    owner_home = home_dir_for_user(owner_user)
    if owner_uid is None or owner_home is None:
        return TrustedOwnerIdentity(
            "", Path(), -1, -1, (f"{owner_user} is not an account on this host",)
        )
    # The kernel's answer and root's record have to be the same answer. A
    # divergence is not something to pick a winner from.
    if owner_home != Path(recorded_home):
        return TrustedOwnerIdentity(
            "", Path(), -1, -1,
            (
                f"{baseline} records {owner_user}'s home as {recorded_home}, and this host says "
                f"{owner_home}. Nothing was changed: which one is right is not this command's "
                "to decide.",
            ),
        )
    try:
        owner_gid = int(pwd.getpwuid(owner_uid).pw_gid)
    except KeyError:
        owner_gid = owner_uid
    return TrustedOwnerIdentity(owner_user, owner_home, owner_uid, owner_gid)


def expected_privileged_uid() -> int:
    """Whose files count as root's for the privileged provision directory.

    Root's on a host. When the privileged provision root has been overridden it
    is the caller's own uid, because that override is the documented seam for
    exercising these paths without writing to /etc and a fixture cannot own a
    root-owned file. The same seam the trusted-release checks use (SYRD-97).
    """
    return os.getuid() if os.environ.get(PRIVILEGED_PROVISION_ROOT_ENV, "").strip() else 0


def root_controlled_problems_for(path: str) -> list[str]:
    """The whole-path root ownership check, through the documented test seam."""
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    overridden = os.environ.get(PRIVILEGED_PROVISION_ROOT_ENV, "").strip()
    return root_controlled_problems(path, base=overridden or "/")


@dataclass(frozen=True)
class PlanDocument:
    """One plan authority, opened without following anything."""

    path: Path
    data: dict[str, Any]
    raw: bytes
    uid: int
    gid: int
    mode: int


def read_plan_no_follow(
    path: Path,
    *,
    require_root_owned: bool,
    require_owner_uids: Sequence[int] | None = None,
    require_single_link: bool = False,
    require_not_shared_writable: bool = False,
) -> tuple[PlanDocument | None, str]:
    """Read one plan authority by fd, refusing symlinks at every component.

    `Path.read_text` on a tenant-controlled directory follows whatever is there.
    The tenant can replace its plan with a symlink to anything, and root would
    then read, truncate and re-own the referent instead (SYRD-100 review).
    """
    relative = Path(str(path).lstrip("/"))
    dir_fd, problem = _walk_no_follow(Path(path.anchor or "/"), relative)
    if dir_fd < 0:
        return None, f"{path}: {problem}"
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None, f"{path} is a symlink, so it is not a plan this will read or write"
            if exc.errno == errno.ENOENT:
                return None, f"{path} does not exist"
            return None, f"{path} could not be opened ({exc.strerror})"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, f"{path} is not a regular file"
            if require_not_shared_writable and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None, (
                    f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                    "group or beyond can write"
                )
            if require_single_link and info.st_nlink != 1:
                # A second link is a second name for somebody else's file, and
                # the owner check passes on the file it points at rather than on
                # the document the tenant is entitled to write (SYRD-228).
                return None, (
                    f"{path} has {info.st_nlink} links, so it is another file under a second "
                    "name rather than this project's own document"
                )
            if require_owner_uids is not None:
                # Root reads a tenant's own document here, so the question is
                # not whether root owns it but whether it belongs to somebody
                # entitled to have written it -- the account root is acting for,
                # or root -- and whether anybody else can rewrite it between
                # this read and the decision made from it (SYRD-155).
                if info.st_uid not in set(require_owner_uids):
                    expected = ", ".join(str(uid) for uid in require_owner_uids)
                    return None, (
                        f"{path} is owned by uid {info.st_uid} rather than by uid {expected}, "
                        "so it is not a document this project's owner or root wrote"
                    )
                if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return None, (
                        f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                        "group or beyond can write"
                    )
            if require_root_owned:
                expected = expected_privileged_uid()
                if info.st_uid != expected:
                    return None, (
                        f"{path} is owned by uid {info.st_uid} rather than by uid {expected}"
                    )
                if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return None, (
                        f"{path} is mode {stat.S_IMODE(info.st_mode):04o}, which anybody in its "
                        "group or beyond can write"
                    )
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        # Why, never what: this may be a file the tenant pointed root at, and a
        # decoder message quotes the bytes it choked on (SYRD-228).
        return None, f"{path} is not readable as a plan document: it is not UTF-8 text"
    except json.JSONDecodeError as exc:
        return None, (
            f"{path} is not readable as a plan document: it is not JSON "
            f"(line {exc.lineno}, column {exc.colno})"
        )
    if not isinstance(data, dict):
        return None, f"{path} is not a plan document"
    return PlanDocument(path, data, raw, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), ""


def _directory_owner_no_follow(directory: Path) -> tuple[os.stat_result | None, str]:
    """A directory's own stat, reached without following a link on the way."""
    relative = Path(str(directory).lstrip("/")) / "_"
    dir_fd, problem = _walk_no_follow(Path(directory.anchor or "/"), relative)
    if dir_fd < 0:
        return None, f"{directory}: {problem}"
    try:
        info = os.fstat(dir_fd)
    finally:
        os.close(dir_fd)
    if not stat.S_ISDIR(info.st_mode):
        return None, f"{directory} is not a directory"
    return info, ""


def read_tenant_document_no_follow(path: Path, *, what: str) -> tuple[dict[str, Any] | None, str]:
    """Read a tenant's own generated document as root, following nothing.

    The privileged upgrade reads the tenant's plan and its generated
    configuration from a directory the tenant owns. `Path.read_text` follows
    whatever is there, so a symlink planted at either name sent root to read a
    file of the tenant's choosing -- reported, if it was not JSON, as a raw
    traceback rather than as a refusal (SYRD-228). The SYRD-227 ownership
    repair already opened these paths this way; the reads that ran BEFORE it
    did not.

    Entitled means the directory's own owner or root, because only root can
    change an owner: a file somebody else owns, one anybody else can write, one
    that is a second link to another file, and a symlink at any component are
    all refused. Returns (document, why not).
    """
    directory, problem = _directory_owner_no_follow(path.parent)
    if directory is None:
        return None, f"refusing to read {what} at {path}: {problem}"
    if directory.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None, (
            f"refusing to read {what} at {path}: {path.parent} is mode "
            f"{stat.S_IMODE(directory.st_mode):04o}, so anybody in its group or beyond can "
            "replace what is in it"
        )
    # Who may have put the file there is a question about the directory. Only
    # root can write into a root-owned directory, so a file it holds is one
    # root placed -- provisioning writes the tenant's own documents there and
    # gives them to the owner, which is exactly that shape. A directory the
    # tenant owns is one the tenant can fill with anything, so there the
    # document has to belong to that owner or to root.
    permitted = None if directory.st_uid == 0 else sorted({0, directory.st_uid})
    document, problem = read_plan_no_follow(
        path,
        require_root_owned=False,
        require_owner_uids=permitted,
        require_single_link=True,
        require_not_shared_writable=True,
    )
    if document is None:
        return None, f"refusing to read {what} at {path}: {problem}"
    return dict(document.data), ""


def github_identity_remedy(status: GithubIdentityStatus, *, project: str = "") -> str:
    """Exactly what a human has to do, named rather than implied (SYRD-74)."""
    if status.ready:
        return ""
    lines = [
        f"warning: switchyard: {status.owner_user} cannot publish to GitHub: {status.detail}"
    ]
    for problem in status.problems:
        lines.append(f"  {problem}")
    upgrade = f"switchyard upgrade {project}" if project else "switchyard upgrade <project>"
    if status.problems:
        lines.append(f"  repair the identity itself with `sudo {upgrade}`, which is re-runnable")
    if status.public_key and not status.authenticated:
        lines.append(
            "  GitHub does not accept this key. Add its public half at "
            "https://github.com/settings/keys (or to the repository's deploy keys with write "
            f"access), then rerun `sudo {upgrade}`:"
        )
        lines.append(f"    {status.public_key}")
        if status.fingerprint:
            lines.append(f"  fingerprint: {status.fingerprint}")
    return "\n".join(lines)


FIRST_RUN_AUTH_LOGIN_COMMANDS: dict[str, list[str]] = {
    "agy": ["agy"],
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login"],
    "hermes": ["hermes", "model"],
}
# Vendor install commands, as TEXT for a person to run themselves. Switchyard
# never executes these. PGU-904 removed agent CLI installation deliberately --
# the user owns which version of each CLI they run -- and wiring this table to a
# subprocess would put that straight back. If you are here to "finish the job"
# by making these runnable, that is the thing this table exists to prevent.
#
# These strings WILL drift as vendors change their installers, and that is the
# accepted trade: a stale command in a message a person reads and can correct is
# a far smaller failure than a stale command we run on their machine. Verified
# on 2026-09-03 by fetching each URL without piping it -- every one served a
# shell script for the expected CLI, and the interpreter matches each script's
# shebang (claude, agy and hermes are bash; codex is sh).
#
# docs/fresh-machine-install.md carries the same four commands for a reader who
# has not run anything yet. Update both together; a test asserts they agree.
AGENT_CLI_INSTALL_COMMANDS: dict[str, str] = {
    "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "hermes": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
}


#: Where that tree is read from. A module constant rather than an environment
#: variable: this decides what a root-run command reads, and the caller's own
#: environment must not be able to point it somewhere else.
PROC_ROOT = Path("/proc")


def host_wide_install_instruction(cli: str) -> str:
    """How to make one CLI host-wide, scoped so it lands where panes look.

    Switchyard does not run this. PGU-904 removed CLI installation from this
    module on purpose, and the reason applies with more force here: the only
    installers these vendors publish are `curl | sh`, and a host-wide variant
    would have to run one as ROOT, during provisioning, from the network. That
    is a supply-chain decision rather than a convenience, and not one to take
    silently while fixing a usability bug (SYRD-210).

    What this does fix is the half that was plainly wrong. The printed remedy
    used to be the bare vendor line, which installs into whichever account runs
    it -- the desktop operator's, never the owner's. This one says where the
    executable has to end up and what must NOT travel with it.
    """
    command = AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
    if not command:
        return (
            f"install {cli} with that vendor's own installer, then place the executable in "
            "/usr/local/bin owned by root, mode 0755, so every tenant resolves it"
        )
    return (
        f"install {cli} host-wide: run the vendor installer under a throwaway HOME so nothing "
        "it writes becomes shared, then move only the executable to /usr/local/bin owned by "
        "root, mode 0755. No configuration, token or session file may travel with it -- "
        "credentials stay in the owner account that authenticates"
    )


def _missing_cli_install_clause(cli: str) -> str:
    """How to install one missing CLI, as text the reader runs themselves.

    It used to say "install <cli> for owner user <owner> with: <vendor command>".
    Both halves were wrong together: the vendor command installs for whoever
    runs it, so following it exactly installed into the operator's own account
    and the tenant still could not start -- and doing it per owner is the
    duplicate installation SYRD-210 removed. Live UAT was given this line on a
    resumed tenant (SYRD-211 second kickback).

    What it names now is the host-wide destination, which serves this owner and
    every later one. The vendor command is still text for a person to run;
    switchyard never fetches or runs it (PGU-904).
    """
    command = AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
    installer = command or "that vendor's own installer"
    return (
        f"install {cli} host-wide with {installer}, or let switchyard promote a copy you "
        "already have when it offers"
    )


def _owner_user_cli_reminder(owner_user: str = "") -> str:
    """The remedy, which is host-wide and once -- not per owner account.

    This used to say "install each one for owner user <owner>". That is the
    duplicate per-owner installation SYRD-210 removed: it is work again for
    every new tenant, and the vendor commands printed beside it install for
    whoever runs them, so an operator who followed it exactly installed into
    their own account and the tenant still could not start. Live Zorin UAT was
    given this text on a resumed tenant after SYRD-210 had already landed
    (SYRD-211 second kickback).
    """
    owner = (owner_user or "").strip()
    whose = f"owner user {owner}" if owner else "the project's owner user"
    return (
        f"switchyard: panes run as {whose}, which does not inherit a CLI installed only for the "
        "user running switchyard. Install it host-wide once -- or, if you already have a private "
        "copy, let switchyard promote that executable to a root-owned host-wide copy when it "
        "offers, which every later project reuses."
    )


def _slug_from_project_name(name: str) -> str:
    raw = unicodedata.normalize("NFKD", name.strip())
    pieces: list[str] = []
    for ch in raw:
        if ch.isascii() and ch.isalnum():
            pieces.append(ch.lower())
        elif unicodedata.category(ch).startswith("M"):
            continue
        else:
            pieces.append("_")
    slug = "_".join(part for part in "".join(pieces).split("_") if part)
    if len(slug) > 40:
        slug = slug[:40].rstrip("_")
    if not slug:
        raise SystemExit("switchyard: project slug cannot be empty")
    return _validate_project_slug(slug)


def _legacy_dash_slug_from_project_name(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise SystemExit("switchyard: project slug cannot be empty")
    return slug


def _project_name_selector_slugs(name: str) -> set[str]:
    selectors: set[str] = set()
    for derive in (_slug_from_project_name, _legacy_dash_slug_from_project_name):
        try:
            selectors.add(derive(name).casefold())
        except SystemExit:
            continue
    return selectors


def _validate_project_slug(value: str) -> str:
    slug = value.strip().lower()
    if not PROJECT_SLUG_RE.fullmatch(slug):
        raise SystemExit("switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$")
    return slug


def _is_valid_owner_user_name(value: str) -> bool:
    return bool(OWNER_USER_NAME_RE.fullmatch(value))


def _switchyard_dir(project_dir: Path) -> Path:
    return project_dir / SWITCHYARD_PROJECT_DIR_NAME


from scripts.ticket_board.workflow_config import (  # noqa: E402
    DIRECTOR_ONBOARDING_MIGRATION,
    DIRECTOR_ROLE,
)


def _project_dir_from_generated_config_path(config_path: Path) -> Path | None:
    resolved = config_path.expanduser().resolve(strict=False)
    if resolved.parent.name != "provision" or resolved.parent.parent.name != SWITCHYARD_PROJECT_DIR_NAME:
        return None
    return resolved.parent.parent.parent


def _chown_project_file(
    *,
    owner_user: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["chown", f"{owner_user}:{owner_user}", str(path)])
    if result.returncode != 0:
        raise SystemExit(f"switchyard: failed to assign {path} to {owner_user}")


def _owner_git_args(owner_user: str, project_dir: Path, *git_args: str) -> list[str]:
    return ["sudo", "-u", owner_user, "git", "-C", str(project_dir), *git_args]


def _owner_process_runner(
    *,
    owner_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return runner(["sudo", "-u", owner_user, "-H", *args], **kwargs)

    return wrapped


def _control_repository_owned_roots(config: ProjectConfig) -> list[Path]:
    owned_roots: list[Path] = []
    if config.control_repository is not None:
        owned_roots.extend([config.control_repository.parent, config.control_repository])
    if config.worktree_base is not None:
        owned_roots.append(config.worktree_base)
    if config.pane_launcher is not None:
        owned_roots.extend([config.pane_launcher.parent, config.pane_launcher])
    return owned_roots


def _owner_command_args(owner_user: str, command: Sequence[str]) -> list[str]:
    if owner_user == current_user_name():
        return list(command)
    return ["sudo", "-u", owner_user, *command]


#: What a terminal needs to keep looking like itself across the owner boundary.
#: `sudo` resets the environment, and a CLI that cannot see TERM or COLORTERM
#: draws its first run in monochrome -- which is what the User was shown
#: (SYRD-191).
TERMINAL_PRESENTATION_ENV_KEYS = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")


def _terminal_presentation_env(source: Mapping[str, str] | None = None) -> list[str]:
    environ = os.environ if source is None else source
    return [
        f"{key}={environ[key]}"
        for key in TERMINAL_PRESENTATION_ENV_KEYS
        if str(environ.get(key) or "").strip()
    ]


def _owner_command_env_args(owner_user: str, owner_home: Path, command: Sequence[str]) -> list[str]:
    path = _prepend_paths(DEFAULT_PANE_BASE_PATH, _owner_home_bin_dirs(owner_home))
    return _owner_command_args(
        owner_user,
        [
            "env",
            f"HOME={owner_home}",
            f"PATH={path}",
            *_terminal_presentation_env(),
            *command,
        ],
    )


#: Decide who runs a printed command when it is run, not when it is printed.
#:
#: `_owner_command_args` answers from `current_user_name()`, which is right for
#: a command this process is about to run itself and wrong for one that is
#: printed for somebody else to run later. An unprivileged `switchyard upgrade`
#: printed the tenant deploy with no boundary at all, because the renderer WAS
#: the tenant owner -- and the operator then ran that line through the rollout
#: recorder under sudo, so the deploy executed as root, `/run/user/$(id -u)`
#: resolved to `/run/user/0`, and the migration ran with the tenant's real
#: notification listener still up. The root-invoked renderer emitted the
#: boundary and the two disagreed about the same release (SYRD-138).
#:
#: Nothing is interpolated into this script: the owner and the command arrive as
#: positional arguments, so a tenant path containing a space, a quote or a
#: dollar sign is data rather than syntax.
OWNER_BOUNDARY_SCRIPT = (
    'target=$1; shift; '
    'if [ "$(id -un)" = "$target" ]; then exec "$@"; fi; '
    'exec sudo -u "$target" "$@"'
)


def _owner_boundary_env_args(owner_user: str, owner_home: Path, command: Sequence[str]) -> list[str]:
    """The same command, guaranteed to run as `owner_user` whoever starts it."""
    path = _prepend_paths(DEFAULT_PANE_BASE_PATH, _owner_home_bin_dirs(owner_home))
    return [
        "sh",
        "-c",
        OWNER_BOUNDARY_SCRIPT,
        "sh",
        owner_user,
        "env",
        f"HOME={owner_home}",
        f"PATH={path}",
        *command,
    ]


def _pane_identity_scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in PROBE_IDENTITY_ENV_KEYS:
        env.pop(key, None)
    return env


def _role_cli_name(role: RoleConfig) -> str:
    return _command_name(role.cli[0]) if role.cli else ""


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_toml_object(path: Path) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


#: How each provider records that its account-wide first run is finished. Read
#: rather than written: Switchyard asks the CLI to run its own setup and then
#: looks again, and never manufactures the answer (SYRD-191).
FIRST_RUN_SETUP_CLIS = frozenset({"claude"})


def _format_missing_cli_launch_failure(report: FirstRunAuthReport) -> str:
    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    cli_details = "; ".join(
        f"{cli} (roles: {', '.join(roles)})" for cli, roles in report.missing_cli_roles.items()
    )
    lines = [
        f"switchyard: cannot launch panes because required CLI(s) are missing{owner_detail}: "
        f"{cli_details}. Install the missing CLI(s){owner_detail} and rerun switchyard.",
        _owner_user_cli_reminder(report.owner_user),
    ]
    width = max((len(cli) for cli in report.missing_cli_roles), default=0)
    for cli in report.missing_cli_roles:
        command = AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
        detail = command or "see that vendor's own installation documentation"
        lines.append(f"switchyard:   {cli.ljust(width)}  {detail}")
    lines.append(
        "switchyard: switchyard never fetches or runs a vendor's installer, so these commands are "
        "yours to run. It can promote an executable you already have to a host-wide copy; that "
        "offer is made before launch."
    )
    return "\n".join(lines)


def stop_before_launch_for_missing_owner_clis(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> bool:
    if not report.missing_cli_roles:
        return False
    print_func(_format_missing_cli_launch_failure(report))
    return True


def run_switchyard_launch_first_run_auth(
    config: ProjectConfig,
    *,
    validate_models: bool = False,
    #: How this process runs its PROBES -- auth status, "is it installed",
    #: model validation. Ordinary callers pass `subprocess.run` and are right
    #: to; `switchyard validate-models` and the workflow launcher's
    #: `prepare_role` both do.
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    #: Who drives the windows a person sits in front of, which is a separate
    #: question and used to be answered by `runner` alone. Any runner at all
    #: meant "fired and forgotten", so every live caller -- each passing
    #: `subprocess.run` for its probes -- silently gave up the pty, the title,
    #: the countdown and the deadline. `None` is the watched path and the right
    #: default for a person at a terminal; a suite driving the steps itself
    #: passes its own runner here (SYRD-221).
    foreground_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    print_func: Callable[[str], None] = print,
) -> FirstRunAuthReport:
    owner_user = (config.run_as_user or current_user_name()).strip()
    if not owner_user:
        return FirstRunAuthReport({}, [])
    return run_first_run_auth_phase(
        config,
        owner_user=owner_user,
        owner_home=_owner_home_for_auth(owner_user),
        validate_models=validate_models,
        runner=runner,
        foreground_runner=foreground_runner,
        print_func=print_func,
    )


def _group_ids_for_user(user_name: str) -> set[int]:
    try:
        user = pwd.getpwnam(user_name)
    except KeyError:
        return set()
    gids = {int(user.pw_gid)}
    try:
        import grp
    except ImportError:
        return gids
    for group in grp.getgrall():
        if user_name in group.gr_mem:
            gids.add(int(group.gr_gid))
    return gids


def _uid_for_user(user_name: str) -> int | None:
    try:
        return int(pwd.getpwnam(user_name).pw_uid)
    except KeyError:
        return None


def _walk_no_follow(base: Path, relative: Path) -> tuple[int, str]:
    """Open `base/relative`'s parent by walking components with O_NOFOLLOW.

    Returns (dir_fd, problem). lstat on the leaf and its immediate parent was
    not enough: an ancestor several levels up can be a symlink, and the whole
    subtree then belongs to wherever it points (SYRD-39).
    """
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return -1, "missing"
    except OSError as exc:
        return -1, f"cannot open {base} ({exc.strerror})"
    for component in relative.parent.parts:
        parent_fd_for_report = fd
        try:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                # O_DIRECTORY|O_NOFOLLOW reports ENOTDIR for a symlinked
                # directory on Linux and ELOOP elsewhere. Either way it is
                # refused; name it accurately so the manifest is actionable.
                # This is the ancestor case lstat on the leaf could not see.
                try:
                    kind = os.lstat(component, dir_fd=parent_fd_for_report)
                    if stat.S_ISLNK(kind.st_mode):
                        os.close(fd)
                        return -1, f"ancestor {component} is a symlink"
                except OSError:
                    pass
                os.close(fd)
                return -1, f"ancestor {component} is not a directory"
            os.close(fd)
            if exc.errno == errno.ENOENT:
                # Nothing there yet: absent, not unsafe.
                return -1, "missing"
            return -1, f"ancestor {component} is unusable ({exc.strerror})"
        os.close(fd)
        fd = child
    return fd, ""


def _project_entries(config_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in config_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = _load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        roles_raw = raw.get("roles")
        if not isinstance(roles_raw, list) or not roles_raw:
            continue
        try:
            slug = _validate_project_slug(str(raw.get("project") or path.stem))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
        entries.append(SwitchyardProjectEntry(slug=slug, name=name, config_path=path))
    return entries


def _registry_project_entries(registry_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    registry_dir = registry_dir or switchyard_registry_dir()
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in registry_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = _load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        if str(raw.get("schema") or "") != SWITCHYARD_REGISTRY_SCHEMA:
            continue
        try:
            slug = _validate_project_slug(str(raw.get("slug") or ""))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("name") or slug).strip() or slug
        config_path_raw = str(raw.get("config_path") or "").strip()
        if not slug or not config_path_raw:
            continue
        entries.append(SwitchyardProjectEntry(slug=slug, name=name, config_path=Path(config_path_raw).expanduser()))
    return entries


def _switchyard_entries(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> list[SwitchyardProjectEntry]:
    entries: dict[str, SwitchyardProjectEntry] = {}
    for entry in [*_project_entries(config_dir), *_registry_project_entries(registry_dir)]:
        key = entry.slug.casefold()
        entries.setdefault(key, entry)
    return sorted(entries.values(), key=lambda entry: (entry.name.casefold(), entry.slug.casefold()))


def _registered_project_collision(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> str:
    skip_resolved = skip_config_path.expanduser().resolve(strict=False) if skip_config_path is not None else None
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        if skip_resolved is not None and entry.config_path.expanduser().resolve(strict=False) == skip_resolved:
            continue
        if entry.slug.casefold() == slug.casefold():
            return (
                f"switchyard: project slug {slug!r} is already registered to "
                f"{entry.name!r} at {entry.config_path}"
            )
        if entry.name.casefold() == name.casefold():
            return (
                f"switchyard: project name {name!r} is already registered as "
                f"{entry.slug!r} at {entry.config_path}"
            )
    return ""


def _check_switchyard_registration_available(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> None:
    collision = _registered_project_collision(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=skip_config_path,
    )
    if collision:
        raise SystemExit(collision)


def _register_switchyard_project(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> Path:
    resolved_config_path = config_path.expanduser().resolve(strict=False)
    raw = _load_json(resolved_config_path)
    raw_slug = str(raw.get("project") or resolved_config_path.stem).strip()
    if not raw_slug:
        raise SystemExit(f"switchyard: cannot register {resolved_config_path}: project slug is empty")
    slug = _validate_project_slug(raw_slug)
    config = load_project_config(slug, resolved_config_path)
    name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
    registry_dir = registry_dir or switchyard_registry_dir()
    registry_path = registry_dir / f"{slug}.json"
    _check_switchyard_registration_available(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=resolved_config_path,
    )
    if registry_path.exists():
        raise SystemExit(f"switchyard: registry entry {registry_path} already exists; refusing to overwrite")
    payload = {
        "schema": SWITCHYARD_REGISTRY_SCHEMA,
        "slug": slug,
        "name": name,
        "config_path": str(resolved_config_path),
        # Recorded here because this is the last moment the configuration and a
        # root-owned, world-readable file are both in reach: afterwards the
        # configuration is under the owner's home, and a launch cannot read it
        # from the operator's side of the boundary. Without it a launch has to
        # guess which CLIs a tenant uses, and guessing "all of them" asked the
        # `test` tenant to promote a Hermes no role of its uses (SYRD-220).
        SWITCHYARD_REGISTRY_AGENT_CLIS_KEY: _configured_agent_clis(config),
    }
    try:
        registry_dir.mkdir(parents=True, exist_ok=True)
        registry_dir.parent.chmod(0o755)
        registry_dir.chmod(0o755)
        registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        registry_path.chmod(0o644)
    except OSError as exc:
        raise SystemExit(f"switchyard: failed to register project {slug!r} in {registry_dir}: {exc}") from exc
    return registry_path


def partial_provision_record(slug: str) -> Path | None:
    """Root's own record of a project whose provisioning did not finish.

    The one place a partial installation can be recognised from. The tenant's
    own directory cannot answer this -- it is writable by the account every
    role runs as, and a project that never reached registration has no registry
    entry to check either (SYRD-147).
    """
    baseline = privileged_baseline_plan_path(slug)
    try:
        info = os.stat(baseline, follow_symlinks=False)
    except OSError:
        return None
    return baseline if stat.S_ISREG(info.st_mode) else None


def _resume_provision_hint(slug: str) -> str:
    """What to say about a project that is not registered but was started."""
    if partial_provision_record(slug) is None:
        return ""
    return (
        f"switchyard: {slug!r} is not registered, but root holds a provisioning record for it: "
        f"its `switchyard new` stopped before registration. Resume it with "
        f"`sudo switchyard resume-provision {slug}`."
    )


# ---------------------------------------------------------------------------
# Finishing a provision whose privileged packet has already run (SYRD-155).
#
# The packet is the privileged half of `switchyard new`, and it is the half an
# operator runs by hand during a recovery. Everything after it -- registering
# the project and starting its roles -- belonged to the `switchyard new`
# process that had already exited, so a recovery that ran the packet perfectly
# still left the project unregistered, unnamed by every ordinary command, and
# with no sessions at all. That is what testing journal 0011 produced.
#
# So the recovery continues past the packet. It reads what the packet actually
# did rather than assuming it, it registers the generated tenant configuration
# only after checking it against root's own record, and it starts the roles
# through `launch_project` -- the same path `switchyard new` and `switchyard
# <slug>` use, which already delegates role sessions to the project owner when
# the caller is somebody else. Every phase is derived from the world rather
# than from a progress file, so an interrupted run resumes by being run again.
# ---------------------------------------------------------------------------


def _system_unit_is_active(unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    result = runner(
        ["systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return getattr(result, "returncode", 1) == 0


def read_board_declared_workflow(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[dict | None, str]:
    """The workflow document the running board is enforcing, over its own socket.

    Asked of the board rather than of any file, because this is the thing an
    adoption has to agree with: the board's copy is what decides every
    transition and capability right now, and it can only have been installed
    through the write API's own authority (SYRD-166).
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/workflow")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return None, f"the board answered HTTP {response.status} for its workflow"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return None, f"the board's workflow could not be read: {exc}"
    if not isinstance(payload, dict):
        return None, "the board's workflow response is not a document"
    document = payload.get("document")
    if document is None:
        return None, "the board is running no declared workflow"
    if not isinstance(document, dict):
        return None, "the board's workflow response carries no document"
    return document, ""


def read_board_workflow_state(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[int, dict | None, str]:
    """The board's workflow revision AND document, over its own socket.

    `read_board_declared_workflow` answers only the document, and the revision
    is what makes an install safe: it is the `expected_revision` the database
    compares under an advisory lock, so a board that changed underneath this is
    a refused write rather than a lost one.
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/workflow")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return 0, None, f"the board answered HTTP {response.status} for its workflow"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return 0, None, f"the board's workflow could not be read: {exc}"
    if not isinstance(payload, dict):
        return 0, None, "the board's workflow response is not a document"
    document = payload.get("document")
    revision = payload.get("revision")
    return (
        int(revision) if isinstance(revision, int) else 0,
        document if isinstance(document, dict) else None,
        "",
    )


def switchyard_register_command(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    registry_path = _register_switchyard_project(config_path, config_dir=config_dir, registry_dir=registry_dir)
    print_func(f"switchyard: registered {config_path.expanduser().resolve(strict=False)} at {registry_path}")
    return 0


def switchyard_menu_command(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    print_func("new...")
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        suffix = f" ({entry.slug})" if entry.name.casefold() != entry.slug.casefold() else ""
        print_func(f"{entry.name}{suffix}")
    return 0


def _list_process_command_lines(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    proc = runner(
        ["ps", "-eo", "args=", "--no-headers"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        reason = _proc_failure_reason(proc, f"ps failed with exit {proc.returncode}")
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


def switchyard_validate_models_command(
    project: str,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    entry = _resolve_switchyard_project(project, config_dir=config_dir, registry_dir=registry_dir)
    config = load_project_config(entry.slug, entry.config_path)
    report = run_switchyard_launch_first_run_auth(
        config,
        validate_models=True,
        runner=runner,
        print_func=print_func,
    )
    report_first_run_auth_warnings(report, print_func=print_func)
    return 1 if report.model_validation_failures else 0


def _resolve_switchyard_project(
    selection: str,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> SwitchyardProjectEntry:
    wanted = selection.strip().casefold()
    if not wanted:
        raise SystemExit("switchyard: project name cannot be empty")
    entries = _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir)
    exact = [
        entry
        for entry in entries
        if entry.name.casefold() == wanted or entry.slug.casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SystemExit(f"switchyard: project selector {selection!r} is ambiguous")
    fallback = [entry for entry in entries if wanted in _project_name_selector_slugs(entry.name)]
    if len(fallback) == 1:
        return fallback[0]
    words = selection.split()
    if len(words) > 1:
        first = words[0].casefold()
        first_matches = [
            entry
            for entry in entries
            if entry.slug.casefold() == first or entry.name.casefold() == first
        ]
        if len(first_matches) == 1:
            raise SystemExit(
                f"switchyard: {words[0]!r} is a project; did you mean `switchyard {first_matches[0].slug}`? "
                "A bare project name starts or attaches it."
            )
    # A project that never reached registration is unknown to every ordinary
    # command, and the installation is still there: the account, its
    # repository, its journal and its exported release. Saying only "unknown"
    # sent a live recovery looking for a workaround, so the one supported way
    # back is named here, where the failure is (SYRD-147).
    hint = _resume_provision_hint(selection.strip())
    if hint:
        raise SystemExit(hint)
    raise SystemExit(f"switchyard: unknown project {selection!r}")


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
    new_project_choices = _resolve_new_project_choices(
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
    new_project_preflight = _check_new_project_preflight(
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
    new_project_accounts = _prepare_new_project_accounts(
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
    new_project_board = _prepare_new_project_board(
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
    if not isinstance(new_project_board, NewProjectBoard):
        return new_project_board
    config = new_project_board.config
    config_path = new_project_board.config_path
    new_project_sign_in = _run_new_project_sign_in(
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
    if not isinstance(new_project_sign_in, NewProjectSignIn):
        return new_project_sign_in
    config = new_project_sign_in.config
    first_run_auth_report = new_project_sign_in.first_run_auth_report
    launch_deferred = new_project_sign_in.launch_deferred
    launch_runner = new_project_sign_in.launch_runner
    return _launch_new_project_panes(
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


def provision_runtime_command(user_name: str | None, config: ProjectConfig | None = None) -> int:
    user = (user_name or "").strip()
    if not user and config is not None:
        user = config.run_as_user or current_user_name()
    if not user:
        user = current_user_name()
    runtime_dir = ensure_user_linger_runtime(user)
    print(f"runtime ready for {user}: {runtime_dir}")
    return 0


def role_isolation_gaps(config: ProjectConfig) -> list[str]:
    """Compatibility shim for callers from the dedicated-account rollout.

    Process registration is the launch gate now. Legacy account fields remain
    readable solely so their resumable state can be repatriated safely.
    """
    legacy = [
        f"{role.role}: resumable state from {role.run_as_user} has not been repatriated"
        for role in config.roles
        if role.run_as_user and role.run_as_user != (config.run_as_user or current_user_name())
    ]
    if legacy:
        return legacy
    return []


# What makes a role the tenant's control role is what the workflow lets it do,
# not what it is called. These are the capabilities that take a ticket out of
# the ordinary flow, and no implementer or reviewer role carries them (SYRD-49).
CONTROL_ROLE_CAPABILITIES = frozenset({"set_manually_controlled", "merge"})


def control_role_name(
    config: ProjectConfig, *, config_path: Path | None = None
) -> tuple[str, str]:
    """The configured role that controls this tenant, and why not when it is not.

    A declarative tenant says which role that is by giving it the control
    capabilities, so the name is the tenant's to choose. Zero matches and more
    than one both fail closed: a privileged grant is not something to guess at.
    Only a tenant with no workflow document falls back to the historical name
    (SYRD-49).
    """
    document = None
    if config_path is not None:
        try:
            document = (_load_json(config_path) or {}).get("workflow")
        except SystemExit:
            document = None
    configured = {role.role for role in config.roles}
    if isinstance(document, Mapping) and document.get("roles"):
        matches = [
            str(role.get("name") or "")
            for role in document.get("roles") or []
            if isinstance(role, Mapping)
            and role.get("active", True)
            and CONTROL_ROLE_CAPABILITIES <= set(role.get("capabilities") or [])
        ]
        present = [name for name in matches if name in configured]
        if not present:
            return "", (
                "this project's workflow declares no active role with the control capabilities "
                f"({', '.join(sorted(CONTROL_ROLE_CAPABILITIES))})"
            )
        if len(present) > 1:
            return "", (
                "this project's workflow gives the control capabilities to more than one role: "
                + ", ".join(sorted(present))
            )
        return present[0], ""
    if "director" in configured:
        return "director", ""
    return "", "this project configures no director role"


def director_role_name(config: ProjectConfig, *, config_path: Path | None = None) -> str:
    """The control role's name, or empty when it cannot be established."""
    name, _reason = control_role_name(config, config_path=config_path)
    return name


def role_control_accounts(config: ProjectConfig) -> tuple[tuple[str, str], ...]:
    """(role, account) for every role of this project that runs as its own process.

    The control interface is rendered from this, and the artifacts that install it
    are the same ones that create the accounts -- so a configured account wins over
    the canonical name. A grant naming an account the artifact never creates matches
    nothing, which is a control interface that silently does not work (SYRD-51).
    """
    from scripts.ticket_board.project_provision import NON_PROCESS_ROLES

    owner = config.run_as_user or current_user_name()
    pairs: list[tuple[str, str]] = []
    for role in config.roles:
        name = role.role.strip().lower()
        if not name or name in NON_PROCESS_ROLES:
            continue
        if any(existing == name for existing, _account in pairs):
            continue
        account = role.run_as_user or role_account_name(config.project, name)
        if not account or account == owner:
            continue
        pairs.append((name, account))
    return tuple(pairs)


def remove_tenant_publication_boundary(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    dry_run: bool = False,
    sudoers_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Take away the publication hop this tenant was given, on the ordinary upgrade.

    The User restored the project account's write access to the forge, so an
    implementer publishes by pushing and there is nothing for a root-owned
    publisher to do (SYRD-123). Leaving it installed would leave a NOPASSWD rule
    to a privileged program in place while the documentation says there is no
    privileged gate, and the first of those two is the one that would still be
    true.

    What goes: the sudo rule, and -- through the retirement list in
    `role_tooling_staging_commands` -- the staged copies of the programs it
    named. What stays: the root-owned key and grant under /etc/switchyard, which
    no role can read and nothing now runs. Deleting a credential is not
    reversible and this decision has already been reversed once, so it is not
    this upgrade's to make; with the rule gone there is no path to it.
    """
    from scripts.ticket_board.project_provision import publish_sudoers_path

    sudoers_path = Path(publish_sudoers_path(config.project, root=sudoers_root))
    if dry_run:
        if sudoers_path.exists():
            print_func(f"switchyard: would remove {config.project}'s publication sudo rule {sudoers_path}")
        return []
    if os.geteuid() != 0:
        return [f"{sudoers_path} can only be removed by root"]
    if not sudoers_path.exists():
        return []
    removed = runner(
        ["rm", "-f", str(sudoers_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if getattr(removed, "returncode", 1) != 0:
        detail = (str(getattr(removed, "stderr", "") or "").strip() or "no output")[:300]
        return [f"could not remove {sudoers_path}: {detail}"]
    # Gone, not "the command said so". A grant still on disk is still a grant,
    # and an upgrade that reports it removed is the one way this can be worse
    # than leaving it alone.
    if sudoers_path.exists():
        return [f"{sudoers_path} is still on disk after removing it"]
    print_func(
        f"switchyard: removed {config.project}'s publication sudo rule; implementers publish by "
        "pushing to the project remote and nothing here runs as root"
    )
    return []


def install_tenant_publication_boundary(
    config: ProjectConfig,
    *,
    release,
    publish_remote: str = "",
    config_path: Path | None = None,
    dry_run: bool = False,
    sudoers_root: Path | None = None,
    registration_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Give an existing tenant the publication boundary a fresh one is given.

    `publish_grant_commands()` is reachable from the operator command packet and
    from nowhere else, and the shared-project-account path skips the role-account
    migration that used to carry it, so a tenant that was provisioned before
    SYRD-93 upgrades everything except the one thing that stops every role under
    the shared account from pushing. This is that step, on the ordinary upgrade,
    run by root in this process rather than written out for an operator to
    execute -- a generated file is writable by every role under one account, and
    running it as root would hand them root (SYRD-97).
    """
    from scripts.ticket_board.project_provision import (
        publish_sudoers_document,
        publish_sudoers_path,
    )
    from scripts.ticket_board.publication_boundary import (
        install_publication_boundary,
        report_publication_outcome,
    )

    owner_user = config.run_as_user or current_user_name()
    sudoers_path = str(publish_sudoers_path(config.project, root=sudoers_root))
    # The credential this boundary exists to replace. Named so the report can
    # print the exact bounded check for it, and so its fingerprint keys the
    # evidence: a rotated shared key is not the one a verdict was about.
    from scripts.ticket_board.project_provision import (
        owner_github_key_path,
        resolve_owner_github_identity,
    )

    # Which key this tenant actually publishes with, not the default name. A
    # verdict recorded against the wrong key would be a verdict about a
    # credential nobody uses (SYRD-100 found the same trap from the other side).
    owner_home = home_dir_for_user(owner_user)
    plan_data = _plan_data_from_config(config, config_path) if config_path else {}
    selected = resolve_owner_github_identity(
        str(owner_home),
        recorded_key_name=str(plan_data.get("owner_github_key_name") or ""),
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    shared_identity = owner_github_key_path(
        str(owner_home), key_name=selected.key_name if selected.resolved else ""
    )
    outcome = install_publication_boundary(
        project=config.project,
        release=release,
        registration_root=registration_root or switchyard_privileged_provision_root(),
        sudoers_path=sudoers_path,
        sudoers_document=publish_sudoers_document(config.project, owner_user),
        declared_remote=publish_remote,
        shared_identity_file=shared_identity,
        owner_user=owner_user,
        dry_run=dry_run,
        runner=runner,
        print_func=print_func,
    )
    for problem in outcome.problems:
        print_func(f"warning: switchyard: {problem}")
    if not dry_run:
        # After the problems, and driven by what is on disk: a report that runs
        # before the verdict prints artifacts a failed run never reached.
        report_publication_outcome(outcome, project=config.project, print_func=print_func)
    # Pending is not success. It used to reach nobody, so a failed keyscan left
    # the phase recorded done while the boundary was demonstrably unfinished
    # (SYRD-97 review).
    return list(outcome.problems) + [
        f"{path} was not installed, so {config.project}'s publication boundary is incomplete"
        for path in outcome.pending
    ]


def _recovered_pin_behind_host(
    config: "ProjectConfig",
    *,
    source_repo: Path | None,
    deploy_ref: str | None,
    dry_run: bool,
    tooling_root: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> int | None:
    """Stop an unpinned upgrade whose remembered release is behind the host's.

    An upgrade given no release keeps the one the tenant was last pinned to
    (SYRD-61), which is right for resuming one upgrade and wrong once an
    operator has installed a newer shared release: the launcher running this
    is that newer release, the old pin cannot be staged by it, and the old
    advice -- install the pinned release -- meant reinstalling the obsolete one
    (SYRD-284). Checked before anything of the tenant's is written.

    As root it does the one thing that is the host's rather than the tenant's:
    installs the privileged boundary from the release it is running, so the
    pinned preview and upgrade it then names exist. Returns None when the pin
    is not behind, so the upgrade carries on unchanged.
    """
    running = running_launcher_release()
    if running is None or not running.marker_commit:
        return None
    selected, _problems = resolve_trusted_upgrade_release(
        (source_repo or _repo_root()).expanduser().resolve(strict=False),
        deploy_ref or "", dry_run=True, ref_is_pinned=True, runner=runner,
    )
    pinned = selected.commit if selected is not None else str(deploy_ref or "")
    current = running.marker_commit
    if not pinned or pinned == current:
        return None
    print_func(
        f"switchyard: {config.project} is pinned to {pinned} from its last upgrade, and this host "
        f"now runs {current}. An upgrade given no release keeps the tenant's pin, and the launcher "
        f"running it is not that release, so nothing of {config.project}'s was changed."
    )
    if os.geteuid() == 0 or dry_run:
        untrusted: list[str] = []
        if not dry_run and switchyard_shared_install_root() == DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT:
            from scripts.ticket_board.project_provision import untrusted_root_executable_reasons

            untrusted = untrusted_root_executable_reasons(Path(os.path.realpath(__file__)), owner_uid=0)
        if untrusted:
            print_func(
                "switchyard: this host's privileged boundary was not installed: this is not "
                f"running from root-owned code ({untrusted[0]})"
            )
            return 1
        problems = install_host_privileged_boundary(
            running.root, dry_run=dry_run, staging_root=tooling_root, runner=runner, print_func=print_func
        )
        if problems:
            for problem in problems:
                print_func(f"switchyard: {problem}")
            return 1
    else:
        print_func(
            f"switchyard: this host's privileged boundary comes from {current}; as root it is "
            f"installed by `switchyard privileged-action {config.project} upgrade-tenant`, which "
            "stops here in the same way"
        )
    print_func(
        f"switchyard: to move {config.project} to {current}, preview it and then apply it, pinned: "
        f"`switchyard privileged-action {config.project} preview-upgrade commit={current}`, then "
        f"`switchyard privileged-action {config.project} upgrade-tenant-release commit={current}`"
    )
    return 1


def install_host_privileged_boundary(
    release_root: Path,
    *,
    dry_run: bool = False,
    staging_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install this host's privileged boundary from one installed release.

    The boundary -- the root-owned helper, its package and the polkit catalogue
    -- is host-wide, and its helper always runs the SHARED launcher. So it is
    installed from the shared release the host runs, never from a tenant's pin.
    Staging it only inside a tenant upgrade, from that tenant's pinned release,
    is what left MEFP unable to receive `preview-upgrade`: its pin was older
    than the host (SYRD-284). The same `install_commands` provisioning and
    staging use, run directly as root; re-runnable, and it removes the boundary
    when the release carries none (a rollback to a release older than it).
    """
    from scripts.ticket_board import privileged_install

    root, policy_dir = privileged_install.roots_for(staging_root)
    if dry_run:
        print_func(f"switchyard: would install this host's privileged boundary from {release_root}")
        return []
    commands = privileged_install.install_commands(
        str(release_root), root=root, policy_dir=policy_dir, sudo=""
    )
    script = "\n".join(line for line in commands if line and not line.lstrip().startswith("#"))
    result = runner(["sh", "-euc", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(result, "returncode", 1) != 0:
        return [
            f"the privileged boundary could not be installed from {release_root} "
            f"(exit {result.returncode}): {(str(result.stderr or '').strip() or 'no output')[:400]}"
        ]
    problems = privileged_install.verify_installation(root, policy_dir)
    if not problems:
        print_func(f"switchyard: installed this host's privileged boundary from {release_root}")
    return problems


def running_launcher_release(root: Path | None = None) -> SharedSwitchyardRelease | None:
    """Which installed release this process is executing out of, if any."""
    return shared_switchyard_release_for_path(
        (root or Path(__file__).resolve().parent.parent)
    )


def release_rollback_path(project: str) -> Path:
    """Root's own note of what this host was running before an upgrade."""
    return privileged_provision_dir(
        project, root=switchyard_privileged_provision_root()
    ) / "release-rollback.json"


def record_release_rollback(
    config: ProjectConfig,
    *,
    release,
    staging_root: Path | None = None,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Write down what to come back to, before anything is replaced.

    An upgrade repoints the shared release, restages the tenant's root-owned
    tooling and rewrites its sudo grant. Each of those is recoverable only if
    something remembers what was there: otherwise the way back is whatever an
    operator can reconstruct from timestamps under /opt, at the moment they are
    least able to reconstruct anything.

    Written before the first replacement and left alone afterwards, so a retry
    that runs after a partial upgrade still names the release the host was whole
    on rather than the half-installed one it is on now (SYRD-93 live
    acceptance).
    """
    if dry_run:
        return []
    install_root = switchyard_shared_install_root()
    pointer = install_root / "current"
    previous_root = ""
    try:
        if os.path.islink(pointer):
            previous_root = os.readlink(pointer)
    except OSError as exc:
        return [f"could not read the current release pointer {pointer}: {exc}"]
    previous = shared_switchyard_release_for_path(Path(previous_root)) if previous_root else None
    staged = _staged_tooling_dir(config, staging_root)
    staged_marker = staged / SWITCHYARD_RELEASE_MARKER_NAME
    staged_commit = ""
    try:
        if staged_marker.is_file():
            staged_commit = str(
                json.loads(staged_marker.read_text(encoding="utf-8")).get("commit") or ""
            )
    except (OSError, ValueError):
        staged_commit = ""
    record = {
        "schema": RELEASE_ROLLBACK_SCHEMA,
        "project": config.project,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "upgrading_to": release.commit,
        "previous_release_root": previous_root,
        "previous_release_commit": previous.marker_commit if previous else "",
        "previous_staged_commit": staged_commit,
    }
    path = release_rollback_path(config.project)
    if path.is_file():
        # A retry after a partial upgrade must not overwrite the note taken when
        # the host was last whole. Only a record of a DIFFERENT upgrade is
        # replaced.
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if str(existing.get("upgrading_to") or "") == release.commit:
            return []
    try:
        ensure_privileged_provision_dir(path.parent)
        _write_private_json_atomic(path, record)
        path.chmod(privileged_artifact_mode(path.name))
    except OSError as exc:
        return [f"could not record the rollback for {config.project}: {exc}"]
    print_func(
        f"switchyard: recorded the way back for {config.project} in {path}: "
        + (record["previous_release_commit"] or previous_root or "no previous release")
    )
    return []


def record_publication_remote(
    project: str,
    remote: str,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> tuple[bool, str, list[str]]:
    """Root's own record of where this tenant publishes, from `--publish-remote`.

    `resolve_pinned_remote` has always read this file, but the only thing that
    wrote it was the publication boundary's installer, and the boundary was
    retired -- so `switchyard upgrade <project> --publish-remote <url>` stopped
    being remembered, and everything that decides by the remote found none.
    Live on mefp: `set-owner-identity mefp --clear` refused for want of a pin
    right after an upgrade that had been given one (SYRD-229). Written here
    alone, in root's private provision directory; nothing of the boundary is
    recreated. An unchanged value is left as it is.

    Returns (whether it was changed, what it replaced -- "" for nothing --, and
    why not). The upgrade calls this before its first write and treats any
    problem as fatal, so a failed pin never follows a half-done upgrade.
    """
    from scripts.ticket_board.publication_boundary import publish_remote_registration_path

    value = remote.strip()
    if not value or len(value) > 1024 or any(ch.isspace() or not ch.isprintable() for ch in value):
        return False, "", [
            f"{remote!r} is not a single git remote, so it cannot be recorded as {project}'s "
            "publication remote"
        ]
    path = publish_remote_registration_path(project, switchyard_privileged_provision_root())
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return False, "", [f"{path} is not a regular file, so {project}'s publication remote cannot be recorded there"]
    try:
        current = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError as exc:
        return False, "", [f"{path} cannot be read ({exc.strerror})"]
    if current == value:
        print_func(f"switchyard: {project}'s publication remote is already recorded as {value}")
        return False, current, []
    if dry_run:
        print_func(
            f"switchyard: would record {project}'s publication remote as {value} in {path}"
            + (f" (replacing {current})" if current else "")
        )
        return False, current, []
    if os.geteuid() != 0:
        return False, current, [
            f"recording {project}'s publication remote is root's; run the upgrade with sudo"
        ]
    problem = _write_publication_remote(path, value)
    if problem:
        return False, current, [problem]
    print_func(f"switchyard: recorded {project}'s publication remote as {value} in {path}")
    return True, current, []


def _write_publication_remote(path: Path, value: str) -> str:
    try:
        ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (value + "\n").encode("utf-8"))
        finally:
            os.close(descriptor)
        os.replace(staged, path)
    except OSError as exc:
        return f"could not record the publication remote at {path}: {exc}"
    return ""


def restore_publication_remote(project: str, previous: str) -> str:
    """Put the pin back as it was, after a later step of the same upgrade refused."""
    from scripts.ticket_board.publication_boundary import publish_remote_registration_path

    path = publish_remote_registration_path(project, switchyard_privileged_provision_root())
    if previous:
        return _write_publication_remote(path, previous)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return f"could not remove {path}: {exc}"
    return ""


def release_rollback_commands(project: str, *, publish_remote: str = "") -> list[str]:
    """The exact way back, from root's own note. Empty when there is nothing to say."""
    path = release_rollback_path(project)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if str(record.get("schema") or "") != RELEASE_ROLLBACK_SCHEMA:
        return []
    previous_root = str(record.get("previous_release_root") or "")
    previous_commit = str(record.get("previous_release_commit") or "")
    if not previous_root or not previous_commit:
        return []
    install_root = switchyard_shared_install_root()
    pointer = shlex.quote(str(install_root / "current"))
    remote = publish_remote or "<url>"
    return [
        f"sudo ln -sfn {shlex.quote(previous_root)} {pointer}",
        # The same reviewed path the upgrade took, pointed backwards: it
        # restages the tenant's tooling and rewrites its grant from the release
        # being returned to, rather than leaving the two halves disagreeing.
        f"sudo switchyard upgrade {shlex.quote(project)} --source-repo {shlex.quote(previous_root)} "
        f"--deploy-ref {shlex.quote(previous_commit)} --publish-remote {shlex.quote(remote)}",
    ]


#: The label an install attempt carries in the rollout journal.
INSTALL_ROLLOUT_LABEL = "install"
#: What a first install records once root-owned machinery exists to record with.
INSTALL_BOUNDARY_ROLLOUT_LABEL = "install-boundary"


def installed_rollout_recorder() -> Path | None:
    """The recorder root already has, or None when this host has none yet.

    Deliberately NOT `_rollout_recorder_path`, which falls back to the
    checkout's copy. That fallback is right for a step whose surrounding release
    is already installed and running; it is wrong for the step that installs
    one, because on a first install the checkout is precisely the unverified,
    tenant-writable tree this whole design exists to keep root out of. A host
    with no installed recorder has nothing trustworthy to record with, and
    saying so is better than executing an untrusted recorder and calling the
    result a root-owned record (SYRD-172).
    """
    installed = switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    return installed if installed.is_file() else None


def recorded_install_command(
    privileged_lines: "Sequence[str]",
    *,
    project: str,
    commit: str,
    recorder: Path,
    label: str = INSTALL_ROLLOUT_LABEL,
) -> str:
    """The whole bootstrap chain as ONE recorded attempt.

    The install is the step that puts new root-executed code on the host, and
    the only one whose input -- the bundle -- was produced by an unprivileged
    account. Everything the journal records afterwards runs FROM what this step
    installed, so a journal that begins after it cannot answer which release was
    installed, by whom, or from which bundle (SYRD-172).

    In whole, through one `bash -c`, for the reason `recorded_rollout_command`
    gives about the steps already wrapped: the chain carries its own `&&`, and
    leaving part of it outside the recorder would record the first command and
    run the rest unrecorded. `set -euo pipefail` in front so a failure anywhere
    ends the chain -- a half-installed release must be a FAILED attempt with no
    unrecorded suffix, not a successful one that stopped early.
    """
    for line in privileged_lines:
        if "switchyard-record-rollout" in line:
            raise ValueError(
                "refusing to wrap a chain that already records itself: "
                "nested attempts make the journal describe one install twice"
            )
    chain = "set -euo pipefail\n" + "\n".join(privileged_lines)
    prefix = ["sudo", str(recorder), project]
    if commit:
        prefix += ["--target-commit", commit]
    if label:
        prefix += ["--label", label]
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(chain)]
    )


def install_boundary_command(
    *, project: str, commit: str, recorder: Path, release_root: Path, pointer: Path
) -> str:
    """What a FIRST install can record, once there is something to record with.

    There is an irreducible gap on a host with no installed release: the bytes
    that would record the install do not exist until the install has put them
    there. The gap is not papered over by running the checkout's recorder --
    that would be root executing the tenant-writable tree this design refuses --
    so the boundary is recorded immediately AFTER, by the root-owned recorder the
    install itself just installed, and what it records is a verification rather
    than a claim: that `current` resolves to the release directory for this exact
    commit (SYRD-172).
    """
    verification = (
        f"test \"$(readlink -f {shlex.quote(str(pointer))})\" = {shlex.quote(str(release_root))}"
    )
    prefix = ["sudo", str(recorder), project, "--target-commit", commit,
              "--label", INSTALL_BOUNDARY_ROLLOUT_LABEL]
    return " ".join(
        [*(shlex.quote(token) for token in prefix), "--", "bash", "-c", shlex.quote(verification)]
    )


def trusted_bootstrap_commands(
    source_repo: Path,
    commit: str,
    *,
    project: str = "",
    publish_remote: str = "",
) -> list[str]:
    """How root reaches an exact release without reading a role-writable repository.

    The public wrapper dispatches privileged commands to whatever
    `<install root>/current` points at, so an operator who upgrades a checkout
    and runs `sudo switchyard upgrade` is still running whatever was installed
    last. The way out cannot be `sudo ./install`: that executes a script from the
    checkout, and under one shared account (SYRD-69) every role can write it.

    Nor can root simply read that checkout with an exact sha. A full sha does NOT
    make the content immutable: `git archive <sha>` honours `refs/replace/<sha>`,
    so whoever can write the repository can substitute the tree -- and the old
    installer executes the exported release before activating it, which turns
    that substitution into code execution as root. Git config in that repository
    can name programs to run as well. Root is given no access to it at all
    (SYRD-97 review).

    Instead the operator, unprivileged and in their own repository, produces a
    bundle; root builds its own repository from that bundle and demands the exact
    commit inside it. Object lookup is by content hash, so an object served under
    that sha hashes to it or git does not return it, and a bundle that does not
    carry it leaves root with nothing to check out. Replacement is disabled
    throughout, and only root's own configuration is ever in effect.
    """
    install_root = switchyard_shared_install_root()
    bootstrap = install_root / "bootstrap"
    src = bootstrap / "src"
    # Inside the checkout, because the operator writes it as themselves and the
    # root-owned bootstrap directory is not theirs to write. Root only reads it,
    # and reading it is safe for the same reason the whole design is: root
    # demands the exact commit afterwards, so anything else fails closed.
    bundle = source_repo / f".switchyard-bootstrap-{commit}.bundle"
    ref = f"refs/switchyard/bootstrap-{commit}"
    repo, sha = shlex.quote(str(source_repo)), shlex.quote(commit)
    q_src, q_bundle, q_ref = shlex.quote(str(src)), shlex.quote(str(bundle)), shlex.quote(ref)
    installer = shlex.quote(str(install_root / "current" / "scripts" / "install-switchyard"))
    release = shlex.quote(str(install_root / "releases" / commit))
    # Nothing root runs reads the checkout, so `no-replace` here is the
    # operator's own protection rather than the boundary.
    no_replace = "env GIT_NO_REPLACE_OBJECTS=1"
    unprivileged = [
        # Unprivileged, in the operator's own repository, as themselves.
        f"{no_replace} git -C {repo} update-ref {q_ref} {sha}",
        f"{no_replace} git -C {repo} bundle create {q_bundle} {q_ref}",
    ]
    privileged = [
        f"sudo install -d -m 0755 -o root -g root {shlex.quote(str(bootstrap))}",
        # Root, in a repository root creates, reading only that bundle.
        f"sudo {no_replace} git init -q {q_src}",
        f"sudo {no_replace} git -C {q_src} -c fetch.fsckObjects=true fetch --no-tags "
        f"{q_bundle} {q_ref}:{q_ref}",
        # The exact commit, or nothing: a bundle that does not carry it fails
        # here and no release is built.
        f"sudo {no_replace} git -C {q_src} checkout -q --detach {sha}",
        f"sudo {no_replace} SWITCHYARD_SOURCE_REPO={q_src} SWITCHYARD_SOURCE_REF={sha} "
        f"{installer} --apply",
    ]
    # Repointing `current` is not enough. The root-owned upgrade source record
    # still pins whatever was selected last, and the next upgrade recovers it
    # and goes straight back. This is what durably re-selects the reviewed
    # release, and it is the first command that runs the reviewed code.
    select = (
        f"sudo switchyard upgrade {shlex.quote(project or '<project>')} --source-repo {release} "
        f"--deploy-ref {sha} --publish-remote {shlex.quote(publish_remote or '<url>')}"
    )
    recorder = installed_rollout_recorder()
    if recorder is not None and project and commit:
        # AN UPGRADE. Root already holds a recorder it installed, so the step
        # that installs the next release leaves an attempt of its own: operator,
        # pkexec, target commit, exit status and output hashes, like every other
        # privileged step (SYRD-172).
        return [
            *unprivileged,
            recorded_install_command(privileged, project=project, commit=commit, recorder=recorder),
            select,
        ]
    if not project or not commit:
        # Rendered as guidance rather than for a particular host and release;
        # there is nothing to name in a record.
        return [*unprivileged, *privileged, select]
    # A FIRST INSTALL, and the gap is stated rather than hidden. There is no
    # root-owned recorder on this host yet, and the checkout's copy is the
    # tenant-writable tree root refuses to execute -- so these lines run
    # unrecorded, and the boundary is recorded immediately afterwards by the
    # recorder this install itself installs, against what actually landed.
    boundary = install_boundary_command(
        project=project, commit=commit,
        recorder=install_root / "current" / "scripts" / "switchyard-record-rollout",
        release_root=install_root / "releases" / commit,
        pointer=install_root / "current",
    )
    return [
        *unprivileged,
        f"# First install on this host: {install_root}/current does not exist yet, so there is no",
        "# root-owned switchyard-record-rollout to record the next four lines with. They run",
        "# unrecorded -- deliberately, rather than executing the recorder out of an unverified",
        "# checkout -- and the line after them records the boundary against what landed.",
        *privileged,
        boundary,
        select,
    ]


def stale_launcher_problems(
    release, *, source_repo: Path, project: str = "", publish_remote: str = ""
) -> list[str]:
    """Refuse when this process is not the release it is about to install.

    `/usr/local/bin/switchyard` sends privileged commands to whatever
    `<install root>/current` points at. An operator who pulls a checkout and runs
    `sudo switchyard upgrade` is therefore running the previously installed
    launcher, which does not contain this code at all -- so the upgrade quietly
    stages that older release's tools and reports success. It is the same trap as
    "pulling is not installing", one level up, and the only symptom is that the
    thing the operator was told exists is not there.

    Nothing can be done about it from inside the old launcher, which is why this
    is stated as a precondition of the new one: if this process is running from
    an installed release that is not the one selected, it stops and names the
    bootstrap (SYRD-97 review).
    """
    running = running_launcher_release()
    if os.geteuid() == 0 and switchyard_shared_install_root() == DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT:
        # Running privileged out of a path root does not control is the same
        # escalation SYRD-97 refused one level down. There, root was asked to
        # read a role-writable repository to build a release; here root is
        # already executing the launcher out of one -- so every privileged step
        # it is about to take, and every artifact it is about to render, comes
        # from bytes any role can rewrite. The dry run of an upgrade from a
        # worktree proposed exactly that and nothing stopped it (SYRD-93 live
        # acceptance).
        from scripts.ticket_board.project_provision import untrusted_root_executable_reasons

        # Against uid 0 by name: the identity entitled to have written what root
        # executes is root, whatever uid happens to be reading this. A
        # redirected install root is a sandbox by construction, which is why the
        # check above is scoped to the real one -- the same convention the
        # privileged provision root already uses.
        untrusted = untrusted_root_executable_reasons(
            Path(os.path.realpath(__file__)), owner_uid=0
        )
        if untrusted:
            return [
                "this command is running as root out of a path root does not control: "
                + untrusted[0],
                "nothing privileged was staged. Install the release you want with root-owned "
                "code only, and run the upgrade from that:",
                *(
                    f"  {line}"
                    for line in trusted_bootstrap_commands(
                        source_repo, release.commit, project=project, publish_remote=publish_remote
                    )
                ),
            ]
    if running is None:
        # A checkout, not an installed release. Ordinary for a developer run;
        # the privileged case was refused above, and the wrapper's dispatch is
        # what the rest of this is about.
        return []
    if running.marker_commit == release.commit:
        return []
    running_name = running.marker_commit or f"an unmarked release at {running.root}"
    return [
        f"this command is running from installed release {running_name}, but the upgrade "
        f"selected {release.commit}. The public `switchyard` wrapper dispatches privileged "
        "commands to whatever is installed, so it is not running the release you pinned and "
        "nothing privileged was staged.",
        "install that release first, with root-owned code only:",
        *(
            f"  {line}"
            for line in trusted_bootstrap_commands(
                source_repo, release.commit, project=project, publish_remote=publish_remote
            )
        ),
        "then re-run this command.",
    ]


def resolve_trusted_upgrade_release(
    source_repo: Path,
    commit: str,
    *,
    ref_is_pinned: bool = False,
    install_root: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
):
    """The immutable root-owned tree every privileged artifact is installed from.

    This has to happen before staging, not after it. `refresh_staged_role_tooling`
    copies `switchyard-publish-ref` -- the one program the NOPASSWD rule grants
    root on -- into a root-owned path, and it used to copy it out of the source
    checkout. Under one shared account (SYRD-69) a role can write that checkout,
    so root was copying role-writable bytes into the program root would later run
    for them: planting the file was a root shell. The commit selects what is
    staged now, and `git archive` reads the object store rather than the working
    tree (SYRD-97 review).
    """
    from scripts.ticket_board.publication_boundary import (
        TrustedRelease,
        materialize_trusted_release,
        read_release_marker,
        root_controlled_problems,
    )

    # The pinned source may already BE an immutable release -- an operator who
    # pinned /opt/switchyard/releases/<sha> has handed us the exact thing this
    # would otherwise go and build. It is a tree, not a checkout, so asking git
    # which commit it is fails; its marker says. Consumed rather than rebuilt,
    # after the same whole-path check (SYRD-97 review).
    marker = read_release_marker(source_repo)
    marked_commit = str(marker.get("commit") or "").strip()
    if marked_commit:
        overridden_root = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
        problems = root_controlled_problems(
            str(source_repo), base=overridden_root or "/"
        )
        if problems:
            return None, [
                f"{source_repo} is an installed release but is not root-controlled, so it "
                "will not be used"
            ] + problems
        # Being a release is not the same as being the release that was asked
        # for. An operator pointing at the installed one while pinning a newer
        # ref would otherwise stage the OLD tools and be told it worked, which
        # is the stale-global-release case this ticket is about (SYRD-97 review).
        wanted = (commit or "").strip()
        # A ref only competes with the marker when somebody actually chose it.
        # The resolver fills one in when only a source was pinned, and treating
        # that default as a pin refuses the ordinary "install exactly this
        # release" case.
        if wanted and ref_is_pinned and wanted != marked_commit:
            if re.fullmatch(r"[0-9a-f]{40}", wanted):
                # An exact commit needs no resolution to be compared, so there is
                # no state in which this can fail open (SYRD-97 review).
                return None, [
                    f"{source_repo} is the installed release for {marked_commit}, but this "
                    f"upgrade is pinned at {wanted}. Nothing was staged: pointing at one "
                    "release while pinning another installs the older tools and reports success."
                ]
            # Symbolic, and there is nowhere trustworthy to resolve it. The
            # release tree is not a repository, and the checkout and cache it
            # records are writable by the account every role runs as -- a role
            # could move that ref back onto the old release and be believed.
            return None, [
                f"{source_repo} is the installed release for {marked_commit}, and this upgrade "
                f"is pinned at {wanted!r}, which is a name rather than a commit. It is not "
                "resolved here: the repositories that could resolve it are writable by the "
                "account every role runs as. Pin the exact commit instead."
            ]
        return TrustedRelease(root=source_repo, commit=marked_commit), []

    selected = _selected_release_commit(source_repo, commit, runner=runner)
    if not selected:
        return None, [
            f"could not resolve which commit {source_repo} is being installed from, so no "
            "release can be verified and nothing privileged can be staged from it"
        ]
    root = install_root or switchyard_shared_install_root()
    # "/" on a host. It moves only when the shared install root has been
    # overridden, which is the documented seam for exercising the real
    # privileged branch without writing to the host's /opt; a fixture cannot own
    # "/", and refusing its temp directory would be the correct answer to the
    # wrong question.
    overridden = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", "").strip()
    return materialize_trusted_release(
        selected,
        source_repo=source_repo,
        install_root=root,
        trust_base=str(root) if overridden else "/",
        runner=runner,
        dry_run=dry_run,
    )


def _selected_release_commit(
    source_repo: Path,
    ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """The exact commit the upgrade is installing, named rather than implied."""
    candidate = (ref or "").strip() or "HEAD"
    proc = runner(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{candidate}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return str(getattr(proc, "stdout", "") or "").strip()


def refresh_staged_role_tooling(
    config: ProjectConfig,
    *,
    release_root: Path,
    staging_root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Restage the root-owned per-role tooling from the selected release.

    Staging used to happen only inside the account-creation script, so a tenant
    whose accounts already existed skipped it: the roles kept the previous
    release's hooks and board clients while the board moved on under them, and
    the provenance marker named a release the staged files did not come from.
    It is part of the artifacts phase now, which runs on every upgrade including
    a resumed one, and it stages from the exact selected release rather than
    from the `current` symlink (SYRD-62).

    The same renderer the operator script uses, so what an upgrade installs and
    what that script installs cannot drift apart.
    """
    script = "set -eu\n" + "\n".join(
        role_tooling_staging_commands(config.project, str(release_root), staging_root=staging_root)
    )
    result = runner(["sh", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(result, "returncode", 1) != 0:
        detail = (str(getattr(result, "stderr", "") or "").strip() or "no output")[:400]
        return [
            f"could not stage {config.project}'s role tooling from {release_root} "
            f"(exit {result.returncode}): {detail}"
        ]
    problems = staged_role_tooling_problems(
        config.project, str(release_root), staging_root=_staged_tooling_dir(config, staging_root)
    )
    if problems:
        return problems
    print_func(
        f"switchyard: staged {config.project} role tooling in "
        f"{_staged_tooling_dir(config, staging_root)} from {release_root}"
    )
    return []


#: Who owns a staged bundle on a host: root, always. `install -o root -g root`
#: is what puts it there, and every account that later READS it -- the desktop
#: operator running `switchyard new`, the project owner the control bridge
#: crosses to -- is somebody else. The verifier defaults to the reader's own
#: uid, which is the right default for the sandbox fixtures that stage as
#: themselves and the wrong one for every production path, so this says root
#: explicitly and the override exists only for those fixtures (SYRD-249 review).
STAGED_TOOLING_OWNER_UID = 0


def ensure_staged_role_tooling(
    config: ProjectConfig,
    *,
    release_root: Path | None = None,
    staging_root: Path | None = None,
    install_root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
    euid_getter: Callable[[], int] = os.geteuid,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """The bundle a pane runs, present and root-owned, or why it is not.

    Checked before any window opens. A modern single-owner tenant declares no
    per-role accounts, and the whole staging step used to sit behind a test for
    those, so provisioning reported success and then opened six tabs onto
    `sudo: /usr/local/lib/switchyard/<project>/switchyard-display-attach:
    command not found` (SYRD-249).

    Asked through `staged_bundle_launch_problems`, which is the same question
    the operator's half asks before it crosses: a launch repairs absence, never
    drift, and judges a tenant against the release its own bundle was staged
    from. An older tenant whose bundle is complete is healthy, and stays on the
    release its board is pinned to.

    Repaired here only by root, and root is the only account that could: the
    staging commands install as `root:root` at a path no tenant may write. An
    unprivileged caller -- the owner the bridge crossed to -- is told what is
    wrong and runs nothing.
    """
    absent, hostile, release = staged_bundle_launch_problems(
        config.project,
        release_root=str(release_root) if release_root is not None else "",
        root=staging_root,
        install_root=install_root,
        expect_uid=expect_uid,
    )
    if hostile:
        return hostile + [
            f"{config.project}'s staged tooling is not something a launch may replace"
        ]
    if not absent:
        return []
    repair = (
        f"`switchyard start {config.project}` from the operator's own account repairs this "
        f"before it crosses, and `sudo switchyard upgrade {config.project}` restages it"
    )
    if euid_getter() != 0:
        return absent + [
            f"{config.project}'s panes would open on tooling that is not staged, and repairing "
            f"it is root's: {repair}"
        ]
    staged = _staged_tooling_dir(config, staging_root)
    print_func(
        f"switchyard: {config.project}'s staged role tooling in {staged} is incomplete; "
        f"restaging it from {release}"
    )
    repaired = refresh_staged_role_tooling(
        config, release_root=release, staging_root=staging_root,
        runner=runner, print_func=print_func,
    )
    if not repaired:
        return []
    return repaired + [
        f"{config.project}'s panes would open on tooling that is not there: {repair}"
    ]


def _staged_tooling_dir(config: ProjectConfig, staging_root: Path | None) -> Path:
    return Path(role_tooling_staging_dir(config.project, root=staging_root))


PENDING_IDENTITIES_SCHEMA = "switchyard.pending-identities.v1"


def pending_identities_path(config: ProjectConfig) -> Path:
    """Root's record of the accounts a tenant is moving onto, before it moves.

    Preparation -- creating accounts and homes, staging tooling, seeding
    credentials -- has to know the accounts before the active configuration
    names them, because naming them early is what breaks the running roles. The
    plan is root's, so preparation reads an identity list the tenant did not
    write (SYRD-45).
    """
    return privileged_provision_dir(
        config.project, root=switchyard_privileged_provision_root()
    ) / "pending-identities.json"


def write_pending_identities(config: ProjectConfig) -> dict[str, dict[str, str]]:
    """Publish the pending plan where only root can write it."""
    identities = canonical_role_identities(config)
    if os.geteuid() != 0:
        return identities
    path = pending_identities_path(config)
    payload = {
        "schema": PENDING_IDENTITIES_SCHEMA,
        "project": config.project,
        "owner": config.run_as_user or current_user_name(),
        "roles": identities,
    }
    try:
        ensure_privileged_provision_dir(path.parent)
        staged = path.with_name(f".{path.name}.new")
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            os.fchmod(descriptor, privileged_artifact_mode(path.name))
            try:
                os.fchown(descriptor, 0, 0)
            except OSError:
                pass
        finally:
            os.close(descriptor)
        staged.replace(path)
    except OSError as exc:
        print(f"switchyard: could not record {config.project} pending identities: {exc}", file=sys.stderr)
    return identities


def read_pending_identities(config: ProjectConfig) -> dict[str, dict[str, str]]:
    """Root's pending plan, or the canonical derivation when none is recorded."""
    try:
        payload = json.loads(pending_identities_path(config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return canonical_role_identities(config)
    if str(payload.get("schema") or "") != PENDING_IDENTITIES_SCHEMA:
        return canonical_role_identities(config)
    roles = payload.get("roles")
    if not isinstance(roles, dict):
        return canonical_role_identities(config)
    return {
        str(role): {
            "account": str(entry.get("account") or ""),
            "home": str(entry.get("home") or ""),
            "worktree": str(entry.get("worktree") or ""),
        }
        for role, entry in roles.items()
        if isinstance(entry, dict) and str(entry.get("account") or "")
    }


def pending_identity_for(config: ProjectConfig, role: RoleConfig) -> dict[str, str]:
    """The account a role runs as now, or the one it is being prepared for."""
    owner = config.run_as_user or current_user_name()
    if role.run_as_user and role.run_as_user != owner:
        return {
            "account": role.run_as_user,
            "home": str(home_dir_for_user(role.run_as_user) or Path("/home") / role.run_as_user),
            "worktree": role.workdir,
        }
    return read_pending_identities(config).get(role.role, {})


def process_uid(pid: int, *, proc_root: Path | None = None) -> int | None:
    """The uid a running process is actually executing as, from the kernel."""
    if pid <= 0:
        return None
    return _proc_effective_uid(proc_root or PROC_ROOT, str(pid))


def privileged_upgrade_journal_path(config: ProjectConfig) -> Path:
    """Root's own phase record, in the directory only root can write.

    The tenant copy is published as untrusted output, so it cannot decide
    anything privileged: its owner could write `director: done` into it. Every
    gate reads this one, or reads the host and the board directly (SYRD-45).
    """
    return privileged_provision_dir(
        config.project, root=switchyard_privileged_provision_root()
    ) / "upgrade.json"


def resolved_source_selection(source_repo: Path | None) -> str:
    """The tree an operator pinned, named so it cannot be moved out from under them.

    `/opt/switchyard/current` is a symlink, and installing the next shared
    release moves it. Recording that name would pin nothing: between the
    accounts phase and the rerun it asks for, `current` can come to mean a
    different tree, and every phase after the move would regenerate artifacts
    from it while the record still called the selection pinned. What is recorded
    is the release the operator was actually looking at (SYRD-61).
    """
    if source_repo is None:
        return ""
    return str(source_repo.expanduser().resolve(strict=False))


@dataclass(frozen=True)
class DeclaredWorkflowPresence:
    """Where a declared workflow exists for this tenant, asked of each source.

    One boolean cannot answer this. "The tenant's local config has no workflow
    key" and "this tenant has no workflow" are different statements, and
    treating the first as the second is the regression: a legacy tenant, whose
    workflow was never projected locally because it predates declarative
    workflows entirely, was declared exempt from the Director phase without the
    board ever being asked (SYRD-240).

    So each source is recorded separately, and `unreadable` is kept apart from
    absent -- a config that cannot be parsed is not a tenant that needs
    nothing.
    """

    project: str
    #: The tenant's generated launcher config, which is what used to decide
    #: this on its own.
    config_declares: bool = False
    config_unreadable: str = ""
    #: Root's own recorded copy, the one `plan_workflow_from_root` prefers.
    root_records: bool = False
    #: The tenant's plan, which is where `propose_workflow_adoption` reads from.
    plan_declares: bool = False
    #: What the running board is actually enforcing, and why it could not say.
    board_document: bool = False
    board_problem: str = ""
    #: A tenant that keeps the workflow seeded by schema.sql and declares no
    #: per-project document. `pgu` is the one, and it is not a legacy tenant:
    #: it has nothing to adopt and nothing to migrate.
    non_declarative_by_design: bool = False

    @property
    def declared_somewhere(self) -> bool:
        """Whether any source says this tenant has a declared workflow."""
        return self.config_declares or self.root_records or self.plan_declares or self.board_document

    @property
    def board_runs_none(self) -> bool:
        """The board is reachable and carries no document.

        Not the same as "the board could not be read": an unreachable board is
        unknown, and reporting unknown as absent is how a transient failure
        would become a migration.
        """
        return not self.board_document and "no declared workflow" in self.board_problem

    @property
    def legacy_without_workflow(self) -> bool:
        """The condition this ticket exists for.

        The board a tenant is running is enforcing no declared workflow. That
        tenant's Director is on provisioning-scaffold onboarding by
        construction -- the pane hook's scaffold branch is gated on
        `TICKET_BOARD_ROLE_ONBOARDING_MIGRATED`, which exists only when a
        workflow document carries the migration marker -- and it is the state
        the upgrade used to close over.

        Deliberately NOT conditioned on some other source already declaring a
        workflow. A tenant provisioned before declarative workflows existed has
        one nowhere, which is precisely why it needs the migration; requiring a
        declaration first would exempt every tenant this ticket is about.

        The one exclusion is a tenant that declares none by design, which has
        no document to install and no scaffold to migrate away from.
        """
        return self.board_runs_none and not self.non_declarative_by_design


def declared_workflow_presence(
    config: ProjectConfig,
    *,
    config_path: Path,
    board_reader: Callable[[ProjectConfig], tuple[dict | None, str]] | None = None,
) -> DeclaredWorkflowPresence:
    """Ask every source that can hold a declared workflow, and keep the answers apart."""
    config_declares = False
    config_unreadable = ""
    try:
        config_declares = bool(_load_json(config_path).get("workflow"))
    except (SystemExit, OSError, ValueError) as exc:
        # Deliberately NOT "False". An unreadable config used to be
        # indistinguishable from an exempt tenant, which is the same wrong
        # answer for a completely different reason.
        config_unreadable = f"{config_path} could not be read ({exc})"

    recorded, _problem = recorded_declared_workflow(config.project)

    plan_declares = False
    non_declarative = False
    try:
        plan = _load_json(config_path.parent / "plan.json")
        plan_declares = bool(plan.get("workflow"))
        non_declarative = str(plan.get("workflow_seed") or "") == NON_DECLARATIVE_WORKFLOW_SEED
    except (SystemExit, OSError, ValueError):
        plan_declares = False

    reader = board_reader or read_board_declared_workflow
    document, board_problem = reader(config)
    return DeclaredWorkflowPresence(
        project=config.project,
        config_declares=config_declares,
        config_unreadable=config_unreadable,
        root_records=recorded is not None,
        plan_declares=plan_declares,
        board_document=document is not None,
        board_problem=board_problem,
        non_declarative_by_design=non_declarative,
    )


def _open_board_url(url: str) -> Any:
    import urllib.request

    return urllib.request.urlopen(url, timeout=5)


#: The entries under a board root the owner-run deploy has to be able to write,
#: and nothing else. `deploy-restart` makes its release in `releases/` (a temp
#: tree, then a rename), replaces `current` and `canary.env` by rename in the
#: board root, and rewrites `system-unit.sha256` in place. Existing release
#: trees are only read -- activation and rollback move a link -- so they are
#: left exactly as they are.
RELEASE_ROOT_WRITABLE_DIRS = ("releases",)
RELEASE_ROOT_WRITABLE_FILES = ("system-unit.sha256",)


def release_root_repair_record_path(project: str) -> Path:
    return privileged_provision_dir(
        project, root=switchyard_privileged_provision_root()
    ) / "release-root-repair.json"


def _open_release_root_entries(
    board_root: Path, allowed_uids: set[int], owner_user: str, project: str
) -> tuple[list[tuple[str, int, os.stat_result, int]], str]:
    """Open the board root and the entries a deploy writes, following nothing.

    Returns (entries, problem): each entry is (path, fd, stat, bits the owner
    needs), board root first, and the caller closes every fd. No entries and
    no problem means there is no board root yet.
    """
    relative = Path(str(board_root).lstrip("/")) / "_"
    root_fd, walk_problem = _walk_no_follow(Path(board_root.anchor or "/"), relative)
    if root_fd < 0:
        if walk_problem == "missing":
            # Nothing there yet: the owner's own first deploy makes it.
            return [], ""
        return [], f"{board_root} cannot be walked safely: {walk_problem}"
    root_info = os.fstat(root_fd)
    entries = [(str(board_root), root_fd, root_info, 0o700)]

    def refuse(problem: str) -> tuple[list[tuple[str, int, os.stat_result, int]], str]:
        for _path, fd, _info, _bits in entries:
            os.close(fd)
        return [], problem

    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid not in allowed_uids:
        return refuse(f"{board_root} is owned by uid {root_info.st_uid}, neither {owner_user} "
                      "nor root, so it cannot be attributed to this tenant")
    for name, directory in (
        *((n, True) for n in RELEASE_ROOT_WRITABLE_DIRS),
        *((n, False) for n in RELEASE_ROOT_WRITABLE_FILES),
    ):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (
            os.O_DIRECTORY if directory else os.O_NONBLOCK
        )
        try:
            fd = os.open(name, flags, dir_fd=root_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            kind = "a symbolic link" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else exc.strerror
            return refuse(f"{board_root / name} is {kind}; refusing to repair {project}'s release root")
        info = os.fstat(fd)
        entries.append((str(board_root / name), fd, info, 0o700 if directory else 0o600))
        if directory and not stat.S_ISDIR(info.st_mode):
            return refuse(f"{board_root / name} is not a directory")
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return refuse(f"{board_root / name} is not a regular file with one link")
        if info.st_uid not in allowed_uids:
            return refuse(f"{board_root / name} is owned by uid {info.st_uid}, neither "
                          f"{owner_user} nor root, so it cannot be attributed to this tenant")
    return entries, ""


def prepare_tenant_release_root(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Make sure the tenant owner can publish a release under its board root.

    Live on mefp: the upgrade reported the release `ready` and printed a deploy
    sequence; the operator stopped the listener and ran it; and the deploy,
    running as the owner, could not create its release -- `releases/` under the
    board root had been made by root, by an older release that deployed as root
    (SYRD-231). A fresh tenant has this right already; a legacy one is repaired
    here, before the sequence is printed, or refused.

    A repair is attributed only from what root controls: the owner from root's
    baseline and the kernel (`trusted_owner_identity`), the board root from
    root's baseline, and it must be exactly `<home>/<project>-ticketboard-live`.
    Without that, the same entries are only inspected -- a board root that needs
    nothing is ready, and one that needs a repair is refused. Every component is
    walked without following a link. The board root, `releases/` and
    `system-unit.sha256` may each be owned by the owner or -- as legacy state --
    by root, and nothing else: a symlink, another account's entry or a file with
    another link is refused. Only those entries are changed, never recursively:
    owner and owner-writable. The repair is recorded in root's own provision
    directory. A dry run describes it and writes nothing. Returns why not;
    empty means ready.
    """
    project = config.project
    identity = trusted_owner_identity(project)
    if identity.trusted:
        baseline, problem = read_plan_no_follow(
            privileged_baseline_plan_path(project), require_root_owned=True
        )
        if baseline is None:
            return [f"{project}'s board root cannot be established from root's baseline: {problem}"]
        board_root = Path(str(baseline.data.get("board_root") or ""))
        expected = Path(identity.owner_home) / f"{project}-ticketboard-live"
        if board_root != expected:
            return [f"root's baseline names {board_root or 'no board root'} for {project}, not "
                    f"{expected}; a release root elsewhere is not one this repair will touch"]
        owner_user, owner_uid, owner_gid = identity.owner_user, identity.owner_uid, identity.owner_gid
        unattributed = ""
    else:
        # Only looked at, never changed: which account the tenant says it runs
        # as decides nothing but where to look.
        owner_user = str(config.run_as_user or "").strip()
        owner_uid = uid_for_user(owner_user) if owner_user else None
        home = home_dir_for_user(owner_user) if owner_user else None
        if owner_uid is None or home is None:
            return []
        owner_gid = -1
        board_root = Path(home) / f"{project}-ticketboard-live"
        unattributed = "; ".join(identity.problems)
    entries, problem = _open_release_root_entries(board_root, {0, owner_uid}, owner_user, project)
    if problem:
        return [problem]
    try:
        changes = [
            (path, fd, info, bits) for path, fd, info, bits in entries
            if info.st_uid != owner_uid or (info.st_mode & bits) != bits
        ]
        if not changes:
            return []
        described = [
            f"{path} to {owner_user} (now uid {info.st_uid}, mode "
            f"{stat.S_IMODE(info.st_mode):o} -> {stat.S_IMODE(info.st_mode) | bits:o})"
            for path, _fd, info, bits in changes
        ]
        if unattributed:
            if dry_run:
                try:
                    privileged_baseline_plan_path(project).lstat()
                except FileNotFoundError:
                    # The upgrade this dry run describes stages that baseline
                    # before it gets here; refusing would predict a stop the
                    # real run does not make.
                    for line in described:
                        print_func(f"switchyard: would give {line} once root's baseline is staged")
                    return []
                except OSError:
                    pass
            return [f"{project}'s owner must be given {', '.join(path for path, *_ in changes)} "
                    f"before it can publish a release, and root will not attribute them: "
                    f"{unattributed}"]
        if dry_run:
            for line in described:
                print_func(f"switchyard: would give {line} so the owner can publish a release")
            return []
        if os.geteuid() != 0:
            return [f"{board_root} holds entries {owner_user} cannot write; repairing them is "
                    f"root's -- run `sudo switchyard upgrade {project}`"]
        record = {
            "schema": "switchyard.release-root-repair.v1",
            "project": project,
            "owner": owner_user,
            "at": datetime.now(timezone.utc).isoformat(),
            "entries": [
                {"path": path, "uid_before": info.st_uid,
                 "mode_before": f"{stat.S_IMODE(info.st_mode):o}",
                 "mode_after": f"{stat.S_IMODE(info.st_mode) | bits:o}"}
                for path, _fd, info, bits in changes
            ],
        }
        record_path = release_root_repair_record_path(project)
        try:
            # Recorded before anything changes, so a repair that stops halfway
            # still says what it was doing.
            ensure_privileged_provision_dir(record_path.parent)
            _write_private_json_atomic(record_path, record)
            for path, fd, info, bits in changes:
                os.fchown(fd, owner_uid, owner_gid)
                os.fchmod(fd, stat.S_IMODE(info.st_mode) | bits)
                print_func(f"switchyard: gave {path} to {owner_user} so it can publish a release")
        except OSError as exc:
            return [f"could not repair {project}'s release root: {exc}"]
        print_func(f"switchyard: recorded that repair in {record_path}")
        return []
    finally:
        for _path, fd, _info, _bits in entries:
            os.close(fd)


def owner_release_root_problems(config: ProjectConfig) -> list[str]:
    """The same question, asked by a process that cannot repair: can the owner write it?

    Read-only, so it may use the tenant's own view of where its home is: it
    changes nothing and only decides whether a deploy sequence is printed. Asked
    only when this process IS the owner, which is when access() answers for the
    account that will run the deploy.
    """
    owner = str(config.run_as_user or "").strip()
    if not owner or owner != current_user_name():
        return []
    home = home_dir_for_user(owner)
    if home is None:
        return []
    board_root = Path(home) / f"{config.project}-ticketboard-live"
    unwritable = [
        str(path) for path in (board_root, board_root / "releases",
                               *(board_root / name for name in RELEASE_ROOT_WRITABLE_FILES))
        if path.exists() and not os.access(path, os.W_OK)
    ]
    if not unwritable:
        return []
    return [f"{owner} cannot write {', '.join(unwritable)}, so its deploy could not publish a "
            f"release; `sudo switchyard upgrade {config.project}` repairs a legacy release root"]


# --------------------------------------------------------------------------
# The release phase: proved from the host, never from a claim (SYRD-117)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseAlignment:
    """The four facts an operator has to compare, read one at a time.

    Separate fields rather than a verdict, because the interesting states are
    the disagreements: a board serving a build its own `current` link does not
    name, a trusted journal saying `ready` over a deployment that happened, a
    tenant copy claiming a completion nothing else has. One boolean would hide
    every one of them (SYRD-117).
    """

    project: str
    #: The shared Switchyard release this host has installed.
    shared_release: str = ""
    #: The commit the tenant's board `current` link resolves to.
    deployed_release: str = ""
    #: What the running board reports as its own build.
    live_build: str = ""
    #: The commit this upgrade was pinned to deploy, when one was recorded.
    pinned_release: str = ""
    #: Root's journal, and whether it could be read at all from here.
    trusted_release_state: str = ""
    trusted_readable: bool = True
    #: The tenant's readable copy, and any unprivileged note beside it.
    tenant_release_state: str = ""
    tenant_observation: str = ""
    #: Whether the running board is enforcing a declared workflow, and -- when
    #: it is not -- whether that is this tenant's legacy state rather than its
    #: design. A release phase closed over a board running no workflow is how a
    #: tenant ends up upgraded, "done", and still serving its Director the
    #: provisioning scaffold (SYRD-240).
    board_runs_declared_workflow: bool = True
    legacy_without_workflow: bool = False
    #: Why a reading is missing, per fact.
    errors: tuple[str, ...] = ()

    @property
    def board_is_serving_its_release(self) -> bool:
        """The check the deploy itself makes: the live build IS the deployed tree."""
        return bool(self.deployed_release) and self.live_build == self.deployed_release

    @property
    def deployed_matches_pin(self) -> bool:
        """Whether what is deployed is what this upgrade was asked to deploy."""
        return not self.pinned_release or self.deployed_release == self.pinned_release

    @property
    def diverged(self) -> bool:
        """Whether the records disagree with each other or with the machine."""
        if self.tenant_release_state and self.trusted_readable:
            if self.tenant_release_state != self.trusted_release_state:
                return True
        if not self.trusted_readable:
            return False
        proved = self.board_is_serving_its_release and self.deployed_matches_pin
        if self.trusted_release_state == "done":
            return not proved
        return proved

    def close_refusals(self) -> list[str]:
        """Why the release phase must not be recorded done, in order.

        Read from the host every time. The deploy's exit status is deliberately
        not on this list: a phase closed because a previous command returned 0
        is a phase closed on a claim, and a claim is what this ticket is about.
        Re-reading costs one HTTP request and one readlink.
        """
        refusals: list[str] = []
        if not self.deployed_release:
            refusals.append(
                f"{self.project}'s board root names no deployed release, so there is no "
                "deployment to close the phase over"
            )
        elif not self.live_build:
            detail = f" ({'; '.join(self.errors)})" if self.errors else ""
            refusals.append(f"the running board did not report a build id{detail}")
        elif not self.board_is_serving_its_release:
            refusals.append(
                f"the running board reports build {self.live_build}, but {self.project}'s "
                f"deployed release is {self.deployed_release}; the board is not serving what "
                "was deployed"
            )
        if self.deployed_release and not self.deployed_matches_pin:
            refusals.append(
                f"the deployed release is {self.deployed_release}, but this upgrade was pinned "
                f"to {self.pinned_release}; closing the phase would claim a deploy that did not "
                "happen"
            )
        if self.legacy_without_workflow:
            # The release can be perfectly deployed and this still be wrong.
            # Every check above compares bytes on disk with what the board
            # reports running; none of them asks whether the board is
            # enforcing a workflow at all, so a legacy tenant closed cleanly
            # while its Director kept provisioning-scaffold onboarding and
            # `/api/workflow` kept answering null (SYRD-240).
            refusals.append(
                f"{self.project}'s board is running no declared workflow, so its director is "
                "still on provisioning-scaffold onboarding and /api/workflow answers null. "
                f"Closing the release phase would record a migration that has not happened. "
                f"Run `switchyard migrate-workflow {self.project}` to see what would be "
                "installed, and `pkexec switchyard migrate-workflow "
                f"{self.project} --apply` to install it"
            )
        return refusals


def _live_board_build_id(
    config: ProjectConfig, *, opener: Callable[[str], Any] | None = None
) -> tuple[str, str]:
    """What the running board says it is, or why it could not be asked."""
    board_root = (
        str(config.board_url or "").rstrip("/").removesuffix("/api/tickets").removesuffix("/api")
    )
    if not board_root:
        return "", f"{config.project} declares no board url"
    open_url = opener or _open_board_url
    try:
        with open_url(board_root + "/api/board") as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "not proven"
        return "", f"the board at {board_root} could not be read ({exc})"
    build = str(payload.get("build_id") or "").strip() if isinstance(payload, Mapping) else ""
    return build, "" if build else f"the board at {board_root} reported no build id"


def release_alignment(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
) -> ReleaseAlignment:
    """Compare the shared release, the deployed board, the live build and both journals.

    Reads only. Every value comes from the host or the running board rather
    than from a record somebody wrote about them, which is the point: the
    defect this answers is a journal that disagreed with the machine and won
    (SYRD-117).
    """
    errors: list[str] = []
    shared = ""
    marker = _read_switchyard_release_marker(
        (switchyard_shared_install_root() / "current").resolve(strict=False)
    )
    if marker is not None:
        shared = marker.marker_commit
        if marker.marker_error:
            errors.append(marker.marker_error)

    deployed = ""
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        errors.append(f"{config.project} serves no tenant board release")
    else:
        _release, deployed = _current_tenant_release(board_root)

    live, live_error = _live_board_build_id(config, opener=opener)
    if live_error:
        errors.append(live_error)

    # `read_upgrade_source` already refuses anything that is not root's own
    # record and answers `{}` instead, so there is nothing to guard here.
    pinned = str(read_upgrade_source(config).get("deploy_ref") or "").strip()
    # Only a resolved commit can be compared with a deployed one. A branch name
    # says which branch, not which commit, so it is not a pin for this purpose
    # and treating it as one would fail every close.
    if not re.fullmatch(r"[0-9a-f]{40}", pinned):
        pinned = ""

    trusted_readable = os.geteuid() == 0 or os.access(
        privileged_upgrade_journal_path(config), os.R_OK
    )
    trusted_state = ""
    if trusted_readable:
        trusted_state = upgrade_phase_state(
            read_upgrade_journal(config, config_path=config_path, trusted=True), "release"
        )
    else:
        errors.append(
            "root's journal is not readable from this account; run this as an operator to "
            "compare it"
        )
    tenant_journal = read_upgrade_journal(config, config_path=config_path)
    observation = upgrade_phase_observation(tenant_journal, "release")
    # Read from the running board like every other fact here, rather than from
    # anything written about it.
    presence = declared_workflow_presence(config, config_path=config_path)
    if presence.board_problem and not presence.board_document:
        errors.append(presence.board_problem)
    return ReleaseAlignment(
        project=config.project,
        shared_release=shared,
        deployed_release=deployed,
        live_build=live,
        pinned_release=pinned,
        trusted_release_state=trusted_state,
        trusted_readable=trusted_readable,
        tenant_release_state=upgrade_phase_state(tenant_journal, "release"),
        tenant_observation=str(observation.get("state") or ""),
        board_runs_declared_workflow=presence.board_document,
        legacy_without_workflow=presence.legacy_without_workflow,
        errors=tuple(errors),
    )


def repair_repository_policy_hooks(
    config_path: Path,
    *,
    source_repo: Path,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> tuple[Path, ...]:
    """Reinstall this project's managed Git policy hooks, missing or stale.

    Idempotent: the installer rewrites its own managed hook and preserves any
    pre-existing one, so repeated upgrades converge rather than accumulate. Ownership is
    taken from the existing hooks directory, so a root-run upgrade leaves the tenant's
    hooks owned by the tenant. Only the repositories named in this project's config are
    touched -- no global hooksPath, no scanning of arbitrary homes.
    """
    if dry_run:
        print_func(f"switchyard: would reinstall managed Git policy hooks for {config_path}")
        return ()
    from scripts import repository_hooks

    try:
        installed = repository_hooks.install_project_config(config_path, source_root=source_repo)
    except Exception as exc:
        # Warning-only policy: a repair failure must not fail an otherwise good upgrade.
        print_func(f"warning: switchyard: could not repair Git policy hooks: {exc}")
        return ()
    for path in installed:
        print_func(f"switchyard: reinstalled managed Git policy hook {path}")
    return installed


STATE_TREE_MAX_DEPTH = 64


def _open_tenant_state_root(root: Path) -> tuple[int, str]:
    """Open `root` as a directory, following nothing the tenant could have set.

    Every component is opened with O_NOFOLLOW from its parent's descriptor. A
    symlink is allowed only where root itself put it -- the link and the
    directory holding it are root's, and nobody else can write that directory
    -- because a chown walk that follows a link the tenant can create or swap
    reaches whatever the tenant aims it at, however carefully the final chown
    is written. `follow_symlinks=False` protects the last component and nothing
    above it (SYRD-233 DAT).

    Returns (fd, "") for a directory, (-1, "") when it is simply not there, and
    (-1, reason) when it is refused. The caller owns the descriptor.
    """
    try:
        fd = os.open(root.anchor or "/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        return -1, f"{root.anchor or '/'} cannot be opened ({exc.strerror})"
    walked = Path(root.anchor or "/")
    try:
        for component in root.relative_to(walked).parts:
            walked = walked / component
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                return -1, ""
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
                    return -1, f"{walked} cannot be opened ({exc.strerror})"
                allowed = _root_placed_link(component, dir_fd=fd)
                if allowed:
                    return -1, allowed
                try:
                    child = os.open(
                        component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd
                    )
                except OSError as followed:
                    return -1, f"{walked} cannot be opened ({followed.strerror})"
            os.close(fd)
            fd = child
    except OSError as exc:  # pragma: no cover - defensive
        _close_quietly(fd)
        return -1, f"{walked} cannot be opened ({exc.strerror})"
    except BaseException:
        _close_quietly(fd)
        raise
    return fd, ""


def _root_placed_link(component: str, *, dir_fd: int) -> str:
    """"" when this symlink is root's own layout, else why it is refused."""
    try:
        info = os.lstat(component, dir_fd=dir_fd)
    except OSError as exc:
        return f"{component} cannot be inspected ({exc.strerror})"
    if not stat.S_ISLNK(info.st_mode):
        return f"{component} is not a directory"
    holder = os.fstat(dir_fd)
    if info.st_uid != 0 or holder.st_uid != 0 or holder.st_mode & 0o022:
        return (
            f"{component} is a symlink that root did not place (it belongs to uid "
            f"{info.st_uid}, in a directory owned by uid {holder.st_uid}), and a root-run "
            "chown does not follow one"
        )
    return ""


def _close_quietly(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:  # pragma: no cover - defensive
        pass


def _walk_tenant_state_tree(
    root: Path, act: Callable[[Path, os.stat_result, Callable[[int, int], None]], str]
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Visit `root` and everything beneath it without following any symlink.

    `act` is called for each path with its `lstat` and a `chown(uid, gid)` that
    acts through the descriptor the path was just read through, so nothing can
    be swapped between deciding about a path and changing it; it returns a
    reason to report, or "". A symlink is visited as the link itself and never
    descended into, so it cannot become a traversal root.

    Returns (findings, refusals). A refusal is a root that could not be walked
    safely, which is a reason to stop rather than a path to fix.
    """
    findings: list[tuple[Path, str]] = []
    refusals: list[tuple[Path, str]] = []
    fd, problem = _open_tenant_state_root(root)
    if problem:
        return findings, [(root, problem)]
    if fd < 0:
        return findings, refusals

    def visit(display: Path, directory: int, depth: int) -> None:
        if depth > STATE_TREE_MAX_DEPTH:
            refusals.append((display, f"is nested deeper than {STATE_TREE_MAX_DEPTH} directories"))
            return
        try:
            names = sorted(entry.name for entry in os.scandir(directory))
        except OSError as exc:
            findings.append((display, f"cannot be listed ({exc.strerror})"))
            return
        for name in names:
            child = display / name
            try:
                info = os.lstat(name, dir_fd=directory)
            except OSError as exc:
                findings.append((child, f"cannot be inspected ({exc.strerror})"))
                continue

            def chown(uid: int, gid: int, _name: str = name, _fd: int = directory) -> None:
                os.chown(_name, uid, gid, dir_fd=_fd, follow_symlinks=False)

            reason = act(child, info, chown)
            if reason:
                findings.append((child, reason))
            if not stat.S_ISDIR(info.st_mode):
                continue
            try:
                below = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=directory,
                )
            except OSError as exc:
                findings.append((child, f"cannot be opened ({exc.strerror})"))
                continue
            try:
                visit(child, below, depth + 1)
            finally:
                os.close(below)

    try:
        info = os.fstat(fd)

        def chown_root(uid: int, gid: int, _fd: int = fd) -> None:
            os.fchown(_fd, uid, gid)

        reason = act(root, info, chown_root)
        if reason:
            findings.append((root, reason))
        visit(root, fd, 1)
    finally:
        os.close(fd)
    return findings, refusals


def role_state_roots(config: ProjectConfig) -> list[Path]:
    """The session store and each role's store, deduplicated, in walk order."""
    roots = [config.session_dir.expanduser()]
    for role in config.roles:
        directory = role_session_dir(config, role).expanduser()
        if directory not in roots:
            roots.append(directory)
    return roots


def role_state_ownership_problems(
    config: ProjectConfig,
) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """(wrong owner, refused) for this tenant's role state store.

    Read-only, so an upgrade can report them before it repairs them, and so a
    launch can say why it is leaving a role alone. The store is the tenant's
    own: every directory and file under it belongs to the project account, and
    anything that does not is something a privileged run left behind
    (SYRD-233).

    A refusal is not a path to chown. It is a store root that is a symlink, or
    is not a directory, or cannot be walked without following one, and it stops
    the repair rather than redirecting it (SYRD-233 DAT).
    """
    owner = config.run_as_user or current_user_name()
    try:
        wanted = pwd.getpwnam(owner).pw_uid
    except KeyError:
        return [], []
    found: list[tuple[Path, str]] = []
    refused: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def inspect(path: Path, info: os.stat_result, _chown: Callable[[int, int], None]) -> str:
        if path in seen:
            return ""
        seen.add(path)
        if info.st_uid != wanted:
            return f"is owned by {_uid_owner_name(info.st_uid) or info.st_uid}, not {owner}"
        return ""

    for root in role_state_roots(config):
        findings, refusals = _walk_tenant_state_tree(root, inspect)
        found.extend(findings)
        refused.extend(refusals)
    return found, refused


def _uid_owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return ""


def repair_role_state_ownership(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Give the tenant account its own role state back. Root's to do.

    `ensure_owner_state_dirs` assigns the store's two top-level directories and
    is not recursive, and repatriation -- which does walk the per-role ones --
    returns immediately for a tenant already on isolated state with no legacy
    account bindings. So a role directory created by a root-run launch stays
    root's, and the project account cannot record that role's provider state
    ever again (SYRD-233 live UAT).
    """
    owner = config.run_as_user or current_user_name()
    problems, refusals = role_state_ownership_problems(config)
    if refusals:
        # Before anything is changed, and whoever is asking: a store root that
        # is a symlink is not a store this may walk, and repairing "what it
        # points at" is how a root-run chown ends up outside the tenant's tree.
        path, reason = refusals[0]
        print_func(
            f"switchyard: refusing to touch {config.project}'s role state: {path} {reason}. "
            f"No ownership was changed. Make it a real directory under {owner}'s state store, "
            f"then run `sudo switchyard upgrade {config.project}` again."
        )
        return False
    if not problems:
        return True
    if os.geteuid() != 0:
        print_func(
            f"switchyard: {config.project}'s role state is not all {owner}'s "
            f"({len(problems)} path(s), first: {problems[0][0]} {problems[0][1]}), and only root "
            f"can give it back. Run `sudo switchyard upgrade {config.project}`."
        )
        return False
    if dry_run:
        print_func(
            f"switchyard: would give {owner} back {len(problems)} path(s) of {config.project}'s "
            f"role state, starting with {problems[0][0]} ({problems[0][1]}); nothing written"
        )
        return True
    try:
        ids = pwd.getpwnam(owner)
    except KeyError:
        print_func(f"switchyard: {owner} is not a local account; {config.project}'s role state was left alone")
        return False
    failures: list[str] = []

    def give_back(path: Path, info: os.stat_result, chown: Callable[[int, int], None]) -> str:
        if info.st_uid == ids.pw_uid:
            return ""
        try:
            # Through the descriptor this path was just read through, and never
            # through what a symlink points at: the link itself is the tenant's,
            # its target may be anywhere at all.
            chown(ids.pw_uid, ids.pw_gid)
        except OSError as exc:
            failures.append(f"switchyard: could not give {path} back to {owner}: {exc.strerror}")
        return ""

    for root in role_state_roots(config):
        _findings, denied = _walk_tenant_state_tree(root, give_back)
        if denied:
            failures.append(f"switchyard: could not walk {denied[0][0]}: {denied[0][1]}")
        if failures:
            print_func(failures[0])
            return False
    remaining, still_refused = role_state_ownership_problems(config)
    if still_refused:
        print_func(
            f"switchyard: {config.project}'s role state could not be re-read after the repair: "
            f"{still_refused[0][0]} {still_refused[0][1]}"
        )
        return False
    if remaining:
        print_func(
            f"switchyard: {config.project}'s role state still has {len(remaining)} path(s) that "
            f"are not {owner}'s, starting with {remaining[0][0]}"
        )
        return False
    print_func(
        f"switchyard: gave {owner} back {len(problems)} path(s) of {config.project}'s role state, "
        "so its roles can record what their runtimes were started against"
    )
    return True


def restore_interrupted_role_state(
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    owner_home: Path | None = None,
) -> bool:
    """Give the role state back AND finish what the broken store interrupted.

    Ownership alone is half a repair. A role left running because its store was
    unusable still has no record, `roles_with_stale_provider_runtime` calls a
    missing record stale, and the ordinary launch that follows the upgrade ends
    the very panes this was protecting -- the restart postponed by one command
    rather than avoided (SYRD-233 post-DAT).
    """
    if owner_home is None:
        owner_home = home_dir_for_user(config.run_as_user or current_user_name()) or Path.home()
    captured = _interrupted_provider_state_roles(config, runner=runner, owner_home=owner_home)
    if dry_run and captured:
        for role, pane_pid, _generation in captured:
            print_func(
                f"switchyard: would finish the provider-state record {role.role} could not write, "
                f"so its live {_role_cli_name(role)} (pid {pane_pid}) would not be restarted; "
                "nothing written"
            )
    if not repair_role_state_ownership(config, dry_run=dry_run, print_func=print_func):
        return False
    if dry_run:
        return True
    return _finish_interrupted_provider_state(
        config, captured, runner=runner, owner_home=owner_home, print_func=print_func
    )


def upgrade_project_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    desktop_policy: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    # `None` means the caller said nothing about the ref, which is not the same
    # as asking for the default one (SYRD-61).
    deploy_ref: str | None = None,
    # Where this tenant's root-owned role tooling is staged. Only a test names
    # it; on a host it is /usr/local/lib/switchyard (SYRD-62).
    tooling_root: Path | None = None,
    # Stated once by an operator and then recorded root-owned. It is not read
    # from the tenant, because every role runs as the account that owns the
    # tenant's git config and could aim the push somewhere else (SYRD-97 review).
    publish_remote: str = "",
    # Stated once by an operator when a tenant gains an upstream board or that
    # board moves; recorded in the tenant's configuration afterwards, so the
    # next upgrade needs no flag and the panes need none ever (SYRD-238).
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Run the privileged phases of a tenant upgrade, in order, and stop there.

    The phases are ordered because their failure modes are: writing per-role
    accounts into the configuration before those accounts exist leaves roles
    that cannot start, and installing the matching board authority table would
    then stop recognising the panes that are actually running. Each phase is
    journaled so an interrupted upgrade resumes, and the phases root does not
    own -- creating the accounts, and the director's own board write -- are
    reported rather than attempted (SYRD-45).
    """
    source_pinned = _pin_upgrade_source(
        config,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        desktop_policy=desktop_policy,
        dry_run=dry_run,
        print_func=print_func,
        publish_remote=publish_remote,
        runner=runner,
        source_repo=source_repo,
        tooling_root=tooling_root,
    )
    if not isinstance(source_pinned, UpgradeSourcePinned):
        return source_pinned
    desktop_choice = source_pinned.desktop_choice
    deploy_ref_chosen = source_pinned.deploy_ref_chosen
    source_repo = source_pinned.source_repo
    commit_git_dir = source_pinned.commit_git_dir
    deploy_ref = source_pinned.deploy_ref
    state_ready = _recover_upgrade_state(
        config,
        config_path=config_path,
        deploy_ref=deploy_ref,
        desktop_policy=desktop_policy,
        dry_run=dry_run,
        print_func=print_func,
        runner=runner,
        source_repo=source_repo,
    )
    if not isinstance(state_ready, UpgradeStateReady):
        return state_ready
    source_repo = state_ready.source_repo
    effective_source_repo = state_ready.effective_source_repo
    config = state_ready.config
    cutover = state_ready.cutover

    release_report_config = config
    trusted_release_root: Path | None = None
    publication_detail = ""
    config = _refresh_upgrade_artifacts(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        registry_dir=registry_dir,
        runner=runner,
        source_repo=source_repo,
        upstream_report_token_file=upstream_report_token_file,
        upstream_report_url=upstream_report_url,
    )
    tooling_staged = _stage_upgrade_tooling(
        config,
        config_path=config_path,
        deploy_ref=deploy_ref,
        deploy_ref_chosen=deploy_ref_chosen,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        publication_detail=publication_detail,
        publish_remote=publish_remote,
        runner=runner,
        tooling_root=tooling_root,
        trusted_release_root=trusted_release_root,
    )
    if not isinstance(tooling_staged, UpgradeToolingStaged):
        return tooling_staged
    trusted_release_root = tooling_staged.trusted_release_root
    publication_detail = tooling_staged.publication_detail
    identities_done = _upgrade_identities_and_accounts(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        cutover=cutover,
        deploy_ref=deploy_ref,
        desktop_choice=desktop_choice,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        publication_detail=publication_detail,
        publish_remote=publish_remote,
        release_report_config=release_report_config,
        runner=runner,
        source_repo=source_repo,
        tooling_root=tooling_root,
        trusted_release_root=trusted_release_root,
    )
    if not isinstance(identities_done, UpgradeIdentitiesDone):
        return identities_done
    config = identities_done.config
    release_report_config = identities_done.release_report_config

    return _finish_upgrade(
        config,
        commit_git_dir=commit_git_dir,
        config_path=config_path,
        deploy_ref=deploy_ref,
        desktop_choice=desktop_choice,
        dry_run=dry_run,
        effective_source_repo=effective_source_repo,
        print_func=print_func,
        release_report_config=release_report_config,
        runner=runner,
    )


def _role_accounts_ready(config: ProjectConfig) -> bool:
    """Compatibility predicate: SYRD-69 requires only the project account."""
    return True


# Where systemd reads units from. A module attribute so the transaction can be
# exercised against a real directory in a test rather than against a stub of the
# install itself (SYRD-45).
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")


def _installed_unit_path(unit: str) -> Path:
    return SYSTEMD_UNIT_DIR / unit


def _tenant_owner_home(config: ProjectConfig, config_path: Path | None) -> Path:
    """The owner home this tenant actually records, not merely the passwd one."""
    owner = config.run_as_user or current_user_name()
    if config_path is not None:
        recorded = str(_plan_data_from_config(config, config_path).get("owner_home") or "").strip()
        if recorded:
            return Path(recorded)
    return home_dir_for_user(owner) or Path("/home") / owner


def _owner_user_systemctl(
    config: ProjectConfig, action: str, unit: str, *, config_path: Path | None = None
) -> list[str]:
    """Drive the owner's user manager the way the rest of the launcher does."""
    owner = config.run_as_user or current_user_name()
    home = _tenant_owner_home(config, config_path)
    # Some questions are asked of the manager itself rather than of a unit.
    operation = f"systemctl --user {action}"
    if unit:
        operation = f"{operation} {shlex.quote(unit)}"
    if action in {"start", "restart"}:
        operation = f"systemctl --user daemon-reload && {operation}"
    # Exported rather than prefixed. A prefix binds to one command, and every
    # action that needs a reload is two: `... systemctl --user daemon-reload &&
    # systemctl --user restart <unit>` ran the restart with no XDG_RUNTIME_DIR
    # and no bus address at all, so it could not reach the manager it had just
    # reloaded and exited 1. That is the "could not start the notify listener"
    # a rollback reported while the same unit started immediately by hand with
    # the owner's runtime directory set (SYRD-61).
    script = (
        'runtime="/run/user/$(id -u)"; '
        'export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
        + operation
    )
    return _owner_command_env_args(owner, home, ["sh", "-c", script])


def capture_installed_units(
    config: ProjectConfig, *, config_path: Path | None = None
) -> dict[str, bytes | None]:
    """What is installed now, so the transaction can put it back exactly.

    Two managers, two paths: the board and its canary are system units and the
    listener is the owner's user unit (SYRD-45). Read from the same list the
    transaction installs from, so anything it puts in is something the rollback
    can take back out -- the canary was installed by nothing here and restored
    by nothing here, which is only safe while nothing installs it (SYRD-63).
    """
    captured: dict[str, bytes | None] = {}
    for unit, path, _ownership in authority_unit_installs(config, config_path=config_path):
        try:
            captured[unit] = path.read_bytes()
        except OSError:
            captured[unit] = None
    return captured


def capture_release_pointer(config: ProjectConfig, *, config_path: Path | None = None) -> tuple[Path | None, str]:
    """Where the board release symlink points now, so it can be put back."""
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        return None, ""
    current = board_root / "current"
    try:
        return current, os.readlink(current)
    except OSError:
        return current, ""


def deploy_release_in_transaction(
    config: ProjectConfig,
    *,
    config_path: Path,
    source_repo: Path,
    commit_git_dir: str | None,
    deploy_ref: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> tuple[list[str], bool]:
    """Switch the board to the release that enforces the per-role table.

    The binary is part of the same change as the units, the workers and the
    authority table: an old board does not group-own its runtime directory for
    the roles group, so restarting it against strict units can leave the role
    accounts unable to reach the socket at all. Switching it here means the
    rollback can put it back (SYRD-45).

    Returns its problems and whether it restarted the board. `deploy-restart`
    does restart it, having smoke-checked the service, verified the live build
    id and checked the post-deploy runtime; a deploy with nothing to do restarts
    nothing, and the caller still has to make the installed authority the one
    being served (SYRD-63).
    """
    status = tenant_release_status(
        config,
        config_path=config_path,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
    )
    if status is None:
        # Not every project serves its board from a tenant release; there is
        # nothing to switch, and nothing to roll back either.
        return [], False
    if not status.target_sha:
        return [f"the release to deploy could not be resolved: {status.resolve_error}"], False
    if status.unchanged:
        return [], False
    command = tenant_release_deploy_command(status, config.project)
    if not command:
        return ["no deploy command could be built for this release"], False
    result = runner(["sh", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return [
            f"deploying {status.target_sha} failed (exit {result.returncode}): "
            f"{(str(result.stderr).strip() or 'no output')[:400]}"
        ], False
    print_func(f"switchyard: {config.project} board release deployed: {status.target_sha}")
    return [], True


def restore_release_pointer(
    pointer: Path | None,
    target: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    """Put the release symlink back where the transaction found it."""
    if pointer is None or not target:
        return []
    try:
        if os.path.islink(pointer) and os.readlink(pointer) == target:
            return []
        staged = pointer.with_name(f".{pointer.name}.rollback")
        if staged.exists() or os.path.islink(staged):
            staged.unlink()
        os.symlink(target, staged)
        os.replace(staged, pointer)
    except OSError as exc:
        return [f"could not restore the release pointer {pointer}: {exc}"]
    return []


def install_board_authority_files(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install the staged units and reload systemd. Nothing is restarted here.

    This has to happen before the release deploy, not after it. `deploy-restart`
    compares the release's own production unit with the one installed and
    refuses when they differ, because a daemon-reload is deliberately outside
    the board's deploy grant -- and the whole point of this transaction is that
    the new unit differs: it carries `SupplementaryGroups`, the strict runtime
    directory mode and the per-role identity table. With the old unit still
    installed, the migration this transaction *is* is indistinguishable from
    operator drift, and the deploy correctly refuses (SYRD-63).

    Installing the file and reloading changes nothing about what is running, so
    the invariant behind the old ordering still holds: the old board is never
    restarted under a unit its release cannot serve. The restart happens with
    the new binary, inside the deploy or in `activate_board_authority` after it.

    The same three units the printed operator sequence installs, including the
    canary -- the deploy starts that one through systemd, so a transaction that
    left it uninstalled or stale was relying on provisioning having got there
    first (SYRD-63).
    """
    problems: list[str] = []
    staged_dir = privileged_provision_dir(config.project, root=switchyard_privileged_provision_root())
    owner = config.run_as_user or current_user_name()
    for unit, destination, ownership in authority_unit_installs(
        config, config_path=config_path
    ):
        # The staged source is the generated unit; a destination that is a copy
        # of it says so in its name rather than being a second, different file.
        staged = staged_dir / unit.split(" (", 1)[0]
        if not staged.is_file():
            problems.append(f"{unit} has not been generated under {staged_dir}")
            continue
        result = runner(
            ["install", "-D", "-m", "0644", *ownership, str(staged), str(destination)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            problems.append(f"could not install {unit} (exit {result.returncode})")
    if problems:
        return problems
    if runner(["systemctl", "daemon-reload"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode != 0:
        return ["systemctl daemon-reload failed"]
    print_func(
        f"switchyard: installed {config.project}'s generated units and reloaded systemd; "
        f"{owner}'s listener stays stopped until the roles have verified"
    )
    return []


def install_board_authority(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Install the staged units, reload, restart and health-check the board.

    The whole operation, for callers with no release deploy between the two
    halves. The identity transaction has one, and uses the halves (SYRD-63).
    """
    problems = install_board_authority_files(
        config, runner=runner, config_path=config_path, print_func=print_func
    )
    if problems:
        return problems
    # The listener stays stopped through the release and schema migration; it is
    # started again only once the workers, their writes and the presentation
    # have all verified (SYRD-45).
    return activate_board_authority(config, runner=runner, restart=True, print_func=print_func)


def _finish_upgrade_preview(
    config: ProjectConfig,
    *,
    config_path: Path,
    director: str,
    source_repo: Path | None,
    commit_git_dir: str | None,
    deploy_ref: str | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> int:
    """finish-upgrade's steps in apply's order: every read made, every write described.

    Its verdict is apply's verdict. On MEFP the dry run stopped at its first
    line and exited 0, and apply then made the one-way workflow write before
    failing on a release it could not resolve -- a read-only check the dry run
    never reached (SYRD-254). The release step here is the same function apply
    runs, so the two cannot disagree about it.
    """
    stops: list[str] = []

    def verdict() -> int:
        if stops:
            print_func(
                f"switchyard: dry run: apply would not complete for {config.project}: "
                + "; ".join(stops)
                + ". Nothing was written."
            )
            return 1
        print_func(
            f"switchyard: dry run: every check apply makes for {config.project} passes. "
            "Nothing was written."
        )
        return 0

    installed = install_handed_off_workflow(
        config, config_path=config_path, caller_role=director, dry_run=True, print_func=print_func
    )
    if installed is False:
        stops.append("the handed-off workflow would be refused")
    print_func(
        f"switchyard: would migrate {config.project}'s declarative director onboarding as "
        f"{current_user_name()}, then record the director phase from what the board serves"
    )
    cutover = role_account_cutover(config, runner=runner)
    if not cutover.is_complete:
        print_func(
            f"switchyard: apply would stop after the director phase: {config.project}'s "
            "per-role accounts are not in place yet, so no release step follows"
        )
        return verdict()
    root_problems = owner_release_root_problems(config)
    if root_problems:
        for problem in root_problems:
            print_func(f"switchyard: {problem}")
        stops.append("the owner's release root needs repair first")
        return verdict()
    print_func(f"switchyard: the release step apply would report for {config.project}:")
    release_status = report_tenant_release_upgrade(
        config,
        config_path=config_path,
        source_repo=(source_repo or _repo_root()).expanduser().resolve(strict=False),
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
        print_func=lambda line: print_func(f"  {line}"),
    )
    blocked = release_update_blocked(release_status)
    if blocked:
        stops.append(f"its release phase would not complete: {blocked}")
    return verdict()


def director_release_divergence_report(
    config: ProjectConfig,
    *,
    config_path: Path,
    opener: Callable[[str], Any] | None = None,
) -> list[str]:
    """What the director should say about the release phase, and never claim.

    The director can read the board and the tenant copy; it cannot read or write
    root's journal. So it reports what it can see, says plainly that it did not
    close anything, and names the one supported action that does (SYRD-117).
    """
    alignment = release_alignment(config, config_path=config_path, opener=opener)
    lines = [
        f"switchyard: finish-upgrade does not close {config.project}'s release phase. That phase "
        "is root's record and this command is unprivileged by design, so what it wrote is an "
        "observation beside the phases, not a phase.",
    ]
    if alignment.trusted_readable and alignment.trusted_release_state == "done":
        return lines + [
            f"switchyard: root's journal already records the release phase done for "
            f"{config.project}; nothing is outstanding."
        ]
    if alignment.close_refusals():
        return lines + [
            f"switchyard: {config.project}'s board is not serving a deployment that could close "
            "the phase yet:",
            *(f"  - {refusal}" for refusal in alignment.close_refusals()),
        ]
    return lines + [
        f"switchyard: {config.project}'s board is serving {alignment.deployed_release} and every "
        "check passes, but the authoritative release phase is "
        + (
            f"recorded {alignment.trusted_release_state or 'pending'}"
            if alignment.trusted_readable
            else "not readable from this account"
        )
        + f". An operator closes it with `pkexec switchyard release-status {config.project} "
        "--close`, which re-verifies the live build and deploys, restarts and rolls back nothing.",
    ]


def _plan_data_from_config(config: ProjectConfig, config_path: Path) -> dict[str, Any]:
    plan_path = config_path.parent / "plan.json"
    # Following nothing, and falling back to what the configuration itself says
    # rather than to whatever a planted link names. A document that cannot be
    # read safely is absent as far as this is concerned (SYRD-228).
    document, _problem = read_tenant_document_no_follow(
        plan_path, what=f"{config.project}'s generated runtime plan"
    )
    raw = dict(document or {})
    if raw:
        return raw
    port = None
    match = re.search(r":([0-9]{1,5})(?:/|$)", config.board_url)
    if match:
        port = int(match.group(1))
    board_root = _tenant_board_root_from_config(config)
    return {
        "project": config.project,
        "owner_user": config.run_as_user or current_user_name(),
        "port": port,
        "database": "pgu" if config.project == "pgu" else f"{config.project}_ticket_board",
        "ticket_prefix": config.ticket_prefix,
        "source_repo": str(_repo_root()),
        "board_root": str(board_root) if board_root else None,
        "asset_dir": None,
        "frame_dir": None,
        "audit_roles": ["audit"] if any(role.role == "audit" for role in config.roles) else [],
        "board_service_traversal": True,
        "operation_allowed_roles": [],
    }


@dataclass(frozen=True)
class ResidualProcess:
    """A process still carrying this project's identity after its session died."""

    pid: int
    uid: int
    command: str


def _process_environ(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for item in raw.decode("utf-8", "replace").split("\0"):
        name, separator, value = item.partition("=")
        if separator:
            values[name] = value
    return values


def _process_ancestry(pid: int, *, proc_root: Path) -> set[int]:
    """This process and everything that started it, so a stop cannot kill itself."""
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            stat = (proc_root / str(current) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        # Field 4 is the parent, counted after the LAST ')': a comm may contain
        # spaces and parentheses, and splitting on whitespace reads the wrong
        # field for any process whose name does (SYRD-169).
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 2:
            break
        try:
            current = int(tail[1])
        except ValueError:
            break
    return seen


def process_systemd_unit(entry: Path) -> str:
    """The systemd unit a process belongs to, from its own cgroup.

    Real unit metadata, not a guess from a name: the kernel reports
    `0::/system.slice/<project>-ticket-board.service` for a system unit and
    `0::/user.slice/user-<uid>.slice/user@<uid>.service/app.slice/<unit>` for one
    of the owner's, and the last segment is the unit either way. Readable
    without privilege, which matters because the caller here IS root and can
    read everything -- the exclusion has to hold for the one reader that sees
    every process.
    """
    try:
        raw = (entry / "cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in raw.splitlines():
        path = line.rpartition(":")[2]
        segment = path.rstrip("/").rpartition("/")[2]
        if segment.endswith(".service") or segment.endswith(".scope"):
            return segment
    return ""


def residual_project_processes(
    config: ProjectConfig,
    *,
    proc_root: Path | None = None,
    exclude: Iterable[int] = (),
) -> list[ResidualProcess]:
    """Processes still carrying this project's identity, by environment.

    The marker is `TICKET_BOARD_PROJECT`, read from the ENVIRONMENT rather than
    from argv. That is the whole point: a child that outlived its pane -- or
    that exec'd away from the wrapper that started it -- keeps its inherited
    environment, while the argv marker is gone the moment it execs, which is the
    mistake SYRD-169 was. An exact match on the slug, because `atlas` must not
    answer for `atlas-staging`.

    This command's own process and every one of its ancestors are excluded:
    running `switchyard stop` from inside a role pane must not report, or
    terminate, the shell that is running it.
    """
    root = Path(proc_root) if proc_root is not None else Path("/proc")
    skip = set(exclude) | _process_ancestry(os.getpid(), proc_root=root)
    residual: list[ResidualProcess] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return residual
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue
        environment = _process_environ(entry)
        if environment.get("TICKET_BOARD_PROJECT", "") != config.project:
            continue
        # A role WORKLOAD, positively: a pane and everything it started carry a
        # caller role, and a managed service does not. The board unit sets
        # `TICKET_BOARD_PROJECT` too -- on this host
        # `testing-ticket-board.service` literally does -- so the project marker
        # alone selects the services this stop is supposed to stop through their
        # own managers (SYRD-193 review).
        if not environment.get("TICKET_BOARD_CALLER_ROLE", "").strip():
            continue
        # And belt-and-braces from the kernel's own record of unit membership,
        # so a service that ever gained a caller role in its environment is
        # still out of scope.
        if process_systemd_unit(entry) in managed_unit_names(config):
            continue
        try:
            uid = entry.stat().st_uid
            command = (entry / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ").strip()
        except OSError:
            continue
        residual.append(ResidualProcess(pid, uid, command or f"pid {pid}"))
    return residual


def contain_residual_project_processes(
    config: ProjectConfig,
    *,
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Terminate what escaped the panes, and report precisely what would not go.

    A suspension that leaves a role's work running is not a suspension: the
    tenant looks stopped and is still writing. Everything signalled here carries
    this project's own identity in its environment, so nothing belonging to
    another tenant -- or to the desktop account's own unrelated work -- is in
    scope even when they share a uid.
    """
    problems: list[str] = []
    for process in residual_project_processes(config, proc_root=proc_root):
        try:
            signaller(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            problems.append(
                f"{config.project} process {process.pid} (uid {process.uid}) survived its pane and "
                f"this process may not stop it: {process.command[:120]}"
            )
            continue
        except OSError as exc:
            problems.append(f"could not stop {config.project} process {process.pid}: {exc}")
            continue
        print_func(f"stopped escaped {config.project} process: {process.pid} ({process.command[:80]})")
    return problems


#: What a tenant's runtime can be, as `status` and `list` report it. A
#: suspension is reversible and keeps every byte of restart state; a teardown
#: removes provisioned state and is a different verb (SYRD-193).
TENANT_RUNTIME_STATES = (
    "running",              # board, listener, sessions and a window
    "presentation-closed",  # the window is gone, everything else still runs
    "partially-stopped",    # some of it is down and some is not
    "suspended",            # nothing runs, everything is preserved
    "unregistered",         # no registration: torn down, or never provisioned
)


@dataclass(frozen=True)
class TenantRuntime:
    """What is actually up for one tenant, each part asked of its own manager."""

    project: str
    board_active: bool
    listener_active: bool
    live_sessions: tuple[str, ...]
    presentation_open: bool
    residual: tuple[int, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        running = [self.board_active, self.listener_active, bool(self.live_sessions)]
        if all(running) and self.presentation_open:
            return "running"
        if all(running) and not self.presentation_open:
            return "presentation-closed"
        if not any(running) and not self.presentation_open and not self.residual:
            return "suspended"
        return "partially-stopped"


def describe_tenant_runtime(runtime: TenantRuntime) -> str:
    """One line a person can act on, naming what is still up."""
    parts = [
        f"board {'up' if runtime.board_active else 'down'}",
        f"listener {'up' if runtime.listener_active else 'down'}",
        f"sessions {len(runtime.live_sessions)}",
        f"window {'open' if runtime.presentation_open else 'closed'}",
    ]
    if runtime.residual:
        parts.append(f"escaped processes {len(runtime.residual)}")
    line = f"{runtime.project}: {runtime.state} ({', '.join(parts)})"
    if runtime.unknown:
        line += " -- not proved: " + "; ".join(runtime.unknown)
    return line


def suspend_tenant(
    config: ProjectConfig,
    *,
    config_path: Path,
    gui_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Suspend one tenant: reversible, project-scoped, and honest about failure.

    The order is the dependency order read backwards, because each step's
    consumer has to go first: the window before the sessions it frames, the
    sessions before the listener that wakes them, the listener before the board
    it reads. Nothing here removes state -- no database, no worktree, no
    registration, no journal -- which is the whole difference between this and
    `teardown`.

    Every step runs even if an earlier one failed. A window that would not close
    is no reason to leave a board serving, and reporting the first failure while
    silently skipping the rest is how a tenant ends up half suspended with one
    line of output about it.
    """
    problems: list[str] = []
    problems.extend(
        close_presentation_window(
            config, config_path=config_path, gui_user=gui_user,
            proc_root=proc_root, signaller=signaller, print_func=print_func,
        )
    )
    if stop_project(config, runner=runner, print_func=print_func) != 0:
        problems.append(f"not every {config.project} session could be stopped")
    problems.extend(
        contain_residual_project_processes(
            config, proc_root=proc_root, signaller=signaller, print_func=print_func
        )
    )
    listener_problems = stop_owner_listener(config, runner=runner, config_path=config_path)
    problems.extend(listener_problems)
    if not listener_problems:
        print_func(f"stopped listener: {_listener_user_unit(config)}")
    board_problems = _board_system_unit_action(config, "stop", runner=runner)
    problems.extend(board_problems)
    if not board_problems:
        print_func(f"stopped board: {_board_system_unit(config)}")
    return problems


def resume_tenant(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Bring a suspended tenant back, in dependency order, stopping at the boundary.

    Forwards this time: the board before the listener that reads it, and both
    before the sessions whose panes talk to them. Unlike the suspension, this
    STOPS at the first failed boundary and says which one -- starting sessions
    against a board that did not come up produces panes that cannot register,
    and a report of success over them is worse than the failure.
    """
    # The staged bundle first, because it is what every pane this resume leads
    # to will run. This can be reached as root (`sudo switchyard start`) or as
    # the project owner (the control bridge crossed to it), so it repairs only
    # in the first case and reports in the second: the owner may not stage
    # root's files, and the operator's own path repairs these two programs
    # before it crosses, through the recorded privileged command
    # `ensure_tenant_control_helper` already uses (SYRD-211, SYRD-249 review).
    staging_problems = ensure_staged_role_tooling(config, runner=runner, print_func=print_func)
    if staging_problems:
        return staging_problems + [
            f"{config.project} was not resumed: its panes would start against tooling that is "
            "not staged. Nothing was stopped or removed"
        ]
    # Idempotent by asking first. `systemctl start` on a live unit is a no-op,
    # but the listener is restored with `restart`, which would bounce a healthy
    # one -- and resuming an already-running tenant must not interrupt it.
    if board_system_unit_is_active(config, runner=runner):
        print_func(f"already running board: {_board_system_unit(config)}")
        board_problems: list[str] = []
    else:
        board_problems = _board_system_unit_action(config, "start", runner=runner)
    if board_problems:
        return board_problems + [
            f"{config.project} was not resumed past its board; nothing after it was started, and "
            "the tenant is still suspended rather than half up"
        ]
    if not board_problems and not board_system_unit_is_active(config, runner=runner):
        return [f"{_board_system_unit(config)} did not come up; nothing after it was started"]
    print_func(f"started board: {_board_system_unit(config)}")
    if capture_listener_state(config, runner=runner, config_path=config_path) == "active":
        print_func(f"already running listener: {_listener_user_unit(config)}")
        return []
    listener_problems = start_owner_listener(config, runner=runner, config_path=config_path)
    if listener_problems:
        return listener_problems + [
            f"{config.project}'s board is up but its listener is not; its roles would run without "
            "notifications, so no session was started"
        ]
    print_func(f"started listener: {_listener_user_unit(config)}")
    return []


def stop_project(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    # The viewer and display sessions belong to the project owner; each role's
    # session lives in that role's own tmux server, so it has to be probed and
    # killed there or stop reports success while the session is still alive
    # (SYRD-39).
    owner_runner = runner
    if config.run_as_user and current_user_name() != config.run_as_user:
        owner_runner = _owner_process_runner(owner_user=config.run_as_user, runner=runner)
    exit_code = 0
    viewer_session = viewer_session_for_project(config.project)
    viewer_exists = owner_runner(
        tmux_has_session_by_name_args(viewer_session),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if viewer_exists:
        result = owner_runner(
            tmux_kill_session_by_name_args(viewer_session),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            reason = _proc_failure_reason(result, f"tmux kill-session failed with exit {result.returncode}")
            print_func(f"failed to stop viewer: {viewer_session}: {reason}")
            exit_code = exit_code or int(result.returncode)
        else:
            print_func(f"stopped viewer: {viewer_session}")
    else:
        print_func(f"already stopped viewer: {viewer_session}")
    from scripts import presentation_controller

    presentation_stop = presentation_controller.stop_presentation(
        config,
        runner=runner,
        print_func=print_func,
    )
    exit_code = exit_code or presentation_stop
    # Always, not short-circuited on an earlier failure: a viewer that would not
    # close is no reason to leave every worker running.
    workers_stopped = stop_role_sessions(config, runner=runner, print_func=print_func)
    return exit_code or workers_stopped


def _layout_slot_count(config: ProjectConfig) -> int:
    try:
        layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot read layout {config.layout}: {exc}") from exc
    return len(_layout_leaves(layout))


def _raw_role_for_update(raw_roles: Any, role_name: str) -> dict[str, Any]:
    if not isinstance(raw_roles, list):
        raise SystemExit("team-launcher: launcher config roles must be a JSON list")
    for raw_role in raw_roles:
        if isinstance(raw_role, dict) and str(raw_role.get("role") or "").strip() == role_name:
            return raw_role
    raise SystemExit(f"unknown role {role_name!r} in launcher config")


def _write_role_visibility(
    config: ProjectConfig,
    *,
    config_path: Path,
    role: RoleConfig,
    detached: bool,
    slot: int | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> ProjectConfig:
    raw_config = _load_json(config_path)
    raw_role = _raw_role_for_update(raw_config.get("roles"), role.role)
    if detached:
        raw_role["detached"] = True
        raw_role.pop("slot", None)
    else:
        if slot is None:
            raise ValueError("visible role update requires slot")
        raw_role["detached"] = False
        raw_role["slot"] = slot
    try:
        _write_json_atomic(config_path, raw_config)
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot update launcher config {config_path}: {exc}") from exc
    ensure_owner_file(config, config_path, runner=runner)
    return load_project_config(config.project, config_path)


def detach_role_from_slot(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    role = _role_by_name(config, role_name)
    if role.detached:
        print_func(f"team-launcher: role {role.role} is already detached")
        return 0
    previous_slot = role.slot
    _write_role_visibility(
        config,
        config_path=config_path,
        role=role,
        detached=True,
        slot=None,
        runner=runner,
    )
    print_func(f"team-launcher: detached role {role.role} from slot {previous_slot}; tmux session remains headless")
    if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return 0
    detach_proc = runner(tmux_detach_clients_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if detach_proc.returncode != 0:
        print_func(f"team-launcher: no live tmux client detached for {role.role}; session remains configured headless")
    return 0


def _role_by_name(config: ProjectConfig, role_name: str) -> RoleConfig:
    for role in config.roles:
        if role.role == role_name:
            return role
    raise SystemExit(f"unknown role {role_name!r} in project {config.project}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch or reload a JSON-configured project team.")
    parser.add_argument("project", help="project name, for example pgu")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=[
            "start",
            "attach",
            "reload",
            "stop",
            "design",
            "new",
            "provision-runtime",
            "deploy-launcher",
            "upgrade",
            "add-role",
            "set-vcs-close-role",
            "pane",
            "teardown",
        ],
        help="start is idempotent attach-or-start (resumes tracked session ids when relaunching a stopped pane); reload force-restarts running CLIs with tracked resume ids",
    )
    parser.add_argument("pane_mode", nargs="?")
    parser.add_argument("role", nargs="?")
    parser.add_argument("--slot", type=int, help="layout slot for `pane attach-role <role>`")
    parser.add_argument("--config", type=Path, help="project launcher config JSON")
    parser.add_argument("--layout-output", type=Path, help="write generated Konsole layout here")
    parser.add_argument(
        "--layout",
        choices=sorted(LAYOUT_MODE_CHOICES),
        default=LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument("--script-path", type=Path, default=Path(__file__).resolve().with_name("team-launcher"))
    parser.add_argument("--pane-state-dir", type=Path, help=f"write initial pane idle state here (default: {DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--no-attach", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="print launch plan without starting Konsole")
    parser.add_argument("--confirm", help="project slug required for destructive teardown")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow teardown to drop a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow teardown to remove a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="teardown may remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument("--remove-owner-user", action="store_true", help="teardown may remove the owner Unix account")
    parser.add_argument("--owner-user", help="new project owner Unix user (default: <project>-agent)")
    parser.add_argument("--port", type=int, help="new project board port; omitted means deterministic allocation")
    parser.add_argument("--database", help="new project PostgreSQL database; omitted means <project>_ticket_board")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--repository", type=Path, help="project working checkout opened by generated panes")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design command")
    parser.add_argument("--new-output-dir", type=Path, help="write new-project artifacts here")
    parser.add_argument("--design-output-dir", type=Path, help="write design document and project artifact here")
    parser.add_argument("--project-artifact", type=Path, help="write project artifact here during design")
    parser.add_argument("--design-document", type=Path, help="write project design document here during design")
    parser.add_argument("--design-title", help="project design document title")
    parser.add_argument("--design-body", help="project design document body")
    parser.add_argument("--remote", help="project git remote name for generated launcher config")
    parser.add_argument("--default-branch", help="project default branch for generated launcher config")
    parser.add_argument("--worktree-policy", choices=sorted(WORKTREE_POLICIES), help="shared or isolated role worktrees")
    parser.add_argument("--ticket-prefix", help="ticket id prefix for the new board, e.g. OTTO")
    parser.add_argument("--upstream-report-url", help="board URL where tenant reports should be filed")
    parser.add_argument("--upstream-report-token-file", help="0600 file containing the report-only token for --upstream-report-url")
    parser.add_argument("--push-policy", help="reviewable push policy label recorded in the design artifact")
    parser.add_argument("--audit-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether audit signoff is a project gate")
    parser.add_argument("--needs-inspection", action=argparse.BooleanOptionalAction, default=None, help="record whether inspection is a project gate")
    parser.add_argument("--needs-user-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether user signoff is a project gate")
    parser.add_argument("--board-service-traversal", action=argparse.BooleanOptionalAction, default=None, help="record whether boardsvc may traverse the owner home")
    parser.add_argument("--supplementary-group", action="append", dest="supplementary_groups", help="owner-user supplementary group to record; repeat as needed")
    parser.add_argument("--linger", action=argparse.BooleanOptionalAction, default=None, help="record whether linger should be enabled")
    parser.add_argument("--owner-shell", help="owner user's shell to record")
    parser.add_argument("--execute", action="store_true", help="execute new-project provisioning after precheck")
    parser.add_argument("--runtime-user", help="local user whose lingering /run/user/<uid> runtime should be provisioned")
    parser.add_argument("--launcher-repo", type=Path, help="launcher checkout to update or verify (default: this script's repo)")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy during upgrade (default: the pinned release, else origin/main)")
    parser.add_argument("--cli", dest="add_role_cli", default="codex", help="CLI runtime for `add-role` (default: codex)")
    parser.add_argument("--audit", dest="add_role_audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--detached", action="store_true", help="configure `add-role` as headless instead of visible")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--clean-launcher", action="store_true", help="run git clean -fdx after updating --launcher-repo")
    parser.add_argument("--force", action="store_true", help="allow reload to kill/relaunch even if live command validation fails")
    parser.add_argument("--allow-stale-launcher", action="store_true", help="emergency override: warn but proceed when the launcher checkout is provably stale")
    parser.add_argument("--no-launcher-self-deploy", action="store_true", help="restore refuse-only behavior instead of automatically fast-forwarding a stale launcher checkout")
    parser.add_argument("--skip-launcher-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-policy", type=Path, help="approved Wayland JSON policy or headless; installed before launch")
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    return parser


def _build_switchyard_new_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard new", description="Create a Switchyard project.")
    parser.add_argument("--slug", help="project slug; prompted when omitted")
    parser.add_argument("--agent-name", help="owner user; prompted default is <slug>-agent when omitted")
    parser.add_argument("--project-name", help="human project name; prompted when omitted")
    parser.add_argument("--project-path", type=Path, help="project working directory; prompted when omitted")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design session")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--output-dir", type=Path, help="write provisioning artifacts here")
    parser.add_argument(
        "--agent-cli-policy",
        choices=list(AGENT_CLI_POLICIES),
        default="",
        help=(
            "what an unattended run may do about a selected CLI that is not installed host-wide: "
            "require-host-wide refuses before creating anything; promote-local copies an "
            "executable you name to /usr/local/bin, root-owned, reusable by every later project. "
            "Switchyard never fetches a vendor installer. Interactive runs ask instead."
        ),
    )
    parser.add_argument(
        "--agent-cli-source",
        action="append",
        default=[],
        metavar="CLI=PATH",
        help=(
            "local executable to promote host-wide for CLI, required by "
            "--agent-cli-policy promote-local; repeatable"
        ),
    )
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database; omitted means <slug>_ticket_board")
    parser.add_argument("--yes", action="store_true", help="proceed without the confirmation prompt")
    parser.add_argument(
        "--no-git-init",
        action="store_true",
        help="do not initialize --project-path; it must already be a git repository with an initial commit",
    )
    parser.add_argument(
        "--allow-existing-owner-user",
        action="store_true",
        help="reuse an existing owner user without the existing-user confirmation prompt",
    )
    parser.add_argument(
        "--agy-credential-source",
        metavar="USER",
        help=(
            "override this host's recorded agy credential source for this project; copies USER's "
            "agy OAuth token into the owner user so the project's panes do not need their own "
            "agy login, sharing that one Google account across every role"
        ),
    )
    parser.add_argument(
        "--no-agy-credential",
        action="store_true",
        help="do not seed any agy credential for this project, ignoring this host's recorded source",
    )
    parser.add_argument(
        "--layout",
        choices=sorted(LAYOUT_MODE_CHOICES),
        default=LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument(
        "--desktop-policy",
        type=Path,
        help=(
            "advanced: import an explicit policy. headless, or a JSON file recording scoped "
            "Wayland consent. Ordinary desktop provisioning generates this itself"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="install without screenshot or clipboard access; works with no compositor present",
    )
    parser.add_argument(
        "--desktop-gui-user",
        default="",
        help=(
            "the desktop account this project may use, when more than one is signed in; "
            "otherwise the single active Wayland session is used"
        ),
    )
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    return parser


def _build_switchyard_repair_boundary_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard repair-boundary",
        description=(
            "Apply the reviewed repository and worktree authority boundary to a registered "
            "tenant. The commands are lifted from root's own installed packet, between the "
            "markers it writes around that phase, so what runs is what root installed and "
            "nothing else in the packet runs at all -- no deploy, no schema or workflow seed, "
            "no RBAC, no unit installation or reload, no session startup. Writes nothing "
            "without --apply, needs Polkit, and is kept in the rollout journal."
        ),
    )
    parser.add_argument("project", help="the registered project to repair")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="run the repair; without it this reports what is open and changes nothing",
    )
    return parser


def _build_switchyard_approve_desktop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard approve-desktop",
        description=(
            "Record, show or withdraw this host's standing desktop approval -- the one a "
            "project's scoped Wayland policy is generated from. Approving and revoking need "
            "root and record who asked, taken from the mechanism that elevated the run "
            "(PKEXEC_UID, then SUDO_USER) rather than from a flag. This is a host-level "
            "standing grant and is not --desktop-policy, which supplies a policy for one "
            "launch."
        ),
    )
    parser.add_argument(
        "--gui-user",
        default="",
        metavar="USER",
        help="whose desktop this host is approving; inferred when exactly one is signed in",
    )
    parser.add_argument(
        "--reference",
        default="",
        metavar="TEXT",
        help="required: on what basis this was approved, kept in the record",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--show", action="store_true", help="print the current record and stop")
    action.add_argument(
        "--revoke",
        action="store_true",
        help="withdraw the approval, keeping a record of who withdrew it and why",
    )
    return parser


def _build_switchyard_resume_provision_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard resume-provision",
        description=(
            "Finish a project whose provisioning stopped before it was registered: rebuild "
            "root's artifacts from an audited release, hand back the operator packet while it "
            "is unfinished, then register the generated configuration and start the configured "
            "roles. Re-runnable -- it continues from wherever the last attempt stopped."
        ),
    )
    parser.add_argument("project", help="the project slug root holds a provisioning record for")
    parser.add_argument(
        "--source-repo",
        type=Path,
        help=(
            "the audited release to rebuild the artifacts from; defaults to the installed "
            "shared release"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        dest="config_path",
        help=(
            "the generated launcher configuration to register, for a project whose checkout "
            "is not where it was generated; it is checked against root's record either way"
        ),
    )
    return parser


def _build_switchyard_register_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard register", description="Register an existing Switchyard project config.")
    parser.add_argument("config_path", type=Path, help="path to the project's generated launcher config JSON")
    return parser


def _build_switchyard_upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard upgrade", description="Upgrade safe generated artifacts for a Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing files")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy (default: the pinned release, else origin/main)")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="replace and persist the git repository path(s) used to verify board commit hashes",
    )
    parser.add_argument("--desktop-policy", type=Path, help="headless, or a JSON file recording scoped Wayland consent; installed before role launch")
    parser.add_argument(
        "--upstream-report-url",
        default="",
        help=(
            "board URL where this tenant files reports; recorded in its configuration and "
            "refreshed from that board's own credential, so panes need no flags afterwards"
        ),
    )
    parser.add_argument(
        "--upstream-report-token-file",
        default="",
        help="where the report-only credential belongs (default: ~/.config/<project>/upstream-report.env)",
    )
    parser.add_argument(
        "--publish-remote",
        default="",
        help=(
            "the exact remote root may publish to, recorded root-owned and reused afterwards; "
            "it is never read from the project account, which every role runs as"
        ),
    )
    return parser


def _build_switchyard_install_shared_release_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard install-shared-release",
        description=(
            "Make an already-built release this host's current one. Takes a full "
            "content-addressed commit and nothing else: the release is resolved against "
            "root's own cache under /opt/switchyard/releases, and a caller never names a "
            "path. The previous target is recorded before anything moves, the pointer is "
            "swapped atomically, what landed is verified, and the previous target is put "
            "back if it does not verify."
        ),
    )
    parser.add_argument("--commit", default="", help="the full 40-character commit to activate")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="return to the target recorded by the last activation",
    )
    parser.add_argument("--dry-run", action="store_true", help="report and change nothing")
    return parser


def switchyard_install_shared_release_command(
    commit: str,
    *,
    rollback: bool = False,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
    boundary_root: Path | None = None,
) -> int:
    """Activate a shared release, as root, with a way back recorded first.

    `boundary_root` redirects where the privileged boundary is installed; only
    a sandbox passes it.
    """
    from scripts.ticket_board import privileged_actions, shared_release_activation

    if rollback and commit:
        print_func("switchyard: give either --commit or --rollback, not both")
        return 2
    if not rollback:
        try:
            commit = privileged_actions.action_for("install-shared-release").validate(
                {"commit": commit}
            )["commit"]
        except privileged_actions.ArgumentError as exc:
            print_func(f"switchyard: {exc}")
            return 2
    if os.geteuid() != 0:
        # Not a refusal on principle: without root the pointer cannot move, and
        # a message saying so beats a permission error from three frames down.
        print_func(
            "switchyard: install-shared-release changes /opt/switchyard/current and must "
            "run as root. It is reached through `switchyard privileged-action <project> "
            "install-shared-release commit=<sha>`, which asks polkit for it"
        )
        return 1
    if dry_run:
        record = shared_release_activation.recorded_rollback()
        current = shared_release_activation.read_pointer()
        print_func(f"switchyard: current points at {current or 'nothing'}")
        print_func(
            "switchyard: would activate "
            + (f"the recorded previous target {record.get('previous_target') or 'nothing'}"
               if rollback else commit)
        )
        return 0
    try:
        if rollback:
            result = shared_release_activation.rollback(print_func=print_func)
        else:
            result = shared_release_activation.activate(commit, print_func=print_func)
    except shared_release_activation.ActivationFailed as exc:
        print_func(f"switchyard: {exc}")
        return 1
    print_func(f"switchyard: {result.describe()}")
    if getattr(result, "rolled_back", False) or not getattr(result, "release_root", ""):
        return 0
    # The host's boundary follows the host's shared release, so installing one
    # installs the other: the actions a release catalogues are usable as soon
    # as it is current, with no tenant upgrade in between (SYRD-284).
    problems = install_host_privileged_boundary(
        Path(result.release_root), staging_root=boundary_root, print_func=print_func
    )
    if problems:
        for problem in problems:
            print_func(f"switchyard: {problem}")
        print_func(
            f"switchyard: {result.commit} is current, but this host's privileged boundary was "
            "not installed from it; run this again to repair it"
        )
        return 1
    return 0


def _build_switchyard_privileged_action_parser() -> argparse.ArgumentParser:
    from scripts.ticket_board import privileged_actions

    known = ", ".join(action.name for action in privileged_actions.CATALOGUE)
    parser = argparse.ArgumentParser(
        prog="switchyard privileged-action",
        description=(
            "Run one bounded privileged Switchyard operation. An action is a name from a "
            "fixed catalogue plus typed values -- never a program, a path, a command string "
            "or an environment -- and polkit is asked only whether one installed, root-owned "
            "helper may run that named action. Use --dry-run first: it reports the exact "
            "action, the validated arguments, the installed policy, what root would run and "
            "the rollback path, and asks for no privilege at all. "
            f"Catalogued actions: {known}."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("action", help="a catalogued action name")
    parser.add_argument(
        "values",
        nargs="*",
        metavar="key=value",
        help="typed values for the action; anything undeclared is refused",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be asked for and stop, without requesting privilege",
    )
    return parser


def _build_switchyard_rollout_log_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard rollout-log",
        description=(
            "Read what a privileged provisioning or upgrade run recorded. The journal is "
            "root-owned and every role may read it without sudo, which is the point: the "
            "evidence of a run is not owned by the account that run was about. The chained "
            "index is an integrity check -- it catches an entry edited, removed or reordered "
            "without the hashes after it being recomputed -- and not a proof against root, "
            "which owns the whole file and could recompute them."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--attempt", default="", help="one attempt id, or the latest by default")
    parser.add_argument(
        "--output", action="store_true", help="print the captured stdout and stderr as well"
    )
    return parser


def rollout_log_command(
    project: str, *, attempt: str = "", output: bool = False, print_func=print
) -> int:
    """Show the journal, or one attempt of it. Reads only; needs no privilege."""
    from scripts.ticket_board.rollout_journal import (
        RESULT_NAME,
        attempts,
        format_attempts,
        project_journal_dir,
        verify_index,
    )

    records = attempts(project)
    problems = verify_index(project)
    if not attempt:
        print_func(format_attempts(project, records, problems))
        if not records:
            print_func(
                f"switchyard: nothing has been recorded for {project} under "
                f"{project_journal_dir(project)}"
            )
            return 0
        attempt = records[-1]["attempt"]
        if not output:
            return 1 if problems else 0
    selected = next((record for record in records if record["attempt"] == attempt), None)
    if selected is None:
        print_func(f"switchyard: {project} has no recorded attempt {attempt}")
        return 1
    directory = Path(selected.get("directory") or "")
    result = directory / RESULT_NAME if directory else None
    if result is not None and result.is_file():
        print_func(result.read_text(encoding="utf-8").rstrip())
    else:
        print_func(
            f"switchyard: attempt {attempt} recorded no result; it started at "
            f"{selected.get('started_at', '?')} and never completed"
        )
    if output and directory:
        for name in ("stdout.log", "stderr.log"):
            path = directory / name
            if path.is_file():
                print_func(f"--- {name} ---")
                print_func(path.read_text(encoding="utf-8", errors="replace").rstrip())
    return 1 if problems else 0


def _build_switchyard_publication_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard publication-status",
        description=(
            "Report where a project's publication-key cutover stands, and optionally check the "
            "one thing no successful push can establish."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "ask the forge whether the shared project credential may still write. The check is a "
            "dry-run push of the remote's own tip onto its own ref: it proposes no change, moves "
            "no ref, and leaves read access alone"
        ),
    )
    return parser


def publication_status_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    verify: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Report the cutover, and nothing else at all.

    Deliberately not a flag on `upgrade`. Asking whether one credential may
    write should not run a tenant upgrade: that path resolves releases, stages
    root-owned tooling, rewrites grants and advances recorded phases, and none of
    that is what was asked for. The only thing this can write is the root-owned,
    non-secret evidence file, and only when --verify is given (SYRD-116 review).
    """
    from scripts.ticket_board.project_provision import (
        owner_github_key_path,
        publish_identity_path,
        resolve_owner_github_identity,
    )
    from scripts.ticket_board.publication_boundary import (
        CUTOVER_READY,
        cutover_state,
        read_cutover_evidence,
        resolve_pinned_remote,
        shared_credential_check_command,
        verify_shared_credential,
    )

    project = config.project
    owner_user = config.run_as_user or current_user_name()
    owner_home = home_dir_for_user(owner_user)
    plan_data = _plan_data_from_config(config, config_path)
    selected = resolve_owner_github_identity(
        str(owner_home),
        recorded_key_name=str(plan_data.get("owner_github_key_name") or ""),
        recorded_host_alias=str(plan_data.get("owner_github_host_alias") or ""),
    )
    shared_identity = owner_github_key_path(
        str(owner_home), key_name=selected.key_name if selected.resolved else ""
    )

    remote, remote_problem = resolve_pinned_remote(
        project, registration_root=switchyard_privileged_provision_root(), declared_remote=""
    )
    if not remote:
        print_func(f"switchyard: {project} has no root-owned publication remote: {remote_problem}")
        return 1

    publication_fingerprint = _public_key_fingerprint(
        f"{publish_identity_path(project)}.pub", runner=runner
    )
    shared_fingerprint = _public_key_fingerprint(f"{shared_identity}.pub", runner=runner)
    if not publication_fingerprint:
        print_func(
            f"switchyard: {project}'s publication public key could not be read, so the key in use "
            "cannot be identified and no recorded verdict describes it."
        )
    if not shared_fingerprint:
        print_func(
            f"switchyard: {project}'s shared credential could not be identified at "
            f"{shared_identity}.pub, so any recorded verdict about it no longer describes what is "
            "in use."
        )

    if verify:
        finding = verify_shared_credential(
            project,
            remote=remote,
            owner_user=owner_user,
            identity_file=shared_identity,
            publication_fingerprint=publication_fingerprint,
            shared_fingerprint=shared_fingerprint,
            runner=runner,
        )
        print_func(
            f"switchyard: {project} shared credential write authority: {finding.state}"
            + (f" -- {finding.detail}" if finding.detail else "")
        )

    evidence = read_cutover_evidence(
        project,
        remote=remote,
        publication_fingerprint=publication_fingerprint,
        shared_fingerprint=shared_fingerprint,
    )
    for reason in evidence.stale:
        print_func(f"switchyard: {project}'s recorded cutover state no longer applies: {reason}")
    state = cutover_state(evidence)
    print_func(f"switchyard: {project} publication cutover: {state}")
    print_func(
        f"switchyard:   publication key ({publication_fingerprint or 'unidentified'}): "
        f"{evidence.publication.state}"
        + (f" -- {evidence.publication.detail}" if evidence.publication.detail else "")
    )
    print_func(
        f"switchyard:   shared credential ({shared_fingerprint or 'unidentified'}): "
        f"{evidence.shared.state}"
        + (f" -- {evidence.shared.detail}" if evidence.shared.detail else "")
    )
    if state != CUTOVER_READY and not verify:
        command = " ".join(
            shlex.quote(part)
            for part in shared_credential_check_command(
                remote, owner_user=owner_user, identity_file=shared_identity
            )
        )
        print_func(
            f"switchyard: check the shared credential with `switchyard publication-status "
            f"{project} --verify`, which runs: {command}"
        )
    return 0


def _public_key_fingerprint(path: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    """The SHA256 fingerprint of a public key, or nothing if it cannot be read."""
    shown = runner(["ssh-keygen", "-l", "-f", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if getattr(shown, "returncode", 1) != 0:
        return ""
    for token in str(getattr(shown, "stdout", "") or "").split():
        if token.startswith("SHA256:"):
            return token
    return ""


def _build_switchyard_cutover_roles_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard cutover-roles",
        description=(
            "Stop a project's roles, move them onto their own Unix accounts, restart them and "
            "verify the uid each role process is actually running as. Rolls back if it does not hold."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would happen without stopping anything")
    return parser


def _build_switchyard_finish_upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard finish-upgrade",
        description=(
            "Run the director-owned phase of a Switchyard project upgrade. Must be run by the "
            "director, unprivileged."
        ),
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument("--deploy-ref", default=None, help="board release ref to deploy (default: the pinned release, else origin/main)")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument("--commit-git-dir", help="git repository path(s) used to verify board commit hashes")
    return parser


def _build_switchyard_add_role_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard add-role", description="Add a role to an existing Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="new role name")
    parser.add_argument(
        "--cli",
        default="",
        choices=sorted(SUPPORTED_CONFIG_CLI_NAMES),
        help=(
            "CLI runtime for the role; omit it at a terminal to choose from the "
            "runtimes this host supports (default: codex)"
        ),
    )
    parser.add_argument("--audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--slot", type=int, help="visible layout slot; generated layouts append automatically when omitted")
    parser.add_argument("--detached", action="store_true", help="start the role as a headless tmux session")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--no-start", action="store_true", help="register the role without starting its pane")
    return parser


def _build_switchyard_set_vcs_close_role_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard set-vcs-close-role",
        description="Set the role that closes a provisioned project after final sign-off.",
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="existing project role allowed to mark tickets done")
    return parser


def _build_switchyard_replace_window_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard replace-window",
        description=(
            "Replace a Switchyard project's root-owned presentation window with an unprivileged "
            "one. Worker sessions keep running."
        ),
    )
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard stop", description="Stop a Switchyard project's tmux pane sessions.")
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_recover_display_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard recover-display",
        description=(
            "Reattach a project's Director display after its window slot lost it. Run it from "
            "the desktop session of the operator the project is registered to; it reaches the "
            "project owner over the tenant-control bridge and recovers the Director slot only."
        ),
    )
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_start_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard start")
    parser.add_argument("project", nargs="+")
    return parser


def _build_switchyard_teardown_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard teardown",
        description="Remove Switchyard-created provisioning artifacts for a project.",
    )
    parser.add_argument("project", help="project slug")
    parser.add_argument("--dry-run", action="store_true", help="list exactly what would be removed without changing anything")
    parser.add_argument("--confirm", help="required project slug for the destructive run")
    parser.add_argument("--owner-user", help="owner user for an unregistered partial provision (default: <project>-agent)")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow dropping a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow removing a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="also remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument(
        "--remove-owner-user",
        action="store_true",
        help="also remove the owner Unix account; off by default",
    )
    return parser


def _build_switchyard_set_role_runtime_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard set-role-runtime",
        description="Change an existing role's agent runtime and reconnect the panes showing it.",
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument("role", help="existing role whose runtime changes")
    parser.add_argument(
        "--cli",
        default="",
        choices=sorted(SUPPORTED_CONFIG_CLI_NAMES),
        help=(
            "agent runtime the role should run from now on; omit it at a terminal "
            "to choose from the runtimes this host supports"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "model the role should run, checked against the project owner's own catalog; "
            "pass an empty value to drop it and take the runtime's default. Omit it to keep "
            "the configured model, or to be asked when it is one the owner does not offer"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="switch even though the role is mid-turn; requires --reason",
    )
    parser.add_argument("--reason", default="", help="why a busy role was interrupted; recorded with the change")
    parser.add_argument("--dry-run", action="store_true", help="run every check, change nothing")
    return parser


def _owner_account_exists(owner_user: str) -> bool:
    """Whether there is an account to ask anything of yet.

    A `switchyard new` chooses its roles' models before it creates the owner,
    so "cannot enumerate" and "does not exist yet" are different answers that
    used to look identical (SYRD-250 DAT).
    """
    if not owner_user:
        return False
    try:
        pwd.getpwnam(owner_user)
    except KeyError:
        return False
    return True


def _owner_catalog_args(config: "ProjectConfig") -> tuple[str, tuple[str, ...]]:
    """The account whose model list decides, and the argv prefix to ask it."""
    owner_user = str(getattr(config, "run_as_user", "") or "")
    if not owner_user:
        return "", ()
    owner_home = _owner_home_for_auth(owner_user)
    return owner_user, tuple(_owner_command_env_args(owner_user, owner_home, []))


def _build_switchyard_present_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard present",
        description="Map persistent project role sessions into stable display slots.",
    )
    parser.add_argument("project", help="registered project name or slug")
    actions = parser.add_subparsers(dest="action", required=True)
    list_parser = actions.add_parser("list", help="show desired and actual display state")
    list_parser.add_argument("--json", action="store_true", help="emit machine-readable presentation state")
    show_parser = actions.add_parser("show", help="move a role into a display slot")
    show_parser.add_argument("role")
    show_parser.add_argument("--slot", type=int, required=True)
    swap_parser = actions.add_parser("swap", help="swap two display slots")
    swap_parser.add_argument("slot_a", type=int)
    swap_parser.add_argument("slot_b", type=int)
    hide_parser = actions.add_parser("hide", help="hide the role in a display slot")
    hide_parser.add_argument("--slot", type=int, required=True)
    focus_parser = actions.add_parser("focus", help="select a display slot in the project viewer")
    focus_parser.add_argument("--slot", type=int, required=True)
    restore_parser = actions.add_parser("restore", help="restore a configured presentation layout")
    restore_parser.add_argument("--layout", default="default")
    bootstrap_parser = actions.add_parser("bootstrap", help="create stable slots and a presentation window")
    # The native window, not the nested tmux viewer. The viewer builds sessions
    # and opens nothing, so bootstrapping into it reported a recovery that
    # nobody could see; the separate layout is one terminal, laid out by the
    # terminal itself, with the project's slots in its tabs (SYRD-65).
    bootstrap_parser.add_argument(
        "--layout", choices=(LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER), default=LAYOUT_MODE_SEPARATE
    )
    recover_parser = actions.add_parser("recover", help="make one bounded start/resume attempt for a role")
    recover_parser.add_argument("role")
    return parser


def _build_switchyard_attach_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard attach",
        description="Attach this terminal to a project role's live worker, by role name.",
    )
    parser.add_argument("project", help="registered project name or slug")
    parser.add_argument(
        "role",
        nargs="?",
        help="role to attach to; omit to list the project's roles and which are attachable",
    )
    parser.add_argument("--json", action="store_true", help="emit the role listing as JSON")
    return parser


def switchyard_attach_command(
    config: ProjectConfig,
    *,
    args: argparse.Namespace,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import presentation_controller

    return presentation_controller.attach_role_command(
        config,
        role_name=args.role,
        json_output=args.json,
        runner=runner,
        print_func=print_func,
    )


def switchyard_present_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    args: argparse.Namespace,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import presentation_controller

    if args.action == "list":
        report = presentation_controller.presentation_report(
            config,
            config_path=config_path,
            runner=runner,
        )
        presentation_controller.print_presentation_report(report, json_output=args.json, print_func=print_func)
        return 0
    presentation_controller.presentation_action(
        config,
        config_path=config_path,
        action=args.action,
        role_name=getattr(args, "role", None),
        slot=getattr(args, "slot", getattr(args, "slot_a", None)),
        other_slot=getattr(args, "slot_b", None),
        layout=getattr(args, "layout", "default"),
        runner=runner,
    )
    report = presentation_controller.presentation_report(config, config_path=config_path, runner=runner)
    presentation_controller.print_presentation_report(report, json_output=False, print_func=print_func)
    return 0


def switchyard_help_text() -> str:
    commands = ", ".join(SWITCHYARD_COMMANDS)
    return f"""Usage:
  switchyard
  switchyard <project name or slug>
  switchyard <command> [options]

Commands:
  board-skill      install or verify the portable board skill for every agent CLI
  new              create and provision a new project
  register         register an existing project config
  upgrade          update generated project artifacts and report release drift
  repair-boundary  apply the reviewed repository boundary to a registered tenant
  approve-desktop  record, show or withdraw this host's standing desktop approval
  adopt-workflow   record an existing project's declared workflow as root's own copy
  finish-upgrade   run the director-owned phase of an upgrade from the director's session
  cutover-roles    legacy compatibility command (new runtimes use the project account)
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  worker-pool      plan, apply and run a project's declared pool of interchangeable workers
  present          map persistent role sessions into stable display slots at runtime
  attach           attach this terminal to a role's live worker by project and role name
  replace-window   replace a root-owned presentation window without stopping any worker
  recover-display  reattach a project's disconnected Director display, as its desktop operator
  set-vcs-close-role
                   set which existing project role can mark tickets done
  set-role-runtime change an existing role's agent runtime and reconnect its panes
  agy-credential   show, set, or clear this host's agy credential source
  role-prompt      show, set, or clear a role's onboarding prompt
  onboarding-readiness
                   report whether every registered tenant has migrated director onboarding
  stop             suspend a project: window, sessions, listener and board, reversibly
  start            resume a suspended project in dependency order
  teardown         remove project board provisioning artifacts after a dry-run review
  release-status   compare the shared release, deployed board, live build and both journals
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: {commands}.
"""


def _switchyard_command_display(argv: Sequence[str]) -> str:
    if not argv:
        return "switchyard"
    return f"switchyard {argv[0]}"


def switchyard_invocation_requires_root(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    command = argv[0].casefold()
    if command in {"-h", "--help", "help", "--version", "version"}:
        return False
    if len(argv) >= 2 and argv[1] in {"-h", "--help"}:
        return False
    if command not in SWITCHYARD_PRIVILEGED_COMMANDS:
        return False
    # A lifecycle verb this caller can reach over their tenant's control bridge
    # needs no root at all. Saying otherwise here escalates at the trampoline,
    # before any of the routing below runs -- which would put a sudo prompt in
    # front of exactly the human the bridge exists to spare (SYRD-50 rollout
    # review). A bare project name is already unprivileged, which is why only
    # `stop` and `status` needed this.
    return not _tenant_control_can_serve(argv)


def _tenant_control_can_serve(argv: Sequence[str]) -> bool:
    """Whether this exact invocation is one the caller's control bridge runs.

    Deliberately narrow and quiet: an unrecognised shape, an unknown project or
    any error means "no", so the answer can only ever remove an escalation that
    the bridge is about to make unnecessary, never add one.
    """
    if len(argv) != 2:
        return False
    project = argv[1]
    if argv[0].casefold() not in TENANT_CONTROL_OPERATIONS:
        return False
    if not PROJECT_SLUG_RE.fullmatch(project):
        return False
    try:
        grant = _tenant_control_grant(project)
        return bool(grant) and grant.get("authorized_user") == current_user_name()
    except Exception:  # pragma: no cover - classification must never raise
        return False


def _switchyard_user_can_prompt_for_sudo() -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    try:
        user = pwd.getpwuid(os.geteuid())
        group_ids = {user.pw_gid, *os.getgroups()}
    except KeyError:
        return False
    group_names: set[str] = set()
    for gid in group_ids:
        try:
            group_names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return bool({"sudo", "wheel", "admin"} & group_names)


#: Lifecycle verbs the tenant control bridge will run. Everything else keeps
#: the operator path: the bridge exists for start/stop/status, not for
#: provisioning, upgrade or teardown.
TENANT_CONTROL_OPERATIONS = {"start", "stop", "status", "recover-display"}
TENANT_CONTROL_ROOT = Path("/usr/local/lib/switchyard")


def _tenant_control_grant(project: str, *, root: Path | None = None) -> dict[str, str]:
    """The root-owned record of who may drive this tenant without sudo.

    Read here only to decide whether to use the bridge and to refuse an
    unauthorized caller without a password prompt. The bridge re-reads and
    re-validates it as root; nothing is trusted on the strength of this read.
    """
    base = root or TENANT_CONTROL_ROOT
    try:
        payload = json.loads((base / project / "control-grant.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("project") != project:
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _tenant_control_operation(argv: Sequence[str], project_argument: str) -> str:
    """Which lifecycle verb this invocation is, if the bridge can run it."""
    if not argv:
        return ""
    head = argv[0].casefold()
    if head == project_argument.casefold():
        # `switchyard <slug>` with nothing else is the ordinary start/attach.
        return "start" if len(argv) == 1 else ""
    if head in TENANT_CONTROL_OPERATIONS and len(argv) == 2 and argv[1].casefold() == project_argument.casefold():
        return head
    return ""


#: The label the repair leaves in the rollout journal, so an operator reading it
#: can tell a staged-tool repair apart from a provisioning or upgrade run.
TENANT_CONTROL_REPAIR_LABEL = "tenant-control-repair"

#: Root, and never "whoever is asking".
#:
#: These staged paths are root-owned by requirement -- that is the whole point
#: of staging them outside the owner's home -- while this verification runs in
#: the operator's UNPRIVILEGED launcher process. So the entitled identity is a
#: property of the path, not of the caller, and
#: `untrusted_root_executable_reasons` defaulting `owner_uid` to this process's
#: own uid is exactly wrong here: live Zorin UAT rejected a correctly installed
#: tenant because verification ran as uid 1000 and demanded that uid on
#: root-owned files (SYRD-211 kickback).
TENANT_CONTROL_OWNER_UID = 0


@dataclass(frozen=True)
class TenantControlHelperState:
    """What the registered helper actually is, before root is asked to run it.

    Absence and hostility are different answers, and collapsing them is what
    SYRD-211 is. A tenant whose provisioning was interrupted after the grant and
    the sudoers rule were written, or one that predates staging, has a perfectly
    valid registration and no file -- that is recoverable by re-staging. A file
    that exists with the wrong owner, the wrong mode, or a symlink in its path
    is not recoverable by anything this program should do on its own.
    """

    project: str
    path: Path
    present: bool
    reasons: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.present and not self.reasons

    @property
    def repairable(self) -> bool:
        return not self.present and not self.reasons


def tenant_control_helper_state(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    root: Path | None = None,
    owner_uid: int | None = None,
    name: str = "switchyard-tenant-control",
) -> TenantControlHelperState:
    """Verify the helper as a pinned exec target, not just as a filename.

    The whole path is checked, not the leaf: a root-owned program in a directory
    somebody else can write is a program somebody else can replace, and the
    sudoers rule names the path rather than the bytes. That check already exists
    for exactly this reason (`untrusted_root_executable_reasons`, SYRD-62) and
    is reused here rather than approximated.

    Cross-tenant isolation is decided before any filesystem call. The slug comes
    from the command line, so a slug carrying a separator would otherwise aim
    this -- and the repair that follows -- at another tenant's directory.

    `owner_uid` is the identity entitled to have written all of it, and it
    defaults to root rather than to this process. A sandbox that cannot create
    root-owned files passes its own uid to stand in for root; production never
    does, because the caller here is deliberately unprivileged.
    """
    entitled = TENANT_CONTROL_OWNER_UID if owner_uid is None else owner_uid
    base = Path(root) if root is not None else TENANT_CONTROL_ROOT
    reasons: list[str] = []
    slug = str(project)
    if not slug or slug in {".", ".."} or "/" in slug or "\\" in slug or "\x00" in slug:
        # Refused without touching the disk: this value chooses the directory.
        return TenantControlHelperState(
            project=slug,
            path=base,
            present=False,
            reasons=(f"{slug!r} is not a project slug this may be aimed at",),
        )
    path = base / slug / name
    if grant:
        # Only a value that is there and disagrees. `_tenant_control_grant`
        # already refuses a grant whose project is not this one and returns an
        # empty mapping, so an absent key here means "no grant was read", which
        # that caller handles -- reading it as a disagreement refuses tenants
        # whose grant simply was not loaded.
        recorded = str(grant.get("project") or "")
        if recorded and recorded != slug:
            reasons.append(
                f"the grant at {base / slug} records project {recorded} rather than {slug}"
            )
    try:
        path.lstat()
        present = True
    except FileNotFoundError:
        present = False
    except OSError as exc:
        return TenantControlHelperState(
            project=slug, path=path, present=False,
            reasons=(*reasons, f"{path} cannot be inspected: {exc}"),
        )
    if present:
        reasons.extend(
            untrusted_root_executable_reasons(path, boundary=base, owner_uid=entitled)
        )
        try:
            if not path.lstat().st_mode & 0o111:
                reasons.append(f"{path} is not executable")
        except OSError as exc:  # pragma: no cover - lstat succeeded a moment ago
            reasons.append(f"{path} cannot be inspected: {exc}")
    return TenantControlHelperState(
        project=slug, path=path, present=present, reasons=tuple(reasons)
    )


def tenant_control_repair_command(
    project: str, *, release_root: str = "", root: Path | None = None
) -> str:
    """The recorded privileged command that re-stages this tenant's tooling.

    Deliberately the whole staging step rather than one `install` of one file.
    That step is the supported way these executables reach the shared path, it
    is idempotent by construction -- each name is installed if the release
    carries it and removed if it does not -- and it is scoped to this project's
    directory, so no other tenant is touched by a repair.
    """
    release = release_root or str(switchyard_shared_install_root() / "current")
    commands = role_tooling_staging_commands(project, release, staging_root=root)
    script = "\n".join(commands)
    recorder = _rollout_recorder_path()
    if recorder is None:
        return f"bash -c {shlex.quote(script)}"
    return " ".join(
        [
            "sudo",
            shlex.quote(str(recorder)),
            shlex.quote(project),
            "--label",
            TENANT_CONTROL_REPAIR_LABEL,
            "--",
            "bash",
            "-c",
            shlex.quote(script),
        ]
    )


def repair_tenant_control_helper(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> str:
    """Re-stage the missing helper through the recorded privileged path.

    Returns an empty string when the helper is usable afterwards, and the reason
    it is not otherwise. The repair is announced before it runs because it asks
    for a privileged step the operator did not type.
    """
    command = tenant_control_repair_command(project, release_root=release_root, root=root)
    print_func(
        f"switchyard: {project}: the tenant control helper is not staged; repairing it from "
        "the current release before continuing"
    )
    result = runner(["bash", "-c", command])
    code = int(getattr(result, "returncode", 1) or 0)
    if code != 0:
        return (
            f"repairing the tenant control helper for {project} failed (exit {code}); "
            "the rollout journal records the attempt"
        )
    return ""


#: The staged programs whose WIRE CONTRACT this launch depends on: the bridge
#: it crosses and the helper each tab of the window runs. Deliberately not every
#: staged executable -- restaging a tenant because its `directorctl` differs
#: would make an ordinary launch privileged on any drift at all, and say nothing
#: about whether the window can be opened (SYRD-211 DAT rejection).
PROTOCOL_STAGED_EXECUTABLES: tuple[str, ...] = (
    "switchyard-tenant-control",
    "switchyard-display-attach",
)


def staged_protocol_states(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    root: Path | None = None,
    owner_uid: int | None = None,
) -> dict[str, TenantControlHelperState]:
    """Each protocol program, checked as a pinned exec target in its own right.

    Only the bridge used to be checked, and the other one was then compared with
    `is_file()` and `read_bytes()` -- which follow symlinks and ask nothing
    about ownership or mode. So a world-writable display helper read as ordinary
    version drift and was restaged over, and a symlink whose target happened to
    carry the current bytes read as current and became the pathname root's
    display grant executes (SYRD-211 DAT rejection).

    The grant is checked once, against the tenant, rather than once per file.
    """
    states: dict[str, TenantControlHelperState] = {}
    for index, name in enumerate(PROTOCOL_STAGED_EXECUTABLES):
        states[name] = tenant_control_helper_state(
            project,
            grant=grant if index == 0 else None,
            root=root,
            owner_uid=owner_uid,
            name=name,
        )
    return states


def staged_tooling_out_of_date(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    only: Sequence[str] | None = None,
) -> list[str]:
    """Staged protocol programs whose bytes are not this release's.

    Answered only for files that have already passed the shape check: comparing
    bytes is reading a file, and reading one root is about to overwrite -- or
    that root's grant is about to execute -- is a question to ask after "is this
    a root-owned regular file at a root-controlled path", not before.

    Present is not current. A tenant repaired or provisioned by an earlier
    release keeps that release's staged copies for ever: nothing restages them
    merely because the shared release moved on, and a shape check is satisfied
    by an executable of the right shape. The preserved Zorin tenant held a
    `switchyard-display-attach` that accepts only numeric slots, so a window
    asking it for `viewer` would have been refused by the tenant's own copy
    however correct the release was (SYRD-211 DAT rejection).
    """
    release = Path(release_root) if release_root else switchyard_shared_install_root() / "current"
    staging = Path(root) if root is not None else TENANT_CONTROL_ROOT
    staging = staging / project
    names = list(only) if only is not None else list(PROTOCOL_STAGED_EXECUTABLES)
    stale: list[str] = []
    for name in names:
        source = release / "scripts" / name
        if not source.is_file():
            # Not in this release: the staging step removes it, which is that
            # step's business rather than this check's.
            continue
        target = staging / name
        try:
            if target.is_symlink() or not target.is_file():
                stale.append(name)
                continue
            if target.read_bytes() != source.read_bytes():
                stale.append(name)
        except OSError:
            stale.append(name)
    return stale


def ensure_tenant_control_helper(
    project: str,
    *,
    grant: Mapping[str, str] | None = None,
    release_root: str = "",
    root: Path | None = None,
    owner_uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> None:
    """Every protocol program: refuse, repair, or carry on -- decided per file.

    Three states, kept apart for each of them, because collapsing any two is
    what this ticket keeps finding:

    * **hostile** -- wrong owner, writable by others, a symlink anywhere in its
      path, not a regular executable. Refused, and no privileged step is run.
      Restaging over it would overwrite evidence and reward whoever put it
      there; accepting it hands root's grant a pathname somebody else controls.
    * **absent** -- a valid registration with no file. Re-staged.
    * **safe but stale** -- this release would install different bytes. Also
      re-staged, because present is not current.

    The shape of every protocol file is settled before any of them is read, and
    both shape and content are checked again after a repair (SYRD-211).
    """
    states = staged_protocol_states(
        project, grant=grant, root=root, owner_uid=owner_uid
    )
    hostile = {name: state for name, state in states.items() if state.reasons}
    if hostile:
        lines: list[str] = []
        for name, state in hostile.items():
            lines.append(f"switchyard: refusing to run {state.path} as root:")
            lines.extend(f"switchyard:   {reason}" for reason in state.reasons)
        lines.append(
            "switchyard: this is not repaired automatically, and nothing was run in its "
            "place. A staged program somebody else can write is not version drift"
        )
        raise SystemExit("\n".join(lines))

    absent = [name for name, state in states.items() if not state.present]
    safe = [name for name, state in states.items() if state.present]
    stale = staged_tooling_out_of_date(
        project, release_root=release_root, root=root, only=safe
    )
    if not absent and not stale:
        # Correct, current, and nothing rewritten: a second launch costs a few
        # stats and no privileged step.
        return
    if absent:
        for name in absent:
            print_func(
                f"switchyard: {project}: {name} is not staged; repairing it from the "
                "current release before continuing"
            )
    if stale:
        print_func(
            f"switchyard: {project}'s staged tooling is from an older release "
            f"({', '.join(stale)}); restaging it from the current one before continuing"
        )
    problem = repair_tenant_control_helper(
        project, release_root=release_root, root=root, runner=runner, print_func=print_func
    )
    if problem:
        raise SystemExit(f"switchyard: {problem}")

    # Shape AND content, for every protocol file, before anything crosses.
    states = staged_protocol_states(
        project, grant=grant, root=root, owner_uid=owner_uid
    )
    unusable = {name: state for name, state in states.items() if not state.usable}
    remaining = staged_tooling_out_of_date(
        project,
        release_root=release_root,
        root=root,
        only=[name for name, state in states.items() if state.present],
    )
    if not unusable and not remaining:
        for name in absent:
            print_func(
                f"switchyard: {project}: {name} repaired at {states[name].path}"
            )
        print_func(f"switchyard: {project}'s staged tooling is this release's")
        return
    detail = "; ".join(
        [
            *(
                f"{name}: {'; '.join(state.reasons) or 'still not staged'}"
                for name, state in unusable.items()
            ),
            *(f"{name}: still not this release's" for name in remaining),
        ]
    )
    raise SystemExit(
        f"switchyard: {project}'s staged tooling is still unusable after repair, so it "
        f"could not be brought up to this release: {detail}"
    )


#: A staged file that is simply not there. Absence is the one shape an
#: ordinary launch may repair by itself: SYRD-211 deliberately refused to
#: restage on DRIFT, because "this release would install different bytes" is
#: true of every tenant the moment the shared release moves, and would make
#: every launch privileged. Nothing being there at all is not drift.
STAGED_TOOLING_ABSENT_MARKER = "is not staged at"
#: Shapes an ordinary launch refuses rather than overwrites: somebody else's
#: file, one anybody may write, one that is not a regular executable.
STAGED_TOOLING_HOSTILE_MARKERS = ("owned by uid", "writable", "is not a regular file")


def tenant_pinned_release_root(
    project: str, *, root: Path | None = None, install_root: Path | None = None
) -> Path | None:
    """The release a tenant's staged bundle was made from, when it says so.

    A launch is not an upgrade. Validating a tenant against whatever the host
    installed most recently would call an older tenant's complete bundle
    incomplete -- a file the NEWER release added is not missing from the older
    one -- and repairing it from `current` would move that tenant's role
    tooling while its board stayed pinned to the build it was deployed with.
    So the bundle's own release marker decides, and `current` is used only when
    a tenant has no staged release at all, which is the tenant that has nothing
    (SYRD-249 review).
    """
    staged = Path(role_tooling_staging_dir(project, root=root))
    marker = _read_switchyard_release_marker(staged)
    if marker is None or not marker.marker_commit:
        return None
    candidate = (
        (install_root or switchyard_shared_install_root()) / "releases" / marker.marker_commit
    )
    return candidate if candidate.is_dir() else None


def director_readable_pinned_release(
    project: str, *, root: Path | None = None, install_root: Path | None = None
) -> tuple[Path | None, str, str]:
    """The release root pinned for this tenant, as the director is allowed to see it.

    Returns (release root, commit, "") or (None, "", why not). Root's own record
    of the pin sits in the privileged provision directory, which is 0700 root,
    so `finish-upgrade` -- unprivileged by design -- could not read it, took
    `origin/main` in its place without a word, and failed resolving a branch
    nobody had asked for after the operator had pinned and deployed a release
    (SYRD-255).

    Root already publishes where every role can read it the tenant's staged
    bundle, and the bundle's release marker names the release it was staged
    from. That marker is root's, not the tenant's: every directory to it is
    proven root-owned and unwritable by anybody else before a byte is
    believed, and the release it names is then held to the installed-release
    rules like any operator-named one -- root-controlled, and carrying root's
    marker for exactly this commit.
    """
    staging_override = os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT", "").strip()
    staged = Path(role_tooling_staging_dir(project, root=root))
    marker_path = staged / SWITCHYARD_RELEASE_MARKER_NAME
    from scripts.ticket_board.publication_boundary import root_controlled_problems

    # The documented seam, as for the installed release: the walk starts at "/"
    # on a host and moves only with the staging root's test override, because a
    # fixture cannot own "/".
    base = str(root) if root is not None else staging_override
    problems = root_controlled_problems(
        str(marker_path),
        expect_uid=os.getuid() if base else 0,
        base=base or "/",
    )
    if problems:
        return None, "", f"the staged release marker {marker_path} is not root's: " + "; ".join(problems)
    marker = _read_switchyard_release_marker(staged)
    if marker is None or not marker.marker_commit:
        reason = marker.marker_error if marker is not None else "it is absent"
        return None, "", f"the staged release marker {marker_path} names no release: {reason}"
    commit = marker.marker_commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, "", f"the staged release marker {marker_path} names {commit!r}, not a commit"
    release = (install_root or switchyard_shared_install_root()) / "releases" / commit
    if not release.is_dir():
        return None, "", f"{release}, the release {marker_path} names, is not installed"
    return release, commit, ""


def staged_bundle_launch_problems(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    install_root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
) -> tuple[list[str], list[str], Path]:
    """What an ordinary launch may say about a staged bundle: (absent, hostile, release).

    One policy, asked by both halves of a launch -- the operator's, before it
    crosses to the tenant account, and the owner's, on the way back up -- so
    they cannot answer differently about the same tenant. Everything else a
    full verification reports (an older release marker, a name this release no
    longer carries) is deliberately not here: moving a tenant between releases
    is what `switchyard upgrade` is for.
    """
    pinned = tenant_pinned_release_root(project, root=root, install_root=install_root)
    release = (
        Path(release_root) if release_root
        else pinned or (install_root or switchyard_shared_install_root()) / "current"
    )
    problems = staged_role_tooling_problems(
        project, str(release), staging_root=Path(role_tooling_staging_dir(project, root=root)),
        expect_uid=expect_uid,
    )
    absent = [problem for problem in problems if STAGED_TOOLING_ABSENT_MARKER in problem]
    hostile = [
        problem for problem in problems
        if problem not in absent
        and any(marker in problem for marker in STAGED_TOOLING_HOSTILE_MARKERS)
    ]
    return absent, hostile, release


def ensure_staged_role_bundle_before_crossing(
    project: str,
    *,
    release_root: str = "",
    root: Path | None = None,
    expect_uid: int = STAGED_TOOLING_OWNER_UID,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> str:
    """Stage what a tenant is missing, before root runs anything as its owner.

    The bridge repair before this one covers the two programs whose wire
    contract the crossing itself depends on. That is the right scope for it and
    the wrong scope for a tenant that has NONE of its bundle: `test` was
    provisioned with a staging directory holding only control-grant.json and
    its board unit, so the bridge crossed cleanly and its panes still had
    nothing to run (SYRD-249).

    So absence is repaired here, through the same recorded privileged command
    the helper repair uses -- one journalled step, scoped to this tenant's own
    directory, rendered from the selected release. The caller is the desktop
    operator, who has sudo; the tenant account is never asked to stage anything.

    A bundle that is merely OLDER is left alone, because restaging on drift
    would make every launch privileged (SYRD-211). A bundle that is present and
    wrong -- another account's, writable by others, not a regular file -- is
    refused rather than overwritten, for the same reason the helper repair
    refuses it. Returns "" when the launch may proceed.
    """
    absent, hostile, release = staged_bundle_launch_problems(
        project, release_root=release_root, root=root, expect_uid=expect_uid
    )
    if hostile:
        return f"{project}'s staged tooling is not root's to replace: " + "; ".join(hostile)
    if not absent:
        # The healthy path -- including a complete bundle from an older release
        # -- costs a few stats and no privileged step.
        return ""
    print_func(
        f"switchyard: {project} is missing {len(absent)} of its staged role tooling; "
        f"restaging the bundle from {release} before continuing"
    )
    problem = repair_tenant_control_helper(
        project, release_root=str(release), root=root, runner=runner, print_func=print_func
    )
    if problem:
        return problem
    still_absent, _hostile, _release = staged_bundle_launch_problems(
        project, release_root=str(release), root=root, expect_uid=expect_uid
    )
    if still_absent:
        return (
            f"{project}'s staged role tooling is still incomplete after restaging: "
            + "; ".join(still_absent[:4])
        )
    return ""


def switchyard_recover_display_command(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`switchyard recover-display <project>`: the operator's way back to the Director.

    This verb was advertised by the disconnected Director slot, listed in
    SWITCHYARD_COMMANDS and mapped in the tenant-control bridge -- and never
    dispatched. `switchyard_main` fell through to bare-project selection, so the
    live MEFP UAT got `unknown project 'recover-display mefp'` and nothing was
    recovered (SYRD-239).

    For the desktop operator the work happens on the far side. Loading the
    configuration crosses to the owner the same way `stop` and `status` do: the
    bridge maps this verb to `present <slug> recover director` from root-owned
    data and runs it as the owner, and control does not come back here. So the
    verb carries no authority of its own; it only chooses the route.

    Reached past that line only when no crossing happened -- the caller is the
    owner, or root. Neither is the registered operator the bridge authenticates,
    so the recovery is decided by the presentation controller's own check, and
    a caller it would refuse is told why in terms of this command rather than
    being sent back to run it again.
    """
    from scripts import presentation_controller

    args = _build_switchyard_recover_display_parser().parse_args(list(argv[1:]))
    entry = _resolve_switchyard_project(" ".join(args.project))
    # The slug, not what was typed: the bridge only serves `<verb> <slug>`, so
    # a display name here would silently fall back to sudo.
    config = _load_switchyard_project_config_for_command(entry, ["recover-display", entry.slug])
    env = os.environ if environ is None else environ
    actor = (env.get("TICKET_BOARD_CALLER_ROLE") or env.get("PGU_TICKET_BOARD_CALLER_ROLE") or "").strip().lower()
    if actor != presentation_controller.DIRECTOR_ROLE and not presentation_controller.operator_recovery_caller(
        config, env
    ):
        grant = _tenant_control_grant(config.project)
        registered = grant.get("authorized_user", "")
        route = (
            f"it is registered to {registered}; run this from {registered}'s desktop session"
            if registered
            else f"{config.project} has no tenant-control grant, so no operator is registered for it"
        )
        raise SystemExit(
            f"switchyard: recover-display reaches {config.project}'s Director through its "
            f"tenant-control bridge, as the desktop operator the project is registered to. "
            f"{current_user_name()} did not arrive over that bridge, so nothing authorizes the "
            f"recovery here: {route}"
        )
    present_args = _build_switchyard_present_parser().parse_args(
        [entry.slug, "recover", presentation_controller.DIRECTOR_ROLE]
    )
    return switchyard_present_command(config, config_path=entry.config_path, args=present_args)


def _switchyard_exec_through_tenant_control(
    project: str,
    operation: str,
    *,
    grant: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    exec_func: Callable[[str, Sequence[str]], Any] = os.execvp,
    print_func: Callable[[str], None] = print,
    ensure_helper: Callable[..., None] = ensure_tenant_control_helper,
) -> None:
    """Run one lifecycle verb as the owner, without a password.

    Only the project and the verb cross the boundary. The bridge decides the
    owner, the launcher and whether this caller is allowed, from root-owned
    data, so nothing here can widen what it will do.
    """
    sudo_bin = os.environ.get("SWITCHYARD_SUDO_BIN", "sudo")
    helper = str(TENANT_CONTROL_ROOT / project / "switchyard-tenant-control")
    caller = current_user_name()
    authorized = grant.get("authorized_user", "")
    if caller != authorized:
        # Refused here, so an unauthorized local user never sees a prompt.
        raise SystemExit(
            f"switchyard: {caller} may not control {project}; it is registered to {authorized}\n"
            "switchyard: ask that user, or run this as the project owner or an operator"
        )
    # Before root is asked to run it. A registered tenant whose staging was
    # interrupted -- or which predates staging -- has a valid grant, a valid
    # sudoers rule and no file, and handing that to sudo produced the whole of
    # SYRD-211: `command not found`, before anything else could say why. Absent
    # is repaired from the current release and the launch resumes; any other
    # shape is refused here rather than executed.
    ensure_helper(project, grant=grant, runner=runner, print_func=print_func)
    # And the rest of the bundle, while this process still belongs to somebody
    # with sudo. Past this line the work happens as the tenant owner, which may
    # not stage root's files -- so a tenant missing everything but its grant
    # would otherwise cross successfully and open panes with nothing to run
    # (SYRD-249).
    bundle_problem = ensure_staged_role_bundle_before_crossing(
        project, runner=runner, print_func=print_func
    )
    if bundle_problem:
        raise SystemExit(f"switchyard: {bundle_problem}")
    # Run, not replace. The bridge answers with one validated handoff when the
    # owner half could not do the desktop half, and this process -- which owns
    # the desktop -- is the one that can. Its terminal is passed through
    # untouched, so a verb that attaches a tmux client still has one (SYRD-90).
    result = runner([sudo_bin, "-n", helper, project, operation])
    code = int(getattr(result, "returncode", 1) or 0)
    if code == 0:
        # Symmetric with the open. A stop's desktop half is closing the window
        # this account owns; the owner half cannot see it or signal it
        # (SYRD-202).
        if operation == "stop":
            code = close_desktop_presentation(project, caller=caller)
        elif operation == "recover-display":
            # A recovery reattaches the Director inside the window that is
            # already open; it has no window half to complete. Asking for one
            # would find no handoff and -- on a tenant with desktop access --
            # report the successful recovery as "no presentation window was
            # handed back ... run it again" (SYRD-239 live UAT).
            print_func(f"switchyard: {project}'s Director display was recovered")
        else:
            code = complete_desktop_presentation(project, caller=caller, runner=runner)
    raise SystemExit(code)


def _switchyard_exec_with_root(
    argv: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    exec_func: Callable[[str, Sequence[str]], Any] = os.execvp,
) -> None:
    sudo_bin = os.environ.get("SWITCHYARD_SUDO_BIN", "sudo")
    command_display = _switchyard_command_display(argv)
    target_argv = [sys.argv[0], *argv]
    if runner([sudo_bin, "-n", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        exec_func(sudo_bin, [sudo_bin, "-n", *target_argv])
        raise SystemExit(0)
    if _switchyard_user_can_prompt_for_sudo():
        exec_func(sudo_bin, [sudo_bin, *target_argv])
        raise SystemExit(0)
    raise SystemExit(
        f"switchyard: command requires root: {command_display}\n"
        "switchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator"
    )


def _project_config_path_owner_user(path: Path) -> str:
    expanded = path.expanduser()
    parts = expanded.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home" and parts[2]:
        return parts[2]
    try:
        return pwd.getpwuid(expanded.stat().st_uid).pw_name
    except (KeyError, OSError):
        return ""


def _configured_role_account_caller(config: ProjectConfig) -> str:
    """The configured role this caller's Unix account IS, or empty.

    Bound to the account, never to a role name the caller supplies: the
    configuration says which account belongs to which role, and the board still
    decides authority from the peer uid on its own (SYRD-49).
    """
    caller = current_user_name()
    owner = (config.run_as_user or "").strip()
    for role in config.roles:
        account = (role.run_as_user or "").strip()
        if account and account != owner and account == caller:
            return role.role
    return ""


def _switchyard_command_is_unprivileged(argv: Sequence[str]) -> bool:
    return bool(argv) and argv[0].casefold() in SWITCHYARD_UNPRIVILEGED_COMMANDS


def _switchyard_cross_account(
    project: str,
    argv: Sequence[str],
    *,
    agent_cli_policy: str = "",
    agent_cli_sources: Mapping[str, str] | None = None,
    interactive: bool | None = None,
    which: Callable[..., str | None] = caller_aware_which,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
    promoter: Callable[..., AgentCliAvailability] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ensure_helper: Callable[..., None] | None = None,
) -> None:
    """Reach the owner's account, by the narrowest route that is installed.

    The bridge first: it needs no password and can run only this tenant's
    lifecycle verbs. Sudo remains for everything else, and for tenants that
    have no bridge at all.
    """
    grant = _tenant_control_grant(project)
    if grant:
        operation = _tenant_control_operation(argv, project)
        if operation:
            if operation == "start":
                # Here, and not on the far side. Past the bridge the launcher
                # runs as the owner with a built PATH and cannot see -- let
                # alone promote -- this operator's private copies, so a resumed
                # tenant reported them as per-owner installs the operator was
                # told to repeat. Offered rather than required: nothing is being
                # created, so declining must leave the launch untouched
                # (SYRD-211).
                offer_host_wide_promotion_before_launch(
                    project,
                    policy=agent_cli_policy,
                    sources=agent_cli_sources,
                    interactive=(
                        sys.stdin.isatty() if interactive is None else interactive
                    ),
                    which=which,
                    input_func=input_func,
                    print_func=print_func,
                    promoter=promoter,
                    runner=runner,
                )
            bridge_kwargs: dict[str, Any] = {"runner": runner, "print_func": print_func}
            if ensure_helper is not None:
                bridge_kwargs["ensure_helper"] = ensure_helper
            _switchyard_exec_through_tenant_control(
                project, operation, grant=grant, **bridge_kwargs
            )
    _switchyard_exec_with_root(argv)


def _require_switchyard_owner_hint_or_root(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> None:
    owner = _project_config_path_owner_user(entry.config_path)
    if not owner or current_user_name() == owner or os.geteuid() == 0:
        return
    if _switchyard_command_is_unprivileged(argv) and os.access(entry.config_path, os.R_OK):
        # A role account running its own unprivileged command. It can read the
        # configuration because provisioning granted that account exactly that,
        # and escalating here would hand the command to root -- which the
        # commands that matter then refuse, leaving no way to run them at all
        # (SYRD-49).
        return
    _switchyard_cross_account(entry.slug, argv)


def _require_switchyard_project_owner_or_root(config: ProjectConfig, argv: Sequence[str]) -> None:
    owner = (config.run_as_user or "").strip()
    if not owner or current_user_name() == owner or os.geteuid() == 0:
        return
    if _switchyard_command_is_unprivileged(argv) and _configured_role_account_caller(config):
        return
    _switchyard_cross_account(config.project, argv)


def _load_switchyard_project_config_for_command(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> ProjectConfig:
    _require_switchyard_owner_hint_or_root(entry, argv)
    try:
        # Root's read of this path is the no-follow one, inside the loader.
        config = load_project_config(entry.slug, entry.config_path)
    except PermissionError:
        if os.geteuid() != 0:
            _switchyard_exec_with_root(argv)
        raise
    _require_switchyard_project_owner_or_root(config, argv)
    return config


def report_installed_release_version(
    *,
    root: Path | None = None,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] | None = None,
) -> list[str]:
    """Say when the installed release is older than the checkout it came from.

    Pulling a checkout does not change what `switchyard` runs, because it runs
    from the installed release. The only symptom is a bug the user has already
    been told is fixed, so the conclusion they draw is that it was not -- which
    costs trust rather than time. This reports the mismatch and stops there:
    reinstalling is privileged and is theirs to decide (SYRD-94).

    Nothing here can fail the command it is attached to. A version notice that
    can break the tool is worse than the silence it replaces.
    """
    from scripts.version_notice import release_notice_lines

    emit = print_func or (lambda line: print(line, file=sys.stderr))
    try:
        lines = release_notice_lines(
            (root or _repo_root()),
            environ=dict(os.environ) if environ is None else environ,
            runner=runner,
        )
    except Exception:
        return []
    for line in lines:
        emit(line)
    return lines


def switchyard_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--switchyard-wrapper-requires-root"]:
        print("requires-root" if switchyard_invocation_requires_root(argv[1:]) else "no-root")
        return 0
    # Before the work, so it is read alongside whatever the command says rather
    # than scrolled past after it. `release_notice_lines` is silent unless this
    # process really is running from an installed release whose source checkout
    # is present and ahead of it, so an ordinary checkout run prints nothing.
    report_installed_release_version()
    if not argv:
        return switchyard_menu_command()
    if argv[0] in {"-h", "--help", "help"}:
        print(switchyard_help_text(), end="")
        return 0
    if argv[0] in {"--version", "version"}:
        print(switchyard_version_text())
        return 0
    if argv[0].casefold() == "new":
        args = _build_switchyard_new_parser().parse_args(argv[1:])
        return switchyard_new_command(
            slug=args.slug,
            agent_name=args.agent_name,
            project_name=args.project_name,
            project_path=args.project_path,
            from_artifact=args.from_artifact,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            output_dir=args.output_dir,
            agent_cli_policy=args.agent_cli_policy,
            agent_cli_sources=args.agent_cli_source,
            port=args.port,
            database=args.database,
            yes=args.yes,
            desktop_policy=args.desktop_policy,
            headless=args.headless,
            desktop_gui_user=args.desktop_gui_user,
            allow_existing_owner_user=args.allow_existing_owner_user,
            agy_credential_source=args.agy_credential_source,
            no_agy_credential=args.no_agy_credential,
            layout_mode=args.layout,
            git_init=not args.no_git_init,
        )
    if argv[0].casefold() == "agy-credential":
        parser = argparse.ArgumentParser(prog="switchyard agy-credential")
        parser.add_argument("action", choices=("show", "set", "clear"))
        parser.add_argument("user", nargs="?", help="Unix user to seed agy credentials from (set only)")
        args = parser.parse_args(argv[1:])
        if args.action == "set" and not args.user:
            raise SystemExit("switchyard: agy-credential set requires a user name")
        return switchyard_agy_credential_command(args.action, source_user=args.user)
    if argv[0].casefold() == "seed-role-credentials":
        parser = argparse.ArgumentParser(prog="switchyard seed-role-credentials")
        parser.add_argument("project", help="registered project name or slug")
        parser.add_argument("--role", default="", help="seed only this role")
        parser.add_argument(
            "--reseed",
            action="store_true",
            help="replace credentials a role already has; the deliberate repair path",
        )
        args = parser.parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_seed_role_credentials_command(
            config, role_name=args.role, reseed=args.reseed
        )
    if argv[0].casefold() == "set-owner-identity":
        parser = argparse.ArgumentParser(
            prog="switchyard set-owner-identity",
            description=(
                "Record which of the tenant owner's existing SSH keys this project publishes "
                "with, and select it for the forge. Creates no key and reads no private material."
            ),
        )
        parser.add_argument("project", help="registered project name or slug")
        parser.add_argument(
            "--key-name",
            default="",
            help="file name of an existing key pair in the owner's ~/.ssh, without a path",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help=(
                "for a tenant that does not publish to GitHub: clear a recorded GitHub identity "
                "from both plans and remove Switchyard's managed block"
            ),
        )
        parser.add_argument(
            "--host-alias",
            default="",
            help="an additional Host pattern the managed block should answer to",
        )
        parser.add_argument("--host", default="github.com", help="forge host, default github.com")
        parser.add_argument("--dry-run", action="store_true", help="say what would change")
        args = parser.parse_args(argv[1:])
        if args.clear == bool(args.key_name):
            parser.error("give exactly one of --key-name or --clear")
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        if args.clear:
            return clear_owner_github_identity_command(
                config, config_path=entry.config_path, dry_run=args.dry_run
            )
        return set_owner_github_identity_command(
            config,
            config_path=entry.config_path,
            key_name=args.key_name,
            host_alias=args.host_alias,
            host=args.host,
            dry_run=args.dry_run,
        )
    if argv[0].casefold() == "register":
        args = _build_switchyard_register_parser().parse_args(argv[1:])
        return switchyard_register_command(args.config_path)
    if argv[0].casefold() == "repair-boundary":
        args = _build_switchyard_repair_boundary_parser().parse_args(argv[1:])
        return switchyard_repair_boundary_command(args.project, apply=args.apply)
    if argv[0].casefold() == "approve-desktop":
        args = _build_switchyard_approve_desktop_parser().parse_args(argv[1:])
        return switchyard_approve_desktop_command(
            gui_user=args.gui_user,
            reference=args.reference,
            show=args.show,
            revoke=args.revoke,
        )
    if argv[0].casefold() == "rebind-workflow-panes":
        args = _build_switchyard_rebind_workflow_panes_parser().parse_args(argv[1:])
        runtimes: dict[str, str] = {}
        for item in args.runtime:
            name, sep, value = item.partition("=")
            if not sep or not name.strip() or not value.strip():
                raise SystemExit(f"switchyard: --runtime takes ROLE=RUNTIME, not {item!r}")
            runtimes[name.strip()] = value.strip()
        slots: dict[str, int] = {}
        for item in args.slot:
            name, sep, value = item.partition("=")
            if not sep or not name.strip() or not value.strip().isdigit():
                raise SystemExit(f"switchyard: --slot takes ROLE=SLOT, not {item!r}")
            slots[name.strip()] = int(value.strip())
        return switchyard_rebind_workflow_panes_command(
            args.project, apply=args.apply, expect=args.expect, runtimes=runtimes, slots=slots,
            config_path=args.config_path,
        )
    if argv[0].casefold() == "migrate-workflow":
        args = _build_switchyard_migrate_workflow_parser().parse_args(argv[1:])
        return switchyard_migrate_workflow_command(
            args.project, apply=args.apply, config_path=args.config_path
        )
    if argv[0].casefold() == "adopt-workflow":
        args = _build_switchyard_adopt_workflow_parser().parse_args(argv[1:])
        return switchyard_adopt_workflow_command(
            args.project,
            apply=args.apply,
            despite_board=args.despite_board,
            config_path=args.config_path,
        )
    if argv[0].casefold() == "resume-provision":
        args = _build_switchyard_resume_provision_parser().parse_args(argv[1:])
        return switchyard_resume_provision_command(
            args.project, source_repo=args.source_repo, config_path=args.config_path
        )
    if argv[0].casefold() == "upgrade":
        args = _build_switchyard_upgrade_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return upgrade_project_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
            publish_remote=getattr(args, "publish_remote", ""),
            upstream_report_url=getattr(args, "upstream_report_url", "") or "",
            upstream_report_token_file=getattr(args, "upstream_report_token_file", "") or "",
        )
    if argv[0].casefold() == "install-shared-release":
        args = _build_switchyard_install_shared_release_parser().parse_args(argv[1:])
        return switchyard_install_shared_release_command(
            args.commit, rollback=args.rollback, dry_run=args.dry_run
        )
    if argv[0].casefold() == "privileged-action":
        args = _build_switchyard_privileged_action_parser().parse_args(argv[1:])
        values: dict[str, str] = {}
        for item in args.values:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise SystemExit(f"switchyard: expected key=value, got {item!r}")
            if key in values:
                raise SystemExit(f"switchyard: {key} was given twice")
            values[key] = value
        from scripts.ticket_board import privileged_front_door

        return privileged_front_door.privileged_action_command(
            args.project,
            args.action,
            values,
            dry_run=args.dry_run,
            rollback_commands=lambda project: release_rollback_commands(project),
        )
    if argv[0].casefold() == "rollout-log":
        args = _build_switchyard_rollout_log_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        # The registry entry's slug, which is the tenant's `project` field and
        # so the key the journal is written under. `entry.project` does not
        # exist and crashed every invocation of this command (SYRD-132); the
        # config is deliberately not loaded, because reading a record must keep
        # working for a tenant whose configuration does not.
        return rollout_log_command(entry.slug, attempt=args.attempt, output=args.output)
    if argv[0].casefold() == "publication-status":
        args = _build_switchyard_publication_status_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return publication_status_command(
            config, config_path=entry.config_path, verify=args.verify
        )
    if argv[0].casefold() == "cutover-roles":
        args = _build_switchyard_cutover_roles_parser().parse_args(argv[1:])
        print(
            f"switchyard: cutover-roles is retired for {args.project}; run `switchyard upgrade "
            f"{args.project}` to repatriate resumable state without creating or deleting accounts"
        )
        return 1
    if argv[0].casefold() == "finish-upgrade":
        args = _build_switchyard_finish_upgrade_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return finish_upgrade_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
        )
    if argv[0].casefold() == "release-status":
        args = _build_switchyard_release_status_parser().parse_args(argv[1:])
        return switchyard_release_status_command(args.project, close=args.close)
    if argv[0].casefold() == "worker-pool":
        args = _build_switchyard_worker_pool_parser().parse_args(argv[1:])
        if WORKER_POOL_ACTIONS[args.action] and not args.member.strip():
            raise SystemExit(
                f"switchyard: worker-pool {args.action} needs the worker it acts on, "
                f"e.g. `switchyard worker-pool {args.project} {args.action} <pool>-3`"
            )
        return switchyard_worker_pool_command(
            args.project,
            action=args.action,
            member=args.member.strip(),
            apply_changes=args.apply_changes,
            force=args.force,
            out=args.out,
            journal=args.journal,
        )
    if argv[0].casefold() == "add-role":
        args = _build_switchyard_add_role_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return add_project_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
            cli=args.cli,
            audit_role=args.audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_start,
            script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
        )
    if argv[0].casefold() == "board-skill":
        from scripts import board_skill_cli

        return board_skill_cli.main(argv[1:], prog="switchyard board-skill")
    if argv[0].casefold() == "onboarding-readiness":
        from scripts import onboarding_readiness

        return onboarding_readiness.main(argv[1:])
    if argv[0].casefold() == "role-prompt":
        parser = argparse.ArgumentParser(
            prog="switchyard role-prompt",
            description=(
                "Show, set, or clear the onboarding prompt a role receives when its next "
                "conversation starts fresh. A running conversation is never interrupted or "
                "rewritten: a changed prompt is used by the next fresh session or an "
                "explicit role restart."
            ),
        )
        parser.add_argument("action", choices=("show", "set", "clear"))
        parser.add_argument("role")
        parser.add_argument(
            "--project",
            default=os.environ.get("TICKET_BOARD_PROJECT", ""),
            help="project name or slug; defaults to TICKET_BOARD_PROJECT in the caller's pane",
        )
        parser.add_argument("--prompt", help="prompt text; use --prompt-file for anything long")
        parser.add_argument(
            "--prompt-file",
            type=Path,
            help="read the prompt from a file, or from stdin when given as -",
        )
        args = parser.parse_args(argv[1:])
        if not args.project.strip():
            raise SystemExit(
                "switchyard: no project selected; pass --project or run where "
                "TICKET_BOARD_PROJECT is set"
            )
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        from scripts import workflow_manage

        forwarded = [
            f"{args.action}-role-prompt",
            "--role",
            args.role,
            "--board-url",
            config.board_url,
        ]
        if args.action != "show":
            forwarded += ["--config", str(entry.config_path)]
        if args.prompt is not None:
            forwarded += ["--prompt", args.prompt]
        if args.prompt_file is not None:
            forwarded += ["--prompt-file", str(args.prompt_file)]
        return workflow_manage.main(forwarded)
    if argv[0].casefold() == "set-role-runtime":
        args = _build_switchyard_set_role_runtime_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return set_project_role_runtime_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
            runtime=args.cli,
            model=args.model,
            force=args.force,
            reason=args.reason,
            dry_run=args.dry_run,
        )
    if argv[0].casefold() == "present":
        args = _build_switchyard_present_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_present_command(config, config_path=entry.config_path, args=args)
    if argv[0].casefold() == "attach":
        args = _build_switchyard_attach_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_attach_command(config, args=args)
    if argv[0].casefold() == "replace-window":
        args = _build_switchyard_replace_window_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(" ".join(args.project))
        config = _load_switchyard_project_config_for_command(entry, argv)
        return replace_presentation_window_command(config, config_path=entry.config_path)
    if argv[0].casefold() == "set-vcs-close-role":
        args = _build_switchyard_set_vcs_close_role_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return set_project_vcs_close_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
        )
    if argv[0].casefold() == "recover-display":
        return switchyard_recover_display_command(argv)
    if argv[0].casefold() == "stop":
        args = _build_switchyard_stop_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = _resolve_switchyard_project(project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        # SYRD-193: the public verb is a whole-tenant suspension now -- window,
        # sessions, escaped processes, listener and board -- and it preserves
        # every byte of restart state. The worker-only stop it used to be is
        # still `stop_role_sessions`, which is what the upgrade transaction
        # calls and what must not take a board or a window down.
        problems = suspend_tenant(config, config_path=entry.config_path)
        for problem in problems:
            print(f"switchyard: {problem}")
        if problems:
            print(
                f"switchyard: {config.project} is partially stopped; nothing was removed and "
                f"`switchyard start {config.project}` still resumes what is down."
            )
            return 1
        print(
            f"switchyard: {config.project} is suspended. Its board database, history, worktrees, "
            f"credentials, provider state and session records are untouched; "
            f"`switchyard start {config.project}` brings it back."
        )
        return 0
    if argv[0].casefold() == "start":
        args = _build_switchyard_start_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = _resolve_switchyard_project(project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        problems = resume_tenant(config, config_path=entry.config_path)
        for problem in problems:
            print(f"switchyard: {problem}")
        if problems:
            return 1
        config = prepare_project_desktop(config)
        return launch_project(
            config,
            config_path=entry.config_path,
            mode="start",
            script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
            report_session_records=True,
        )
    if argv[0].casefold() == "teardown":
        args = _build_switchyard_teardown_parser().parse_args(argv[1:])
        return switchyard_teardown_command(
            args.project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if argv[0].casefold() == "status":
        args = _build_switchyard_status_parser().parse_args(argv[1:])
        selection = " ".join(args.project)
        if selection:
            # One project needs no root: its own owner can answer for it. That
            # matters beyond tidiness -- crossing accounts then uses the same
            # policy as every other project command, which is what lets the
            # recorded human reach it over the control bridge without a
            # password. The unscoped listing still reads every tenant, so it
            # still takes the root path (SYRD-50 rollout review).
            # Resolved, not loaded: loading the tenant's configuration here
            # would cross to its owner -- or re-exec under root when the file
            # cannot be read -- for a command that only reports. The status
            # itself reads what this account may read and says what it may not
            # (SYRD-241).
            entry = _resolve_switchyard_project(selection)
            return switchyard_status_command(json_output=args.json, project=entry.slug)
        return switchyard_status_command(json_output=args.json)
    if argv[0].casefold() == "validate-models":
        if len(argv) < 2:
            raise SystemExit("switchyard validate-models requires <project>")
        project = " ".join(argv[1:])
        entry = _resolve_switchyard_project(project)
        _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_validate_models_command(project)
    selection = " ".join(argv)
    entry = _resolve_switchyard_project(selection)
    config = _load_switchyard_project_config_for_command(entry, argv)
    # Model validation intentionally runs only for `switchyard new` and the
    # explicit validate-models command. It performs provider API calls, so a
    # routine team start should not depend on provider availability.
    # SYRD-193: a suspended tenant is recovered by the ordinary launch, not only
    # by the explicit `start` verb. The bridge maps its `start` operation onto
    # this bare path, so a resumption that only lived in the named verb would be
    # unreachable through the very route a desktop user takes. Units already
    # active are left alone, so a running tenant is untouched by this.
    for problem in resume_tenant(config, config_path=entry.config_path):
        print(f"switchyard: {problem}")
        return 1
    config = prepare_project_desktop(config)
    first_run_auth_report = run_switchyard_launch_first_run_auth(config)
    if stop_before_launch_for_missing_owner_clis(first_run_auth_report):
        return 1
    if stop_before_launch_for_unauthenticated_providers(first_run_auth_report):
        return 1
    if stop_before_launch_for_unknown_models(first_run_auth_report, project=config.project):
        return 1
    launch_result = launch_project(
        config,
        config_path=entry.config_path,
        mode="start",
        script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
        report_session_records=True,
    )
    if launch_result != 0:
        return launch_result
    report_first_run_auth_warnings(first_run_auth_report)
    return 0


def _reject_removed_commands(argv: Sequence[str]) -> None:
    if len(argv) > 1 and str(argv[1]).strip() in REMOVED_CONFIG_FREE_COMMANDS:
        project = str(argv[0]).strip() or "<project>"
        replacement = BOOTSTRAP_REPLACEMENT_COMMAND.replace("<project>", project)
        raise SystemExit(
            f"team-launcher: {argv[1]} has been removed; use `{replacement}` to create a launchable project config"
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _reject_removed_commands(argv)
    args = _build_parser().parse_args(argv)
    if args.command == "design":
        return design_project_command(
            args.project,
            output_dir=args.design_output_dir,
            artifact_path=args.project_artifact,
            design_document=args.design_document,
            design_title=args.design_title,
            design_body=args.design_body,
            repository=args.repository,
            remote=args.remote,
            default_branch=args.default_branch,
            worktree_policy=args.worktree_policy,
            owner_user=args.owner_user,
            ticket_prefix=args.ticket_prefix,
            push_policy=args.push_policy,
            audit_signoff=args.audit_signoff,
            needs_inspection=args.needs_inspection,
            needs_user_signoff=args.needs_user_signoff,
            board_service_traversal=args.board_service_traversal,
            supplementary_groups=args.supplementary_groups,
            linger=args.linger,
            owner_shell=args.owner_shell,
        )
    if args.command == "new":
        return new_project_command(
            args.project,
            from_artifact=args.from_artifact,
            owner_user=args.owner_user,
            desktop_policy=args.desktop_policy,
            port=args.port,
            database=args.database,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            repository=args.repository,
            output_dir=args.new_output_dir,
            execute=args.execute,
            dry_run=args.dry_run,
            upstream_report_url=args.upstream_report_url or "",
            upstream_report_token_file=args.upstream_report_token_file or "",
        )
    if args.command == "provision-runtime" and args.config is None:
        try:
            resolved = _resolve_launcher_project_config(args.project)
            config_path = resolved.config_path
            config_project = resolved.slug
        except SystemExit:
            config_path = DEFAULT_CONFIG_DIR / f"{args.project}.json"
            config_project = args.project
    else:
        resolved = _resolve_launcher_project_config(args.project, explicit_config=args.config)
        config_path = resolved.config_path
        config_project = resolved.slug
    if args.command == "provision-runtime":
        config = load_project_config(config_project, config_path) if config_path.exists() else None
        return provision_runtime_command(args.runtime_user, config)
    config = load_project_config(config_project, config_path)
    if args.command == "upgrade":
        return upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
            publish_remote=getattr(args, "publish_remote", ""),
            upstream_report_url=getattr(args, "upstream_report_url", "") or "",
            upstream_report_token_file=getattr(args, "upstream_report_token_file", "") or "",
        )
    if args.command == "add-role":
        if not args.pane_mode or args.role:
            raise SystemExit("add-role requires exactly one <role> argument")
        return add_project_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            cli=args.add_role_cli,
            audit_role=args.add_role_audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_attach,
            script_path=args.script_path,
            pane_state_dir=args.pane_state_dir,
        )
    if args.command == "set-vcs-close-role":
        if not args.pane_mode or args.role:
            raise SystemExit("set-vcs-close-role requires exactly one <role> argument")
        return set_project_vcs_close_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            runner=subprocess.run,
        )
    if args.command == "stop":
        # The session-level stop, deliberately: this is the lower-level entry
        # point, and `switchyard stop` is the tenant suspension that also takes
        # the board, the listener and the window (SYRD-193).
        return stop_project(config)
    if args.command == "teardown":
        return switchyard_teardown_command(
            config_project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if args.command == "deploy-launcher":
        return deploy_launcher_checkout(
            config,
            launcher_repo=args.launcher_repo,
            clean=args.clean_launcher,
        )
    if args.command != "pane" and (args.pane_mode or args.role):
        raise SystemExit(f"{args.command} does not accept extra pane arguments")
    if args.command == "pane":
        if not args.pane_mode or not args.role:
            raise SystemExit("pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>")
        if args.pane_mode not in {"start", "attach", "attach-or-start", "reload", "attach-role", "detach-role"}:
            raise SystemExit(f"unknown pane mode: {args.pane_mode}")
        if args.pane_mode not in {"attach", "detach-role"}:
            config = prepare_project_desktop(config)
        role = _role_by_name(config, args.role)
        # Every lifecycle path for this role runs as the role's own account, so
        # its tmux server, pane processes and board writes all carry that uid.
        # Re-execing as the shared project owner here is what previously undid
        # the per-role accounts the layout had already selected (SYRD-39).
        pane_user = role_run_as_user(config, role)
        pane_state_dir = args.pane_state_dir or default_pane_state_dir_for_user(pane_user, project=config.project)
        if pane_user and current_user_name() != pane_user:
            return subprocess.run(
                pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=args.pane_mode,
                    script_path=args.script_path,
                    slot=args.slot,
                    pane_state_dir=pane_state_dir,
                    force_reload=args.force,
                    skip_launcher_check=args.skip_launcher_check,
                    allow_stale_launcher=args.allow_stale_launcher,
                    no_attach=args.no_attach,
                    run_as_user=pane_user,
                )
            ).returncode
        if not args.skip_launcher_check:
            ensure_launcher_checkout_current(
                config,
                runner=subprocess.run,
                auto_deploy=False,
                allow_stale=args.allow_stale_launcher,
            )
        ensure_configured_runtime_user(config)
        seed_default_session_dir_from_legacy_sources(config.session_dir)
        if args.pane_mode == "attach-role":
            if args.slot is None:
                raise SystemExit("pane attach-role requires --slot")
            return attach_role_to_slot(
                config,
                config_path=config_path,
                role_name=role.role,
                slot=args.slot,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
            )
        if args.pane_mode == "detach-role":
            return detach_role_from_slot(
                config,
                config_path=config_path,
                role_name=role.role,
            )
        if args.no_attach and not role.detached:
            return ensure_visible_role_session_for_viewer(
                role,
                mode=args.pane_mode,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        if role.detached:
            return run_detached_role(
                role,
                mode=args.pane_mode,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        return run_role_pane(
            role,
            mode=args.pane_mode,
            session_dir=role_session_dir(config, role),
            pane_state_dir=pane_state_dir,
            force_reload=args.force,
            bin_user=pane_user,
        )
    return launch_project(
        config,
        config_path=config_path,
        mode=args.command,
        script_path=args.script_path,
        dry_run=args.dry_run,
        layout_output=args.layout_output,
        pane_state_dir=args.pane_state_dir,
        force_reload=args.force,
        allow_stale_launcher=args.allow_stale_launcher,
        no_launcher_self_deploy=args.no_launcher_self_deploy,
        report_session_records=True,
        layout_mode=args.layout,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
