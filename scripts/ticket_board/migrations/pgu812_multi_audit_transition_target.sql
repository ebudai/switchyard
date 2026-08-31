CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN (
            SELECT CASE
                WHEN NULLIF(p_assignee, 'unassigned') = ANY(ws.owner_roles) THEN NULLIF(p_assignee, 'unassigned')
                WHEN cardinality(ws.owner_roles) = 1 THEN ws.owner_roles[1]
                ELSE NULL
            END
            FROM ticket_board.workflow_stages ws
            WHERE ws.name = 'audit'
        )
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN NULL
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;
