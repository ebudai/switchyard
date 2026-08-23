CREATE OR REPLACE FUNCTION ticket_board.requeue_notification(
    p_notification_id bigint,
    p_delay interval DEFAULT interval '30 seconds',
    p_error text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('requeue_notification');
    IF coalesce(p_error, '') <> 'pane busy' THEN
        PERFORM ticket_board.record_notification_trace(
            q.ticket_id,
            q.id,
            q.target_role,
            q.kind,
            'requeue',
            NULL,
            p_error,
            NULL,
            jsonb_build_object('delay', p_delay, 'attempts', q.attempts, 'payload', q.payload)
        )
        FROM ticket_board.ticket_notification_queue q
        WHERE q.id = p_notification_id;
    END IF;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'retry_loud',
        NULL,
        p_error,
        NULL,
        jsonb_build_object(
            'attempts', q.attempts,
            'age_seconds', floor(extract(epoch FROM now_at - q.created_at))::integer,
            'payload', q.payload
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
      AND (q.attempts >= 12 OR q.created_at <= now_at - interval '1 hour')
      AND NOT EXISTS (
          SELECT 1
          FROM ticket_board.notification_trace nt
          WHERE nt.notification_id = q.id
            AND nt.event = 'retry_loud'
      );

    UPDATE ticket_board.ticket_notification_queue
    SET claimed_at = NULL,
        next_attempt_at = now_at + p_delay,
        last_error = p_error,
        updated_at = now_at
    WHERE id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_finish_current_notifications_for_released_ticket()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reset_at timestamptz := clock_timestamp();
    reset_count integer;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RETURN NULL;
    END IF;
    IF OLD.state <> 'in_progress'
       OR NOT ticket_board.ticket_is_implementer_assignee(OLD.assignee)
       OR coalesce(OLD.manually_controlled, false) THEN
        RETURN NULL;
    END IF;
    IF NEW.state = 'in_progress'
       AND NEW.assignee IS NOT DISTINCT FROM OLD.assignee
       AND NOT coalesce(NEW.manually_controlled, false) THEN
        RETURN NULL;
    END IF;

    UPDATE ticket_board.ticket_notification_queue q
    SET claimed_at = NULL,
        next_attempt_at = reset_at,
        last_error = NULL,
        updated_at = reset_at
    WHERE q.target_role = OLD.assignee
      AND q.next_attempt_at > reset_at
      -- Must match FINISH_CURRENT_REQUEUE_ERROR in notify_listener.py.
      AND q.last_error = 'finish current';
    GET DIAGNOSTICS reset_count = ROW_COUNT;

    IF reset_count > 0 THEN
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object(
                'kind', 'wake',
                'reason', 'finish_current_released',
                'ticket_id', OLD.id,
                'target_role', OLD.assignee,
                'reset_count', reset_count
            )::text
        );
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzzx_reset_finish_current_notifications ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzx_reset_finish_current_notifications
AFTER UPDATE OF state, assignee, manually_controlled ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.reset_finish_current_notifications_for_released_ticket();
