-- PGU-909: a comment by the awaited role must not clear awaiting_role.
--
-- The clearing lived in the ticket_comments trigger, not in
-- clear_awaiting_role_from_ticket_activity's `actor = awaiting_role` clause,
-- which does not fire on comments. See schema.sql for the reasoning.

CREATE OR REPLACE FUNCTION ticket_board.touch_ticket_notification_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.ts, clock_timestamp());
BEGIN
    UPDATE ticket_board.ticket_notification_state
    SET last_activity_at = activity_at,
        nudge_count = 0
    WHERE ticket_id = NEW.ticket_id;
    RETURN NULL;
END;
$$;
