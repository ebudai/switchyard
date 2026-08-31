CREATE OR REPLACE FUNCTION ticket_board.stage_entry_assignee(p_state text, p_current_assignee text, p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN cardinality(owner_roles) = 1 THEN owner_roles[1]
        WHEN p_state = 'audit'
             AND cardinality(owner_roles) > 1
             AND NULLIF(btrim(coalesce(p_current_assignee, '')), 'unassigned') = ANY(owner_roles)
            THEN NULLIF(btrim(coalesce(p_current_assignee, '')), 'unassigned')
        WHEN p_state = 'audit' AND cardinality(owner_roles) > 1 THEN
            owner_roles[
                ((coalesce((substring(p_ticket_id FROM '^[A-Z][A-Z0-9]*-([0-9]+)$'))::integer, 1) - 1)
                % cardinality(owner_roles)) + 1
            ]
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
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;
    RETURN NEW;
END;
$$;
