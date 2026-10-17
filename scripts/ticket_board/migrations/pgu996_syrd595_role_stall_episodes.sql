-- SYRD-595: a role that keeps failing to resolve its ticket is prompted a
-- bounded number of times, then the Director is told once; never both at once.
--
-- On 2026-10-16 SYRD Audit's agy provider ran out of quota. Every turn it was
-- given failed. Measured on main with a provider that fails every turn: the
-- owner was prompted "this turn ended without resolving it" once per grace
-- period with no end, and the Director was told "may be stuck" and "the grace
-- period has passed" in between, so both were being told about the same stall
-- at once. A provider whose hook stayed busy after the failure got nothing at
-- all, ever.
--
-- The listener reports each role's pane on every pass
-- (scripts/ticket_board/stall_episode.py). This keeps one episode per ticket
-- round (its stage entry or hand-over), in the database, so a restarted
-- listener or a replaced runtime cannot fan one episode out to both recipients:
--
--   * phase `role`: the owner's repair prompts are the attempts, and the
--     Director hears nothing about the stall;
--   * after p_max_attempts prompts were sent and the last one failed too, or
--     when the pane has stayed busy with nothing moving since it was handed the
--     work: one escalation to the Director, and phase `director`, in which the
--     owner is sent no automatic prompt;
--   * a Director comment on the ticket after that hands it back: a new `role`
--     phase, recorded;
--   * it closes when the ticket moves or changes hands, or its owner writes on
--     it; one opened on a prompt that then never went closes as withdrawn.
--     A prompt left queued from an earlier round counts for nothing (SYRD-527).
--
-- Delivery asks `role_stall_notice_is_current` too, which drops what the phase
-- does not allow. schema.sql carries the same objects.

BEGIN;

-- SYRD-589's redelivery, tied to the conversation that received it: a send never seen to arrive counts as
-- the old provider's too, and a provider that resumed the very conversation the ticket was typed into is
-- not handed it again. schema.sql's copy of the function is this text; replacing it keeps its grants.
CREATE OR REPLACE FUNCTION ticket_board.redeliver_replaced_work(
    p_turnless_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    queued integer := 0;
    started timestamptz;
    provider jsonb;
    session_now text;
    dedupe text;
    message text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('redeliver_replaced_work');
    IF jsonb_typeof(p_turnless_since_by_role) IS DISTINCT FROM 'object' THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        WITH waiting AS MATERIALIZED (
            -- Waiting in an implementer's queue is nobody's current work (SYRD-568).
            SELECT q.ticket_id FROM ticket_board.serial_queue() q GROUP BY q.ticket_id HAVING NOT bool_or(q.active)
        )
        SELECT DISTINCT ON (current_work.owner_role) current_work.*
        FROM (
            SELECT t.id, t.title, t.state, t.assignee, t.ticket_number, ns.entered_current_state_at,
                   ticket_board.transition_target_role(t.state, t.assignee) AS owner_role
            FROM ticket_board.tickets t
            LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            -- What the board counts as a role's current work (ticket_read_query's
            -- is_actionable_current), and the first of it in the board's order.
            WHERE NOT t.manually_controlled
              AND coalesce(ns.awaiting_role, '') = ''
              AND coalesce(t.queued_for_assignee, '') = ''
              AND coalesce(t.queued_behind_ticket, '') = ''
              AND NOT coalesce((SELECT s.is_terminal FROM ticket_board.workflow_stages s WHERE s.name = t.state), false)
              AND NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id AND NOT b.resolved)
              AND NOT EXISTS (SELECT FROM waiting w WHERE w.ticket_id = t.id)
        ) current_work
        WHERE current_work.owner_role IS NOT NULL AND current_work.owner_role <> ''
          AND p_turnless_since_by_role ? current_work.owner_role
        ORDER BY current_work.owner_role, current_work.entered_current_state_at NULLS LAST, current_work.ticket_number
    LOOP
        -- When the provider started, or {started, session}: the conversation it holds, when its hook
        -- recorded one (SYRD-595).
        BEGIN
            provider := p_turnless_since_by_role -> candidate.owner_role;
            IF jsonb_typeof(provider) = 'object' THEN
                started := (provider ->> 'started')::timestamptz;
                session_now := nullif(provider ->> 'session', '');
            ELSE
                started := (provider #>> '{}')::timestamptz;
                session_now := NULL;
            END IF;
        EXCEPTION WHEN others THEN
            CONTINUE;
        END;
        IF started IS NULL OR started > p_now THEN
            CONTINUE;
        END IF;
        -- Delivered in this stage, and to a provider older than this one. A send never seen to arrive
        -- is no more this provider's than one that was (SYRD-595).
        IF NOT EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id
              AND trace.target_role = candidate.owner_role
              AND trace.kind = 'transition'
              AND trace.event IN ('send', 'send_unconfirmed')
              AND trace.ticket_state_at_event = candidate.state
              AND (candidate.entered_current_state_at IS NULL OR trace.ts >= candidate.entered_current_state_at)
        ) OR EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id
              AND trace.target_role = candidate.owner_role
              AND trace.kind = 'transition'
              AND trace.event IN ('send', 'send_unconfirmed')
              AND trace.ts >= started
        ) THEN
            CONTINUE;
        END IF;
        -- It resumed the very conversation the ticket was last typed into: that conversation has it (SYRD-595).
        IF session_now IS NOT NULL AND session_now = (
            SELECT trace.detail ->> 'provider_session'
            FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id
              AND trace.target_role = candidate.owner_role
              AND trace.kind = 'transition'
              AND trace.event IN ('send', 'send_unconfirmed')
              AND trace.ticket_state_at_event = candidate.state
              AND (candidate.entered_current_state_at IS NULL OR trace.ts >= candidate.entered_current_state_at)
            ORDER BY trace.ts DESC
            LIMIT 1
        ) THEN
            CONTINUE;
        END IF;
        -- Nothing about it already on its way.
        IF EXISTS (
            SELECT FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidate.id AND q.target_role = candidate.owner_role AND q.kind = 'transition'
        ) THEN
            CONTINUE;
        END IF;
        dedupe := 'redelivery:' || candidate.id || ':' || candidate.owner_role || ':'
                  || floor(extract(epoch FROM started))::bigint::text;
        IF EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id AND trace.event = 'enqueue' AND trace.detail->>'dedupe_key' = dedupe
        ) THEN
            CONTINUE;
        END IF;
        message := ticket_board.transition_message(candidate.id, candidate.title, candidate.state, candidate.state, true);
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'transition',
            candidate.owner_role,
            message,
            jsonb_build_object(
                'kind', 'transition',
                'id', candidate.id,
                'title', candidate.title,
                'old_state', candidate.state,
                'new_state', candidate.state,
                'assignee', candidate.assignee,
                'updated_at', p_now,
                'ticket_number', candidate.ticket_number,
                'target_role', candidate.owner_role,
                'message', message,
                'redelivery', jsonb_build_object('reason', 'provider_replaced', 'provider_started_at', started)
            ),
            dedupe
        );
        queued := queued + 1;
    END LOOP;
    RETURN queued;
END;
$$;

CREATE TABLE IF NOT EXISTS ticket_board.role_stall_episodes (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL,
    owner_role text NOT NULL,
    ticket_state text NOT NULL,
    ticket_assignee text NOT NULL,
    -- The ticket's round with the owner (ticket_notification_state.current_assignment_at).
    round_at timestamptz,
    phase text NOT NULL DEFAULT 'role' CHECK (phase IN ('role', 'director')),
    phase_no integer NOT NULL DEFAULT 1 CHECK (phase_no > 0),
    phase_started_at timestamptz NOT NULL,
    -- The owner's registered runtime generation when the phase began: a replacement starts a new phase.
    phase_generation bigint,
    -- How many repair prompts a phase allows; the listener's setting, refreshed on every pass.
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    -- Every phase, in order: {phase_no, recipient, started_at, escalated_at, notification_id, reason}.
    phases jsonb NOT NULL DEFAULT '[]'::jsonb,
    escalated_at timestamptz,
    escalation_notification_id bigint,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    close_reason text CHECK (close_reason IS NULL OR close_reason IN ('moved', 'progress', 'withdrawn'))
);

-- One open episode per ticket.
CREATE UNIQUE INDEX IF NOT EXISTS role_stall_episodes_open
    ON ticket_board.role_stall_episodes (ticket_id) WHERE closed_at IS NULL;

-- Close an episode; an escalation not sent yet is withdrawn with it.
CREATE OR REPLACE FUNCTION ticket_board.close_role_stall(p_episode_id bigint, p_how text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ep ticket_board.role_stall_episodes%ROWTYPE;
BEGIN
    UPDATE ticket_board.role_stall_episodes
    SET closed_at = clock_timestamp(), close_reason = p_how
    WHERE id = p_episode_id AND closed_at IS NULL
    RETURNING * INTO ep;
    IF NOT FOUND OR ep.escalation_notification_id IS NULL THEN
        RETURN;
    END IF;
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id, q.id, q.target_role, q.kind, 'discard', NULL, 'role_stall_' || p_how, NULL,
        jsonb_build_object('reason', 'role_stall_' || p_how, 'episode', ep.id))
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = ep.escalation_notification_id AND q.dead_lettered_at IS NULL;
    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = ep.escalation_notification_id AND dead_lettered_at IS NULL;
END;
$$;

-- The runtime generation that was registered for a role at a moment: the one a
-- notice sent then reached. NULL when the role has no registration history.
CREATE OR REPLACE FUNCTION ticket_board.role_generation_at(p_role text, p_at timestamptz)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT h.generation FROM ticket_board.role_runtime_assignment_history h
    WHERE h.role = p_role AND h.registered_at <= p_at
    ORDER BY h.registered_at DESC, h.generation DESC
    LIMIT 1;
$$;

-- One listener pass's report: role -> {state, updated_at, turn_started_at,
-- busy_still, condition}. The first three are the owner's own turn as its CLI
-- hook recorded it; busy_still says the hook has said busy for longer than the
-- grace while nothing on the screen moved and no question is open; condition
-- is the last text the pane shows, or null. A role left out could not be read,
-- and nothing about it changes. Returns how many escalations were queued.
CREATE OR REPLACE FUNCTION ticket_board.note_role_stall(
    p_report jsonb,
    p_max_attempts integer,
    p_grace interval,
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
    ep ticket_board.role_stall_episodes%ROWTYPE;
    cur record;
    hook_state text;
    hook_updated timestamptz;
    turn_started timestamptz;
    busy_still boolean;
    condition_text text;
    attempts jsonb;
    sent integer;
    last_sent timestamptz;
    handed_at timestamptz;
    handed_enqueued timestamptz;
    rearm_at timestamptz;
    why text;
    message text;
    notice_id bigint;
    generation_now bigint;
    runtime_now text;
    reached text;
    history_text text;
    replaced_from text;
    counted_from timestamptz;
    queued integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_role_stall');
    IF jsonb_typeof(p_report) IS DISTINCT FROM 'object' OR coalesce(p_max_attempts, 0) < 1 THEN
        RETURN 0;
    END IF;

    FOR item IN
        SELECT r.key AS role, r.value AS v FROM jsonb_each(p_report) r
        -- The Director's own pane is not escalated about: the alert would wait there too (SYRD-570).
        WHERE r.key <> 'director' AND jsonb_typeof(r.value) = 'object'
        ORDER BY r.key
    LOOP
        BEGIN
            hook_state := item.v->>'state';
            hook_updated := nullif(item.v->>'updated_at', '')::timestamptz;
            turn_started := nullif(item.v->>'turn_started_at', '')::timestamptz;
            busy_still := coalesce((item.v->>'busy_still')::boolean, false);
        EXCEPTION WHEN others THEN
            CONTINUE;
        END;
        condition_text := left(nullif(btrim(coalesce(item.v->>'condition', '')), ''), 300);

        -- The role's current work, selected as redeliver_replaced_work selects it.
        WITH waiting AS MATERIALIZED (
            SELECT q.ticket_id FROM ticket_board.serial_queue() q GROUP BY q.ticket_id HAVING NOT bool_or(q.active)
        )
        SELECT t.id, t.title, t.state, t.assignee, ns.current_assignment_at AS round_at
        INTO cur
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

        -- Every open episode of this role that is not about its current round is over.
        FOR ep IN
            SELECT * FROM ticket_board.role_stall_episodes e
            WHERE e.owner_role = item.role AND e.closed_at IS NULL
              AND (cur.id IS NULL OR (e.ticket_id, e.ticket_state, e.ticket_assignee, e.round_at)
                   IS DISTINCT FROM (cur.id, cur.state, cur.assignee, cur.round_at))
            FOR UPDATE
        LOOP
            PERFORM ticket_board.close_role_stall(ep.id, 'moved');
        END LOOP;
        IF cur.id IS NULL THEN
            CONTINUE;
        END IF;

        SELECT * INTO ep FROM ticket_board.role_stall_episodes e
        WHERE e.ticket_id = cur.id AND e.closed_at IS NULL FOR UPDATE;
        SELECT a.generation, a.runtime INTO generation_now, runtime_now
        FROM ticket_board.role_runtime_assignments a WHERE a.role = item.role;

        -- Resolved for now by one of the legitimate means (a snooze, a lease, its own notice on its way): nothing
        -- to decide on this pass. The episode stays: none of those is the owner working it.
        IF ticket_board.ticket_turn_is_resolved(cur.id, p_now) THEN
            CONTINUE;
        END IF;
        -- The owner wrote on it: not stalled.
        IF ep.id IS NOT NULL AND EXISTS (
            SELECT FROM ticket_board.ticket_comments c
            WHERE c.ticket_id = cur.id AND lower(c.who) = item.role AND c.ts >= ep.phase_started_at
        ) THEN
            PERFORM ticket_board.close_role_stall(ep.id, 'progress');
            CONTINUE;
        END IF;

        -- The owner's repair prompts in this phase that reached its pane: the attempts. A new episode counts
        -- from the round's start, or from when the last episode of it closed.
        counted_from := coalesce(ep.phase_started_at, greatest(cur.round_at, (
            SELECT max(done.closed_at) FROM ticket_board.role_stall_episodes done
            WHERE done.ticket_id = cur.id AND done.closed_at IS NOT NULL)));
        SELECT coalesce(jsonb_agg(jsonb_build_object(
                   'notification_id', a.notification_id, 'sent_at', a.sent_at,
                   'generation', ticket_board.role_generation_at(item.role, a.sent_at)) ORDER BY a.sent_at), '[]'::jsonb),
               count(*), max(a.sent_at)
        INTO attempts, sent, last_sent
        FROM (
            SELECT tr.notification_id, min(tr.ts) AS sent_at
            FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = cur.id
              AND tr.target_role = item.role
              AND tr.kind = 'unresolved_turn_repair'
              AND tr.event IN ('send', 'send_unconfirmed')
              AND (counted_from IS NULL OR tr.ts >= counted_from)
            GROUP BY tr.notification_id
        ) a;

        IF ep.id IS NULL THEN
            -- Nothing to bound until the owner has been prompted, or its pane took the work and stopped moving.
            -- A prompt on its way counts: a listener back after downtime queues it, and the Director's notices,
            -- on the same pass, and this pass runs before any is delivered.
            IF sent = 0 AND NOT busy_still AND NOT EXISTS (
                SELECT FROM ticket_board.ticket_notification_queue q
                WHERE q.ticket_id = cur.id AND q.target_role = item.role AND q.kind = 'unresolved_turn_repair'
                  AND q.dead_lettered_at IS NULL
                  -- Queued in this round: one left over from an earlier round is never delivered (SYRD-527).
                  AND (cur.round_at IS NULL OR q.created_at >= cur.round_at)) THEN
                CONTINUE;
            END IF;
            INSERT INTO ticket_board.role_stall_episodes
                (ticket_id, owner_role, ticket_state, ticket_assignee, round_at, phase, phase_no, phase_started_at,
                 phase_generation, max_attempts, phases, opened_at)
            VALUES (cur.id, item.role, cur.state, cur.assignee, cur.round_at, 'role', 1,
                    coalesce(counted_from, p_now), generation_now, p_max_attempts,
                    jsonb_build_array(jsonb_build_object('phase_no', 1, 'recipient', item.role,
                                                         'started_at', coalesce(counted_from, p_now))),
                    p_now)
            RETURNING * INTO ep;
        END IF;

        IF generation_now IS NOT NULL AND ep.phase_generation IS DISTINCT FROM generation_now THEN
            IF ep.phase_generation IS NOT NULL THEN
                -- The owner's runtime was replaced: the provider running now starts a phase of its own, and an
                -- escalation about the old one not sent yet is withdrawn -- the replacement was the answer to it.
                PERFORM ticket_board.record_notification_trace(
                    q.ticket_id, q.id, q.target_role, q.kind, 'discard', NULL, 'role_stall_runtime_replaced', NULL,
                    jsonb_build_object('reason', 'role_stall_runtime_replaced', 'episode', ep.id))
                FROM ticket_board.ticket_notification_queue q
                WHERE q.id = ep.escalation_notification_id AND q.dead_lettered_at IS NULL;
                DELETE FROM ticket_board.ticket_notification_queue
                WHERE id = ep.escalation_notification_id AND dead_lettered_at IS NULL;
                UPDATE ticket_board.role_stall_episodes
                SET phase = 'role', phase_no = phase_no + 1, phase_started_at = p_now, phase_generation = generation_now,
                    escalated_at = NULL, escalation_notification_id = NULL,
                    phases = phases || jsonb_build_array(jsonb_build_object(
                        'phase_no', phase_no + 1, 'recipient', item.role, 'started_at', p_now,
                        'reason', 'runtime_replaced', 'generation', generation_now, 'from_generation', ep.phase_generation))
                WHERE id = ep.id
                RETURNING * INTO ep;
                CONTINUE;
            END IF;
            UPDATE ticket_board.role_stall_episodes SET phase_generation = generation_now WHERE id = ep.id
            RETURNING * INTO ep;
        END IF;
        -- Opened on a prompt that then never went (dropped, or withdrawn unsent): no attempt was made, and the
        -- episode is not left holding anyone's notices.
        IF ep.phase = 'role' AND ep.phase_no = 1 AND sent = 0 AND NOT busy_still AND NOT EXISTS (
            SELECT FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = cur.id AND q.target_role = item.role AND q.kind = 'unresolved_turn_repair'
              AND q.dead_lettered_at IS NULL AND (cur.round_at IS NULL OR q.created_at >= cur.round_at)) THEN
            PERFORM ticket_board.close_role_stall(ep.id, 'withdrawn');
            CONTINUE;
        END IF;
        IF ep.max_attempts IS DISTINCT FROM p_max_attempts THEN
            UPDATE ticket_board.role_stall_episodes SET max_attempts = p_max_attempts WHERE id = ep.id
            RETURNING * INTO ep;
        END IF;

        IF ep.phase = 'director' THEN
            -- Handed back: a Director comment since the escalation starts a new phase for the owner.
            SELECT max(c.ts) INTO rearm_at FROM ticket_board.ticket_comments c
            WHERE c.ticket_id = cur.id AND lower(c.who) = 'director' AND c.ts > ep.escalated_at;
            IF rearm_at IS NOT NULL THEN
                UPDATE ticket_board.role_stall_episodes
                SET phase = 'role', phase_no = phase_no + 1, phase_started_at = rearm_at,
                    escalated_at = NULL, escalation_notification_id = NULL,
                    phases = phases || jsonb_build_array(jsonb_build_object(
                        'phase_no', phase_no + 1, 'recipient', item.role, 'started_at', rearm_at,
                        'reason', 'director_comment'))
                WHERE id = ep.id;
            END IF;
            CONTINUE;
        END IF;

        -- When the work last reached the pane: the latest notice for this ticket sent to it in this phase.
        SELECT max(tr.ts) INTO handed_at FROM ticket_board.notification_trace tr
        WHERE tr.ticket_id = cur.id AND tr.target_role = item.role
          AND tr.event IN ('send', 'send_unconfirmed') AND tr.ts >= ep.phase_started_at;
        SELECT min(en.ts) INTO handed_enqueued FROM ticket_board.notification_trace sent_tr
        JOIN ticket_board.notification_trace en ON en.notification_id = sent_tr.notification_id AND en.event = 'enqueue'
        WHERE sent_tr.ticket_id = cur.id AND sent_tr.target_role = item.role
          AND sent_tr.event IN ('send', 'send_unconfirmed') AND sent_tr.ts = handed_at;

        why := NULL;
        IF busy_still AND hook_state IS DISTINCT FROM 'idle' AND handed_at IS NOT NULL
           AND turn_started IS NOT NULL AND turn_started >= coalesce(handed_enqueued, handed_at)
           AND p_now >= greatest(handed_at, coalesce(hook_updated, handed_at)) + p_grace THEN
            -- It took the work and has said busy since, with nothing moving: it cannot be prompted again.
            why := 'busy_no_progress';
        ELSIF sent >= p_max_attempts AND (
                  (hook_state = 'idle' AND turn_started IS NOT NULL AND turn_started >= last_sent - interval '5 seconds'
                   AND hook_updated > last_sent)
                  OR p_now >= last_sent + p_grace) THEN
            -- Every prompt went, and the turn after the last one ended unresolved too (or never came).
            why := 'attempts_exhausted';
        END IF;
        IF why IS NULL THEN
            CONTINUE;
        END IF;

        SELECT p->>'from_generation' INTO replaced_from FROM jsonb_array_elements(ep.phases) p
        WHERE (p->>'phase_no')::integer = ep.phase_no AND p->>'reason' = 'runtime_replaced' LIMIT 1;
        SELECT string_agg(DISTINCT (x->>'generation'), ', ') INTO reached
        FROM jsonb_array_elements(attempts) x WHERE x->>'generation' IS NOT NULL;
        SELECT string_agg(to_char((x->>'sent_at')::timestamptz AT TIME ZONE 'UTC', 'HH24:MI:SS'), ', ' ORDER BY x->>'sent_at')
        INTO history_text FROM jsonb_array_elements(attempts) x;
        message := format(
            'PRIORITY %s -- %s: %s ',
            cur.id, cur.title, item.role)
            || CASE why
                   WHEN 'busy_no_progress' THEN format(
                       'took it at %s UTC and its pane has said busy since, with nothing moving on screen. ',
                       to_char(handed_at AT TIME ZONE 'UTC', 'HH24:MI:SS'))
                   ELSE format(
                       'was prompted %s times (%s UTC) to resolve it, and each turn ended without resolving it. ',
                       sent, history_text)
               END
            || CASE WHEN condition_text IS NOT NULL THEN format('Its pane shows: "%s". ', condition_text) ELSE '' END
            || CASE WHEN generation_now IS NOT NULL THEN format('Its runtime is %s, generation %s%s%s. ', runtime_now,
                    generation_now,
                    CASE WHEN replaced_from IS NOT NULL THEN format(', which replaced generation %s', replaced_from) ELSE '' END,
                    CASE WHEN reached IS NOT NULL AND reached <> generation_now::text
                                         THEN format(' (the prompts reached generation %s)', reached) ELSE '' END)
                    ELSE '' END
            || format('Nothing more will be sent to %s about it until you act: comment on the ticket to hand it back '
                      'for another round, reassign it, or fix the pane (`switchyard attach %s %s`).',
                      item.role, coalesce(nullif(ticket_board.declared_workflow()->>'project', ''), '<project>'), item.role);
        notice_id := ticket_board.enqueue_notification(
            cur.id, 'unresolved_turn', 'director', message,
            jsonb_build_object('kind', 'stall_escalation', 'id', cur.id, 'title', cur.title, 'state', cur.state,
                               'assignee', cur.assignee, 'target_role', 'director', 'role', item.role,
                               'reason', why, 'condition', condition_text, 'episode', ep.id, 'phase_no', ep.phase_no,
                               'attempts', attempts, 'generation', generation_now, 'runtime', runtime_now,
                               'message', message),
            'stall-escalation:' || ep.id || ':' || ep.phase_no);
        UPDATE ticket_board.role_stall_episodes
        SET phase = 'director', escalated_at = p_now, escalation_notification_id = notice_id,
            phases = phases || jsonb_build_array(jsonb_build_object(
                'phase_no', ep.phase_no, 'recipient', 'director', 'started_at', p_now, 'reason', why,
                'notification_id', notice_id, 'attempts', attempts))
        WHERE id = ep.id;
        queued := queued + 1;
    END LOOP;
    RETURN queued;
END;
$$;

-- Delivery asks this as well: what an open episode's phase does not allow is not typed. Only one recipient
-- is ever being told about a stall: the owner while it is being prompted, the Director once it was escalated.
CREATE OR REPLACE FUNCTION ticket_board.role_stall_notice_is_current(p_ticket_id text, p_target_role text, p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE
        WHEN p_payload->>'kind' = 'stall_escalation' THEN EXISTS (
            SELECT FROM ticket_board.role_stall_episodes e
            WHERE e.id = (p_payload->>'episode')::bigint AND e.closed_at IS NULL AND e.phase = 'director'
              AND e.phase_no = (p_payload->>'phase_no')::integer AND p_target_role = 'director')
        ELSE NOT coalesce((
            -- About a turn of a provider that has since been replaced: the one running now never had it.
            SELECT CASE p_payload->>'kind'
                WHEN 'unresolved_turn_repair' THEN CASE WHEN p_payload->>'turn_id' ~ '^\d{4}-\d\d-\d\d[T ]'
                    THEN (p_payload->>'turn_id')::timestamptz < a.registered_at ELSE false END
                WHEN 'capacity_retry' THEN (
                    SELECT max((f->>'observed_at')::timestamptz) < a.registered_at
                    FROM ticket_board.provider_capacity_episodes ce, jsonb_array_elements(ce.failures) f
                    WHERE ce.id::text = p_payload->>'episode')
                ELSE false END
            FROM ticket_board.role_runtime_assignments a WHERE a.role = p_target_role), false)
        -- The owner's repair prompt on its way is the owner's turn to be told, episode or not yet. Not the
        -- handoff about that prompt itself (unresolved_turn): it comes a grace later, and only when the prompt
        -- is still not delivered, or was not answered (SYRD-584).
        AND NOT (p_target_role = 'director'
                 AND p_payload->>'kind' IN ('nudge', 'escalation')
                 AND coalesce(p_payload->>'reason', '') <> 'permission_prompt'
                 AND EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                             LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = q.ticket_id
                             WHERE q.ticket_id = p_ticket_id AND q.kind = 'unresolved_turn_repair'
                               AND q.dead_lettered_at IS NULL
                               -- In this round: an earlier round's is never delivered (SYRD-527). to_jsonb keeps
                               -- a board without the SYRD-514 column readable: no column, no bound.
                               AND q.created_at >= coalesce((to_jsonb(ns)->>'current_assignment_at')::timestamptz,
                                                            q.created_at)))
        AND NOT EXISTS (
            SELECT FROM ticket_board.role_stall_episodes e
            WHERE e.ticket_id = p_ticket_id AND e.closed_at IS NULL
              AND (
                  -- Never the Director's other stall notices about it: before the escalation the owner is the
                  -- one being told, and after it the escalation is the one message.
                  (p_target_role = 'director'
                   AND p_payload->>'kind' IN ('unresolved_turn', 'nudge', 'escalation')
                   AND coalesce(p_payload->>'reason', '') <> 'permission_prompt')
                  -- The owner's repair prompt is the attempt; its other automatic reminder is not another one,
                  -- and a phase allows only so many.
                  OR (p_target_role = e.owner_role AND e.phase = 'role' AND p_payload->>'kind' = 'idle_reminder')
                  OR (p_target_role = e.owner_role AND e.phase = 'role' AND p_payload->>'kind' = 'unresolved_turn_repair'
                      AND (SELECT count(DISTINCT tr.notification_id) FROM ticket_board.notification_trace tr
                           WHERE tr.ticket_id = e.ticket_id AND tr.target_role = e.owner_role
                             AND tr.kind = 'unresolved_turn_repair' AND tr.event IN ('send', 'send_unconfirmed')
                             AND tr.ts >= e.phase_started_at) >= e.max_attempts)
                  -- Escalated: no automatic prompt to the owner until the Director hands it back.
                  OR (p_target_role = e.owner_role AND e.phase = 'director'
                      AND p_payload->>'kind' IN ('unresolved_turn_repair', 'idle_reminder', 'capacity_retry'))
              ))
    END;
$$;

REVOKE ALL ON ticket_board.role_stall_episodes FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.close_role_stall(bigint, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.role_generation_at(text, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_role_stall(jsonb, integer, interval, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.role_stall_notice_is_current(text, text, jsonb) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.role_stall_episodes TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.note_role_stall(jsonb, integer, interval, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_stall_notice_is_current(text, text, jsonb) TO ticket_board_listener;

COMMIT;
