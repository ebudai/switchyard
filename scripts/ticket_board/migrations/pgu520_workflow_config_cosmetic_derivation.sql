CREATE OR REPLACE FUNCTION ticket_board.state_rank(p_state text)
RETURNS integer
LANGUAGE sql
STABLE
AS $$
    SELECT rank
    FROM ticket_board.workflow_stages
    WHERE name = p_state;
$$;
