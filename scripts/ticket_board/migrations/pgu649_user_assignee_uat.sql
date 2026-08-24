ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN (
        'unassigned',
        'main',
        'app',
        'perf',
        'ops',
        'audit',
        'inspector',
        'agent',
        'director',
        'research',
        'user'
    ));

UPDATE ticket_board.workflow_stages
SET owner_roles = ARRAY['user']::text[]
WHERE name = 'user_review';

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
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN NULL
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_reserved_implementer(
    p_ticket_id text,
    p_state text,
    p_assignee text
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(p_assignee) THEN p_assignee
        WHEN p_state = 'user_review' AND p_assignee = 'user' THEN NULL
        WHEN p_state IN ('inspection', 'audit', 'dat', 'user_review', 'director_review') THEN (
            SELECT nullif(ns.last_implementer_assignee, '')
            FROM ticket_board.ticket_notification_state ns
            WHERE ns.ticket_id = p_ticket_id
              AND ticket_board.ticket_is_implementer_assignee(nullif(ns.last_implementer_assignee, ''))
        )
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_current_reserved_ticket(
    p_implementer text,
    p_excluding_ticket_id text DEFAULT NULL
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT t.id
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE ticket_board.ticket_is_implementer_assignee(p_implementer)
      AND (p_excluding_ticket_id IS NULL OR t.id <> p_excluding_ticket_id)
      AND NOT t.manually_controlled
      AND t.state IN ('in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
      AND (
          (t.state = 'in_progress' AND t.assignee = p_implementer)
          OR (
              t.state IN ('inspection', 'audit', 'dat', 'user_review', 'director_review')
              AND ns.last_implementer_assignee = p_implementer
              AND NOT (t.state = 'user_review' AND t.assignee = 'user')
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.director_dat_sign_off(
    id text,
    comment_text text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('director_dat_sign_off', id);
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'user_review',
        assignee = 'user'
    WHERE tickets.id = director_dat_sign_off.id
      AND tickets.state = 'dat'
      AND tickets.needs_user_signoff;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'DAT ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

WITH moved AS (
    UPDATE ticket_board.tickets
    SET assignee = 'user',
        manually_controlled = false
    WHERE state = 'user_review'
      AND needs_user_signoff
      AND (assignee IS DISTINCT FROM 'user' OR manually_controlled)
    RETURNING id
)
SELECT ticket_board.refresh_ticket_source_json(id)
FROM moved;
