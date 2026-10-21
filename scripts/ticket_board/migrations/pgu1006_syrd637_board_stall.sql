-- SYRD-637: the Director is told, once, when runnable board work has made no
-- durable progress for a while. schema.sql carries the same tables and
-- functions; rbac.sql the same privileges.
BEGIN;

-- Durable progress, one row per event, appended by triggers on the board's own
-- mutations of admitted work (board_stall_note_progress): a ticket's stage,
-- owner, hold or candidate changing, a blocker or a dependency changing, a
-- hand-over delivered, the owner's own comment. Append-only, so recording
-- progress never waits on another writer; the evaluator reads the latest and
-- trims what it no longer needs. Heartbeats, retries, nudges and reminders
-- write nothing here.
CREATE TABLE IF NOT EXISTS ticket_board.board_progress_events (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ticket_id text,
    event text NOT NULL CHECK (btrim(event) <> '')
);

-- The evaluator's own state, one row: when it first saw runnable work in the
-- current run of it, what it saw on its last pass and under which threshold.
-- Only the listener's evaluator writes it, under FOR UPDATE.
CREATE TABLE IF NOT EXISTS ticket_board.board_stall_state (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    runnable_since timestamptz,
    evaluated_at timestamptz,
    threshold_seconds integer NOT NULL DEFAULT 1800 CHECK (threshold_seconds BETWEEN 60 AND 86400),
    runnable jsonb NOT NULL DEFAULT '[]'::jsonb,
    excluded jsonb NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO ticket_board.board_stall_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING;

-- One row per stall episode: opened, with its one Director digest, after the
-- threshold passed with runnable work and no progress; closed by progress or
-- when no runnable work remains. At most one is open.
CREATE TABLE IF NOT EXISTS ticket_board.board_stall_episodes (
    id bigserial PRIMARY KEY,
    opened_at timestamptz NOT NULL,
    clock_started_at timestamptz NOT NULL,
    -- The latest progress event when it opened: a later one closes it.
    progress_event_id bigint,
    last_event text NOT NULL,
    last_event_at timestamptz,
    threshold_seconds integer NOT NULL,
    tickets jsonb NOT NULL,
    anchor_ticket text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    notification_id bigint,
    closed_at timestamptz,
    close_reason text CHECK (close_reason IS NULL OR close_reason IN ('progress', 'no_runnable_work'))
);
CREATE UNIQUE INDEX IF NOT EXISTS board_stall_episodes_open
    ON ticket_board.board_stall_episodes ((true)) WHERE closed_at IS NULL;

-- Whether work in `stage` is admitted, active workflow work, by the declared
-- stage's shape and never its name: not terminal (Done, Cancelled), not a
-- draft (Draft), not a parking stage (Backlog), and not a Director hold --
-- non-review and owned by the Director alone (Triage/Analysis and the
-- Director's other holding stages). A pull policy's ready stage counts:
-- declared_parking_stage already tells it apart from parking. A board with no
-- declared workflow counts nothing.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_stage_counts(p_stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT coalesce((
        SELECT NOT coalesce((s->>'terminal')::boolean, false) AND s->>'kind' <> 'draft'
               AND NOT ticket_board.declared_parking_stage(p_stage)
               AND NOT ticket_board.declared_director_hold(p_stage)
          FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s
         WHERE s->>'name' = p_stage), false);
$$;

CREATE OR REPLACE FUNCTION ticket_board.board_stall_note_progress(p_ticket text, p_stage text, p_event text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF ticket_board.board_stall_stage_counts(p_stage) THEN
        INSERT INTO ticket_board.board_progress_events (ticket_id, event) VALUES (p_ticket, left(p_event, 300));
    END IF;
END;
$$;

-- A ticket's stage, owner, hold or candidate changed, or it was created in
-- admitted work: progress, when either side of it is admitted work.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_ticket_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE what text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM ticket_board.board_stall_note_progress(NEW.id, NEW.state, format('%s created in %s', NEW.id, NEW.state));
        RETURN NULL;
    END IF;
    what := CASE
        WHEN OLD.state IS DISTINCT FROM NEW.state THEN format('%s moved %s -> %s', NEW.id, OLD.state, NEW.state)
        WHEN OLD.assignee IS DISTINCT FROM NEW.assignee THEN format('%s assigned to %s', NEW.id, NEW.assignee)
        WHEN OLD.manually_controlled IS DISTINCT FROM NEW.manually_controlled THEN
            format('%s %s', NEW.id, CASE WHEN NEW.manually_controlled THEN 'held' ELSE 'released from hold' END)
        WHEN OLD.parked IS DISTINCT FROM NEW.parked THEN format('%s %s', NEW.id, CASE WHEN NEW.parked THEN 'parked' ELSE 'unparked' END)
        WHEN OLD.commit_hash IS DISTINCT FROM NEW.commit_hash THEN format('%s candidate %s', NEW.id, left(NEW.commit_hash, 12))
    END;
    IF what IS NOT NULL THEN
        PERFORM ticket_board.board_stall_note_progress(NEW.id, OLD.state, what);
        IF NEW.state IS DISTINCT FROM OLD.state THEN
            PERFORM ticket_board.board_stall_note_progress(NEW.id, NEW.state, what);
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_zz_board_stall_progress ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_board_stall_progress AFTER INSERT OR UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.board_stall_ticket_progress();

CREATE OR REPLACE FUNCTION ticket_board.board_stall_blocker_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE subject text := CASE WHEN TG_OP = 'DELETE' THEN OLD.ticket_id ELSE NEW.ticket_id END;
    blocker text := CASE WHEN TG_OP = 'DELETE' THEN OLD.blocker_ticket_id ELSE NEW.blocker_ticket_id END;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.resolved IS NOT DISTINCT FROM NEW.resolved THEN
        RETURN NULL;
    END IF;
    PERFORM ticket_board.board_stall_note_progress(subject, (SELECT state FROM ticket_board.tickets WHERE id = subject),
        format('%s blocker %s %s', subject, blocker,
               CASE TG_OP WHEN 'INSERT' THEN 'set' WHEN 'DELETE' THEN 'removed'
                          ELSE CASE WHEN NEW.resolved THEN 'resolved' ELSE 'reopened' END END));
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_blockers_board_stall_progress ON ticket_board.ticket_blockers;
CREATE TRIGGER ticket_blockers_board_stall_progress AFTER INSERT OR UPDATE OR DELETE ON ticket_board.ticket_blockers
    FOR EACH ROW EXECUTE FUNCTION ticket_board.board_stall_blocker_progress();

CREATE OR REPLACE FUNCTION ticket_board.board_stall_dependency_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF OLD.awaiting_role IS DISTINCT FROM NEW.awaiting_role THEN
        PERFORM ticket_board.board_stall_note_progress(NEW.ticket_id,
            (SELECT state FROM ticket_board.tickets WHERE id = NEW.ticket_id),
            format('%s %s', NEW.ticket_id, CASE WHEN nullif(NEW.awaiting_role, '') IS NULL THEN 'dependency cleared'
                                                ELSE 'waits on ' || NEW.awaiting_role END));
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_notification_state_board_stall_progress ON ticket_board.ticket_notification_state;
CREATE TRIGGER ticket_notification_state_board_stall_progress AFTER UPDATE ON ticket_board.ticket_notification_state
    FOR EACH ROW EXECUTE FUNCTION ticket_board.board_stall_dependency_progress();

-- A hand-over delivered: a transition, a dependency or a publication notice
-- acknowledged by the listener. A nudge, reminder or alert delivered is not.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_delivery_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF NEW.event = 'ack' AND NEW.kind IN ('transition', 'awaiting_role', 'publication') AND NEW.ticket_id IS NOT NULL THEN
        PERFORM ticket_board.board_stall_note_progress(NEW.ticket_id,
            (SELECT state FROM ticket_board.tickets WHERE id = NEW.ticket_id),
            format('%s %s notice delivered to %s', NEW.ticket_id, NEW.kind, coalesce(NEW.target_role, 'its owner')));
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS notification_trace_board_stall_progress ON ticket_board.notification_trace;
CREATE TRIGGER notification_trace_board_stall_progress AFTER INSERT ON ticket_board.notification_trace
    FOR EACH ROW EXECUTE FUNCTION ticket_board.board_stall_delivery_progress();

-- The ticket's own owner writing on it: an explicit checkpoint.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_comment_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE t ticket_board.tickets;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = NEW.ticket_id;
    IF FOUND AND NEW.who = t.assignee THEN
        PERFORM ticket_board.board_stall_note_progress(t.id, t.state, format('%s checkpoint by %s', t.id, NEW.who));
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_comments_board_stall_progress ON ticket_board.ticket_comments;
CREATE TRIGGER ticket_comments_board_stall_progress AFTER INSERT ON ticket_board.ticket_comments
    FOR EACH ROW EXECUTE FUNCTION ticket_board.board_stall_comment_progress();

-- Every ticket that is runnable now, with who is responsible and why each
-- other admitted ticket is not. Runnable now: in admitted, active work
-- (board_stall_stage_counts); not held or parked; no unresolved blocker,
-- internal or external; no active dependency; not reminder-snoozed; not
-- queued behind a reservation; not waiting behind its role's current ticket
-- (finish_current_stage_blocker: an implementer's serial slot, a reviewer's
-- lane); its next action not the Director's own nor the User's; and not in
-- its own open stall episode (SYRD-595), which already has one recipient. In a
-- pull policy's ready stage the claimants are responsible, and only while one
-- of them could claim it. Listener-only: the lane check is.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_runnable(p_now timestamptz)
RETURNS TABLE (ticket_id text, state text, role text, why_not text, waiting_since timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); policy jsonb := ticket_board.declared_scheduling();
    r record; owners text[]; responsible text; why text; kinds jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('board_stall_runnable');
    SELECT coalesce(jsonb_object_agg(x->>'name', x->>'kind'), '{}'::jsonb) INTO kinds
      FROM jsonb_array_elements(coalesce(cfg->'roles', '[]'::jsonb)) x;
    FOR r IN
        SELECT t.*, ns.entered_current_state_at, ns.current_assignment_at, ns.awaiting_role, ns.awaiting_since_at
          FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE ticket_board.board_stall_stage_counts(t.state)
         ORDER BY ns.entered_current_state_at, t.ticket_number
    LOOP
        why := NULL;
        responsible := NULL;
        SELECT ARRAY(SELECT jsonb_array_elements_text(x->'owners')) INTO owners
          FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = r.state;
        IF policy IS NOT NULL AND r.state = policy->>'ready_stage' THEN
            SELECT string_agg(c, ', ' ORDER BY c) INTO responsible
              FROM ticket_board.pull_claimant_pool(cfg) c WHERE ticket_board.ticket_ready_for_pull(r.id, c);
            IF responsible IS NULL THEN why := 'no claimant may claim it'; END IF;
        ELSIF r.assignee = ANY(owners) THEN
            responsible := r.assignee;
        ELSE
            why := 'nobody who owns its stage holds it';
        END IF;
        IF why IS NULL THEN
            why := CASE
                WHEN responsible = 'director' THEN 'its next action is the Director''s'
                WHEN kinds->>responsible = 'user' THEN 'its next action is the User''s'
                WHEN r.manually_controlled THEN 'held by the Director'
                WHEN r.parked THEN 'parked'
                WHEN ticket_board.ticket_has_unresolved_blockers(r.id) THEN
                    'blocked by ' || ticket_board.unresolved_blocker_list(r.id)
                WHEN ticket_board.ticket_awaiting_role_is_active(r.awaiting_role, r.awaiting_since_at, p_now) THEN
                    'waiting on ' || r.awaiting_role
                WHEN ticket_board.ticket_reminders_snoozed(r.id, p_now) THEN 'its reminders are snoozed'
                WHEN btrim(coalesce(r.queued_for_assignee, '')) <> '' THEN
                    'queued for ' || r.queued_for_assignee || ' behind ' || coalesce(nullif(r.queued_behind_ticket, ''), 'its work')
                -- SYRD-595's episode is this ticket's one recipient: its owner while being
                -- prompted, the Director once escalated. Never both about one stall.
                WHEN EXISTS (SELECT FROM ticket_board.role_stall_episodes e
                              WHERE e.ticket_id = r.id AND e.closed_at IS NULL AND e.phase = 'director') THEN
                    'its own stall is already with the Director'
                WHEN EXISTS (SELECT FROM ticket_board.role_stall_episodes e
                              WHERE e.ticket_id = r.id AND e.closed_at IS NULL) THEN
                    'its owner is being prompted about its own stall'
            END;
        END IF;
        IF why IS NULL AND NOT (policy IS NOT NULL AND r.state = policy->>'ready_stage') THEN
            why := nullif(ticket_board.finish_current_stage_blocker(r.id, responsible, r.state, p_now), '');
            IF why IS NOT NULL THEN
                why := 'waiting behind ' || responsible || '''s current ' || why;
            END IF;
        END IF;
        ticket_id := r.id; state := r.state; role := responsible; why_not := why;
        waiting_since := greatest(r.entered_current_state_at, r.current_assignment_at);
        RETURN NEXT;
    END LOOP;
END;
$$;

-- What the listener sees of one role, and what the board holds about it: the
-- evidence a digest gives for each responsible role, never a reason to stay
-- quiet. `p_roles` maps a role to {"state": ..., "detail": ...} as the
-- listener judged it this pass.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_role_evidence(p_role text, p_roles jsonb)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT concat_ws('; ',
        CASE WHEN p_role IS NULL THEN NULL
             WHEN p_roles ? p_role THEN nullif(concat_ws(': ', p_roles->p_role->>'state', nullif(p_roles->p_role->>'detail', '')), '')
             ELSE 'not reported by the listener' END,
        (SELECT 'provider at capacity since ' || to_char(e.opened_at AT TIME ZONE 'UTC', 'HH24:MI"Z"')
           FROM ticket_board.provider_capacity_episodes e WHERE e.target_role = p_role AND e.closed_at IS NULL LIMIT 1),
        (SELECT 'pane blocked (' || e.classification || ') since ' || to_char(e.blocked_since AT TIME ZONE 'UTC', 'HH24:MI"Z"')
           FROM ticket_board.blocked_pane_episodes e WHERE e.target_role = p_role AND e.cleared_at IS NULL LIMIT 1));
$$;

-- Each role responsible for a runnable ticket, with its own evidence: one role,
-- or every claimant that may claim ready work -- never folded into one name,
-- so a missing, stopped, busy or capacity-blocked claimant is told apart from
-- an eligible one.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_responsible(p_roles_text text, p_roles jsonb)
RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('role', r, 'evidence', ticket_board.board_stall_role_evidence(r, p_roles))
                              ORDER BY ordinality), '[]'::jsonb)
      FROM unnest(string_to_array(p_roles_text, ', ')) WITH ORDINALITY AS u(r, ordinality)
     WHERE btrim(coalesce(r, '')) <> '';
$$;

-- The evaluator, run on every listener pass. Opens an episode, and enqueues
-- its one Director digest in the same transaction, once runnable work has
-- existed with no durable progress for the threshold; closes it on progress,
-- or when no runnable work remains. Restart-safe and exactly-once: its state
-- is these rows, taken FOR UPDATE, the digest's dedupe key is the episode's,
-- and an open episode is never notified again. Nothing here assigns, moves or
-- notifies anybody but the Director.
CREATE OR REPLACE FUNCTION ticket_board.note_board_stall(
    p_roles jsonb, p_threshold_seconds integer, p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE st ticket_board.board_stall_state; ep ticket_board.board_stall_episodes; v_last ticket_board.board_progress_events;
    v_runnable jsonb; v_excluded jsonb; v_clock timestamptz; anchor text; message text; queued bigint; lines text;
    threshold integer := least(greatest(coalesce(p_threshold_seconds, 1800), 60), 86400);
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
               runnable = v_runnable, excluded = v_excluded WHERE id;
        RETURN jsonb_build_object('runnable', 0, 'episode', NULL);
    END IF;
    UPDATE ticket_board.board_stall_state SET runnable_since = coalesce(runnable_since, p_now), evaluated_at = p_now,
           threshold_seconds = threshold, runnable = v_runnable, excluded = v_excluded WHERE id RETURNING * INTO st;
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

-- Whether a board-stall digest may still be delivered: its episode is still open.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_notice_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.board_stall_episodes e
                    WHERE e.id = (p_payload->>'episode')::bigint AND e.closed_at IS NULL);
$$;

-- The stall state, read-only, as the last evaluation left it: what is runnable
-- and why the rest is not, the clock, the last progress, when it would fire,
-- and the open and recent episodes. So the Director can see why an alert did
-- or did not fire.
CREATE OR REPLACE FUNCTION ticket_board.board_stall_status(p_now timestamptz DEFAULT clock_timestamp())
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    WITH st AS (SELECT * FROM ticket_board.board_stall_state WHERE id),
         last AS (SELECT * FROM ticket_board.board_progress_events ORDER BY id DESC LIMIT 1),
         clock AS (SELECT CASE WHEN st.runnable_since IS NULL THEN NULL
                               ELSE greatest(st.runnable_since, coalesce((SELECT at FROM last), st.runnable_since)) END AS at
                     FROM st)
    SELECT jsonb_build_object(
        'evaluated_at', st.evaluated_at, 'threshold_seconds', st.threshold_seconds,
        'runnable', st.runnable, 'excluded', st.excluded, 'runnable_since', st.runnable_since,
        'clock_started_at', clock.at,
        'fires_at', CASE WHEN clock.at IS NULL THEN NULL ELSE clock.at + make_interval(secs => st.threshold_seconds) END,
        'last_progress', (SELECT jsonb_build_object('at', at, 'event', event, 'ticket', ticket_id) FROM last),
        'open_episode', (SELECT to_jsonb(e) FROM ticket_board.board_stall_episodes e WHERE e.closed_at IS NULL),
        'recent_episodes', (SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.id DESC), '[]'::jsonb)
                              FROM (SELECT * FROM ticket_board.board_stall_episodes ORDER BY id DESC LIMIT 5) e))
      FROM st, clock;
$$;

REVOKE ALL ON ticket_board.board_progress_events, ticket_board.board_stall_state, ticket_board.board_stall_episodes FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.board_progress_events_id_seq, ticket_board.board_stall_episodes_id_seq FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_stage_counts(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_note_progress(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_ticket_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_blocker_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_dependency_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_delivery_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_comment_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_runnable(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_role_evidence(text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_responsible(text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_board_stall(jsonb, integer, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_notice_is_current(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.board_stall_status(timestamptz) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.board_progress_events, ticket_board.board_stall_state, ticket_board.board_stall_episodes TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_board_stall(jsonb, integer, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.board_stall_runnable(timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.board_stall_notice_is_current(jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.board_stall_status(timestamptz) TO ticket_board_service, ticket_board_listener;

COMMIT;
