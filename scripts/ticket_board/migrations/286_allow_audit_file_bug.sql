GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text) TO audit;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text
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
    source_id text := upper(btrim(coalesce(source_ticket_id, '')));
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor = 'ticket_board_service' THEN
        PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research', 'audit'], 'file_bug');
        actor := ticket_board.current_app_actor();
    END IF;
    IF actor NOT IN ('ticket_board_service', 'main', 'app', 'ops', 'perf', 'research', 'audit') THEN
        RAISE EXCEPTION 'role % cannot call file_bug', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF source_id !~ '^PGU-[0-9]+$' THEN
        RAISE EXCEPTION 'source_ticket_id must look like PGU-N';
    END IF;
    PERFORM 1 FROM ticket_board.tickets WHERE id = source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source_ticket_id ticket not found: %', source_id;
    END IF;

    ticket_id := ticket_board.next_ticket_id();
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parent_id,
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
        source_id,
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
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;
