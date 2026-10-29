-- SYRD-697: on pie, Ops asked Audit for a review on PIE-17 and went idle. Audit reviewed and cleared the wait, and
-- nothing reached Ops' pane for an hour:
--
-- * Clearing a wait told nobody. A role's comment tells the owner only when it is urgent, so the review did not
--   either. The ticket was its owner's work again, and its owner did not know.
-- * The wait's notices still queued (5 and 15 minutes to the awaited role, 30 to the Director) were dropped by the
--   listener once the wait was gone, and a dropped notice is removed with ack_notification. The board stall alert
--   counted each such ack as "awaiting_role notice delivered to director", durable progress, which restarted the
--   ticket's stall clock with nothing delivered.
--
-- Now a wait cleared by anyone but the ticket's owner tells the owner, in the in-place change notice a blocker change
-- gives, unless it is a held review's completion wait, which the owner's approval set; and only a notice the listener
-- sent counts as delivered. schema.sql carries the same functions.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    cleared text;
    signed_off boolean;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'clear_awaiting_role'
    );
    SELECT t.manually_controlled AND (ticket_board.pending_held_review(t.id) IS NOT NULL
                                      OR CASE t.state WHEN 'inspection' THEN t.inspector_signoff
                                                      WHEN 'audit' THEN t.audit_signoff
                                                      WHEN 'user_review' THEN t.user_signoff
                                                      ELSE false END)
    INTO signed_off
    FROM ticket_board.tickets t WHERE t.id = clear_awaiting_role.id FOR UPDATE;
    SELECT ns.awaiting_role INTO cleared
    FROM ticket_board.ticket_notification_state ns
    WHERE ns.ticket_id = clear_awaiting_role.id;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL,
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    WHERE ticket_id = clear_awaiting_role.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    -- SYRD-697: the ticket is its owner's work again, and its owner may be sitting idle at its prompt waiting for
    -- exactly this. Told as for any change to the ticket's blockers; an owner clearing its own wait already knows,
    -- and clearing a ticket with no wait changes nothing. Nor is a held review's completion wait (SYRD-22, SYRD-180)
    -- its owner's: the owner has approved, the approval is deferred until the hold is released, and the wait is on
    -- whoever takes the ticket next.
    IF coalesce(cleared, '') <> '' AND NOT coalesce(signed_off, false) THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, format('the wait on %s was cleared', cleared));
    END IF;
END;
$$;

-- A hand-over delivered: a transition, a dependency or a publication notice the listener sent and acknowledged. A
-- notice it dropped -- stale, for an unknown role, or held for its provider's quota -- is acked without a send, and
-- is not a delivery (SYRD-697). A nudge, reminder or alert delivered is not one either.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_delivery_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF NEW.event = 'ack' AND NEW.kind IN ('transition', 'awaiting_role', 'publication') AND NEW.ticket_id IS NOT NULL
       AND EXISTS (SELECT FROM ticket_board.notification_trace sent
                   WHERE sent.notification_id = NEW.notification_id
                     AND sent.event IN ('send', 'send_unconfirmed')) THEN
        PERFORM ticket_board.board_stall_note_progress(NEW.ticket_id,
            (SELECT state FROM ticket_board.tickets WHERE id = NEW.ticket_id),
            format('%s %s notice delivered to %s', NEW.ticket_id, NEW.kind, coalesce(NEW.target_role, 'its owner')));
    END IF;
    RETURN NULL;
END;
$$;

COMMIT;
