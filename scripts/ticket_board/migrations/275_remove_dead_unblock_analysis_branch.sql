-- Remove the unreachable analysis/unassigned fallback from unblock notifications.

CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT COALESCE(
        ticket_board.transition_target_role(p_state, p_assignee),
        CASE
            WHEN p_state = 'backlog' THEN 'director'
            ELSE NULL
        END
    );
$$;
