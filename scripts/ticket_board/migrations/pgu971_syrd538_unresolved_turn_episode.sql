-- SYRD-538: one unresolved-turn repair prompt per unresolved episode.
--
-- notify_unresolved_turn_end deduped the owner's repair prompt on the turn
-- identity, and every turn end is a new one. Delivering the prompt wakes the
-- owner's session; that woken turn ends unresolved under a new turn id, and is
-- prompted again -- every 35-70 s on SYRD-537, five times while one background
-- job ran. The escalation clock was not reset (it counts from the earliest
-- prompt of the current assignment), but every prompt cost the owner a turn.
--
-- Only notify_unresolved_turn_end changes, and only its prompt half: a ticket
-- whose current-assignment prompt is still queued or was delivered within the
-- grace window is not prompted again -- unless the Director has since been
-- told or the owner has since taken a continuation. A prompt dropped as stale
-- or removed undelivered opens no episode. The escalation half is unchanged.
BEGIN;

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
