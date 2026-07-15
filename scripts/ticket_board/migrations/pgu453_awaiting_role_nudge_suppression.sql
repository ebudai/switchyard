ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_role text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_since_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_awaiting_role_check
    CHECK (
        awaiting_role = ''
        OR awaiting_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research')
    );

CREATE OR REPLACE FUNCTION ticket_board.ticket_awaiting_role_is_active(
    p_awaiting_role text,
    p_awaiting_since_at timestamptz,
    p_now timestamptz,
    p_timeout interval DEFAULT interval '4 hours'
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(p_awaiting_role, '') IS NOT NULL
       AND p_awaiting_since_at IS NOT NULL
       AND p_awaiting_since_at > p_now - p_timeout;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notification_delivery_in_backoff(
    p_ticket_id text,
    p_target_role text,
    p_now timestamptz,
    p_recent interval DEFAULT interval '5 minutes'
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH latest_delivery_event AS (
        SELECT nt.event, nt.ts
        FROM ticket_board.notification_trace nt
        WHERE nt.ticket_id = p_ticket_id
          AND nt.target_role = p_target_role
          AND nt.event IN (
              'gate_defer',
              'send_failed',
              'send',
              'listener_ack',
              'drop',
              'max_defer_force_deliver',
              'ack'
          )
        ORDER BY nt.ts DESC, nt.id DESC
        LIMIT 1
    )
    SELECT EXISTS (
        SELECT 1
        FROM latest_delivery_event lde
        WHERE lde.event IN ('gate_defer', 'send_failed')
          AND lde.ts >= p_now - p_recent
    )
    OR EXISTS (
        SELECT 1
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = p_ticket_id
          AND q.target_role = p_target_role
          AND q.claimed_at IS NULL
          AND q.next_attempt_at > p_now
          AND btrim(coalesce(q.last_error, '')) <> ''
    )
    OR EXISTS (
        SELECT 1
        FROM ticket_board.ticket_notification_state ns
        WHERE ns.ticket_id = p_ticket_id
          AND ticket_board.ticket_awaiting_role_is_active(
              ns.awaiting_role,
              ns.awaiting_since_at,
              p_now
          )
    );
$$;

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

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role_from_ticket_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    actor text := nullif(current_setting('ticket_board.caller_role', true), '');
BEGIN
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL
    WHERE ticket_id = NEW.id
      AND awaiting_role <> ''
      AND (
          OLD.state IS DISTINCT FROM NEW.state
          OR OLD.assignee IS DISTINCT FROM NEW.assignee
          OR actor = awaiting_role
      );
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.touch_ticket_notification_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.ts, clock_timestamp());
BEGIN
    UPDATE ticket_board.ticket_notification_state
    SET last_activity_at = activity_at,
        nudge_count = 0,
        awaiting_role = CASE
            WHEN awaiting_role = NEW.who THEN ''
            ELSE awaiting_role
        END,
        awaiting_since_at = CASE
            WHEN awaiting_role = NEW.who THEN NULL
            ELSE awaiting_since_at
        END
    WHERE ticket_id = NEW.ticket_id;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_clear_awaiting_role_activity ON ticket_board.tickets;
CREATE TRIGGER tickets_clear_awaiting_role_activity
AFTER UPDATE OF state, assignee, updated_at ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.clear_awaiting_role_from_ticket_activity();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.set_awaiting_role(text, text) TO ticket_board_service;
        GRANT EXECUTE ON FUNCTION ticket_board.clear_awaiting_role(text) TO ticket_board_service;
    END IF;
END;
$$;
