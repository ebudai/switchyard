#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'if [[ -n "${PGDATA_DIR:-}" && -d "$PGDATA_DIR" ]]; then pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null 2>&1 || true; fi; rm -rf "$TMPDIR_T"' EXIT

for tool in initdb pg_ctl createdb psql; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ticket_board_migration_runner_test: skipped, missing $tool"
        exit 0
    }
done

free_port() {
    python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

psql_test() {
    psql -X -v ON_ERROR_STOP=1 -tA "$ADMIN_CONN" "$@"
}

PGDATA_DIR="$TMPDIR_T/pgdata"
SOCKET_DIR="$TMPDIR_T/socket"
MIGRATIONS_DIR="$TMPDIR_T/migrations"
TRANSITION_MIGRATIONS_DIR="$TMPDIR_T/transition-migrations"
mkdir -p "$SOCKET_DIR" "$MIGRATIONS_DIR"
PORT="$(free_port)"
DBNAME="pgu_migration_runner_test"
ADMIN_CONN="host=$SOCKET_DIR port=$PORT dbname=$DBNAME user=postgres"

cat >"$MIGRATIONS_DIR/pgu401_create_probe.sql" <<'SQL'
CREATE SCHEMA IF NOT EXISTS ticket_board;
CREATE TABLE ticket_board.migration_probe (
    id integer PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO ticket_board.migration_probe (id, value) VALUES (1, 'first');
SQL

cat >"$MIGRATIONS_DIR/pgu402_update_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+second'
WHERE id = 1;
SQL

initdb -D "$PGDATA_DIR" -A trust --no-locale --username=postgres >/dev/null
pg_ctl -D "$PGDATA_DIR" -o "-k $SOCKET_DIR -p $PORT -h ''" -w start >/dev/null
createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$DBNAME"

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

applied="$(
    psql_test <<'SQL'
SELECT string_agg(name || ':' || seq::text, ',' ORDER BY seq)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$applied" == "pgu401_create_probe.sql:1,pgu402_update_probe.sql:2" ]] || {
    echo "FAIL: migrations were not recorded correctly: $applied" >&2
    exit 1
}

value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$value" == "first+second" ]] || {
    echo "FAIL: migrations did not apply in numeric order: $value" >&2
    exit 1
}

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

count="$(psql_test -c "SELECT count(*) FROM ticket_board.schema_migrations;")"
[[ "$count" == "2" ]] || {
    echo "FAIL: rerun should not duplicate migration records: $count" >&2
    exit 1
}

value_after_rerun="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$value_after_rerun" == "first+second" ]] || {
    echo "FAIL: rerun should skip applied migrations: $value_after_rerun" >&2
    exit 1
}

cat >"$MIGRATIONS_DIR/pgu403_late_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+third'
WHERE id = 1;
SQL

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

late_applied="$(
    psql_test <<'SQL'
SELECT string_agg(name || ':' || seq::text, ',' ORDER BY seq)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$late_applied" == "pgu401_create_probe.sql:1,pgu402_update_probe.sql:2,pgu403_late_probe.sql:3" ]] || {
    echo "FAIL: late migration was not recorded correctly: $late_applied" >&2
    exit 1
}

late_value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$late_value" == "first+second+third" ]] || {
    echo "FAIL: late migration did not apply exactly once: $late_value" >&2
    exit 1
}

TRANSITION_DBNAME="pgu_migration_runner_transition_test"
TRANSITION_CONN="host=$SOCKET_DIR port=$PORT dbname=$TRANSITION_DBNAME user=postgres"
mkdir -p "$TRANSITION_MIGRATIONS_DIR"
createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$TRANSITION_DBNAME"
psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" <<'SQL' >/dev/null
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener;
    END IF;
END $$;
SQL
psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" -f "$REPO_ROOT/scripts/ticket_board/schema.sql" >/dev/null
psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" <<'SQL' >/dev/null
GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board TO ticket_board_service;
SQL
psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" <<'SQL' >/dev/null
CREATE OR REPLACE FUNCTION ticket_board.append_ticket_comment(
    p_id text,
    p_who text,
    p_text text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.append_ticket_comment(p_id, p_who, p_text, false);
END;
$$;
SQL
cp "$REPO_ROOT/scripts/ticket_board/schema.sql" "$TMPDIR_T/schema.sql"
cp "$REPO_ROOT"/scripts/ticket_board/migrations/*.sql "$TRANSITION_MIGRATIONS_DIR/"
cat >"$TRANSITION_MIGRATIONS_DIR/270_ready_to_in_progress.sql" <<'SQL'
DO $$
BEGIN
    RAISE EXCEPTION 'legacy migration should have been backfilled, not executed';
END;
$$;
SQL
cat >"$TRANSITION_MIGRATIONS_DIR/pgu999_runner_probe.sql" <<'SQL'
CREATE TABLE ticket_board.runner_probe (
    id integer PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO ticket_board.runner_probe (id, value) VALUES (1, 'applied');
SQL

TICKET_BOARD_ADMIN_DATABASE_URL="$TRANSITION_CONN" TICKET_BOARD_MIGRATIONS_DIR="$TRANSITION_MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

transition_applied="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" <<'SQL'
SELECT string_agg(name, ',' ORDER BY seq)
FROM ticket_board.schema_migrations;
SQL
)"
expected_transition_applied="schema.sql,270_ready_to_in_progress.sql,271_optional_implementation.sql,272_resolved_blockers.sql,273_suppress_self_notifications.sql,274_unblock_notifications.sql,275_remove_dead_unblock_analysis_branch.sql,276_revert_backlog_unblock_notifications.sql,277_owner_update_notifications.sql,278_idle_turn_end_nudges.sql,279_idle_reminder_escalation.sql,280_reject_stale_resubmit.sql,281_idle_reminder_escalate_problem_clause.sql,282_http_director_create_notification_source.sql,283_idle_reminder_defers_to_primary.sql,284_add_regression_flag.sql,285_require_actor_caller_role_backstop.sql,286_allow_audit_file_bug.sql,287_active_inprogress_idle_filter.sql,pgu367_unresolved_blocked_by_source_json.sql,pgu405_terminal_transition_assignee_notify.sql,pgu437_idle_reminder_delivery_count.sql,pgu444_finish_current_locked_reconcile.sql,pgu450_in_progress_implementer_assignee.sql,pgu451_serial_implementer_focus.sql,pgu453_awaiting_role_nudge_suppression.sql,pgu455_director_discretionary_nudges.sql,pgu457_close_utility_tasks.sql,pgu458_pgu340_director_force_move.sql,pgu458_submit_to_inspection.sql,pgu471_coalesce_new_ticket_notifications.sql,pgu472_urgent_comment_notifications.sql,pgu474_hard_serial_focus_force_move.sql,pgu477_drop_legacy_append_ticket_comment.sql,pgu479_optional_kickback_comments.sql,pgu480_queued_backlog_nudge_suppression.sql,pgu494_create_ticket_needs_eric_signoff.sql,pgu497_eric_review_notifications.sql,pgu514_draft_stage.sql,pgu517_workflow_config_phase0.sql,pgu519_manual_control_comment_notifications.sql,pgu520_workflow_config_cosmetic_derivation.sql,pgu521_workflow_transition_shadow_mode.sql,pgu521_zz_shadow_log_read_grants.sql,pgu522_workflow_config_authoritative.sql,pgu527_workflow_rbac_shadow.sql,pgu528_workflow_rbac_config_authoritative.sql,pgu531_attachment_crop_metadata.sql,pgu541_inprogress_stuck_requires_pane_idle.sql,pgu544_dat_stage.sql,pgu563_grant_listener_unresolved_blockers.sql,pgu584_stage_default_assignee.sql,pgu588_stage_default_assignee_from_workflow.sql,pgu589_depersonalize_user_role.sql,pgu591_idle_stall_nudge_cadence.sql,pgu593_drop_orphaned_eric_functions.sql,pgu597_project_ticket_prefix.sql,pgu999_runner_probe.sql"
[[ "$transition_applied" == "$expected_transition_applied" ]] || {
    echo "FAIL: schema.sql backfill did not record legacy migration names correctly: $transition_applied" >&2
    exit 1
}

stage_default_result="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" <<'SQL'
WITH expected(state_name, expected_assignee) AS (
    VALUES
        ('analysis', 'director'),
        ('in_progress', NULL),
        ('inspection', 'inspector'),
        ('audit', 'audit'),
        ('dat', 'director'),
        ('user_review', 'director'),
        ('director_review', 'director'),
        ('draft', NULL),
        ('backlog', NULL),
        ('done', NULL),
        ('cancelled', NULL)
)
SELECT count(*) = 0
FROM expected
WHERE ticket_board.stage_default_assignee(state_name) IS DISTINCT FROM expected_assignee;
SQL
)"
[[ "$stage_default_result" == "t" ]] || {
    echo "FAIL: stage_default_assignee did not match workflow_stages single-owner data: $stage_default_result" >&2
    exit 1
}

psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" <<'SQL' >/dev/null
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, manually_controlled,
    created_text, updated_text, created_at, updated_at, source_json
) VALUES
    (
        'PGU-58401', 'Stage assignee handoff', '', 'analysis', 'ops', 'Ready.', false,
        '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00', clock_timestamp(), clock_timestamp(),
        '{"id":"PGU-58401","title":"Stage assignee handoff","body":"","state":"analysis","assignee":"ops","implementation":"Ready.","comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
    ),
    (
        'PGU-58402', 'Manual stage assignee handoff', '', 'analysis', 'ops', 'Ready.', true,
        '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00', clock_timestamp(), clock_timestamp(),
        '{"id":"PGU-58402","title":"Manual stage assignee handoff","body":"","state":"analysis","assignee":"ops","implementation":"Ready.","comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
    ),
    (
        'PGU-58403', 'Force stage assignee handoff', '', 'analysis', 'ops', 'Ready.', false,
        '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00', clock_timestamp(), clock_timestamp(),
        '{"id":"PGU-58403","title":"Force stage assignee handoff","body":"","state":"analysis","assignee":"ops","implementation":"Ready.","comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
    ),
    (
        'PGU-58801', 'Analysis assignee handoff', '', 'backlog', 'unassigned', '', false,
        '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00', clock_timestamp(), clock_timestamp(),
        '{"id":"PGU-58801","title":"Analysis assignee handoff","body":"","state":"backlog","assignee":"unassigned","implementation":"","comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
    ),
    (
        'PGU-58802', 'Implementer analysis auto-advance', '', 'backlog', 'research', 'Ready.', false,
        '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00', clock_timestamp(), clock_timestamp(),
        '{"id":"PGU-58802","title":"Implementer analysis auto-advance","body":"","state":"backlog","assignee":"research","implementation":"Ready.","comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
    );
UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '5840101' WHERE id = 'PGU-58401';
UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '5840202' WHERE id = 'PGU-58402';
BEGIN;
SELECT set_config('ticket_board.force_move', 'on', true);
UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '5840303' WHERE id = 'PGU-58403';
COMMIT;
UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-58801';
UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-58802';
SQL

stage_handoff_result="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" <<'SQL'
SELECT jsonb_object_agg(
    t.id,
    jsonb_build_object(
        'state', t.state,
        'assignee', t.assignee,
        'queue_target', (
            SELECT q.target_role
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = t.id
              AND q.payload->>'new_state' = t.state
            ORDER BY q.id DESC
            LIMIT 1
        )
    )
    ORDER BY t.id
)::text
FROM ticket_board.tickets t
WHERE t.id IN ('PGU-58401', 'PGU-58402', 'PGU-58403', 'PGU-58801', 'PGU-58802');
SQL
)"
expected_stage_handoff_result='{"PGU-58401": {"state": "audit", "assignee": "audit", "queue_target": "audit"}, "PGU-58402": {"state": "audit", "assignee": "ops", "queue_target": null}, "PGU-58403": {"state": "audit", "assignee": "ops", "queue_target": "audit"}, "PGU-58801": {"state": "analysis", "assignee": "director", "queue_target": "director"}, "PGU-58802": {"state": "in_progress", "assignee": "research", "queue_target": "research"}}'
[[ "$stage_handoff_result" == "$expected_stage_handoff_result" ]] || {
    echo "FAIL: migrated DB stage assignee handoff produced wrong state or notification target: $stage_handoff_result" >&2
    exit 1
}

legacy_append_overload_count="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" <<'SQL'
SELECT count(*)
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'ticket_board'
  AND p.proname = 'append_ticket_comment'
  AND p.pronargs = 3;
SQL
)"
[[ "$legacy_append_overload_count" == "0" ]] || {
    echo "FAIL: legacy 3-arg append_ticket_comment overload survived migrations" >&2
    exit 1
}

psql -X -v ON_ERROR_STOP=1 "$TRANSITION_CONN" <<'SQL' >/dev/null
INSERT INTO ticket_board.tickets (id, title, body, state, assignee, created_text, updated_text, created_at, updated_at, source_json)
VALUES
    ('PGU-47701', 'Force move append regression', '', 'analysis', 'unassigned', '2026-07-16T00:00:00+00:00', '2026-07-16T00:00:00+00:00', clock_timestamp(), clock_timestamp(), '{"id":"PGU-47701","title":"Force move append regression","body":"","state":"analysis","assignee":"unassigned","comments":[],"created":"2026-07-16T00:00:00+00:00","updated":"2026-07-16T00:00:00+00:00"}'::jsonb),
    ('PGU-47702', 'User reopen append regression', '', 'user_review', 'director', '2026-07-16T00:00:00+00:00', '2026-07-16T00:00:00+00:00', clock_timestamp(), clock_timestamp(), '{"id":"PGU-47702","title":"User reopen append regression","body":"","state":"user_review","assignee":"director","comments":[],"created":"2026-07-16T00:00:00+00:00","updated":"2026-07-16T00:00:00+00:00"}'::jsonb),
    ('PGU-47703', 'Audit kickback append regression', '', 'audit', 'audit', '2026-07-16T00:00:00+00:00', '2026-07-16T00:00:00+00:00', clock_timestamp(), clock_timestamp(), '{"id":"PGU-47703","title":"Audit kickback append regression","body":"","state":"audit","assignee":"audit","comments":[],"created":"2026-07-16T00:00:00+00:00","updated":"2026-07-16T00:00:00+00:00"}'::jsonb);

SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.force_move('PGU-47701', 'done', 'director', true);
SELECT set_config('ticket_board.caller_role', 'user', false);
SELECT ticket_board.user_reopen('PGU-47702', 'Return for rework regression check.');
SELECT set_config('ticket_board.caller_role', 'audit', false);
SELECT ticket_board.audit_kick_back('PGU-47703', 'Audit kickback regression check.', 'main');
RESET ROLE;
SQL

append_action_result="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" <<'SQL'
SELECT jsonb_object_agg(
    t.id,
    jsonb_build_object(
        'state', t.state,
        'assignee', t.assignee,
        'comments', (SELECT count(*)::int FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id)
    )
    ORDER BY t.id
)::text
FROM ticket_board.tickets t
WHERE t.id IN ('PGU-47701', 'PGU-47702', 'PGU-47703');
SQL
)"
expected_append_action_result='{"PGU-47701": {"state": "done", "assignee": "director", "comments": 1}, "PGU-47702": {"state": "analysis", "assignee": "director", "comments": 1}, "PGU-47703": {"state": "in_progress", "assignee": "main", "comments": 1}}'
[[ "$append_action_result" == "$expected_append_action_result" ]] || {
    echo "FAIL: migrated DB append actions failed or produced wrong state: $append_action_result" >&2
    exit 1
}

transition_value="$(psql -X -v ON_ERROR_STOP=1 -tA "$TRANSITION_CONN" -c "SELECT value FROM ticket_board.runner_probe WHERE id = 1;")"
[[ "$transition_value" == "applied" ]] || {
    echo "FAIL: pgu999_runner_probe.sql did not apply on top of schema.sql snapshot: $transition_value" >&2
    exit 1
}

LIVE_RENAME_DBNAME="pgu_migration_runner_live_rename_test"
LIVE_RENAME_CONN="host=$SOCKET_DIR port=$PORT dbname=$LIVE_RENAME_DBNAME user=postgres"
LIVE_RENAME_MIGRATIONS_DIR="$TMPDIR_T/live-rename-migrations"
mkdir -p "$LIVE_RENAME_MIGRATIONS_DIR"
createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$LIVE_RENAME_DBNAME"
if git -C "$REPO_ROOT" show origin/main:scripts/ticket_board/schema.sql >"$TMPDIR_T/origin-main-schema.sql"; then
    psql -X -v ON_ERROR_STOP=1 "$LIVE_RENAME_CONN" -f "$TMPDIR_T/origin-main-schema.sql" >/dev/null
    psql -X -v ON_ERROR_STOP=1 "$LIVE_RENAME_CONN" <<'SQL' >/dev/null
ALTER TABLE ticket_board.tickets RENAME COLUMN needs_user_signoff TO needs_eric_signoff;
ALTER TABLE ticket_board.tickets RENAME COLUMN user_signoff TO eric_signoff;
ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_state_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_state_check CHECK (state IN (
        'draft',
        'backlog',
        'analysis',
        'in_progress',
        'inspection',
        'audit',
        'dat',
        'eric_review',
        'director_review',
        'done',
        'cancelled'
    ));
ALTER TABLE ticket_board.tickets DISABLE TRIGGER ALL;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, audit_signoff,
    needs_eric_signoff, eric_signoff, created_text, updated_text, created_at, updated_at, source_json
) VALUES (
    'PGU-42901', 'Live old UAT row', '', 'eric_review', 'director', 'Ready.', true,
    true, true, '2026-08-22T00:00:00+00:00', '2026-08-22T00:00:00+00:00',
    clock_timestamp(), clock_timestamp(),
    '{"id":"PGU-42901","title":"Live old UAT row","body":"","state":"eric_review","assignee":"director","implementation":"Ready.","audit_signoff":true,"needs_eric_signoff":true,"eric_signoff":true,"comments":[],"created":"2026-08-22T00:00:00+00:00","updated":"2026-08-22T00:00:00+00:00"}'::jsonb
);
ALTER TABLE ticket_board.tickets ENABLE TRIGGER ALL;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, ts, text, source_json)
VALUES (
    'PGU-42901',
    0,
    'eric',
    '2026-08-22T00:01:00+00:00',
    '2026-08-22T00:01:00+00:00'::timestamptz,
    'Looks good.',
    '{"who":"eric","ts":"2026-08-22T00:01:00+00:00","text":"Looks good."}'::jsonb
);
INSERT INTO ticket_board.ticket_notification_state (
    ticket_id,
    current_state,
    current_assignee,
    previous_state
) VALUES (
    'PGU-42901',
    'eric_review',
    'director',
    'dat'
);
CREATE OR REPLACE FUNCTION ticket_board.eric_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    UPDATE ticket_board.tickets
    SET eric_signoff = true,
        state = 'director_review'
    WHERE tickets.id = eric_sign_off.id
      AND tickets.state = 'eric_review';
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.numeric_survivor(value numeric)
RETURNS numeric
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT value;
$$;
SQL
    cp "$REPO_ROOT/scripts/ticket_board/schema.sql" "$TMPDIR_T/schema.sql"
    cp "$REPO_ROOT/scripts/ticket_board/migrations/pgu589_depersonalize_user_role.sql" "$LIVE_RENAME_MIGRATIONS_DIR/"
    cp "$REPO_ROOT/scripts/ticket_board/migrations/pgu593_drop_orphaned_eric_functions.sql" "$LIVE_RENAME_MIGRATIONS_DIR/"
    TICKET_BOARD_ADMIN_DATABASE_URL="$LIVE_RENAME_CONN" TICKET_BOARD_MIGRATIONS_DIR="$LIVE_RENAME_MIGRATIONS_DIR" \
        "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null
    live_rename_result="$(
        psql -X -v ON_ERROR_STOP=1 -tA "$LIVE_RENAME_CONN" <<'SQL'
SELECT jsonb_build_object(
    'state', t.state,
    'needs_user_signoff', t.needs_user_signoff,
    'user_signoff', t.user_signoff,
    'source_state', t.source_json->>'state',
    'source_needs_user_signoff', t.source_json->'needs_user_signoff',
    'source_user_signoff', t.source_json->'user_signoff',
    'comment_who', (SELECT c.who FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id ORDER BY c.position LIMIT 1),
    'comment_source_who', (SELECT c.source_json->>'who' FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id ORDER BY c.position LIMIT 1),
    'notification_state', (SELECT ns.current_state FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = t.id),
    'old_column_count', (
        SELECT count(*)::int
        FROM information_schema.columns
        WHERE table_schema = 'ticket_board'
          AND table_name = 'tickets'
          AND column_name IN ('needs_eric_signoff', 'eric_signoff')
    ),
    'old_stage_count', (SELECT count(*)::int FROM ticket_board.workflow_stages WHERE name = 'eric_review'),
    'new_stage_count', (SELECT count(*)::int FROM ticket_board.workflow_stages WHERE name = 'user_review'),
    'old_function_exists', to_regprocedure('ticket_board.eric_sign_off(text,text)') IS NOT NULL,
    'old_one_arg_function_exists', to_regprocedure('ticket_board.eric_sign_off(text)') IS NOT NULL,
    'old_identifier_body_count', (
        SELECT count(*)::int
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'ticket_board'
          AND p.prokind = 'f'
          AND lower(pg_get_functiondef(p.oid)) ~ '(eric_signoff|eric_review|eric_sign_off|eric_reopen|needs_eric)'
    ),
    'numeric_survivor_exists', to_regprocedure('ticket_board.numeric_survivor(numeric)') IS NOT NULL,
    'new_function_exists', to_regprocedure('ticket_board.user_sign_off(text,text)') IS NOT NULL,
    'migration_recorded', EXISTS (
        SELECT 1 FROM ticket_board.schema_migrations WHERE name = 'pgu589_depersonalize_user_role.sql'
    ),
    'drift_cleanup_recorded', EXISTS (
        SELECT 1 FROM ticket_board.schema_migrations WHERE name = 'pgu593_drop_orphaned_eric_functions.sql'
    )
)::text
FROM ticket_board.tickets t
WHERE t.id = 'PGU-42901';
SQL
    )"
    expected_live_rename_result='{"state": "user_review", "comment_who": "user", "source_state": "user_review", "user_signoff": true, "new_stage_count": 1, "old_stage_count": 0, "old_column_count": 0, "comment_source_who": "user", "migration_recorded": true, "needs_user_signoff": true, "notification_state": "user_review", "new_function_exists": true, "old_function_exists": false, "source_user_signoff": true, "drift_cleanup_recorded": true, "numeric_survivor_exists": true, "old_identifier_body_count": 0, "source_needs_user_signoff": true, "old_one_arg_function_exists": false}'
    [[ "$live_rename_result" == "$expected_live_rename_result" ]] || {
        echo "FAIL: pgu589 did not migrate old eric_review data cleanly: $live_rename_result" >&2
        exit 1
    }
else
    echo "ticket_board_migration_runner_test: skipped pgu589 old-schema regression, origin/main schema unavailable" >&2
fi

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
mkdir -p "$SOURCE_REPO/scripts/ticket_board/migrations"
git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
cp "$REPO_ROOT/scripts/ticket-board-migrate" "$SOURCE_REPO/scripts/ticket-board-migrate"
chmod +x "$SOURCE_REPO/scripts/ticket-board-migrate"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
cat >"$SOURCE_REPO/scripts/ticket_board/migrations/pgu404_deploy_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+deploy'
WHERE id = 1;
SQL
git -C "$SOURCE_REPO" add scripts/ticket-board-migrate scripts/ticket-board.py scripts/ticket_board/migrations/pgu404_deploy_probe.sql
git -C "$SOURCE_REPO" commit -m "seed deploy migration" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null

deploy_value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$deploy_value" == "first+second+third+deploy" ]] || {
    echo "FAIL: service deploy did not apply pending migration: $deploy_value" >&2
    exit 1
}

deploy_applied="$(
    psql_test <<'SQL'
SELECT string_agg(name || ':' || seq::text, ',' ORDER BY seq)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$deploy_applied" == "pgu401_create_probe.sql:1,pgu402_update_probe.sql:2,pgu403_late_probe.sql:3,pgu404_deploy_probe.sql:4" ]] || {
    echo "FAIL: service deploy did not record the pending migration: $deploy_applied" >&2
    exit 1
}

echo "ticket_board_migration_runner_test: ok"
