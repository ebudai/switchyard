-- SYRD-881 (pgu1061): a hand-off left undelivered when its ticket leaves a stage, or changes owner, is never
-- delivered for a later entry. A trigger retires it (traced as dropped, left_its_entry) when the ticket moves.
--
-- schema.sql carries the same.

BEGIN;

-- A hand-off belongs to the entry it was queued for. When a ticket leaves a stage, or changes owner within one, the
-- hand-offs still queued for that stage and owner are retired: never delivered, traced as dropped. Without this, a
-- notice deferred because its reviewer was busy stayed queued, and when the ticket came back to the same owner in the
-- same stage it was current again, beside the new entry's own hand-off: two hand-offs for one entry (SYRD-881).
--
-- Only undelivered `transition` notices (`dead_lettered_at` unset) whose payload names the stage and owner the ticket
-- just left. The new entry's hand-off, queued by the same update, names the new stage or owner, so it is never one of
-- them. A notice the listener is delivering holds the ticket row (hold_for_delivery) until it is sent, so this waits
-- for that send rather than racing it. A notice already typed into its pane, whose delivery proof is still open
-- (SYRD-565: unsubmitted, or awaiting its receipt), is left to that proof: it settles the text left in the composer and
-- tells the Director to clear it, which a quiet delete here would skip.
CREATE OR REPLACE FUNCTION ticket_board.retire_left_handoffs()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id, q.id, q.target_role, q.kind, 'drop', NULL, 'left_its_entry', NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload, 'reason', 'left_its_entry',
                           'left', jsonb_build_object('state', OLD.state, 'assignee', OLD.assignee),
                           'now', jsonb_build_object('state', NEW.state, 'assignee', NEW.assignee)))
      FROM ticket_board.ticket_notification_queue q
     WHERE q.ticket_id = NEW.id AND q.kind = 'transition' AND q.dead_lettered_at IS NULL
       AND q.payload->>'new_state' = OLD.state AND q.payload->>'assignee' = OLD.assignee
       AND NOT EXISTS (SELECT FROM ticket_board.notification_proofs p
                        WHERE p.notification_id = q.id AND p.state IN ('unsubmitted', 'receipt_pending'));
    DELETE FROM ticket_board.ticket_notification_queue q
     WHERE q.ticket_id = NEW.id AND q.kind = 'transition' AND q.dead_lettered_at IS NULL
       AND q.payload->>'new_state' = OLD.state AND q.payload->>'assignee' = OLD.assignee
       AND NOT EXISTS (SELECT FROM ticket_board.notification_proofs p
                        WHERE p.notification_id = q.id AND p.state IN ('unsubmitted', 'receipt_pending'));
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.retire_left_handoffs() FROM PUBLIC;
DROP TRIGGER IF EXISTS tickets_z_retire_left_handoffs ON ticket_board.tickets;
CREATE TRIGGER tickets_z_retire_left_handoffs AFTER UPDATE OF state, assignee ON ticket_board.tickets
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee)
    EXECUTE FUNCTION ticket_board.retire_left_handoffs();

COMMIT;
