-- SYRD-633: a pane whose hook says `blocked` is one episode, and the Director
-- hears of it once, whatever wrote it.
--
-- SYRD-234's notify_permission_prompt_waits escalates only the two sources it
-- knows, on the role's lowest ticket id, under one dedupe key per role and
-- blocked instant. On syrd, 2026-10-20, that ticket was SYRD-624, blocked on
-- another ticket, so the escalation was dropped as stale before the Director
-- saw it -- and the key then kept every later pass silent while SYRD-629's
-- activation waited behind the pane 111 times.
--
-- The listener now reports every pane it read on each pass. An episode is one
-- role's blocked instant; it is told once, after p_alert_after, on the held
-- notice's own ticket when one waits and otherwise on the role's active
-- ticket, as a `blocked_pane` notice, which only the Director is told and no
-- ticket's blockers can drop. It closes when the hook says anything else, or
-- blocked from a later instant; an alert not sent yet is withdrawn.
--
-- schema.sql carries the same table and functions.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.blocked_pane_episodes (
    id bigserial PRIMARY KEY,
    target_role text NOT NULL,
    blocked_since timestamptz NOT NULL,
    source text NOT NULL,
    classification text NOT NULL,
    opened_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    alerted_at timestamptz,
    alert_notification_id bigint,
    cleared_at timestamptz,
    clear_reason text CHECK (clear_reason IS NULL OR clear_reason IN ('cleared', 'replaced'))
);

CREATE UNIQUE INDEX IF NOT EXISTS blocked_pane_episodes_identity
    ON ticket_board.blocked_pane_episodes (target_role, blocked_since);
CREATE UNIQUE INDEX IF NOT EXISTS blocked_pane_episodes_open
    ON ticket_board.blocked_pane_episodes (target_role) WHERE cleared_at IS NULL;

-- Close an episode. An alert the Director has not been sent yet is withdrawn,
-- and the episode forgets it, so the same blocked instant seen again is told.
CREATE OR REPLACE FUNCTION ticket_board.close_blocked_pane(p_episode_id bigint, p_how text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.blocked_pane_episodes%ROWTYPE;
BEGIN
    UPDATE ticket_board.blocked_pane_episodes
    SET cleared_at = clock_timestamp(), clear_reason = p_how
    WHERE id = p_episode_id AND cleared_at IS NULL
    RETURNING * INTO ep;
    IF NOT FOUND OR ep.alert_notification_id IS NULL
       OR NOT EXISTS (SELECT FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id) THEN
        RETURN;
    END IF;
    PERFORM ticket_board.record_notification_trace(
        a.ticket_id, a.id, a.target_role, a.kind, 'discard', NULL, 'blocked_pane_cleared', NULL,
        jsonb_build_object('reason', 'blocked_pane_cleared', 'episode', ep.id, 'clear_reason', p_how))
    FROM ticket_board.ticket_notification_queue a WHERE a.id = ep.alert_notification_id;
    DELETE FROM ticket_board.ticket_notification_queue WHERE id = ep.alert_notification_id;
    UPDATE ticket_board.blocked_pane_episodes SET alerted_at = NULL, alert_notification_id = NULL WHERE id = ep.id;
END;
$$;

-- One pass's panes: {"observed": [role, ...], "blocked": {role: {since, source,
-- classification, target, what, ...}}}. A role observed and not blocked closes
-- its episode; a role not observed is left as it was. Returns how many alerts
-- this pass enqueued.
CREATE OR REPLACE FUNCTION ticket_board.note_blocked_panes(p_panes jsonb, p_alert_after interval)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
    role_name text;
    pane jsonb;
    since timestamptz;
    ep ticket_board.blocked_pane_episodes%ROWTYPE;
    stale_id bigint;
    held_ids bigint[];
    held_tickets text[];
    on_ticket text;
    notice text;
    alert_id bigint;
    alerted integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_blocked_panes');
    FOR role_name IN
        SELECT DISTINCT value FROM jsonb_array_elements_text(coalesce(p_panes->'observed', '[]'::jsonb)) ORDER BY value
    LOOP
        -- The Director's own pane is not alerted about: the alert would wait there too.
        CONTINUE WHEN role_name = 'director';
        pane := p_panes->'blocked'->role_name;
        since := NULL;
        IF jsonb_typeof(pane) = 'object' THEN
            BEGIN
                since := (pane->>'since')::timestamptz;
            EXCEPTION WHEN others THEN
                since := NULL;  -- an instant nobody can read describes no episode
            END;
        END IF;
        FOR stale_id IN
            SELECT e.id FROM ticket_board.blocked_pane_episodes e
            WHERE e.target_role = role_name AND e.cleared_at IS NULL AND (since IS NULL OR e.blocked_since <> since)
        LOOP
            PERFORM ticket_board.close_blocked_pane(stale_id, CASE WHEN since IS NULL THEN 'cleared' ELSE 'replaced' END);
        END LOOP;
        CONTINUE WHEN since IS NULL;
        INSERT INTO ticket_board.blocked_pane_episodes AS e
            (target_role, blocked_since, source, classification, opened_at, last_seen_at, evidence)
        VALUES (role_name, since, coalesce(pane->>'source', ''), coalesce(nullif(pane->>'classification', ''), 'unknown_source'),
                now_at, now_at, pane)
        ON CONFLICT (target_role, blocked_since) DO UPDATE
        SET last_seen_at = now_at, source = EXCLUDED.source, classification = EXCLUDED.classification,
            evidence = EXCLUDED.evidence, cleared_at = NULL, clear_reason = NULL
        RETURNING * INTO ep;
        CONTINUE WHEN ep.alerted_at IS NOT NULL OR now_at - ep.blocked_since < p_alert_after;
        SELECT array_agg(q.id ORDER BY q.id), array_agg(DISTINCT q.ticket_id)
        INTO held_ids, held_tickets
        FROM ticket_board.ticket_notification_queue q
        WHERE q.target_role = role_name AND q.dead_lettered_at IS NULL;
        -- Said on the ticket whose notice waits longest, else on the role's own work.
        SELECT q.ticket_id INTO on_ticket
        FROM ticket_board.ticket_notification_queue q
        WHERE q.target_role = role_name AND q.dead_lettered_at IS NULL
        ORDER BY q.id LIMIT 1;
        IF on_ticket IS NULL THEN
            SELECT t.id INTO on_ticket
            FROM ticket_board.tickets t
            WHERE t.assignee = role_name AND ticket_board.transition_target_role(t.state, t.assignee) = role_name
            ORDER BY t.updated_at DESC NULLS LAST, t.id LIMIT 1;
        END IF;
        CONTINUE WHEN on_ticket IS NULL;  -- nothing waits on the pane, and there is nothing to say it on
        notice := format(
            '%s''s pane hook has said `blocked` since %s (%s min), written by `%s`. %s %s',
            role_name, to_char(ep.blocked_since, 'YYYY-MM-DD HH24:MI:SS TZ'),
            floor(extract(epoch FROM now_at - ep.blocked_since) / 60)::bigint, ep.source,
            CASE WHEN held_ids IS NULL THEN 'No ticket notice is waiting for it.'
                 ELSE format('%s ticket notice%s wait%s behind it: %s (notification%s %s).',
                             cardinality(held_ids), CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
                             CASE WHEN cardinality(held_ids) = 1 THEN 's' ELSE '' END,
                             array_to_string(held_tickets, ', '),
                             CASE WHEN cardinality(held_ids) = 1 THEN '' ELSE 's' END,
                             array_to_string(held_ids, ', ')) END,
            coalesce(pane->>'what', ''));
        alert_id := ticket_board.enqueue_notification(
            on_ticket, 'ticket_update', 'director', notice,
            jsonb_build_object('kind', 'blocked_pane', 'id', on_ticket, 'target_role', 'director',
                               'episode', ep.id, 'role', role_name, 'source', ep.source,
                               'classification', ep.classification, 'blocked_since', ep.blocked_since,
                               'notification_ids', coalesce(to_jsonb(held_ids), '[]'::jsonb),
                               'holders', coalesce(pane->'holders', '[]'::jsonb), 'message', notice),
            'blocked_pane:' || ep.id);
        UPDATE ticket_board.blocked_pane_episodes
        SET alerted_at = now_at, alert_notification_id = alert_id
        WHERE id = ep.id;
        alerted := alerted + 1;
    END LOOP;
    RETURN alerted;
END;
$$;

REVOKE ALL ON ticket_board.blocked_pane_episodes FROM PUBLIC;
GRANT SELECT ON ticket_board.blocked_pane_episodes TO ticket_board_service;
REVOKE EXECUTE ON FUNCTION ticket_board.close_blocked_pane(bigint, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_blocked_panes(jsonb, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.note_blocked_panes(jsonb, interval) TO ticket_board_listener;

COMMIT;
