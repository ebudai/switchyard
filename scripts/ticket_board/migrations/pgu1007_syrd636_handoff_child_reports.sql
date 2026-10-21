-- SYRD-636: at a hand-over, tell the Director, once per child, about background
-- work earlier tickets left running in the role's pane.
--
-- On syrd two QEMU VMs App had started for earlier tickets ran on under App's
-- provider through the next hand-over and overnight, 2 x 8 vCPU and 2 x 16 GiB.
-- They held no notice, so SYRD-583's report -- made only when a delivery goes out
-- past a child -- never fired, and the Director found them in the pane's footer.
-- The listener now looks under the role's pane when a hand-over is delivered and
-- reports what earlier work left there. Nothing is stopped.
--
-- The record is SYRD-583's: one row per role and child (a process group and its
-- start time), so a child named by either report is never named again, and the
-- same dedupe key.
--
-- schema.sql carries the same function.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.note_handoff_child(
    p_role text, p_ticket_id text, p_child text, p_what text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    added integer;
    notice text;
    alert_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_handoff_child');
    IF btrim(coalesce(p_role, '')) = '' OR btrim(coalesce(p_child, '')) = ''
            OR btrim(coalesce(p_what, '')) = ''
            OR NOT EXISTS (SELECT 1 FROM ticket_board.tickets t WHERE t.id = p_ticket_id) THEN
        RETURN false;
    END IF;
    INSERT INTO ticket_board.background_child_alerts (target_role, child, ticket_id)
    VALUES (p_role, p_child, p_ticket_id)
    ON CONFLICT (target_role, child) DO NOTHING;
    GET DIAGNOSTICS added = ROW_COUNT;
    IF added = 0 THEN
        RETURN false;
    END IF;
    notice := format('%s was handed %s with background work still running in its pane from earlier work: %s',
        p_role, p_ticket_id, p_what);
    alert_id := ticket_board.enqueue_notification(
        p_ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'background_child', 'id', p_ticket_id, 'target_role', 'director',
                           'role', p_role, 'child', p_child, 'at', 'handoff', 'message', notice),
        'background_child:' || p_role || ':' || p_child);
    UPDATE ticket_board.background_child_alerts SET alert_notification_id = alert_id
    WHERE target_role = p_role AND child = p_child;
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.note_handoff_child(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_handoff_child(text, text, text, text) TO ticket_board_listener;

COMMIT;
