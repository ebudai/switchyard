-- SYRD-756 (pgu1038): stall clocks do not run while work is paused.
--
-- The role stall episode (note_role_stall), the unresolved-turn escalation (notify_unresolved_turn_end), the board
-- stall digest (note_board_stall), and the pull policy's idle-capacity and pinned-rework alerts measure from
-- ticket_board.unpaused_since(start, p_now) (pgu1036): paused time is not time without progress, so resuming fires no
-- escalation for it. While the pause stands its roles are held as well (role_provider_hold_is_current), so none of
-- these decides about them meanwhile. Nothing stored is rewritten.
--
-- Each function is its owning migration's copy as schema.sql's last copy has it, with only those comparisons
-- changed. Grants are unchanged.
--
-- schema.sql carries the same functions.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.note_role_stall(
    p_report jsonb,
    p_max_attempts integer,
    p_grace interval,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_busy_threshold interval DEFAULT NULL
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
    clock timestamptz;
    stale text;
    -- How long the pane may say busy with nothing moving: the board's configured stall threshold, never less
    -- than the grace (SYRD-675). The listener passes the one it is configured with, so the first pass after
    -- a start or a change uses it too; a caller that does not falls back to what the board stall pass recorded.
    busy_threshold interval := greatest(p_grace, coalesce(p_busy_threshold, make_interval(secs => coalesce(
        (SELECT s.threshold_seconds FROM ticket_board.board_stall_state s WHERE s.id), 1800))));
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
            -- Not delivered yet: asked again, as delivery asks (SYRD-675). Progress since what it saw, or a drop at
            -- delivery, ends the episode, so the owner is not left without its prompts by an alert nobody read.
            IF NOT EXISTS (SELECT FROM ticket_board.notification_trace tr
                           WHERE tr.notification_id = ep.escalation_notification_id
                             AND tr.event IN ('send', 'send_unconfirmed')) THEN
                SELECT ticket_board.role_stall_escalation_stale_reason(
                           q.payload || jsonb_build_object('owner_hook', item.v), p_now)
                INTO stale
                FROM ticket_board.ticket_notification_queue q
                WHERE q.id = ep.escalation_notification_id AND q.dead_lettered_at IS NULL;
                IF stale IS NOT NULL THEN
                    PERFORM ticket_board.record_notification_trace(
                        q.ticket_id, q.id, q.target_role, q.kind, 'discard', NULL, 'stall_premise_' || stale, NULL,
                        jsonb_build_object('reason', 'stall_premise_' || stale, 'episode', ep.id,
                                           'progress_at', q.payload->'progress_at'))
                    FROM ticket_board.ticket_notification_queue q WHERE q.id = ep.escalation_notification_id;
                    DELETE FROM ticket_board.ticket_notification_queue WHERE id = ep.escalation_notification_id;
                END IF;
                IF stale IS NOT NULL OR NOT EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                                                    WHERE q.id = ep.escalation_notification_id) THEN
                    PERFORM ticket_board.close_role_stall(ep.id, 'progress');
                    CONTINUE;
                END IF;
            END IF;
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

        -- When the work last reached the pane: the latest notice for this ticket sent to it in this round. Not
        -- only in this phase: an episode the owner's checkpoint closed opens again later, and the work it was
        -- handed is still the work (SYRD-675); a later phase restarts the clock below instead.
        SELECT max(tr.ts) INTO handed_at FROM ticket_board.notification_trace tr
        WHERE tr.ticket_id = cur.id AND tr.target_role = item.role
          AND tr.event IN ('send', 'send_unconfirmed') AND tr.ts >= coalesce(cur.round_at, ep.phase_started_at);
        SELECT min(en.ts) INTO handed_enqueued FROM ticket_board.notification_trace sent_tr
        JOIN ticket_board.notification_trace en ON en.notification_id = sent_tr.notification_id AND en.event = 'enqueue'
        WHERE sent_tr.ticket_id = cur.id AND sent_tr.target_role = item.role
          AND sent_tr.event IN ('send', 'send_unconfirmed') AND sent_tr.ts = handed_at;

        -- The one progress clock (SYRD-675), with the hand-over and the hook's own word: the latest of them is
        -- what an escalation is bound to, and delivery drops it once anything is later.
        -- A later phase -- the Director's hand-back, a replaced runtime -- is a recovery, and starts it again.
        -- SYRD-756: time the Director paused work for is not time without progress.
        clock := ticket_board.unpaused_since(greatest(handed_at, hook_updated, ticket_board.role_progress_at(cur.id, item.role, p_now),
                          CASE WHEN ep.phase_no > 1 THEN ep.phase_started_at END), p_now);
        why := NULL;
        IF busy_still AND hook_state IS DISTINCT FROM 'idle' AND handed_at IS NOT NULL
           AND turn_started IS NOT NULL AND turn_started >= coalesce(handed_enqueued, handed_at)
           AND p_now >= clock + busy_threshold THEN
            -- It took the work and has said busy since, with nothing moving and no progress for the board's stall
            -- threshold: it cannot be prompted again.
            why := 'busy_no_progress';
        ELSIF sent >= p_max_attempts AND (
                  (hook_state = 'idle' AND turn_started IS NOT NULL AND turn_started >= last_sent - interval '5 seconds'
                   AND hook_updated > last_sent)
                  OR p_now >= ticket_board.unpaused_since(last_sent, p_now) + p_grace) THEN
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
                               'progress_at', clock, 'round_at', cur.round_at,
                               'threshold_seconds', extract(epoch FROM busy_threshold)::integer,
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

CREATE OR REPLACE FUNCTION ticket_board.notify_unresolved_turn_end(
    p_turn_by_role jsonb,
    p_now timestamptz,
    -- How long the owner has to answer their own prompt before the Director is
    -- told. Staged recovery: one unresolved turn is one nudge to the person who
    -- can fix it, and an escalation only if that does not work (SYRD-203).
    p_grace interval DEFAULT interval '10 minutes',
    -- The owners' own turns, as their CLI hooks recorded them:
    -- {role: {state, updated_at, turn_started_at}} (SYRD-584). NULL, or a role
    -- missing from it, is "cannot tell", and the grace then runs from the
    -- prompt's delivery alone.
    p_owner_turns jsonb DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    enqueued integer := 0;
    identity text;
    escalate record;
    owner_reported boolean;
    owner_turn jsonb;
    turn_started timestamptz;
    owner_idle_at timestamptz;
    grace_from timestamptz;
    choices text[];
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_unresolved_turn_end');
    -- An empty map means no turn ended on this pass. That skips the prompting
    -- half and NOT the escalation half: the case the escalation exists for is
    -- an owner who has gone quiet, and a quiet owner produces no turn ends at
    -- all. Returning here is what would make the grace period unreachable
    -- (SYRD-203).
    FOR candidate IN
        SELECT * FROM (SELECT 1) AS _guard WHERE coalesce(p_turn_by_role, '{}'::jsonb) <> '{}'::jsonb
    LOOP
    END LOOP;

    FOR candidate IN
        SELECT
            t.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee)) AS turn_id
        FROM ticket_board.tickets t
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND p_turn_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
          -- Owned, active, and not resolved by any of the legitimate means --
          -- a recorded dependency among them, however long it has waited
          -- (SYRD-619).
          AND NOT ticket_board.ticket_turn_end_is_resolved(t.id, p_now)
          -- A lease the owner took for THIS turn, still open and not yet
          -- expired, is the one thing that buys silence here. An expired lease
          -- buys nothing, which is what stops it becoming a way to stall: the
          -- owner promised a next turn and none arrived.
          AND NOT EXISTS (
              SELECT 1
              FROM ticket_board.turn_continuation_lease l
              WHERE l.ticket_id = t.id
                AND l.owner_role = ticket_board.transition_target_role(t.state, t.assignee)
                AND l.turn_id = (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))
                AND l.consumed_at IS NULL
                AND l.expires_at > p_now
          )
        ORDER BY t.id
    LOOP
        identity := ticket_board.turn_unresolved_identity(
            candidate.id, candidate.state, candidate.assignee, candidate.turn_id
        );
        -- Reported once per turn identity, per audience. The marker is the
        -- dedupe key itself, which outlives the queue row, so acknowledging or
        -- delivering cannot cause a repeat for the same unresolved end
        -- (SYRD-133's lesson, applied to a different identity).
        --
        -- Per audience, because these are two notifications about one event and
        -- each has its own key: skipping the candidate on the Director's marker
        -- alone would silence the owner's repair prompt for a turn nobody ever
        -- prompted them about (SYRD-203).
        owner_reported := EXISTS (
            SELECT 1 FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = candidate.id
              AND tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
              AND tr.detail ->> 'dedupe_key' = 'repair:' || identity
        )
        -- One prompt per unresolved EPISODE, not per turn end (SYRD-538). The
        -- prompt wakes the owner's session, that woken turn ends with a new
        -- turn id, and keying on the turn alone prompted again: five prompts
        -- in four minutes on SYRD-537, each costing the owner a turn. An
        -- episode is the grace window of the latest prompt in the current
        -- assignment -- the same prompts and the same bound the escalation
        -- below counts -- so its clock is untouched, and a turn that ends
        -- unresolved after the grace starts a new episode, prompted and
        -- escalated as before.
            OR EXISTS (
                SELECT 1
                FROM ticket_board.notification_trace tr
                JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = tr.ticket_id
                WHERE tr.ticket_id = candidate.id
                  AND tr.kind = 'unresolved_turn_repair'
                  AND tr.event = 'enqueue'
                  AND left(tr.detail ->> 'dedupe_key', length('repair:' || ticket_board.turn_unresolved_identity(
                          candidate.id, candidate.state, candidate.assignee, '')))
                      = 'repair:' || ticket_board.turn_unresolved_identity(
                          candidate.id, candidate.state, candidate.assignee, '')
                  AND tr.ts >= ns.current_assignment_at
                  AND tr.ts > p_now - p_grace
                  -- Only a prompt still waiting for the owner, or one that
                  -- reached them, opens an episode: the loop is a DELIVERED
                  -- prompt waking the session. One dropped as stale (a hold,
                  -- SYRD-517) or removed undelivered never reached the owner,
                  -- so the next unresolved turn is prompted afresh.
                  AND (
                      EXISTS (
                          SELECT 1 FROM ticket_board.ticket_notification_queue pending
                          WHERE pending.id = tr.notification_id
                            AND pending.dead_lettered_at IS NULL
                      )
                      OR EXISTS (
                          SELECT 1 FROM ticket_board.notification_trace sent
                          WHERE sent.notification_id = tr.notification_id
                            AND sent.event IN ('send', 'send_unconfirmed')
                      )
                  )
                  -- Once the Director has been told, the episode is over: a
                  -- fresh unresolved turn prompts the owner again, under the
                  -- same rule the escalation half dedupes by (SYRD-194).
                  AND NOT EXISTS (
                      SELECT 1 FROM ticket_board.notification_trace told
                      WHERE told.ticket_id = tr.ticket_id
                        AND told.target_role = 'director'
                        AND told.kind IN ('escalation', 'unresolved_turn')
                        AND told.event = 'enqueue'
                        AND told.ts >= tr.ts
                  )
                  -- A continuation the owner took after the prompt is a new
                  -- promise: once the turn it covered has passed, the next
                  -- unresolved turn is prompted again (SYRD-194).
                  AND NOT EXISTS (
                      SELECT 1 FROM ticket_board.turn_continuation_lease promised
                      WHERE promised.ticket_id = tr.ticket_id
                        AND promised.granted_at >= tr.ts
                  )
                  -- So is a dependency recorded after it (SYRD-619): it
                  -- answered the prompt, and once it clears the next
                  -- unresolved turn is prompted afresh.
                  AND NOT coalesce(ns.wait_recorded_at >= tr.ts, false)
            );
        -- No combined skip here. Each audience is guarded by its own marker
        -- below, so a `CONTINUE` on both would be a third guard that can only
        -- ever agree with them -- and a guard no test can distinguish is one no
        -- reader can trust (the SYRD-159 lesson).
        -- The turn-end pass nudges the OWNER and nobody else. A turn that
        -- ended without resolving a ticket is first of all news for the person
        -- who can resolve it, and telling the Director in the same breath makes
        -- every ordinary forgotten turn an escalation. The Director is told by
        -- the second half of this function, and only when the prompt did not
        -- work (SYRD-203, correcting the immediate dual delivery this shipped
        -- with).
        -- The owner's single repair prompt: one per turn identity, on its own
        -- dedupe key. It is guarded by its OWN marker and by nothing the
        -- Director has heard -- news the Director already had used to silence
        -- the owner's only prompt to repair the turn, which was half of
        -- SYRD-203.
        IF NOT owner_reported THEN
        -- Only the moves this owner can make on this ticket, in this stage
        -- (SYRD-549). The fixed list offered a blocker only the Director can
        -- set, "continue work", which no role can reach, and a wait the stage
        -- or the role could not record.
        choices := ticket_board.unresolved_turn_choices(candidate.state, candidate.assignee, candidate.owner_role);
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'unresolved_turn_repair',
            candidate.owner_role,
            format(
                '%s is still yours and this turn ended without resolving it. %s The Director has NOT '
                || 'been told, and will be if it is still unresolved in %s.',
                candidate.id,
                ticket_board.unresolved_turn_repair_options(choices),
                p_grace
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn_repair',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'owner_role', candidate.owner_role,
                'target_role', candidate.owner_role,
                'turn_id', candidate.turn_id,
                'choices', to_jsonb(choices),
                'message', format(
                    '%s is still yours and this turn ended without resolving it.',
                    candidate.id
                ),
                'grace', extract(epoch from p_grace)
            ),
            'repair:' || identity
        );
        -- Counted here, because on this pass the owner's prompt IS the event.
        -- The escalation half counts its own, so a caller's number is "how many
        -- unresolved turns did this pass act on", either way.
        enqueued := enqueued + 1;
        END IF;
        -- Counted once per TICKET reported, not once per row: the two
        -- notifications are one event with two audiences.
    END LOOP;

    -- SECOND HALF: the escalation, staged behind the owner's prompt.
    --
    -- The Director hears about an unresolved turn only when the prompt did not
    -- work: the ticket is still unresolved and the owner has had the grace
    -- period to answer, or the prompt could not be delivered to them at all. It
    -- runs on every pass rather than on a turn-end, because "the owner did not
    -- answer in time" is a statement about elapsed time and there is no second
    -- turn-end to hang it on -- a silent owner produces no events at all, which
    -- is exactly the case this exists for.
    FOR escalate IN
        WITH prompted AS (
            SELECT
                tr.ticket_id AS id,
                tr.detail ->> 'dedupe_key' AS repair_key,
                min(tr.ts) AS prompted_at
            FROM ticket_board.notification_trace tr
            WHERE tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
            GROUP BY tr.ticket_id, tr.detail ->> 'dedupe_key'
        )
        SELECT
            prompted.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            prompted.repair_key,
            prompted.prompted_at,
            -- When the prompt actually reached the owner (SYRD-584). A prompt
            -- held behind a menu, a busy pane or a composer was not an answer
            -- the owner could give, so its grace cannot have started.
            (SELECT min(sent.ts)
             FROM ticket_board.notification_trace sent
             WHERE sent.event IN ('send', 'send_unconfirmed')
               AND sent.ts >= prompted.prompted_at
               AND sent.notification_id IN (
                   SELECT queued.notification_id
                   FROM ticket_board.notification_trace queued
                   WHERE queued.ticket_id = prompted.id
                     AND queued.kind = 'unresolved_turn_repair'
                     AND queued.event = 'enqueue'
                     AND queued.detail ->> 'dedupe_key' = prompted.repair_key
               )) AS delivered_at,
            EXISTS (
                SELECT 1 FROM ticket_board.ticket_notification_queue q
                WHERE q.dedupe_key = prompted.repair_key
                  AND q.dead_lettered_at IS NOT NULL
            ) AS undeliverable
        FROM prompted
        JOIN ticket_board.tickets t ON t.id = prompted.id
        JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND NOT ticket_board.ticket_turn_end_is_resolved(t.id, p_now)
          -- SYRD-619: a prompt the owner answered by recording a dependency
          -- cannot escalate, then or after the dependency clears. On SYRD-609
          -- the Director was told a prompt answered 32 seconds after it was
          -- sent was "still unresolved", four hours later.
          AND NOT coalesce(ns.wait_recorded_at >= prompted.prompted_at, false)
          -- Only a prompt from the CURRENT assignment can escalate (SYRD-514).
          -- A prompt from before a submission, a return or a reassignment is
          -- about work that has since been handed on; letting its grace run out
          -- after a Director or DAT return told the Director that the returned
          -- owner had ended a turn without resolving it, while that owner had
          -- ended no turn since and was working (MEFP-104, MEFP-106, SYRD-486).
          -- The key must name the current state and assignee (a prefix compare,
          -- no LIKE wildcards), and the prompt must be no older than the start
          -- of the current assignment -- which a same-state A -> B -> A
          -- reassignment moves even though the stage entry does not. A new
          -- unresolved turn in the current assignment is prompted afresh and
          -- escalates exactly as before.
          AND left(prompted.repair_key, length('repair:' || ticket_board.turn_unresolved_identity(t.id, t.state, t.assignee, '')))
              = 'repair:' || ticket_board.turn_unresolved_identity(t.id, t.state, t.assignee, '')
          AND prompted.prompted_at >= ns.current_assignment_at
        ORDER BY prompted.id
    LOOP
        -- SYRD-584: the grace is the owner's time to answer a prompt they
        -- have, so once the prompt is delivered it runs from the delivery, not
        -- the enqueue -- on Otto a prompt held behind a CLI menu had used most
        -- of its grace before it arrived. A prompt still waiting runs from its
        -- enqueue as before: a hold that outlasts the grace is still news. And
        -- the grace is the owner's time NOT working on it: a turn the owner
        -- started after the prompt cancels this wave while it runs, and once
        -- it ends unresolved the grace runs again from that end. And it runs
        -- from the owner's latest progress, on the one clock every stall alert
        -- reads (SYRD-675): a checkpoint, a delivered hand-over, a recovery, or
        -- a bounded child the owner runs -- which holds only up to its lease's
        -- deadline. An undeliverable prompt is still reported at once.
        IF NOT escalate.undeliverable THEN
            grace_from := coalesce(escalate.delivered_at, escalate.prompted_at);
            owner_turn := p_owner_turns -> escalate.owner_role;
            BEGIN
                turn_started := (owner_turn ->> 'turn_started_at')::timestamptz;
                owner_idle_at := (owner_turn ->> 'updated_at')::timestamptz;
            EXCEPTION WHEN others THEN
                -- An instant nobody can read is "cannot tell", not a turn.
                turn_started := NULL;
                owner_idle_at := NULL;
            END;
            IF turn_started >= escalate.prompted_at THEN
                CONTINUE WHEN owner_turn ->> 'state' IS DISTINCT FROM 'idle';
                grace_from := greatest(grace_from, owner_idle_at);
            END IF;
            grace_from := greatest(grace_from, ticket_board.role_progress_at(escalate.id, escalate.owner_role, p_now));
            CONTINUE WHEN ticket_board.unpaused_since(grace_from, p_now) > p_now - p_grace;  -- SYRD-756: paused time does not count
        END IF;
        identity := replace(escalate.repair_key, 'repair:', '');
        -- Not at all if the Director has already been told about this
        -- ticket since the owner was prompted -- whether that telling is still
        -- queued or has been delivered and acknowledged. Reading only the queue
        -- made an acknowledged escalation invisible, so this generator followed
        -- the reminder path with the same news a moment later: the Director is
        -- owed one notification about a ticket, not one per generator.
        CONTINUE WHEN EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = escalate.id
              AND q.target_role = 'director'
              AND q.kind IN ('escalation', 'unresolved_turn')
        ) OR EXISTS (
            SELECT 1
            FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = escalate.id
              AND tr.target_role = 'director'
              AND tr.kind IN ('escalation', 'unresolved_turn')
              AND tr.event = 'enqueue'
              AND tr.ts >= escalate.prompted_at
        );
        PERFORM ticket_board.enqueue_notification(
            escalate.id,
            'unresolved_turn',
            'director',
            format(
                '%s ended a turn without resolving %s (%s), and %s after being prompted it is '
                || 'still unresolved.',
                escalate.owner_role, escalate.id, escalate.state,
                CASE WHEN escalate.undeliverable
                     THEN 'the prompt could not be delivered'
                     ELSE 'the grace period has passed' END
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn',
                'id', escalate.id,
                'state', escalate.state,
                'assignee', escalate.assignee,
                'owner_role', escalate.owner_role,
                'target_role', 'director',
                'undeliverable', escalate.undeliverable,
                'prompted_at', escalate.prompted_at,
                'delivered_at', escalate.delivered_at,
                'grace_from', CASE WHEN escalate.undeliverable THEN NULL ELSE grace_from END,
                'message', format(
                    '%s is still unresolved after %s was prompted.',
                    escalate.id, escalate.owner_role
                )
            ),
            identity
        );
        enqueued := enqueued + 1;
    END LOOP;
    RETURN enqueued;
END;
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
    v_clock := ticket_board.unpaused_since(greatest(st.runnable_since, coalesce(v_last.at, st.runnable_since)), p_now);  -- SYRD-756
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

CREATE OR REPLACE FUNCTION ticket_board.notify_pull_idle_capacity(p_idle jsonb, p_now timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb:=ticket_board.declared_scheduling(); waiting record; free jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_pull_idle_capacity');
    IF policy IS NULL THEN RETURN 0; END IF;
    SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb) INTO free
      FROM jsonb_each_text(coalesce(p_idle,'{}'::jsonb)) AS e(k, v)
     -- Only a declared claimant is idle capacity; the listener names the
     -- running ones (SYRD-573), and nothing else it might send is believed.
     WHERE k IN (SELECT ticket_board.pull_claimant_pool(ticket_board.declared_workflow()))
       AND ticket_board.ticket_current_reserved_ticket(k) IS NULL;
    IF free='{}'::jsonb THEN RETURN 0; END IF;
    SELECT t.id, t.state, t.assignee, ns.entered_current_state_at AS since INTO waiting
      FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
     WHERE t.state=policy->>'ready_stage'
       AND ticket_board.unpaused_since(ns.entered_current_state_at, p_now) <= p_now - make_interval(secs => coalesce((policy->>'idle_alert_seconds')::int, 900))
       AND EXISTS (SELECT FROM jsonb_object_keys(free) k WHERE ticket_board.ticket_ready_for_pull(t.id, k))
     ORDER BY ns.entered_current_state_at, t.ticket_number LIMIT 1;
    IF waiting.id IS NULL THEN RETURN 0; END IF;
    -- Once per episode, delivered or not: the queue's own dedupe forgets a
    -- row once it is acknowledged; the trace of its enqueue does not.
    IF EXISTS (SELECT FROM ticket_board.notification_trace tr
               WHERE tr.ticket_id=waiting.id AND tr.event='enqueue'
                 AND tr.detail->>'dedupe_key'='pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint) THEN
        RETURN 0; END IF;
    PERFORM ticket_board.enqueue_notification(
        waiting.id, 'ticket_update', 'director',
        format('%s has been ready to claim since %s, and these implementers are idle without claiming it: %s.',
               waiting.id, to_char(waiting.since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'),
               (SELECT string_agg(k || ' (' || v || ')', ', ' ORDER BY k) FROM jsonb_each_text(free) AS e(k, v))),
        jsonb_build_object('kind', 'pull_idle_capacity', 'id', waiting.id,
                           'state', waiting.state, 'assignee', waiting.assignee, 'target_role', 'director',
                           'idle', free, 'ready_since', waiting.since),
        'pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint);
    RETURN 1;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_pull_pinned_rework_strands(p_idle jsonb, p_unavailable jsonb,
                                                                          p_now timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb := ticket_board.declared_scheduling(); strands jsonb; free jsonb; strand record; sent integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_pull_pinned_rework_strands');
    SELECT coalesce(jsonb_agg(jsonb_build_object('id', t.id, 'author', btrim(t.queued_for_assignee))), '[]'::jsonb)
      INTO strands
      FROM ticket_board.tickets t
     WHERE t.state = policy->>'ready_stage'
       AND btrim(t.queued_for_assignee) <> ''
       AND ticket_board.ticket_ready_for_pull(t.id, btrim(t.queued_for_assignee))
       AND ticket_board.ticket_current_reserved_ticket(btrim(t.queued_for_assignee)) IS NULL
       AND NOT coalesce(p_idle, '{}'::jsonb) ? btrim(t.queued_for_assignee);
    DELETE FROM ticket_board.pull_pinned_rework_strands s
     WHERE NOT EXISTS (SELECT FROM jsonb_to_recordset(strands) AS c(id text, author text)
                        WHERE c.id = s.ticket_id AND c.author = s.author);
    SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb) INTO free
      FROM jsonb_each_text(coalesce(p_idle, '{}'::jsonb)) AS e(k, v)
     -- Idle and able to take work: what pull_pickup says of a running claimant that found nothing to claim.
     WHERE v = 'idle, nothing claimable';
    INSERT INTO ticket_board.pull_pinned_rework_strands AS s (ticket_id, author, since, reason, idle)
    SELECT c.id, c.author, p_now,
           coalesce(nullif(p_unavailable->>c.author, ''), 'it did not claim it on the listener''s last pass'), free
      FROM jsonb_to_recordset(strands) AS c(id text, author text)
    ON CONFLICT (ticket_id) DO UPDATE SET reason = EXCLUDED.reason, idle = EXCLUDED.idle;
    IF free = '{}'::jsonb THEN
        RETURN 0;
    END IF;
    FOR strand IN
        SELECT s.*, t.state, t.assignee, btrim(t.queued_behind_ticket) AS behind
          FROM ticket_board.pull_pinned_rework_strands s JOIN ticket_board.tickets t ON t.id = s.ticket_id
         WHERE s.notified_at IS NULL
           AND ticket_board.unpaused_since(s.since, p_now) <= p_now - make_interval(secs => coalesce((policy->>'idle_alert_seconds')::int, 900))
         ORDER BY s.since, t.ticket_number
    LOOP
        UPDATE ticket_board.pull_pinned_rework_strands SET notified_at = p_now WHERE ticket_id = strand.ticket_id;
        PERFORM ticket_board.enqueue_notification(
            strand.ticket_id, 'ticket_update', 'director',
            format('%s is rework pinned to %s, and %s has not been able to take it since %s: %s holds no ticket now%s, '
                   'but %s. These implementers are idle and cannot claim it while it is pinned: %s. Start %s, or move '
                   '%s out of %s and route it to one of them.',
                   strand.ticket_id, strand.author, strand.author,
                   to_char(strand.since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'), strand.author,
                   CASE WHEN strand.behind <> '' THEN ' (it was queued behind ' || strand.behind || ')' ELSE '' END,
                   strand.reason,
                   (SELECT string_agg(k || ' (' || v || ')', ', ' ORDER BY k) FROM jsonb_each_text(free) AS e(k, v)),
                   strand.author, strand.ticket_id, policy->>'ready_stage'),
            -- queued_for and reserved_by are the queue's own identity: the listener drops this notice when the
            -- ticket's pin has moved on before it is delivered (SYRD-108), as it does a queue announcement.
            jsonb_build_object('kind', 'pull_pinned_rework', 'id', strand.ticket_id, 'state', strand.state,
                               'assignee', strand.assignee, 'target_role', 'director',
                               'queued_for', strand.author, 'reserved_by', strand.behind,
                               'reason', strand.reason, 'idle', free, 'stranded_since', strand.since),
            'pull-pinned-rework:' || strand.ticket_id || ':' || strand.author || ':'
                || extract(epoch FROM strand.since)::bigint);
        sent := sent + 1;
    END LOOP;
    RETURN sent;
END;
$$;

COMMIT;
