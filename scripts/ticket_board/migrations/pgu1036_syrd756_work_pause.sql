-- SYRD-756 (pgu1036): the Director's pause switch. Stop all work now, or drain.
--
-- One project's pause, on its own board, set by its Director or its User and recorded with who, when and why:
--
--   drain -- nothing is typed into any role pane but the Director's: no transition notice, nudge, reminder,
--            escalation to an owner, capacity retry, held-notice release, context restore, claim, route or worker
--            start. A turn already running finishes. Board writes keep working. What would have gone waits in the
--            queue, held, and goes once at the resume if it is still current.
--   stop  -- all of that, and each role pane busy with a turn that was running when stop was set has that turn
--            interrupted with its runtime's own interrupt (the listener does it, never Ctrl-C and never killing a CLI).
--            Once that pane is idle it is told, once, that work is paused; its background commands are reported to
--            the Director with pid and ticket, not killed. The resume tells each such role once that work resumed.
--
-- The Director's pane is exempt, and so is the User, who has none. The Publisher keeps publishing (User decision,
-- 2026-11-12): it is the listener's, not a pane.
--
-- The pause reuses the provider-hold machinery (SYRD-644, SYRD-672) rather than building beside it:
-- role_provider_hold_is_current -- the question every reminder, unresolved-turn prompt, stall episode, board-stall
-- digest, review balance and pull pickup already asks -- also says "held" for a paused role, and the listener's
-- delivery check asks the pause first (work_pause.hold_delivery, beside quota_hold.hold_delivery) and requeues as
-- `work_paused`, which the resume makes due at once, as the account hold's clear does. Who may set and end it is
-- who may hold a provider account (provider_account_hold_actor): the Director, or the User. Clocks that would
-- otherwise all come due at the resume subtract paused time (unpaused_since; pgu1037 and pgu1038 apply it).
--
-- schema.sql carries the same objects; rbac.sql the same grants.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.work_pauses (
    id bigserial PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('drain', 'stop')),
    began_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    began_by text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    -- When the mode last became 'stop'. A turn running then is the Director's to interrupt; one that began after it
    -- was begun by a person typing into the pane, which still works, and is left alone.
    stop_since timestamptz,
    -- Every change of mode: {at, by, from, to, reason}.
    changes jsonb NOT NULL DEFAULT '[]'::jsonb,
    ended_at timestamptz,
    ended_by text,
    end_reason text,
    CHECK ((mode = 'stop') = (stop_since IS NOT NULL) OR ended_at IS NOT NULL),
    CHECK ((ended_at IS NULL) = (ended_by IS NULL))
);
-- At most one pause stands at a time.
CREATE UNIQUE INDEX IF NOT EXISTS work_pauses_current ON ticket_board.work_pauses ((true)) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS work_pauses_span ON ticket_board.work_pauses (began_at);

-- What a pause did to each role: the turn it interrupted, how that turn ended, the notice it was given, what was
-- reported of its background commands, and the resume notice.
CREATE TABLE IF NOT EXISTS ticket_board.work_pause_roles (
    pause_id bigint NOT NULL REFERENCES ticket_board.work_pauses(id),
    role text NOT NULL,
    runtime text NOT NULL DEFAULT '',
    -- The interrupted turn, by the pane hook's own turn start (epoch seconds), so the same turn is never interrupted
    -- twice and a later one only by a later stop.
    turn_started_at double precision,
    interrupted_at timestamptz,
    interrupt jsonb,
    -- 'idle': the turn ended and the pane is free; 'prompt_returned': the runtime put the interrupted prompt back in
    -- the composer, where it stays; 'typed': the runtime's interrupt is a message, and the notice was that message.
    settled text CHECK (settled IN ('idle', 'prompt_returned', 'typed')),
    settled_at timestamptz,
    notice_ticket text,
    noticed_at timestamptz,
    background jsonb,
    background_reported_at timestamptz,
    resumed_notice_at timestamptz,
    PRIMARY KEY (pause_id, role)
);

-- The pause standing now, or NULL.
CREATE OR REPLACE FUNCTION ticket_board.current_work_pause()
RETURNS ticket_board.work_pauses LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT * FROM ticket_board.work_pauses WHERE ended_at IS NULL;
$$;

-- The pause that holds this role, or NULL. Only role panes are paused: never the Director's, and never a role with
-- no pane -- the User, or a role with no runtime that has never registered one, such as the Publisher, which keeps
-- publishing (User decision, 2026-11-12).
CREATE OR REPLACE FUNCTION ticket_board.role_paused(p_role text)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT p.id FROM ticket_board.work_pauses p
     WHERE p.ended_at IS NULL
       AND coalesce(p_role, '') NOT IN ('', 'director', 'user')
       AND NOT EXISTS (SELECT FROM ticket_board.workflow_roles w
                        WHERE w.name = p_role
                          AND (w.definition->>'kind' = 'user'
                               OR (coalesce(w.definition->>'runtime', '') = ''
                                   AND NOT EXISTS (SELECT FROM ticket_board.role_runtime_assignments a
                                                    WHERE a.role = p_role))));
$$;

-- A clock's start, moved on by the time work was paused between it and p_now: a clock reads "elapsed" as
-- p_now - unpaused_since(start, p_now), so it does not run while paused and resuming fires nothing for the pause.
CREATE OR REPLACE FUNCTION ticket_board.unpaused_since(p_start timestamptz, p_now timestamptz)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN p_start IS NULL OR p_now IS NULL OR p_start >= p_now
                     OR p_start = '-infinity'::timestamptz THEN p_start
                ELSE p_start + coalesce((
                    SELECT sum(least(coalesce(p.ended_at, p_now), p_now) - greatest(p.began_at, p_start))
                      FROM ticket_board.work_pauses p
                     WHERE p.began_at < p_now AND coalesce(p.ended_at, p_now) > p_start), interval '0') END;
$$;

-- The pause notice's text and the resume notice's: the words a role is given, as a function so a test reads the
-- same words the board sends.
CREATE OR REPLACE FUNCTION ticket_board.work_pause_notice_text(p_kind text, p_pause ticket_board.work_pauses,
                                                               p_ticket text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE p_kind
        WHEN 'work_pause' THEN format(
            'WORK PAUSED (pause %s): %s stopped all work in this project -- "%s". Your turn was interrupted on '
            'purpose. Record where you are on %s now: one board comment saying what is done, what is half-done and '
            'what is next. Then start nothing new -- no new command, ticket or transition -- until you are told work '
            'has resumed.', p_pause.id, p_pause.began_by, left(p_pause.reason, 300), coalesce(p_ticket, 'your ticket'))
        ELSE format(
            'WORK RESUMED (pause %s ended): %s resumed work -- "%s". Carry on with %s from where you recorded you '
            'were.', p_pause.id, p_pause.ended_by, left(coalesce(p_pause.end_reason, ''), 300),
            coalesce(p_ticket, 'your ticket')) END;
$$;

-- Who may set, change or end a pause: the Director, or the User -- the same two SYRD-672 admits to hold a provider
-- account (provider_account_hold_actor) -- refused in words that say so. Returns the caller, which is recorded.
CREATE OR REPLACE FUNCTION ticket_board.work_pause_actor(p_action text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    RETURN ticket_board.provider_account_hold_actor(p_action);
EXCEPTION WHEN insufficient_privilege THEN
    RAISE EXCEPTION 'only the Director or the User may pause or resume work (% was refused: %)', p_action, SQLERRM
        USING ERRCODE = '42501';
END;
$$;

-- Set the pause, or change its mode. The Director or the User, with a reason; repeating the standing mode changes
-- nothing. Returns what stands.
CREATE OR REPLACE FUNCTION ticket_board.pause_work(p_mode text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text;
    cur ticket_board.work_pauses;
    now_at timestamptz := clock_timestamp();
BEGIN
    actor := ticket_board.work_pause_actor('pause_work');
    IF p_mode IS NULL OR p_mode NOT IN ('drain', 'stop') THEN
        RAISE EXCEPTION 'a pause is drain or stop, not %', coalesce(p_mode, 'nothing');
    END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'pausing work needs a reason'; END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.work_pauses'));
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE ended_at IS NULL FOR UPDATE;
    IF cur.id IS NULL THEN
        INSERT INTO ticket_board.work_pauses (mode, began_at, began_by, reason, stop_since)
        VALUES (p_mode, now_at, actor, left(btrim(p_reason), 500), CASE WHEN p_mode = 'stop' THEN now_at END)
        RETURNING * INTO cur;
        PERFORM pg_notify('ticket_board_state_transition',
                          jsonb_build_object('kind', 'work_pause', 'pause', cur.id, 'mode', cur.mode)::text);
        RETURN jsonb_build_object('pause', cur.id, 'began', true, 'changed', true, 'mode', cur.mode, 'by', actor);
    END IF;
    IF cur.mode = p_mode THEN
        RETURN jsonb_build_object('pause', cur.id, 'began', false, 'changed', false, 'mode', cur.mode,
                                  'by', cur.began_by);
    END IF;
    UPDATE ticket_board.work_pauses
       SET mode = p_mode,
           stop_since = CASE WHEN p_mode = 'stop' THEN now_at END,
           changes = changes || jsonb_build_array(jsonb_build_object(
               'at', now_at, 'by', actor, 'from', cur.mode, 'to', p_mode, 'reason', left(btrim(p_reason), 500)))
     WHERE id = cur.id RETURNING * INTO cur;
    PERFORM pg_notify('ticket_board_state_transition',
                      jsonb_build_object('kind', 'work_pause', 'pause', cur.id, 'mode', cur.mode)::text);
    RETURN jsonb_build_object('pause', cur.id, 'began', false, 'changed', true, 'mode', cur.mode, 'by', actor);
END;
$$;

-- End the pause, whichever mode, with who and why. What waited is due now, once each, and asked again before it goes;
-- each role a stop pause interrupted or told is told once that work resumed.
CREATE OR REPLACE FUNCTION ticket_board.resume_work(p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text;
    cur ticket_board.work_pauses;
    r record;
    anchor text;
    told text[] := '{}';
    released integer := 0;
    now_at timestamptz := clock_timestamp();
BEGIN
    actor := ticket_board.work_pause_actor('resume_work');
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'resuming work needs a reason'; END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.work_pauses'));
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE ended_at IS NULL FOR UPDATE;
    IF cur.id IS NULL THEN RAISE EXCEPTION 'work is not paused'; END IF;
    UPDATE ticket_board.work_pauses SET ended_at = now_at, ended_by = actor, end_reason = left(btrim(p_reason), 500)
     WHERE id = cur.id RETURNING * INTO cur;
    UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = now_at
     WHERE q.last_error LIKE 'work_paused%' AND q.claimed_at IS NULL AND q.dead_lettered_at IS NULL;
    GET DIAGNOSTICS released = ROW_COUNT;
    FOR r IN SELECT * FROM ticket_board.work_pause_roles
              WHERE pause_id = cur.id AND (interrupted_at IS NOT NULL OR noticed_at IS NOT NULL)
                AND resumed_notice_at IS NULL
              ORDER BY role LOOP
        -- One interrupted but never told -- its prompt came back to its composer, say -- is told all the same.
        anchor := coalesce(r.notice_ticket, ticket_board.provider_hold_anchor(r.role));
        CONTINUE WHEN anchor IS NULL;
        PERFORM ticket_board.enqueue_notification(
            anchor, 'ticket_update', r.role, ticket_board.work_pause_notice_text('work_resumed', cur, anchor),
            jsonb_build_object('kind', 'work_resumed', 'reason', 'work_resumed', 'pause', cur.id, 'role', r.role,
                               'id', anchor, 'target_role', r.role),
            format('work-resumed:%s:%s', cur.id, r.role));
        UPDATE ticket_board.work_pause_roles SET resumed_notice_at = now_at WHERE pause_id = cur.id AND role = r.role;
        told := told || r.role;
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition',
                      jsonb_build_object('kind', 'work_resumed', 'pause', cur.id)::text);
    RETURN jsonb_build_object('pause', cur.id, 'resumed', true, 'by', actor, 'mode', cur.mode,
                              'released_notices', released, 'told', to_jsonb(told));
END;
$$;

-- The listener interrupted p_role's running turn for this stop: recorded once per turn. False when this pause already
-- interrupted that turn, or the pause no longer stops work.
CREATE OR REPLACE FUNCTION ticket_board.note_work_pause_interrupt(p_pause bigint, p_role text, p_runtime text,
                                                                  p_turn double precision, p_detail jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cur ticket_board.work_pauses; prior ticket_board.work_pause_roles;
BEGIN
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE id = p_pause AND ended_at IS NULL AND mode = 'stop';
    IF cur.id IS NULL OR ticket_board.role_paused(p_role) IS DISTINCT FROM p_pause THEN RETURN false; END IF;
    SELECT * INTO prior FROM ticket_board.work_pause_roles WHERE pause_id = p_pause AND role = p_role FOR UPDATE;
    IF prior.interrupted_at IS NOT NULL AND prior.turn_started_at IS NOT DISTINCT FROM p_turn THEN RETURN false; END IF;
    INSERT INTO ticket_board.work_pause_roles AS w (pause_id, role, runtime, turn_started_at, interrupted_at, interrupt)
    VALUES (p_pause, p_role, coalesce(p_runtime, ''), p_turn, clock_timestamp(), p_detail)
    ON CONFLICT (pause_id, role) DO UPDATE
       SET runtime = EXCLUDED.runtime, turn_started_at = EXCLUDED.turn_started_at,
           interrupted_at = EXCLUDED.interrupted_at, interrupt = EXCLUDED.interrupt,
           settled = NULL, settled_at = NULL;
    RETURN true;
END;
$$;

-- How an interrupted turn ended, as the listener saw it; once. Returns whether this recorded it.
CREATE OR REPLACE FUNCTION ticket_board.note_work_pause_settled(p_pause bigint, p_role text, p_how text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    WITH done AS (
        UPDATE ticket_board.work_pause_roles SET settled = p_how, settled_at = clock_timestamp()
         WHERE pause_id = p_pause AND role = p_role AND interrupted_at IS NOT NULL AND settled IS NULL
        RETURNING 1)
    SELECT EXISTS (SELECT FROM done);
$$;

-- A stop pause's one notice to p_role, recorded before the listener types it: the text, on the ticket its notices
-- hang on -- the work it holds, else what it owns, else its last hand-over. The listener types it itself once the
-- role's interrupted turn is seen to be over (or, for Hermes, as the interrupt), so a background command left running
-- under the pane does not hold it back. NULL when the pause no longer stops work, the role was told already, or it
-- has no ticket to hang it on.
CREATE OR REPLACE FUNCTION ticket_board.note_work_pause_notice(p_pause bigint, p_role text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cur ticket_board.work_pauses; prior ticket_board.work_pause_roles; anchor text;
BEGIN
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE id = p_pause AND ended_at IS NULL AND mode = 'stop';
    IF cur.id IS NULL OR ticket_board.role_paused(p_role) IS DISTINCT FROM p_pause THEN RETURN NULL; END IF;
    SELECT * INTO prior FROM ticket_board.work_pause_roles WHERE pause_id = p_pause AND role = p_role FOR UPDATE;
    IF prior.noticed_at IS NOT NULL THEN RETURN NULL; END IF;
    anchor := ticket_board.provider_hold_anchor(p_role);
    IF anchor IS NULL THEN RETURN NULL; END IF;
    INSERT INTO ticket_board.work_pause_roles AS w (pause_id, role, notice_ticket, noticed_at)
    VALUES (p_pause, p_role, anchor, clock_timestamp())
    ON CONFLICT (pause_id, role) DO UPDATE SET notice_ticket = EXCLUDED.notice_ticket, noticed_at = EXCLUDED.noticed_at;
    RETURN jsonb_build_object('ticket', anchor, 'text', ticket_board.work_pause_notice_text('work_pause', cur, anchor));
END;
$$;

-- Tell the Director, once per role per pause, which background commands a stop left running under that role's
-- pane, with pid and ticket. Nothing is stopped. Returns the notice, or NULL when there was nothing new to say.
CREATE OR REPLACE FUNCTION ticket_board.report_work_pause_background(p_pause bigint, p_role text, p_commands jsonb)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cur ticket_board.work_pauses; prior ticket_board.work_pause_roles; anchor text; listed text; notice bigint;
BEGIN
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE id = p_pause AND ended_at IS NULL AND mode = 'stop';
    IF cur.id IS NULL OR jsonb_typeof(p_commands) IS DISTINCT FROM 'array' OR jsonb_array_length(p_commands) = 0 THEN
        RETURN NULL;
    END IF;
    SELECT * INTO prior FROM ticket_board.work_pause_roles WHERE pause_id = p_pause AND role = p_role FOR UPDATE;
    IF prior.background_reported_at IS NOT NULL THEN RETURN NULL; END IF;
    SELECT string_agg(format('pid %s (%s) on %s', c->>'pid', left(coalesce(c->>'command', '?'), 120),
                             coalesce(nullif(c->>'ticket', ''), 'no ticket')), '; ' ORDER BY (c->>'pid')::bigint)
      INTO listed FROM jsonb_array_elements(p_commands) c;
    anchor := coalesce((SELECT nullif(c->>'ticket', '') FROM jsonb_array_elements(p_commands) c
                         WHERE nullif(c->>'ticket', '') IS NOT NULL LIMIT 1), ticket_board.provider_hold_anchor(p_role));
    IF anchor IS NOT NULL THEN
        notice := ticket_board.enqueue_notification(
            anchor, 'ticket_update', 'director',
            format('Work paused (stop now, pause %s): %s had background commands running, left running -- %s. '
                   'Nothing was killed. To see them: ticket-board-read leases --role %s; to stop one deliberately: '
                   'ticket-board-write interrupt-lease LEASE --reason-file PATH.', cur.id, p_role, listed, p_role),
            jsonb_build_object('kind', 'work_pause_background', 'reason', 'work_pause_background', 'pause', cur.id,
                               'role', p_role, 'id', anchor, 'target_role', 'director', 'commands', p_commands),
            format('work-pause-background:%s:%s', cur.id, p_role));
    END IF;
    INSERT INTO ticket_board.work_pause_roles AS w (pause_id, role, background, background_reported_at)
    VALUES (p_pause, p_role, p_commands, clock_timestamp())
    ON CONFLICT (pause_id, role) DO UPDATE
       SET background = EXCLUDED.background, background_reported_at = EXCLUDED.background_reported_at;
    RETURN notice;
END;
$$;

-- Whether a queued pause notice is still worth sending: a resume notice while its pause is over and no later pause has
-- begun since -- one that waited out a second pause would say work resumed after the wrong one; the Director's
-- background report while its pause stands.
CREATE OR REPLACE FUNCTION ticket_board.work_pause_notice_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE p_payload->>'kind'
        WHEN 'work_resumed' THEN EXISTS (
            SELECT FROM ticket_board.work_pauses p
             WHERE p.id = (p_payload->>'pause')::bigint AND p.ended_at IS NOT NULL)
            AND NOT EXISTS (SELECT FROM ticket_board.work_pauses p WHERE p.id > (p_payload->>'pause')::bigint)
        WHEN 'work_pause_background' THEN EXISTS (
            SELECT FROM ticket_board.work_pauses p
             WHERE p.id = (p_payload->>'pause')::bigint AND p.ended_at IS NULL)
        ELSE true END;
$$;

-- Read-only: the pause standing now -- mode, who set it, why, since when, its changes, each role it holds with what
-- waits for it, and what a stop did to each role -- and the last few ended.
CREATE OR REPLACE FUNCTION ticket_board.work_pause_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'paused', EXISTS (SELECT FROM ticket_board.work_pauses WHERE ended_at IS NULL),
        'pause', (SELECT jsonb_build_object(
                     'id', p.id, 'mode', p.mode, 'began_at', p.began_at, 'began_by', p.began_by, 'reason', p.reason,
                     'stop_since', p.stop_since, 'changes', p.changes,
                     'held', coalesce((SELECT jsonb_object_agg(h.role, h.waiting) FROM (
                                SELECT q.target_role AS role, count(*) AS waiting
                                  FROM ticket_board.ticket_notification_queue q
                                 WHERE q.dead_lettered_at IS NULL AND ticket_board.role_paused(q.target_role) = p.id
                                 GROUP BY q.target_role) h), '{}'::jsonb),
                     'roles', coalesce((SELECT jsonb_agg(jsonb_build_object(
                                'role', w.role, 'runtime', w.runtime, 'interrupted_at', w.interrupted_at,
                                'settled', w.settled, 'settled_at', w.settled_at, 'noticed_at', w.noticed_at,
                                'notice_ticket', w.notice_ticket, 'background', w.background) ORDER BY w.role)
                              FROM ticket_board.work_pause_roles w WHERE w.pause_id = p.id), '[]'::jsonb))
                   FROM ticket_board.work_pauses p WHERE p.ended_at IS NULL),
        'recent', coalesce((SELECT jsonb_agg(r ORDER BY (r->>'id')::bigint DESC) FROM (
                     SELECT jsonb_build_object('id', p.id, 'mode', p.mode, 'began_at', p.began_at,
                                               'began_by', p.began_by, 'reason', p.reason, 'ended_at', p.ended_at,
                                               'ended_by', p.ended_by, 'end_reason', p.end_reason,
                                               'changes', p.changes) AS r
                       FROM ticket_board.work_pauses p WHERE p.ended_at IS NOT NULL
                      ORDER BY p.id DESC LIMIT 5) recent), '[]'::jsonb));
$$;

-- SYRD-644's first question, as SYRD-672 left it, also says held for a role the pause holds: its reminders,
-- unresolved-turn prompts, stall episodes, board-stall runnability, review balance and pull pickup leave it alone.
-- role_provider_hold_until is unchanged: a paused role's deliveries are held by the pause's own check, asked first
-- (work_pause.hold_delivery), which lets the pause's own notices through.
CREATE OR REPLACE FUNCTION ticket_board.role_provider_hold_is_current(p_role text, p_now timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT ticket_board.role_paused(p_role) IS NOT NULL OR ticket_board.role_account_hold(p_role) IS NOT NULL OR EXISTS (
        SELECT FROM ticket_board.provider_quota_holds h
        JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
        WHERE h.role = p_role
          AND ((h.state = 'held' AND h.deadline > p_now)
               OR (h.state = 'released' AND h.released_at > p_now - interval '10 minutes')));
$$;
REVOKE ALL ON ticket_board.work_pauses FROM PUBLIC;
REVOKE ALL ON ticket_board.work_pause_roles FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.work_pauses_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.current_work_pause() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.role_paused(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.unpaused_since(timestamptz, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.work_pause_actor(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.work_pause_notice_text(text, ticket_board.work_pauses, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.pause_work(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.resume_work(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_work_pause_interrupt(bigint, text, text, double precision, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_work_pause_settled(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_work_pause_notice(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.report_work_pause_background(bigint, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.work_pause_notice_is_current(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.work_pause_status() FROM PUBLIC;
GRANT SELECT ON ticket_board.work_pauses TO ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.work_pause_roles TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.current_work_pause() TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_paused(text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.unpaused_since(timestamptz, timestamptz) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.pause_work(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.resume_work(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.note_work_pause_interrupt(bigint, text, text, double precision, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_work_pause_settled(bigint, text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_work_pause_notice(bigint, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.report_work_pause_background(bigint, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.work_pause_notice_is_current(jsonb) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.work_pause_status() TO ticket_board_service, ticket_board_listener;

COMMIT;
