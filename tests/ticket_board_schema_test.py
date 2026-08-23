#!/usr/bin/env python3
"""Static checks for the ticket-board PostgreSQL schema."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"

EXPECTED_JSON_FIELDS = {
    "assignee",
    "audit_prompt",
    "audit_signoff",
    "inspector_signoff",
    "blocked_by",
    "blockers",
    "blocked_reason",
    "body",
    "comments",
    "commit_exempt",
    "commit_hash",
    "created",
    "user_signoff",
    "id",
    "implementation",
    "manually_controlled",
    "needs_inspection",
    "needs_user_signoff",
    "parent_id",
    "regression",
    "screenshot",
    "screenshots",
    "state",
    "title",
    "updated",
}

EXPECTED_COMMENT_FIELDS = {"who", "ts", "text"}
EXPECTED_STATES = {
    "draft",
    "backlog",
    "analysis",
    "in_progress",
    "inspection",
    "audit",
    "dat",
    "user_review",
    "director_review",
    "done",
    "cancelled",
}
EXPECTED_ASSIGNEES = {
    "unassigned",
    "main",
    "app",
    "perf",
    "ops",
    "audit",
    "inspector",
    "agent",
    "director",
    "research",
}
EXPECTED_FUNCTION_API = {
    "ticket_board.create_ticket",
    "ticket_board.file_bug",
    "ticket_board.release_draft",
    "ticket_board.route",
    "ticket_board.force_move",
    "ticket_board.start_work",
    "ticket_board.submit_to_inspection",
    "ticket_board.submit_to_audit",
    "ticket_board.request_commit_exempt",
    "ticket_board.start_task",
    "ticket_board.complete_task",
    "ticket_board.set_awaiting_role",
    "ticket_board.clear_awaiting_role",
    "ticket_board.inspector_sign_off",
    "ticket_board.inspector_kick_back",
    "ticket_board.audit_sign_off",
    "ticket_board.audit_kick_back",
    "ticket_board.director_dat_sign_off",
    "ticket_board.director_dat_kick_back",
    "ticket_board.user_sign_off",
    "ticket_board.user_reopen",
    "ticket_board.mark_done",
    "ticket_board.defer",
    "ticket_board.cancel",
    "ticket_board.set_manually_controlled",
    "ticket_board.set_blockers",
    "ticket_board.add_comment",
    "ticket_board.edit_fields",
    "ticket_board.merge",
}

FIELD_TO_SCHEMA_TOKENS = {
    "assignee": ["assignee"],
    "audit_prompt": ["audit_prompt"],
    "audit_signoff": ["audit_signoff"],
    "inspector_signoff": ["inspector_signoff"],
    "blocked_by": ["ticket_blockers", "blocker_ticket_id"],
    "blockers": ["ticket_blockers", "resolved"],
    "blocked_reason": ["blocked_reason"],
    "body": ["body"],
    "comments": ["ticket_comments"],
    "commit_exempt": ["commit_exempt"],
    "commit_hash": ["commit_hash"],
    "created": ["created_text", "created_at"],
    "user_signoff": ["user_signoff"],
    "id": ["id", "ticket_number"],
    "implementation": ["implementation"],
    "manually_controlled": ["manually_controlled"],
    "needs_inspection": ["needs_inspection"],
    "needs_user_signoff": ["needs_user_signoff"],
    "parent_id": ["parent_id"],
    "regression": ["regression"],
    "screenshot": ["screenshot", "ticket_attachments"],
    "screenshots": ["ticket_attachments"],
    "state": ["state"],
    "title": ["title"],
    "updated": ["updated_text", "updated_at"],
}


def assert_contains_all(schema: str, values: set[str], label: str) -> None:
    missing = sorted(value for value in values if value not in schema)
    assert not missing, f"{label} missing from schema: {missing}"


def queue_notification_kinds(schema_lower: str) -> set[str]:
    match = re.search(
        r"create table if not exists ticket_board\.ticket_notification_queue\s*\(.*?kind text not null check \(kind in \(([^)]*)\)\)",
        schema_lower,
        re.DOTALL,
    )
    assert match, "ticket_notification_queue kind constraint missing from schema"
    return {
        value.strip().strip("'")
        for value in match.group(1).split(",")
        if value.strip()
    }


def main() -> int:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_lower = schema.lower()
    executable_schema_lower = "\n".join(
        line for line in schema_lower.splitlines() if not line.lstrip().startswith("--")
    )
    for field in sorted(EXPECTED_JSON_FIELDS):
        tokens = FIELD_TO_SCHEMA_TOKENS[field]
        assert_contains_all(schema, set(tokens), f"field {field!r}")

    for field in sorted(EXPECTED_COMMENT_FIELDS):
        assert field in schema, f"comment field {field!r} missing from schema"

    assert "source_json jsonb not null" in schema_lower, "tickets.source_json is required for lossless import"
    assert "create table if not exists ticket_board.schema_migrations" in schema_lower
    assert "create sequence if not exists ticket_board.schema_migrations_seq as bigint" in schema_lower
    assert "name text primary key" in schema_lower
    assert "seq bigint not null default nextval('ticket_board.schema_migrations_seq')" in schema_lower
    assert "applied_at timestamptz not null default now()" in schema_lower
    assert "create unique index if not exists schema_migrations_seq_idx" in schema_lower
    assert "values ('schema.sql')" in schema_lower
    assert "create table if not exists ticket_board.ticket_blockers" in schema_lower
    assert "create table if not exists ticket_board.ticket_comments" in schema_lower
    assert "create table if not exists ticket_board.ticket_attachments" in schema_lower
    assert "metadata jsonb not null default '{}'::jsonb" in schema_lower
    assert "create or replace function ticket_board.append_ticket_attachment" in schema_lower
    rbac_sql = (ROOT / "scripts" / "ticket_board" / "rbac.sql").read_text(encoding="utf-8").lower()
    assert "grant execute on function ticket_board.append_ticket_attachment(text, text, jsonb) to ticket_board_service" in rbac_sql
    crop_metadata_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu531_attachment_crop_metadata.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists metadata jsonb not null default '{}'::jsonb" in crop_metadata_migration
    assert "create or replace function ticket_board.append_ticket_attachment" in crop_metadata_migration
    assert "grant execute on function ticket_board.append_ticket_attachment(text, text, jsonb) to ticket_board_service" in crop_metadata_migration
    assert "create table if not exists ticket_board.notification_trace" in schema_lower
    assert "ticket_state_at_event" in schema_lower
    assert "ticket_assignee_at_event" in schema_lower
    assert "pane_busy_determination" in schema_lower
    assert "region_digest" in schema_lower
    assert "create or replace function ticket_board.record_notification_trace" in executable_schema_lower
    assert "create index if not exists notification_trace_send_lookup_idx" in executable_schema_lower
    assert "where kind = 'transition' and event = 'send'" in executable_schema_lower
    assert "create or replace function ticket_board.prune_notification_trace" in executable_schema_lower
    assert "event in ('claim', 'listener_claim', 'requeue', 'gate_defer')" in executable_schema_lower
    assert "create or replace function ticket_board.install_notification_trace_prune_cron" in executable_schema_lower
    assert "ticket_board_notification_trace_prune" in executable_schema_lower
    assert "create or replace function ticket_board.next_notification_attempt" in executable_schema_lower
    assert "create or replace function ticket_board.notification_delivery_in_backoff" in executable_schema_lower
    assert {"transition", "ticket_update", "nudge", "escalation", "idle_reminder"} <= queue_notification_kinds(
        executable_schema_lower
    )

    assert_contains_all(schema, EXPECTED_STATES, "state constraint")
    assert "'ready'" not in schema, "removed ready state must not appear in schema.sql"
    assert "implementation must be non-empty before a ticket can enter in_progress" not in schema
    assert "in_progress tickets require an implementer assignee" in schema
    assert "create or replace function ticket_board.ticket_is_implementer_assignee" in executable_schema_lower
    assert "create or replace function ticket_board.stage_default_assignee" in executable_schema_lower
    assert "create or replace function ticket_board.apply_stage_default_assignee_update" in executable_schema_lower
    assert "tickets_zzzz_stage_default_assignee_update" in executable_schema_lower
    assert "ticket_board.stage_default_assignee(new.state)" in executable_schema_lower
    assert "from ticket_board.workflow_stages" in executable_schema_lower
    assert "when cardinality(owner_roles) = 1 then owner_roles[1]" in executable_schema_lower
    stage_default_function = re.search(
        r"create or replace function ticket_board\.stage_default_assignee\(p_state text\).*?\$\$(.*?)\$\$;",
        executable_schema_lower,
        re.S,
    )
    assert stage_default_function
    assert "workflow_stages" in stage_default_function.group(1)
    assert "when p_state = 'inspection' then 'inspector'" not in stage_default_function.group(1)
    enforce_update_function = re.search(
        r"create or replace function ticket_board\.enforce_ticket_workflow_update\(\).*?\$\$(.*?)\$\$;",
        executable_schema_lower,
        re.S,
    )
    assert enforce_update_function
    assert "stage_default_assignee" not in enforce_update_function.group(1)
    stage_default_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu584_stage_default_assignee.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.stage_default_assignee" in stage_default_migration
    assert "tickets_zzzz_stage_default_assignee_update" in stage_default_migration
    stage_default_from_workflow_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu588_stage_default_assignee_from_workflow.sql"
    ).read_text(encoding="utf-8").lower()
    assert "from ticket_board.workflow_stages" in stage_default_from_workflow_migration
    assert "when cardinality(owner_roles) = 1 then owner_roles[1]" in stage_default_from_workflow_migration
    assert "create or replace function ticket_board.apply_stage_default_assignee_update" in stage_default_from_workflow_migration
    assert "new.state <> 'analysis'" in stage_default_from_workflow_migration
    assert "add constraint tickets_in_progress_assignee_check" not in schema_lower
    assert re.search(
        r"resolved\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "ticket_blockers.resolved must persist blocker resolution"
    assert re.search(
        r"idle_reminder_count\s+integer\s+not null\s+default\s+0",
        schema_lower,
    ), "ticket_notification_state.idle_reminder_count must persist per-episode reminder state"
    assert re.search(
        r"awaiting_role\s+text\s+not null\s+default\s+''",
        schema_lower,
    ), "ticket_notification_state.awaiting_role must persist explicit cross-pane waits"
    assert "create or replace function ticket_board.ticket_awaiting_role_is_active" in executable_schema_lower
    assert "create or replace function ticket_board.set_awaiting_role" in executable_schema_lower
    assert "create or replace function ticket_board.clear_awaiting_role" in executable_schema_lower
    assert "unresolved blocker prevents forward promotion" in schema
    assert "create or replace function ticket_board.ticket_is_forward_promotion" in executable_schema_lower
    assert "create or replace function ticket_board.resolve_completed_blockers" in executable_schema_lower
    assert "create trigger tickets_zzzy_resolve_completed_blockers" in executable_schema_lower
    resolved_blockers_migration = (ROOT / "scripts" / "ticket_board" / "migrations" / "272_resolved_blockers.sql").read_text(encoding="utf-8").lower()
    assert "add column if not exists resolved boolean" in resolved_blockers_migration
    assert "unresolved blocker prevents forward promotion" in resolved_blockers_migration
    optional_implementation_migration = (ROOT / "scripts" / "ticket_board" / "migrations" / "271_optional_implementation.sql").read_text(encoding="utf-8")
    assert "implementation must be non-empty before a ticket can enter in_progress" not in optional_implementation_migration
    assert_contains_all(schema, EXPECTED_ASSIGNEES, "assignee constraint")

    assert re.search(r"commit_hash\s+text\s+not null", schema_lower), "commit_hash column should be text"
    assert re.search(r"last_rejected_commit\s+text", schema_lower), "last_rejected_commit column should be text"
    assert "cannot submit: commit %" in schema, "stale resubmit guard message missing from schema"
    assert re.search(
        r"manually_controlled\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "manually_controlled must be a default-false trigger escape hatch"
    assert re.search(
        r"parked\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "parked must be a default-false dedicated defer marker"
    assert re.search(
        r"regression\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "regression must be a default-false ticket metadata flag"
    assert "create table if not exists ticket_board.workflow_stages" in schema_lower
    assert "create table if not exists ticket_board.workflow_transitions" in schema_lower
    assert "create table if not exists ticket_board.workflow_transition_shadow_log" in schema_lower
    assert "create table if not exists ticket_board.workflow_transition_rbac_shadow_log" in schema_lower
    assert "display_label text not null" in schema_lower
    assert "owner_roles text[] not null default array[]::text[]" in schema_lower
    assert "owner_scoped boolean not null default false" in schema_lower
    assert "director_override boolean not null default false" in schema_lower
    assert "entry_gate_field text" in schema_lower
    assert "gate_skip_to text references ticket_board.workflow_stages(name)" in schema_lower
    assert "exit_signoff_field text" in schema_lower
    assert "is_terminal boolean not null default false" in schema_lower
    assert "hardcoded_allowed boolean not null" in schema_lower
    assert "config_allowed boolean not null" in schema_lower
    workflow_config_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu517_workflow_config_phase0.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create table if not exists ticket_board.workflow_stages" in workflow_config_migration
    assert "create table if not exists ticket_board.workflow_transitions" in workflow_config_migration
    assert "('analysis', 'triage', 2, array['director']::text[]" in workflow_config_migration
    assert "('user_review', 'uat', 6, array['director']::text[]" in workflow_config_migration
    assert "('draft', 'analysis', 'release_draft', array['director', 'user']::text[])" in workflow_config_migration
    cosmetic_derivation_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu520_workflow_config_cosmetic_derivation.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.state_rank" in cosmetic_derivation_migration
    assert "from ticket_board.workflow_stages" in cosmetic_derivation_migration
    assert "from ticket_board.workflow_stages" in executable_schema_lower
    assert "create or replace function ticket_board.state_rank(p_state text)" in executable_schema_lower
    assert "language sql\nstable" in executable_schema_lower
    transition_shadow_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu521_workflow_transition_shadow_mode.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create table if not exists ticket_board.workflow_transition_shadow_log" in transition_shadow_migration
    assert "create or replace function ticket_board.workflow_transition_allowed_hardcoded" in transition_shadow_migration
    assert "create or replace function ticket_board.workflow_transition_allowed_config" in transition_shadow_migration
    assert "create or replace function ticket_board.log_workflow_transition_shadow_mismatch" in transition_shadow_migration
    assert "create or replace function ticket_board.workflow_transition_allowed_hardcoded" in executable_schema_lower
    assert "create or replace function ticket_board.workflow_transition_allowed_config" in executable_schema_lower
    assert "create or replace function ticket_board.log_workflow_transition_shadow_mismatch" in executable_schema_lower
    assert "create or replace function ticket_board.workflow_transition_rbac_allowed_config" in executable_schema_lower
    assert "create or replace function ticket_board.workflow_transition_rbac_owner_scoped_config" in executable_schema_lower
    assert "create or replace function ticket_board.workflow_transition_rbac_director_override_config" in executable_schema_lower
    assert "create or replace function ticket_board.require_workflow_transition_actor" in executable_schema_lower
    assert "create or replace function ticket_board.log_workflow_transition_rbac_shadow_mismatch" in executable_schema_lower
    assert "workflow transition shadow mismatch" in executable_schema_lower
    assert "workflow transition rbac shadow mismatch" in executable_schema_lower
    assert "workflow_transition_allowed_hardcoded(old.state, new.state)" in executable_schema_lower
    assert "workflow_transition_allowed_config(old.state, new.state)" in executable_schema_lower
    rbac_sql = (ROOT / "scripts" / "ticket_board" / "rbac.sql").read_text(encoding="utf-8").lower()
    assert "revoke all on ticket_board.workflow_transition_shadow_log" in rbac_sql
    assert "grant select on ticket_board.workflow_transition_shadow_log" in rbac_sql
    assert "revoke all on ticket_board.workflow_transition_rbac_shadow_log" in rbac_sql
    assert "grant select on ticket_board.workflow_transition_rbac_shadow_log" in rbac_sql
    shadow_log_grants_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu521_zz_shadow_log_read_grants.sql"
    ).read_text(encoding="utf-8").lower()
    assert "grant select on ticket_board.workflow_transition_shadow_log" in shadow_log_grants_migration
    rbac_shadow_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu527_workflow_rbac_shadow.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists owner_scoped" in rbac_shadow_migration
    assert "create table if not exists ticket_board.workflow_transition_rbac_shadow_log" in rbac_shadow_migration
    assert "create or replace function ticket_board.workflow_transition_rbac_allowed_config" in rbac_shadow_migration
    assert "create or replace function ticket_board.log_workflow_transition_rbac_shadow_mismatch" in rbac_shadow_migration
    assert "grant select on ticket_board.workflow_transition_rbac_shadow_log" in rbac_shadow_migration
    rbac_cutover_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu528_workflow_rbac_config_authoritative.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists director_override" in rbac_cutover_migration
    assert "create or replace function ticket_board.require_workflow_transition_actor" in rbac_cutover_migration
    assert "wt.director_override and p_actor = 'director'" in executable_schema_lower
    assert "ticket_board.require_workflow_transition_actor('submit_to_inspection'" in executable_schema_lower
    assert "ticket_board.require_workflow_transition_actor('request_commit_exempt'" in executable_schema_lower
    assert "ticket_board.require_workflow_transition_actor('start_task'" in executable_schema_lower
    assert "if not config_transition_allowed then" in executable_schema_lower
    assert "if not hardcoded_transition_allowed then" not in executable_schema_lower
    config_authoritative_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu522_workflow_config_authoritative.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.enforce_ticket_workflow_update" in config_authoritative_migration
    assert "if not config_transition_allowed then" in config_authoritative_migration
    assert "if not hardcoded_transition_allowed then" not in config_authoritative_migration
    assert "add column if not exists parked boolean not null default false" in schema_lower
    assert "add column if not exists regression boolean not null default false" in schema_lower
    assert "{7,40}" in schema, "commit_hash check must allow historical short hashes"
    assert "create trigger tickets_enforce_workflow_update" in executable_schema_lower
    assert "create trigger tickets_enforce_workflow_insert" in executable_schema_lower
    assert "create trigger tickets_zzz_notify_transition" in executable_schema_lower
    assert "after insert or update on ticket_board.tickets" in executable_schema_lower
    assert "create or replace function ticket_board.enforce_ticket_workflow_update" in executable_schema_lower
    assert "create or replace function ticket_board.enqueue_transition_notification" in executable_schema_lower
    assert "create or replace function ticket_board.ticket_update_message" in executable_schema_lower
    assert "create or replace function ticket_board.notify_ticket_owner_in_place_change" in executable_schema_lower
    assert "create or replace function ticket_board.unblock_transition_target_role" in executable_schema_lower
    assert "create or replace function ticket_board.unblock_transition_message" in executable_schema_lower
    assert "create or replace function ticket_board.enqueue_unblock_notification" in executable_schema_lower
    assert "create or replace function ticket_board.notify_unblocked_dependents" in executable_schema_lower
    assert "if urgent or comment_actor in ('director', 'user') then" in executable_schema_lower
    manual_control_comment_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu519_manual_control_comment_notifications.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.add_comment" in manual_control_comment_migration
    assert "if urgent or comment_actor in ('director', 'user') then" in manual_control_comment_migration
    assert "when p_state = 'backlog' then 'director'" not in executable_schema_lower
    assert "when p_state = 'analysis' and p_assignee = 'unassigned' then 'director'" not in executable_schema_lower
    assert "source_role := nullif(current_setting('ticket_board.notification_source_role', true), '')" in executable_schema_lower
    assert "source_role := nullif(current_setting('ticket_board.caller_role', true), '')" in executable_schema_lower
    assert "source_role is distinct from target_role" in executable_schema_lower
    suppress_self_notify_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "273_suppress_self_notifications.sql"
    ).read_text(encoding="utf-8").lower()
    assert "source_role is distinct from target_role" in suppress_self_notify_migration
    stale_resubmit_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "280_reject_stale_resubmit.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists last_rejected_commit" in stale_resubmit_migration
    assert "cannot submit: commit %" in stale_resubmit_migration
    assert "ticket_last_rejected_commit <> ''" in stale_resubmit_migration
    http_director_create_notify_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "282_http_director_create_notification_source.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.enqueue_transition_notification" in http_director_create_notify_migration
    assert "current_setting('ticket_board.notification_source_role', true)" in http_director_create_notify_migration
    assert "source_role is distinct from target_role" in http_director_create_notify_migration
    assert "coalesce(p_old_state, 'insert')" in http_director_create_notify_migration
    assert "pg_current_xact_id()::text" in http_director_create_notify_migration
    assert "pg_notify" in http_director_create_notify_migration
    assert "jsonb_build_object('kind', 'wake'" in http_director_create_notify_migration
    unblock_notify_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "274_unblock_notifications.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.enqueue_unblock_notification" in unblock_notify_migration
    assert "when p_state = 'backlog' then 'director'" in unblock_notify_migration
    assert "ticket_board.unblock_transition_target_role(t.state, t.assignee) is not null" in unblock_notify_migration
    dead_branch_cleanup_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "275_remove_dead_unblock_analysis_branch.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.unblock_transition_target_role" in dead_branch_cleanup_migration
    assert "when p_state = 'backlog' then 'director'" in dead_branch_cleanup_migration
    assert "when p_state = 'analysis' and p_assignee = 'unassigned' then 'director'" not in dead_branch_cleanup_migration
    backlog_unblock_revert_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "276_revert_backlog_unblock_notifications.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.unblock_transition_target_role" in backlog_unblock_revert_migration
    assert "select ticket_board.transition_target_role(p_state, p_assignee);" in backlog_unblock_revert_migration
    assert "when p_state = 'backlog' then 'director'" not in backlog_unblock_revert_migration
    owner_update_notify_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "277_owner_update_notifications.sql"
    ).read_text(encoding="utf-8").lower()
    assert "ticket_update" in owner_update_notify_migration
    assert "create or replace function ticket_board.notify_ticket_owner_in_place_change" in owner_update_notify_migration
    assert "current_state is not distinct from new_state" in owner_update_notify_migration
    idle_turn_end_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "278_idle_turn_end_nudges.sql"
    ).read_text(encoding="utf-8").lower()
    assert "idle_reminder" in idle_turn_end_migration
    assert "ticket_update" in idle_turn_end_migration
    assert "create or replace function ticket_board.notify_idle_turn_end_nudges" in idle_turn_end_migration
    assert "create or replace function ticket_board.idle_without_advancing_message" in idle_turn_end_migration
    idle_reminder_escalation_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "279_idle_reminder_escalation.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists idle_reminder_count integer not null default 0" in idle_reminder_escalation_migration
    assert "create or replace function ticket_board.idle_without_advancing_escalation_message" in idle_reminder_escalation_migration
    assert "create or replace function ticket_board.idle_without_advancing_director_message" in idle_reminder_escalation_migration
    assert "'escalation'" in idle_reminder_escalation_migration
    assert "idle_reminder_count = idle_reminder_count + 1" in idle_reminder_escalation_migration
    idle_reminder_clause_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "281_idle_reminder_escalate_problem_clause.sql"
    ).read_text(encoding="utf-8").lower()
    assert "tell the director directly what is wrong" in idle_reminder_clause_migration
    assert "tell user directly what is wrong" in idle_reminder_clause_migration
    assert "<the next rung>" not in idle_reminder_clause_migration
    idle_reminder_primary_defer_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "283_idle_reminder_defers_to_primary.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.notify_idle_turn_end_nudges" in idle_reminder_primary_defer_migration
    assert "q.kind not in ('idle_reminder', 'escalation')" in idle_reminder_primary_defer_migration
    assert "q.target_role = candidates.target_role" in idle_reminder_primary_defer_migration
    idle_reminder_delivery_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu437_idle_reminder_delivery_count.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.ack_notification" in idle_reminder_delivery_migration
    assert "when q.kind = 'idle_reminder' then ns.idle_reminder_count + 1" in idle_reminder_delivery_migration
    assert "when q.kind = 'transition' then clock_timestamp()" in idle_reminder_delivery_migration
    assert "create or replace function ticket_board.idle_without_advancing_problem_message" in idle_reminder_delivery_migration
    assert "is waiting in your " in idle_reminder_delivery_migration
    regression_flag_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "284_add_regression_flag.sql"
    ).read_text(encoding="utf-8").lower()
    assert "add column if not exists regression boolean not null default false" in regression_flag_migration
    assert "'regression', ticket_row.regression" in regression_flag_migration
    assert "'regression'" in regression_flag_migration
    require_actor_backstop_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "285_require_actor_caller_role_backstop.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.require_actor" in require_actor_backstop_migration
    assert "caller_role := nullif(current_setting('ticket_board.caller_role', true), '')" in require_actor_backstop_migration
    assert "caller_role <> all(p_allowed_roles)" in require_actor_backstop_migration
    assert "role % cannot call %" in require_actor_backstop_migration
    audit_file_bug_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "286_allow_audit_file_bug.sql"
    ).read_text(encoding="utf-8").lower()
    assert "grant execute on function ticket_board.file_bug(text, text, text) to audit" in audit_file_bug_migration
    assert "create or replace function ticket_board.file_bug" in audit_file_bug_migration
    assert "array['main', 'app', 'ops', 'perf', 'research', 'audit']" in audit_file_bug_migration
    assert "current_actor_role()" in audit_file_bug_migration
    assert "current_app_actor()" in audit_file_bug_migration
    assert "role % cannot call file_bug" in audit_file_bug_migration
    assert "grant execute on function ticket_board.file_bug(text, text, text) to audit" in (
        ROOT / "scripts" / "ticket_board" / "rbac.sql"
    ).read_text(encoding="utf-8").lower()
    active_inprogress_idle_filter_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "287_active_inprogress_idle_filter.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.notify_idle_turn_end_nudges" in active_inprogress_idle_filter_migration
    assert "row_number() over" in active_inprogress_idle_filter_migration
    assert "notification_trace trace" in active_inprogress_idle_filter_migration
    assert "candidates.state <> 'in_progress' or candidates.in_progress_rank = 1" in active_inprogress_idle_filter_migration
    inprogress_stuck_idle_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu541_inprogress_stuck_requires_pane_idle.sql"
    ).read_text(encoding="utf-8").lower()
    assert "create or replace function ticket_board.notify_due_nudges" in inprogress_stuck_idle_migration
    assert "where t.state in ('inspection', 'audit', 'director_review')" in inprogress_stuck_idle_migration
    assert "where t.state in ('in_progress', 'inspection', 'audit', 'director_review')" not in inprogress_stuck_idle_migration
    idle_stall_cadence_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu591_idle_stall_nudge_cadence.sql"
    ).read_text(encoding="utf-8").lower()
    assert "p_cadence interval default interval '30 minutes'" in idle_stall_cadence_migration
    assert "p_cadence interval default interval '30 minutes'" in executable_schema_lower
    drop_orphaned_eric_functions_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu593_drop_orphaned_eric_functions.sql"
    ).read_text(encoding="utf-8").lower()
    assert "pg_get_function_identity_arguments(p.oid)" in drop_orphaned_eric_functions_migration
    assert "drop function if exists %i.%i(%s)" in drop_orphaned_eric_functions_migration
    assert "pg_get_functiondef(p.oid)" in drop_orphaned_eric_functions_migration
    assert "~ '(eric_signoff|eric_review|eric_sign_off|eric_reopen|needs_eric)'" in drop_orphaned_eric_functions_migration
    assert "like '%eric%'" not in drop_orphaned_eric_functions_migration
    terminal_transition_notify_migration = (
        ROOT / "scripts" / "ticket_board" / "migrations" / "pgu405_terminal_transition_assignee_notify.sql"
    ).read_text(encoding="utf-8").lower()
    assert "target_role is null and p_new_state in ('done', 'cancelled')" in terminal_transition_notify_migration
    assert "transition_target_role(p_old_state, p_assignee)" in terminal_transition_notify_migration
    assert "when p_new_state = 'done'" in terminal_transition_notify_migration
    assert "when p_new_state = 'cancelled'" in terminal_transition_notify_migration
    assert "create or replace function ticket_board.idle_without_advancing_problem_message" in executable_schema_lower
    assert "do not do nothing." in executable_schema_lower
    assert "tell '" in executable_schema_lower
    assert "is waiting in your " in executable_schema_lower
    assert "q.kind not in ('idle_reminder', 'escalation')" in executable_schema_lower
    assert "q.target_role = candidates.target_role" in executable_schema_lower
    assert "when q.kind = 'idle_reminder' then ns.idle_reminder_count + 1" in executable_schema_lower
    assert "when q.kind = 'transition' then clock_timestamp()" in executable_schema_lower
    assert "caller_role := nullif(current_setting('ticket_board.caller_role', true), '')" in executable_schema_lower
    assert "caller_role <> all(p_allowed_roles)" in executable_schema_lower
    assert "role % cannot call %" in executable_schema_lower
    assert "array['main', 'app', 'ops', 'perf', 'research', 'audit']" in executable_schema_lower
    assert "current_actor_role()" in executable_schema_lower
    assert "current_app_actor()" in executable_schema_lower
    assert "role % cannot call file_bug" in executable_schema_lower
    assert "candidates.state <> 'in_progress' or candidates.in_progress_rank = 1" in executable_schema_lower
    assert "where t.state in ('inspection', 'audit', 'dat', 'director_review')" in executable_schema_lower
    assert "where t.state in ('in_progress', 'inspection', 'audit', 'director_review')" not in executable_schema_lower
    assert "target_role is null and p_new_state in ('done', 'cancelled')" in executable_schema_lower
    assert "transition_target_role(p_old_state, p_assignee)" in executable_schema_lower
    assert "when p_new_state = 'done'" in executable_schema_lower
    assert "when p_new_state = 'cancelled'" in executable_schema_lower
    assert "create or replace function ticket_board.notify_ticket_state_transition" in executable_schema_lower
    assert "pg_notify" in executable_schema_lower, "PGU-191 must notify on state transitions"
    assert "manually_controlled" in executable_schema_lower, "PGU-191 triggers must honor manual control"
    for function_name in EXPECTED_FUNCTION_API:
        assert f"create or replace function {function_name}" in executable_schema_lower

    print("ticket_board_schema_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
