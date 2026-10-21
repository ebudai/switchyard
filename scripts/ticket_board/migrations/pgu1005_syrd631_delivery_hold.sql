-- SYRD-631: a notice's last eligibility read and its send hold the rows that decide it.
-- schema.sql carries the same function; rbac.sql the same grant.
BEGIN;

-- SYRD-631: a notice's last eligibility read and its send are one step.
--
-- The notify listener asks whether a notice is still current immediately
-- before it sends it, but a board write could commit between that read and the
-- send: a reassignment, a terminal move, a blocker, a new serial order. Audit
-- reproduced it -- PGU-680 reassigned from App to Main after the read, and App
-- was still handed it. So the listener holds the notice's ticket row, FOR
-- SHARE, for the transaction that makes the read and the send. Every write that
-- could retire the notice locks that row first -- FOR UPDATE, or by updating
-- it; a blocker is set through the ticket, and a role's serial slot moves only
-- when its holder's own row changes -- so it waits for the send. One already
-- holding it makes this fail at once (NOWAIT), and the notice waits for the
-- next pass. The listener never waits on the row.
CREATE OR REPLACE FUNCTION ticket_board.hold_for_delivery(p_ticket_id text, p_target_role text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = p_ticket_id FOR SHARE NOWAIT;
    RETURN true;
EXCEPTION WHEN lock_not_available THEN
    RETURN false;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.hold_for_delivery(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.hold_for_delivery(text, text) TO ticket_board_listener;

COMMIT;
