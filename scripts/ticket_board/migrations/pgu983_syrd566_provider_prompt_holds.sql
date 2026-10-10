-- SYRD-566: a hold can say what it is.
--
-- A role's pane stopped at its provider CLI's own question -- Codex's
-- model-retirement notice, a folder-trust or sign-in screen, the `/new` menu --
-- is held by the activity gate like any busy pane, and SYRD-557's hold episodes
-- already tell the Director once when a hold lasts. But their alert says
-- "behind background work it is still running", which for a question nobody
-- will ever answer by waiting is the wrong instruction. note_background_hold
-- now takes what the hold is, and the alert says that instead.
--
-- The three-argument function is replaced, not overloaded: with a defaulted
-- fourth argument both would match a three-argument call. A listener that
-- passes three arguments still gets SYRD-557's wording.
--
-- schema.sql carries the same function.

BEGIN;

DROP FUNCTION IF EXISTS ticket_board.note_background_hold(bigint, text, interval);

CREATE OR REPLACE FUNCTION ticket_board.note_background_hold(
    p_notification_id bigint, p_reason text, p_alert_after interval, p_what text DEFAULT NULL)
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
    IF p_what IS NULL THEN
        notice := format(
            '%s''s pane has held %s ticket notice%s (%s) for %s minutes behind background work it is still running (%s). '
            'Nothing was interrupted and nothing will be; they go out when the work ends. A job that never ends -- '
            'a wait loop whose pgrep matches itself, say -- holds them for good: check %s''s pane.',
            ep.target_role, cardinality(held_ids), CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
            array_to_string(held_tickets, ', '), floor(extract(epoch FROM now_at - ep.held_since) / 60)::bigint,
            ep.reason, ep.target_role);
    ELSE
        -- The hold says what it is (SYRD-566): a provider's own question, say.
        notice := format('%s''s pane has held %s ticket notice%s (%s) for %s minutes: %s',
            ep.target_role, cardinality(held_ids), CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
            array_to_string(held_tickets, ', '), floor(extract(epoch FROM now_at - ep.held_since) / 60)::bigint,
            p_what);
    END IF;
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

REVOKE EXECUTE ON FUNCTION ticket_board.note_background_hold(bigint, text, interval, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_background_hold(bigint, text, interval, text) TO ticket_board_listener;

COMMIT;
