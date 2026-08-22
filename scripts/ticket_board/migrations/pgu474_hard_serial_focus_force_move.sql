-- Keep director force-move from silently stacking multiple in_progress tickets per implementer.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.force_move(
    id text,
    new_state text,
    assignee text,
    suppress_notification boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_state text;
    current_assignee text;
    target_state text;
    serial_focus_queued boolean := false;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'force_move');
    IF new_state NOT IN ('backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'user_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'agent', 'director', 'research') THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = force_move.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    target_state := new_state;
    IF new_state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(force_move.assignee)
       AND ticket_board.ticket_current_reserved_ticket(force_move.assignee, force_move.id) IS NOT NULL THEN
        target_state := 'backlog';
        serial_focus_queued := true;
    END IF;

    PERFORM set_config('ticket_board.force_move', 'on', true);
    IF suppress_notification THEN
        PERFORM set_config('ticket_board.suppress_transition_notify', 'on', true);
    END IF;
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director override: '
        || current_state
        || '/'
        || current_assignee
        || ' -> '
        || new_state
        || '/'
        || assignee
        || CASE
            WHEN serial_focus_queued THEN ' (queued: implementer already has active work)'
            ELSE ''
        END
        || CASE WHEN suppress_notification THEN ' (notification suppressed)' ELSE '' END
    );

    UPDATE ticket_board.tickets
    SET state = target_state,
        assignee = force_move.assignee,
        parked = false
    WHERE tickets.id = force_move.id;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT suppress_notification
       AND current_state IS NOT DISTINCT FROM target_state
       AND current_assignee IS DISTINCT FROM assignee THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'assignee');
    END IF;
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.force_move(text, text, text, boolean) TO ticket_board_service;
    END IF;
END;
$$;

COMMIT;
