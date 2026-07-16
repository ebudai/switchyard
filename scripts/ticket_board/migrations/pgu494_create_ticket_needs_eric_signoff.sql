CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    blocked_by text[],
    blocked_reason text,
    needs_eric_signoff boolean
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
    normalized_assignee text := 'unassigned';
    normalized_parked boolean := false;
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'eric'], 'create_ticket');
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_state = '' THEN
        normalized_state := 'analysis';
    END IF;
    IF normalized_state NOT IN ('analysis', 'backlog') THEN
        RAISE EXCEPTION 'invalid create state: %; allowed: analysis, backlog', normalized_state;
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
        needs_eric_signoff,
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
        coalesce(needs_eric_signoff, false),
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
            'needs_eric_signoff', coalesce(needs_eric_signoff, false),
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
    blocked_reason text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, blocked_by, blocked_reason, false);
$$;

GRANT EXECUTE ON FUNCTION ticket_board.create_ticket(text, text, text, text[], text, boolean) TO ticket_board_service;
