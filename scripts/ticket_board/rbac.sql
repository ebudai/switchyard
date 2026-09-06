-- PGU ticket-board PostgreSQL roles and grants.
--
-- Single-writer write path: the board service role gets read access plus
-- EXECUTE on narrow SECURITY DEFINER functions. Pane roles keep read access
-- only; app-layer identity decides who may request each write. Direct
-- INSERT/UPDATE/DELETE on ticket_board tables is revoked from every non-owner
-- role; triggers remain the coarse backstop for writes performed inside the
-- functions.

BEGIN;

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['director', 'user', 'ops', 'app', 'audit', 'inspector', 'perf', 'research', 'main'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', role_name);
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

REVOKE ALL ON SCHEMA ticket_board FROM PUBLIC;
GRANT USAGE ON SCHEMA ticket_board TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;

REVOKE ALL ON ALL TABLES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM PUBLIC;

REVOKE ALL ON ticket_board.tickets FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_blockers FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_comments FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_attachments FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_notification_queue FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.notification_trace FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.schema_migrations FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.workflow_stages FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.workflow_transitions FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.workflow_transition_shadow_log FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.workflow_transition_rbac_shadow_log FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON SEQUENCE ticket_board.ticket_comments_id_seq FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON SEQUENCE ticket_board.ticket_notification_queue_id_seq FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON SEQUENCE ticket_board.notification_trace_id_seq FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;

GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.schema_migrations TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.workflow_stages, ticket_board.workflow_transitions TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.workflow_transition_shadow_log TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.workflow_transition_rbac_shadow_log TO director, "user", ops, app, audit, inspector, perf, research, main, ticket_board_service, ticket_board_listener;

GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text[], text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text[], text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text[], text, boolean, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text, text[], text, boolean, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_report(text, text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text) TO audit;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text, text, text[], text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text, text, text[], text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.release_draft(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.route(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.force_move(text, text, text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.start_work(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_inspection(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit_without_commit(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.implementer_kick_back(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.request_commit_exempt(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.start_task(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.complete_task(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_awaiting_role(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.clear_awaiting_role(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_sign_off(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_kick_back(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_kick_back(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.director_dat_sign_off(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.director_dat_kick_back(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.inspector_sign_off(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.inspector_kick_back(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.inspector_kick_back(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.user_sign_off(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.user_reopen(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.mark_done(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.defer(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.cancel(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_manually_controlled(text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_blockers(text, text[], text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.edit_fields(text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.append_ticket_attachment(text, text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.merge(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.dismiss_notification(bigint, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.dismiss_notification_by_key(text, text, text, text) TO ticket_board_service;

GRANT EXECUTE ON FUNCTION ticket_board.claim_notification(timestamptz, interval) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.next_notification_attempt(timestamptz, interval) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.finish_current_blocker(text, text, timestamptz, interval) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.ack_notification(bigint) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.dead_letter_notification(bigint, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.requeue_notification(bigint, interval, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.reset_notification_backoff_for_idle_roles(jsonb, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.record_notification_trace(text, bigint, text, text, text, text, text, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_stall_nudges(jsonb, timestamptz, interval, interval, integer, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_turn_end_nudges(jsonb, timestamptz) TO ticket_board_listener;
-- The delivery currency check (_notification_is_current -> _current_ticket_state)
-- calls ticket_has_unresolved_blockers; the listener role must be able to run it,
-- otherwise every delivery throws "permission denied for function" and no
-- notification is ever sent. Added after PGU-549 introduced the call without a grant.
GRANT EXECUTE ON FUNCTION ticket_board.ticket_has_unresolved_blockers(text) TO ticket_board_listener;

COMMIT;

DO $workflow_grants$ BEGIN
IF to_regclass('ticket_board.workflow_configuration') IS NOT NULL THEN
    GRANT EXECUTE ON FUNCTION ticket_board.transition_target_role(text,text),ticket_board.legacy_transition_target_role(text,text) TO ticket_board_service;

    GRANT EXECUTE ON FUNCTION ticket_board.workflow_wait_stage(text) TO ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.workflow_configuration,ticket_board.workflow_revisions,ticket_board.workflow_roles FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.apply_declared_workflow(jsonb),ticket_board.perform_workflow_action(text,text,jsonb) FROM PUBLIC;
GRANT SELECT ON ticket_board.workflow_configuration,ticket_board.workflow_revisions,ticket_board.workflow_roles TO ticket_board_service,ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.apply_declared_workflow(jsonb),ticket_board.perform_workflow_action(text,text,jsonb) TO ticket_board_service;

GRANT EXECUTE ON FUNCTION ticket_board.apply_declared_workflow(jsonb,bigint), ticket_board.declared_workflow(), ticket_board.workflow_flag(jsonb,text,jsonb), ticket_board.set_workflow_flag(jsonb,text,boolean), ticket_board.enforce_declared_ticket_update(ticket_board.tickets,ticket_board.tickets) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.declared_workflow() TO ticket_board_listener;

GRANT EXECUTE ON FUNCTION ticket_board.set_declared_flags(text,jsonb) TO ticket_board_service;
END IF;
END $workflow_grants$;
