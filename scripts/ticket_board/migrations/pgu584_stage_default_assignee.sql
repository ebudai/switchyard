CREATE OR REPLACE FUNCTION ticket_board.stage_default_assignee(p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state IN ('dat', 'eric_review', 'director_review') THEN 'director'
        ELSE NULL
    END;
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
       AND ticket_board.stage_default_assignee(NEW.state) IS NOT NULL THEN
        NEW.assignee := ticket_board.stage_default_assignee(NEW.state);
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzz_stage_default_assignee_update ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzz_stage_default_assignee_update
BEFORE UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.apply_stage_default_assignee_update();

UPDATE ticket_board.tickets
SET assignee = ticket_board.stage_default_assignee(state)
WHERE NOT coalesce(manually_controlled, false)
  AND ticket_board.stage_default_assignee(state) IS NOT NULL
  AND assignee IS DISTINCT FROM ticket_board.stage_default_assignee(state);
