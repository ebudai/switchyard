BEGIN;

-- SYRD-728 (pgu1028): a Director Codex turn that ended on "Selected model is at capacity" is retried, and the
-- operator is told -- not the Director -- when the retries are spent.
--
-- SYRD-594 retries a ticket owner's turn through the notification queue, and leaves the Director out: an alert
-- about the Director's pane would wait in that same pane, and its episode is bound to a ticket the Director may not
-- have. On 2026-11-02 the Director's Codex turn ended on the error, a person's "please retry" met it again, and
-- nothing resumed the Director when capacity came back.
--
-- The listener reports the Director's pane each pass (capacity_retry.report: the failed turn, a turn that ended
-- normally, a person typing, and the pane's process) to note_director_capacity, which keeps one episode, in
-- the database, so a restarted listener neither resets nor repeats an attempt:
--
--   * an attempt is claimed here before the listener types it, timed from the failure it answers by each of the
--     listener's delays (1, 2 and then 5 minutes), one at a time, and only when nothing else for the Director is
--     queued -- that goes first and is the next turn; the listener records whether it typed it;
--   * a turn that ends normally closes the episode (recovered); so does a person typing in the composer, or a
--     failed turn that is not exactly one the board's own sends recorded starting (superseded), or a new
--     registration or pane process (runtime_changed);
--   * when the last attempt has failed too: one user_events row (director_capacity) for the phone bridge, and the
--     board page's banner (director_capacity_status), never a notice to the same pane. A later normal turn clears
--     it.
--
-- No notice row: the queue needs a ticket, and a Director turn may have none.
CREATE TABLE IF NOT EXISTS ticket_board.director_capacity_episodes (
    id bigserial PRIMARY KEY,
    opened_at timestamptz NOT NULL,
    -- The Director's runtime registration and pane process when it opened: {generation, pid, start_time, target} and
    -- {pid, start_time}. Either changing ends the episode: a new conversation is not the one interrupted.
    runtime jsonb NOT NULL,
    pane jsonb NOT NULL,
    -- The ticket the first failed turn's board notice was about, if a notice started it; informational.
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    -- Each failed turn once, in order: {turn, observed_at}; `turn` is its start in epoch seconds.
    failures jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Each attempt, in order: {attempt, claimed_at, outcome, turn, recorded_at}; outcome sent, send_failed or
    -- not_sent; turn the start of the turn it began, as the pane's hook recorded it.
    attempts jsonb NOT NULL DEFAULT '[]'::jsonb,
    escalated_at timestamptz,
    user_event_id bigint,
    closed_at timestamptz,
    close_reason text CHECK (close_reason IS NULL OR close_reason = ANY (ARRAY['recovered', 'superseded',
                                                                               'runtime_changed', 'exhausted'])),
    -- An exhausted episode's alert stands until a Director turn ends normally.
    cleared_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS director_capacity_episodes_open
    ON ticket_board.director_capacity_episodes ((true)) WHERE closed_at IS NULL;

-- The phone bridge's events gain the kind. A ticket stays required for every kind but the two that are about the
-- board as a whole. Written with ANY: schema_test reads the last "kind IN" list in schema.sql as the queue's.
DO $kinds$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'ticket_board.user_events'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%kind%' LOOP
        EXECUTE format('ALTER TABLE ticket_board.user_events DROP CONSTRAINT %I', c.conname);
    END LOOP;
END
$kinds$;
ALTER TABLE ticket_board.user_events
    ADD CONSTRAINT user_events_kind_known CHECK (kind = ANY (ARRAY['user_review', 'operator_blocker',
        'operation_failed', 'director_ad_hoc', 'director_capacity'])),
    ADD CONSTRAINT user_events_notice_is_ad_hoc CHECK ((kind = 'director_ad_hoc') = (notice_id IS NOT NULL)),
    ADD CONSTRAINT user_events_ticket_unless_board_wide CHECK (
        kind = ANY (ARRAY['director_ad_hoc', 'director_capacity']) OR ticket_id IS NOT NULL);

CREATE OR REPLACE FUNCTION ticket_board.user_events_after(p_after bigint, p_limit integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'latest', (SELECT coalesce(max(id), 0) FROM ticket_board.user_events),
        'events', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', e.id,
                       'key', format('%s:%s:%s:%s', e.kind, coalesce(e.ticket_id, '-'), e.id,
                                     to_char(e.at AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS.US')),
                       'kind', e.kind, 'ticket', coalesce(e.ticket_id, ''), 'detail', e.detail, 'at', e.at,
                       'title', coalesce(left(t.title, 120), ''),
                       'notice', CASE WHEN n.id IS NULL THEN NULL ELSE jsonb_build_object(
                           'title', n.title, 'message', n.message, 'urgent', n.urgent, 'reason', n.reason, 'requester_role',
                           n.requester_role, 'requester_pid', n.requester_pid, 'requester_generation',
                           n.requester_generation, 'digest', n.digest) END,
                       'standing', CASE e.kind
                           WHEN 'user_review' THEN t.state = e.detail
                                                   AND ticket_board.user_event_needs_user(t.state, t.assignee)
                           WHEN 'operator_blocker' THEN EXISTS (
                               SELECT FROM ticket_board.user_event_operator_waits w
                                WHERE w.ticket_id = e.ticket_id AND w.operator = e.detail)
                           -- SYRD-728: until a Director turn ends normally again.
                           WHEN 'director_capacity' THEN EXISTS (
                               SELECT FROM ticket_board.director_capacity_episodes d
                                WHERE d.user_event_id = e.id AND d.cleared_at IS NULL)
                           ELSE true END) ORDER BY e.id)
              FROM (SELECT * FROM ticket_board.user_events WHERE id > greatest(coalesce(p_after, 0), 0)
                     ORDER BY id LIMIT least(greatest(coalesce(p_limit, 20), 1), 50)) e
              LEFT JOIN ticket_board.tickets t ON t.id = e.ticket_id
              LEFT JOIN ticket_board.director_notices n ON n.id = e.notice_id), '[]'::jsonb));
$$;

-- One listener pass's report of the Director's pane: {capacity_turn, ended_turn, human, pane: {pid, start_time}}, as capacity_retry.report reads it. Returns what to do now: {retry, of, episode, message} for an
-- attempt claimed here, which the listener types and then records; {escalated} for an alert raised; or {}.
CREATE OR REPLACE FUNCTION ticket_board.note_director_capacity(
    p_report jsonb, p_delays_seconds double precision[], p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.director_capacity_episodes%ROWTYPE;
    failed_turn timestamptz;
    ended_turn timestamptz;
    v_runtime jsonb;
    v_pane jsonb;
    v_ticket text;
    counted integer;
    last_claim timestamptz;
    last_failure timestamptz;
    base timestamptz;
    v_message text;
    v_event bigint;
    error_text constant text := 'Selected model is at capacity. Please try a different model.';
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_director_capacity');
    IF jsonb_typeof(p_report) IS DISTINCT FROM 'object' OR coalesce(cardinality(p_delays_seconds), 0) = 0 THEN
        RETURN '{}'::jsonb;
    END IF;
    BEGIN
        failed_turn := nullif(p_report->>'capacity_turn', '')::timestamptz;
        ended_turn := nullif(p_report->>'ended_turn', '')::timestamptz;
    EXCEPTION WHEN others THEN
        RETURN '{}'::jsonb;
    END;
    SELECT jsonb_build_object('generation', a.generation, 'pid', a.process_pid, 'start_time', a.process_start_time,
                              'target', a.actual_target)
      INTO v_runtime FROM ticket_board.role_runtime_assignments a WHERE a.role = 'director';
    v_pane := CASE WHEN jsonb_typeof(p_report->'pane') = 'object' THEN p_report->'pane' END;
    SELECT * INTO ep FROM ticket_board.director_capacity_episodes WHERE closed_at IS NULL FOR UPDATE;

    -- A normal turn end: the provider answered. It ends an open episode and clears an exhausted one's alert.
    IF ended_turn IS NOT NULL THEN
        UPDATE ticket_board.director_capacity_episodes SET cleared_at = p_now
         WHERE close_reason = 'exhausted' AND cleared_at IS NULL
           AND extract(epoch FROM ended_turn) > (failures->-1->>'turn')::numeric;
    END IF;
    IF ep.id IS NOT NULL AND (v_runtime IS DISTINCT FROM ep.runtime OR v_pane IS DISTINCT FROM ep.pane) THEN
        UPDATE ticket_board.director_capacity_episodes SET closed_at = p_now, close_reason = 'runtime_changed'
         WHERE id = ep.id;
        ep := NULL;
    END IF;
    IF ep.id IS NOT NULL AND failed_turn IS NULL THEN
        IF ended_turn IS NOT NULL AND extract(epoch FROM ended_turn) > (ep.failures->-1->>'turn')::numeric THEN
            UPDATE ticket_board.director_capacity_episodes SET closed_at = p_now, close_reason = 'recovered'
             WHERE id = ep.id;
        ELSIF coalesce((p_report->>'human')::boolean, false) THEN
            UPDATE ticket_board.director_capacity_episodes SET closed_at = p_now, close_reason = 'superseded'
             WHERE id = ep.id;
        END IF;
        RETURN '{}'::jsonb;
    END IF;
    IF failed_turn IS NULL OR failed_turn > p_now OR v_runtime IS NULL OR v_pane IS NULL THEN
        RETURN '{}'::jsonb;
    END IF;

    IF ep.id IS NULL THEN
        -- The screen of an episode already over (exhausted, superseded, moved on) opens nothing.
        IF EXISTS (SELECT FROM ticket_board.director_capacity_episodes d, jsonb_array_elements(d.failures) f
                    WHERE (f->>'turn')::numeric >= extract(epoch FROM failed_turn)) THEN
            RETURN '{}'::jsonb;
        END IF;
        -- The ticket of the board notice that started the failed turn, if one did: the delivery records the turn it
        -- started, by the pane's own hook.
        SELECT t.ticket_id INTO v_ticket FROM ticket_board.notification_trace t
         WHERE t.target_role = 'director' AND t.event = 'send'
           AND (t.detail->'submission'->>'turn')::timestamptz = failed_turn
         ORDER BY t.ts LIMIT 1;
        INSERT INTO ticket_board.director_capacity_episodes (opened_at, runtime, pane, ticket_id, failures)
        VALUES (p_now, v_runtime, v_pane, v_ticket,
                jsonb_build_array(jsonb_build_object('turn', extract(epoch FROM failed_turn), 'observed_at', p_now)))
        RETURNING * INTO ep;
    ELSIF NOT EXISTS (SELECT FROM jsonb_array_elements(ep.failures) f
                       WHERE (f->>'turn')::numeric = extract(epoch FROM failed_turn)) THEN
        -- A turn the board started -- an attempt, or a board notice -- is the board's only when it is exactly the
        -- turn that send recorded starting, by the pane's own hook. Any other turn is a person's, whenever it began:
        -- they have taken over, and the board stops. A send whose turn was never seen claims none.
        IF NOT EXISTS (SELECT FROM jsonb_array_elements(ep.attempts) a
                        WHERE (a->>'turn')::timestamptz = failed_turn)
           AND NOT EXISTS (SELECT FROM ticket_board.notification_trace t
                            WHERE t.target_role = 'director' AND t.event = 'send'
                              AND (t.detail->'submission'->>'turn')::timestamptz = failed_turn) THEN
            UPDATE ticket_board.director_capacity_episodes SET closed_at = p_now, close_reason = 'superseded',
                   failures = failures || jsonb_build_array(jsonb_build_object(
                       'turn', extract(epoch FROM failed_turn), 'observed_at', p_now))
             WHERE id = ep.id;
            RETURN '{}'::jsonb;
        END IF;
        UPDATE ticket_board.director_capacity_episodes
           SET failures = failures || jsonb_build_array(jsonb_build_object(
                   'turn', extract(epoch FROM failed_turn), 'observed_at', p_now))
         WHERE id = ep.id RETURNING * INTO ep;
    END IF;

    -- An attempt counts once claimed, unless the listener found the pane changed and typed nothing: a claim whose
    -- listener stopped before recording it may have been typed, and is never typed again.
    SELECT count(*), max((a->>'claimed_at')::timestamptz) INTO counted, last_claim
      FROM jsonb_array_elements(ep.attempts) a WHERE coalesce(a->>'outcome', '') <> 'not_sent';
    last_failure := (ep.failures->-1->>'observed_at')::timestamptz;
    base := greatest(last_failure, last_claim);

    IF counted >= cardinality(p_delays_seconds) THEN
        -- The last attempt failed too (a failure seen since it), or nothing came of it in its own wait.
        IF last_failure > last_claim OR p_now >= last_claim + make_interval(secs => p_delays_seconds[counted]) THEN
            v_message := format('The Director''s Codex pane stopped on "%s" at %s UTC, and %s automatic retries did '
                                'not get through; nothing more is sent to it.', error_text,
                                to_char((ep.failures->0->>'observed_at')::timestamptz AT TIME ZONE 'UTC', 'HH24:MI'),
                                counted);
            INSERT INTO ticket_board.user_events (kind, ticket_id, detail)
            VALUES ('director_capacity', NULL, ep.id::text) RETURNING id INTO v_event;
            UPDATE ticket_board.director_capacity_episodes
               SET escalated_at = p_now, user_event_id = v_event, closed_at = p_now, close_reason = 'exhausted'
             WHERE id = ep.id;
            RETURN jsonb_build_object('escalated', ep.id, 'event', v_event, 'message', v_message);
        END IF;
        RETURN '{}'::jsonb;
    END IF;
    IF p_now < base + make_interval(secs => p_delays_seconds[counted + 1]) THEN
        RETURN '{}'::jsonb;
    END IF;
    -- Anything already on its way to the Director goes first, and is itself the next turn.
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                WHERE q.target_role = 'director' AND q.dead_lettered_at IS NULL) THEN
        RETURN '{}'::jsonb;
    END IF;
    v_message := format('Your last turn ended on Codex''s error "%s" before it finished%s (automatic retry %s of %s). '
                        'Before you repeat anything, check what that turn already did -- the board, git status and '
                        'log, and any command it ran -- and do not redo what took effect. Then carry on from where it '
                        'stopped, or say it was finished.', error_text,
                        CASE WHEN ep.ticket_id IS NOT NULL THEN format('; it began with the board''s notice about %s',
                                                                      ep.ticket_id) ELSE '' END,
                        counted + 1, cardinality(p_delays_seconds));
    UPDATE ticket_board.director_capacity_episodes
       SET attempts = attempts || jsonb_build_array(jsonb_build_object('attempt', jsonb_array_length(attempts) + 1,
                                                                       'claimed_at', p_now))
     WHERE id = ep.id;
    RETURN jsonb_build_object('retry', counted + 1, 'of', cardinality(p_delays_seconds), 'episode', ep.id,
                              'attempt', jsonb_array_length(ep.attempts) + 1, 'message', v_message);
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.note_director_capacity(jsonb, double precision[], timestamptz) FROM PUBLIC;

-- What the listener did with an attempt it was given: sent, send_failed, or not_sent (the pane had changed when it
-- looked again, and nothing was typed); and, for one sent, the turn it started, as the pane's hook recorded it, or
-- NULL when none was seen. Once only.
CREATE OR REPLACE FUNCTION ticket_board.record_director_capacity_attempt(p_episode bigint, p_attempt integer,
                                                                        p_outcome text, p_turn timestamptz)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('record_director_capacity_attempt');
    IF p_outcome IS NULL OR NOT p_outcome = ANY (ARRAY['sent', 'send_failed', 'not_sent']) THEN
        RAISE EXCEPTION 'an attempt is sent, send_failed or not_sent, not %', p_outcome;
    END IF;
    UPDATE ticket_board.director_capacity_episodes d
       SET attempts = (SELECT jsonb_agg(CASE WHEN (a->>'attempt')::integer = p_attempt AND NOT a ? 'outcome'
                                             THEN a || jsonb_build_object('outcome', p_outcome, 'turn', p_turn,
                                                                          'recorded_at', clock_timestamp())
                                             ELSE a END ORDER BY n)
                         FROM jsonb_array_elements(d.attempts) WITH ORDINALITY x(a, n))
     WHERE d.id = p_episode;
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.record_director_capacity_attempt(bigint, integer, text, timestamptz) FROM PUBLIC;

-- The board page's banner and GET /api/director-capacity: the standing alert, if any, and the open episode.
CREATE OR REPLACE FUNCTION ticket_board.director_capacity_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'alert', (SELECT jsonb_build_object('episode', d.id, 'since', ticket_board.utc_text(
                                                (d.failures->0->>'observed_at')::timestamptz),
                                            'escalated_at', ticket_board.utc_text(d.escalated_at),
                                            'failures', jsonb_array_length(d.failures),
                                            'attempts', (SELECT count(*) FROM jsonb_array_elements(d.attempts) a
                                                          WHERE coalesce(a->>'outcome', '') <> 'not_sent'),
                                            'ticket', d.ticket_id, 'error', 'Selected model is at capacity. Please try a different model.')
                    FROM ticket_board.director_capacity_episodes d
                   WHERE d.close_reason = 'exhausted' AND d.cleared_at IS NULL ORDER BY d.id DESC LIMIT 1),
        'open', (SELECT jsonb_build_object('episode', d.id, 'since', ticket_board.utc_text(d.opened_at),
                                           'failures', jsonb_array_length(d.failures),
                                           'attempts', jsonb_array_length(d.attempts), 'ticket', d.ticket_id)
                   FROM ticket_board.director_capacity_episodes d WHERE d.closed_at IS NULL));
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.director_capacity_status() FROM PUBLIC;

COMMIT;
