CREATE OR REPLACE FUNCTION ticket_board.reset_notification_backoff_for_idle_roles(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    reset_count integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('reset_notification_backoff_for_idle_roles');

    WITH idle_roles AS (
        SELECT lower(btrim(key)) AS role
        FROM jsonb_each_text(coalesce(p_idle_since_by_role, '{}'::jsonb))
        WHERE btrim(key) <> ''
    ),
    reset_rows AS (
        UPDATE ticket_board.ticket_notification_queue q
        SET claimed_at = NULL,
            next_attempt_at = p_now,
            last_error = NULL,
            updated_at = p_now
        FROM idle_roles i
        WHERE q.target_role = i.role
          AND q.next_attempt_at > p_now
          AND q.last_error = 'pane busy'
        RETURNING q.id
    )
    SELECT count(*)::integer
    INTO reset_count
    FROM reset_rows;

    RETURN coalesce(reset_count, 0);
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.reset_notification_backoff_for_idle_roles(jsonb, timestamptz) TO ticket_board_listener;
    END IF;
END;
$$;
