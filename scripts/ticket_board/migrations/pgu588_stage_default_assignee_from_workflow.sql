CREATE OR REPLACE FUNCTION ticket_board.stage_default_assignee(p_state text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN cardinality(owner_roles) = 1 THEN owner_roles[1]
        ELSE NULL
    END
    FROM ticket_board.workflow_stages
    WHERE name = p_state;
$$;

UPDATE ticket_board.tickets
SET assignee = ticket_board.stage_default_assignee(state)
WHERE NOT coalesce(manually_controlled, false)
  AND ticket_board.stage_default_assignee(state) IS NOT NULL
  AND (
      state <> 'analysis'
      OR assignee IS NULL
      OR btrim(assignee) = ''
      OR assignee = 'unassigned'
  )
  AND assignee IS DISTINCT FROM ticket_board.stage_default_assignee(state);
