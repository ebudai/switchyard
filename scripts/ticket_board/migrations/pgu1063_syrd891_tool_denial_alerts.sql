-- SYRD-891 (pgu1063): an agy role whose tool calls are all being denied, told to the Director once, whatever the
-- cause. A table of one row per role and run of refused calls, and the listener-only note_tool_denials that keeps
-- it and queues the Director's notice.
--
-- schema.sql carries the same.

BEGIN;

-- SYRD-891: an agy role whose tool calls are all being denied, told to the Director once. The listener reports, each
-- pass, every agy role whose transcript ends on a run of refused tool calls (scripts/ticket_board/tool_denials.py);
-- this keeps one row per role and run -- the conversation and the step it began at -- and queues one notice for it.
CREATE TABLE IF NOT EXISTS ticket_board.tool_denial_alerts (
    id bigserial PRIMARY KEY,
    target_role text NOT NULL,
    episode text NOT NULL CHECK (btrim(episode) <> ''),
    ticket_id text NOT NULL,
    denied integer NOT NULL CHECK (denied > 0),
    detail jsonb NOT NULL,
    notification_id bigint,
    noted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (target_role, episode)
);

-- p_report: role -> {episode, denied, first_at, last_at, last_error, suspects}. A role with no current ticket is not
-- recorded, so it is told about once it has one; the Director's own pane is never told about, as the notice would
-- wait there too. Returns how many notices were queued.
CREATE OR REPLACE FUNCTION ticket_board.note_tool_denials(p_report jsonb)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    item record;
    v_episode text;
    v_denied integer;
    cur ticket_board.tickets%ROWTYPE;
    added integer;
    notice text;
    alert_id bigint;
    causes text;
    queued integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_tool_denials');
    IF jsonb_typeof(p_report) IS DISTINCT FROM 'object' THEN
        RETURN 0;
    END IF;
    FOR item IN
        SELECT r.key AS role, r.value AS v FROM jsonb_each(p_report) r
        WHERE r.key <> 'director' AND jsonb_typeof(r.value) = 'object' ORDER BY r.key
    LOOP
        v_episode := btrim(coalesce(item.v->>'episode', ''));
        BEGIN
            v_denied := (item.v->>'denied')::integer;
        EXCEPTION WHEN others THEN
            CONTINUE;
        END;
        IF v_episode = '' OR v_denied IS NULL OR v_denied <= 0 THEN
            CONTINUE;
        END IF;
        SELECT t.* INTO cur FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE ticket_board.transition_target_role(t.state, t.assignee) = item.role
          AND NOT coalesce((SELECT s.is_terminal FROM ticket_board.workflow_stages s WHERE s.name = t.state), false)
        ORDER BY ns.entered_current_state_at DESC NULLS LAST, t.ticket_number DESC
        LIMIT 1;
        IF NOT FOUND THEN
            CONTINUE;
        END IF;
        INSERT INTO ticket_board.tool_denial_alerts (target_role, episode, ticket_id, denied, detail)
        VALUES (item.role, v_episode, cur.id, v_denied, item.v)
        ON CONFLICT (target_role, episode) DO NOTHING;
        GET DIAGNOSTICS added = ROW_COUNT;
        IF added = 0 THEN
            CONTINUE;
        END IF;
        SELECT string_agg(value, '; ') INTO causes FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(item.v->'suspects') = 'array' THEN item.v->'suspects' ELSE '[]'::jsonb END);
        notice := format('%s, on agy, has had its last %s tool calls denied and none run since %s, on %s: agy said "%s". '
                         'Likely: %s.',
                         item.role, v_denied, coalesce(nullif(item.v->>'first_at', ''), 'an unknown time'), cur.id,
                         left(coalesce(item.v->>'last_error', ''), 300), coalesce(nullif(causes, ''), 'unknown'));
        alert_id := ticket_board.enqueue_notification(
            cur.id, 'ticket_update', 'director', notice,
            jsonb_build_object('kind', 'tool_denials', 'id', cur.id, 'target_role', 'director', 'role', item.role,
                               'state', cur.state, 'assignee', cur.assignee, 'episode', v_episode, 'denied', v_denied,
                               'message', notice),
            'tool_denials:' || item.role || ':' || v_episode);
        UPDATE ticket_board.tool_denial_alerts SET notification_id = alert_id
        WHERE target_role = item.role AND episode = v_episode;
        queued := queued + 1;
    END LOOP;
    RETURN queued;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.note_tool_denials(jsonb) FROM PUBLIC;

COMMIT;
