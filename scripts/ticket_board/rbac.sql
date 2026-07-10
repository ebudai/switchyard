-- PGU ticket-board PostgreSQL roles and grants.
--
-- This script intentionally does not set passwords. Eric provisions credentials
-- separately; these statements only create/login-enable roles and grant the
-- minimum table/column privileges needed by the parallel-run DB board work.

BEGIN;

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'director',
        'ops',
        'app',
        'audit',
        'perf',
        'research',
        'main',
        'ticket_board_service'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', role_name);
        ELSE
            EXECUTE format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', role_name);
        END IF;
    END LOOP;
END;
$$;

REVOKE ALL ON SCHEMA ticket_board FROM PUBLIC;
GRANT USAGE ON SCHEMA ticket_board TO director, ops, app, audit, perf, research, main, ticket_board_service;

REVOKE ALL ON ALL TABLES IN SCHEMA ticket_board FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ticket_board FROM PUBLIC;

REVOKE ALL ON ticket_board.tickets FROM director, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_blockers FROM director, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_comments FROM director, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.ticket_attachments FROM director, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON ticket_board.schema_migrations FROM director, ops, app, audit, perf, research, main, ticket_board_service;
REVOKE ALL ON SEQUENCE ticket_board.ticket_comments_id_seq FROM director, ops, app, audit, perf, research, main, ticket_board_service;

GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO director, ops, app, audit, perf, research, main, ticket_board_service;

GRANT INSERT (
    id,
    title,
    body,
    state,
    assignee,
    parent_id,
    blocked_reason,
    implementation,
    audit_prompt,
    audit_signoff,
    needs_eric_signoff,
    eric_signoff,
    commit_hash,
    commit_exempt,
    screenshot,
    created_text,
    updated_text,
    created_at,
    updated_at,
    source_json,
    source_file_name
) ON ticket_board.tickets TO director, ticket_board_service;

GRANT UPDATE (
    title,
    body,
    state,
    assignee,
    parent_id,
    blocked_reason,
    implementation,
    audit_prompt,
    audit_signoff,
    needs_eric_signoff,
    eric_signoff,
    commit_hash,
    commit_exempt,
    screenshot,
    created_text,
    updated_text,
    created_at,
    updated_at,
    source_json,
    source_file_name,
    imported_at,
    row_updated_at
) ON ticket_board.tickets TO director, ticket_board_service;

GRANT UPDATE (manually_controlled) ON ticket_board.tickets TO director;

GRANT UPDATE (
    state,
    assignee,
    blocked_reason,
    implementation,
    audit_prompt,
    commit_hash,
    commit_exempt,
    updated_text,
    updated_at,
    source_json,
    row_updated_at
) ON ticket_board.tickets TO ops, app, perf, research, main;

GRANT UPDATE (
    state,
    audit_prompt,
    audit_signoff,
    commit_hash,
    commit_exempt,
    updated_text,
    updated_at,
    source_json,
    row_updated_at
) ON ticket_board.tickets TO audit;

GRANT INSERT, UPDATE, DELETE ON ticket_board.ticket_blockers TO director, ticket_board_service;
GRANT INSERT, UPDATE, DELETE ON ticket_board.ticket_attachments TO director, ticket_board_service;
GRANT INSERT, UPDATE, DELETE ON ticket_board.ticket_comments TO director, ticket_board_service;
GRANT INSERT ON ticket_board.ticket_comments TO ops, app, audit, perf, research, main;
GRANT USAGE, SELECT ON SEQUENCE ticket_board.ticket_comments_id_seq TO director, ops, app, audit, perf, research, main, ticket_board_service;

GRANT SELECT ON ticket_board.schema_migrations TO director, ops, app, audit, perf, research, main, ticket_board_service;

COMMIT;
