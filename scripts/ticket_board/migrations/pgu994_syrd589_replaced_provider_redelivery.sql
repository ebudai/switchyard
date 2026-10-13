-- SYRD-589: work delivered to a provider process that has since been replaced
-- is told to its replacement, once. MEFP restored six worker panes; each
-- ticket's notice had been sent to the provider before, so the board read it
-- as delivered while every new provider sat at its prompt knowing nothing of
-- it. The listener names the roles whose provider has begun no turn since it
-- started; this queues each one's current ticket again, in the repeat wording,
-- once per provider. schema.sql carries the same text.
BEGIN;

-- SYRD-589: a role's current ticket, delivered to a provider process that has
-- since been replaced -- restored, reloaded, or started fresh because its
-- resume failed -- whose replacement has begun no turn. The send still read as
-- the delivery, so the board showed the work as delivered while the new
-- provider sat at its prompt knowing nothing of it (MEFP, six workers).
--
-- The listener says which roles' providers are like that, and since when
-- (role -> when the provider started; it cannot be later than now). For each,
-- the role's current ticket -- the one the board shows as its active work --
-- is told again in the repeat wording, when its notice for this stage was sent
-- before that start and none is queued now. Once per provider: the queued
-- notice is keyed by the start, and a key already used is never used again,
-- whatever became of that notice.
CREATE OR REPLACE FUNCTION ticket_board.redeliver_replaced_work(
    p_turnless_since_by_role jsonb,
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
    queued integer := 0;
    started timestamptz;
    dedupe text;
    message text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('redeliver_replaced_work');
    IF jsonb_typeof(p_turnless_since_by_role) IS DISTINCT FROM 'object' THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        WITH waiting AS MATERIALIZED (
            -- Waiting in an implementer's queue is nobody's current work (SYRD-568).
            SELECT q.ticket_id FROM ticket_board.serial_queue() q GROUP BY q.ticket_id HAVING NOT bool_or(q.active)
        )
        SELECT DISTINCT ON (current_work.owner_role) current_work.*
        FROM (
            SELECT t.id, t.title, t.state, t.assignee, t.ticket_number, ns.entered_current_state_at,
                   ticket_board.transition_target_role(t.state, t.assignee) AS owner_role
            FROM ticket_board.tickets t
            LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            -- What the board counts as a role's current work (ticket_read_query's
            -- is_actionable_current), and the first of it in the board's order.
            WHERE NOT t.manually_controlled
              AND coalesce(ns.awaiting_role, '') = ''
              AND coalesce(t.queued_for_assignee, '') = ''
              AND coalesce(t.queued_behind_ticket, '') = ''
              AND NOT coalesce((SELECT s.is_terminal FROM ticket_board.workflow_stages s WHERE s.name = t.state), false)
              AND NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id AND NOT b.resolved)
              AND NOT EXISTS (SELECT FROM waiting w WHERE w.ticket_id = t.id)
        ) current_work
        WHERE current_work.owner_role IS NOT NULL AND current_work.owner_role <> ''
          AND p_turnless_since_by_role ? current_work.owner_role
        ORDER BY current_work.owner_role, current_work.entered_current_state_at NULLS LAST, current_work.ticket_number
    LOOP
        BEGIN
            started := (p_turnless_since_by_role ->> candidate.owner_role)::timestamptz;
        EXCEPTION WHEN others THEN
            CONTINUE;
        END;
        IF started IS NULL OR started > p_now THEN
            CONTINUE;
        END IF;
        -- Delivered in this stage, and to a provider older than this one.
        IF NOT EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id
              AND trace.target_role = candidate.owner_role
              AND trace.kind = 'transition'
              AND trace.event = 'send'
              AND trace.ticket_state_at_event = candidate.state
              AND (candidate.entered_current_state_at IS NULL OR trace.ts >= candidate.entered_current_state_at)
        ) OR EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id
              AND trace.target_role = candidate.owner_role
              AND trace.kind = 'transition'
              AND trace.event = 'send'
              AND trace.ts >= started
        ) THEN
            CONTINUE;
        END IF;
        -- Nothing about it already on its way.
        IF EXISTS (
            SELECT FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidate.id AND q.target_role = candidate.owner_role AND q.kind = 'transition'
        ) THEN
            CONTINUE;
        END IF;
        dedupe := 'redelivery:' || candidate.id || ':' || candidate.owner_role || ':'
                  || floor(extract(epoch FROM started))::bigint::text;
        IF EXISTS (
            SELECT FROM ticket_board.notification_trace trace
            WHERE trace.ticket_id = candidate.id AND trace.event = 'enqueue' AND trace.detail->>'dedupe_key' = dedupe
        ) THEN
            CONTINUE;
        END IF;
        message := ticket_board.transition_message(candidate.id, candidate.title, candidate.state, candidate.state, true);
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'transition',
            candidate.owner_role,
            message,
            jsonb_build_object(
                'kind', 'transition',
                'id', candidate.id,
                'title', candidate.title,
                'old_state', candidate.state,
                'new_state', candidate.state,
                'assignee', candidate.assignee,
                'updated_at', p_now,
                'ticket_number', candidate.ticket_number,
                'target_role', candidate.owner_role,
                'message', message,
                'redelivery', jsonb_build_object('reason', 'provider_replaced', 'provider_started_at', started)
            ),
            dedupe
        );
        queued := queued + 1;
    END LOOP;
    RETURN queued;
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.redeliver_replaced_work(jsonb, timestamptz) FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.redeliver_replaced_work(jsonb, timestamptz) TO ticket_board_listener;
    END IF;
END $$;

COMMIT;
