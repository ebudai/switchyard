"""How provisioning projects the workflow: schema.sql's seed rows, and each tenant's stages and transitions.

The board's workflow is seeded in `schema.sql`. This reads its
`workflow_stages` and `workflow_transitions` INSERT rows back as
`WorkflowStageSeed` and `WorkflowTransitionSeed` (`schema_workflow_stages`,
`schema_workflow_transitions`, with the small SQL literal parsers they need),
and projects them onto one tenant's plan: the stages and transitions a tenant
board exposes (`TENANT_WORKFLOW_EXCLUDED_STAGES`,
`TENANT_WORKFLOW_EXCLUDED_ACTIONS`), their ranks, owner roles and allowed
roles (`project_workflow_stages`, `project_workflow_transitions`,
`project_workflow_state_names`).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-465).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
`_dedupe` and `DEFAULT_IMPLEMENTER_ROLES` -- is read through it when they run,
so a patch there still reaches them. This module imports `project_provision`
only inside the functions that need it, when they run, with the same fallback
for direct script execution. The SQL rendered from the projection, and the
workflow record, stay in `project_provision`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SCHEMA_SQL_PATH = Path(__file__).with_name("schema.sql")


# Tenant boards intentionally expose a smaller workflow surface than the pgu
# operations board. Keep this as a projection policy over schema.sql, not as a
# separately maintained transition table.
TENANT_WORKFLOW_EXCLUDED_STAGES = frozenset({"backlog", "inspection"})
TENANT_WORKFLOW_EXCLUDED_ACTIONS = frozenset(
    {"defer", "request_commit_exempt", "start_task", "submit_to_inspection"}
)


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


def _schema_sql_text(schema_sql: str | None = None) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if schema_sql is not None:
        return schema_sql
    return provision.SCHEMA_SQL_PATH.read_text(encoding="utf-8")


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
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    stripped = token.strip()
    if stripped.upper() == "NULL":
        return None
    return provision._parse_sql_string(stripped)


def _parse_sql_bool(token: str) -> bool:
    stripped = token.strip().lower()
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    raise ValueError(f"expected SQL boolean, got {token!r}")


def _parse_sql_text_array(token: str) -> tuple[str, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    stripped = token.strip()
    match = re.fullmatch(r"ARRAY\[(.*)\]::text\[]", stripped, flags=re.DOTALL)
    if not match:
        raise ValueError(f"expected SQL text array, got {token!r}")
    inner = match.group(1).strip()
    if not inner:
        return ()
    return tuple(provision._parse_sql_string(field) for field in provision._split_sql_fields(inner))


def schema_workflow_stages(schema_sql: str | None = None) -> tuple[WorkflowStageSeed, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    rows = provision._split_sql_tuple_rows(provision._insert_values_block(provision._schema_sql_text(schema_sql), "workflow_stages"))
    stages: list[WorkflowStageSeed] = []
    for row in rows:
        fields = provision._split_sql_fields(row)
        if len(fields) != 8:
            raise ValueError(f"workflow_stages seed row has {len(fields)} fields, expected 8: {row}")
        stages.append(
            provision.WorkflowStageSeed(
                name=provision._parse_sql_string(fields[0]),
                display_label=provision._parse_sql_string(fields[1]),
                rank=int(fields[2]),
                owner_roles=provision._parse_sql_text_array(fields[3]),
                entry_gate_field=provision._parse_sql_nullable_string(fields[4]),
                gate_skip_to=provision._parse_sql_nullable_string(fields[5]),
                exit_signoff_field=provision._parse_sql_nullable_string(fields[6]),
                is_terminal=provision._parse_sql_bool(fields[7]),
            )
        )
    return tuple(stages)


def schema_workflow_transitions(schema_sql: str | None = None) -> tuple[WorkflowTransitionSeed, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    rows = provision._split_sql_tuple_rows(provision._insert_values_block(provision._schema_sql_text(schema_sql), "workflow_transitions"))
    transitions: list[WorkflowTransitionSeed] = []
    for row in rows:
        fields = provision._split_sql_fields(row)
        if len(fields) != 6:
            raise ValueError(f"workflow_transitions seed row has {len(fields)} fields, expected 6: {row}")
        transitions.append(
            provision.WorkflowTransitionSeed(
                from_stage=provision._parse_sql_string(fields[0]),
                to_stage=provision._parse_sql_string(fields[1]),
                action_name=provision._parse_sql_string(fields[2]),
                allowed_roles=provision._parse_sql_text_array(fields[3]),
                owner_scoped=provision._parse_sql_bool(fields[4]),
                director_override=provision._parse_sql_bool(fields[5]),
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
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if plan.workflow_seed == "pgu-full":
        return stage.owner_roles
    if stage.name == "draft":
        return plan.draft_roles
    if stage.name == "in_progress":
        return provision._project_implementation_owner_roles(plan)
    if stage.name == "audit":
        return plan.audit_roles
    return stage.owner_roles


def _project_workflow_allowed_roles(roles: Sequence[str], plan: ProjectBoardProvision) -> tuple[str, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if plan.workflow_seed == "pgu-full":
        return tuple(roles)
    result: list[str] = []
    inserted_implementers = False
    for role in roles:
        if role in provision.DEFAULT_IMPLEMENTER_ROLES:
            if not inserted_implementers:
                result.extend(plan.implementer_roles)
                inserted_implementers = True
            continue
        if role == "audit":
            result.extend(plan.audit_roles)
            continue
        result.append(role)
    return provision._dedupe(result)


def _rank_project_stages(stages: Sequence[WorkflowStageSeed]) -> tuple[WorkflowStageSeed, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    result: list[WorkflowStageSeed] = []
    next_rank = 0
    terminal_rank_offset = 1 if any(stage.name == "vcs" for stage in stages) else 0
    for stage in stages:
        if stage.is_terminal:
            result.append(
                provision.WorkflowStageSeed(
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
            provision.WorkflowStageSeed(
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
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    source_stages = provision.schema_workflow_stages(schema_sql)
    if plan.workflow_seed == "pgu-full":
        return source_stages

    excluded_stages = set(provision.TENANT_WORKFLOW_EXCLUDED_STAGES)
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
                provision.WorkflowStageSeed(
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
            provision.WorkflowStageSeed(
                name=stage.name,
                display_label=stage.display_label,
                rank=stage.rank,
                owner_roles=provision._project_workflow_owner_roles(stage, plan),
                entry_gate_field=stage.entry_gate_field,
                gate_skip_to=None if stage.gate_skip_to in excluded_stages else stage.gate_skip_to,
                exit_signoff_field=stage.exit_signoff_field,
                is_terminal=stage.is_terminal,
            )
        )
    return provision._rank_project_stages(projected)


def project_workflow_transitions(
    plan: ProjectBoardProvision, *, schema_sql: str | None = None
) -> tuple[WorkflowTransitionSeed, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if plan.workflow_seed == "pgu-full":
        return provision.schema_workflow_transitions(schema_sql)

    stage_names = {stage.name for stage in provision.project_workflow_stages(plan, schema_sql=schema_sql)}
    include_audit = bool(plan.audit_roles)
    mark_done_roles = dict(plan.operation_allowed_roles).get("mark_done", ())
    has_vcs_close = bool(mark_done_roles)
    transitions: list[WorkflowTransitionSeed] = []
    for transition in provision.schema_workflow_transitions(schema_sql):
        if transition.action_name in provision.TENANT_WORKFLOW_EXCLUDED_ACTIONS:
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
        allowed_roles = provision._project_workflow_allowed_roles(transition.allowed_roles, plan)
        owner_scoped = transition.owner_scoped
        if from_stage == "audit" and transition.action_name in {"audit_sign_off", "audit_kick_back"}:
            owner_scoped = True
        if transition.action_name == "release_draft":
            allowed_roles = provision._dedupe((*plan.draft_roles, *allowed_roles))
        transitions.append(
            provision.WorkflowTransitionSeed(
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
                provision.WorkflowTransitionSeed("director_review", "vcs", "route", ("director",), False, False),
                provision.WorkflowTransitionSeed("vcs", "done", "mark_done", mark_done_roles, False, False),
            ]
        )
    return tuple(transitions)


def project_workflow_state_names(plan: ProjectBoardProvision) -> tuple[str, ...]:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return tuple(stage.name for stage in provision.project_workflow_stages(plan))
