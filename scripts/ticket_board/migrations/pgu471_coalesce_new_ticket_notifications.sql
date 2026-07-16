CREATE OR REPLACE FUNCTION ticket_board.enqueue_transition_notification(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_assignee text,
    p_updated_at timestamptz,
    p_ticket_number integer,
    p_message text
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_role text;
    message text;
    source_role text;
    dedupe_key text;
BEGIN
    target_role := ticket_board.transition_target_role(p_new_state, p_assignee);
    IF target_role IS NULL AND p_new_state IN ('done', 'cancelled') THEN
        target_role := ticket_board.transition_target_role(p_old_state, p_assignee);
    END IF;
    message := p_message;
    source_role := nullif(current_setting('ticket_board.notification_source_role', true), '');
    IF source_role IS NULL THEN
        source_role := nullif(current_setting('ticket_board.caller_role', true), '');
    END IF;
    IF target_role IS NOT NULL AND message IS NOT NULL AND source_role IS DISTINCT FROM target_role THEN
        dedupe_key := 'transition:' || p_ticket_id || ':' || coalesce(p_old_state, 'insert') || ':' || p_new_state || ':' || pg_current_xact_id()::text;
        IF p_new_state IN ('analysis', 'in_progress')
           AND message LIKE 'New ticket for you: %' THEN
            dedupe_key := 'transition:new_ticket:' || p_ticket_id || ':' || target_role || ':' || pg_current_xact_id()::text;
        END IF;
        PERFORM ticket_board.enqueue_notification(
            p_ticket_id,
            'transition',
            target_role,
            message,
            jsonb_build_object(
                'kind', 'transition',
                'id', p_ticket_id,
                'title', p_title,
                'old_state', p_old_state,
                'new_state', p_new_state,
                'assignee', p_assignee,
                'updated_at', p_updated_at,
                'ticket_number', p_ticket_number,
                'target_role', target_role,
                'message', message
            ),
            dedupe_key
        );
    ELSE
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object('kind', 'wake', 'id', p_ticket_id, 'new_state', p_new_state)::text
        );
    END IF;
END;
$$;

