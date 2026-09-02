"""Render per-project ticket-board provisioning artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

try:
    from .commit_repos import commit_git_dir_env_for_project
except ImportError:  # pragma: no cover - supports direct script execution
    from commit_repos import commit_git_dir_env_for_project


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
SCHEMA_SQL_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_SHARED_PYTHON = "/opt/switchyard/venv/bin/python"

# Tenant boards intentionally expose a smaller workflow surface than the pgu
# operations board. Keep this as a projection policy over schema.sql, not as a
# separately maintained transition table.
TENANT_WORKFLOW_EXCLUDED_STAGES = frozenset({"backlog", "inspection"})
TENANT_WORKFLOW_EXCLUDED_ACTIONS = frozenset(
    {"defer", "request_commit_exempt", "start_task", "submit_to_inspection"}
)


@dataclass(frozen=True)
class ProjectBoardProvision:
    project: str
    ticket_prefix: str
    owner_user: str
    service_user: str
    board_unit: str
    listener_unit: str
    tmpfiles_name: str
    polkit_name: str
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


@dataclass(frozen=True)
class WorkflowStageSeed:
    name: str
    display_label: str
    rank: int
    owner_roles: tuple[str, ...]
    entry_gate_field: str | None
    gate_skip_to: str | None
    exit_signoff_field: str | None
    is_terminal: bool


@dataclass(frozen=True)
class WorkflowTransitionSeed:
    from_stage: str
    to_stage: str
    action_name: str
    allowed_roles: tuple[str, ...]
    owner_scoped: bool
    director_override: bool


def _validate_project(value: str) -> str:
    project = value.strip().lower()
    if not PROJECT_RE.fullmatch(project):
        raise SystemExit("project must match ^[a-z0-9][a-z0-9_]{0,39}$")
    return project


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
    owner_user: str,
    port: int | None = None,
    database: str | None = None,
    service_user: str = "boardsvc",
    service_role: str = "ticket_board_service",
    listener_role: str = "ticket_board_listener",
    board_root: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: Path | None = None,
    asset_dir: Path | None = None,
    frame_dir: Path | None = None,
    implementer_roles: Sequence[str] | None = None,
    ticket_prefix: str | None = None,
    board_service_traversal: bool = True,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    vcs_close_role: str | None = None,
) -> ProjectBoardProvision:
    project = _validate_project(project)
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
    home = Path("/home") / owner_user
    resolved_board_root = board_root or home / f"{project}-ticketboard-live"
    resolved_source_repo = source_repo or Path(__file__).resolve().parents[2]
    resolved_commit_git_dir = str(commit_git_dir) if commit_git_dir is not None else commit_git_dir_env_for_project(project=project, owner_home=home)
    resolved_asset_dir = asset_dir or home / ".claude" / f"{project}-tickets-assets"
    if frame_dir is not None:
        resolved_frame_dir = frame_dir
    elif project == "pgu":
        resolved_frame_dir = Path("/tmp/pgu-frames")
    else:
        resolved_frame_dir = home / ".claude" / f"{project}-ticket-frames"
    resolved_database = database or ("pgu" if project == "pgu" else f"{ident}_ticket_board")
    resolved_port = port if port is not None else allocated_port(project)
    if not (1 <= resolved_port <= 65535):
        raise SystemExit("port must be between 1 and 65535")
    board_current = resolved_board_root / "current"
    return ProjectBoardProvision(
        project=project,
        ticket_prefix=resolved_ticket_prefix,
        owner_user=owner_user,
        service_user=service_user,
        board_unit=f"{unit_prefix}.service",
        listener_unit=f"{unit_prefix}-notify-listener.service",
        tmpfiles_name=f"{unit_prefix}.conf",
        polkit_name=f"49-{unit_prefix}-deploy.rules",
        runtime_directory=runtime_directory,
        socket_path=f"/run/{runtime_directory}/ticket-board.sock",
        port=resolved_port,
        database=resolved_database,
        service_role=service_role,
        listener_role=listener_role,
        board_root=str(resolved_board_root),
        board_current=str(board_current),
        source_repo=str(resolved_source_repo),
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
    )


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def postgres_sql_file_command(sql_file: str, *, database_url: str = "") -> str:
    command = "sudo cat " + shell_quote(sql_file)
    command += " | sudo -u postgres psql -X -v ON_ERROR_STOP=1"
    if database_url:
        command += " " + shell_quote(database_url)
    command += " -f -"
    return command


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_text_array(values: Sequence[str]) -> str:
    return "ARRAY[" + ", ".join(sql_literal(value) for value in values) + "]::text[]"


def _schema_sql_text(schema_sql: str | None = None) -> str:
    if schema_sql is not None:
        return schema_sql
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")


def _insert_values_block(schema_sql: str, table: str) -> str:
    match = re.search(
        rf"INSERT INTO ticket_board\.{re.escape(table)}\s*\([^;]+?\)\s*VALUES\s*(.*?)\nON CONFLICT",
        schema_sql,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(f"could not find ticket_board.{table} seed INSERT in schema.sql")
    return match.group(1)


def _split_sql_tuple_rows(values_sql: str) -> tuple[str, ...]:
    rows: list[str] = []
    depth = 0
    start: int | None = None
    in_quote = False
    index = 0
    while index < len(values_sql):
        char = values_sql[index]
        if char == "'":
            if in_quote and index + 1 < len(values_sql) and values_sql[index + 1] == "'":
                index += 2
                continue
            in_quote = not in_quote
        elif not in_quote:
            if char == "(":
                if depth == 0:
                    start = index + 1
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    if start is None:
                        raise ValueError("malformed SQL tuple row")
                    rows.append(values_sql[start:index].strip())
                    start = None
        index += 1
    if depth != 0 or in_quote:
        raise ValueError("unterminated SQL values block")
    return tuple(rows)


def _split_sql_fields(row_sql: str) -> tuple[str, ...]:
    fields: list[str] = []
    start = 0
    bracket_depth = 0
    in_quote = False
    index = 0
    while index < len(row_sql):
        char = row_sql[index]
        if char == "'":
            if in_quote and index + 1 < len(row_sql) and row_sql[index + 1] == "'":
                index += 2
                continue
            in_quote = not in_quote
        elif not in_quote:
            if char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth -= 1
            elif char == "," and bracket_depth == 0:
                fields.append(row_sql[start:index].strip())
                start = index + 1
        index += 1
    fields.append(row_sql[start:].strip())
    return tuple(fields)


def _parse_sql_string(token: str) -> str:
    stripped = token.strip()
    if not (stripped.startswith("'") and stripped.endswith("'")):
        raise ValueError(f"expected SQL string literal, got {token!r}")
    return stripped[1:-1].replace("''", "'")


def _parse_sql_nullable_string(token: str) -> str | None:
    stripped = token.strip()
    if stripped.upper() == "NULL":
        return None
    return _parse_sql_string(stripped)


def _parse_sql_bool(token: str) -> bool:
    stripped = token.strip().lower()
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    raise ValueError(f"expected SQL boolean, got {token!r}")


def _parse_sql_text_array(token: str) -> tuple[str, ...]:
    stripped = token.strip()
    match = re.fullmatch(r"ARRAY\[(.*)\]::text\[]", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"expected SQL text array, got {token!r}")
    inner = match.group(1).strip()
    if not inner:
        return ()
    return tuple(_parse_sql_string(field) for field in _split_sql_fields(inner))


def schema_workflow_stages(schema_sql: str | None = None) -> tuple[WorkflowStageSeed, ...]:
    rows = _split_sql_tuple_rows(_insert_values_block(_schema_sql_text(schema_sql), "workflow_stages"))
    stages: list[WorkflowStageSeed] = []
    for row in rows:
        fields = _split_sql_fields(row)
        if len(fields) != 8:
            raise ValueError(f"workflow_stages seed row has {len(fields)} fields, expected 8: {row}")
        stages.append(
            WorkflowStageSeed(
                name=_parse_sql_string(fields[0]),
                display_label=_parse_sql_string(fields[1]),
                rank=int(fields[2]),
                owner_roles=_parse_sql_text_array(fields[3]),
                entry_gate_field=_parse_sql_nullable_string(fields[4]),
                gate_skip_to=_parse_sql_nullable_string(fields[5]),
                exit_signoff_field=_parse_sql_nullable_string(fields[6]),
                is_terminal=_parse_sql_bool(fields[7]),
            )
        )
    return tuple(stages)


def schema_workflow_transitions(schema_sql: str | None = None) -> tuple[WorkflowTransitionSeed, ...]:
    rows = _split_sql_tuple_rows(_insert_values_block(_schema_sql_text(schema_sql), "workflow_transitions"))
    transitions: list[WorkflowTransitionSeed] = []
    for row in rows:
        fields = _split_sql_fields(row)
        if len(fields) != 6:
            raise ValueError(f"workflow_transitions seed row has {len(fields)} fields, expected 6: {row}")
        transitions.append(
            WorkflowTransitionSeed(
                from_stage=_parse_sql_string(fields[0]),
                to_stage=_parse_sql_string(fields[1]),
                action_name=_parse_sql_string(fields[2]),
                allowed_roles=_parse_sql_text_array(fields[3]),
                owner_scoped=_parse_sql_bool(fields[4]),
                director_override=_parse_sql_bool(fields[5]),
            )
        )
    return tuple(transitions)


def _project_implementation_owner_roles(plan: ProjectBoardProvision) -> tuple[str, ...]:
    return (
        ("main", *(role for role in plan.implementer_roles if role != "main"))
        if "main" in plan.implementer_roles
        else plan.implementer_roles
    )


def _project_workflow_owner_roles(stage: WorkflowStageSeed, plan: ProjectBoardProvision) -> tuple[str, ...]:
    if plan.workflow_seed == "pgu-full":
        return stage.owner_roles
    if stage.name == "draft":
        return plan.draft_roles
    if stage.name == "in_progress":
        return _project_implementation_owner_roles(plan)
    if stage.name == "audit":
        return plan.audit_roles
    return stage.owner_roles


def _project_workflow_allowed_roles(roles: Sequence[str], plan: ProjectBoardProvision) -> tuple[str, ...]:
    if plan.workflow_seed == "pgu-full":
        return tuple(roles)
    result: list[str] = []
    inserted_implementers = False
    for role in roles:
        if role in DEFAULT_IMPLEMENTER_ROLES:
            if not inserted_implementers:
                result.extend(plan.implementer_roles)
                inserted_implementers = True
            continue
        if role == "audit":
            result.extend(plan.audit_roles)
            continue
        result.append(role)
    return _dedupe(result)


def _rank_project_stages(stages: Sequence[WorkflowStageSeed]) -> tuple[WorkflowStageSeed, ...]:
    result: list[WorkflowStageSeed] = []
    next_rank = 0
    terminal_rank_offset = 1 if any(stage.name == "vcs" for stage in stages) else 0
    for stage in stages:
        if stage.is_terminal:
            result.append(
                WorkflowStageSeed(
                    name=stage.name,
                    display_label=stage.display_label,
                    rank=stage.rank + terminal_rank_offset,
                    owner_roles=stage.owner_roles,
                    entry_gate_field=stage.entry_gate_field,
                    gate_skip_to=stage.gate_skip_to,
                    exit_signoff_field=stage.exit_signoff_field,
                    is_terminal=stage.is_terminal,
                )
            )
            continue
        result.append(
            WorkflowStageSeed(
                name=stage.name,
                display_label=stage.display_label,
                rank=next_rank,
                owner_roles=stage.owner_roles,
                entry_gate_field=stage.entry_gate_field,
                gate_skip_to=stage.gate_skip_to,
                exit_signoff_field=stage.exit_signoff_field,
                is_terminal=stage.is_terminal,
            )
        )
        next_rank += 1
    return tuple(result)


def project_workflow_stages(
    plan: ProjectBoardProvision, *, schema_sql: str | None = None
) -> tuple[WorkflowStageSeed, ...]:
    source_stages = schema_workflow_stages(schema_sql)
    if plan.workflow_seed == "pgu-full":
        return source_stages

    excluded_stages = set(TENANT_WORKFLOW_EXCLUDED_STAGES)
    if not plan.audit_roles:
        excluded_stages.update({"audit", "dat", "user_review"})

    has_vcs_close = bool(dict(plan.operation_allowed_roles).get("mark_done"))
    vcs_close_role = dict(plan.operation_allowed_roles).get("mark_done", ("",))[0] if has_vcs_close else ""
    projected: list[WorkflowStageSeed] = []
    for stage in source_stages:
        if stage.name in excluded_stages:
            continue
        if has_vcs_close and stage.name == "done":
            projected.append(
                WorkflowStageSeed(
                    name="vcs",
                    display_label="VCS",
                    rank=stage.rank,
                    owner_roles=(vcs_close_role,),
                    entry_gate_field=None,
                    gate_skip_to=None,
                    exit_signoff_field=None,
                    is_terminal=False,
                )
            )
        projected.append(
            WorkflowStageSeed(
                name=stage.name,
                display_label=stage.display_label,
                rank=stage.rank,
                owner_roles=_project_workflow_owner_roles(stage, plan),
                entry_gate_field=stage.entry_gate_field,
                gate_skip_to=None if stage.gate_skip_to in excluded_stages else stage.gate_skip_to,
                exit_signoff_field=stage.exit_signoff_field,
                is_terminal=stage.is_terminal,
            )
        )
    return _rank_project_stages(projected)


def project_workflow_transitions(
    plan: ProjectBoardProvision, *, schema_sql: str | None = None
) -> tuple[WorkflowTransitionSeed, ...]:
    if plan.workflow_seed == "pgu-full":
        return schema_workflow_transitions(schema_sql)

    stage_names = {stage.name for stage in project_workflow_stages(plan, schema_sql=schema_sql)}
    include_audit = bool(plan.audit_roles)
    mark_done_roles = dict(plan.operation_allowed_roles).get("mark_done", ())
    has_vcs_close = bool(mark_done_roles)
    transitions: list[WorkflowTransitionSeed] = []
    for transition in schema_workflow_transitions(schema_sql):
        if transition.action_name in TENANT_WORKFLOW_EXCLUDED_ACTIONS:
            continue
        from_stage = transition.from_stage
        to_stage = transition.to_stage
        if (
            not include_audit
            and transition.from_stage == "in_progress"
            and transition.to_stage == "audit"
            and transition.action_name == "submit_to_audit"
        ):
            to_stage = "director_review"
        if has_vcs_close and (from_stage, to_stage, transition.action_name) == (
            "director_review",
            "done",
            "mark_done",
        ):
            continue
        if from_stage not in stage_names or to_stage not in stage_names:
            continue
        allowed_roles = _project_workflow_allowed_roles(transition.allowed_roles, plan)
        owner_scoped = transition.owner_scoped
        if from_stage == "audit" and transition.action_name in {"audit_sign_off", "audit_kick_back"}:
            owner_scoped = True
        if transition.action_name == "release_draft":
            allowed_roles = _dedupe((*plan.draft_roles, *allowed_roles))
        transitions.append(
            WorkflowTransitionSeed(
                from_stage=from_stage,
                to_stage=to_stage,
                action_name=transition.action_name,
                allowed_roles=allowed_roles,
                owner_scoped=owner_scoped,
                director_override=transition.director_override,
            )
        )
    if has_vcs_close:
        transitions.extend(
            [
                WorkflowTransitionSeed("director_review", "vcs", "route", ("director",), False, False),
                WorkflowTransitionSeed("vcs", "done", "mark_done", mark_done_roles, False, False),
            ]
        )
    return tuple(transitions)


def env_list(values: Sequence[str]) -> str:
    return ",".join(values)


def env_operation_role_map(values: Sequence[tuple[str, Sequence[str]]]) -> str:
    return ";".join(f"{operation}={env_list(roles)}" for operation, roles in values)


def owned_ancestor_dirs(owner_user: str, target: str, *, include_target: bool) -> tuple[str, ...]:
    home = PurePosixPath("/home") / owner_user
    path = PurePosixPath(target)
    try:
        path.relative_to(home)
    except ValueError:
        return ()
    end = path if include_target else path.parent
    if end == home:
        return ()
    relative_parts = end.relative_to(home).parts
    return tuple(str(home.joinpath(*relative_parts[:index])) for index in range(1, len(relative_parts) + 1))


def owned_directory_command(plan: ProjectBoardProvision, dirs: Sequence[str], *, mode: str = "0755") -> str:
    unique_dirs = _dedupe(tuple(dirs))
    if not unique_dirs:
        return ""
    quoted_dirs = " ".join(shell_quote(value) for value in unique_dirs)
    return (
        f"sudo install -d -m {mode} -o {shell_quote(plan.owner_user)} "
        f"-g {shell_quote(plan.owner_user)} {quoted_dirs}"
    )


def default_ticket_board_python() -> str:
    override = os.environ.get("TICKET_BOARD_PYTHON")
    if override:
        return override
    shared_python = os.environ.get("SWITCHYARD_SHARED_PYTHON", DEFAULT_SHARED_PYTHON)
    if Path(shared_python).is_file() and os.access(shared_python, os.X_OK):
        return shared_python
    return "/usr/bin/python3"


def render_board_unit(plan: ProjectBoardProvision) -> str:
    operation_allowed_roles = env_operation_role_map(plan.operation_allowed_roles)
    operation_allowed_roles_line = (
        f"Environment=TICKET_BOARD_OPERATION_ALLOWED_ROLES={operation_allowed_roles}\n"
        if operation_allowed_roles
        else ""
    )
    return f"""[Unit]
Description={plan.project} Ticket Board
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={plan.service_user}
WorkingDirectory={plan.board_current}
RuntimeDirectory={plan.runtime_directory}
ExecStart={default_ticket_board_python()} {plan.board_current}/scripts/ticket-board.py --host 127.0.0.1 --port {plan.port} --unix-socket {plan.socket_path} --frames {plan.frame_dir} --assets {plan.asset_dir}
Restart=on-failure
RestartSec=2
EnvironmentFile=-/home/{plan.owner_user}/.config/{plan.project}/ticket-board.env
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/home/{plan.owner_user}
Environment=TICKET_BOARD_PROJECT={plan.project}
Environment=TICKET_BOARD_TICKET_PREFIX={plan.ticket_prefix}
Environment=TICKET_BOARD_COMMIT_GIT_DIR={plan.commit_git_dir}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.service_role}
Environment=TICKET_BOARD_SOCKET={plan.socket_path}
Environment=TICKET_BOARD_DATABASE_URL={plan.board_database_url}
Environment=TICKET_BOARD_DRAFT_ROLES={env_list(plan.draft_roles)}
Environment=TICKET_BOARD_IMPLEMENTER_ROLES={env_list(plan.implementer_roles)}
Environment=TICKET_BOARD_ASSIGNEES={env_list(plan.assignee_roles)}
Environment=TICKET_BOARD_CALLER_ROLES={env_list(plan.caller_roles)}
{operation_allowed_roles_line}NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths={plan.asset_dir} {plan.frame_dir}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def render_listener_unit(plan: ProjectBoardProvision) -> str:
    return f"""[Unit]
Description={plan.project} Ticket Board Notify Listener
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
WorkingDirectory={plan.board_current}
ExecStart={default_ticket_board_python()} {plan.board_current}/scripts/ticket-board-notify-listener
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=TICKET_BOARD_PROJECT={plan.project}
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE={plan.database}
Environment=PGUSER={plan.listener_role}
Environment=TICKET_BOARD_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_NOTIFY_DATABASE_URL={plan.listener_database_url}
Environment=TICKET_BOARD_PANE_STATE_DIR=%t/{plan.runtime_directory}/pane-state
EnvironmentFile=-%h/.config/{plan.project}/ticket-board-notify-listener.env
StandardOutput=append:{plan.listener_log}
StandardError=append:{plan.listener_log}

[Install]
WantedBy=default.target
"""


def render_tmpfiles(plan: ProjectBoardProvision) -> str:
    if plan.project == "pgu" and plan.frame_dir == "/tmp/pgu-frames":
        return "d /tmp/pgu-frames 1777 root root -\n"
    return f"d {plan.frame_dir} 0775 {plan.owner_user} {plan.owner_user} -\n"


def render_polkit_rule(plan: ProjectBoardProvision) -> str:
    return f"""// Allow {plan.owner_user} to deploy the {plan.project} board without blanket sudo.
// Scope is intentionally limited to fixed, root-owned board service units.
// Do NOT add org.freedesktop.systemd1.manage-unit-files here. On systemd
// policy, that action can imply both reload-daemon and manage-units, which
// turns this narrow per-unit grant into global daemon-reload plus unrestricted
// unit management. reload-daemon cannot be scoped to a unit; it carries no unit
// detail for polkit to inspect.
polkit.addRule(function(action, subject) {{
    if (subject.user !== "{plan.owner_user}") {{
        return polkit.Result.NOT_HANDLED;
    }}
    if (action.id !== "org.freedesktop.systemd1.manage-units") {{
        return polkit.Result.NOT_HANDLED;
    }}

    var unit = action.lookup("unit");
    var verb = action.lookup("verb");
    if (unit === "{plan.board_unit}") {{
        if (verb === "start" || verb === "stop" || verb === "restart") {{
            return polkit.Result.YES;
        }}
        return polkit.Result.NOT_HANDLED;
    }}

    if (unit === "{plan.project}-ticket-board-canary.service") {{
        if (verb === "start" || verb === "stop") {{
            return polkit.Result.YES;
        }}
    }}

    return polkit.Result.NOT_HANDLED;
}});
"""


def render_database_sql(plan: ProjectBoardProvision) -> str:
    db_ident = sql_identifier(plan.database)
    return f"""-- Bootstrap database container for {plan.project}.
-- Run as a PostgreSQL admin before applying schema.sql, migrations, and rbac.sql
-- inside database {plan.database}.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{plan.service_role}') THEN
        CREATE ROLE {plan.service_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{plan.listener_role}') THEN
        CREATE ROLE {plan.listener_role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

SELECT format('CREATE DATABASE %I', '{plan.database}')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '{plan.database}')\\gexec

COMMENT ON DATABASE {db_ident} IS 'ticket board database for project {plan.project}';
"""


def project_workflow_state_names(plan: ProjectBoardProvision) -> tuple[str, ...]:
    return tuple(stage.name for stage in project_workflow_stages(plan))


def render_project_role_constraint_sql(plan: ProjectBoardProvision) -> str:
    notification_roles = tuple(role for role in plan.assignee_roles if role not in {"unassigned", "agent", "user"})
    state_names = project_workflow_state_names(plan)
    return f"""
ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_name_check CHECK (name IN ({", ".join(sql_literal(state) for state in state_names)}));

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_state_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_state_check CHECK (state IN ({", ".join(sql_literal(state) for state in state_names)}));

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN ({", ".join(sql_literal(role) for role in plan.assignee_roles)}));

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_awaiting_role_check
    CHECK (
        awaiting_role = ''
        OR awaiting_role IN ({", ".join(sql_literal(role) for role in notification_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_last_implementer_assignee_check
    CHECK (
        last_implementer_assignee = ''
        OR last_implementer_assignee IN ({", ".join(sql_literal(role) for role in plan.implementer_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_target_role_check
    CHECK (target_role IN ({", ".join(sql_literal(role) for role in notification_roles)}));
"""


def render_workflow_sql(plan: ProjectBoardProvision, *, schema_sql: str | None = None) -> str:
    if plan.workflow_seed == "pgu-full":
        return f"""-- {plan.project} keeps the full workflow seeded by schema.sql.
-- No per-project workflow override is applied.
"""

    stages = project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(name),
                sql_literal(label),
                str(rank),
                sql_text_array(owner_roles),
                "NULL" if entry_gate_field is None else sql_literal(entry_gate_field),
                "NULL" if gate_skip_to is None else sql_literal(gate_skip_to),
                "NULL" if exit_signoff_field is None else sql_literal(exit_signoff_field),
                "true" if is_terminal else "false",
            ]
        )
        + ")"
        for name, label, rank, owner_roles, entry_gate_field, gate_skip_to, exit_signoff_field, is_terminal in (
            (
                stage.name,
                stage.display_label,
                stage.rank,
                stage.owner_roles,
                stage.entry_gate_field,
                stage.gate_skip_to,
                stage.exit_signoff_field,
                stage.is_terminal,
            )
            for stage in stages
        )
    )
    transition_rows = ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(from_stage),
                sql_literal(to_stage),
                sql_literal(action_name),
                sql_text_array(allowed_roles),
                "true" if owner_scoped else "false",
                "true" if director_override else "false",
            ]
        )
        + ")"
        for from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override in (
            (
                transition.from_stage,
                transition.to_stage,
                transition.action_name,
                transition.allowed_roles,
                transition.owner_scoped,
                transition.director_override,
            )
            for transition in transitions
        )
    )
    return f"""-- Seed the default project workflow for {plan.project}.
-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ticket_board.tickets) THEN
        RAISE EXCEPTION 'project workflow seed must run before tickets exist';
    END IF;
END;
$$;

DELETE FROM ticket_board.workflow_transitions;
UPDATE ticket_board.workflow_stages SET gate_skip_to = NULL WHERE gate_skip_to IS NOT NULL;
DELETE FROM ticket_board.workflow_stages;

{render_project_role_constraint_sql(plan)}

INSERT INTO ticket_board.workflow_stages (
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
) VALUES
{stage_rows};

INSERT INTO ticket_board.workflow_transitions (
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
) VALUES
{transition_rows};

COMMIT;
"""


def _workflow_stage_rows_sql(stages: Sequence[WorkflowStageSeed]) -> str:
    return ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(stage.name),
                sql_literal(stage.display_label),
                str(stage.rank),
                sql_text_array(stage.owner_roles),
                "NULL" if stage.entry_gate_field is None else sql_literal(stage.entry_gate_field),
                "NULL" if stage.gate_skip_to is None else sql_literal(stage.gate_skip_to),
                "NULL" if stage.exit_signoff_field is None else sql_literal(stage.exit_signoff_field),
                "true" if stage.is_terminal else "false",
            ]
        )
        + ")"
        for stage in stages
    )


def _workflow_transition_rows_sql(transitions: Sequence[WorkflowTransitionSeed]) -> str:
    return ",\n".join(
        "    ("
        + ", ".join(
            [
                sql_literal(transition.from_stage),
                sql_literal(transition.to_stage),
                sql_literal(transition.action_name),
                sql_text_array(transition.allowed_roles),
                "true" if transition.owner_scoped else "false",
                "true" if transition.director_override else "false",
            ]
        )
        + ")"
        for transition in transitions
    )


def render_add_role_sql(plan: ProjectBoardProvision, role: str, *, schema_sql: str | None = None) -> str:
    resolved_role = _validate_role(role)
    if plan.workflow_seed == "pgu-full":
        raise SystemExit("incremental add-role SQL is only for provisioned project workflows")
    if resolved_role in plan.implementer_roles:
        role_kind = "implementer"
    elif resolved_role in plan.audit_roles:
        role_kind = "auditor"
    else:
        raise SystemExit("incremental add-role SQL requires the role in plan.implementer_roles or plan.audit_roles")
    stages = project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = _workflow_stage_rows_sql(stages)
    transition_rows = _workflow_transition_rows_sql(transitions)
    return f"""-- Add {role_kind} role {resolved_role} to the existing project workflow for {plan.project}.
-- Run after the launcher config has been updated with the expanded role set.
BEGIN;

{render_project_role_constraint_sql(plan)}

WITH desired(
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
) AS (
    VALUES
{stage_rows}
)
INSERT INTO ticket_board.workflow_stages (
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
)
SELECT
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
FROM desired
ON CONFLICT (name) DO UPDATE SET
    display_label = EXCLUDED.display_label,
    rank = EXCLUDED.rank,
    owner_roles = EXCLUDED.owner_roles,
    entry_gate_field = EXCLUDED.entry_gate_field,
    gate_skip_to = EXCLUDED.gate_skip_to,
    exit_signoff_field = EXCLUDED.exit_signoff_field,
    is_terminal = EXCLUDED.is_terminal;

WITH desired(
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
) AS (
    VALUES
{transition_rows}
)
INSERT INTO ticket_board.workflow_transitions (
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
)
SELECT
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
FROM desired
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE SET
    allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;

COMMIT;
"""


def render_vcs_close_role_sql(plan: ProjectBoardProvision, *, schema_sql: str | None = None) -> str:
    if plan.workflow_seed == "pgu-full":
        raise SystemExit("incremental VCS close-role SQL is only for provisioned project workflows")
    mark_done_roles = dict(plan.operation_allowed_roles).get("mark_done", ())
    if len(mark_done_roles) != 1:
        raise SystemExit("incremental VCS close-role SQL requires exactly one configured mark_done role")
    role = mark_done_roles[0]
    if role not in plan.assignee_roles or role not in plan.caller_roles:
        raise SystemExit("--vcs-close-role must be one of the configured project roles")
    stages = project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = _workflow_stage_rows_sql(stages)
    transition_rows = _workflow_transition_rows_sql(transitions)
    return f"""-- Configure VCS close role {role} for the existing project workflow for {plan.project}.
-- Run after the launcher plan has been updated with operation_allowed_roles mark_done={role}.
BEGIN;

{render_project_role_constraint_sql(plan)}

UPDATE ticket_board.workflow_stages
SET rank = rank + 1000;

WITH desired(
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
) AS (
    VALUES
{stage_rows}
)
INSERT INTO ticket_board.workflow_stages (
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
)
SELECT
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
FROM desired
ON CONFLICT (name) DO UPDATE SET
    display_label = EXCLUDED.display_label,
    rank = EXCLUDED.rank,
    owner_roles = EXCLUDED.owner_roles,
    entry_gate_field = EXCLUDED.entry_gate_field,
    gate_skip_to = EXCLUDED.gate_skip_to,
    exit_signoff_field = EXCLUDED.exit_signoff_field,
    is_terminal = EXCLUDED.is_terminal;

DELETE FROM ticket_board.workflow_transitions
WHERE action_name = 'mark_done';

WITH desired(
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
) AS (
    VALUES
{transition_rows}
)
INSERT INTO ticket_board.workflow_transitions (
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
)
SELECT
    from_stage,
    to_stage,
    action_name,
    allowed_roles,
    owner_scoped,
    director_override
FROM desired
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE SET
    allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;

COMMIT;
"""


def render_operator_commands(plan: ProjectBoardProvision, *, enable_owner_linger: bool = True) -> str:
    q_asset = shell_quote(plan.asset_dir)
    q_frame = shell_quote(plan.frame_dir)
    q_board_root = shell_quote(plan.board_root)
    q_source_repo = shell_quote(plan.source_repo)
    q_commit_git_dir = shell_quote(plan.commit_git_dir)
    q_deploy_script = shell_quote(f"{plan.source_repo}/scripts/ticket-board-service.sh")
    q_board_unit = shell_quote(f"/etc/systemd/system/{plan.board_unit}")
    q_tmpfiles = shell_quote(f"/etc/tmpfiles.d/{plan.tmpfiles_name}")
    q_polkit = shell_quote(f"/etc/polkit-1/rules.d/{plan.polkit_name}")
    q_listener_unit = shell_quote(
        f"/home/{plan.owner_user}/.config/systemd/user/{plan.listener_unit}"
    )
    q_owner_home = shell_quote(f"/home/{plan.owner_user}")
    q_hook_installer = shell_quote(f"{plan.board_current}/scripts/ticket-board-install-pane-hooks")
    q_hook_source = shell_quote(f"{plan.board_current}/scripts/ticket-board-pane-idle-hook")
    q_hook_bin = shell_quote(f"/home/{plan.owner_user}/.local/bin/ticket-board-pane-idle-hook")
    q_pane_session_dir = shell_quote(
        f"/home/{plan.owner_user}/.local/state/{plan.runtime_directory}/pane-sessions"
    )
    workflow_seed_command = ""
    if plan.workflow_seed != "pgu-full":
        workflow_seed_command = postgres_sql_file_command(
            plan.project + "-workflow.sql",
            database_url=plan.admin_database_url,
        )
        workflow_seed_command += "\n"
    install_board_root = owned_directory_command(
        plan,
        owned_ancestor_dirs(plan.owner_user, plan.board_root, include_target=True),
    )
    if not install_board_root:
        install_board_root = f"sudo install -d -m 0755 {q_board_root}"
    install_asset_frame_parent = owned_directory_command(
        plan,
        (
            *owned_ancestor_dirs(plan.owner_user, plan.asset_dir, include_target=False),
            *owned_ancestor_dirs(plan.owner_user, plan.frame_dir, include_target=False),
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
            plan.owner_user,
            f"/home/{plan.owner_user}/.config/systemd/user",
            include_target=True,
        ),
    )
    q_owner_user = shell_quote(plan.owner_user)
    q_board_env_file = shell_quote(f"/home/{plan.owner_user}/.config/{plan.project}/ticket-board.env")
    q_owner_config_dir = shell_quote(f"/home/{plan.owner_user}/.config")
    q_board_env_dir = shell_quote(f"/home/{plan.owner_user}/.config/{plan.project}")
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
                f"sudo setfacl -m u:{plan.service_user}:--x {shell_quote('/home/' + plan.owner_user)}",
                f"sudo setfacl -m u:{plan.service_user}:--x {shell_quote('/home/' + plan.owner_user + '/.claude')}",
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
{install_board_root}
sudo env TICKET_BOARD_PROJECT={shell_quote(plan.project)} TICKET_BOARD_COMMIT_GIT_DIR={q_commit_git_dir} SOURCE_REPO={q_source_repo} BOARD_ROOT={q_board_root} DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 {q_deploy_script} deploy
{grant_board_root}
{install_asset_frame}
{grant_home_traversal}
{effective_grant_asset_frame}
{install_board_env_parent}
if ! sudo -u {q_owner_user} test -s {q_board_env_file}; then
    report_token="$(/usr/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'TICKET_BOARD_TENANT_REPORT_TOKEN=%s\\n' "$report_token" | sudo -u {q_owner_user} tee {q_board_env_file} >/dev/null
    sudo -u {q_owner_user} chmod 0600 {q_board_env_file}
fi
sudo install -m 0644 {shell_quote(plan.board_unit)} {q_board_unit}
sudo install -m 0644 {shell_quote(plan.tmpfiles_name)} {q_tmpfiles}
sudo install -m 0644 {shell_quote(plan.polkit_name)} {q_polkit}
sudo systemd-tmpfiles --create {q_tmpfiles}
{postgres_sql_file_command(plan.project + '-database.sql')}
{postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/schema.sql', database_url=plan.admin_database_url)}
sudo env TICKET_BOARD_ADMIN_DATABASE_URL={shell_quote(plan.admin_database_url)} {shell_quote(plan.board_current + '/scripts/ticket-board-migrate')}
{workflow_seed_command.rstrip()}
{postgres_sql_file_command(plan.board_current + '/scripts/ticket_board/rbac.sql', database_url=plan.admin_database_url)}
sudo systemctl daemon-reload
sudo systemctl enable --now {plan.board_unit}
{install_listener_unit_parent}
sudo install -m 0644 -o {q_owner_user} -g {q_owner_user} {shell_quote(plan.listener_unit)} {q_listener_unit}
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
sudo -u {q_owner_user} env XDG_RUNTIME_DIR="$owner_runtime_dir" DBUS_SESSION_BUS_ADDRESS="unix:path=$owner_bus" systemctl --user enable --now {plan.listener_unit}
curl -fsS http://127.0.0.1:{plan.port}/api/board >/dev/null
"""


def write_artifacts(plan: ProjectBoardProvision, output_dir: Path, *, enable_owner_linger: bool = True) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "plan.json": json.dumps(asdict(plan), indent=2, sort_keys=True) + "\n",
        plan.board_unit: render_board_unit(plan),
        plan.listener_unit: render_listener_unit(plan),
        plan.tmpfiles_name: render_tmpfiles(plan),
        plan.polkit_name: render_polkit_rule(plan),
        f"{plan.project}-database.sql": render_database_sql(plan),
        f"{plan.project}-workflow.sql": render_workflow_sql(plan),
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
    parser.add_argument("--owner-user", required=True, help="Unix user that owns this project/team")
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database name; omitted means <project>_ticket_board")
    parser.add_argument("--ticket-prefix", help="ticket id prefix; omitted means normalized project slug")
    parser.add_argument("--service-user", default="boardsvc", help="systemd User for the board service")
    parser.add_argument("--service-role", default="ticket_board_service", help="database writer role")
    parser.add_argument("--listener-role", default="ticket_board_listener", help="database listener role")
    parser.add_argument("--board-root", type=Path, help="versioned release root; default under owner home")
    parser.add_argument("--source-repo", type=Path, help="source checkout to export into the board release root")
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
        choices=["board-unit", "listener-unit", "tmpfiles", "polkit", "database-sql", "workflow-sql", "commands"],
        help="print one rendered artifact",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    plan = build_plan(
        project=args.project,
        owner_user=args.owner_user,
        port=args.port,
        database=args.database,
        ticket_prefix=args.ticket_prefix,
        service_user=args.service_user,
        service_role=args.service_role,
        listener_role=args.listener_role,
        board_root=args.board_root,
        source_repo=args.source_repo,
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
