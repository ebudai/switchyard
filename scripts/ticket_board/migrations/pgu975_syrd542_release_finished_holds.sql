-- SYRD-542: Audit's next ticket waited about five minutes after the previous
-- review left it. Serial review admission held MEFP-570's notice ("finish
-- current") behind MEFP-569 eight times; each requeue doubled the delay to the
-- 300-second cap. When Audit finished 569 and its pane went idle, the listener
-- re-armed only rows waiting for a busy pane, so 570 sat out a deadline nothing
-- was holding it to any more.
--
-- reset_notification_backoff_for_idle_roles, which the listener runs on every
-- pass with the roles its trusted hook evidence shows idle, now also re-arms a
-- held transition notice whose hold no longer applies -- asked of the same
-- admission check that held it (finish_current_stage_blocker). Rows still
-- behind a current ticket keep their deadline; delivery still runs every pane
-- check. Otherwise exactly pgu776's copy; schema.sql carries the same text.
BEGIN;

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
          AND q.dead_lettered_at IS NULL
          AND (
                -- Must match PANE_BUSY_REQUEUE_ERROR in notify_listener.py.
                q.last_error = 'pane busy'
                -- SYRD-542: held only because another ticket was this role's
                -- current one (FINISH_CURRENT_REQUEUE_ERROR), and no ticket
                -- holds it now. Its earlier holds doubled its delay to the cap,
                -- so it would wait out a deadline nothing is holding it to; it
                -- is due on this pass instead. A row still behind a current
                -- ticket keeps its deadline, and delivery still checks the pane.
             OR (q.last_error = 'finish current'
                 AND q.kind = 'transition'
                 AND coalesce(ticket_board.finish_current_stage_blocker(
                         q.ticket_id, q.target_role, q.payload ->> 'new_state', p_now), '') = '')
          )
        RETURNING q.id
    )
    SELECT count(*)::integer
    INTO reset_count
    FROM reset_rows;

    RETURN coalesce(reset_count, 0);
END;
$$;

COMMIT;
