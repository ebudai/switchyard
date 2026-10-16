-- SYRD-594: a Codex turn that ended on "Selected model is at capacity" is
-- retried with backoff, and the Director is told once if the retries fail.
--
-- Codex 0.163.0 ends a turn whose model request found the model at capacity by
-- drawing "■ Selected model is at capacity. Please try a different model." and
-- returning to its empty prompt, and it runs no Stop hook for that turn. On
-- otto the uiux pane sat there until the User nudged it by hand.
--
-- The listener recognises that pane (scripts/ticket_board/capacity_retry.py)
-- and reports every Codex role's pane here on each pass. This keeps one episode
-- per role, in the database, so a restarted listener neither resets nor
-- repeats an attempt:
--
--   * while the role's current ticket is unchanged and its pane still shows the
--     error, one retry is queued after each delay the listener passes (1, 2 and
--     then 5 minutes), timed from the failure it answers, and only when nothing
--     else for the role is waiting to go;
--   * when the last retry has failed too, the Director is told once: the role,
--     the ticket, the condition and each failure's time;
--   * a normal turn end closes the episode, and so does the ticket moving or
--     changing hands. A retry or alert not yet sent is withdrawn, and a retry
--     is withdrawn as soon as the pane is working again.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.provider_capacity_episodes (
    id bigserial PRIMARY KEY,
    target_role text NOT NULL,
    ticket_id text NOT NULL,
    ticket_state text NOT NULL,
    ticket_assignee text NOT NULL,
    condition text NOT NULL,
    opened_at timestamptz NOT NULL,
    -- Each failed turn once, in order: {turn, turn_started_at, observed_at};
    -- `turn` is the turn's start in epoch seconds, its identity.
    failures jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Each retry queued, in order: {retry, notification_id, queued_at}.
    retries jsonb NOT NULL DEFAULT '[]'::jsonb,
    escalated_at timestamptz,
    escalation_notification_id bigint,
    closed_at timestamptz,
    close_reason text CHECK (close_reason IS NULL OR close_reason IN ('recovered', 'ticket_moved'))
);

-- One open episode per role.
CREATE UNIQUE INDEX IF NOT EXISTS provider_capacity_episodes_open
    ON ticket_board.provider_capacity_episodes (target_role) WHERE closed_at IS NULL;

-- Take back an episode's notice that has not gone yet.
CREATE OR REPLACE FUNCTION ticket_board.withdraw_provider_capacity_notice(
    p_notification_id bigint, p_episode_id bigint, p_why text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id, q.id, q.target_role, q.kind, 'discard', NULL, 'provider_capacity_' || p_why, NULL,
        jsonb_build_object('reason', 'provider_capacity_' || p_why, 'episode', p_episode_id))
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id AND q.dead_lettered_at IS NULL;
    DELETE FROM ticket_board.ticket_notification_queue WHERE id = p_notification_id AND dead_lettered_at IS NULL;
END;
$$;

-- Close an episode, withdrawing whatever of it has not gone yet.
CREATE OR REPLACE FUNCTION ticket_board.close_provider_capacity(p_episode_id bigint, p_how text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.provider_capacity_episodes%ROWTYPE;
BEGIN
    UPDATE ticket_board.provider_capacity_episodes
    SET closed_at = clock_timestamp(), close_reason = p_how
    WHERE id = p_episode_id AND closed_at IS NULL
    RETURNING * INTO ep;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM ticket_board.withdraw_provider_capacity_notice((r->>'notification_id')::bigint, ep.id, p_how)
    FROM jsonb_array_elements(ep.retries) r;
    IF ep.escalation_notification_id IS NOT NULL THEN
        PERFORM ticket_board.withdraw_provider_capacity_notice(ep.escalation_notification_id, ep.id, p_how);
    END IF;
END;
$$;

-- One listener pass's report: role -> {capacity_turn, ended_turn, working}.
-- capacity_turn is when the turn started that the pane shows ended on the
-- capacity error, while the pane is still and at its empty prompt; ended_turn
-- is when the turn started that the runtime's own hook says ended; working says
-- the pane is busy otherwise. A role left out could not be read, and nothing
-- about it changes. p_delays_seconds is the wait before each retry, and there
-- are as many retries as delays. Returns how many notices were queued.
CREATE OR REPLACE FUNCTION ticket_board.note_provider_capacity(
    p_report jsonb,
    p_delays_seconds double precision[],
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    item record;
    ep ticket_board.provider_capacity_episodes%ROWTYPE;
    cur_id text;
    cur_title text;
    cur_state text;
    cur_assignee text;
    cur_entered timestamptz;
    failed_turn timestamptz;
    ended_turn timestamptz;
    last_failure jsonb;
    sent integer;
    last_sent_at timestamptz;
    message text;
    notice_id bigint;
    failures_text text;
    waits_text text;
    queued integer := 0;
    error_text constant text := 'Selected model is at capacity. Please try a different model.';
    project text := coalesce(nullif(ticket_board.declared_workflow()->>'project', ''), '<project>');
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_provider_capacity');
    IF jsonb_typeof(p_report) IS DISTINCT FROM 'object' OR coalesce(cardinality(p_delays_seconds), 0) = 0 THEN
        RETURN 0;
    END IF;

    FOR item IN
        SELECT r.key AS role, r.value AS v FROM jsonb_each(p_report) r
        -- The Director's own pane is not escalated about: the alert would wait there too.
        WHERE r.key <> 'director' AND jsonb_typeof(r.value) = 'object'
        ORDER BY r.key
    LOOP
        BEGIN
            failed_turn := nullif(item.v->>'capacity_turn', '')::timestamptz;
            ended_turn := nullif(item.v->>'ended_turn', '')::timestamptz;
        EXCEPTION WHEN others THEN
            CONTINUE;
        END;
        SELECT * INTO ep FROM ticket_board.provider_capacity_episodes
        WHERE target_role = item.role AND closed_at IS NULL FOR UPDATE;

        -- The role's current work, selected as redeliver_replaced_work selects
        -- it: the ticket the board shows as the role's active work.
        WITH waiting AS MATERIALIZED (
            SELECT q.ticket_id FROM ticket_board.serial_queue() q GROUP BY q.ticket_id HAVING NOT bool_or(q.active)
        )
        SELECT t.id, t.title, t.state, t.assignee, ns.entered_current_state_at
        INTO cur_id, cur_title, cur_state, cur_assignee, cur_entered
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE ticket_board.transition_target_role(t.state, t.assignee) = item.role
          AND NOT t.manually_controlled
          AND coalesce(ns.awaiting_role, '') = ''
          AND coalesce(t.queued_for_assignee, '') = ''
          AND coalesce(t.queued_behind_ticket, '') = ''
          AND NOT coalesce((SELECT s.is_terminal FROM ticket_board.workflow_stages s WHERE s.name = t.state), false)
          AND NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id AND NOT b.resolved)
          AND NOT EXISTS (SELECT FROM waiting w WHERE w.ticket_id = t.id)
        ORDER BY ns.entered_current_state_at NULLS LAST, t.ticket_number
        LIMIT 1;

        -- The ticket moved on or changed hands: what was being retried is not current work any more.
        IF ep.id IS NOT NULL AND (cur_id IS NULL OR (cur_id, cur_state, cur_assignee)
                                  IS DISTINCT FROM (ep.ticket_id, ep.ticket_state, ep.ticket_assignee)) THEN
            PERFORM ticket_board.close_provider_capacity(ep.id, 'ticket_moved');
            ep := NULL;
        END IF;

        IF failed_turn IS NULL THEN
            IF ep.id IS NULL THEN
                CONTINUE;
            END IF;
            IF ended_turn IS NOT NULL
               AND extract(epoch FROM ended_turn) > (ep.failures->-1->>'turn')::numeric THEN
                -- A turn since the last failure ended as turns do: the provider answered.
                PERFORM ticket_board.close_provider_capacity(ep.id, 'recovered');
            ELSIF coalesce((item.v->>'working')::boolean, false) THEN
                -- Working again (or a person is typing): a retry not yet sent waits for nothing.
                PERFORM ticket_board.withdraw_provider_capacity_notice((r->>'notification_id')::bigint, ep.id, 'pane_busy')
                FROM jsonb_array_elements(ep.retries) r;
            END IF;
            CONTINUE;
        END IF;
        IF cur_id IS NULL OR failed_turn > p_now THEN
            CONTINUE;  -- no current work to carry on with
        END IF;
        IF ep.id IS NULL AND failed_turn < cur_entered THEN
            CONTINUE;  -- a turn from before this ticket was the role's: its own notice goes first
        END IF;

        IF ep.id IS NULL THEN
            INSERT INTO ticket_board.provider_capacity_episodes
                (target_role, ticket_id, ticket_state, ticket_assignee, condition, opened_at, failures)
            VALUES (item.role, cur_id, cur_state, cur_assignee, 'codex_capacity', p_now,
                    jsonb_build_array(jsonb_build_object('turn', extract(epoch FROM failed_turn),
                                                         'turn_started_at', failed_turn, 'observed_at', p_now)))
            RETURNING * INTO ep;
        ELSIF NOT EXISTS (SELECT FROM jsonb_array_elements(ep.failures) f
                          WHERE (f->>'turn')::numeric = extract(epoch FROM failed_turn)) THEN
            UPDATE ticket_board.provider_capacity_episodes
            SET failures = failures || jsonb_build_array(jsonb_build_object(
                    'turn', extract(epoch FROM failed_turn), 'turn_started_at', failed_turn, 'observed_at', p_now))
            WHERE id = ep.id
            RETURNING * INTO ep;
        END IF;

        -- A retry on its way already: it is the next attempt.
        IF EXISTS (SELECT FROM jsonb_array_elements(ep.retries) r
                   JOIN ticket_board.ticket_notification_queue q ON q.id = (r->>'notification_id')::bigint
                   WHERE q.dead_lettered_at IS NULL) THEN
            CONTINUE;
        END IF;
        -- A retry counts once it was typed into the pane; one withdrawn before then does not.
        SELECT count(*), max(sent_at) INTO sent, last_sent_at
        FROM (
            SELECT (SELECT min(t.ts) FROM ticket_board.notification_trace t
                    WHERE t.notification_id = (r->>'notification_id')::bigint
                      AND t.event IN ('send', 'send_unconfirmed')) AS sent_at
            FROM jsonb_array_elements(ep.retries) r
        ) s
        WHERE s.sent_at IS NOT NULL;
        last_failure := ep.failures->-1;

        IF sent >= cardinality(p_delays_seconds) THEN
            -- Every retry went, and the provider was still at capacity after the last: tell the Director, once.
            IF ep.escalated_at IS NULL AND (last_failure->>'observed_at')::timestamptz > last_sent_at THEN
                SELECT string_agg(to_char((f->>'observed_at')::timestamptz AT TIME ZONE 'UTC', 'HH24:MI:SS'), ', ')
                INTO failures_text FROM jsonb_array_elements(ep.failures) f;
                SELECT string_agg(trim_scale(round((d / 60)::numeric, 2))::text, ', ') INTO waits_text
                FROM unnest(p_delays_seconds) d;
                message := format(
                    'PRIORITY %s -- %s: %s''s Codex pane keeps stopping on "%s" Its turns failed %s times (%s UTC), '
                    'and the %s automatic retries, after %s minutes, did not get through. The pane is idle at its '
                    'prompt and nothing more will be sent to it on its own. Switch %s to another model, or nudge it '
                    'once the provider has capacity: `switchyard attach %s %s`.',
                    cur_id, cur_title, item.role, error_text, jsonb_array_length(ep.failures), failures_text,
                    sent, waits_text, item.role, project, item.role);
                notice_id := ticket_board.enqueue_notification(
                    cur_id, 'ticket_update', 'director', message,
                    jsonb_build_object('kind', 'provider_capacity', 'id', cur_id, 'title', cur_title,
                                       'state', cur_state, 'assignee', cur_assignee, 'target_role', 'director',
                                       'role', item.role, 'condition', ep.condition, 'error', error_text,
                                       'episode', ep.id, 'failures', ep.failures, 'retries', ep.retries,
                                       'message', message),
                    'provider_capacity:' || ep.id);
                UPDATE ticket_board.provider_capacity_episodes
                SET escalated_at = p_now, escalation_notification_id = notice_id
                WHERE id = ep.id;
                queued := queued + 1;
            END IF;
            CONTINUE;
        END IF;
        -- Not yet: each retry waits its delay after the failure it answers.
        IF p_now < (last_failure->>'observed_at')::timestamptz + make_interval(secs => p_delays_seconds[sent + 1]) THEN
            CONTINUE;
        END IF;
        -- Anything else on its way to this role goes first, and is itself the next turn.
        IF EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                   WHERE q.target_role = item.role AND q.dead_lettered_at IS NULL) THEN
            CONTINUE;
        END IF;
        message := ticket_board.transition_message(cur_id, cur_title, cur_state, cur_state, true)
                   || format('. Your last turn ended on Codex''s error "%s" before it finished: carry on from where '
                             'it stopped (automatic retry %s of %s).', error_text, sent + 1, cardinality(p_delays_seconds));
        notice_id := ticket_board.enqueue_notification(
            cur_id, 'ticket_update', item.role, message,
            jsonb_build_object('kind', 'capacity_retry', 'id', cur_id, 'title', cur_title, 'state', cur_state,
                               'assignee', cur_assignee, 'target_role', item.role, 'condition', ep.condition,
                               'episode', ep.id, 'retry', sent + 1, 'message', message),
            'capacity_retry:' || ep.id || ':' || (sent + 1));
        UPDATE ticket_board.provider_capacity_episodes
        SET retries = retries || jsonb_build_array(jsonb_build_object(
                'retry', sent + 1, 'notification_id', notice_id, 'queued_at', p_now))
        WHERE id = ep.id;
        queued := queued + 1;
    END LOOP;
    RETURN queued;
END;
$$;

-- Delivery asks this as well, so a retry the episode no longer wants is not typed.
CREATE OR REPLACE FUNCTION ticket_board.provider_capacity_retry_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM ticket_board.provider_capacity_episodes ep
        WHERE ep.id = (p_payload->>'episode')::bigint
          AND ep.closed_at IS NULL
          AND ep.target_role = p_payload->>'target_role'
          AND (ep.retries->-1->>'retry') = (p_payload->>'retry')
    );
$$;

REVOKE ALL ON ticket_board.provider_capacity_episodes FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.withdraw_provider_capacity_notice(bigint, bigint, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.close_provider_capacity(bigint, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_provider_capacity(jsonb, double precision[], timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.provider_capacity_retry_is_current(jsonb) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.provider_capacity_episodes TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.note_provider_capacity(jsonb, double precision[], timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.provider_capacity_retry_is_current(jsonb) TO ticket_board_listener;

COMMIT;
