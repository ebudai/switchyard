-- SYRD-549: a declared workflow's roles could not record the waits it gives
-- them. On otto (release 2951c5c9) the declared `designer`, owning a Draft
-- ticket and waiting on the User's answers, was told by its turn-end prompt to
-- `request-dependency`. It did, `--role user`, and nothing was recorded: the
-- User has no pane target, and Draft was not a stage a wait could be recorded
-- in. At grace expiry the Director was told the turn was unresolved, after the
-- User had answered.
--
-- An owned Draft stage now admits a wait (workflow_wait_stage). The declared
-- User may be awaited: the wait is recorded and mutes the owner's reminders,
-- and nothing is sent, because the User has no pane (wait_target_kind,
-- set_awaiting_role, enqueue_awaiting_role_handoff). The owner's turn-end
-- prompt names only the moves that owner can make on that ticket in that stage
-- (unresolved_turn_choices, unresolved_turn_repair_options, and the guard that
-- sends it). Admission to record a wait was already the caller's own
-- `await_role` capability under a declared workflow; that is unchanged.
-- Signatures are unchanged, so no grant moves. schema.sql carries the same
-- text.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.workflow_wait_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN stage IN ('in_progress','inspection','audit','user_review')
 ELSE EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s WHERE s->>'name'=stage AND NOT (s->>'terminal')::boolean
  -- SYRD-549: owned Draft work can wait like any other; a draft stage that
  -- nobody owns (Backlog) holds no work to wait.
  AND (s->>'kind'<>'draft' OR jsonb_array_length(coalesce(s->'owners','[]'::jsonb))>0)) END;
$$;

-- SYRD-549: who a wait may name, and how they are reached.
--   'pane':     a role with a pane, handed the wait by the awaiting-role
--               schedule;
--   'external': the declared User, who has no pane and no process authority.
--               The wait is recorded and mutes the owner's reminders, and
--               nothing is sent: a notice to a role with no pane is requeued
--               for ever, and the Director being told the User has not
--               answered is the non-decision SYRD-273 ended;
--   NULL:       not a role a wait may name.
CREATE OR REPLACE FUNCTION ticket_board.wait_target_kind(p_role text)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        RETURN CASE WHEN p_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research')
                    THEN 'pane' END;
    END IF;
    RETURN (SELECT CASE WHEN r.definition->>'target' IS NOT NULL THEN 'pane'
                        WHEN r.definition->>'kind' = 'user' THEN 'external' END
            FROM ticket_board.workflow_roles r
            WHERE r.name = p_role AND (r.definition->>'active')::boolean);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF NOT ticket_board.workflow_wait_stage(t.state)
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    -- SYRD-148: the same rule as the legacy definition above. This is the one
    -- a board with a declared workflow actually runs, and it overrides that
    -- one, so a guard added only there is a guard this board never reaches.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;
    -- SYRD-549: a wait on the User is recorded and sends nothing; see
    -- wait_target_kind. Marked handed over, so a repeated request is too.
    IF ticket_board.wait_target_kind(ns.awaiting_role) = 'external' THEN
        UPDATE ticket_board.ticket_notification_state
        SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    -- The list is the legacy board's. Under a declared workflow require_actor
    -- admits by the caller's own `await_role` capability and ignores it, so a
    -- custom-named role holding the capability may record a wait (SYRD-549).
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    -- A pane role, or the declared User (SYRD-549).
    IF ticket_board.wait_target_kind(normalized_role) IS NULL THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- SYRD-148: an awaiting-role handoff is an actionable instruction to
    -- somebody else. A ticket whose own dependency has not resolved cannot
    -- give one: the named role would be told to act on work that cannot
    -- proceed, and after thirty minutes the Director would be told they had
    -- ignored it. Refused rather than accepted quietly, and the blockers are
    -- named so the caller knows what has to move first.
    IF ticket_board.ticket_has_unresolved_blockers(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents an awaiting-role handoff: %',
            ticket_board.unresolved_blocker_list(set_awaiting_role.id);
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND ticket_board.workflow_wait_stage(t.state)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND ticket_board.workflow_wait_stage(t.state);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;

-- SYRD-549: what the owner of an unresolved turn may do about it, from its
-- own authority on this ticket in this stage, by the rules the moves
-- themselves check:
--   'transition':         a declared transition out of the stage that names
--                         the owner (transition_allows_actor, and owner_scoped
--                         as the assignee);
--   'request-dependency': the owner holds add_comment and await_role, and the
--                         stage is one a wait may be recorded in;
--   'blocker':            the owner holds set_blockers.
-- "Continue work" is not among them: no route reaches grant_turn_continuation.
-- A legacy board keeps its fixed rules: the owner's move, a wait for any role
-- but the User in a wait stage, and blockers for the Director.
CREATE OR REPLACE FUNCTION ticket_board.unresolved_turn_choices(p_state text, p_assignee text, p_owner text)
RETURNS text[]
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    cfg jsonb := ticket_board.declared_workflow();
    caps jsonb;
    choices text[] := ARRAY[]::text[];
BEGIN
    IF cfg IS NULL THEN
        choices := ARRAY['transition'];
        IF ticket_board.workflow_wait_stage(p_state) AND p_owner <> 'user' THEN
            choices := choices || 'request-dependency'::text;
        END IF;
        IF p_owner = 'director' THEN
            choices := choices || 'blocker'::text;
        END IF;
        RETURN choices;
    END IF;
    SELECT r.definition->'capabilities' INTO caps
    FROM ticket_board.workflow_roles r
    WHERE r.name = p_owner AND (r.definition->>'active')::boolean;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'transitions') tr
        WHERE tr->>'from' = p_state
          AND ticket_board.transition_allows_actor(cfg, tr, p_owner)
          AND (NOT coalesce((tr->>'owner_scoped')::boolean, false) OR p_owner = p_assignee)
    ) THEN
        choices := choices || 'transition'::text;
    END IF;
    IF ticket_board.workflow_wait_stage(p_state) AND coalesce(caps ?& ARRAY['add_comment', 'await_role'], false) THEN
        choices := choices || 'request-dependency'::text;
    END IF;
    IF coalesce(caps ? 'set_blockers', false) THEN
        choices := choices || 'blocker'::text;
    END IF;
    RETURN choices;
END;
$$;

-- SYRD-549: the sentence of the owner's repair prompt that names what to do,
-- naming only `p_choices`. The guard writes the rest, as it always has.
CREATE OR REPLACE FUNCTION ticket_board.unresolved_turn_repair_options(p_choices text[])
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    options text[];
BEGIN
    SELECT coalesce(array_agg(CASE c
               WHEN 'transition' THEN 'transition it if the work is done'
               WHEN 'request-dependency' THEN '`request-dependency` if it is waiting on another role'
                   || CASE WHEN EXISTS (SELECT FROM ticket_board.workflow_roles r
                                        WHERE ticket_board.wait_target_kind(r.name) = 'external')
                           THEN ' or on the User' ELSE '' END
               WHEN 'blocker' THEN 'record a blocker if something else must land first'
           END ORDER BY n) FILTER (WHERE c IN ('transition', 'request-dependency', 'blocker')), ARRAY[]::text[])
    INTO options
    FROM unnest(p_choices) WITH ORDINALITY AS u(c, n);
    RETURN CASE cardinality(options)
               WHEN 0 THEN 'No move on it from this stage is yours to make: say in a comment what it needs.'
               WHEN 1 THEN 'Do this: ' || options[1] || '.'
               ELSE 'Do one of: ' || array_to_string(options[1:cardinality(options) - 1], '; ')
                    || '; or ' || options[cardinality(options)] || '.'
           END;
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
          -- Owned, active, and not resolved by any of the legitimate means.
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
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
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
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
        -- it ends unresolved the grace runs again from that end. Turns are read
        -- from the owner's CLI hooks alone, so a background child left running
        -- holds nothing here. An undeliverable prompt is still reported at once.
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
            CONTINUE WHEN grace_from > p_now - p_grace;
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

REVOKE ALL ON FUNCTION ticket_board.wait_target_kind(text),
    ticket_board.unresolved_turn_choices(text, text, text),
    ticket_board.unresolved_turn_repair_options(text[]) FROM PUBLIC;

COMMIT;
