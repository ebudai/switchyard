CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text,
    assignee text,
    blocked_by text[],
    blocked_reason text
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
    target_assignee text := btrim(coalesce(assignee, 'unassigned'));
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    IF target_assignee = '' THEN
        target_assignee := 'unassigned';
    END IF;
    actor := ticket_board.current_actor_role();
    IF actor = 'ticket_board_service' THEN
        PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research', 'audit'], 'file_bug');
        actor := ticket_board.current_app_actor();
    END IF;
    IF actor <> 'ticket_board_service'
       AND actor <> 'audit'
       AND NOT ticket_board.ticket_is_implementer_assignee(actor) THEN
        RAISE EXCEPTION 'role % cannot call file_bug', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF source_id !~ ticket_board.ticket_id_pattern() THEN
        RAISE EXCEPTION 'source_ticket_id must look like PREFIX-N';
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(target_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', target_assignee;
    END IF;
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(blocked_by, ARRAY[]::text[])) AS raw_id;
    PERFORM 1 FROM ticket_board.tickets WHERE id = source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source_ticket_id ticket not found: %', source_id;
    END IF;

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
        target_assignee,
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
            'assignee', target_assignee,
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    IF blocker_count > 0 THEN
        PERFORM ticket_board.apply_blockers(ticket_id, blocked_by, blocked_reason);
    END IF;
    IF actor <> 'ticket_board_service' THEN
        PERFORM ticket_board.append_ticket_comment(ticket_id, actor, 'Filed bug against ' || source_id || '.');
    END IF;
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text,
    assignee text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_bug(title, body, source_ticket_id, assignee, ARRAY[]::text[], '');
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_bug(title, body, source_ticket_id, 'unassigned', ARRAY[]::text[], '');
$$;

GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.file_bug(text, text, text, text, text[], text) TO ticket_board_service;
