CREATE OR REPLACE FUNCTION ticket_board.route(
    id text,
    new_state text,
    assignee text
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
BEGIN
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = route.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('route', id, current_assignee);
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;

    UPDATE ticket_board.tickets
    SET state = new_state,
        assignee = route.assignee,
        parked = false
    WHERE tickets.id = route.id;
    PERFORM ticket_board.touch_ticket(id);
    IF current_state IS NOT DISTINCT FROM new_state AND current_assignee IS DISTINCT FROM assignee THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'assignee');
    END IF;
END;
$$;

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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
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
