-- SYRD-868 (pgu1057): the reason a ticket closed with no code, as its no-code move recorded it.
--
-- A report closed upstream with no commit -- a no-code close (only a configuration change), or a won't-fix closed
-- as done -- is announced to its reporter's Director, because no release will tell it anything, and the notice must
-- carry the reason (scripts/ticket_board/report_closure.py). A declared board writes a no-code move's reason as a
-- plain comment, which nothing else tells apart from any other, so this keeps it when the move is made: an AFTER
-- UPDATE trigger, on a move whose declared transition is allow_no_code, records the ticket's newest comment. It
-- writes only its own table. An undeclared board records nothing, and the notice falls back to the Director's comment.
--
-- schema.sql carries the same objects; rbac.sql grants the read to the board service.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_no_code_reasons (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    reason text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON ticket_board.ticket_no_code_reasons FROM PUBLIC;
GRANT SELECT ON ticket_board.ticket_no_code_reasons TO ticket_board_service;

CREATE OR REPLACE FUNCTION ticket_board.record_no_code_reason()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- A declared allow_no_code move (the no-code submission): keep the reason it wrote, the ticket's newest comment,
-- which perform_workflow_action_as appends before the update that moves and exempts the ticket. Nothing else that
-- exempts a ticket (a Director edit, a merge) is a no-code reason, and neither is recorded.
DECLARE said text;
BEGIN
    IF NOT EXISTS (
        SELECT FROM jsonb_array_elements(coalesce(ticket_board.declared_workflow()->'transitions', '[]'::jsonb)) x
         WHERE x->>'from' = OLD.state  -- its 'to' may be gate-skipped past
           AND x->>'action' = current_setting('ticket_board.workflow_action', true)
           AND coalesce((x->>'allow_no_code')::boolean, false)) THEN
        RETURN NULL;
    END IF;
    SELECT c.text INTO said FROM ticket_board.ticket_comments c WHERE c.ticket_id = NEW.id
     ORDER BY c.position DESC LIMIT 1;
    IF btrim(coalesce(said, '')) <> '' THEN
        INSERT INTO ticket_board.ticket_no_code_reasons (ticket_id, reason) VALUES (NEW.id, said);
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.record_no_code_reason() FROM PUBLIC;
DROP TRIGGER IF EXISTS tickets_z_no_code_reason ON ticket_board.tickets;
CREATE TRIGGER tickets_z_no_code_reason AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW WHEN (NEW.commit_exempt AND NEW.state IS DISTINCT FROM OLD.state)
    EXECUTE FUNCTION ticket_board.record_no_code_reason();

COMMIT;
