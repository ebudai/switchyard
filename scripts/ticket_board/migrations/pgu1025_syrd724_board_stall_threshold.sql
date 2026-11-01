BEGIN;

-- SYRD-724 (pgu1025): a project's own board stall threshold.
--
-- The board stall alert -- runnable work with no durable progress (SYRD-637) --
-- fired after the listener's one configured threshold, 30 minutes unless the
-- host set TICKET_BOARD_BOARD_STALL_SECONDS. A project whose validation phases
-- legitimately run longer was alerted, and snoozed, for healthy work. Its
-- declared workflow may now carry `board_stall.threshold_seconds`, a whole
-- number of seconds from 300 to 86400 (workflow_config refuses anything else,
-- and the Director alone applies a workflow). The evaluator reads it on every
-- pass and uses it in place of the listener's value, which stays the default
-- for a project that declares none and is recorded beside it, so the status
-- says which one is in force.
--
-- Only this alert reads it. The role stall escalation's busy threshold is the
-- listener's own value, passed in (SYRD-675); leases, held notices, provider
-- quota and outage detection have their own. A change counts from the next
-- pass, against the same progress clock: it opens no episode while one is
-- open, so an alert already sent is never sent again, and it restarts nothing.
-- Re-running root's provisioning packet keeps it (provisioned_workflow).
ALTER TABLE ticket_board.board_stall_state ADD COLUMN IF NOT EXISTS default_threshold_seconds integer
    CHECK (default_threshold_seconds IS NULL OR default_threshold_seconds BETWEEN 60 AND 86400);

-- The project's declared threshold, or NULL for the default. A value outside
-- what workflow_config admits is read as none, never as a reason to stop.
CREATE OR REPLACE FUNCTION ticket_board.declared_board_stall_seconds()
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN jsonb_typeof(d->'board_stall'->'threshold_seconds') = 'number'
                     AND (d->'board_stall'->>'threshold_seconds') ~ '^[0-9]{3,5}$'
                     AND (d->'board_stall'->>'threshold_seconds')::integer BETWEEN 300 AND 86400
                THEN (d->'board_stall'->>'threshold_seconds')::integer END
      FROM (SELECT ticket_board.declared_workflow() AS d) w;
$$;

CREATE OR REPLACE FUNCTION ticket_board.note_board_stall(
    p_roles jsonb, p_threshold_seconds integer, p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE st ticket_board.board_stall_state; ep ticket_board.board_stall_episodes; v_last ticket_board.board_progress_events;
    v_runnable jsonb; v_excluded jsonb; v_clock timestamptz; anchor text; message text; queued bigint; lines text;
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
    v_clock := greatest(st.runnable_since, coalesce(v_last.at, st.runnable_since));
    IF ep.id IS NULL AND p_now - v_clock >= make_interval(secs => threshold) THEN
        anchor := v_runnable->0->>'ticket';
        SELECT string_agg(format('%s (%s, %s; %s since %s)', x->>'ticket', x->>'state', coalesce(x->>'role', 'nobody'),
                                 coalesce(nullif(x->>'evidence', ''), 'no evidence'),
                                 to_char((x->>'waiting_since')::timestamptz AT TIME ZONE 'UTC', 'HH24:MI"Z"')), '; ')
          INTO lines FROM jsonb_array_elements(v_runnable) x;
        message := format('Board stall: runnable work has made no durable progress for %s minutes (since %s). '
                          'Last progress: %s. Runnable: %s. Read: ticket-board-read stall',
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

CREATE OR REPLACE FUNCTION ticket_board.board_stall_status(p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    WITH st AS (SELECT * FROM ticket_board.board_stall_state WHERE id),
         declared AS (SELECT ticket_board.declared_board_stall_seconds() AS seconds),
         eff AS (SELECT coalesce(declared.seconds, st.default_threshold_seconds, st.threshold_seconds) AS seconds,
                        coalesce(st.default_threshold_seconds, st.threshold_seconds) AS default_seconds
                   FROM st, declared),
         last AS (SELECT * FROM ticket_board.board_progress_events ORDER BY id DESC LIMIT 1),
         clock AS (SELECT CASE WHEN st.runnable_since IS NULL THEN NULL
                               ELSE greatest(st.runnable_since, coalesce((SELECT at FROM last), st.runnable_since)) END AS at
                     FROM st)
    SELECT jsonb_build_object(
        'evaluated_at', st.evaluated_at, 'threshold_seconds', eff.seconds,
        'threshold_source', CASE WHEN declared.seconds IS NULL THEN 'default' ELSE 'workflow' END,
        'default_threshold_seconds', eff.default_seconds, 'evaluated_threshold_seconds', st.threshold_seconds,
        'runnable', st.runnable, 'excluded', st.excluded, 'runnable_since', st.runnable_since,
        'clock_started_at', clock.at,
        'fires_at', CASE WHEN clock.at IS NULL THEN NULL ELSE clock.at + make_interval(secs => eff.seconds) END,
        'last_progress', (SELECT jsonb_build_object('at', at, 'event', event, 'ticket', ticket_id) FROM last),
        'open_episode', (SELECT to_jsonb(e) FROM ticket_board.board_stall_episodes e WHERE e.closed_at IS NULL),
        'recent_episodes', (SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.id DESC), '[]'::jsonb)
                              FROM (SELECT * FROM ticket_board.board_stall_episodes ORDER BY id DESC LIMIT 5) e))
      FROM st, clock, declared, eff;
$$;

-- What root's provisioning packet applies (provision_workflow_sql.render_workflow_sql). Root's recorded workflow
-- is the document, and wins everywhere, but a board that already runs a declared workflow keeps its own board stall
-- threshold: the Director's decision, made since root recorded the workflow, which grants nothing. Root's copy of
-- that one key is not applied; a board running none takes root's record as it is. Its caller holds the workflow
-- lock from an earlier statement (the packet does), so this reads the board's value after any apply ahead of it.
CREATE OR REPLACE FUNCTION ticket_board.provisioned_workflow(p_document jsonb)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN p_document
                WHEN ticket_board.declared_board_stall_seconds() IS NULL THEN p_document - 'board_stall'
                ELSE jsonb_set(p_document, '{board_stall}',
                               jsonb_build_object('threshold_seconds', ticket_board.declared_board_stall_seconds()))
           END;
$$;

COMMIT;
