"""How provisioning renders a tenant's workflow as SQL.

The SQL that seeds a tenant board's workflow from its plan
(`render_workflow_sql`: the projected stages and transitions, or a declared
workflow applied as a document), the role constraint it installs
(`render_project_role_constraint_sql`), and the migrations that add a role or a
VCS close role to a board already provisioned (`render_add_role_sql`,
`render_vcs_close_role_sql`, with the row renderers they share).

Moved out of `scripts/ticket_board/project_provision.py` unchanged (SYRD-466).
`project_provision` imports this module and re-exports every name, so every
module and test that imports them from there, or patches them there, still
reaches the same objects. What they read of `project_provision` -- each other,
the projected workflow, `sql_literal`, `sql_text_array`, `_validate_role` and
`UNVERIFIED_DECLARED_WORKFLOW` -- is read through it when they run, so a patch
there still reaches them. This module imports `project_provision` only inside
the functions that need it, when they run, with the same fallback for direct
script execution.
"""

from __future__ import annotations

import json
from typing import Sequence


def render_project_role_constraint_sql(plan: ProjectBoardProvision) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    notification_roles = tuple(role for role in plan.assignee_roles if role not in {"unassigned", "agent", "user"})
    state_names = provision.project_workflow_state_names(plan)
    return f"""
ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_name_check CHECK (name IN ({", ".join(provision.sql_literal(state) for state in state_names)}));

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_state_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_state_check CHECK (state IN ({", ".join(provision.sql_literal(state) for state in state_names)}));

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN ({", ".join(provision.sql_literal(role) for role in plan.assignee_roles)}));

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_awaiting_role_check
    CHECK (
        awaiting_role = ''
        OR awaiting_role IN ({", ".join(provision.sql_literal(role) for role in notification_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_last_implementer_assignee_check
    CHECK (
        last_implementer_assignee = ''
        OR last_implementer_assignee IN ({", ".join(provision.sql_literal(role) for role in plan.implementer_roles)})
    );

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_target_role_check
    CHECK (target_role IN ({", ".join(provision.sql_literal(role) for role in notification_roles)}));
"""


def render_workflow_sql(plan: ProjectBoardProvision, *, schema_sql: str | None = None) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if plan.workflow is not None:
        # Canonical, so the same workflow renders the same bytes however it
        # reached this plan -- generated here, read back from root's record, or
        # migrated through a plan document. An artifact that changed spelling
        # on every regeneration would look like a change to every check that
        # compares what root installed with what root would install (SYRD-165).
        document = json.dumps(plan.workflow, sort_keys=True).replace("'", "''")
        return "BEGIN;\nSET LOCAL ROLE ticket_board_service;\nSELECT set_config('ticket_board.project','" + plan.project + "',true);\nSELECT set_config('ticket_board.caller_role','director',true);\nSELECT ticket_board.apply_declared_workflow('" + document + "'::jsonb);\nCOMMIT;\n"
    if plan.workflow_seed == provision.UNVERIFIED_DECLARED_WORKFLOW:
        return f"""-- {plan.project} declares its own workflow, and root holds no verified copy of it.
--
-- Root will not seed a workflow it cannot vouch for, and it will not seed the
-- default one in its place: that would replace this project's roles, stages,
-- transitions and labels with somebody else's on any board not yet carrying
-- them. The board keeps what it is running, and this phase does nothing.
--
-- Root records a project's declared workflow when it first generates that
-- project's artifacts. A project provisioned before root kept one has no such
-- record (SYRD-165), and `pkexec switchyard adopt-workflow {plan.project}` is
-- how an operator gives it one: it shows the document, its digest and the
-- workflow the running board is enforcing, refuses if those disagree, and
-- keeps the decision in the rollout journal (SYRD-166).
"""
    if plan.workflow_seed == "pgu-full":
        return f"""-- {plan.project} keeps the full workflow seeded by schema.sql.
-- No per-project workflow override is applied.
"""

    stages = provision.project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = provision.project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = ",\n".join(
        "    ("
        + ", ".join(
            [
                provision.sql_literal(name),
                provision.sql_literal(label),
                str(rank),
                provision.sql_text_array(owner_roles),
                "NULL" if entry_gate_field is None else provision.sql_literal(entry_gate_field),
                "NULL" if gate_skip_to is None else provision.sql_literal(gate_skip_to),
                "NULL" if exit_signoff_field is None else provision.sql_literal(exit_signoff_field),
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
                provision.sql_literal(from_stage),
                provision.sql_literal(to_stage),
                provision.sql_literal(action_name),
                provision.sql_text_array(allowed_roles),
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
--
-- This phase is the INITIAL seed, and it is the one phase of the operator
-- packet that is not a repair. It deletes the stages and transitions the board
-- has and installs this project's, which is right for a board being brought
-- up and wrong for one that is running: its tickets sit in those stages, its
-- roles hold runtime assignments against them, and its history refers to
-- transitions by name.
--
-- The packet as a whole is re-runnable, so a supported upgrade of a registered
-- tenant reaches this file too. It used to run unconditionally and stop the
-- whole rollout on the guard below -- `project workflow seed must run before
-- tickets exist` -- which is how an existing tenant became unupgradable
-- (SYRD-164, syrd rollout journal 0050). The boundary is explicit now: an
-- established board is left exactly as it is, and the run continues to the
-- phases that ARE repairs.
--
-- The guard itself is untouched and still here. Nothing in the supported path
-- reaches it any more, and that is the point: it is what stands between a
-- live board and this file if anything ever does.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ticket_board.tickets)
        OR EXISTS (SELECT 1 FROM ticket_board.workflow_configuration WHERE singleton)
    THEN
        RAISE NOTICE 'workflow already established for {plan.project}; leaving its stages, transitions, tickets and runtime assignments as they are';
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.tickets) THEN
        RAISE EXCEPTION 'project workflow seed must run before tickets exist';
    END IF;

    DELETE FROM ticket_board.workflow_transitions;
    UPDATE ticket_board.workflow_stages SET gate_skip_to = NULL WHERE gate_skip_to IS NOT NULL;
    DELETE FROM ticket_board.workflow_stages;

{provision.render_project_role_constraint_sql(plan)}

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
END;
$$;

COMMIT;
"""


def _workflow_stage_rows_sql(stages: Sequence[WorkflowStageSeed]) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return ",\n".join(
        "    ("
        + ", ".join(
            [
                provision.sql_literal(stage.name),
                provision.sql_literal(stage.display_label),
                str(stage.rank),
                provision.sql_text_array(stage.owner_roles),
                "NULL" if stage.entry_gate_field is None else provision.sql_literal(stage.entry_gate_field),
                "NULL" if stage.gate_skip_to is None else provision.sql_literal(stage.gate_skip_to),
                "NULL" if stage.exit_signoff_field is None else provision.sql_literal(stage.exit_signoff_field),
                "true" if stage.is_terminal else "false",
            ]
        )
        + ")"
        for stage in stages
    )


def _workflow_transition_rows_sql(transitions: Sequence[WorkflowTransitionSeed]) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    return ",\n".join(
        "    ("
        + ", ".join(
            [
                provision.sql_literal(transition.from_stage),
                provision.sql_literal(transition.to_stage),
                provision.sql_literal(transition.action_name),
                provision.sql_text_array(transition.allowed_roles),
                "true" if transition.owner_scoped else "false",
                "true" if transition.director_override else "false",
            ]
        )
        + ")"
        for transition in transitions
    )


def render_add_role_sql(plan: ProjectBoardProvision, role: str, *, schema_sql: str | None = None) -> str:
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    resolved_role = provision._validate_role(role)
    if plan.workflow_seed == "pgu-full":
        raise SystemExit("incremental add-role SQL is only for provisioned project workflows")
    if resolved_role in plan.implementer_roles:
        role_kind = "implementer"
    elif resolved_role in plan.audit_roles:
        role_kind = "auditor"
    else:
        raise SystemExit("incremental add-role SQL requires the role in plan.implementer_roles or plan.audit_roles")
    stages = provision.project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = provision.project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = provision._workflow_stage_rows_sql(stages)
    transition_rows = provision._workflow_transition_rows_sql(transitions)
    return f"""-- Add {role_kind} role {resolved_role} to the existing project workflow for {plan.project}.
-- Run after the launcher config has been updated with the expanded role set.
BEGIN;

{provision.render_project_role_constraint_sql(plan)}

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
    try:
        from . import project_provision as provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision as provision

    if plan.workflow_seed == "pgu-full":
        raise SystemExit("incremental VCS close-role SQL is only for provisioned project workflows")
    mark_done_roles = dict(plan.operation_allowed_roles).get("mark_done", ())
    if len(mark_done_roles) != 1:
        raise SystemExit("incremental VCS close-role SQL requires exactly one configured mark_done role")
    role = mark_done_roles[0]
    if role not in plan.assignee_roles or role not in plan.caller_roles:
        raise SystemExit("--vcs-close-role must be one of the configured project roles")
    stages = provision.project_workflow_stages(plan, schema_sql=schema_sql)
    transitions = provision.project_workflow_transitions(plan, schema_sql=schema_sql)
    stage_rows = provision._workflow_stage_rows_sql(stages)
    transition_rows = provision._workflow_transition_rows_sql(transitions)
    return f"""-- Configure VCS close role {role} for the existing project workflow for {plan.project}.
-- Run after the launcher plan has been updated with operation_allowed_roles mark_done={role}.
BEGIN;

{provision.render_project_role_constraint_sql(plan)}

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
