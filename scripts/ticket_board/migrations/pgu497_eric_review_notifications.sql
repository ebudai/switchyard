CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'eric_review' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN ticket_board.state_rank(p_old_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) < ticket_board.state_rank(p_old_state)
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' kicked back to you'
        WHEN p_new_state = 'analysis'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'in_progress'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'inspection'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for inspection'
        WHEN p_new_state = 'audit'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for audit'
        WHEN p_new_state = 'eric_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for Eric UAT'
        WHEN p_new_state = 'director_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for your review'
        WHEN p_new_state = 'done'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' marked done'
        WHEN p_new_state = 'cancelled'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' cancelled'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.pending_transition_notifications()
RETURNS TABLE(ticket_id text, payload jsonb)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('pending_transition_notifications');
    RETURN QUERY
    SELECT
        t.id,
        ticket_board.transition_notification_payload(t.id, ns.previous_state, t.state, t.assignee)
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'eric_review', 'director_review')
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;
