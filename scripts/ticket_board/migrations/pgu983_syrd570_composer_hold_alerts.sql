-- SYRD-570: tell the Director, once, when a composer holds a role's notices while
-- the runtime's own trusted hook says its turn is over.
--
-- On otto, Hermes redraw residue made two idle panes' composers look typed into,
-- and OTTO-71's and OTTO-79's notices were held as "pane busy" for hours with
-- nobody told. SYRD-550 stopped that residue reading as input; any other
-- ambiguous or genuinely stuck composer still holds silently.
--
-- The hold itself stays exactly as it is: nothing here submits into the composer,
-- clears it, sends a key, kills anything or calls a notice delivered. A person may
-- be writing there, and an alert is information, not permission to type over it.
-- What is new is a record of each composer-hold episode, per role:
--
--   * the listener notes every gate deferral (note_composer_hold). A composer
--     hold under a trusted idle hook opens the role's episode or joins it;
--   * once the episode is older than the listener's threshold, one ticket_update
--     tells the Director, at most once per episode;
--   * the episode closes when its last notice leaves the queue -- delivered,
--     superseded or dead-lettered -- or when a deferral shows the role is really
--     working (work_resumed). An alert not yet sent is withdrawn; one already
--     sent is followed by one line saying how the hold ended.
--
-- The episode lives here rather than in the listener, so a restarted listener
-- joins the hold it left instead of starting the clock again.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.composer_hold_episodes (
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
    clear_reason text CHECK (clear_reason IS NULL
                             OR clear_reason IN ('delivered', 'superseded', 'dead_lettered', 'work_resumed'))
);

-- One open episode per role: every notice a composer holds on that role's pane
-- belongs to it.
CREATE UNIQUE INDEX IF NOT EXISTS composer_hold_episodes_open
    ON ticket_board.composer_hold_episodes (target_role) WHERE cleared_at IS NULL;

-- Close an episode: withdraw an alert that has not gone yet, or follow one that
-- has with how the hold ended. p_ticket_id is a ticket the closing line may be
-- said on; NULL when there is none.
CREATE OR REPLACE FUNCTION ticket_board.close_composer_hold(p_episode_id bigint, p_how text, p_ticket_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.composer_hold_episodes%ROWTYPE;
    notice text;
BEGIN
    UPDATE ticket_board.composer_hold_episodes
    SET cleared_at = clock_timestamp(), clear_reason = p_how
    WHERE id = p_episode_id AND cleared_at IS NULL
    RETURNING * INTO ep;
    IF NOT FOUND OR ep.alert_notification_id IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id) THEN
        -- Not sent yet: the Director need never hear about a hold that is over.
        PERFORM ticket_board.record_notification_trace(
            a.ticket_id, a.id, a.target_role, a.kind, 'discard', NULL, 'composer_hold_cleared', NULL,
            jsonb_build_object('reason', 'composer_hold_cleared', 'episode', ep.id, 'clear_reason', p_how))
        FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id;
        DELETE FROM ticket_board.ticket_notification_queue WHERE id = ep.alert_notification_id;
        RETURN;
    END IF;
    IF p_ticket_id IS NULL OR NOT EXISTS (SELECT FROM ticket_board.tickets t WHERE t.id = p_ticket_id) THEN
        RETURN;  -- nothing to say it on
    END IF;
    notice := format('The composer hold on %s''s pane (%s) reported at %s has ended: %s.',
                     ep.target_role, array_to_string(ep.ticket_ids, ', '),
                     to_char(ep.alerted_at, 'YYYY-MM-DD HH24:MI:SS TZ'),
                     CASE p_how WHEN 'delivered' THEN 'its ticket notices were delivered'
                                WHEN 'superseded' THEN 'its ticket notices were no longer needed and were dropped'
                                WHEN 'dead_lettered' THEN 'its ticket notices were dead-lettered'
                                ELSE format('%s is working again, so its notices wait behind that work as usual',
                                            ep.target_role) END);
    PERFORM ticket_board.enqueue_notification(
        p_ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'composer_hold', 'id', p_ticket_id, 'target_role', 'director',
                           'episode', ep.id, 'role', ep.target_role, 'cleared', p_how, 'message', notice),
        'composer_hold_cleared:' || ep.id);
END;
$$;

-- A gate deferral. A composer hold under a trusted idle hook (p_composer) opens or
-- joins the role's episode, and tells the Director once it has lasted
-- p_alert_after; any other deferral means the role is working, which ends it.
CREATE OR REPLACE FUNCTION ticket_board.note_composer_hold(
    p_notification_id bigint, p_reason text, p_composer boolean, p_alert_after interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
    q ticket_board.ticket_notification_queue%ROWTYPE;
    ep ticket_board.composer_hold_episodes%ROWTYPE;
    held_ids bigint[];
    held_tickets text[];
    most_attempts integer;
    project text := coalesce(nullif(ticket_board.declared_workflow()->>'project', ''), '<project>');
    notice text;
    alert_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_composer_hold');
    SELECT * INTO q FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id AND dead_lettered_at IS NULL;
    -- The Director's own pane is not alerted about: the alert would wait there too.
    IF NOT FOUND OR q.target_role = 'director' THEN
        RETURN;
    END IF;
    IF NOT coalesce(p_composer, false) THEN
        SELECT * INTO ep FROM ticket_board.composer_hold_episodes
        WHERE target_role = q.target_role AND cleared_at IS NULL FOR UPDATE;
        IF FOUND THEN
            PERFORM ticket_board.close_composer_hold(ep.id, 'work_resumed', q.ticket_id);
        END IF;
        RETURN;
    END IF;
    INSERT INTO ticket_board.composer_hold_episodes AS e
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
    SELECT array_agg(h.id ORDER BY h.id), array_agg(DISTINCT h.ticket_id), max(h.attempts)
    INTO held_ids, held_tickets, most_attempts
    FROM ticket_board.ticket_notification_queue h
    WHERE h.id = ANY(ep.notification_ids) AND h.dead_lettered_at IS NULL;
    notice := format(
        '%s''s pane has held %s ticket notice%s (%s) for %s minutes, %s delivery attempt%s, because its composer '
        'looks in use (%s) while %s''s own hook says its turn is over. Nothing was typed into, cleared or sent. '
        'If a person is writing there, leave it: the notices go out once the line is sent or emptied. If it is '
        'leftover text nobody is writing, look with `switchyard attach %s %s` and empty the composer yourself; '
        'the next delivery pass sends them.',
        ep.target_role, cardinality(held_ids), CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
        array_to_string(held_tickets, ', '), floor(extract(epoch FROM now_at - ep.held_since) / 60)::bigint,
        most_attempts, CASE WHEN most_attempts = 1 THEN '' ELSE 's' END,
        ep.reason, ep.target_role, project, ep.target_role);
    alert_id := ticket_board.enqueue_notification(
        q.ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'composer_hold', 'id', q.ticket_id, 'target_role', 'director',
                           'episode', ep.id, 'role', ep.target_role, 'reason', ep.reason,
                           'held_since', ep.held_since, 'attempts', most_attempts,
                           'notification_ids', to_jsonb(held_ids), 'message', notice),
        'composer_hold:' || ep.id);
    UPDATE ticket_board.composer_hold_episodes
    SET alerted_at = now_at, alert_notification_id = alert_id
    WHERE id = ep.id;
END;
$$;

-- A held notice left the queue. When it was the episode's last, the hold is over.
CREATE OR REPLACE FUNCTION ticket_board.composer_hold_notice_left()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.composer_hold_episodes%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.dead_lettered_at IS NOT NULL OR NEW.dead_lettered_at IS NULL) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO ep FROM ticket_board.composer_hold_episodes
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
    PERFORM ticket_board.close_composer_hold(ep.id, CASE
        WHEN TG_OP = 'UPDATE' THEN 'dead_lettered'
        WHEN EXISTS (SELECT FROM ticket_board.notification_trace t
                     WHERE t.notification_id = OLD.id AND t.event IN ('send', 'send_unconfirmed')) THEN 'delivered'
        ELSE 'superseded'
    END, OLD.ticket_id);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS composer_hold_notice_left ON ticket_board.ticket_notification_queue;
CREATE TRIGGER composer_hold_notice_left
    AFTER DELETE OR UPDATE OF dead_lettered_at ON ticket_board.ticket_notification_queue
    FOR EACH ROW EXECUTE FUNCTION ticket_board.composer_hold_notice_left();

REVOKE ALL ON ticket_board.composer_hold_episodes FROM PUBLIC;
GRANT SELECT ON ticket_board.composer_hold_episodes TO ticket_board_service;
REVOKE EXECUTE ON FUNCTION ticket_board.close_composer_hold(bigint, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_composer_hold(bigint, text, boolean, interval) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.composer_hold_notice_left() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_composer_hold(bigint, text, boolean, interval) TO ticket_board_listener;

COMMIT;
