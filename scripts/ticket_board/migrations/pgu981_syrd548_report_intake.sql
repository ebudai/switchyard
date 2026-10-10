-- SYRD-548: a tenant report may ask for Backlog, and the Director decides whether it lands there.
--
-- A report always landed in Triage (`analysis`), unassigned, so a tenant that
-- wanted its report deferred needed this board's Director to move it -- spending
-- that Director's turn on exactly the work the request said could wait.
--
-- file_report now takes a requested stage. The only stage a report may ask for
-- is `backlog`; the request is the reporter's, never an instruction, so it is
-- honoured only when the Director's report-intake policy says so, and the board
-- has a backlog stage. Either way the request is written on the ticket, by the
-- board service, saying what was asked and what the policy did with it.
--
-- The policy is one row, set only by the Director (`set_report_intake`), and
-- absent means Triage: nothing changes on a board whose Director never chose.
-- The report token still opens nothing else -- no assignee, no flag, no other
-- stage.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.report_intake_policy (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    backlog_requests_land_in_backlog boolean NOT NULL,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (btrim(reason) <> '')
);

CREATE OR REPLACE FUNCTION ticket_board.report_intake()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT coalesce(
        (SELECT jsonb_build_object(
            'backlog_requests', CASE WHEN p.backlog_requests_land_in_backlog THEN 'backlog' ELSE 'triage' END,
            'set_by', p.set_by,
            'set_at', ticket_board.utc_text(p.set_at),
            'reason', p.reason)
         FROM ticket_board.report_intake_policy p),
        jsonb_build_object('backlog_requests', 'triage', 'set_by', '', 'set_at', '', 'reason', '')
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_report_intake(
    p_backlog_requests_land_in_backlog boolean,
    p_reason text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    who text;
BEGIN
    -- Where a report lands is a stage only the Director could otherwise give it,
    -- so this takes the Director's edit authority; on a declared board, the role
    -- whose declared capabilities include director_edit, by whatever name.
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    IF p_backlog_requests_land_in_backlog IS NULL THEN
        RAISE EXCEPTION 'set_report_intake needs a decision: backlog or triage';
    END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'set_report_intake needs a reason';
    END IF;
    INSERT INTO ticket_board.report_intake_policy (id, backlog_requests_land_in_backlog, set_by, set_at, reason)
    VALUES (true, p_backlog_requests_land_in_backlog, who, clock_timestamp(), btrim(p_reason))
    ON CONFLICT (id) DO UPDATE
    SET backlog_requests_land_in_backlog = EXCLUDED.backlog_requests_land_in_backlog,
        set_by = EXCLUDED.set_by,
        set_at = EXCLUDED.set_at,
        reason = EXCLUDED.reason;
    RETURN ticket_board.report_intake();
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_report(
    title text,
    body text,
    origin_project text,
    external_source_ref text,
    requested_stage text
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
    normalized_requested_stage text := lower(btrim(coalesce(requested_stage, '')));
    landing_stage text := 'analysis';
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
    actor text := ticket_board.current_actor_role();
BEGIN
    -- Report-only authority, deliberately NOT require_actor (SYRD-256; the
    -- reasoning is in migration pgu957). The database writer must be the service, and
    -- what this may do is fixed below -- a new ticket, unassigned, audit
    -- required, in Triage unless the Director's policy honours a Backlog request.
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
    -- SYRD-548: a report may ask for Backlog and for nothing else. The request
    -- is the reporter's; whether it is honoured is the Director's policy.
    IF normalized_requested_stage NOT IN ('', 'backlog') THEN
        RAISE EXCEPTION 'a report may request backlog or nothing, not %', requested_stage;
    END IF;
    IF normalized_requested_stage = 'backlog'
       AND coalesce((SELECT p.backlog_requests_land_in_backlog FROM ticket_board.report_intake_policy p), false)
       AND EXISTS (SELECT 1 FROM ticket_board.workflow_stages s WHERE s.name = 'backlog') THEN
        landing_stage := 'backlog';
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
        landing_stage,
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
            'state', landing_stage,
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
    IF normalized_requested_stage <> '' THEN
        PERFORM ticket_board.append_ticket_comment(
            ticket_id,
            'ticket_board_service',
            CASE WHEN landing_stage = 'backlog' THEN
                format('Report from %s requested Backlog (deferred). This board''s report-intake policy honours '
                       'that request, so the report was created in Backlog. The Director can route it to Triage '
                       'at any time.', normalized_origin_project)
            ELSE
                format('Report from %s requested Backlog (deferred). This board''s report-intake policy keeps '
                       'reports in Triage, so it was created here with the request recorded; the Director '
                       'decides whether to defer it.', normalized_origin_project)
            END
        );
    END IF;
    -- The actor is named for the refresh only, as REPORT_ACTOR (SYRD-256): not a
    -- role, unable to become one, and in no transition's actor list.
    PERFORM set_config('ticket_board.workflow_actor', 'report:tenant', true);
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    RETURN ticket_id;
END;
$$;

-- The four-argument form every client before SYRD-548 calls: a report that asks
-- for nothing, exactly as before.
CREATE OR REPLACE FUNCTION ticket_board.file_report(
    title text,
    body text,
    origin_project text,
    external_source_ref text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_report(title, body, origin_project, external_source_ref, '');
$$;

REVOKE ALL ON ticket_board.report_intake_policy FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.report_intake() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.set_report_intake(boolean, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.file_report(text, text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.report_intake() TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_report_intake(boolean, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_report(text, text, text, text, text) TO ticket_board_service;

COMMIT;
