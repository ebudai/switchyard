-- SYRD-619: a recorded dependency answers the unresolved-turn prompt, for as long as it is recorded.
--
-- SYRD-609 waited in director_review on the User (`request-dependency`) for a
-- polkit dialog. Four hours later the Director was told "director ended a turn
-- without resolving SYRD-609 (director_review), and the grace period has passed
-- after being prompted it is still unresolved", then prompted again on every
-- turn end. Two causes:
--
-- - ticket_turn_is_resolved counts a wait only while
--   ticket_awaiting_role_is_active, which lapses after four hours. That lapse
--   is the idle nudges' design (PGU-453); a recorded dependency is still the
--   owner's answer, and its own path governs it (the awaiting-role handoff for
--   a pane role, the board's attention queue for the User, SYRD-549).
-- - The escalation re-raised the prompt the Director had answered by recording
--   the dependency, because nothing remembered that a wait came after it.
--
-- So: wait_recorded_at stamps when a wait was recorded and is never cleared;
-- ticket_turn_end_is_resolved adds a recorded wait to ticket_turn_is_resolved,
-- for the unresolved-turn generator and for delivery of its notices; and a
-- prompt older than the latest wait neither opens an episode nor escalates.
-- Idle nudges, stall episodes and their escalations keep their own rules.
--
-- schema.sql carries the same objects.

BEGIN;

ALTER TABLE ticket_board.ticket_notification_state ADD COLUMN IF NOT EXISTS wait_recorded_at timestamptz;

-- A wait recorded before this column existed was recorded when it says.
UPDATE ticket_board.ticket_notification_state
SET wait_recorded_at = awaiting_since_at
WHERE wait_recorded_at IS NULL AND awaiting_role <> '' AND awaiting_since_at IS NOT NULL;

-- Stamped by trigger, so every writer of a wait is covered: request_dependency,
-- set_awaiting_role in both definitions, and anything later. A new wait is a
-- role named where none was, or a different role; restating the same wait, or
-- rewriting when it began, is not a new answer.
CREATE OR REPLACE FUNCTION ticket_board.stamp_wait_recorded()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.awaiting_role <> '' AND NEW.awaiting_since_at IS NOT NULL
       AND (TG_OP = 'INSERT' OR OLD.awaiting_role IS DISTINCT FROM NEW.awaiting_role OR OLD.awaiting_since_at IS NULL) THEN
        NEW.wait_recorded_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ticket_notification_state_stamp_wait ON ticket_board.ticket_notification_state;
CREATE TRIGGER ticket_notification_state_stamp_wait
BEFORE INSERT OR UPDATE OF awaiting_role, awaiting_since_at ON ticket_board.ticket_notification_state
FOR EACH ROW EXECUTE FUNCTION ticket_board.stamp_wait_recorded();

-- The unresolved-turn question: resolved by any legitimate means, or waiting on
-- a recorded dependency however long it has waited. The generator and the
-- delivery check both ask this, so they cannot disagree (SYRD-517).
CREATE OR REPLACE FUNCTION ticket_board.ticket_turn_end_is_resolved(
    p_ticket text,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.ticket_turn_is_resolved(p_ticket, p_now)
        OR EXISTS (
            SELECT 1 FROM ticket_board.ticket_notification_state ns
            WHERE ns.ticket_id = p_ticket
              AND ns.awaiting_role <> ''
              AND ns.awaiting_since_at IS NOT NULL
        );
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


REVOKE ALL ON FUNCTION ticket_board.stamp_wait_recorded() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.ticket_turn_end_is_resolved(text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_turn_end_is_resolved(text, timestamptz) TO ticket_board_listener;

COMMIT;
