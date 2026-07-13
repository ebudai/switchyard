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
    "eric_signoff",
    "id",
    "implementation",
    "manually_controlled",
    "needs_inspection",
    "needs_eric_signoff",
    "parent_id",
    "screenshot",
    "screenshots",
    "state",
    "title",
    "updated",
}

EXPECTED_COMMENT_FIELDS = {"who", "ts", "text"}
EXPECTED_STATES = {
    "backlog",
    "analysis",
    "in_progress",
    "inspection",
    "audit",
    "eric_review",
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
    "ticket_board.route",
    "ticket_board.start_work",
    "ticket_board.submit_to_audit",
    "ticket_board.request_commit_exempt",
    "ticket_board.inspector_sign_off",
    "ticket_board.inspector_kick_back",
    "ticket_board.audit_sign_off",
    "ticket_board.audit_kick_back",
    "ticket_board.eric_sign_off",
    "ticket_board.eric_reopen",
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
    "eric_signoff": ["eric_signoff"],
    "id": ["id", "ticket_number"],
    "implementation": ["implementation"],
    "manually_controlled": ["manually_controlled"],
    "needs_inspection": ["needs_inspection"],
    "needs_eric_signoff": ["needs_eric_signoff"],
    "parent_id": ["parent_id"],
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
    assert "version integer primary key" in schema_lower
    assert "description text not null" in schema_lower
    assert "applied_at timestamptz not null default now()" in schema_lower
    assert "create table if not exists ticket_board.ticket_blockers" in schema_lower
    assert "create table if not exists ticket_board.ticket_comments" in schema_lower
    assert "create table if not exists ticket_board.ticket_attachments" in schema_lower
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
    assert "assignee must not be unassigned before a ticket can enter in_progress" in schema
    assert re.search(
        r"resolved\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "ticket_blockers.resolved must persist blocker resolution"
    assert re.search(
        r"idle_reminder_count\s+integer\s+not null\s+default\s+0",
        schema_lower,
    ), "ticket_notification_state.idle_reminder_count must persist per-episode reminder state"
    assert "unresolved blocker prevents forward promotion" in schema
    assert "create or replace function ticket_board.ticket_is_forward_promotion" in executable_schema_lower
    assert "create or replace function ticket_board.resolve_completed_blockers" in executable_schema_lower
    assert "create trigger tickets_zzzy_resolve_completed_blockers" in executable_schema_lower
    resolved_blockers_migration = (ROOT / "scripts" / "ticket_board" / "migrations" / "272_resolved_blockers.sql").read_text(encoding="utf-8").lower()
    assert "add column if not exists resolved boolean" in resolved_blockers_migration
    assert "unresolved blocker prevents forward promotion" in resolved_blockers_migration
    optional_implementation_migration = (ROOT / "scripts" / "ticket_board" / "migrations" / "271_optional_implementation.sql").read_text(encoding="utf-8")
    assert "implementation must be non-empty before a ticket can enter in_progress" not in optional_implementation_migration
    assert "assignee must not be unassigned before a ticket can enter in_progress" in optional_implementation_migration
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
    assert "add column if not exists parked boolean not null default false" in schema_lower
    assert "{7,40}" in schema, "commit_hash check must allow historical short hashes"
    assert "create trigger tickets_enforce_workflow_update" in executable_schema_lower
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
    assert "when p_state = 'backlog' then 'director'" not in executable_schema_lower
    assert "when p_state = 'analysis' and p_assignee = 'unassigned' then 'director'" not in executable_schema_lower
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
    assert "create or replace function ticket_board.notify_ticket_state_transition" in executable_schema_lower
    assert "pg_notify" in executable_schema_lower, "PGU-191 must notify on state transitions"
    assert "manually_controlled" in executable_schema_lower, "PGU-191 triggers must honor manual control"
    for function_name in EXPECTED_FUNCTION_API:
        assert f"create or replace function {function_name}" in executable_schema_lower

    print("ticket_board_schema_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
