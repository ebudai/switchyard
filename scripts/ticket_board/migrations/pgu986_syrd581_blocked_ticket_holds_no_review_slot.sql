-- SYRD-581: on MEFP (release f92a3d4d, workflow revision 115) Audit sat idle
-- while MEFP-593..595 waited as "finish current". MEFP-529 had been in Audit
-- since 2026-10-07, blocked on MEFP-665. The listener drops a blocked
-- ticket's notice, so it was never handed over -- yet serial review admission
-- (finish_current_stage_blocker) counted it as Audit's current ticket, being
-- the earliest arrival, and held every later Audit ticket behind it. SYRD-542's
-- idle release (pgu975) asks the same check, so it released nothing either.
--
-- A blocked ticket is now nobody's current ticket, as SYRD-568 already made
-- it for an implementer's slot. So that unblocking it cannot hand the role a
-- second ticket, the ticket handed over most recently in the stage (a
-- transition notice sent, as the trace records it) is the current one; before
-- anything is handed over, arrival order decides, as before. The
-- implementation-stage branch is unchanged. schema.sql carries the same text.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.finish_current_stage_blocker(
    p_ticket_id text,
    p_target_role text,
    p_state text,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    blocker_ticket_id text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('finish_current_stage_blocker');

    -- SYRD-568: in an implementation stage the board already decided whose
    -- turn it is -- the implementer's active ticket, the only one ever handed
    -- over (settle_serial_focus). Ordering by arrival here instead held an
    -- active ticket's notice behind tickets queued or blocked before it.
    IF ticket_board.declared_workflow() IS NOT NULL AND ticket_board.declared_stage_kind(p_state) = 'implementation' THEN
        -- Manual control is the Director's own hold, never admitted behind
        -- anything, exactly as the arrival order below never held it.
        IF EXISTS (SELECT FROM ticket_board.tickets t WHERE t.id = p_ticket_id AND t.manually_controlled) THEN
            RETURN '';
        END IF;
        blocker_ticket_id := ticket_board.ticket_current_reserved_ticket(p_target_role);
        RETURN CASE WHEN blocker_ticket_id IS NULL OR blocker_ticket_id = p_ticket_id THEN '' ELSE blocker_ticket_id END;
    END IF;

    -- SYRD-581: the role's current ticket is the one last handed to it in this
    -- stage -- a transition notice actually sent, as the trace records it; a
    -- dropped notice is acknowledged too, so last_transition_notified_at cannot
    -- say -- and before anything is handed over, the earliest arrival. A
    -- blocked ticket is nobody's current ticket, as SYRD-568 made it for an
    -- implementer's slot: the listener drops its notice, so it is never handed
    -- over, and counting it held every later ticket for good. On MEFP,
    -- MEFP-529 sat in Audit blocked on another ticket, and MEFP-593..595
    -- waited behind it as "finish current" while Audit was idle. When it
    -- unblocks, it waits behind the ticket handed over meanwhile.
    WITH stage_tickets AS (
        SELECT
            t.id,
            t.ticket_number,
            ns.entered_current_state_at,
            (SELECT max(nt.ts)
             FROM ticket_board.notification_trace nt
             WHERE nt.ticket_id = t.id
               AND nt.target_role = p_target_role
               AND nt.ticket_state_at_event = p_state
               AND nt.kind = 'transition'
               AND nt.event = 'send'
               AND nt.ts >= ns.entered_current_state_at) AS handed_over_at
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.state = p_state
          AND t.assignee = p_target_role
          AND NOT t.manually_controlled
    ),
    current_ticket AS (
        SELECT * FROM stage_tickets WHERE id = p_ticket_id
    ),
    candidates AS (
        SELECT *
        FROM stage_tickets s
        WHERE s.id <> p_ticket_id
          AND NOT ticket_board.ticket_has_unresolved_blockers(s.id)
    ),
    blocker_candidates AS (
        SELECT b.*
        FROM current_ticket c
        JOIN candidates b ON (
            -- Handed over in this stage: the latest handover is the work under way.
            (
                b.handed_over_at IS NOT NULL
                AND (
                    c.handed_over_at IS NULL
                    OR b.handed_over_at > c.handed_over_at
                    OR (b.handed_over_at = c.handed_over_at AND b.ticket_number < c.ticket_number)
                )
            )
            -- Nothing handed over to either: arrival order, as before.
            OR (
                b.handed_over_at IS NULL
                AND c.handed_over_at IS NULL
                AND (
                    (
                        b.entered_current_state_at IS NOT NULL
                        AND c.entered_current_state_at IS NOT NULL
                        AND b.entered_current_state_at < c.entered_current_state_at
                    )
                    OR (
                        (
                            b.entered_current_state_at IS NULL
                            OR c.entered_current_state_at IS NULL
                            OR b.entered_current_state_at = c.entered_current_state_at
                        )
                        AND b.ticket_number < c.ticket_number
                    )
                )
            )
        )
    )
    SELECT b.id
    INTO blocker_ticket_id
    FROM blocker_candidates b
    WHERE NOT (
        EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
            ORDER BY q.next_attempt_at, q.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
    )
    ORDER BY
        b.handed_over_at DESC NULLS LAST,
        CASE
            WHEN b.entered_current_state_at IS NOT NULL
                THEN b.entered_current_state_at
            ELSE NULL
        END NULLS LAST,
        b.ticket_number
    LIMIT 1;

    RETURN coalesce(blocker_ticket_id, '');
END;
$$;

COMMIT;
