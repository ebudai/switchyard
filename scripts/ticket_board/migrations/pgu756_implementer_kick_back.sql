INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override)
VALUES
    ('in_progress', 'analysis', 'implementer_kick_back', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false)
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;

CREATE OR REPLACE FUNCTION ticket_board.implementer_kick_back(
    id text,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    comment_actor text;
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('implementer_kick_back', id);
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'implementer_kick_back requires a non-empty reason';
    END IF;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_reason);
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'director',
        parked = false
    WHERE tickets.id = implementer_kick_back.id
      AND tickets.state = 'in_progress';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'in_progress ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.implementer_kick_back(text, text) TO ticket_board_service;
