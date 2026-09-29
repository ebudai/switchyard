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
        "audit_roles",
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
        "operation_allowed_roles",
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
    for name in plan_field_names():
        if name in document:
            continue
        if name in PLAN_BASELINE_FIELDS:
            unresolved.append(name)
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


#: How the operator packet names a file that ships beside it.
#:
#: The packet is installed root-owned and run by absolute path -- from a
#: journal, from a Polkit transaction, from whatever directory the operator
#: happened to be in. A bare file name is resolved against that directory, so
#: `install -m 0644 testing-ticket-board.conf ...` looked up a root-owned
#: artifact in the caller's cwd and failed there, which is what journal attempt
#: 0007 recorded. `$provision_dir` is the packet's own directory, computed from
#: `BASH_SOURCE` at the top, so a companion is addressed from the packet rather
#: than from whoever started it (SYRD-149).
PACKET_PROVISION_DIR = '"$provision_dir"'


def packet_companion(name: str) -> str:
    """One artifact that ships beside the packet, addressed from the packet."""
    if name.startswith("/"):
        raise SystemExit(f"a packet companion is a file name beside the packet, not a path: {name}")
    return f"{PACKET_PROVISION_DIR}/{shell_quote(name)}"


def postgres_sql_file_command(sql_file: str, *, database_url: str = "", companion: bool = False) -> str:
    command = "sudo cat " + (packet_companion(sql_file) if companion else shell_quote(sql_file))
    command += " | sudo -u postgres psql -X -v ON_ERROR_STOP=1"
    if database_url:
        command += " " + shell_quote(database_url)
    command += " -f -"
    return command


def service_user_command(service_user: str) -> str:
    q_service_user = shell_quote(service_user)
    return f"""if ! getent passwd {q_service_user} >/dev/null 2>&1; then
    service_shell="$(command -v nologin 2>/dev/null || true)"
    [ -n "$service_shell" ] || service_shell="/usr/sbin/nologin"
    [ -x "$service_shell" ] || service_shell="/bin/false"
    if [ ! -x "$service_shell" ]; then
        echo 'ERROR: neither nologin nor /bin/false is executable; install util-linux or provide a non-login shell before provisioning' >&2
        exit 1
    fi
    sudo useradd -r -M -d /nonexistent -s "$service_shell" {q_service_user}
fi"""


def acl_write_grants(
    path: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> tuple[list[str], str]:
    """Access control entries that let somebody other than root write this.

    Mode bits are not the whole story and `ls -l` shows only a trailing `+`:
    the grant behind this was `user:<control role>:rwx` on a file whose mode
    said `root:root`. Returns the offending entries and, separately, why the
    list could not be read -- which is itself a refusal, because a permission
    that cannot be checked has not been checked (SYRD-62).
    """
    try:
        proc = runner(
            ["getfacl", "-p", "--omit-header", "--absolute-names", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return [], str(exc)
    if getattr(proc, "returncode", 1) != 0:
        return [], (str(getattr(proc, "stderr", "") or "").strip() or "getfacl failed")
    grants: list[str] = []
    for line in str(getattr(proc, "stdout", "") or "").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        fields = entry.split(":")
        default = fields[0] == "default"
        if default:
            fields = fields[1:]
        if len(fields) < 3:
            continue
        kind, qualifier, perms = fields[0], fields[1], fields[2]
        if kind == "mask" or "w" not in perms:
            continue
        # The base entries are the mode bits, and those are checked directly.
        # A default entry is different: it decides what a file created here
        # later will carry, so `default:other::rw-` is a standing grant even
        # though nothing writable exists yet.
        named = bool(qualifier) and kind in {"user", "group"}
        inherited_world = default and kind == "other"
        if not (named or inherited_world):
            continue
        grants.append(f"{'default:' if default else ''}{kind}:{qualifier}:{perms}")
    return grants, ""


def untrusted_root_executable_reasons(
    path: Path,
    *,
    boundary: Path | None = None,
    owner_uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    """Why root must not execute this file. Empty means every step of it is root's.

    A root-run script is only as trustworthy as the whole path to it. The file's
    own mode says nothing while the directory holding it belongs to somebody
    else, who can unlink it and put their own script at the same name -- which
    is exactly the shape this found: a `root:root` script in a tenant-owned
    directory, both carrying a named `rwx` entry for the control role.

    So the file, every directory above it up to `boundary` (the filesystem root
    unless a caller narrows it), the absence of any symlink along the way, and
    the access control entries of each are all checked (SYRD-62).

    `owner_uid` is the identity entitled to have written all of it. It defaults
    to this process's own real uid, which is root wherever this decides whether
    root may execute something -- naming it rather than a literal zero is what
    lets the security cases be exercised as a namespace root instead of only on
    a host.
    """
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return [f"{path} is not an absolute path in normal form"]
    expected_uid = os.getuid() if owner_uid is None else owner_uid
    reasons: list[str] = []
    real = Path(os.path.realpath(candidate))
    if real != candidate:
        reasons.append(f"{candidate} is reached through a symlink and resolves to {real}")
    stop = Path(boundary) if boundary is not None else Path(candidate.anchor or "/")
    chain: list[Path] = []
    for entry in (candidate, *candidate.parents):
        chain.append(entry)
        if entry == stop:
            break
    else:
        reasons.append(f"{candidate} is not under {stop}")
    for entry in chain:
        try:
            info = entry.lstat()
        except OSError as exc:
            reasons.append(f"{entry} cannot be inspected: {exc}")
            continue
        if stat.S_ISLNK(info.st_mode):
            reasons.append(f"{entry} is a symlink")
            continue
        if entry == candidate and not stat.S_ISREG(info.st_mode):
            reasons.append(f"{entry} is not a regular file")
        if entry != candidate and not stat.S_ISDIR(info.st_mode):
            reasons.append(f"{entry} is not a directory")
        if info.st_uid != expected_uid:
            reasons.append(
                f"{entry} is owned by uid {info.st_uid} rather than by uid {expected_uid}"
            )
        if info.st_mode & 0o022:
            reasons.append(
                f"{entry} is group- or world-writable (mode {stat.S_IMODE(info.st_mode):04o})"
            )
        grants, unreadable = acl_write_grants(entry, runner=runner)
        if unreadable:
            reasons.append(f"{entry} access control list could not be read: {unreadable}")
        reasons.extend(f"{entry} grants write through {grant}" for grant in grants)
    return reasons


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

#: The packet's repository-boundary phase, fenced so it can be lifted out of a
#: root-owned packet and applied on its own. A tenant that is already running
#: needs this repair and needs nothing else in the packet -- not a deploy, not
#: the schema, not a unit reload -- and the only way to be sure that is what it
#: gets is to take the lines from root's own artifact rather than render them
#: again beside it (SYRD-175).
REPOSITORY_BOUNDARY_BEGIN = "# >>> switchyard repository boundary"
REPOSITORY_BOUNDARY_END = "# <<< switchyard repository boundary"

#: What a line inside that fence may start with. The fence says where the phase
#: is; this says what a phase is allowed to be, so a packet that grew a deploy
#: inside the markers is refused rather than run.
REPOSITORY_BOUNDARY_ALLOWED = (
    "sudo install -d ",
    "sudo setfacl ",
    "sudo find ",
    "sudo groupadd ",
    "sudo gpasswd ",
)

#: The guards the phase is allowed to put around those commands, exactly as it
#: writes them. A guard is matched whole rather than by prefix: its own line is
#: the one place the phase spells `;`, `>` and `&`, so anything else wearing
#: that shape has to be rejected on sight.
_QUOTED = r"(?:'[^']*'|\"'\")+"
REPOSITORY_BOUNDARY_GUARDS = (
    re.compile(rf"^if (?:! )?getent group {_QUOTED} >/dev/null 2>&1; then$"),
    re.compile(rf"^if \[ -d {_QUOTED} \]; then$"),
)

#: What may appear outside quotes in a command line. The phase quotes every
#: path it names, so a `;`, a `|`, a backtick or a `$(` in the open is a second
#: command riding along on the first.
_SHELL_METACHARACTERS = ";&|`$<>()\n"


def _outside_quotes(line: str) -> str:
    """The part of a line the shell would read as syntax rather than as text."""
    bare: list[str] = []
    single = double = False
    for character in line:
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif not single and not double:
            bare.append(character)
    if single or double:
        return _SHELL_METACHARACTERS  # an unbalanced quote is not a line we run
    return "".join(bare)


def _is_boundary_line(line: str) -> bool:
    stripped = line.strip()
    if stripped == "fi" or stripped.startswith("# "):
        return True
    if any(guard.match(stripped) for guard in REPOSITORY_BOUNDARY_GUARDS):
        return True
    if not stripped.startswith(REPOSITORY_BOUNDARY_ALLOWED):
        return False
    # The one redirection the phase writes -- `gpasswd` reporting the group it
    # just changed -- is part of the command, not a second one.
    stripped = re.sub(r" >/dev/null(?: 2>&1)?$", "", stripped)
    return not any(
        character in _SHELL_METACHARACTERS for character in _outside_quotes(stripped)
    )


def repository_boundary_statements(phase: list[str]) -> list[list[str]]:
    """Group a boundary phase's lines into the statements the shell sees.

    The phase's guards -- `if [ -d ... ]; then` around the worktree sweep and
    `if getent group ...; then` around the retirement -- are several lines and
    one command. Running the lines one at a time would hand `sh` half an `if`,
    so the grouping the shell would do is done here, and each statement is run
    and reported whole (SYRD-175).
    """
    statements: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for line in phase:
        current.append(line)
        stripped = line.strip()
        if stripped.endswith("; then"):
            depth += 1
        elif stripped == "fi":
            depth -= 1
        if depth <= 0:
            statements.append(current)
            current = []
            depth = 0
    if current:
        statements.append(current)
    return statements


def repository_boundary_phase(packet: str) -> tuple[list[str], str]:
    """The boundary phase of a rendered packet, or why it may not be used.

    Lifted from between the markers rather than re-rendered, so what runs is
    what root installed. Every line is then checked against the shapes a
    boundary phase is made of: this exists to apply an ACL and mode repair to a
    tenant that is already serving, and a phase carrying anything else -- a
    deploy, a psql, a systemctl -- is a packet this will not run at all.
    """
    lines = packet.splitlines()
    try:
        start = lines.index(REPOSITORY_BOUNDARY_BEGIN)
        end = lines.index(REPOSITORY_BOUNDARY_END, start)
    except ValueError:
        return [], (
            "this packet has no repository boundary phase in it, so it was generated "
            "before that phase existed"
        )
    phase = [line for line in lines[start + 1 : end] if line.strip()]
    for line in phase:
        if not _is_boundary_line(line):
            return [], (
                f"the repository boundary phase of this packet contains a line that is not "
                f"part of one: {line.strip()!r}"
            )
    return phase, ""


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


def peer_auth_command(plan: ProjectBoardProvision) -> str:
    q_setup_script = shell_quote(f"{plan.source_repo}/scripts/ticket-board-boardsvc-setup.sh")
    return (
        "sudo env "
        f"PG_DATABASE={shell_quote(plan.database)} "
        f"PG_IDENT_MAP={shell_quote(DEFAULT_PG_IDENT_MAP)} "
        f"SERVICE_USER={shell_quote(plan.service_user)} "
        f"SERVICE_ROLE={shell_quote(plan.service_role)} "
        f"{q_setup_script} --apply-peer-auth"
    )


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_text_array(values: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(sql_literal(value) for value in values) + "]::text[]"


def owned_directory_command(plan: ProjectBoardProvision, dirs: Sequence[str], *, mode: str = "0755") -> str:
    unique_dirs = _dedupe(tuple(dirs))
    if not unique_dirs:
        return ""
    quoted_dirs = " ".join(shell_quote(value) for value in unique_dirs)
    return (
        f"sudo install -d -m {mode} -o {shell_quote(plan.owner_user)} "
        f"-g {shell_quote(plan.owner_user)} {quoted_dirs}"
    )


def render_operator_commands(plan: ProjectBoardProvision, *, enable_owner_linger: bool = True) -> str:
    q_asset = shell_quote(plan.asset_dir)
    q_frame = shell_quote(plan.frame_dir)
    q_board_root = shell_quote(plan.board_root)
    q_source_repo = shell_quote(plan.source_repo)
    q_commit_git_dir = shell_quote(plan.commit_git_dir)
    q_deploy_script = shell_quote(f"{plan.source_repo}/scripts/ticket-board-service.sh")
    q_board_unit = shell_quote(f"/etc/systemd/system/{plan.board_unit}")
    # Published from the artifacts the operator is reviewing, so the deployer
    # compares the release's unit against the loaded one rather than being sent
    # into a directory it cannot read (SYRD-127).
    system_unit_proof = "\n".join(
        system_unit_proof_commands(plan.project, '"$system_unit_candidate"')
    )
    q_readable_system_unit = shell_quote(readable_system_unit_path(plan.project))
    q_canary_unit = shell_quote(f"/etc/systemd/system/{plan.canary_unit}")
    q_tmpfiles = shell_quote(f"/etc/tmpfiles.d/{plan.tmpfiles_name}")
    q_polkit = shell_quote(f"/etc/polkit-1/rules.d/{plan.polkit_name}")
    q_role_control_sudoers = shell_quote(f"/etc/sudoers.d/{plan.role_control_sudoers_name}")
    github_identity = "\n".join(
        owner_github_identity_commands(
            plan.owner_user,
            plan.owner_home,
            key_name=plan.owner_github_key_name,
            host_alias=plan.owner_github_host_alias,
            comment=f"{plan.owner_user} switchyard {plan.project}",
        )
    )
    if role_control_sudoers(plan).strip():
        # visudo -c first: a malformed sudoers file can lock the host out of
        # sudo entirely, so it is validated before it is installed.
        install_role_control_sudoers = (
            f"sudo install -m 0440 -o root -g root {packet_companion(plan.role_control_sudoers_name)} "
            f"{q_role_control_sudoers}.staged\n"
            f"sudo visudo -c -f {q_role_control_sudoers}.staged\n"
            f"sudo mv {q_role_control_sudoers}.staged {q_role_control_sudoers}"
        )
    else:
        install_role_control_sudoers = (
            "# no role control interface: this project declares no director role"
        )
    q_tenant_control_sudoers = shell_quote(f"/etc/sudoers.d/{plan.tenant_control_sudoers_name}")
    if plan.control_user:
        # After the role tooling staging, because the rule names the staged
        # helper and should not be live before it exists. Same
        # visudo-before-install order, and the grant lands first so the rule is
        # never live for a moment without the data that constrains it.
        install_tenant_control = "\n".join(
            [
                f"sudo install -d -m 0755 -o root -g root {shell_quote(TENANT_CONTROL_ROOT + '/' + plan.project)}",
                f"sudo install -m 0644 -o root -g root {packet_companion(tenant_control_grant_name(plan.project))} "
                f"{shell_quote(tenant_control_grant_path(plan.project))}",
                f"sudo install -m 0440 -o root -g root {packet_companion(plan.tenant_control_sudoers_name)} "
                f"{q_tenant_control_sudoers}.staged",
                f"sudo visudo -c -f {q_tenant_control_sudoers}.staged",
                f"sudo mv {q_tenant_control_sudoers}.staged {q_tenant_control_sudoers}",
            ]
        )
    else:
        install_tenant_control = (
            "# no lifecycle control bridge: this project records no human controller"
        )
    q_listener_unit = shell_quote(
        f"{plan.owner_home}/.config/systemd/user/{plan.listener_unit}"
    )
    q_owner_home = shell_quote(plan.owner_home)
    q_hook_installer = shell_quote(f"{plan.board_current}/scripts/ticket-board-install-pane-hooks")
    q_hook_source = shell_quote(f"{plan.board_current}/scripts/ticket-board-pane-idle-hook")
    q_hook_bin = shell_quote(f"{plan.owner_home}/.local/bin/ticket-board-pane-idle-hook")
    q_board_skill_installer = shell_quote(f"{plan.board_current}/scripts/switchyard-board-skill")
    # Roles get the same preparation as the owner, from the deployed release,
    # once the board exists (SYRD-39).
    role_runtime_step = role_runtime_command(plan) or (
        "# no per-role runtime preparation: this project declares no role accounts"
    )
    q_pane_session_dir = shell_quote(
        f"{plan.owner_home}/.local/state/{plan.runtime_directory}/pane-sessions"
    )
    workflow_seed_command = ""
    if plan.workflow_seed != "pgu-full":
        workflow_seed_command = postgres_sql_file_command(
            plan.project + "-workflow.sql",
            database_url=plan.admin_database_url,
            companion=True,
        )
        workflow_seed_command += "\n"
    install_board_root = owned_directory_command(
        plan,
        owned_ancestor_dirs(plan.owner_home, plan.board_root, include_target=True),
    )
    if not install_board_root:
        install_board_root = f"sudo install -d -m 0755 {q_board_root}"
    install_asset_frame_parent = owned_directory_command(
        plan,
        (
            *owned_ancestor_dirs(plan.owner_home, plan.asset_dir, include_target=False),
            *owned_ancestor_dirs(plan.owner_home, plan.frame_dir, include_target=False),
        ),
    )
    if plan.project == "pgu" and plan.frame_dir == "/tmp/pgu-frames":
        install_asset_frame = "\n".join(
            line
            for line in (
                install_asset_frame_parent,
                f"sudo install -d -m 0775 -o {shell_quote(plan.owner_user)} -g {shell_quote(plan.owner_user)} {q_asset}",
                f"sudo install -d -m 1777 -o root -g root {q_frame}",
            )
            if line
        )
        grant_asset_frame = f"sudo setfacl -R -m u:{plan.service_user}:rwx {q_asset}"
    else:
        install_asset_frame = "\n".join(
            line
            for line in (
                install_asset_frame_parent,
                f"sudo install -d -m 0775 -o {shell_quote(plan.owner_user)} -g {shell_quote(plan.owner_user)} {q_asset} {q_frame}",
            )
            if line
        )
        grant_asset_frame = f"sudo setfacl -R -m u:{plan.service_user}:rwx {q_asset} {q_frame}"
    install_listener_unit_parent = owned_directory_command(
        plan,
        owned_ancestor_dirs(
            plan.owner_home,
            f"{plan.owner_home}/.config/systemd/user",
            include_target=True,
        ),
    )
    q_owner_user = shell_quote(plan.owner_user)
    q_board_env_file = shell_quote(f"{plan.owner_home}/.config/{plan.project}/ticket-board.env")
    q_owner_config_dir = shell_quote(f"{plan.owner_home}/.config")
    q_board_env_dir = shell_quote(f"{plan.owner_home}/.config/{plan.project}")
    install_board_env_parent = "\n".join(
        [
            f"sudo install -d -m 0755 -o {q_owner_user} -g {q_owner_user} {q_owner_config_dir}",
            f"sudo install -d -m 0700 -o {q_owner_user} -g {q_owner_user} {q_board_env_dir}",
        ]
    )
    if plan.board_service_traversal:
        grant_board_root = "\n".join(
            [
                f"sudo setfacl -R -m u:{plan.service_user}:rx {q_board_root}",
                f"sudo find {q_board_root} -type d -exec setfacl -m d:u:{plan.service_user}:rx {{}} +",
            ]
        )
        grant_home_traversal = "\n".join(
            [
                f"sudo setfacl -m u:{plan.service_user}:--x {shell_quote(plan.owner_home)}",
                f"sudo setfacl -m u:{plan.service_user}:--x {shell_quote(plan.owner_home + '/.claude')}",
            ]
        )
        effective_grant_asset_frame = grant_asset_frame
    else:
        grant_board_root = (
            f"# board_service_traversal=false: not granting {plan.service_user} ACLs on "
            "the owner home, board release, assets, or frames."
        )
        grant_home_traversal = (
            f"# {plan.service_user} must already be able to traverse/read/write configured board paths, "
            "or the board health check will fail."
        )
        effective_grant_asset_frame = ""
    # Before the traversal grant rather than after it: the tenant tree is closed
    # first, and only then is anything allowed to walk through the home. On a
    # tenant already provisioned this is the repair, and re-running it changes
    # nothing (SYRD-156).
    confine_source_tree = (
        "\n".join(
            tenant_source_confinement_commands(
                owner_user=plan.owner_user,
                owner_home=plan.owner_home,
                checkout=plan.project_repository,
            )
        )
        if plan.project_repository
        else ""
    ) or (
        f"# the checkout {plan.project_repository} is outside {plan.owner_home}; "
        "it is not this tenant's tree to confine"
        if plan.project_repository
        else (
            "# this plan does not record where this tenant's checkout is, so there is nothing "
            "to confine here; `switchyard upgrade` and `switchyard resume-provision` record it "
            "from the generated configuration's own location"
        )
    )
    # The commit store the board will resolve against, closed and then granted
    # to the service by name, read-only. Closed first, because `chmod`
    # recomputes the ACL mask and would clip a grant made before it. `install
    # -d` also guarantees the directory exists, so the grant cannot fail on a
    # fresh tenant whose bare repository has not been cloned yet -- git clones
    # into an existing empty directory quite happily (SYRD-157).
    commit_stores = [
        entry for entry in str(plan.commit_git_dir).split(os.pathsep) if entry.strip()
    ]
    repository_boundary_lines: list[str] = list(
        repository_copy_confinement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            repositories=commit_stores,
            mode=WRITABLE_REPOSITORY_COPY_MODE,
        )
    )
    if plan.board_service_traversal:
        for store in commit_stores:
            repository_boundary_lines.extend(
                commit_store_read_commands(
                    owner_home=plan.owner_home,
                    service_user=plan.service_user,
                    commit_git_dir=store,
                )
            )
    # The worktree base, which SYRD-156 closed one directory over and this did
    # not: closed first, then the repository group granted for a tenant whose
    # roles have their own accounts, and only then the socket group retired --
    # so nothing loses access in the gap between taking one grant away and
    # making the one that replaces it (SYRD-171).
    worktree_base = tenant_worktree_base(plan)
    control_repository = tenant_control_repository(plan)
    repository_boundary_lines.extend(
        tenant_worktree_confinement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            worktree_base=worktree_base,
            worktrees=[path for _role, path in plan.role_worktrees],
        )
    )
    if plan.role_accounts:
        repository_boundary_lines.extend(
            repository_group_commands(
                plan.project,
                [plan.owner_user, *(account for _role, account in plan.role_accounts)],
            )
        )
        repository_boundary_lines.extend(
            role_worktree_access_commands(
                owner_home=plan.owner_home,
                repository_group=repository_group_name(plan.project),
                worktree_base=worktree_base,
                control_repository=control_repository,
            )
        )
    repository_boundary_lines.extend(
        socket_group_retirement_commands(
            owner_user=plan.owner_user,
            owner_home=plan.owner_home,
            socket_group=roles_group_name(plan.project),
            worktree_base=worktree_base,
            control_repository=control_repository,
        )
    )
    repository_boundary = "\n".join(
        [
            REPOSITORY_BOUNDARY_BEGIN,
            *(
                repository_boundary_lines
                or [
                    f"# the commit store {plan.commit_git_dir} is outside {plan.owner_home}; "
                    "this tenant grants the board service nothing there"
                ]
            ),
            REPOSITORY_BOUNDARY_END,
        ]
    )
    if enable_owner_linger:
        owner_linger_step = f"sudo loginctl enable-linger {q_owner_user}"
        owner_bus_error = (
            f"ERROR: user bus for {plan.owner_user} did not appear at $owner_bus within 30s after enable-linger"
        )
    else:
        owner_linger_step = (
            f"sudo loginctl show-user {q_owner_user} -p Linger --value 2>/dev/null | grep -qx yes || "
            f"{{ echo \"ERROR: linger is not enabled for {plan.owner_user}; switchyard refuses to modify an existing owner user\" >&2; exit 1; }}"
        )
        owner_bus_error = (
            f"ERROR: user bus for {plan.owner_user} did not appear at $owner_bus; enable linger or start that user's systemd manager"
        )
    return f"""#!/usr/bin/env bash
set -euo pipefail

# Review generated artifacts first. These commands require host privileges.
provision_dir="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
system_unit_candidate="$provision_dir/{plan.board_unit}"
canary_unit_candidate="$provision_dir/{plan.canary_unit}"
# The deployer runs as the project account and has to read the reviewed unit to
# compare it against the one systemd loaded. Root publishes a copy it can reach,
# rather than the account being sent into a directory root owns (SYRD-127).
{system_unit_proof}
readable_system_unit={q_readable_system_unit}
{service_user_command(plan.service_user)}
{role_accounts_command(plan)}
{peer_auth_command(plan)}
{install_board_root}
{grant_board_root}
{install_asset_frame}
{confine_source_tree}
{repository_boundary}
{grant_home_traversal}
{effective_grant_asset_frame}
# The deploy exports an immutable release into the board root and then starts a
# canary AS THE SERVICE ACCOUNT, so every grant that account needs is made
# above it rather than below. It used to run first: on a host where the tree
# already carried the grants the deploy succeeded and the grants below it were
# a no-op, and on a fresh one the export landed at 0750 with no named entry and
# no default to inherit, the canary could not traverse its own release, and the
# packet died before reaching the line that would have fixed it. The default
# ACL set above is what carries the grant into releases that do not exist yet
# (SYRD-145).
sudo -u {q_owner_user} -H env HOME={q_owner_home} TICKET_BOARD_OWNER_HOME={q_owner_home} TICKET_BOARD_PROJECT={shell_quote(plan.project)} TICKET_BOARD_COMMIT_GIT_DIR={q_commit_git_dir} TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="$readable_system_unit" SOURCE_REPO={q_source_repo} BOARD_ROOT={q_board_root} DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 {q_deploy_script} deploy
{install_board_env_parent}
if ! sudo -u {q_owner_user} test -s {q_board_env_file}; then
    report_token="$(/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'TICKET_BOARD_TENANT_REPORT_TOKEN=%s\\n' "$report_token" | sudo -u {q_owner_user} tee {q_board_env_file} >/dev/null
    sudo -u {q_owner_user} chmod 0600 {q_board_env_file}
fi
sudo install -m 0644 "$system_unit_candidate" {q_board_unit}
sudo install -m 0644 "$canary_unit_candidate" {q_canary_unit}
sudo install -m 0644 {packet_companion(plan.tmpfiles_name)} {q_tmpfiles}
sudo install -m 0644 {packet_companion(plan.polkit_name)} {q_polkit}
{install_role_control_sudoers}
# The owner's GitHub identity, and the configuration that selects it. Without
# the selection git offers no key at all and every push fails as though there
# were no credential (SYRD-74). Re-runnable: an existing key is left alone.
# This is the whole credential story for a new project: the account every role
# runs as holds it, implementers publish candidates by pushing, and nothing on
# this host runs as root to do it (SYRD-123).
{github_identity}
sudo systemd-tmpfiles --create {q_tmpfiles}
{postgres_sql_file_command(plan.project + '-database.sql', companion=True)}
{postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/schema.sql', database_url=plan.admin_database_url)}
sudo env TICKET_BOARD_ADMIN_DATABASE_URL={shell_quote(plan.admin_database_url)} {shell_quote(plan.board_current + '/scripts/ticket-board-migrate')}
{workflow_seed_command.rstrip()}
{postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/rbac.sql', database_url=plan.admin_database_url)}
sudo systemctl daemon-reload
sudo systemctl enable --now {plan.board_unit}
{install_listener_unit_parent}
sudo install -m 0644 -o {q_owner_user} -g {q_owner_user} {packet_companion(plan.listener_unit)} {q_listener_unit}
{owner_linger_step}
owner_uid="$(id -u {q_owner_user})"
owner_runtime_dir="/run/user/$owner_uid"
owner_bus="$owner_runtime_dir/bus"
for _ in $(seq 1 300); do
    [ -S "$owner_bus" ] && break
    sleep 0.1
done
if [ ! -S "$owner_bus" ]; then
    echo {shell_quote(owner_bus_error)} >&2
    exit 1
fi
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user daemon-reload
sudo -u {q_owner_user} -H env XDG_RUNTIME_DIR="$owner_runtime_dir" TICKET_BOARD_PROJECT={shell_quote(plan.project)} TICKET_BOARD_PANE_STATE_DIR="$owner_runtime_dir/{plan.runtime_directory}/pane-state" TICKET_BOARD_PANE_SESSION_DIR={q_pane_session_dir} {q_hook_installer} install --home {q_owner_home} --hook-source {q_hook_source} --bin-path {q_hook_bin} --seed-codex-hook-trust-if-new
sudo -u {q_owner_user} -H {q_board_skill_installer} install --home {q_owner_home}
{role_runtime_step}
{install_tenant_control}
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user enable --now {plan.listener_unit}
curl -fsS http://127.0.0.1:{plan.port}/api/board >/dev/null
"""


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
