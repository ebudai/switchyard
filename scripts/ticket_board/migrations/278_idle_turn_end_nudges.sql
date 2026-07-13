-- Enqueue immediate reminder notifications when a pane goes idle without advancing its owned ticket.

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;

ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder'));

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT 'You went idle but '
        || p_ticket_id
        || ' is still in '
        || p_state
        || ' and you have not moved it forward -- tend to it (advance it or hand it off).';
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_turn_end_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_turn_end_nudges');

    IF p_idle_since_by_role IS NULL OR p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.target_role)
            candidates.id,
            candidates.state,
            candidates.assignee,
            candidates.target_role,
            candidates.idle_since_at
        FROM (
            SELECT
                t.id,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.entered_current_state_at,
                ticket_board.transition_target_role(t.state, t.assignee) AS target_role,
                (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'inspection' THEN 2
                    WHEN 'in_progress' THEN 3
                    WHEN 'audit' THEN 4
                    WHEN 'director_review' THEN 5
                    ELSE 9
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'director_review')
              AND NOT t.manually_controlled
              AND ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
              AND p_idle_since_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
              AND greatest(
                    ns.entered_current_state_at,
                    (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                  ) <= p_now
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
        ) AS candidates
        ORDER BY candidates.target_role, candidates.priority, candidates.ticket_number
    LOOP
        payload := jsonb_build_object(
            'kind', 'idle_reminder',
            'id', candidate.id,
            'state', candidate.state,
            'assignee', candidate.assignee,
            'target_role', candidate.target_role,
            'message', ticket_board.idle_without_advancing_message(candidate.id, candidate.state),
            'idle_since', candidate.idle_since_at
        );

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'idle_reminder',
            candidate.target_role,
            payload ->> 'message',
            payload,
            'idle-reminder:' || candidate.id || ':' || candidate.target_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
        );
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;
