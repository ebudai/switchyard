-- SYRD-557: tell the Director, once, when background work holds a role's ticket
-- notices too long.
--
-- On otto a wait loop whose `pgrep -f` matched itself ran for 2 h 44 min.
-- SYRD-538 rightly read the pane as busy with background work and held two
-- ticket notices behind it -- and nothing told the Director that ready work had
-- sat undelivered for hours.
--
-- The hold itself stays exactly as it is: nothing here delivers through it,
-- interrupts it or kills anything. What is new is a record of each hold
-- episode, per role:
--
--   * the listener notes every requeue that background work caused
--     (note_background_hold), which opens the role's episode or joins it;
--   * once the episode is older than the listener's threshold, one ticket_update
--     tells the Director, at most once per episode;
--   * when the last of its notices leaves the queue -- delivered, superseded or
--     dead-lettered -- a trigger closes the episode. An alert the Director has
--     not been sent yet is withdrawn; one already sent is followed by one line
--     saying the hold ended, so the last word on it is true.
--
-- The episode lives here rather than in the listener, so a restarted listener
-- joins the hold it left instead of starting the clock again.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.background_hold_episodes (
    id bigserial PRIMARY KEY,
    target_role text NOT NULL,
    reason text NOT NULL,
    held_since timestamptz NOT NULL,
    last_held_at timestamptz NOT NULL,
    notification_ids bigint[] NOT NULL DEFAULT '{}',
    ticket_ids text[] NOT NULL DEFAULT '{}',
    alerted_at timestamptz,
    alert_notification_id bigint,
    cleared_at timestamptz,
    clear_reason text CHECK (clear_reason IS NULL OR clear_reason IN ('delivered', 'superseded', 'dead_lettered'))
);

-- One open episode per role: every notice that background work holds on that
-- role's pane belongs to it.
CREATE UNIQUE INDEX IF NOT EXISTS background_hold_episodes_open
    ON ticket_board.background_hold_episodes (target_role) WHERE cleared_at IS NULL;

-- A requeue caused by background work: open or join the role's episode, and
-- tell the Director once it has lasted p_alert_after.
CREATE OR REPLACE FUNCTION ticket_board.note_background_hold(
    p_notification_id bigint, p_reason text, p_alert_after interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
    q ticket_board.ticket_notification_queue%ROWTYPE;
    ep ticket_board.background_hold_episodes%ROWTYPE;
    held_ids bigint[];
    held_tickets text[];
    notice text;
    alert_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_background_hold');
    SELECT * INTO q FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id AND dead_lettered_at IS NULL;
    -- The Director's own pane is not alerted about: the alert would wait there too.
    IF NOT FOUND OR q.target_role = 'director' THEN
        RETURN;
    END IF;
    INSERT INTO ticket_board.background_hold_episodes AS e
        (target_role, reason, held_since, last_held_at, notification_ids, ticket_ids)
    VALUES (q.target_role, p_reason, now_at, now_at, ARRAY[q.id], ARRAY[q.ticket_id])
    ON CONFLICT (target_role) WHERE cleared_at IS NULL DO UPDATE
    SET last_held_at = now_at,
        reason = EXCLUDED.reason,
        notification_ids = CASE WHEN q.id = ANY(e.notification_ids) THEN e.notification_ids
                                ELSE e.notification_ids || q.id END,
        ticket_ids = CASE WHEN q.ticket_id = ANY(e.ticket_ids) THEN e.ticket_ids
                          ELSE e.ticket_ids || q.ticket_id END
    RETURNING * INTO ep;
    IF ep.alerted_at IS NOT NULL OR now_at - ep.held_since < p_alert_after THEN
        RETURN;
    END IF;
    SELECT array_agg(h.id ORDER BY h.id), array_agg(DISTINCT h.ticket_id)
    INTO held_ids, held_tickets
    FROM ticket_board.ticket_notification_queue h
    WHERE h.id = ANY(ep.notification_ids) AND h.dead_lettered_at IS NULL;
    notice := format(
        '%s''s pane has held %s ticket notice%s (%s) for %s minutes behind background work it is still running (%s). '
        'Nothing was interrupted and nothing will be; they go out when the work ends. A job that never ends -- '
        'a wait loop whose pgrep matches itself, say -- holds them for good: check %s''s pane.',
        ep.target_role, cardinality(held_ids), CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
        array_to_string(held_tickets, ', '), floor(extract(epoch FROM now_at - ep.held_since) / 60)::bigint,
        ep.reason, ep.target_role);
    alert_id := ticket_board.enqueue_notification(
        q.ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'background_hold', 'id', q.ticket_id, 'target_role', 'director',
                           'episode', ep.id, 'role', ep.target_role, 'reason', ep.reason,
                           'held_since', ep.held_since, 'notification_ids', to_jsonb(held_ids),
                           'message', notice),
        'background_hold:' || ep.id);
    UPDATE ticket_board.background_hold_episodes
    SET alerted_at = now_at, alert_notification_id = alert_id
    WHERE id = ep.id;
END;
$$;

-- A held notice left the queue. When it was the episode's last, the hold is
-- over: close it, withdraw an alert still waiting to go, or follow one that went
-- with the news that it ended.
CREATE OR REPLACE FUNCTION ticket_board.background_hold_notice_left()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.background_hold_episodes%ROWTYPE;
    how text;
    notice text;
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.dead_lettered_at IS NOT NULL OR NEW.dead_lettered_at IS NULL) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO ep FROM ticket_board.background_hold_episodes
    WHERE cleared_at IS NULL AND OLD.id = ANY(notification_ids)
    FOR UPDATE;
    IF NOT FOUND OR EXISTS (
        SELECT FROM ticket_board.ticket_notification_queue h
        WHERE h.id = ANY(ep.notification_ids) AND h.id <> OLD.id AND h.dead_lettered_at IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    -- Delivered means sent. A notice dropped as stale is acknowledged too, so
    -- the acknowledgement alone cannot say which it was; the send trace can.
    how := CASE
        WHEN TG_OP = 'UPDATE' THEN 'dead_lettered'
        WHEN EXISTS (SELECT FROM ticket_board.notification_trace t
                     WHERE t.notification_id = OLD.id AND t.event IN ('send', 'send_unconfirmed')) THEN 'delivered'
        ELSE 'superseded'
    END;
    UPDATE ticket_board.background_hold_episodes
    SET cleared_at = clock_timestamp(), clear_reason = how
    WHERE id = ep.id;
    IF ep.alert_notification_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id) THEN
        -- Not sent yet: the Director need never hear about a hold that is over.
        PERFORM ticket_board.record_notification_trace(
            a.ticket_id, a.id, a.target_role, a.kind, 'discard', NULL, 'background_hold_cleared', NULL,
            jsonb_build_object('reason', 'background_hold_cleared', 'episode', ep.id, 'clear_reason', how))
        FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id;
        DELETE FROM ticket_board.ticket_notification_queue WHERE id = ep.alert_notification_id;
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.tickets t WHERE t.id = OLD.ticket_id) THEN
        RETURN NULL;  -- the ticket itself is being deleted: nothing to say it on
    END IF;
    notice := format('The hold on %s''s pane (%s) reported at %s has ended: its ticket notices were %s.',
                     ep.target_role, array_to_string(ep.ticket_ids, ', '),
                     to_char(ep.alerted_at, 'YYYY-MM-DD HH24:MI:SS TZ'),
                     CASE how WHEN 'delivered' THEN 'delivered'
                              WHEN 'superseded' THEN 'no longer needed and were dropped'
                              ELSE 'dead-lettered' END);
    PERFORM ticket_board.enqueue_notification(
        OLD.ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'background_hold', 'id', OLD.ticket_id, 'target_role', 'director',
                           'episode', ep.id, 'role', ep.target_role, 'cleared', how, 'message', notice),
        'background_hold_cleared:' || ep.id);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS background_hold_notice_left ON ticket_board.ticket_notification_queue;
CREATE TRIGGER background_hold_notice_left
    AFTER DELETE OR UPDATE OF dead_lettered_at ON ticket_board.ticket_notification_queue
    FOR EACH ROW EXECUTE FUNCTION ticket_board.background_hold_notice_left();

REVOKE ALL ON ticket_board.background_hold_episodes FROM PUBLIC;
GRANT SELECT ON ticket_board.background_hold_episodes TO ticket_board_service;
REVOKE EXECUTE ON FUNCTION ticket_board.note_background_hold(bigint, text, interval) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.background_hold_notice_left() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_background_hold(bigint, text, interval) TO ticket_board_listener;

COMMIT;
