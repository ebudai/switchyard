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

CREATE OR REPLACE FUNCTION ticket_board.apply_stage_default_assignee_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state
       AND current_setting('ticket_board.force_move', true) IS DISTINCT FROM 'on'
       AND NOT coalesce(OLD.manually_controlled, false)
       AND NOT coalesce(NEW.manually_controlled, false)
       AND ticket_board.stage_default_assignee(NEW.state) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_default_assignee(NEW.state);
    END IF;
    RETURN NEW;
END;
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
