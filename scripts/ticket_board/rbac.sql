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
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

REVOKE ALL ON SCHEMA ticket_board FROM PUBLIC;
GRANT USAGE ON SCHEMA ticket_board TO director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;

REVOKE ALL ON ALL TABLES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM PUBLIC;

REVOKE ALL ON ticket_board.tickets FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_blockers FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_comments FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.ticket_attachments FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON ticket_board.schema_migrations FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE ALL ON SEQUENCE ticket_board.ticket_comments_id_seq FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;

GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.schema_migrations TO director, eric, ops, app, audit, perf, research, main, ticket_board_service, ticket_board_listener;

GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.route(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.start_work(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_sign_off(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_kick_back(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.eric_sign_off(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.eric_reopen(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.mark_done(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.defer(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.cancel(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_manually_controlled(text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_blockers(text, text[], text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.edit_fields(text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.merge(text, text) TO ticket_board_service;

COMMIT;
