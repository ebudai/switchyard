-- PGU ticket-board PostgreSQL roles and grants.
--
-- Model B write path: pane roles get read access plus EXECUTE on narrow
-- SECURITY DEFINER functions. Direct INSERT/UPDATE/DELETE on ticket_board
-- tables is revoked from every non-owner role; triggers remain the coarse
-- backstop for writes performed inside the functions.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END;
$$;

REVOKE ALL ON SCHEMA ticket_board FROM PUBLIC;
GRANT USAGE ON SCHEMA ticket_board TO director, eric, ops, app, audit, perf, research, main, ticket_board_service;

REVOKE ALL ON ALL TABLES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM PUBLIC;

REVOKE ALL ON ticket_board.tickets FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_blockers FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_comments FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_attachments FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.schema_migrations FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON SEQUENCE ticket_board.ticket_comments_id_seq FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board FROM director, eric, ops, app, audit, perf, research, main, ticket_board_service;

GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO director, eric, ops, app, audit, perf, research, main, ticket_board_service;
GRANT SELECT ON ticket_board.schema_migrations TO director, eric, ops, app, audit, perf, research, main, ticket_board_service;

GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text) TO director, eric;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text) TO main, app, ops, perf, research;
GRANT EXECUTE ON FUNCTION ticket_board.route(text, text, text) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.start_work(text) TO main, app, ops, perf, research;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit(text, text) TO main, app, ops, perf, research;
GRANT EXECUTE ON FUNCTION ticket_board.audit_sign_off(text) TO audit;
GRANT EXECUTE ON FUNCTION ticket_board.audit_kick_back(text, text) TO audit;
GRANT EXECUTE ON FUNCTION ticket_board.eric_sign_off(text) TO eric;
GRANT EXECUTE ON FUNCTION ticket_board.eric_reopen(text, text) TO eric;
GRANT EXECUTE ON FUNCTION ticket_board.mark_done(text, text) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.defer(text) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.cancel(text, text) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.set_manually_controlled(text, boolean) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.set_blockers(text, text[], text) TO director;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text) TO director, eric, main, app, ops, audit, perf, research;

COMMIT;
