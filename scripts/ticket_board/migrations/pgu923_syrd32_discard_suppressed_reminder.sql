-- SYRD-32: suppressing a reminder must not count as delivering it.
-- ack_notification increments idle_reminder_count for an idle_reminder, which the
-- idle generator reads as "already reminded" and escalates to the director on the
-- next wave. A reminder held back because its owner is working was never delivered,
-- so the listener needs a removal path that leaves no delivery accounting behind.
CREATE OR REPLACE FUNCTION ticket_board.discard_notification(
    p_notification_id bigint,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('discard_notification');
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'discard_notification requires a non-empty reason';
    END IF;

    -- Removes a queued notification that was never delivered, WITHOUT the
    -- delivery accounting in ack_notification. That function bumps
    -- ticket_notification_state.idle_reminder_count for an idle_reminder, and
    -- notify_idle_turn_end_nudges reads idle_reminder_count >= 1 on a
    -- non-director owner as "already reminded", switching the next wave to an
    -- escalation addressed to the director. Acking a reminder that was
    -- suppressed because its owner is working would therefore tell the director
    -- the owner was reminded and ignored it, when nothing was ever delivered
    -- (SYRD-32).
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'discard',
        NULL,
        left(p_reason, 500),
        NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload, 'reason', p_reason)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.discard_notification(bigint, text) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.discard_notification(bigint, text) TO ticket_board_listener;
    END IF;
END
$$;
