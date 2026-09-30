-- SYRD-514: record when a ticket really entered its stage and assignment, and
-- escalate an unresolved turn only for the current assignment.
--
-- 1. upsert_ticket_notification_state took a transition's time from the row's
--    updated_at/row_updated_at, which the workflow actions never touch. After
--    a submission, reviews and a return, entered_current_state_at still named
--    the last EDIT, stages earlier. A real state change or activation reset
--    now records clock_timestamp(). INSERT (seeded or imported history) and
--    every non-transition update behave as before.
-- 2. current_assignment_at, new, is the start of the current assignment: the
--    stage entry, or a change of assignee in the same stage. Existing rows
--    start from their stage entry; no existing column is rewritten.
-- 3. notify_unresolved_turn_end escalated any repair prompt ever enqueued for a
--    ticket once the ticket, as it is now, was owned and unresolved. It now
--    counts only a prompt for the current state and assignee enqueued since the
--    current assignment began (MEFP-104, MEFP-106, SYRD-486).
BEGIN;

ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS current_assignment_at timestamptz;
UPDATE ticket_board.ticket_notification_state
SET current_assignment_at = entered_current_state_at
WHERE current_assignment_at IS NULL;
ALTER TABLE ticket_board.ticket_notification_state
    ALTER COLUMN current_assignment_at SET DEFAULT clock_timestamp(),
    ALTER COLUMN current_assignment_at SET NOT NULL;

CREATE OR REPLACE FUNCTION ticket_board.upsert_ticket_notification_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.updated_at, NEW.row_updated_at, clock_timestamp());
    reset_idle_reminder boolean := false;
    activation_reset boolean := false;
    comment_touch boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_board.ticket_notification_state (
            ticket_id,
            current_state,
            current_assignee,
            previous_state,
            entered_current_state_at,
            current_assignment_at,
            last_activity_at,
            last_transition_notified_at,
            last_implementer_assignee
        ) VALUES (
            NEW.id,
            NEW.state,
            NEW.assignee,
            NULL,
            activity_at,
            activity_at,
            activity_at,
            activity_at,
            CASE
                WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
                ELSE ''
            END
        )
        ON CONFLICT (ticket_id) DO UPDATE
        SET current_state = EXCLUDED.current_state,
            current_assignee = EXCLUDED.current_assignee,
            previous_state = EXCLUDED.previous_state,
            entered_current_state_at = EXCLUDED.entered_current_state_at,
            current_assignment_at = EXCLUDED.current_assignment_at,
            last_activity_at = EXCLUDED.last_activity_at,
            last_transition_notified_at = EXCLUDED.last_transition_notified_at,
            last_nudged_at = NULL,
            nudge_count = 0,
            idle_reminder_count = 0,
            last_implementer_assignee = EXCLUDED.last_implementer_assignee;
        RETURN NULL;
    END IF;

    activation_reset := (
        (OLD.state = 'backlog' OR OLD.parked)
        AND NOT NEW.parked
        AND NEW.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
    );
    reset_idle_reminder := OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee OR activation_reset;
    comment_touch := current_setting('ticket_board.awaiting_role_comment_touch', true) = 'on';

    INSERT INTO ticket_board.ticket_notification_state (
        ticket_id,
        current_state,
        current_assignee,
        previous_state,
        entered_current_state_at,
        current_assignment_at,
        last_activity_at,
        last_transition_notified_at,
        last_nudged_at,
        nudge_count,
        last_implementer_assignee
    ) VALUES (
        NEW.id,
        NEW.state,
        NEW.assignee,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE NULL
        END,
        -- SYRD-514: a real transition happens NOW. activity_at is the row's own
        -- updated_at/row_updated_at, which the workflow actions do not touch, so
        -- it named the last edit -- often a stage or two earlier -- as the time
        -- the ticket entered the stage it was just moved into.
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN clock_timestamp()
            ELSE activity_at
        END,
        -- The start of the current assignment: the stage entry, or a change of
        -- assignee within the same stage. Distinct from the stage entry, which a
        -- same-state reassignment rightly does not move.
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee OR activation_reset
                THEN clock_timestamp()
            ELSE activity_at
        END,
        activity_at,
        NULL,
        NULL,
        0,
        CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ''
        END
    )
    ON CONFLICT (ticket_id) DO UPDATE
    SET current_state = EXCLUDED.current_state,
        current_assignee = EXCLUDED.current_assignee,
        previous_state = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE ticket_board.ticket_notification_state.previous_state
        END,
        entered_current_state_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN EXCLUDED.entered_current_state_at
            ELSE ticket_board.ticket_notification_state.entered_current_state_at
        END,
        current_assignment_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee OR activation_reset
                THEN EXCLUDED.current_assignment_at
            ELSE ticket_board.ticket_notification_state.current_assignment_at
        END,
        last_activity_at = EXCLUDED.last_activity_at,
        last_transition_notified_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_transition_notified_at
        END,
        last_nudged_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_nudged_at
        END,
        nudge_count = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN 0
            ELSE ticket_board.ticket_notification_state.nudge_count
        END,
        idle_reminder_count = CASE
            WHEN reset_idle_reminder THEN 0
            ELSE ticket_board.ticket_notification_state.idle_reminder_count
        END,
        awaiting_role = CASE
            WHEN reset_idle_reminder THEN ''
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN ''
            ELSE ticket_board.ticket_notification_state.awaiting_role
        END,
        awaiting_since_at = CASE
            WHEN reset_idle_reminder THEN NULL
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN NULL
            ELSE ticket_board.ticket_notification_state.awaiting_since_at
        END,
        last_implementer_assignee = CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ticket_board.ticket_notification_state.last_implementer_assignee
        END;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_unresolved_turn_end(
    p_turn_by_role jsonb,
    p_now timestamptz,
    -- How long the owner has to answer their own prompt before the Director is
    -- told. Staged recovery: one unresolved turn is one nudge to the person who
    -- can fix it, and an escalation only if that does not work (SYRD-203).
    p_grace interval DEFAULT interval '10 minutes'
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
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'unresolved_turn_repair',
            candidate.owner_role,
            format(
                '%s is still yours and this turn ended without resolving it. Do one of: '
                || 'transition it if the work is done; `request-dependency` if you need '
                || 'another role; record a blocker if something else must land first; or '
                || 'continue work, which covers the next turn only. The Director has NOT '
                || 'been told, and will be if it is still unresolved in %s.',
                candidate.id,
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
                'choices', jsonb_build_array(
                    'transition', 'request-dependency', 'blocker', 'continue'
                ),
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
        CONTINUE WHEN NOT (
            escalate.undeliverable OR escalate.prompted_at <= p_now - p_grace
        );
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

GRANT EXECUTE ON FUNCTION ticket_board.notify_unresolved_turn_end(jsonb, timestamptz, interval)
    TO ticket_board_listener;

COMMIT;
