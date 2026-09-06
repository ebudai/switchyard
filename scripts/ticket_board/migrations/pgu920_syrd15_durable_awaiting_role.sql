BEGIN;

ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_notified_since_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder', 'awaiting_role'));

-- Persist the entire bounded schedule in the existing queue. The marker survives
-- ACK/deletion, so repeated requests and migration retries cannot recreate it.
CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF t.state NOT IN ('in_progress', 'inspection', 'audit')
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.enqueue_awaiting_role_handoff(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF normalized_role NOT IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND t.state IN ('in_progress', 'inspection', 'audit')
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND t.state IN ('in_progress', 'inspection', 'audit');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'clear_awaiting_role'
    );
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = clear_awaiting_role.id FOR UPDATE;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL,
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    WHERE ticket_id = clear_awaiting_role.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

-- Seed existing active waits without extending their original suppression clock.
DO $$ DECLARE pending record; BEGIN
    FOR pending IN SELECT ticket_id FROM ticket_board.ticket_notification_state
        WHERE awaiting_role <> '' AND awaiting_since_at > clock_timestamp() - interval '4 hours'
        ORDER BY ticket_id
    LOOP
        PERFORM ticket_board.enqueue_awaiting_role_handoff(pending.ticket_id);
    END LOOP;
END $$;
COMMIT;
