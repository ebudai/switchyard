-- SYRD-735 (pgu1030): a ticket whose owner's provider is out of quota is not runnable work for the board stall alert.
--
-- On 2026-11-05 App's Claude account hit its weekly limit and App was held (SYRD-644) until the reset on November 7,
-- with SYRD-734 assigned to it so that the hold would deliver it then. The board stall evaluator still counted the
-- ticket as runnable with App idle, opened episode 15, and sent the Director notification 8897 an hour later: it never
-- asked about holds. The reminders, unresolved-turn prompts and role stalls already ask role_provider_hold_is_current
-- for the ticket's owner (ticket_turn_is_resolved); board_stall_runnable now asks it too, and says why the ticket is
-- not runnable. A released hold still counts during its one attempt's window (ten minutes at most), as it does for
-- them. After that the ticket is counted again as before the hold: a held hand-over delivered at the release is
-- progress (board_stall_delivery_progress), the hold's own recheck is not, so an owner that makes no durable progress
-- after its attempt is reported as any stalled owner is.
--
-- schema.sql carries the same function.

BEGIN;

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
                -- SYRD-696: a recorded wait on the User is the User's next action for as long as it stands, as the
                -- User's own stage is: no role on this board can move it, and nothing makes it due. A wait on a pane
                -- role keeps its handoff's window, below.
                WHEN kinds->>nullif(r.awaiting_role, '') = 'user' THEN
                    'waiting on ' || r.awaiting_role || ', the User, until the wait is cleared'
                WHEN ticket_board.ticket_awaiting_role_is_active(r.awaiting_role, r.awaiting_since_at, p_now) THEN
                    'waiting on ' || r.awaiting_role
                -- SYRD-735: an owner whose provider is out of quota cannot move it until the reset, when its hold
                -- delivers the work; the hold's own notice told the Director (SYRD-644). The question the reminders ask.
                WHEN ticket_board.role_provider_hold_is_current(responsible, p_now) THEN
                    responsible || '''s provider is out of quota, and its work waits for the reset'
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

COMMIT;
