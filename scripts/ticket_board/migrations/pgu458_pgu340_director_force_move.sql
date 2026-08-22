BEGIN;

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_in_progress_assignee_check;

DO $$
DECLARE
    fn text;
BEGIN
    fn := pg_get_functiondef('ticket_board.enforce_ticket_workflow_update()'::regprocedure);
    IF position('ticket_board.force_move' in fn) = 0 THEN
        fn := replace(
            fn,
            $old$
    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
$old$,
            $new$
    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
$new$
        );
        IF position('ticket_board.force_move' in fn) = 0 THEN
            RAISE EXCEPTION 'could not patch enforce_ticket_workflow_update for PGU-340';
        END IF;
        EXECUTE fn;
    END IF;

    fn := pg_get_functiondef('ticket_board.notify_ticket_state_transition()'::regprocedure);
    IF position('ticket_board.suppress_transition_notify' in fn) = 0 THEN
        fn := replace(
            fn,
            $old$
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
$old$,
            $new$
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF current_setting('ticket_board.suppress_transition_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
$new$
        );
        IF position('ticket_board.suppress_transition_notify' in fn) = 0 THEN
            RAISE EXCEPTION 'could not patch notify_ticket_state_transition for PGU-340';
        END IF;
        EXECUTE fn;
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
        || CASE WHEN suppress_notification THEN ' (notification suppressed)' ELSE '' END
    );

    UPDATE ticket_board.tickets
    SET state = new_state,
        assignee = force_move.assignee,
        parked = false
    WHERE tickets.id = force_move.id;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT suppress_notification
       AND current_state IS NOT DISTINCT FROM new_state
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
