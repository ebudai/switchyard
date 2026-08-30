CREATE OR REPLACE FUNCTION ticket_board.dismiss_notification_by_key(
    p_ticket_id text,
    p_target_role text,
    p_kind text DEFAULT 'transition',
    p_reason text DEFAULT ''
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    notification_id bigint;
    normalized_ticket_id text := upper(btrim(coalesce(p_ticket_id, '')));
    normalized_target_role text := lower(btrim(coalesce(p_target_role, '')));
    normalized_kind text := lower(coalesce(nullif(btrim(p_kind), ''), 'transition'));
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'dismiss_notification');
    IF normalized_ticket_id = '' THEN
        RAISE EXCEPTION 'dismiss_notification_by_key requires a non-empty ticket_id';
    END IF;
    IF normalized_target_role = '' THEN
        RAISE EXCEPTION 'dismiss_notification_by_key requires a non-empty target_role';
    END IF;

    SELECT q.id
    INTO notification_id
    FROM ticket_board.ticket_notification_queue q
    WHERE q.ticket_id = normalized_ticket_id
      AND q.target_role = normalized_target_role
      AND q.kind = normalized_kind
    ORDER BY q.dead_lettered_at NULLS LAST, q.created_at, q.id
    LIMIT 1
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'notification for ticket %, target %, kind % not found',
            normalized_ticket_id,
            normalized_target_role,
            normalized_kind;
    END IF;

    PERFORM ticket_board.dismiss_notification(notification_id, p_reason);
    RETURN notification_id;
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.dismiss_notification_by_key(text, text, text, text) TO ticket_board_service;
