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
# `switchyard deploy-release` (SYRD-531): the catalogued `deploy-release`
# action's command, dispatched from `switchyard_main` below.
from scripts.tenant_release_deploy import (
    _build_switchyard_deploy_release_parser,
    switchyard_deploy_release_command,
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
from scripts.release_alignment import (
    ReleaseAlignment,
    _current_tenant_release,
    _live_board_build_id,
    _read_deploy_sha_marker,
    director_release_divergence_report,
    release_alignment,
)
from scripts.release_rollback import (
    RELEASE_ROLLBACK_SCHEMA,
    _write_publication_remote,
    record_publication_remote,
    record_release_rollback,
    release_rollback_commands,
    release_rollback_path,
    restore_publication_remote,
)
from scripts.tenant_release_report import (
    OWNER_BOUNDARY_SCRIPT,
    TenantReleaseStatus,
    _format_release_path,
    _owner_boundary_env_args,
    capture_release_pointer,
    deploy_release_in_transaction,
    recorded_rollout_command,
    release_update_blocked,
    report_tenant_release_upgrade,
    restore_release_pointer,
    tenant_release_deploy_command,
    tenant_release_listener_command,
    tenant_release_unit_install_command,
)
from scripts.tenant_release_root import (
    RELEASE_ROOT_WRITABLE_DIRS,
    RELEASE_ROOT_WRITABLE_FILES,
    _open_release_root_entries,
    owner_release_root_problems,
    prepare_tenant_release_root,
    release_root_repair_record_path,
)
from scripts.trusted_owner_identity import (
    TrustedOwnerIdentity,
    trusted_owner_identity,
)
from scripts.no_follow_records import (
    _directory_owner_no_follow,
    _walk_no_follow,
    expected_privileged_uid,
    read_plan_no_follow,
    read_tenant_document_no_follow,
    root_controlled_problems_for,
)
from scripts.privileged_provision_records import (
    PRIVILEGED_ARTIFACT_MODE,
    PRIVILEGED_EXECUTABLE_ARTIFACT_MODE,
    PRIVILEGED_PROVISION_DIR_MODE,
    ensure_privileged_provision_dir,
    privileged_artifact_mode,
    privileged_baseline_plan_path,
    recorded_declared_workflow,
    switchyard_privileged_provision_root,
    workflow_record_path,
)
from scripts.privileged_artifacts import (
    install_privileged_artifacts,
    render_privileged_artifacts,
)
from scripts.pending_identity_records import (
    PENDING_IDENTITIES_SCHEMA,
    pending_identities_path,
    pending_identity_for,
    read_pending_identities,
    write_pending_identities,
)
from scripts.tenant_artifact_publish import (
    publish_tenant_artifact,
)
from scripts.polkit_readiness import (
    POLKIT_ANSWERED_EXIT_CODES,
    POLKIT_QUERY_TIMEOUT_SECONDS,
    POLKIT_RESTART_COMMAND,
    POLKIT_RULES_DIR,
    _apt_archive_has,
    polkit_install_command,
    polkit_readiness_problems,
    polkit_service_problem,
)
from scripts.tenant_control_helper import (
    PROTOCOL_STAGED_EXECUTABLES,
    TENANT_CONTROL_OPERATIONS,
    TENANT_CONTROL_OWNER_UID,
    TENANT_CONTROL_REPAIR_LABEL,
    TENANT_CONTROL_ROOT,
    TenantControlHelperState,
    _tenant_control_can_serve,
    _tenant_control_grant,
    _tenant_control_operation,
    ensure_tenant_control_helper,
    repair_tenant_control_helper,
    staged_protocol_states,
    staged_tooling_out_of_date,
    tenant_control_helper_state,
    tenant_control_repair_command,
)
from scripts.role_state_ownership import (
    STATE_TREE_MAX_DEPTH,
    _close_quietly,
    _open_tenant_state_root,
    _root_placed_link,
    _uid_owner_name,
    _walk_tenant_state_tree,
    repair_role_state_ownership,
    role_state_ownership_problems,
    role_state_roots,
)
from scripts.tenant_publication_boundary import (
    install_tenant_publication_boundary,
    remove_tenant_publication_boundary,
)
from scripts.trusted_bootstrap import (
    INSTALL_BOUNDARY_ROLLOUT_LABEL,
    INSTALL_ROLLOUT_LABEL,
    install_boundary_command,
    installed_rollout_recorder,
    recorded_install_command,
    stale_launcher_problems,
    trusted_bootstrap_commands,
)
from scripts.trusted_upgrade_release import (
    _recovered_pin_behind_host,
    _selected_release_commit,
    resolve_trusted_upgrade_release,
)
from scripts.workflow_presence import (
    DeclaredWorkflowPresence,
    NON_DECLARATIVE_WORKFLOW_SEED,
    declared_workflow_presence,
)
from scripts.staged_role_tooling import (
    STAGED_TOOLING_OWNER_UID,
    _staged_tooling_dir,
    ensure_staged_role_tooling,
    refresh_staged_role_tooling,
)
from scripts.host_boundary_install import (
    install_host_privileged_boundary,
    switchyard_install_shared_release_command,
)
from scripts.board_authority_install import (
    install_board_authority,
    install_board_authority_files,
)
from scripts.residual_processes import (
    ResidualProcess,
    _process_ancestry,
    _process_environ,
    contain_residual_project_processes,
    process_systemd_unit,
    residual_project_processes,
)
from scripts.tenant_runtime import (
    TENANT_RUNTIME_STATES,
    TenantRuntime,
    describe_tenant_runtime,
)
from scripts.tenant_suspension import (
    resume_tenant,
    suspend_tenant,
)
from scripts.role_visibility import (
    _raw_role_for_update,
    _write_role_visibility,
    detach_role_from_slot,
    tmux_detach_clients_args,
)
from scripts.project_stop import (
    stop_project,
)
from scripts.finish_upgrade_preview import (
    _finish_upgrade_preview,
)
from scripts.pane_pid import (
    pane_pid_for_role,
    tmux_pane_pid_args,
)
from scripts.publication_status import (
    _build_switchyard_publication_status_parser,
    _public_key_fingerprint,
    publication_status_command,
)
from scripts.switchyard_parsers import (
    _build_switchyard_add_role_parser,
    _build_switchyard_approve_desktop_parser,
    _build_switchyard_attach_parser,
    _build_switchyard_cutover_roles_parser,
    _build_switchyard_finish_upgrade_parser,
    _build_switchyard_install_shared_release_parser,
    _build_switchyard_new_parser,
    _build_switchyard_present_parser,
    _build_switchyard_privileged_action_parser,
    _build_switchyard_recover_display_parser,
    _build_switchyard_register_parser,
    _build_switchyard_repair_boundary_parser,
    _build_switchyard_replace_window_parser,
    _build_switchyard_resume_provision_parser,
    _build_switchyard_rollout_log_parser,
    _build_switchyard_set_role_runtime_parser,
    _build_switchyard_set_vcs_close_role_parser,
    _build_switchyard_start_parser,
    _build_switchyard_stop_parser,
    _build_switchyard_teardown_parser,
    _build_switchyard_upgrade_parser,
)
from scripts.presentation_commands import (
    switchyard_attach_command,
    switchyard_present_command,
    switchyard_recover_display_command,
)
from scripts.command_crossing import (
    _configured_role_account_caller,
    _load_switchyard_project_config_for_command,
    _require_switchyard_owner_hint_or_root,
    _require_switchyard_project_owner_or_root,
    _switchyard_command_display,
    _switchyard_command_is_unprivileged,
    _switchyard_cross_account,
    _switchyard_exec_through_tenant_control,
    _switchyard_exec_with_root,
    _switchyard_user_can_prompt_for_sudo,
    ensure_staged_role_bundle_before_crossing,
)
from scripts.staged_launch_checks import (
    STAGED_TOOLING_ABSENT_MARKER,
    STAGED_TOOLING_HOSTILE_MARKERS,
    director_readable_pinned_release,
    staged_bundle_launch_problems,
    tenant_pinned_release_root,
)
from scripts.shared_release import (
    DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT,
    SWITCHYARD_NAME,
    SWITCHYARD_RELEASE_MARKER_NAME,
    SWITCHYARD_VERSION,
    SharedSwitchyardRelease,
    _read_switchyard_release_marker,
    report_installed_release_version,
    running_launcher_release,
    shared_switchyard_release_for_path,
    switchyard_shared_install_root,
    switchyard_shared_pane_launcher,
    switchyard_shared_target,
    switchyard_version_text,
)
from scripts.generated_layout_upgrade import (
    _generated_project_durable_session_dir,
    upgrade_generated_project_config,
    upgrade_generated_project_layout,
)
from scripts.new_project_artifacts import (
    _dedupe_role_defs,
    _director_seed_project_dir,
    _new_project_control_repository,
    _new_project_launcher_config_payload,
    _new_project_role_env,
    write_new_project_launcher_artifacts,
)
from scripts.project_design_artifact import (
    AGY_SOURCE_ORIGINS,
    PROJECT_DESIGN_FORBIDDEN_KEYS,
    _artifact_audit_role_list,
    _artifact_bool_mapping,
    _artifact_capability_grants,
    _artifact_forbidden_keys,
    _artifact_optional_string,
    _artifact_role_cli_pairs,
    _artifact_role_list,
    _artifact_role_value_pairs,
    _artifact_string,
    load_project_design_artifact,
)
from scripts.new_project_precheck import (
    POSTGRES_ADMIN_SOCKET_DIR,
    POSTGRES_SERVICE_UNIT,
    _database_exists,
    _installed_unit_is_this_plans,
    _looks_like_switchyard_release_tree,
    _path_exists,
    _precheck_deploy_source,
    _switchyard_release_source_error,
    _system_unit_file_exists,
    _tcp_port_in_use,
    _ticket_board_table_count,
    postgres_availability_remedy,
    postgres_cluster_script,
    precheck_new_project,
)
from scripts.role_plan_prompt import (
    NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES,
    NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES,
    _implementer_roles_field,
    _owner_account_exists,
    _prompt_role_runtime_plan,
    _prompt_switchyard_role_choices,
    _prompt_switchyard_role_plan,
)
from scripts.project_design_command import (
    _comma_list,
    _default_project_artifact_path,
    _default_project_design_document_path,
    design_project_command,
)
from scripts.project_upgrade_command import (
    upgrade_project_command,
)
from scripts.switchyard_new_command import (
    switchyard_new_command,
)
from scripts.new_project_command import (
    _new_project_artifact_dir,
    new_project_command,
    recorded_provisioning_command,
)
from scripts.launcher_parser import (
    _build_parser,
)
from scripts.project_launch import (
    launch_project,
)
from scripts.project_config_json import (
    _bool_value,
    _plan_migration_reference,
    _project_board_provision_from_json,
    _recorded_owner_home,
    _role_board_env,
    _role_from_json,
    _string_list,
    _with_project_board_env,
    role_git_template_env,
)
from scripts.switchyard_commands import (
    SWITCHYARD_COMMANDS,
    SWITCHYARD_PRIVILEGED_COMMANDS,
    SWITCHYARD_UNPRIVILEGED_COMMANDS,
    switchyard_help_text,
    switchyard_invocation_requires_root,
)
from scripts.switchyard_registration import (
    _check_switchyard_registration_available,
    _register_switchyard_project,
    _registered_project_collision,
    switchyard_register_command,
)
from scripts.legacy_layouts import (
    _known_generated_project_layout_payloads,
    _legacy_new_project_chunked_row_major_layout_payload,
    _legacy_new_project_column_major_layout_payload,
    _legacy_new_project_sqrt_column_major_layout_payload,
    _legacy_new_project_stacked_layout_payload,
)
from scripts.project_layouts import (
    NEW_PROJECT_GRID_PANES_PER_ROW,
    NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES,
    _new_project_layout_leaves,
    _new_project_layout_payload,
    _row_major_grid_layout_payload,
    _single_row_layout_payload,
)
from scripts.board_workflow_readers import (
    read_board_declared_workflow,
    read_board_workflow_state,
)
from scripts.runtime_user_provisioning import (
    RUNTIME_READY_ATTEMPTS,
    RUNTIME_READY_POLL_SECONDS,
    ensure_configured_runtime_user,
    ensure_user_linger_runtime,
    loginctl_enable_linger_args,
    provision_runtime_command,
)
from scripts.rollout_log import (
    rollout_log_command,
)
from scripts.control_role import (
    CONTROL_ROLE_CAPABILITIES,
    control_role_name,
    director_role_name,
)
from scripts.process_inspection import (
    _list_process_command_lines,
    _owner_process_runner,
    _process_snapshot,
    _role_has_pane_process,
    process_tree_command_names,
    process_uid,
    role_process_runner_for,
)
from scripts.project_design_payload import (
    ProjectDesignArtifact,
    _project_design_markdown,
    _role_cli_map,
    project_design_artifact_payload,
)
from scripts.launch_owner_clis import (
    _format_missing_cli_launch_failure,
    run_switchyard_launch_first_run_auth,
    stop_before_launch_for_missing_owner_clis,
)
from scripts.cli_install_instructions import (
    _missing_cli_install_clause,
    host_wide_install_instruction,
)
from scripts.role_state_restore import (
    restore_interrupted_role_state,
)
from scripts.policy_hook_repair import (
    repair_repository_policy_hooks,
)
from scripts.project_config_loader import (
    load_project_config,
)
from scripts.launcher_dispatch import (
    _reject_removed_commands,
    main,
)
from scripts.switchyard_dispatch import (
    switchyard_main,
)
from scripts.project_resolution import (
    _project_entries,
    _project_name_selector_slugs,
    _registry_project_entries,
    _resolve_switchyard_project,
    _resume_provision_hint,
    _switchyard_entries,
    partial_provision_record,
)
from scripts.project_identity import (
    ProjectConfig,
    RoleConfig,
    SwitchyardProjectEntry,
    _legacy_dash_slug_from_project_name,
    _slug_from_project_name,
    _validate_project_slug,
    switchyard_registry_dir,
)
from scripts.atomic_files import (
    _ensure_private_dir,
    _write_json_atomic,
    _write_private_json_atomic,
)
from scripts.owner_commands import (
    _owner_command_args,
    _owner_command_env_args,
    _owner_user_systemctl,
    _tenant_owner_home,
)
from scripts.env_composition import (
    TERMINAL_PRESENTATION_ENV_KEYS,
    _env_prefix,
    _env_unset_prefix,
    _owner_home_bin_dirs,
    _pane_identity_scrubbed_env,
    _prepend_path,
    _prepend_paths,
    _terminal_presentation_env,
)
from scripts.owner_files import (
    _chown_project_file,
    _path_owner_ids,
    _path_owner_label,
    chown_owner_file_args,
    ensure_owner_file,
)
from scripts.new_project_selection import (
    NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES,
    NEW_PROJECT_RESERVED_ROLE_NAMES,
    NEW_PROJECT_ROLE_CLI_DEFAULTS,
    RoleSelection,
    SUPPORTED_NEW_PROJECT_CLIS,
    SWITCHYARD_PROMPT_MAX_ATTEMPTS,
    _dedupe_role_names,
    _default_new_project_owner,
    _default_role_cli_pairs,
    _prompt_bool,
    _prompt_cli,
    _prompt_text,
    _read_prompt,
    _runtime_choices,
    _runtime_field,
    _validate_new_project_audit_role,
    _validate_new_project_cli,
    _validate_new_project_implementer_role,
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


SWITCHYARD_REGISTRY_SCHEMA = "switchyard.project-registry.v1"
#: The agent CLIs a registered tenant's roles are configured with. Optional, so
#: a record written before it existed still reads; absent means "not recorded",
#: never "none" (SYRD-220).
SWITCHYARD_REGISTRY_AGENT_CLIS_KEY = "agent_clis"
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
WORKTREE_POLICIES = frozenset({"shared", "isolated"})
SWITCHYARD_PROJECT_DIR_NAME = ".switchyard"
DEFAULT_PANE_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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
NO_LAUNCHER_SELF_DEPLOY_ENV = "TEAM_LAUNCHER_NO_SELF_DEPLOY"
LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV = "PGU_TEAM_LAUNCHER_NO_SELF_DEPLOY"
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
# A credential-source user name is joined onto a home base to locate a token, so it is
# constrained to a plain useradd-style name: no separators, no traversal.
OWNER_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class LauncherUpgradeResult:
    changed: bool
    message: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def _normalized_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _quote_command(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


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


def _is_generated_project_layout_template(config: ProjectConfig, *, config_path: Path) -> bool:
    config_file = config_path.expanduser().resolve(strict=False)
    layout_path = config.layout.expanduser().resolve(strict=False)
    provision_dir = config_file.parent
    return (
        provision_dir.name == "provision"
        and layout_path.parent == provision_dir
        and layout_path.name == f"{config.project}-konsole-layout.json"
    )


PRIVILEGED_PROVISION_ROOT_ENV = "SWITCHYARD_PRIVILEGED_PROVISION_ROOT"


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


def _rollout_recorder_path() -> Path | None:
    """The recorder root should execute, or None when there is not one to run."""
    installed = switchyard_shared_install_root() / "current" / "scripts" / "switchyard-record-rollout"
    if installed.is_file():
        return installed
    checkout = _repo_root() / "scripts" / "switchyard-record-rollout"
    return checkout if checkout.is_file() else None


def _format_release_sha(sha: str) -> str:
    return sha if sha else "(none)"


def _privileged_upgrade_check_command(project: str, deploy_ref: str | None) -> str:
    """The boundary command that runs this upgrade's checks as root, read-only."""
    commit = deploy_ref if deploy_ref and re.fullmatch(r"[0-9a-f]{40}", deploy_ref) else "<release commit>"
    return f"switchyard privileged-action {project} preview-upgrade commit={commit}"


def _new_project_session_dir(project: str, owner_user: str) -> str:
    owner_home = home_dir_for_user(owner_user) or Path("/home") / owner_user
    return str(owner_home / ".local" / "state" / f"{project}-ticket-board" / "pane-sessions")


def _new_project_worktree_base(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / f"{project}-worktrees"


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
class PlanDocument:
    """One plan authority, opened without following anything."""

    path: Path
    data: dict[str, Any]
    raw: bytes
    uid: int
    gid: int
    mode: int


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


def _owner_git_args(owner_user: str, project_dir: Path, *git_args: str) -> list[str]:
    return ["sudo", "-u", owner_user, "git", "-C", str(project_dir), *git_args]


def _control_repository_owned_roots(config: ProjectConfig) -> list[Path]:
    owned_roots: list[Path] = []
    if config.control_repository is not None:
        owned_roots.extend([config.control_repository.parent, config.control_repository])
    if config.worktree_base is not None:
        owned_roots.append(config.worktree_base)
    if config.pane_launcher is not None:
        owned_roots.extend([config.pane_launcher.parent, config.pane_launcher])
    return owned_roots


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


def _open_board_url(url: str) -> Any:
    import urllib.request

    return urllib.request.urlopen(url, timeout=5)


def _role_accounts_ready(config: ProjectConfig) -> bool:
    """Compatibility predicate: SYRD-69 requires only the project account."""
    return True


# Where systemd reads units from. A module attribute so the transaction can be
# exercised against a real directory in a test rather than against a stub of the
# install itself (SYRD-45).
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")


def _installed_unit_path(unit: str) -> Path:
    return SYSTEMD_UNIT_DIR / unit


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


def _layout_slot_count(config: ProjectConfig) -> int:
    try:
        layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot read layout {config.layout}: {exc}") from exc
    return len(_layout_leaves(layout))


def _role_by_name(config: ProjectConfig, role_name: str) -> RoleConfig:
    for role in config.roles:
        if role.role == role_name:
            return role
    raise SystemExit(f"unknown role {role_name!r} in project {config.project}")


def _owner_catalog_args(config: "ProjectConfig") -> tuple[str, tuple[str, ...]]:
    """The account whose model list decides, and the argv prefix to ask it."""
    owner_user = str(getattr(config, "run_as_user", "") or "")
    if not owner_user:
        return "", ()
    owner_home = _owner_home_for_auth(owner_user)
    return owner_user, tuple(_owner_command_env_args(owner_user, owner_home, []))


def _project_config_path_owner_user(path: Path) -> str:
    expanded = path.expanduser()
    parts = expanded.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home" and parts[2]:
        return parts[2]
    try:
        return pwd.getpwuid(expanded.stat().st_uid).pw_name
    except (KeyError, OSError):
        return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
