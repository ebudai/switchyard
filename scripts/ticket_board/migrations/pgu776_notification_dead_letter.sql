ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS dead_lettered_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS terminal_reason text;

DROP INDEX IF EXISTS ticket_board.ticket_notification_queue_due_idx;
CREATE INDEX ticket_notification_queue_due_idx
    ON ticket_board.ticket_notification_queue (next_attempt_at, id)
    WHERE claimed_at IS NULL AND dead_lettered_at IS NULL;

DROP INDEX IF EXISTS ticket_board.ticket_notification_queue_claimed_idx;
CREATE INDEX ticket_notification_queue_claimed_idx
    ON ticket_board.ticket_notification_queue (claimed_at, next_attempt_at, id)
    WHERE claimed_at IS NOT NULL AND dead_lettered_at IS NULL;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_notification(
    p_ticket_id text,
    p_kind text,
    p_target_role text,
    p_message text,
    p_payload jsonb,
    p_dedupe_key text,
    p_next_attempt_at timestamptz DEFAULT clock_timestamp()
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    notification_id bigint;
BEGIN
    IF p_target_role IS NULL OR p_message IS NULL OR btrim(p_message) = '' THEN
        RETURN NULL;
    END IF;

    INSERT INTO ticket_board.ticket_notification_queue (
        ticket_id,
        kind,
        target_role,
        message,
        payload,
        dedupe_key,
        next_attempt_at
    ) VALUES (
        p_ticket_id,
        p_kind,
        p_target_role,
        p_message,
        p_payload,
        p_dedupe_key,
        p_next_attempt_at
    )
    ON CONFLICT (dedupe_key) DO UPDATE
    SET payload = EXCLUDED.payload,
        target_role = EXCLUDED.target_role,
        message = EXCLUDED.message,
        claimed_at = NULL,
        next_attempt_at = EXCLUDED.next_attempt_at,
        last_error = NULL,
        dead_lettered_at = NULL,
        terminal_reason = NULL,
        updated_at = clock_timestamp()
    RETURNING id INTO notification_id;

    PERFORM ticket_board.record_notification_trace(
        p_ticket_id,
        notification_id,
        p_target_role,
        p_kind,
        'enqueue',
        NULL,
        NULL,
        NULL,
        jsonb_build_object(
            'dedupe_key', p_dedupe_key,
            'next_attempt_at', p_next_attempt_at,
            'payload', p_payload
        )
    );

    PERFORM pg_notify(
        'ticket_board_state_transition',
        jsonb_build_object('kind', 'wake', 'notification_id', notification_id)::text
    );
    RETURN notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.claim_notification(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS TABLE(notification_id bigint, ticket_id text, target_role text, message text, payload jsonb, attempts integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('claim_notification');
    RETURN QUERY
    WITH candidate AS (
        SELECT q.id
        FROM ticket_board.ticket_notification_queue q
        WHERE q.next_attempt_at <= p_now
          AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
          AND q.dead_lettered_at IS NULL
        ORDER BY q.next_attempt_at, q.id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    claimed AS (
        UPDATE ticket_board.ticket_notification_queue q
        SET claimed_at = p_now,
            attempts = q.attempts + 1,
            updated_at = p_now
        FROM candidate
        WHERE q.id = candidate.id
        RETURNING q.id, q.ticket_id, q.target_role, q.message, q.payload, q.attempts
    ),
    traced AS (
        SELECT ticket_board.record_notification_trace(
            claimed.ticket_id,
            claimed.id,
            claimed.target_role,
            claimed.payload ->> 'kind',
            'claim',
            NULL,
            NULL,
            NULL,
            jsonb_build_object('attempts', claimed.attempts, 'payload', claimed.payload)
        )
        FROM claimed
    )
    SELECT claimed.id, claimed.ticket_id, claimed.target_role, claimed.message, claimed.payload, claimed.attempts
    FROM claimed, traced;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.next_notification_attempt(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS timestamptz
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_attempt timestamptz;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('next_notification_attempt');
    SELECT min(q.next_attempt_at)
    INTO next_attempt
    FROM ticket_board.ticket_notification_queue q
    WHERE (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
      AND q.dead_lettered_at IS NULL;
    RETURN next_attempt;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.finish_current_blocker(
    p_ticket_id text,
    p_target_role text,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    blocker_ticket_id text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('finish_current_blocker');

    WITH current_ticket AS (
        SELECT
            t.ticket_number,
            ns.entered_current_state_at
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.id = p_ticket_id
          AND t.state = 'in_progress'
          AND t.assignee = p_target_role
          AND NOT t.manually_controlled
    ),
    blocker_candidates AS (
        SELECT
            t.id,
            t.ticket_number,
            ns.entered_current_state_at
        FROM current_ticket c
        JOIN ticket_board.tickets t ON t.state = 'in_progress'
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.assignee = p_target_role
          AND t.id <> p_ticket_id
          AND NOT t.manually_controlled
          AND (
              (
                  ns.entered_current_state_at IS NOT NULL
                  AND c.entered_current_state_at IS NOT NULL
                  AND ns.entered_current_state_at < c.entered_current_state_at
              )
              OR (
                  (
                      ns.entered_current_state_at IS NULL
                      OR c.entered_current_state_at IS NULL
                      OR ns.entered_current_state_at = c.entered_current_state_at
                  )
                  AND t.ticket_number < c.ticket_number
              )
          )
    )
    SELECT b.id
    INTO blocker_ticket_id
    FROM blocker_candidates b
    WHERE NOT (
        EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
            ORDER BY q.next_attempt_at, q.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
    )
    ORDER BY
        CASE
            WHEN b.entered_current_state_at IS NOT NULL
                THEN b.entered_current_state_at
            ELSE NULL
        END NULLS LAST,
        b.ticket_number
    LIMIT 1;

    RETURN coalesce(blocker_ticket_id, '');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.dead_letter_notification(
    p_notification_id bigint,
    p_reason text,
    p_detail jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('dead_letter_notification');
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'dead_letter_notification requires a non-empty reason';
    END IF;

    UPDATE ticket_board.ticket_notification_queue
    SET claimed_at = NULL,
        dead_lettered_at = now_at,
        terminal_reason = left(p_reason, 500),
        last_error = left(p_reason, 500),
        updated_at = now_at
    WHERE id = p_notification_id;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'dead_letter',
        NULL,
        p_reason,
        NULL,
        jsonb_build_object(
            'reason', p_reason,
            'attempts', q.attempts,
            'payload', q.payload,
            'detail', coalesce(p_detail, '{}'::jsonb)
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.dismiss_notification(
    p_notification_id bigint,
    p_reason text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'dismiss_notification');

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'director_dismiss',
        NULL,
        coalesce(nullif(btrim(p_reason), ''), 'director dismissed notification'),
        NULL,
        jsonb_build_object(
            'actor', nullif(current_setting('ticket_board.caller_role', true), ''),
            'reason', p_reason,
            'attempts', q.attempts,
            'last_error', q.last_error,
            'terminal_reason', q.terminal_reason,
            'dead_lettered_at', q.dead_lettered_at,
            'payload', q.payload
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'notification % not found', p_notification_id;
    END IF;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.requeue_notification(
    p_notification_id bigint,
    p_delay interval DEFAULT interval '30 seconds',
    p_error text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('requeue_notification');
    IF coalesce(p_error, '') <> 'pane busy' THEN
        PERFORM ticket_board.record_notification_trace(
            q.ticket_id,
            q.id,
            q.target_role,
            q.kind,
            'requeue',
            NULL,
            p_error,
            NULL,
            jsonb_build_object('delay', p_delay, 'attempts', q.attempts, 'payload', q.payload)
        )
        FROM ticket_board.ticket_notification_queue q
        WHERE q.id = p_notification_id;
    END IF;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'retry_loud',
        NULL,
        p_error,
        NULL,
        jsonb_build_object(
            'attempts', q.attempts,
            'age_seconds', floor(extract(epoch FROM now_at - q.created_at))::integer,
            'payload', q.payload
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
      AND (q.attempts >= 12 OR q.created_at <= now_at - interval '1 hour')
      AND NOT EXISTS (
          SELECT 1
          FROM ticket_board.notification_trace nt
          WHERE nt.notification_id = q.id
            AND nt.event = 'retry_loud'
      );

    UPDATE ticket_board.ticket_notification_queue
    SET claimed_at = NULL,
        next_attempt_at = now_at + p_delay,
        last_error = p_error,
        updated_at = now_at
    WHERE id = p_notification_id
      AND dead_lettered_at IS NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_notification_backoff_for_idle_roles(
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
    reset_count integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('reset_notification_backoff_for_idle_roles');

    WITH idle_roles AS (
        SELECT lower(btrim(key)) AS role
        FROM jsonb_each_text(coalesce(p_idle_since_by_role, '{}'::jsonb))
        WHERE btrim(key) <> ''
    ),
    reset_rows AS (
        UPDATE ticket_board.ticket_notification_queue q
        SET claimed_at = NULL,
            next_attempt_at = p_now,
            last_error = NULL,
            updated_at = p_now
        FROM idle_roles i
        WHERE q.target_role = i.role
          AND q.next_attempt_at > p_now
          AND q.last_error = 'pane busy'
          AND q.dead_lettered_at IS NULL
        RETURNING q.id
    )
    SELECT count(*)::integer
    INTO reset_count
    FROM reset_rows;

    RETURN coalesce(reset_count, 0);
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.dismiss_notification(bigint, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.dead_letter_notification(bigint, text, jsonb) TO ticket_board_listener;
