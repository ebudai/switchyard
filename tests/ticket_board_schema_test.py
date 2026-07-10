#!/usr/bin/env python3
"""Static checks for the ticket-board PostgreSQL schema."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
LIVE_STORE = Path("/home/agent/.claude/pgu-tickets")

EXPECTED_JSON_FIELDS = {
    "assignee",
    "audit_prompt",
    "audit_signoff",
    "blocked_by",
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
    "ready",
    "in_progress",
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
}

FIELD_TO_SCHEMA_TOKENS = {
    "assignee": ["assignee"],
    "audit_prompt": ["audit_prompt"],
    "audit_signoff": ["audit_signoff"],
    "blocked_by": ["ticket_blockers", "blocker_ticket_id"],
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
    "needs_eric_signoff": ["needs_eric_signoff"],
    "parent_id": ["parent_id"],
    "screenshot": ["screenshot", "ticket_attachments"],
    "screenshots": ["ticket_attachments"],
    "state": ["state"],
    "title": ["title"],
    "updated": ["updated_text", "updated_at"],
}


def load_live_field_sets() -> tuple[set[str], set[str], set[str], set[str]]:
    ticket_fields: set[str] = set()
    comment_fields: set[str] = set()
    states: set[str] = set()
    assignees: set[str] = set()
    if not LIVE_STORE.is_dir():
        return EXPECTED_JSON_FIELDS, EXPECTED_COMMENT_FIELDS, set(), set()

    for path in LIVE_STORE.glob("PGU-*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        ticket_fields.update(payload)
        if isinstance(payload.get("comments"), list):
            for comment in payload["comments"]:
                if isinstance(comment, dict):
                    comment_fields.update(comment)
        if isinstance(payload.get("state"), str):
            states.add(payload["state"])
        if isinstance(payload.get("assignee"), str):
            assignees.add(payload["assignee"])
    return ticket_fields, comment_fields, states, assignees


def assert_contains_all(schema: str, values: set[str], label: str) -> None:
    missing = sorted(value for value in values if value not in schema)
    assert not missing, f"{label} missing from schema: {missing}"


def main() -> int:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    schema_lower = schema.lower()
    executable_schema_lower = "\n".join(
        line for line in schema_lower.splitlines() if not line.lstrip().startswith("--")
    )
    live_fields, live_comment_fields, live_states, live_assignees = load_live_field_sets()

    unknown_fields = live_fields - EXPECTED_JSON_FIELDS
    assert not unknown_fields, f"live ticket store has fields not modeled by this test: {sorted(unknown_fields)}"

    for field in sorted(live_fields):
        tokens = FIELD_TO_SCHEMA_TOKENS[field]
        assert_contains_all(schema, set(tokens), f"field {field!r}")

    for field in sorted(live_comment_fields):
        assert field in EXPECTED_COMMENT_FIELDS, f"unexpected live comment field: {field}"
        assert field in schema, f"comment field {field!r} missing from schema"

    assert "source_json jsonb not null" in schema_lower, "tickets.source_json is required for lossless import"
    assert "create table if not exists ticket_board.ticket_blockers" in schema_lower
    assert "create table if not exists ticket_board.ticket_comments" in schema_lower
    assert "create table if not exists ticket_board.ticket_attachments" in schema_lower

    assert_contains_all(schema, EXPECTED_STATES | live_states, "state constraint")
    assert_contains_all(schema, EXPECTED_ASSIGNEES | live_assignees, "assignee constraint")

    assert re.search(r"commit_hash\s+text\s+not null", schema_lower), "commit_hash column should be text"
    assert re.search(
        r"manually_controlled\s+boolean\s+not null\s+default\s+false",
        schema_lower,
    ), "manually_controlled must be a default-false trigger escape hatch"
    assert "{7,40}" in schema, "commit_hash check must allow historical short hashes"
    assert "create trigger tickets_enforce_workflow_update" in executable_schema_lower
    assert "create trigger tickets_notify_state_transition" in executable_schema_lower
    assert "create or replace function ticket_board.enforce_ticket_workflow_update" in executable_schema_lower
    assert "create or replace function ticket_board.notify_ticket_state_transition" in executable_schema_lower
    assert "pg_notify" in executable_schema_lower, "PGU-191 must notify on state transitions"
    assert "manually_controlled" in executable_schema_lower, "PGU-191 triggers must honor manual control"
    for function_name in EXPECTED_FUNCTION_API:
        assert f"create or replace function {function_name}" in executable_schema_lower

    print("ticket_board_schema_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
