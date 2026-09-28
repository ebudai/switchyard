"""Reading the design artifact a new project is created from, and refusing what it may not say.

`load_project_design_artifact` reads one artifact file and returns the
project's `ProjectDesignArtifact`, or refuses with the exact reason:

- a document that is not the design schema, or that tries to preconfigure
  stages, a workflow or extra implementer roles (`PROJECT_DESIGN_FORBIDDEN_KEYS`,
  at the top level or inside `project`; `_artifact_forbidden_keys`);
- a project whose slug is invalid or is not the one requested, or whose ticket
  prefix, owner, repository, remote, branch or worktree policy is missing or
  malformed (`_artifact_string`, `_artifact_optional_string`);
- implementer and audit roles that are reserved, malformed or overlapping
  (`_artifact_role_list`, `_artifact_audit_role_list`), CLI choices for
  unknown roles or unsupported CLIs (`_artifact_role_cli_pairs`), and model or
  effort maps that are not maps of strings (`_artifact_role_value_pairs`);
- gates and capability grants of the wrong type, or an agy credential source
  that is not a plain user name or origin (`_artifact_bool_mapping`,
  `_artifact_capability_grants`, `AGY_SOURCE_ORIGINS`).

Moved out of `scripts/team_launcher.py` unchanged (SYRD-420), in their original
order. The launcher imports this module and re-exports all twelve names;
`new_project_command` and `scripts/new_project_phases.py` still reach the
loader through the launcher. Everything the twelve read -- each other and the
two constants included, and the schema, defaults, reserved roles, worktree
policies, result type and the launcher's validators and JSON and path helpers --
is read through the launcher at call time, so a suite that rebinds one there
still intercepts it. No default is bound from another module; the result type
is imported under TYPE_CHECKING. This module imports `team_launcher` only
inside the functions, when they run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectDesignArtifact


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


def _artifact_forbidden_keys(raw: dict[str, Any]) -> list[str]:
    from scripts import team_launcher as launcher

    found = [key for key in raw if key in launcher.PROJECT_DESIGN_FORBIDDEN_KEYS]
    project_raw = raw.get("project")
    if isinstance(project_raw, dict):
        found.extend(f"project.{key}" for key in project_raw if key in launcher.PROJECT_DESIGN_FORBIDDEN_KEYS)
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
    from scripts import team_launcher as launcher

    if raw is None:
        return tuple(defaults)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} field 'project.audit_roles' must be a JSON string list")
    result: list[str] = []
    for item in raw:
        role = item.strip().lower()
        if not launcher.ROLE_RE.fullmatch(role):
            raise SystemExit(f"{path} field 'project.audit_roles' role {role!r} must match ^[a-z][a-z0-9_-]{{0,63}}$")
        if role in launcher.NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
            raise SystemExit(f"{path} field 'project.audit_roles' contains reserved role {role!r}")
        if role not in result:
            result.append(role)
    return tuple(result)


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
    from scripts import team_launcher as launcher

    defaults = launcher._default_role_cli_pairs(
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
        pairs_by_role[role] = launcher._validate_new_project_cli(raw_cli, context=f"CLI for {role}")
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
    from scripts import team_launcher as launcher

    if raw is None:
        return dict(launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'capability_grants' must be a JSON object")
    result = dict(launcher.PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
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
            if candidate and not launcher._is_valid_owner_user_name(candidate):
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source' must be a plain Unix "
                    f"user name, not {candidate!r}"
                )
            result[key] = candidate
        elif key == "agy_credential_source_origin":
            if not isinstance(value, str) or value.strip() not in launcher.AGY_SOURCE_ORIGINS:
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source_origin' must be "
                    f"one of {', '.join(sorted(launcher.AGY_SOURCE_ORIGINS))}"
                )
            result[key] = value.strip()
        elif not isinstance(value, bool):
            raise SystemExit(f"{path} field 'capability_grants.{key}' must be a JSON boolean")
        else:
            result[key] = value
    return result


def load_project_design_artifact(path: Path, *, expected_project: str | None = None) -> ProjectDesignArtifact:
    from scripts import team_launcher as launcher

    artifact_path = path.expanduser().resolve(strict=False)
    raw = launcher._load_json(artifact_path)
    forbidden = launcher._artifact_forbidden_keys(raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    schema = launcher._artifact_string(raw, "schema", path=artifact_path)
    if schema != launcher.PROJECT_DESIGN_ARTIFACT_SCHEMA:
        raise SystemExit(
            f"{artifact_path} schema must be {launcher.PROJECT_DESIGN_ARTIFACT_SCHEMA!r}, got {schema!r}"
        )
    project_raw = raw.get("project")
    if not isinstance(project_raw, dict):
        raise SystemExit(f"{artifact_path} field 'project' must be a JSON object")
    forbidden = launcher._artifact_forbidden_keys(project_raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    slug = launcher._validate_project_slug(launcher._artifact_string(project_raw, "slug", path=artifact_path))
    if expected_project is not None and slug != expected_project:
        raise SystemExit(f"{artifact_path} project slug {slug!r} does not match requested project {expected_project!r}")
    project_name = launcher._artifact_optional_string(project_raw, "name", path=artifact_path, default=slug)
    ticket_prefix = launcher.validate_ticket_prefix(launcher._artifact_string(project_raw, "ticket_prefix", path=artifact_path))
    owner_user = launcher._artifact_string(project_raw, "owner_user", path=artifact_path)
    repository = launcher._expand_path(launcher._artifact_string(project_raw, "repository", path=artifact_path), base=artifact_path.parent)
    remote = launcher._artifact_string(project_raw, "remote", path=artifact_path, default="origin")
    default_branch = launcher._artifact_string(project_raw, "default_branch", path=artifact_path, default="main")
    worktree_policy = launcher._artifact_string(project_raw, "worktree_policy", path=artifact_path, default="shared")
    if worktree_policy not in launcher.WORKTREE_POLICIES:
        raise SystemExit(f"{artifact_path} field 'project.worktree_policy' must be one of {sorted(launcher.WORKTREE_POLICIES)}")
    design_document = launcher._expand_path(launcher._artifact_string(raw, "design_document", path=artifact_path), base=artifact_path.parent)
    implementer_roles = launcher._artifact_role_list(
        project_raw.get("roles", project_raw.get("implementer_roles")),
        path=artifact_path,
        field="project.roles",
        defaults=launcher.DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    )
    include_designer_raw = project_raw.get("include_designer", True)
    if not isinstance(include_designer_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_designer' must be a JSON boolean")
    include_audit_raw = project_raw.get("include_audit", True)
    if not isinstance(include_audit_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_audit' must be a JSON boolean")
    audit_roles = launcher._artifact_audit_role_list(
        project_raw.get("audit_roles"),
        path=artifact_path,
        defaults=("audit",) if include_audit_raw else (),
    )
    role_overlap = set(implementer_roles) & set(audit_roles)
    if role_overlap:
        raise SystemExit(
            f"{artifact_path} roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}"
        )
    role_clis = launcher._artifact_role_cli_pairs(
        project_raw.get("role_clis"),
        path=artifact_path,
        implementer_roles=implementer_roles,
        include_designer=include_designer_raw,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
    )
    role_models = launcher._artifact_role_value_pairs(
        project_raw.get("role_models"), path=artifact_path, field="project.role_models"
    )
    role_efforts = launcher._artifact_role_value_pairs(
        project_raw.get("role_efforts"), path=artifact_path, field="project.role_efforts"
    )
    catalog_version = project_raw.get("catalog_version")
    push_policy = launcher._artifact_string(project_raw, "push_policy", path=artifact_path, default="director-main-only")
    gates = launcher._artifact_bool_mapping(project_raw.get("gates"), path=artifact_path, field="project.gates", defaults=launcher.PROJECT_DESIGN_DEFAULT_GATES)
    capability_grants = launcher._artifact_capability_grants(project_raw.get("capability_grants"), path=artifact_path)
    return launcher.ProjectDesignArtifact(
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
