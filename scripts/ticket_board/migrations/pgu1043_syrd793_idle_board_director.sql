-- SYRD-793 (pgu1043): work waiting on the Director counts toward the board stall alert when the whole board is idle.
--
-- note_board_stall left out every ticket whose next action is the Director's ("its next action is the Director's"):
-- its own handoff tells the Director. On sapi (2026-11-15) that handoff waited 2.4 hours as "pane busy" -- an
-- interrupted Claude turn had left the Director pane's hook busy -- while every role was idle, and nothing else spoke.
-- Now, when nothing is runnable and no role reports working or background work, those tickets are counted: after the
-- board stall threshold (the project's declared board_stall.threshold_seconds, else the listener's, 30 minutes by
-- default) the Director gets one "Board idle" digest, through the same episode, clock and dedupe. A role starting work
-- empties the set, which closes the episode. A board with no such ticket stays quiet.
--
-- The function is pgu1038's copy as schema.sql's last copy has it, with only that clause and its message added.
-- Grants are unchanged.
--
-- schema.sql carries the same function.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.note_board_stall(
    p_roles jsonb, p_threshold_seconds integer, p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE st ticket_board.board_stall_state; ep ticket_board.board_stall_episodes; v_last ticket_board.board_progress_events;
    v_runnable jsonb; v_excluded jsonb; v_clock timestamptz; anchor text; message text; queued bigint; lines text;
    v_waiting jsonb; v_idle boolean := false;
    -- The listener's configured value is the default; the project's declared one, when it has one, wins.
    listener integer := least(greatest(coalesce(p_threshold_seconds, 1800), 60), 86400);
    threshold integer := coalesce(ticket_board.declared_board_stall_seconds(), listener);
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_board_stall');
    SELECT * INTO st FROM ticket_board.board_stall_state WHERE id FOR UPDATE;
    SELECT * INTO v_last FROM ticket_board.board_progress_events ORDER BY id DESC LIMIT 1;
    SELECT coalesce(jsonb_agg(jsonb_build_object('ticket', b.ticket_id, 'state', b.state, 'role', b.role,
                                                 'waiting_since', b.waiting_since, 'responsible', resp.who,
                                                 'evidence', (SELECT string_agg(CASE WHEN jsonb_array_length(resp.who) > 1
                                                                                     THEN format('%s: %s', x->>'role', x->>'evidence')
                                                                                     ELSE x->>'evidence' END, ' | ')
                                                                FROM jsonb_array_elements(resp.who) x))
                              ORDER BY b.waiting_since, b.ticket_id) FILTER (WHERE b.why_not IS NULL), '[]'::jsonb),
           coalesce(jsonb_object_agg(b.ticket_id, b.why_not) FILTER (WHERE b.why_not IS NOT NULL), '{}'::jsonb)
      INTO v_runnable, v_excluded
      FROM ticket_board.board_stall_runnable(p_now) b
      CROSS JOIN LATERAL (SELECT ticket_board.board_stall_responsible(b.role, coalesce(p_roles, '{}'::jsonb)) AS who) resp;
    -- SYRD-793: when the whole board is idle -- nothing runnable, and no role working or running background work --
    -- work waiting on the Director counts too. Its own handoff is what tells the Director otherwise, and on sapi that
    -- handoff waited 2.4 hours behind a pane an interrupted turn had left marked busy while every role sat idle. The
    -- clock is the same: from the later of the last progress and the moment the board went idle.
    IF jsonb_array_length(v_runnable) = 0
       AND NOT EXISTS (SELECT FROM jsonb_each(coalesce(p_roles, '{}'::jsonb)) e
                        WHERE e.value->>'state' IN ('working', 'background work running')) THEN
        SELECT coalesce(jsonb_agg(jsonb_build_object('ticket', b.ticket_id, 'state', b.state, 'role', 'director',
                                                     'waiting_since', b.waiting_since, 'responsible', '[]'::jsonb,
                                                     'evidence', 'no role is working, and its next action is the Director''s')
                                  ORDER BY b.waiting_since, b.ticket_id), '[]'::jsonb)
          INTO v_waiting
          FROM ticket_board.board_stall_runnable(p_now) b WHERE b.why_not = 'its next action is the Director''s';
        IF jsonb_array_length(v_waiting) > 0 THEN
            v_runnable := v_waiting;
            v_idle := true;
            v_excluded := v_excluded - ARRAY(SELECT x->>'ticket' FROM jsonb_array_elements(v_waiting) x);
        END IF;
    END IF;
    SELECT * INTO ep FROM ticket_board.board_stall_episodes WHERE closed_at IS NULL FOR UPDATE;
    IF ep.id IS NOT NULL AND (jsonb_array_length(v_runnable) = 0 OR coalesce(v_last.id, 0) > coalesce(ep.progress_event_id, 0)) THEN
        UPDATE ticket_board.board_stall_episodes
           SET closed_at = p_now, close_reason = CASE WHEN jsonb_array_length(v_runnable) = 0 THEN 'no_runnable_work' ELSE 'progress' END
         WHERE id = ep.id;
        ep := NULL;
    END IF;
    IF jsonb_array_length(v_runnable) = 0 THEN
        UPDATE ticket_board.board_stall_state SET runnable_since = NULL, evaluated_at = p_now, threshold_seconds = threshold,
               default_threshold_seconds = listener,
               runnable = v_runnable, excluded = v_excluded WHERE id;
        RETURN jsonb_build_object('runnable', 0, 'episode', NULL);
    END IF;
    UPDATE ticket_board.board_stall_state SET runnable_since = coalesce(runnable_since, p_now), evaluated_at = p_now,
           threshold_seconds = threshold, default_threshold_seconds = listener, runnable = v_runnable,
           excluded = v_excluded WHERE id RETURNING * INTO st;
    v_clock := ticket_board.unpaused_since(greatest(st.runnable_since, coalesce(v_last.at, st.runnable_since)), p_now);  -- SYRD-756
    IF ep.id IS NULL AND p_now - v_clock >= make_interval(secs => threshold) THEN
        anchor := v_runnable->0->>'ticket';
        SELECT string_agg(format('%s (%s, %s; %s since %s)', x->>'ticket', x->>'state', coalesce(x->>'role', 'nobody'),
                                 coalesce(nullif(x->>'evidence', ''), 'no evidence'),
                                 to_char((x->>'waiting_since')::timestamptz AT TIME ZONE 'UTC', 'HH24:MI"Z"')), '; ')
          INTO lines FROM jsonb_array_elements(v_runnable) x;
        message := format(CASE WHEN v_idle
                               THEN 'Board idle: no role has worked for %s minutes (since %s), and work waits on you. '
                                    'Last progress: %s. Waiting on the Director: %s. Read: ticket-board-read stall'
                               ELSE 'Board stall: runnable work has made no durable progress for %s minutes (since %s). '
                                    'Last progress: %s. Runnable: %s. Read: ticket-board-read stall' END,
                          floor(extract(epoch FROM p_now - v_clock) / 60)::int,
                          to_char(v_clock AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI"Z"'),
                          coalesce(format('%s at %s', v_last.event, to_char(v_last.at AT TIME ZONE 'UTC', 'HH24:MI"Z"')),
                                   'none recorded'),
                          lines);
        INSERT INTO ticket_board.board_stall_episodes (opened_at, clock_started_at, progress_event_id, last_event,
               last_event_at, threshold_seconds, tickets, anchor_ticket)
        VALUES (p_now, v_clock, v_last.id, coalesce(v_last.event, 'none recorded'), v_last.at, threshold, v_runnable, anchor)
        RETURNING * INTO ep;
        queued := ticket_board.enqueue_notification(anchor, 'ticket_update', 'director', message,
            jsonb_build_object('kind', 'board_stall', 'episode', ep.id, 'id', anchor, 'target_role', 'director',
                               'tickets', v_runnable), 'board_stall:' || ep.id);
        UPDATE ticket_board.board_stall_episodes SET notification_id = queued WHERE id = ep.id;
    END IF;
    -- Keep a day of progress, and always the latest event.
    DELETE FROM ticket_board.board_progress_events
     WHERE at < p_now - interval '1 day' AND id < coalesce(v_last.id, 0);
    RETURN jsonb_build_object('runnable', jsonb_array_length(v_runnable), 'episode', ep.id, 'clock_started_at', v_clock);
END;
$$;

COMMIT;
