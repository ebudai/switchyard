-- SYRD-513: a reviewer's turn end was attributed to a ticket that serial review
-- admission was still holding back from that reviewer. MEFP-51's Audit notice
-- sat queued ("finish current") behind MEFP-53; Audit ended a turn on MEFP-53,
-- and notify_unresolved_turn_end prompted Audit about MEFP-51 and, after the
-- grace, told the Director Audit had left MEFP-51 unresolved. Audit had never
-- been handed it.
--
-- ticket_turn_is_resolved decides every unresolved-turn prompt, escalation and
-- their delivery (SYRD-517). It now also answers true while the ticket's
-- current-stage transition notice to its owner is still queued and not
-- dead-lettered. Once the notice is delivered the ticket is judged exactly as
-- before. Same signature, so the listener's grant stands.
BEGIN;

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

COMMIT;
