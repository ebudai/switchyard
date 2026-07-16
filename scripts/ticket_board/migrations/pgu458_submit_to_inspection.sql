CREATE OR REPLACE FUNCTION ticket_board.submit_to_inspection(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_state text;
    ticket_assignee text;
    ticket_needs_inspection boolean;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'submit_to_inspection');
    actor := ticket_board.current_app_actor();
    SELECT tickets.state, tickets.assignee, tickets.needs_inspection
    INTO ticket_state, ticket_assignee, ticket_needs_inspection
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_inspection.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'submit_to_inspection requires an in_progress ticket';
    END IF;
    IF ticket_assignee <> actor THEN
        RAISE EXCEPTION '% cannot submit_to_inspection for ticket assigned to %', actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    IF NOT ticket_needs_inspection THEN
        RAISE EXCEPTION 'submit_to_inspection requires needs_inspection=true';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'inspection',
        assignee = 'inspector'
    WHERE tickets.id = submit_to_inspection.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.submit_to_inspection(text) TO ticket_board_service;
    END IF;
END;
$$;
