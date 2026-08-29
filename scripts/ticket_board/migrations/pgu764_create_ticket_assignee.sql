CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    assignee text,
    blocked_by text[],
    blocked_reason text,
    needs_user_signoff boolean,
    needs_audit boolean
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_id text;
    normalized_state text := btrim(coalesce(initial_state, 'analysis'));
    normalized_assignee text := btrim(coalesce(assignee, 'unassigned'));
    normalized_parked boolean := false;
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'user'], 'create_ticket');
    IF normalized_assignee = '' THEN
        normalized_assignee := 'unassigned';
    END IF;
    IF NOT coalesce(needs_audit, true) AND ticket_board.current_app_actor() <> 'director' THEN
        RAISE EXCEPTION 'needs_audit can only be set to false by director' USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_state = '' THEN
        normalized_state := 'analysis';
    END IF;
    IF normalized_state NOT IN ('draft', 'analysis', 'backlog') THEN
        RAISE EXCEPTION 'invalid create state: %; allowed: draft, analysis, backlog', normalized_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(normalized_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', normalized_assignee;
    END IF;
    IF normalized_state = 'draft' AND normalized_assignee <> 'unassigned' THEN
        RAISE EXCEPTION 'draft tickets cannot be created with an assignee; release and route the draft instead';
    END IF;
    IF normalized_state = 'backlog' THEN
        normalized_parked := true;
    END IF;
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(blocked_by, ARRAY[]::text[])) AS raw_id;

    ticket_id := ticket_board.next_ticket_id();
    IF blocker_count > 0 THEN
        PERFORM set_config('ticket_board.suppress_create_notify', 'on', true);
    END IF;
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parked,
        needs_audit,
        needs_user_signoff,
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
        normalized_state,
        normalized_assignee,
        normalized_parked,
        coalesce(needs_audit, true),
        coalesce(needs_user_signoff, false),
        created_text_value,
        created_text_value,
        created_at_value,
        created_at_value,
        jsonb_build_object(
            'id', ticket_id,
            'title', btrim(title),
            'body', coalesce(body, ''),
            'state', normalized_state,
            'assignee', normalized_assignee,
            'parked', normalized_parked,
            'needs_audit', coalesce(needs_audit, true),
            'needs_user_signoff', coalesce(needs_user_signoff, false),
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    IF blocker_count > 0 THEN
        PERFORM ticket_board.apply_blockers(ticket_id, blocked_by, blocked_reason);
    END IF;
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    blocked_by text[],
    blocked_reason text,
    needs_user_signoff boolean,
    needs_audit boolean
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, 'unassigned', blocked_by, blocked_reason, needs_user_signoff, needs_audit);
$$;

GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text, text[], text, boolean, boolean) TO ticket_board_service;
