-- Backlog tickets stay parked when unblocked; notify only active workflow owners.

CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT ticket_board.transition_target_role(p_state, p_assignee);
$$;
