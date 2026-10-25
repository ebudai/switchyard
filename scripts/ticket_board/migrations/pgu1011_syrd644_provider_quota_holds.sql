-- SYRD-644: a bounded provider-quota hold per role.
--
-- When a role's provider refuses it for an exhausted usage quota, the listener
-- records the provider's own evidence -- Claude Code's StopFailure hook with the
-- transcript's rejected quotaLimits, or the quota line that ends a Codex screen
-- after a turn began -- tied to the role's registered process, generation,
-- provider session and the turn it answered. The hold lasts until the
-- provider's reset plus a grace (or a bounded recheck when it gives none);
-- meanwhile ticket_turn_is_resolved and the listener's delivery check hold back
-- the role's deliveries and nudges and the Director's alerts about it. The
-- Director is told once when it begins. At its deadline it is released first,
-- and one fresh attempt follows; only evidence newer than that release renews
-- it. A new generation, process or session, a normal turn end, or an operator
-- clear ends it at once. Tickets, reservations and context are never touched.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.provider_quota_holds (
    id bigserial PRIMARY KEY,
    role text NOT NULL,
    runtime text NOT NULL,
    -- The registration the evidence came from: a new generation, a new
    -- process or a new provider session is not the account that was held.
    generation bigint NOT NULL,
    pid integer,
    process_start bigint,
    provider_session text NOT NULL DEFAULT '',
    target text NOT NULL DEFAULT '',
    state text NOT NULL CHECK (state IN ('held', 'released', 'ended')),
    -- What the provider said, when, and the turn or hook event it answered.
    evidence jsonb NOT NULL,
    evidence_at timestamptz NOT NULL,
    evidence_turn text NOT NULL DEFAULT '',
    reset_at timestamptz,
    deadline timestamptz NOT NULL,
    began_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    renewals integer NOT NULL DEFAULT 0,
    released_at timestamptz,
    ended_at timestamptz,
    end_reason text,
    notified boolean NOT NULL DEFAULT false,
    joined_hold bigint,
    history jsonb NOT NULL DEFAULT '[]'::jsonb
);
-- One current hold per role: held, or released and awaiting its one attempt.
CREATE UNIQUE INDEX IF NOT EXISTS provider_quota_holds_current ON ticket_board.provider_quota_holds (role)
    WHERE state IN ('held', 'released');

-- The one question the reminders and escalations ask: is this role's provider
-- held now? Only a hold on the role's current registration counts. A released
-- hold still counts for its one attempt's window -- until that attempt renews
-- or ends it, and never more than ten minutes: a stall clock that ran through
-- the hold must not fire at the very moment the attempt goes. Deliveries ask
-- role_provider_hold_until, which a release ends at once.
CREATE OR REPLACE FUNCTION ticket_board.role_provider_hold_is_current(p_role text, p_now timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM ticket_board.provider_quota_holds h
        JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
        WHERE h.role = p_role
          AND ((h.state = 'held' AND h.deadline > p_now)
               OR (h.state = 'released' AND h.released_at > p_now - interval '10 minutes')));
$$;

-- The current hold's deadline, for a delivery to wait until; NULL when none.
CREATE OR REPLACE FUNCTION ticket_board.role_provider_hold_until(p_role text, p_now timestamptz)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT h.deadline FROM ticket_board.provider_quota_holds h
    JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
    WHERE h.role = p_role AND h.state = 'held' AND h.deadline > p_now;
$$;


CREATE OR REPLACE FUNCTION ticket_board.ticket_turn_is_resolved(
    p_ticket text,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT
        t.manually_controlled
        OR t.state IS NULL
        OR ticket_board.transition_target_role(t.state, t.assignee) IS NULL
        OR (CASE WHEN ticket_board.declared_workflow() IS NULL
                 THEN t.state IN ('done', 'cancelled')
                 ELSE coalesce((SELECT is_terminal FROM ticket_board.workflow_stages WHERE name = t.state), false)
            END)
        OR ticket_board.ticket_awaiting_role_is_active(ns.awaiting_role, ns.awaiting_since_at, p_now)
        -- SYRD-537: the Director snoozed this ticket's optional reminders, and it is
        -- still the ticket that was snoozed. Neither the prompt nor its escalation.
        OR ticket_board.ticket_reminders_snoozed(t.id, p_now)
        -- SYRD-644: its owner's provider is held for quota. Nothing about this
        -- turn is news until the hold ends, then one fresh attempt decides.
        OR ticket_board.role_provider_hold_is_current(ticket_board.transition_target_role(t.state, t.assignee), p_now)
        OR ticket_board.ticket_serial_focus_reservation_is_current(
               t.id, t.queued_for_assignee, t.queued_behind_ticket)
        -- SYRD-513: not yet handed to its owner. While the owner's transition
        -- notice for this stage is still queued -- held by serial review
        -- admission behind the ticket the owner is working on, or waiting for
        -- a busy pane -- the owner has never been told about this ticket, so
        -- no turn they end is about it. A dead-lettered notice is the failure
        -- path's to report, not this one's.
        OR EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = t.id
              AND q.kind = 'transition'
              AND q.target_role = ticket_board.transition_target_role(t.state, t.assignee)
              AND q.payload ->> 'new_state' = t.state
              AND q.dead_lettered_at IS NULL
        )
        OR EXISTS (
            SELECT 1
            FROM ticket_board.ticket_blockers tb
            LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
            WHERE tb.ticket_id = t.id
              AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
        )
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id = p_ticket;
$$;

-- Whether a ticket's stage is terminal, as ticket_turn_is_resolved reads it.
CREATE OR REPLACE FUNCTION ticket_board.ticket_turn_terminal(p_ticket text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('done', 'cancelled')
                                 ELSE coalesce((SELECT is_terminal FROM ticket_board.workflow_stages WHERE name = t.state), false) END
                     FROM ticket_board.tickets t WHERE t.id = p_ticket), true);
$$;

-- The ticket a notice about a role hangs on: the work it holds, else what it
-- owns, else its last hand-over. NULL when it has none of them.
CREATE OR REPLACE FUNCTION ticket_board.provider_hold_anchor(p_role text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(
        ticket_board.ticket_current_reserved_ticket(p_role),
        (SELECT t.id FROM ticket_board.tickets t
         WHERE ticket_board.transition_target_role(t.state, t.assignee) = p_role
           AND NOT ticket_board.ticket_turn_terminal(t.id)
         ORDER BY t.updated_at DESC NULLS LAST LIMIT 1),
        (SELECT tr.ticket_id FROM ticket_board.notification_trace tr
         WHERE tr.target_role = p_role AND tr.kind = 'transition' AND tr.event = 'send' AND tr.ticket_id IS NOT NULL
         ORDER BY tr.id DESC LIMIT 1));
$$;

-- The ticket's owner, when that role's provider is held now: whom a Director
-- alert about the ticket is about, for the listener, which may not resolve an
-- owner itself.
CREATE OR REPLACE FUNCTION ticket_board.provider_held_ticket_owner(p_ticket text, p_now timestamptz)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT o.role FROM (SELECT ticket_board.transition_target_role(t.state, t.assignee) AS role
                        FROM ticket_board.tickets t WHERE t.id = p_ticket) o
    WHERE ticket_board.role_provider_hold_until(o.role, p_now) IS NOT NULL;
$$;

-- What a hold that stops applying leaves behind: its waiting notices due now,
-- and its row ended with the reason, kept for the record.
CREATE OR REPLACE FUNCTION ticket_board.end_provider_hold(p_id bigint, p_reason text, p_actor text DEFAULT 'listener')
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE hold ticket_board.provider_quota_holds;
BEGIN
    UPDATE ticket_board.provider_quota_holds h
    SET state = 'ended', ended_at = clock_timestamp(), end_reason = p_reason,
        history = h.history || jsonb_build_array(jsonb_build_object('at', clock_timestamp(), 'event', 'ended',
                                                                    'reason', p_reason, 'by', p_actor))
    WHERE h.id = p_id AND h.state IN ('held', 'released')
    RETURNING * INTO hold;
    IF hold.id IS NOT NULL THEN
        UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = clock_timestamp()
        WHERE q.target_role = hold.role AND q.last_error LIKE 'provider_quota_hold%' AND q.claimed_at IS NULL;
    END IF;
END;
$$;

-- The listener's evidence that a role's provider refused it for quota. Begins,
-- repeats or renews the role's hold; ignores evidence that is not from the
-- registered process (its pid and start time: a reused pid is another
-- process), or not newer than what the hold already used: a quota
-- message already there before the last attempt cannot renew it.
CREATE OR REPLACE FUNCTION ticket_board.note_provider_quota(p_role text, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    reg ticket_board.role_runtime_assignments;
    cur ticket_board.provider_quota_holds;
    last_end timestamptz;
    v_observed timestamptz := (p_evidence->>'observed_at')::timestamptz;
    v_turn text := coalesce(p_evidence->>'turn', '');
    v_reset timestamptz := nullif(p_evidence->>'reset_at', '')::timestamptz;
    grace interval := make_interval(secs => least(greatest(coalesce((p_evidence->>'grace_seconds')::numeric, 120), 0), 3600));
    recheck interval := make_interval(secs => least(greatest(coalesce((p_evidence->>'recheck_seconds')::numeric, 1800), 1), 86400));
    v_deadline timestamptz;
    v_evidence jsonb;
    sibling ticket_board.provider_quota_holds;
    anchor text;
    new_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_provider_quota');
    IF v_observed IS NULL THEN RAISE EXCEPTION 'quota evidence needs the time it was observed'; END IF;
    SELECT * INTO reg FROM ticket_board.role_runtime_assignments a WHERE a.role = p_role;
    IF reg.role IS NULL OR reg.runtime IS DISTINCT FROM p_evidence->>'runtime'
       OR reg.generation IS DISTINCT FROM (p_evidence->>'generation')::bigint
       OR reg.process_pid IS DISTINCT FROM (p_evidence->>'pid')::integer
       OR reg.process_start_time IS DISTINCT FROM (p_evidence->>'start_time')::bigint THEN
        RETURN jsonb_build_object('ignored', 'not from the registered provider process');
    END IF;
    -- A reset already past is no reset: the deadline is a recheck from now.
    v_deadline := CASE WHEN v_reset IS NOT NULL AND v_reset + grace > clock_timestamp() THEN v_reset + grace
                     ELSE greatest(v_observed, clock_timestamp()) + recheck END;
    v_evidence := jsonb_build_object('source', p_evidence->>'source', 'observed_at', v_observed, 'turn', v_turn,
                                   'limit', coalesce(p_evidence->>'limit', ''),
                                   'message', left(coalesce(p_evidence->>'message', ''), 240),
                                   'reset_given', v_reset IS NOT NULL)
                  || CASE WHEN p_evidence ? 'count' THEN jsonb_build_object('count', (p_evidence->>'count')::int)
                          ELSE '{}'::jsonb END;
    SELECT * INTO cur FROM ticket_board.provider_quota_holds h
    WHERE h.role = p_role AND h.state IN ('held', 'released') FOR UPDATE;
    IF cur.id IS NOT NULL AND (cur.generation <> reg.generation OR cur.pid IS DISTINCT FROM reg.process_pid
                               OR cur.process_start IS DISTINCT FROM reg.process_start_time) THEN
        PERFORM ticket_board.end_provider_hold(cur.id, 'replaced');
        cur := NULL;
    END IF;
    -- Past its deadline it is released, whether or not a pass has said so yet:
    -- the attempt that follows may already have gone, and its answer is new.
    IF cur.id IS NOT NULL AND cur.state = 'held' AND cur.deadline <= clock_timestamp() THEN
        PERFORM ticket_board.release_provider_hold(cur.id, cur.deadline);
        cur.state := 'released';
        cur.released_at := cur.deadline;
    END IF;
    IF cur.id IS NOT NULL THEN
        -- Each turn is named by its evidence time, so newer is a new turn. A
        -- screen's evidence must also have grown: one more quota line than the
        -- hold last saw, since a turn id can move before the screen is redrawn.
        IF v_observed <= cur.evidence_at
           OR (p_evidence ? 'count' AND cur.evidence ? 'count'
               AND (p_evidence->>'count')::int <= (cur.evidence->>'count')::int)
           OR (cur.state = 'released' AND v_observed <= cur.released_at) THEN
            RETURN jsonb_build_object('hold', cur.id, 'stale', true);
        END IF;
        UPDATE ticket_board.provider_quota_holds h
        SET state = 'held', evidence = v_evidence, evidence_at = v_observed, evidence_turn = v_turn,
            reset_at = v_reset, deadline = v_deadline,
            provider_session = coalesce(nullif(p_evidence->>'session', ''), h.provider_session),
            renewals = h.renewals + CASE WHEN cur.state = 'released' THEN 1 ELSE 0 END,
            history = h.history || jsonb_build_array(jsonb_build_object(
                'at', clock_timestamp(), 'event', CASE WHEN cur.state = 'released' THEN 'renewed' ELSE 'repeated' END,
                'observed_at', v_observed, 'turn', v_turn, 'deadline', v_deadline))
        WHERE h.id = cur.id;
        RETURN jsonb_build_object('hold', cur.id, CASE WHEN cur.state = 'released' THEN 'renewed' ELSE 'repeated' END, true);
    END IF;
    -- Nothing older than the role's last ended hold: evidence from before an
    -- invalidation or a clear is what that ending already answered.
    SELECT max(h.ended_at) INTO last_end FROM ticket_board.provider_quota_holds h WHERE h.role = p_role;
    IF last_end IS NOT NULL AND v_observed <= last_end THEN
        RETURN jsonb_build_object('stale', true, 'reason', 'older than the role''s last ended hold');
    END IF;
    -- Siblings on one account: a hold on the same runtime and reset window is
    -- already the Director's news.
    SELECT * INTO sibling FROM ticket_board.provider_quota_holds h
    WHERE h.state = 'held' AND h.runtime = reg.runtime AND h.role <> p_role AND h.notified
      AND (h.reset_at = v_reset OR (h.reset_at IS NULL AND v_reset IS NULL AND abs(extract(epoch FROM h.deadline - v_deadline)) < 600))
    ORDER BY h.id LIMIT 1;
    INSERT INTO ticket_board.provider_quota_holds (role, runtime, generation, pid, process_start, provider_session, target,
                                                   state, evidence, evidence_at, evidence_turn, reset_at, deadline,
                                                   joined_hold, history)
    VALUES (p_role, reg.runtime, reg.generation, reg.process_pid, reg.process_start_time,
            coalesce(p_evidence->>'session', ''), coalesce(p_evidence->>'target', reg.actual_target, ''), 'held',
            v_evidence, v_observed, v_turn, v_reset, v_deadline, sibling.id,
            jsonb_build_array(jsonb_build_object('at', clock_timestamp(), 'event', 'began', 'observed_at', v_observed,
                                                 'turn', v_turn, 'deadline', v_deadline)))
    RETURNING id INTO new_id;
    anchor := ticket_board.provider_hold_anchor(p_role);
    IF sibling.id IS NULL AND anchor IS NOT NULL THEN
        PERFORM ticket_board.enqueue_notification(
            anchor, 'escalation', 'director',
            format('%s''s %s account is out of quota%s: "%s". Its deliveries and nudges, and alerts about it, wait '
                   'until %s%s; then it gets one fresh try. Its tickets, reservations and context are untouched. '
                   'Clear early with: ticket-board-write clear-provider-hold %s --reason "..."',
                   p_role, reg.runtime, CASE WHEN p_evidence->>'limit' <> '' THEN ' (' || (p_evidence->>'limit') || ')' ELSE '' END,
                   left(coalesce(p_evidence->>'message', ''), 240), to_char(v_deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'),
                   CASE WHEN v_reset IS NULL THEN ' (the provider gave no reset time; this is a recheck)' ELSE ' (its reset, plus a grace)' END,
                   p_role),
            -- Its own kind, as the Director's other alerts: an open stall episode
            -- lets only its own escalation through (SYRD-595), and this is news
            -- of a different kind about the same role.
            jsonb_build_object('kind', 'provider_quota_hold', 'reason', 'provider_quota_hold', 'id', anchor, 'role', p_role,
                               'owner_role', p_role, 'runtime', reg.runtime, 'hold', new_id, 'deadline', v_deadline,
                               'reset_at', v_reset, 'target_role', 'director'),
            format('provider-quota-hold:%s', new_id));
        UPDATE ticket_board.provider_quota_holds SET notified = true WHERE id = new_id;
    END IF;
    RETURN jsonb_build_object('hold', new_id, 'began', true, 'joined', sibling.id, 'notified', sibling.id IS NULL AND anchor IS NOT NULL);
END;
$$;

-- A hold whose time has come: released, its waiting notices due now, and --
-- when nothing is waiting -- one recheck to the role, as an update about its
-- work. True when the recheck was queued.
CREATE OR REPLACE FUNCTION ticket_board.release_provider_hold(p_id bigint, p_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE hold ticket_board.provider_quota_holds; anchor text;
BEGIN
    UPDATE ticket_board.provider_quota_holds h SET state = 'released', released_at = p_at,
        history = h.history || jsonb_build_array(jsonb_build_object('at', clock_timestamp(), 'event', 'released'))
    WHERE h.id = p_id AND h.state = 'held'
    RETURNING * INTO hold;
    IF hold.id IS NULL THEN RETURN false; END IF;
    UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = clock_timestamp()
    WHERE q.target_role = hold.role AND q.last_error LIKE 'provider_quota_hold%' AND q.claimed_at IS NULL;
    anchor := ticket_board.provider_hold_anchor(hold.role);
    IF anchor IS NULL OR EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                                 WHERE q.target_role = hold.role AND q.dead_lettered_at IS NULL) THEN
        RETURN false;
    END IF;
    -- An update about the work, not a reminder: it waits for the pane like
    -- any update, and is judged current by the ticket it names.
    PERFORM ticket_board.enqueue_notification(
        anchor, 'ticket_update', hold.role,
        format('%s: your %s quota should have reset. Continue the work you were on; if the provider '
               'still refuses, the board will hold again on what it says.', anchor, hold.runtime),
        (SELECT jsonb_build_object('kind', 'ticket_update', 'id', t.id, 'title', t.title, 'state', t.state,
                                   'assignee', t.assignee, 'updated_at', t.updated_at,
                                   'ticket_number', t.ticket_number, 'target_role', hold.role,
                                   'actor', 'listener', 'change_summary', 'provider quota hold released',
                                   'reason', 'provider_quota_recheck', 'hold', hold.id)
         FROM ticket_board.tickets t WHERE t.id = anchor),
        format('provider-quota-recheck:%s:%s', hold.id, hold.renewals));
    RETURN true;
END;
$$;

-- Every pass: end holds that no longer apply, and release those whose time has
-- come. A released hold allows one fresh attempt: what was waiting goes, or,
-- with nothing waiting, one recheck to the role.
CREATE OR REPLACE FUNCTION ticket_board.reconcile_provider_quota_holds(p_facts jsonb, p_now timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    hold ticket_board.provider_quota_holds;
    reg ticket_board.role_runtime_assignments;
    facts jsonb;
    normal_end timestamptz;
    anchor text;
    ended integer := 0;
    released integer := 0;
    rechecks integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('reconcile_provider_quota_holds');
    FOR hold IN SELECT * FROM ticket_board.provider_quota_holds h WHERE h.state IN ('held', 'released') ORDER BY h.id FOR UPDATE LOOP
        SELECT * INTO reg FROM ticket_board.role_runtime_assignments a WHERE a.role = hold.role;
        facts := coalesce(p_facts -> hold.role, '{}'::jsonb);
        normal_end := nullif(facts->>'normal_end_at', '')::timestamptz;
        IF reg.role IS NULL OR reg.runtime IS DISTINCT FROM hold.runtime THEN
            PERFORM ticket_board.end_provider_hold(hold.id, 'reassigned'); ended := ended + 1; CONTINUE;
        END IF;
        IF reg.generation <> hold.generation OR reg.process_pid IS DISTINCT FROM hold.pid
           OR reg.process_start_time IS DISTINCT FROM hold.process_start THEN
            PERFORM ticket_board.end_provider_hold(hold.id, 'replaced'); ended := ended + 1; CONTINUE;
        END IF;
        IF coalesce(facts->>'session', '') <> '' AND hold.provider_session <> '' AND facts->>'session' <> hold.provider_session THEN
            PERFORM ticket_board.end_provider_hold(hold.id, 'new_session'); ended := ended + 1; CONTINUE;
        END IF;
        IF normal_end IS NOT NULL AND normal_end > greatest(hold.evidence_at, coalesce(hold.released_at, hold.evidence_at)) THEN
            PERFORM ticket_board.end_provider_hold(hold.id, 'recovered'); ended := ended + 1; CONTINUE;
        END IF;
        IF hold.state = 'held' AND hold.deadline <= p_now THEN
            released := released + 1;
            rechecks := rechecks + CASE WHEN ticket_board.release_provider_hold(hold.id, clock_timestamp()) THEN 1 ELSE 0 END;
            CONTINUE;
        END IF;
        -- A release nobody answered is history after a day.
        IF hold.state = 'released' AND hold.released_at < p_now - interval '1 day' THEN
            PERFORM ticket_board.end_provider_hold(hold.id, 'lapsed'); ended := ended + 1;
        END IF;
    END LOOP;
    RETURN jsonb_build_object('ended', ended, 'released', released, 'rechecks', rechecks);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_provider_hold(p_role text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; cur ticket_board.provider_quota_holds;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    actor := ticket_board.current_app_actor();
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'clearing a provider hold needs a reason'; END IF;
    SELECT * INTO cur FROM ticket_board.provider_quota_holds h WHERE h.role = p_role AND h.state IN ('held', 'released') FOR UPDATE;
    IF cur.id IS NULL THEN RAISE EXCEPTION 'role % has no provider quota hold', p_role; END IF;
    PERFORM ticket_board.end_provider_hold(cur.id, 'cleared: ' || btrim(p_reason), actor);
    SELECT * INTO cur FROM ticket_board.provider_quota_holds h WHERE h.id = cur.id;
    RETURN to_jsonb(cur);
END;
$$;

-- Read-only: current holds, and the last few ended, without credentials or prompts.
CREATE OR REPLACE FUNCTION ticket_board.provider_hold_status(p_now timestamptz)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object('holds', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'id', h.id, 'role', h.role, 'runtime', h.runtime, 'state', h.state,
            'current', h.state = 'held' AND h.deadline > p_now, 'evidence_at', h.evidence_at,
            'source', h.evidence->>'source', 'limit', h.evidence->>'limit', 'message', h.evidence->>'message',
            'reset_at', h.reset_at, 'deadline', h.deadline, 'recheck', NOT coalesce((h.evidence->>'reset_given')::boolean, false),
            'renewals', h.renewals, 'released_at', h.released_at, 'ended_at', h.ended_at, 'end_reason', h.end_reason,
            'joined_hold', h.joined_hold,
            'clear', CASE WHEN h.state IN ('held', 'released')
                          THEN format('ticket-board-write clear-provider-hold %s --reason "..."', h.role) END)
            ORDER BY h.state = 'ended', h.id DESC)
        FROM (SELECT * FROM ticket_board.provider_quota_holds
              WHERE state IN ('held', 'released') OR ended_at > p_now - interval '1 day'
              ORDER BY id DESC LIMIT 50) h), '[]'::jsonb));
$$;

REVOKE ALL ON ticket_board.provider_quota_holds FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.provider_quota_holds_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.role_provider_hold_is_current(text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.role_provider_hold_until(text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.ticket_turn_terminal(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.provider_hold_anchor(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.provider_held_ticket_owner(text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.end_provider_hold(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.release_provider_hold(bigint, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_provider_quota(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.reconcile_provider_quota_holds(jsonb, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.clear_provider_hold(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.provider_hold_status(timestamptz) FROM PUBLIC;
GRANT SELECT ON ticket_board.provider_quota_holds TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_provider_hold_until(text, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_provider_hold_is_current(text, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.provider_held_ticket_owner(text, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_provider_quota(text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.reconcile_provider_quota_holds(jsonb, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.provider_hold_status(timestamptz) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.clear_provider_hold(text, text) TO ticket_board_service;

COMMIT;
