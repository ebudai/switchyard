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
expected_transition_applied="schema.sql,270_ready_to_in_progress.sql,271_optional_implementation.sql,272_resolved_blockers.sql,273_suppress_self_notifications.sql,274_unblock_notifications.sql,275_remove_dead_unblock_analysis_branch.sql,276_revert_backlog_unblock_notifications.sql,277_owner_update_notifications.sql,278_idle_turn_end_nudges.sql,279_idle_reminder_escalation.sql,280_reject_stale_resubmit.sql,281_idle_reminder_escalate_problem_clause.sql,282_http_director_create_notification_source.sql,283_idle_reminder_defers_to_primary.sql,284_add_regression_flag.sql,285_require_actor_caller_role_backstop.sql,286_allow_audit_file_bug.sql,287_active_inprogress_idle_filter.sql,pgu367_unresolved_blocked_by_source_json.sql,pgu405_terminal_transition_assignee_notify.sql,pgu437_idle_reminder_delivery_count.sql,pgu444_finish_current_locked_reconcile.sql,pgu450_in_progress_implementer_assignee.sql,pgu451_serial_implementer_focus.sql,pgu453_awaiting_role_nudge_suppression.sql,pgu455_director_discretionary_nudges.sql,pgu457_close_utility_tasks.sql,pgu458_pgu340_director_force_move.sql,pgu458_submit_to_inspection.sql,pgu471_coalesce_new_ticket_notifications.sql,pgu472_urgent_comment_notifications.sql,pgu474_hard_serial_focus_force_move.sql,pgu477_drop_legacy_append_ticket_comment.sql,pgu479_optional_kickback_comments.sql,pgu480_queued_backlog_nudge_suppression.sql,pgu494_create_ticket_needs_eric_signoff.sql,pgu497_eric_review_notifications.sql,pgu514_draft_stage.sql,pgu517_workflow_config_phase0.sql,pgu519_manual_control_comment_notifications.sql,pgu520_workflow_config_cosmetic_derivation.sql,pgu521_workflow_transition_shadow_mode.sql,pgu521_zz_shadow_log_read_grants.sql,pgu522_workflow_config_authoritative.sql,pgu527_workflow_rbac_shadow.sql,pgu528_workflow_rbac_config_authoritative.sql,pgu531_attachment_crop_metadata.sql,pgu999_runner_probe.sql"
[[ "$transition_applied" == "$expected_transition_applied" ]] || {
    echo "FAIL: schema.sql backfill did not record legacy migration names correctly: $transition_applied" >&2
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
    ('PGU-47702', 'Eric reopen append regression', '', 'eric_review', 'director', '2026-07-16T00:00:00+00:00', '2026-07-16T00:00:00+00:00', clock_timestamp(), clock_timestamp(), '{"id":"PGU-47702","title":"Eric reopen append regression","body":"","state":"eric_review","assignee":"director","comments":[],"created":"2026-07-16T00:00:00+00:00","updated":"2026-07-16T00:00:00+00:00"}'::jsonb),
    ('PGU-47703', 'Audit kickback append regression', '', 'audit', 'audit', '2026-07-16T00:00:00+00:00', '2026-07-16T00:00:00+00:00', clock_timestamp(), clock_timestamp(), '{"id":"PGU-47703","title":"Audit kickback append regression","body":"","state":"audit","assignee":"audit","comments":[],"created":"2026-07-16T00:00:00+00:00","updated":"2026-07-16T00:00:00+00:00"}'::jsonb);

SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.force_move('PGU-47701', 'done', 'director', true);
SELECT set_config('ticket_board.caller_role', 'eric', false);
SELECT ticket_board.eric_reopen('PGU-47702', 'Return for rework regression check.');
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
