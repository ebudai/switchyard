-- SYRD-696: a ticket waiting on the User is not runnable work, however long it waits.
--
-- SYRD-691 sat in Implementation with Ops waiting on the User's approval of an install packet. Four hours after
-- each wait was recorded, the board stall alert counted it as runnable, and an hour later the Director was sent
-- "Board stall: runnable work has made no durable progress" twice (episodes 7 and 8). board_stall_runnable
-- excluded a wait only while ticket_awaiting_role_is_active, whose four-hour window is the pane-role handoff's
-- (0, 5, 15 and 30 minutes, then the Director; PGU-453's idle nudges resume after it). A wait on the User sends
-- nothing (SYRD-549) and has no such window: when it lapsed, the ticket was counted for its owner, who can do
-- nothing until the User answers.
--
--   * board_stall_runnable excludes a ticket with a recorded wait on a role of the User's kind for as long as the
--     wait stands -- 'waiting on <role>, the User, until the wait is cleared' -- as it already excludes work in the
--     User's own stage. Clearing the wait is progress (board_stall_dependency_progress records it), so a cleared
--     ticket counts again from then, with its own hand-over time unchanged.
--   * Unchanged: a wait on a pane role, excluded while its handoff's window is open; the per-ticket role stall
--     (SYRD-595), which already excludes any recorded wait; and every other exclusion and counted stage.
--
-- Such a wait reaches a board two ways: the Director's await-user in a stage the workflow's user_waits lists, and a
-- wait recorded before SYRD-662, which an upgrade keeps (user_wait_status reports it; the board moves nothing).
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
