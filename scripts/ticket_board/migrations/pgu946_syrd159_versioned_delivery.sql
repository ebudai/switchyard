-- SYRD-159 (second half): close the lost update that the collapsing dedupe key
-- makes possible.
--
-- With a stable key a pending notification can be refreshed WHILE a listener is
-- delivering it -- that is what the collapse is for. Removing it by id alone
-- then deletes the refreshed payload as the acknowledgement of the payload it
-- replaced, and the update that arrived mid-flight is never sent to anybody: a
-- duplicate wake traded for a silent loss.
--
-- Every queue row carries a version, bumped on every refresh. The claim records
-- the version it is delivering in the same statement that takes the claim, and
-- both removal paths refuse when the row has moved on, leaving it queued and
-- already due so the newer payload is delivered exactly once.
--
-- No function changes shape. That is deliberate: the listener cannot be wrong
-- about what it delivered because it does not say, the grants stay where they
-- are, and every historical migration that recreates these functions still
-- applies to a board that has been here.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS and CREATE OR REPLACE throughout.
BEGIN;

-- SYRD-159: which version of this row exists, and which version a listener took
-- to deliver. A collapsing dedupe key means a pending row can be refreshed while
-- it is being delivered -- that is what the collapse is for -- so removing it by
-- id alone deletes the refreshed payload as the acknowledgement of the payload
-- it replaced, and the update that arrived mid-flight is never sent to anybody.
-- The claim records what it took; removal refuses when the row has moved on.
--
-- Recorded by the claim rather than carried by the caller, so no function
-- changes shape: a listener cannot be wrong about what it delivered, and every
-- historical migration that recreates these functions still applies.
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 1;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS claimed_revision bigint;


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
        -- A refreshed row is a different thing to deliver, so it is a new
        -- version, and the claim that is delivering the old one can no longer
        -- remove it (SYRD-159).
        revision = ticket_board.ticket_notification_queue.revision + 1,
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
            -- What this delivery is of. Set in the same statement that takes
            -- the claim, so nothing can slip between choosing the row and
            -- recording which version of it left (SYRD-159).
            claimed_revision = q.revision,
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


-- SYRD-159: one place that decides whether a removal is still removing what was
-- delivered. A collapsing dedupe key means a pending row can be refreshed while
-- a listener is delivering it -- that is what the collapse is for -- so removing
-- it by id alone would delete a payload nobody has been told about. Both removal
-- paths ask this, so the rule cannot drift between them, and both stop rather
-- than re-arming: the enqueue that superseded the row already cleared its claim
-- and made it due, and clearing it a second time could steal a claim a later
-- pass has legitimately taken, which is how one update becomes two deliveries.
--
-- True means "stop": the row has moved on, or it is already gone.
CREATE OR REPLACE FUNCTION ticket_board.notification_delivery_superseded(
    p_notification_id bigint,
    p_event text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    current_revision bigint;
    delivered_revision bigint;
BEGIN
    SELECT q.revision, q.claimed_revision INTO current_revision, delivered_revision
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN true;
    END IF;
    IF delivered_revision IS NULL OR delivered_revision = current_revision THEN
        RETURN false;
    END IF;
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id, q.id, q.target_role, q.kind, p_event,
        NULL, NULL, NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload,
                           'delivered_revision', delivered_revision,
                           'current_revision', current_revision)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;
    RETURN true;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.ack_notification(p_notification_id bigint)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ack_notification');
    IF ticket_board.notification_delivery_superseded(p_notification_id, 'ack_superseded') THEN
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET idle_reminder_count = CASE
            WHEN q.kind = 'idle_reminder' THEN ns.idle_reminder_count + 1
            ELSE ns.idle_reminder_count
        END,
        last_idle_reminder_at = CASE
            WHEN q.kind = 'idle_reminder' THEN clock_timestamp()
            ELSE ns.last_idle_reminder_at
        END,
        last_transition_notified_at = CASE
            WHEN q.kind = 'transition' THEN clock_timestamp()
            ELSE ns.last_transition_notified_at
        END
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
      AND ns.ticket_id = q.ticket_id;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'ack',
        NULL,
        NULL,
        NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.discard_notification(
    p_notification_id bigint,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('discard_notification');
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'discard_notification requires a non-empty reason';
    END IF;
    IF ticket_board.notification_delivery_superseded(p_notification_id, 'discard_superseded') THEN
        RETURN;
    END IF;

    -- Removes a queued notification that was never delivered, WITHOUT the
    -- delivery accounting in ack_notification. That function bumps
    -- ticket_notification_state.idle_reminder_count for an idle_reminder, and
    -- notify_idle_turn_end_nudges reads idle_reminder_count >= 1 on a
    -- non-director owner as "already reminded", switching the next wave to an
    -- escalation addressed to the director. Acking a reminder that was
    -- suppressed because its owner is working would therefore tell the director
    -- the owner was reminded and ignored it, when nothing was ever delivered
    -- (SYRD-32).
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'discard',
        NULL,
        left(p_reason, 500),
        NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload, 'reason', p_reason)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

COMMIT;
