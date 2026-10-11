-- SYRD-583: tell the Director, once per process, when a handoff goes out past
-- background work a finished turn left running.
--
-- On syrd a shell from completed SYRD-579 ran for 18 h under App's provider. It
-- was a wait loop whose `pgrep -f` matched its own command line, so it could
-- never end, and SYRD-581's handoff waited behind it as "pane busy" until the
-- Director found it and stopped that one process group by hand.
--
-- The listener now lets a handoff past such a child once the child is plainly
-- left over: its ticket is no longer that role's, or a finished turn's wait has
-- reached SYRD-212's bound. Nothing is killed. The child is still running, so the
-- Director is told -- once for that process, however many later handoffs pass
-- it -- what it is and how to stop only it.
--
-- One row per child: a process group and its start time, so a reused number is
-- a different child. It is not withdrawn when the handoff is delivered, unlike
-- SYRD-557's hold alert: that hold ended, but this child did not.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.background_child_alerts (
    target_role text NOT NULL,
    child text NOT NULL,
    ticket_id text NOT NULL,
    notification_id bigint,
    alert_notification_id bigint,
    alerted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (target_role, child)
);

CREATE OR REPLACE FUNCTION ticket_board.note_background_child(
    p_notification_id bigint, p_child text, p_what text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    q ticket_board.ticket_notification_queue%ROWTYPE;
    added integer;
    notice text;
    alert_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_background_child');
    IF btrim(coalesce(p_child, '')) = '' OR btrim(coalesce(p_what, '')) = '' THEN
        RETURN false;
    END IF;
    SELECT * INTO q FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id AND dead_lettered_at IS NULL;
    -- The Director's own pane is not alerted about, as with SYRD-557's holds.
    IF NOT FOUND OR q.target_role = 'director' THEN
        RETURN false;
    END IF;
    INSERT INTO ticket_board.background_child_alerts (target_role, child, ticket_id, notification_id)
    VALUES (q.target_role, p_child, q.ticket_id, q.id)
    ON CONFLICT (target_role, child) DO NOTHING;
    GET DIAGNOSTICS added = ROW_COUNT;
    IF added = 0 THEN
        RETURN false;
    END IF;
    notice := format('%s''s %s notice went out past background work a finished turn left running in its pane: %s',
        q.target_role, q.ticket_id, p_what);
    alert_id := ticket_board.enqueue_notification(
        q.ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'background_child', 'id', q.ticket_id, 'target_role', 'director',
                           'role', q.target_role, 'child', p_child, 'notification_id', q.id, 'message', notice),
        'background_child:' || q.target_role || ':' || p_child);
    UPDATE ticket_board.background_child_alerts SET alert_notification_id = alert_id
    WHERE target_role = q.target_role AND child = p_child;
    RETURN true;
END;
$$;

REVOKE ALL ON ticket_board.background_child_alerts FROM PUBLIC;
GRANT SELECT ON ticket_board.background_child_alerts TO ticket_board_service;
REVOKE EXECUTE ON FUNCTION ticket_board.note_background_child(bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_background_child(bigint, text, text) TO ticket_board_listener;

COMMIT;
