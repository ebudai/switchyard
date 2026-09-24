-- SYRD-256: a tenant report could not be filed on a declared-workflow board.
--
-- The report-only HTTP action authenticates X-Ticket-Board-Report-Token and
-- calls ticket_board.file_report with no caller role -- by design, since the
-- report credential is not a role. file_report then called
-- require_actor(ARRAY[]::text[], 'file_report'). On a legacy board that passes
-- for the service writer with no role; on a board running a declared workflow
-- require_actor resolves current_app_actor(), which refuses a NULL role:
--
--     invalid configured caller role: <NULL>
--
-- That is the MEFP Director's live upstream report, which got past the token
-- check (SYRD-238) and then failed here with no ticket created.
--
-- file_report now checks its own, narrower authority instead: the database
-- writer must be ticket_board_service, and no role is consulted. It neither
-- refuses the report path's missing role nor lends it any role's capabilities.
-- What it may write is unchanged and fixed by the function itself: a new
-- ticket in analysis, unassigned, audit required, carrying an origin project.
-- require_actor is untouched, so no other operation's authority moves.
--
-- The definition below is the one in schema.sql, character for character, and
-- tests/tenant_report_declared_workflow_test.py asserts they stay that way: a
-- migration copy that drifts is the copy an upgraded board actually runs.
--
-- Numbered after pgu956 deliberately: ticket-board-migrate applies these in
-- alphabetical order, and this must replace the copy pgu765 installed.
--
-- Idempotent by construction: CREATE OR REPLACE, and a grant that is a no-op
-- when it is already held. The signature is unchanged, so rbac.sql and every
-- existing grant keep applying.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.file_report(
    title text,
    body text,
    origin_project text,
    external_source_ref text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    ticket_id text;
    normalized_origin_project text := btrim(coalesce(origin_project, ''));
    normalized_external_source_ref text := btrim(coalesce(external_source_ref, ''));
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
    actor text := ticket_board.current_actor_role();
BEGIN
    -- Report-only authority, deliberately NOT require_actor (SYRD-256).
    --
    -- A tenant report arrives on its own HTTP action, authenticated by the
    -- report token and by nothing else: it carries no caller role, because the
    -- whole point of that credential is that it is not one. require_actor
    -- resolves a role. On a legacy board it tolerated a missing one; on a
    -- declared-workflow board it asks current_app_actor(), which refuses NULL,
    -- so every report filed against a declared board failed with
    -- `invalid configured caller role: <NULL>` after the token had already been
    -- accepted -- the MEFP Director's live upstream report.
    --
    -- Resolving a role here would be wrong in either direction. Refusing the
    -- NULL role breaks the one caller this function has; admitting it by
    -- borrowing some role's capabilities would hand a report token that role's
    -- authority. So the role is not consulted at all: the database writer must
    -- be the service, and what the function may do is fixed below -- a new
    -- ticket in analysis, unassigned, audit required, no protected field
    -- settable by the caller. A role that somehow reached this function would
    -- gain nothing by carrying one.
    IF actor IS DISTINCT FROM 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call file_report; ticket_board_service is the only database writer', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_origin_project = '' THEN
        RAISE EXCEPTION 'origin_project must be non-empty';
    END IF;

    ticket_id := ticket_board.next_ticket_id();
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parent_id,
        origin_project,
        external_source_ref,
        needs_audit,
        created_text,
        updated_text,
        created_at,
        updated_at,
        source_json,
        source_file_name
    ) VALUES (
        ticket_id,
        btrim(title),
        coalesce(body, ''),
        'analysis',
        'unassigned',
        '',
        normalized_origin_project,
        normalized_external_source_ref,
        true,
        created_text_value,
        created_text_value,
        created_at_value,
        created_at_value,
        jsonb_build_object(
            'id', ticket_id,
            'title', btrim(title),
            'body', coalesce(body, ''),
            'state', 'analysis',
            'assignee', 'unassigned',
            'parent_id', '',
            'origin_project', normalized_origin_project,
            'external_source_ref', normalized_external_source_ref,
            'needs_audit', true,
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    -- The export projection is rebuilt the way every writer rebuilds it, and
    -- that is an UPDATE, which on a declared board runs
    -- enforce_declared_ticket_update. That trigger requires a named actor
    -- before anything else and, with none, asks current_app_actor() -- which
    -- refuses the report path's missing role a second time.
    --
    -- So the actor is named, for this one statement, as REPORT_ACTOR: not a
    -- role, and unable ever to become one, because workflow_roles.name is
    -- CHECKed against ^[a-z][a-z0-9_-]{0,63}$ and this contains a colon. It
    -- satisfies "some actor is named" and nothing more: it is in no
    -- transition's actor list, it is not 'director', and so it can neither
    -- move, reassign, nor flip a flag. Every structural check the trigger
    -- makes still runs. It is set immediately before the refresh and cleared
    -- immediately after, exactly as the workflow executor scopes the same
    -- setting around its own update.
    PERFORM set_config('ticket_board.workflow_actor', 'report:tenant', true);
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    RETURN ticket_id;
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.file_report(text, text, text, text) TO ticket_board_service;

COMMIT;
