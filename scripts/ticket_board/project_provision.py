"""Render per-project ticket-board provisioning artifacts."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import grp
import os
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import MISSING, asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

try:
    from . import privileged_install
    from .board_skill import RELEASE_MARKER_NAME, SKILLS_DIR_NAME
    from .commit_repos import commit_git_dir_env_for_project
    from .provision_github_identity import (
        DEFAULT_GITHUB_HOST,
        GITHUB_IDENTITY_BEGIN,
        GITHUB_IDENTITY_END,
        OwnerGithubIdentity,
        compose_ssh_config,
        existing_owner_ssh_key_names,
        github_identity_block,
        owner_github_block_removal_commands,
        owner_github_identity_commands,
        owner_github_key_path,
        owner_github_selection_commands,
        parse_managed_github_identity,
        publication_remote_host,
        publication_uses_github,
        resolve_owner_github_identity,
    )
    from .provision_path_confinement import (
        INHERITED_WORKTREE_CLOSURE,
        INTERIOR_DIRECTORY_MODE,
        REPOSITORY_COPY_MODE,
        TENANT_SOURCE_MODE,
        _interior_directories,
        _is_within,
        _refuse_prefix_coincidence,
        commit_store_read_commands,
        director_control_access_commands,
        owned_ancestor_dirs,
        owner_home_traversal_commands,
        repository_copy_confinement_commands,
        role_worktree_access_commands,
        socket_group_retirement_commands,
        tenant_source_confinement_commands,
        tenant_worktree_confinement_commands,
    )
    from .provision_workflow_projection import (
        SCHEMA_SQL_PATH,
        TENANT_WORKFLOW_EXCLUDED_ACTIONS,
        TENANT_WORKFLOW_EXCLUDED_STAGES,
        WorkflowStageSeed,
        WorkflowTransitionSeed,
        _insert_values_block,
        _parse_sql_bool,
        _parse_sql_nullable_string,
        _parse_sql_string,
        _parse_sql_text_array,
        _project_implementation_owner_roles,
        _project_workflow_allowed_roles,
        _project_workflow_owner_roles,
        _rank_project_stages,
        _schema_sql_text,
        _split_sql_fields,
        _split_sql_tuple_rows,
        project_workflow_stages,
        project_workflow_state_names,
        project_workflow_transitions,
        schema_workflow_stages,
        schema_workflow_transitions,
    )
    from .provision_workflow_sql import (
        _workflow_stage_rows_sql,
        _workflow_transition_rows_sql,
        render_add_role_sql,
        render_project_role_constraint_sql,
        render_vcs_close_role_sql,
        render_workflow_sql,
    )
    from .provision_role_tooling import (
        GIT_TEMPLATE_DIR_NAME,
        RETIRED_STAGED_EXECUTABLES,
        ROLE_STAGED_EXECUTABLES,
        TENANT_CONTROL_ROOT_ENV,
        _imported_names,
        _release_marker_commit,
        _script_sibling_modules,
        entry_point_module_dependencies,
        git_template_files,
        git_template_staging_commands,
        readable_system_unit_path,
        role_tooling_staging_commands,
        role_tooling_staging_dir,
        staged_role_tooling_problems,
        system_unit_proof_chain,
        system_unit_proof_commands,
        tenant_control_root,
    )
    from .provision_publication_grants import (
        DEFAULT_PUBLISH_GRANT_ROOT,
        DEFAULT_PUBLISH_STAGING_ROOT,
        PUBLISH_GRANT_ROOT,
        PUBLISH_GRANT_SCHEMA,
        PUBLISH_STAGING_ROOT,
        publish_grant_commands,
        publish_grant_path,
        publish_grant_root,
        publish_identity_path,
        publish_staging_root,
        publish_sudoers_document,
        publish_sudoers_path,
    )
    from .plan_role_derivation import PLAN_DERIVED_ROLE_FIELDS, role_reference, unresolved_plan_field_reason
    from .provision_tenant_control import (
        TENANT_CONTROL_GRANT_NAME,
        display_attach_helper_path,
        invoking_human,
        resolve_control_user,
        tenant_control_commands,
        tenant_control_grant,
        tenant_control_grant_document,
        tenant_control_grant_name,
        tenant_control_grant_path,
        tenant_control_helper_path,
        tenant_control_sudoers,
        tenant_control_sudoers_document,
        tenant_control_sudoers_path,
    )
    from .provision_role_accounts import (
        NON_PROCESS_ROLES,
        ROLE_ACCOUNT_MIGRATION_SUFFIX,
        ROLE_CONTROL_SUDOERS_HEREDOC,
        render_role_control_sudoers,
        role_account_commands,
        role_account_home,
        role_account_migration_name,
        role_account_name,
        role_account_table,
        role_accounts_command,
        role_accounts_env,
        role_control_sudoers,
        role_control_sudoers_install_commands,
        role_runtime_command,
        role_runtime_commands,
        roles_group_name,
    )
    from .provision_board_service import (
        DEFAULT_SHARED_PYTHON,
        default_ticket_board_python,
        env_list,
        env_operation_role_map,
        listener_board_url,
        render_board_unit,
        render_canary_unit,
        render_database_sql,
        render_listener_unit,
        render_polkit_rule,
        render_tmpfiles,
        tenant_primary_group,
    )
    from .provision_repository_boundary import (
        REPOSITORY_BOUNDARY_ALLOWED,
        REPOSITORY_BOUNDARY_BEGIN,
        REPOSITORY_BOUNDARY_END,
        REPOSITORY_BOUNDARY_GUARDS,
        _QUOTED,
        _SHELL_METACHARACTERS,
        _is_boundary_line,
        _outside_quotes,
        repository_boundary_phase,
        repository_boundary_statements,
    )
    from .provision_root_trust import (
        acl_write_grants,
        untrusted_root_executable_reasons,
    )
    from .provision_operator_packet import (
        PACKET_PROVISION_DIR,
        owned_directory_command,
        packet_companion,
        peer_auth_command,
        postgres_sql_file_command,
        render_operator_commands,
        service_user_command,
    )
except ImportError:  # pragma: no cover - supports direct script execution
    import privileged_install
    from board_skill import RELEASE_MARKER_NAME, SKILLS_DIR_NAME
    from commit_repos import commit_git_dir_env_for_project
    from provision_github_identity import (
        DEFAULT_GITHUB_HOST,
        GITHUB_IDENTITY_BEGIN,
        GITHUB_IDENTITY_END,
        OwnerGithubIdentity,
        compose_ssh_config,
        existing_owner_ssh_key_names,
        github_identity_block,
        owner_github_block_removal_commands,
        owner_github_identity_commands,
        owner_github_key_path,
        owner_github_selection_commands,
        parse_managed_github_identity,
        publication_remote_host,
        publication_uses_github,
        resolve_owner_github_identity,
    )
    from provision_path_confinement import (
        INHERITED_WORKTREE_CLOSURE,
        INTERIOR_DIRECTORY_MODE,
        REPOSITORY_COPY_MODE,
        TENANT_SOURCE_MODE,
        _interior_directories,
        _is_within,
        _refuse_prefix_coincidence,
        commit_store_read_commands,
        director_control_access_commands,
        owned_ancestor_dirs,
        owner_home_traversal_commands,
        repository_copy_confinement_commands,
        role_worktree_access_commands,
        socket_group_retirement_commands,
        tenant_source_confinement_commands,
        tenant_worktree_confinement_commands,
    )
    from provision_workflow_projection import (
        SCHEMA_SQL_PATH,
        TENANT_WORKFLOW_EXCLUDED_ACTIONS,
        TENANT_WORKFLOW_EXCLUDED_STAGES,
        WorkflowStageSeed,
        WorkflowTransitionSeed,
        _insert_values_block,
        _parse_sql_bool,
        _parse_sql_nullable_string,
        _parse_sql_string,
        _parse_sql_text_array,
        _project_implementation_owner_roles,
        _project_workflow_allowed_roles,
        _project_workflow_owner_roles,
        _rank_project_stages,
        _schema_sql_text,
        _split_sql_fields,
        _split_sql_tuple_rows,
        project_workflow_stages,
        project_workflow_state_names,
        project_workflow_transitions,
        schema_workflow_stages,
        schema_workflow_transitions,
    )
    from provision_workflow_sql import (
        _workflow_stage_rows_sql,
        _workflow_transition_rows_sql,
        render_add_role_sql,
        render_project_role_constraint_sql,
        render_vcs_close_role_sql,
        render_workflow_sql,
    )
    from provision_role_tooling import (
        GIT_TEMPLATE_DIR_NAME,
        RETIRED_STAGED_EXECUTABLES,
        ROLE_STAGED_EXECUTABLES,
        TENANT_CONTROL_ROOT_ENV,
        _imported_names,
        _release_marker_commit,
        _script_sibling_modules,
        entry_point_module_dependencies,
        git_template_files,
        git_template_staging_commands,
        readable_system_unit_path,
        role_tooling_staging_commands,
        role_tooling_staging_dir,
        staged_role_tooling_problems,
        system_unit_proof_chain,
        system_unit_proof_commands,
        tenant_control_root,
    )
    from provision_publication_grants import (
        DEFAULT_PUBLISH_GRANT_ROOT,
        DEFAULT_PUBLISH_STAGING_ROOT,
        PUBLISH_GRANT_ROOT,
        PUBLISH_GRANT_SCHEMA,
        PUBLISH_STAGING_ROOT,
        publish_grant_commands,
        publish_grant_path,
        publish_grant_root,
        publish_identity_path,
        publish_staging_root,
        publish_sudoers_document,
        publish_sudoers_path,
    )
    from plan_role_derivation import PLAN_DERIVED_ROLE_FIELDS, role_reference, unresolved_plan_field_reason
    from provision_tenant_control import (
        TENANT_CONTROL_GRANT_NAME,
        display_attach_helper_path,
        invoking_human,
        resolve_control_user,
        tenant_control_commands,
        tenant_control_grant,
        tenant_control_grant_document,
        tenant_control_grant_name,
        tenant_control_grant_path,
        tenant_control_helper_path,
        tenant_control_sudoers,
        tenant_control_sudoers_document,
        tenant_control_sudoers_path,
    )
    from provision_role_accounts import (
        NON_PROCESS_ROLES,
        ROLE_ACCOUNT_MIGRATION_SUFFIX,
        ROLE_CONTROL_SUDOERS_HEREDOC,
        render_role_control_sudoers,
        role_account_commands,
        role_account_home,
        role_account_migration_name,
        role_account_name,
        role_account_table,
        role_accounts_command,
        role_accounts_env,
        role_control_sudoers,
        role_control_sudoers_install_commands,
        role_runtime_command,
        role_runtime_commands,
        roles_group_name,
    )
    from provision_board_service import (
        DEFAULT_SHARED_PYTHON,
        default_ticket_board_python,
        env_list,
        env_operation_role_map,
        listener_board_url,
        render_board_unit,
        render_canary_unit,
        render_database_sql,
        render_listener_unit,
        render_polkit_rule,
        render_tmpfiles,
        tenant_primary_group,
    )
    from provision_repository_boundary import (
        REPOSITORY_BOUNDARY_ALLOWED,
        REPOSITORY_BOUNDARY_BEGIN,
        REPOSITORY_BOUNDARY_END,
        REPOSITORY_BOUNDARY_GUARDS,
        _QUOTED,
        _SHELL_METACHARACTERS,
        _is_boundary_line,
        _outside_quotes,
        repository_boundary_phase,
        repository_boundary_statements,
    )
    from provision_root_trust import (
        acl_write_grants,
        untrusted_root_executable_reasons,
    )
    from provision_operator_packet import (
        PACKET_PROVISION_DIR,
        owned_directory_command,
        packet_companion,
        peer_auth_command,
        postgres_sql_file_command,
        render_operator_commands,
        service_user_command,
    )


PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TICKET_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
DEFAULT_IMPLEMENTER_ROLES = ("main", "app", "ops", "perf", "research")
DEFAULT_PROJECT_DRAFT_ROLES = ("designer",)
DEFAULT_PROJECT_SUPPORT_ROLES: tuple[str, ...] = ()
DEFAULT_PROJECT_IMPLEMENTER_ROLES = ("app", "main")
DEFAULT_PGU_ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "inspector", "agent", "director", "research", "user")
DEFAULT_PGU_CALLER_ROLES = ("director", "main", "app", "ops", "perf", "audit", "inspector", "research", "user")
# The pinned shared install every tenant's tooling is staged from.
SHARED_RELEASE_CURRENT = "/opt/switchyard/current"
# The account the board service itself runs as.
DEFAULT_SERVICE_USER = "boardsvc"
DEFAULT_PG_IDENT_MAP = "pgu_ticket_board_service"


@dataclass(frozen=True)
class ProjectBoardProvision:
    project: str
    project_name: str
    ticket_prefix: str
    owner_user: str
    owner_home: str
    service_user: str
    board_unit: str
    canary_unit: str
    listener_unit: str
    tmpfiles_name: str
    polkit_name: str
    role_control_sudoers_name: str
    tenant_control_sudoers_name: str
    runtime_directory: str
    socket_path: str
    port: int
    database: str
    service_role: str
    listener_role: str
    board_root: str
    board_current: str
    source_repo: str
    commit_git_dir: str
    asset_dir: str
    frame_dir: str
    board_log: str
    listener_log: str
    board_database_url: str
    listener_database_url: str
    admin_database_url: str
    workflow_seed: str
    draft_roles: tuple[str, ...]
    implementer_roles: tuple[str, ...]
    audit_roles: tuple[str, ...]
    assignee_roles: tuple[str, ...]
    caller_roles: tuple[str, ...]
    operation_allowed_roles: tuple[tuple[str, tuple[str, ...]], ...]
    board_service_traversal: bool
    # SYRD-39: one Unix account per configured role, and the group that
    # lets exactly those accounts reach this project's board socket.
    #: The human account allowed to drive this tenant's lifecycle without sudo.
    #: Recorded at provisioning; empty means no control bridge is installed and
    #: the owner or an operator starts the project as before.
    control_user: str = ""
    role_accounts: tuple[tuple[str, str], ...] = ()
    roles_group: str = ""
    # (role, worktree path) so the rollout can transfer ownership of the
    # tree each role actually works in.
    role_worktrees: tuple[tuple[str, str], ...] = ()
    #: Which of the owner's SSH keys this tenant publishes with, and the host
    #: patterns its managed block covers. Empty means a plan written before this
    #: was recorded, and the selection is recovered from the tenant's own
    #: managed block instead of being defaulted away (SYRD-100).
    owner_github_key_name: str = ""
    owner_github_host_alias: str = ""
    #: The tenant's own project checkout, when this plan knows it. `source_repo`
    #: is the RELEASE the artifacts are rendered from -- on a provisioned host
    #: `/opt/switchyard/releases/<sha>` -- and passing that where the tenant's
    #: tree was meant produced a packet that confined nothing, because the
    #: release is not under the owner's home. Empty means no supported path has
    #: told this plan where the checkout is, and the packet says so rather than
    #: guessing at one (SYRD-156).
    project_repository: str = ""
    workflow: dict | None = None


def plan_field_names() -> tuple[str, ...]:
    """Every field a current plan document carries, in declaration order."""
    return tuple(ProjectBoardProvision.__dataclass_fields__)


def plan_field_defaults() -> dict[str, object]:
    """The fields the plan declares a safe default for, and that default.

    A field with a declared default is one a project can legitimately not have
    configured yet -- no control bridge, no role accounts, no roles group. That
    default is what an older plan means by not carrying it, so a migration must
    fill it in rather than invent a value, which for these fields would be
    inventing authority (SYRD-52).
    """
    defaults: dict[str, object] = {}
    for name, field in ProjectBoardProvision.__dataclass_fields__.items():
        if field.default is not MISSING:
            defaults[name] = field.default
        elif field.default_factory is not MISSING:  # pragma: no cover - none today
            defaults[name] = field.default_factory()
    return defaults


#: What a generated plan has carried since before this migration begins.
#:
#: A document missing one of these is not an older plan; it is not a plan, and
#: completing it from a reference would invent a tenant -- a board root, a
#: database and a release path for a project that never recorded them. Fields
#: added after this point are absent from the set by construction, so a release
#: that adds one has nothing to update here (SYRD-52).
#:
#: `project_name`, `canary_unit`, `owner_home` and every field with a declared
#: default are deliberately absent: each was already derived or defaulted when
#: missing before this migration existed, and that stays true.
PLAN_BASELINE_FIELDS = frozenset(
    {
        "admin_database_url",
        "asset_dir",
        "assignee_roles",
        "board_current",
        "board_database_url",
        "board_log",
        "board_root",
        "board_service_traversal",
        "board_unit",
        "caller_roles",
        "commit_git_dir",
        "database",
        "draft_roles",
        "frame_dir",
        "implementer_roles",
        "listener_database_url",
        "listener_log",
        "listener_role",
        "listener_unit",
        "owner_user",
        "polkit_name",
        "port",
        "project",
        "runtime_directory",
        "service_role",
        "service_user",
        "socket_path",
        "source_repo",
        "ticket_prefix",
        "tmpfiles_name",
        "workflow_seed",
    }
)


def migrate_plan_document(
    raw: Mapping[str, object], *, reference: ProjectBoardProvision | None
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    """Bring a plan written by an older release up to the current field set.

    Every release that adds a field to the plan otherwise makes every already
    provisioned tenant unupgradable, because the document is parsed into the
    current strict shape before anything can regenerate it. The rule is
    data-driven rather than a growing ladder of per-field special cases: a
    field the plan declares a default for takes that default, and a field
    without one is a generated name or path, taken from a reference plan built
    by the same `build_plan` provisioning uses. Nothing is taken from the
    document that the document does not already carry (SYRD-52).

    Returns the completed document, the fields that were filled in, and the
    fields that could not be -- which the caller reports rather than guesses at.
    """
    defaults = plan_field_defaults()
    document = dict(raw)
    added: list[str] = []
    unresolved: list[str] = []
    derived: list[ProjectBoardProvision | None] = []
    for name in plan_field_names():
        if name in document:
            continue
        if name in PLAN_BASELINE_FIELDS:
            unresolved.append(name)
            continue
        if name in PLAN_DERIVED_ROLE_FIELDS:  # from the recorded roles, or unresolved; never identity (SYRD-543)
            derived = derived or [role_reference(raw, reference)[0]]
            (added if derived[0] else unresolved).append(name)
            if derived[0]:
                document[name] = getattr(derived[0], name)
            continue
        if name in defaults:
            document[name] = defaults[name]
        elif reference is not None:
            document[name] = getattr(reference, name)
        else:
            unresolved.append(name)
            continue
        added.append(name)
    return document, tuple(added), tuple(unresolved)


def _validate_project(value: str) -> str:
    project = value.strip().lower()
    if not PROJECT_RE.fullmatch(project):
        raise SystemExit("project must match ^[a-z0-9][a-z0-9_]{0,39}$")
    return project


def _validate_project_name(value: str) -> str:
    project_name = value.strip()
    if not project_name:
        raise SystemExit("project name must not be empty")
    if len(project_name) > 200 or any(not char.isprintable() for char in project_name):
        raise SystemExit("project name must be at most 200 printable characters")
    return project_name


def _validate_user(value: str) -> str:
    user = value.strip()
    if not USER_RE.fullmatch(user):
        raise SystemExit("owner user must look like a local Unix account name")
    return user


def _validate_role(value: str) -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit("role names must match ^[a-z][a-z0-9_-]{0,63}$")
    return role


def validate_ticket_prefix(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "", value.strip()).upper()
    if not prefix:
        raise SystemExit("ticket prefix must contain at least one ASCII letter or digit")
    if prefix[0].isdigit():
        prefix = f"T{prefix}"
    if not TICKET_PREFIX_RE.fullmatch(prefix):
        raise SystemExit("ticket prefix must normalize to ^[A-Z][A-Z0-9]*$")
    return prefix


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _identifier_from_project(project: str) -> str:
    return project


def allocated_port(project: str, *, base: int = 18_770, span: int = 10_000) -> int:
    if project == "pgu":
        return 8770
    digest = hashlib.blake2s(project.encode("utf-8"), digest_size=2).digest()
    offset = int.from_bytes(digest, "big") % span
    return base + offset


def build_plan(
    *,
    project: str,
    project_name: str | None = None,
    workflow: dict | None = None,
    owner_user: str,
    owner_home: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    service_user: str = DEFAULT_SERVICE_USER,
    service_role: str = "ticket_board_service",
    listener_role: str = "ticket_board_listener",
    board_root: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: Path | str | None = None,
    asset_dir: Path | None = None,
    frame_dir: Path | None = None,
    implementer_roles: Sequence[str] | None = None,
    ticket_prefix: str | None = None,
    board_service_traversal: bool = True,
    control_user: str = "",
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    vcs_close_role: str | None = None,
    #: Which of the owner's keys this tenant publishes with. Recorded here so an
    #: upgrade does not have to recover it, and so the generated operator script
    #: names it (SYRD-100).
    owner_github_key_name: str = "",
    owner_github_host_alias: str = "",
    #: The tenant's project checkout, when the caller knows it. Not defaulted to
    #: a conventional path: a guess that is wrong closes and creates a directory
    #: nobody asked for, and a guess that is right hides that nothing told us.
    project_repository: Path | str | None = None,
) -> ProjectBoardProvision:
    project = _validate_project(project)
    resolved_project_name = _validate_project_name(project_name or ("PGU" if project == "pgu" else project))
    owner_user = _validate_user(owner_user)
    resolved_ticket_prefix = validate_ticket_prefix(ticket_prefix or project)
    if service_user:
        _validate_user(service_user)
    if service_role != "ticket_board_service" or listener_role != "ticket_board_listener":
        raise SystemExit(
            "custom database service/listener roles are not supported yet; "
            "the current schema enforces ticket_board_service and ticket_board_listener"
        )
    resolved_vcs_close_role = _validate_role(vcs_close_role) if vcs_close_role else ""
    if implementer_roles is None:
        resolved_implementer_roles = DEFAULT_IMPLEMENTER_ROLES if project == "pgu" else DEFAULT_PROJECT_IMPLEMENTER_ROLES
    else:
        resolved_implementer_roles = tuple(_validate_role(role) for role in implementer_roles)
        if not resolved_implementer_roles:
            raise SystemExit("at least one implementer role is required")
    resolved_implementer_roles = _dedupe(resolved_implementer_roles)
    if resolved_vcs_close_role:
        resolved_implementer_roles = tuple(role for role in resolved_implementer_roles if role != resolved_vcs_close_role)
        if not resolved_implementer_roles:
            raise SystemExit("at least one implementer role is required outside the VCS close role")
    if audit_roles is None:
        resolved_audit_roles = ("audit",) if include_audit else ()
    else:
        resolved_audit_roles = _dedupe(tuple(_validate_role(role) for role in audit_roles))
    forbidden_audit_roles = set(resolved_audit_roles) & {"designer", "director", "user", "unassigned"}
    if forbidden_audit_roles:
        raise SystemExit(f"audit roles cannot include reserved workflow roles: {', '.join(sorted(forbidden_audit_roles))}")
    role_overlap = set(resolved_implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    if project == "pgu":
        if resolved_vcs_close_role:
            raise SystemExit("pgu uses the full built-in workflow; --vcs-close-role is only for provisioned projects")
        if audit_roles is not None and resolved_audit_roles != ("audit",):
            raise SystemExit("pgu uses the full built-in workflow; custom audit roles are only for provisioned projects")
        draft_roles: tuple[str, ...] = ()
        resolved_audit_roles = ("audit",)
        assignee_roles = DEFAULT_PGU_ASSIGNEES
        caller_roles = DEFAULT_PGU_CALLER_ROLES
    else:
        draft_roles = DEFAULT_PROJECT_DRAFT_ROLES if include_designer else ()
        assignee_roles = _dedupe(
            (
                "unassigned",
                *draft_roles,
                *DEFAULT_PROJECT_SUPPORT_ROLES,
                *resolved_implementer_roles,
                *((resolved_vcs_close_role,) if resolved_vcs_close_role else ()),
                *resolved_audit_roles,
                "director",
                "user",
            )
        )
        caller_roles = _dedupe(
            (
                "director",
                *draft_roles,
                *DEFAULT_PROJECT_SUPPORT_ROLES,
                *resolved_implementer_roles,
                *((resolved_vcs_close_role,) if resolved_vcs_close_role else ()),
                *resolved_audit_roles,
                "user",
            )
        )
    operation_allowed_roles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if project != "pgu" and resolved_audit_roles and resolved_audit_roles != ("audit",):
        task_roles = _dedupe((*resolved_implementer_roles, "director", *resolved_audit_roles))
        operation_allowed_roles = (
            ("file_bug", _dedupe((*resolved_implementer_roles, *resolved_audit_roles))),
            ("start_task", task_roles),
            ("complete_task", task_roles),
            ("audit_sign_off", resolved_audit_roles),
            ("audit_kick_back", resolved_audit_roles),
        )
    if resolved_vcs_close_role:
        if resolved_vcs_close_role not in assignee_roles or resolved_vcs_close_role not in caller_roles:
            raise SystemExit("--vcs-close-role must be one of the configured project roles")
        operation_allowed_roles = (*operation_allowed_roles, ("mark_done", (resolved_vcs_close_role,)))
    ident = _identifier_from_project(project)
    unit_prefix = f"{project}-ticket-board"
    runtime_directory = unit_prefix
    home = (owner_home or (Path("/home") / owner_user)).expanduser()
    if not home.is_absolute():
        raise SystemExit("owner home must be an absolute path")
    resolved_board_root = board_root or home / f"{project}-ticketboard-live"
    if workflow is not None:
        try:
            from .workflow_config import validate
        except ImportError:
            from workflow_config import validate
        workflow = validate(workflow,project=project)
    resolved_source_repo = source_repo or Path(__file__).resolve().parents[2]
    resolved_commit_git_dir = str(commit_git_dir) if commit_git_dir is not None else commit_git_dir_env_for_project(project=project, owner_home=home)
    resolved_asset_dir = asset_dir or home / ".claude" / f"{project}-tickets-assets"
    if frame_dir is not None:
        resolved_frame_dir = frame_dir
    elif project == "pgu":
        resolved_frame_dir = Path("/tmp/pgu-frames")
    else:
        resolved_frame_dir = home / ".claude" / f"{project}-ticket-frames"
    for path_name, tenant_path in (
        ("board root", resolved_board_root),
        ("asset directory", resolved_asset_dir),
        ("frame directory", resolved_frame_dir),
    ):
        if not tenant_path.is_absolute():
            raise SystemExit(f"{path_name} must be an absolute tenant path")
    resolved_database = database or ("pgu" if project == "pgu" else f"{ident}_ticket_board")
    resolved_port = port if port is not None else allocated_port(project)
    if not (1 <= resolved_port <= 65535):
        raise SystemExit("port must be between 1 and 65535")
    board_current = resolved_board_root / "current"
    return ProjectBoardProvision(
        project=project,
        project_name=resolved_project_name,
        ticket_prefix=resolved_ticket_prefix,
        owner_user=owner_user,
        control_user=control_user,
        owner_home=str(home),
        owner_github_key_name=owner_github_key_name.strip(),
        owner_github_host_alias=owner_github_host_alias.strip(),
        service_user=service_user,
        board_unit=f"{unit_prefix}.service",
        canary_unit=f"{unit_prefix}-canary.service",
        listener_unit=f"{unit_prefix}-notify-listener.service",
        tmpfiles_name=f"{unit_prefix}.conf",
        polkit_name=f"49-{unit_prefix}-deploy.rules",
        role_control_sudoers_name=f"49-{project}-role-control",
        tenant_control_sudoers_name=f"49-{project}-tenant-control",
        runtime_directory=runtime_directory,
        socket_path=f"/run/{runtime_directory}/ticket-board.sock",
        port=resolved_port,
        database=resolved_database,
        service_role=service_role,
        listener_role=listener_role,
        board_root=str(resolved_board_root),
        board_current=str(board_current),
        source_repo=str(resolved_source_repo),
        project_repository=str(project_repository) if project_repository else "",
        workflow=workflow,
        commit_git_dir=resolved_commit_git_dir,
        asset_dir=str(resolved_asset_dir),
        frame_dir=str(resolved_frame_dir),
        board_log=f"/var/log/{unit_prefix}.log",
        listener_log=str(home / ".local" / "state" / f"{unit_prefix}-notify-listener.log"),
        board_database_url=(
            f"postgresql:///{resolved_database}?host=/var/run/postgresql&user={service_role}"
        ),
        listener_database_url=(
            f"postgresql:///{resolved_database}?host=/var/run/postgresql&user={listener_role}"
        ),
        admin_database_url=f"postgresql:///{resolved_database}?host=/var/run/postgresql",
        workflow_seed="pgu-full" if project == "pgu" else "default-project",
        draft_roles=draft_roles,
        implementer_roles=resolved_implementer_roles,
        audit_roles=resolved_audit_roles,
        assignee_roles=assignee_roles,
        caller_roles=caller_roles,
        operation_allowed_roles=operation_allowed_roles,
        board_service_traversal=board_service_traversal,
        role_accounts=(),
        # The owner and service share only this project's socket. Logical roles
        # are PostgreSQL/process data and do not become Unix users or groups.
        roles_group=owner_user,
    )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def systemd_environment(name: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{name}={escaped}"'


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class PathContainmentError(ValueError):
    """A path that is not where a privileged grant was told it would be."""


def _refuse_unnormalized(path: str, *, what: str) -> None:
    """Refuse a path that is not already an absolute path in normal form.

    Component containment is still lexical: `/home/foo/../foobar` *is*
    relative to `/home/foo` as far as `PurePath` is concerned, and the
    interior sliced out of it is `/home/foo/..`, so a root-run artifact would
    write the grant against `/home`. Normalizing here would mean resolving
    through directories the tenant controls, which is its own escape, so a
    path carrying `..` -- or one that is not absolute at all -- is refused
    instead. `.` and repeated separators are dropped by `PurePosixPath`
    without changing which directory is named, and are not an escape (SYRD-49).
    """
    if not path:
        return
    candidate = PurePosixPath(path)
    if not candidate.is_absolute():
        raise PathContainmentError(
            f"{what} {path} is not an absolute path; refusing to grant access to it"
        )
    if ".." in candidate.parts:
        raise PathContainmentError(
            f"{what} {path} is not in normal form; refusing to grant access to a path "
            "that walks back out of itself"
        )





def repository_group_name(project: str) -> str:
    """The group that may read and write this project's repositories.

    Deliberately not the socket group. The socket group exists so role accounts
    can talk to the board, and the board service must be in it to hand the
    socket over; a repository granted to that group is therefore a repository
    granted to the board service (SYRD-157).
    """
    return f"{project}-repo"


def repository_group_commands(project: str, accounts: Sequence[str]) -> list[str]:
    """Create the repository group and put exactly the git-using accounts in it.

    The board service is not one of them and must never be added: that is the
    whole distinction this group exists to make. Idempotent -- an existing group
    is left alone and `gpasswd -a` on an existing member changes nothing.
    """
    group = shell_quote(repository_group_name(project))
    lines = [
        f"if ! getent group {group} >/dev/null 2>&1; then",
        f"    sudo groupadd -r {group}",
        "fi",
    ]
    for account in accounts:
        if not account:
            continue
        lines.append(f"sudo gpasswd -a {shell_quote(account)} {group} >/dev/null")
    return lines


#: Mode for a repository copy that a group must still WRITE through an ACL.
#: `chmod` recomputes the ACL mask from the group bits, so a repository closed
#: to 0750 has its `rwx` group entry clipped to `r-x` the moment it is closed.
#: The group bits here belong to the tenant's own group, so the extra bit grants
#: nobody anything; what it does is keep the mask from taking away a grant that
#: was deliberately made.
WRITABLE_REPOSITORY_COPY_MODE = "0770"


def tenant_worktree_base(plan: ProjectBoardProvision) -> str:
    """Where this tenant's role worktrees live.

    Taken from the worktrees the plan records when they agree on a parent, and
    otherwise from the convention the launcher creates them under. Either way
    it is derived here rather than read from the tenant's configuration, which
    the account every role runs as can write.
    """
    recorded = {
        str(PurePosixPath(path).parent)
        for _role, path in plan.role_worktrees
        if path
    }
    if len(recorded) == 1:
        return recorded.pop()
    return f"{plan.owner_home}/{plan.project}-worktrees"


def tenant_control_repository(plan: ProjectBoardProvision) -> str:
    """This tenant's control repository, by the convention the product enforces.

    Loading a configuration already refuses a control repository outside the
    managed directory under the owner's home, so the path is not a free choice
    and does not have to be read from a document to be known.
    """
    return (
        f"{plan.owner_home}/.local/state/switchyard/projects/{plan.project}/control.git"
    )


TENANT_CONTROL_ROOT = "/usr/local/lib/switchyard"


#: The lifecycle entrypoint the bridge runs. Deliberately the shared release
#: rather than the tenant's own deployed tree: that tree lives under the owner's
#: home, where the owner can replace the `current` symlink or any directory
#: above the launcher, and a program the tenant owner can swap is not a pinned
#: entrypoint however root-owned its last component is (SYRD-50 review).
TENANT_CONTROL_LAUNCHER = f"{SHARED_RELEASE_CURRENT}/switchyard"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_text_array(values: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(sql_literal(value) for value in values) + "]::text[]"


# SYRD-39: which generated artifacts the tenant owns, and which root consumes.
# Everything root reads back -- units it installs, the tmpfiles and polkit
# fragments, the sudoers grant, the SQL it runs as postgres, the operator
# script -- must stay root-owned wherever it lives, because a tenant that can
# rewrite one of those chooses what root runs. The plan is the tenant's: the
# unprivileged workflow projection writes it.
TENANT_ARTIFACT_NAMES: tuple[str, ...] = ("plan.json",)

DEFAULT_PRIVILEGED_PROVISION_ROOT = Path("/etc/switchyard/provision")


#: Root's own copy of a project's declared workflow, written beside its plan.
#: The document decides which roles exist, what each may call, and every stage
#: and transition the board will accept -- which is to say it decides authority.
#: A copy of it that the account every role runs as can write is a copy that
#: account can grant itself with, so root keeps one where only root can, and
#: regeneration reads that and nothing else (SYRD-165).
WORKFLOW_RECORD_NAME = "workflow.json"

#: What a plan says when this project declares a workflow and root holds no
#: verified copy of it. Root will not seed a workflow it cannot vouch for, and
#: it will not quietly seed the default one in its place: the board keeps what
#: it is running, and the packet says so.
UNVERIFIED_DECLARED_WORKFLOW = "declared-unverified"


def workflow_document_digest(document: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def render_workflow_record(plan: ProjectBoardProvision) -> str:
    """The declared workflow, as root records it for itself."""
    if plan.workflow is None:
        raise SystemExit("a workflow record is only written for a declared workflow")
    return (
        json.dumps(
            {
                "project": plan.project,
                "digest": workflow_document_digest(plan.workflow),
                "document": plan.workflow,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def workflow_record_document(
    raw: Mapping[str, object] | None, *, project: str
) -> tuple[dict | None, str]:
    """The document a workflow record carries, or why it may not be used.

    Every answer but a complete, self-consistent record for this project is a
    refusal. A record whose digest does not match the document it carries has
    been edited by something that did not write it, and the whole point of
    keeping it here is that nothing else should have.
    """
    if raw is None:
        return None, f"root holds no recorded workflow for {project}"
    if not isinstance(raw, Mapping):
        return None, f"root's workflow record for {project} is not a document"
    recorded_project = str(raw.get("project") or "").strip()
    if recorded_project != project:
        return None, (
            f"root's workflow record names project {recorded_project!r}, not {project!r}"
        )
    document = raw.get("document")
    if not isinstance(document, dict):
        return None, f"root's workflow record for {project} carries no document"
    recorded_digest = str(raw.get("digest") or "").strip()
    actual = workflow_document_digest(document)
    if recorded_digest != actual:
        return None, (
            f"root's workflow record for {project} does not match its own digest "
            f"({recorded_digest or 'none recorded'} vs {actual}); it was changed by "
            "something that did not write it"
        )
    try:
        try:
            from .workflow_config import validate
        except ImportError:  # pragma: no cover - direct script execution
            from workflow_config import validate

        validated = validate(document, project=project)
    except (ValueError, SystemExit) as exc:
        return None, f"root's recorded workflow for {project} is not usable: {exc}"
    return validated, ""


def privileged_artifact_names(plan: ProjectBoardProvision) -> tuple[str, ...]:
    """Generated files root installs or executes, in the order write_artifacts emits them."""
    return (
        plan.board_unit,
        plan.canary_unit,
        plan.listener_unit,
        plan.tmpfiles_name,
        plan.polkit_name,
        plan.role_control_sudoers_name,
        *((plan.tenant_control_sudoers_name, tenant_control_grant_name(plan.project))
          if plan.control_user else ()),
        f"{plan.project}-database.sql",
        f"{plan.project}-workflow.sql",
        "operator-commands.sh",
        *((WORKFLOW_RECORD_NAME,) if plan.workflow is not None else ()),
    )


def privileged_provision_dir(project: str, *, root: Path | None = None) -> Path:
    """Root-owned mirror of the privileged artifacts, outside the tenant's tree.

    The provision directory itself is handed to the tenant, so a root-owned file
    inside it can still be unlinked and replaced by its owner. The copy root
    actually installs from therefore lives beside the project registry under
    /etc/switchyard, which no tenant can reach.
    """
    return (root or DEFAULT_PRIVILEGED_PROVISION_ROOT) / project


def write_artifacts(plan: ProjectBoardProvision, output_dir: Path, *, enable_owner_linger: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "plan.json": json.dumps(asdict(plan), indent=2, sort_keys=True) + "\n",
        plan.board_unit: render_board_unit(plan),
        plan.canary_unit: render_canary_unit(plan),
        plan.listener_unit: render_listener_unit(plan),
        plan.tmpfiles_name: render_tmpfiles(plan),
        plan.polkit_name: render_polkit_rule(plan),
        plan.role_control_sudoers_name: role_control_sudoers(plan),
        **(
            {
                plan.tenant_control_sudoers_name: tenant_control_sudoers(plan),
                tenant_control_grant_name(plan.project): tenant_control_grant(plan),
            }
            if plan.control_user
            else {}
        ),
        f"{plan.project}-database.sql": render_database_sql(plan),
        f"{plan.project}-workflow.sql": render_workflow_sql(plan),
        **(
            {WORKFLOW_RECORD_NAME: render_workflow_record(plan)}
            if plan.workflow is not None
            else {}
        ),
        "operator-commands.sh": render_operator_commands(plan, enable_owner_linger=enable_owner_linger),
    }
    for name, text in files.items():
        path = output_dir / name
        path.write_text(text, encoding="utf-8")
        if name.endswith(".sh"):
            path.chmod(0o755)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render per-project ticket-board provisioning artifacts.")
    parser.add_argument("--project", required=True, help="project slug, e.g. pgu or stellaris")
    parser.add_argument("--workflow-config", type=Path, help="validated declarative roles/stages document")
    parser.add_argument("--project-name", help="display name shown by the ticket board")
    parser.add_argument("--owner-user", required=True, help="Unix user that owns this project/team")
    parser.add_argument("--owner-home", type=Path, help="absolute owner home; default /home/<owner-user>")
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database name; omitted means <project>_ticket_board")
    parser.add_argument("--ticket-prefix", help="ticket id prefix; omitted means normalized project slug")
    parser.add_argument("--service-user", default="boardsvc", help="systemd User for the board service")
    parser.add_argument("--service-role", default="ticket_board_service", help="database writer role")
    parser.add_argument("--listener-role", default="ticket_board_listener", help="database listener role")
    parser.add_argument("--board-root", type=Path, help="versioned release root; default under owner home")
    parser.add_argument("--source-repo", type=Path, help="source checkout to export into the board release root")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--asset-dir", type=Path, help="durable attachment directory")
    parser.add_argument("--frame-dir", type=Path, help="frame inbox directory")
    parser.add_argument(
        "--board-service-traversal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="grant boardsvc ACL traversal/read/write access for owner-home board paths",
    )
    parser.add_argument(
        "--implementer-role",
        action="append",
        dest="implementer_roles",
        help="implementation-stage owner role; repeat for multiple implementers",
    )
    parser.add_argument(
        "--include-designer",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the optional designer draft role",
    )
    parser.add_argument(
        "--include-audit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the optional audit role and audit workflow stage",
    )
    parser.add_argument(
        "--audit-role",
        action="append",
        dest="audit_roles",
        help="audit-stage owner role; repeat for multiple auditors (default: audit when --include-audit)",
    )
    parser.add_argument(
        "--vcs-close-role",
        help="insert a VCS stage after final sign-off and allow this role to mark tickets done",
    )
    parser.add_argument("--output-dir", type=Path, help="write all artifacts to this directory")
    parser.add_argument("--json", action="store_true", help="print plan JSON")
    parser.add_argument(
        "--render",
        choices=["board-unit", "listener-unit", "tmpfiles", "polkit", "role-control-sudoers", "tenant-control-sudoers", "tenant-control-grant", "database-sql", "workflow-sql", "commands"],
        help="print one rendered artifact",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    plan = build_plan(
        project=args.project,
        project_name=args.project_name,
        workflow=json.loads(args.workflow_config.read_text()) if args.workflow_config else None,
        owner_user=args.owner_user,
        owner_home=args.owner_home,
        port=args.port,
        database=args.database,
        ticket_prefix=args.ticket_prefix,
        service_user=args.service_user,
        service_role=args.service_role,
        listener_role=args.listener_role,
        board_root=args.board_root,
        source_repo=args.source_repo,
        commit_git_dir=args.commit_git_dir,
        asset_dir=args.asset_dir,
        frame_dir=args.frame_dir,
        implementer_roles=args.implementer_roles,
        board_service_traversal=args.board_service_traversal,
        include_designer=args.include_designer,
        include_audit=args.include_audit,
        audit_roles=args.audit_roles,
        vcs_close_role=args.vcs_close_role,
    )
    if args.output_dir:
        write_artifacts(plan, args.output_dir)
        print(f"wrote {args.output_dir}")
    if args.json:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
    if args.render:
        renderers = {
            "board-unit": render_board_unit,
            "listener-unit": render_listener_unit,
            "tmpfiles": render_tmpfiles,
            "polkit": render_polkit_rule,
            "role-control-sudoers": role_control_sudoers,
            "tenant-control-sudoers": tenant_control_sudoers,
            "tenant-control-grant": tenant_control_grant,
            "database-sql": render_database_sql,
            "workflow-sql": render_workflow_sql,
            "commands": render_operator_commands,
        }
        sys.stdout.write(renderers[args.render](plan))
    if not args.output_dir and not args.json and not args.render:
        print(json.dumps(asdict(plan), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
