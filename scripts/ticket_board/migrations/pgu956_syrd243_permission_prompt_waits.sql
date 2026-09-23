-- SYRD-243: an upgraded board could not be granted a function it never got.
--
-- `ticket_board.notify_permission_prompt_waits` arrived with the permission-wait
-- escalation and was written into schema.sql, which is what a FRESH board is
-- built from. Nothing created it for a board that arrives by migration, so the
-- ordered tail replayed cleanly and then the current rbac.sql tried to grant a
-- function that was not there:
--
--     ERROR: function ticket_board.notify_permission_prompt_waits(
--            jsonb, timestamp with time zone, interval) does not exist
--
-- A legacy history therefore failed at the RBAC boundary while a fresh schema
-- worked, which is the shape that hides until somebody upgrades.
--
-- The definition below is the one in schema.sql, character for character, and
-- a test asserts they stay that way: a migration copy that drifts is the copy
-- an upgraded board actually runs. It keeps the listener-only authority check
-- and the exact signature notify_listener.py calls and rbac.sql grants.
--
-- Numbered after pgu955 deliberately: ticket-board-migrate applies these in
-- alphabetical order, and this must land after everything it depends on --
-- require_ticket_board_listener, enqueue_notification, transition_target_role
-- and the notification trace.
--
-- Idempotent by construction: CREATE OR REPLACE, and a grant that is a no-op
-- when it is already held.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.notify_permission_prompt_waits(
    -- {role: ISO-8601 instant the pane went blocked on a prompt}
    p_blocked_since jsonb,
    p_now timestamptz,
    -- The same staged-recovery shape the unresolved-turn escalation uses: a
    -- prompt answered quickly by a passing human is not worth the Director's
    -- attention, and one that is not is exactly what this exists for.
    p_grace interval DEFAULT interval '2 minutes'
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    waiting record;
    enqueued integer := 0;
    blocked_since timestamptz;
    identity text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_permission_prompt_waits');
    FOR waiting IN
        SELECT
            key AS role,
            value #>> '{}' AS since
        FROM jsonb_each(coalesce(p_blocked_since, '{}'::jsonb))
        ORDER BY key
    LOOP
        BEGIN
            blocked_since := waiting.since::timestamptz;
        EXCEPTION WHEN others THEN
            -- An unparseable instant is not a reason to escalate; it is a
            -- reason to say nothing about a pane nobody can describe.
            CONTINUE;
        END;
        CONTINUE WHEN blocked_since > p_now - p_grace;
        FOR identity IN
            SELECT t.id
            FROM ticket_board.tickets t
            WHERE t.assignee = waiting.role
              AND ticket_board.transition_target_role(t.state, t.assignee) = waiting.role
            ORDER BY t.id
        LOOP
            -- One per role per blocked episode. The key carries the instant the
            -- pane went blocked, so a prompt answered and re-raised later is a
            -- new episode and a repeat of the same one is not.
            CONTINUE WHEN EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_queue q
                WHERE q.dedupe_key = format('permission-prompt:%s:%s', waiting.role, waiting.since)
            ) OR EXISTS (
                -- The queue row is gone once it is delivered and acknowledged,
                -- so the queue alone would let the same episode be reported
                -- again on the next pass. The trace outlives the row, and the
                -- dedupe key is carried in its detail (SYRD-133's lesson).
                SELECT 1
                FROM ticket_board.notification_trace tr
                WHERE tr.event = 'enqueue'
                  AND tr.detail ->> 'dedupe_key'
                      = format('permission-prompt:%s:%s', waiting.role, waiting.since)
            );
            PERFORM ticket_board.enqueue_notification(
                identity,
                'escalation',
                'director',
                format(
                    '%s is stopped on a permission prompt in its pane and cannot answer it '
                    || 'itself. It has been waiting since %s. Nothing it was asked to do is '
                    || 'progressing until somebody answers the prompt or restarts the role.',
                    waiting.role, waiting.since
                ),
                jsonb_build_object(
                    'kind', 'escalation',
                    'reason', 'permission_prompt',
                    'role', waiting.role,
                    'target_role', 'director',
                    'blocked_since', waiting.since,
                    'ticket_id', identity
                ),
                format('permission-prompt:%s:%s', waiting.role, waiting.since)
            );
            enqueued := enqueued + 1;
        END LOOP;
    END LOOP;
    RETURN enqueued;
END;
$$;

-- rbac.sql grants this on a rebuilt board; a board that arrives here by
-- migration needs it stated here, or the listener cannot call it at all.
GRANT EXECUTE ON FUNCTION ticket_board.notify_permission_prompt_waits(jsonb, timestamptz, interval)
    TO ticket_board_listener;

COMMIT;
