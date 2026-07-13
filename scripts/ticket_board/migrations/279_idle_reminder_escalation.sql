-- Escalate repeated idle-without-advancing reminders to the director for the same stall episode.

ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS idle_reminder_count integer NOT NULL DEFAULT 0
    CHECK (idle_reminder_count >= 0);

CREATE OR REPLACE FUNCTION ticket_board.upsert_ticket_notification_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.updated_at, NEW.row_updated_at, clock_timestamp());
    reset_idle_reminder boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_board.ticket_notification_state (
            ticket_id,
            current_state,
            current_assignee,
            previous_state,
            entered_current_state_at,
            last_activity_at,
            last_transition_notified_at
        ) VALUES (
            NEW.id,
            NEW.state,
            NEW.assignee,
            NULL,
            activity_at,
            activity_at,
            activity_at
        )
        ON CONFLICT (ticket_id) DO UPDATE
        SET current_state = EXCLUDED.current_state,
            current_assignee = EXCLUDED.current_assignee,
            previous_state = EXCLUDED.previous_state,
            entered_current_state_at = EXCLUDED.entered_current_state_at,
            last_activity_at = EXCLUDED.last_activity_at,
            last_transition_notified_at = EXCLUDED.last_transition_notified_at,
            last_nudged_at = NULL,
            nudge_count = 0,
            idle_reminder_count = 0;
        RETURN NULL;
    END IF;

    reset_idle_reminder := OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee;

    INSERT INTO ticket_board.ticket_notification_state (
        ticket_id,
        current_state,
        current_assignee,
        previous_state,
        entered_current_state_at,
        last_activity_at,
        last_transition_notified_at,
        last_nudged_at,
        nudge_count
    ) VALUES (
        NEW.id,
        NEW.state,
        NEW.assignee,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE NULL
        END,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN activity_at
            ELSE activity_at
        END,
        activity_at,
        NULL,
        NULL,
        0
    )
    ON CONFLICT (ticket_id) DO UPDATE
    SET current_state = EXCLUDED.current_state,
        current_assignee = EXCLUDED.current_assignee,
        previous_state = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE ticket_board.ticket_notification_state.previous_state
        END,
        entered_current_state_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN EXCLUDED.entered_current_state_at
            ELSE ticket_board.ticket_notification_state.entered_current_state_at
        END,
        last_activity_at = EXCLUDED.last_activity_at,
        last_transition_notified_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN NULL
            ELSE ticket_board.ticket_notification_state.last_transition_notified_at
        END,
        last_nudged_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN NULL
            ELSE ticket_board.ticket_notification_state.last_nudged_at
        END,
        nudge_count = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN 0
            ELSE ticket_board.ticket_notification_state.nudge_count
        END,
        idle_reminder_count = CASE
            WHEN reset_idle_reminder THEN 0
            ELSE ticket_board.ticket_notification_state.idle_reminder_count
        END;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is still in '
        || p_state
        || ' and you haven''t advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT, notify the director directly with the reason. Do NOT do nothing.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_director_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is still in '
        || p_state
        || ' and you haven''t advanced it. Advance it now (do the work or hand it off). Do NOT do nothing.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_escalation_message(
    p_owner_role text,
    p_ticket_id text,
    p_state text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_owner_role
        || ' was reminded about '
        || p_ticket_id
        || ' (in '
        || p_state
        || ') and still hasn''t advanced it -- may be stuck.';
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
            candidates.kind,
            candidates.owner_role,
            candidates.target_role,
            candidates.idle_reminder_count,
            candidates.idle_since_at
        FROM (
            SELECT
                t.id,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.entered_current_state_at,
                ns.idle_reminder_count,
                ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
                CASE
                    WHEN ticket_board.transition_target_role(t.state, t.assignee) <> 'director'
                         AND ns.idle_reminder_count >= 1 THEN 'escalation'
                    ELSE 'idle_reminder'
                END AS kind,
                CASE
                    WHEN ticket_board.transition_target_role(t.state, t.assignee) <> 'director'
                         AND ns.idle_reminder_count >= 1 THEN 'director'
                    ELSE ticket_board.transition_target_role(t.state, t.assignee)
                END AS target_role,
                (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                CASE
                    WHEN ticket_board.transition_target_role(t.state, t.assignee) <> 'director'
                         AND ns.idle_reminder_count >= 1 THEN -1
                    WHEN t.state = 'analysis' THEN 0
                    WHEN t.state = 'inspection' THEN 2
                    WHEN t.state = 'in_progress' THEN 3
                    WHEN t.state = 'audit' THEN 4
                    WHEN t.state = 'director_review' THEN 5
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
            'kind', candidate.kind,
            'id', candidate.id,
            'state', candidate.state,
            'assignee', candidate.assignee,
            'owner_role', candidate.owner_role,
            'target_role', candidate.target_role,
            'message', CASE
                WHEN candidate.kind = 'escalation' THEN ticket_board.idle_without_advancing_escalation_message(
                    candidate.owner_role,
                    candidate.id,
                    candidate.state
                )
                WHEN candidate.owner_role = 'director' THEN ticket_board.idle_without_advancing_director_message(
                    candidate.id,
                    candidate.state
                )
                ELSE ticket_board.idle_without_advancing_message(candidate.id, candidate.state)
            END,
            'idle_since', candidate.idle_since_at
        );

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            candidate.kind,
            candidate.target_role,
            payload ->> 'message',
            payload,
            CASE
                WHEN candidate.kind = 'escalation' THEN
                    'idle-reminder-escalation:' || candidate.id || ':' || candidate.owner_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
                ELSE
                    'idle-reminder:' || candidate.id || ':' || candidate.target_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET idle_reminder_count = idle_reminder_count + 1
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;
