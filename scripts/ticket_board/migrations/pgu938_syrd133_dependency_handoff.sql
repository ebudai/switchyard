-- SYRD-133: a dependency left in a comment, and an escalation that repeats.
--
-- On SYRD-131 Ops wrote that the remaining step was a privileged Director one
-- and stopped. The sentence was on the ticket; the wait was not. `awaiting_role`
-- stayed empty, so no durable handoff existed and the work sat in
-- `in_progress/ops` with nobody holding it.
--
-- The fail-safe underneath did work -- an idle reminder reached Ops at 14:56 and
-- the escalation reached the Director at 15:43 -- but it does not stop. The
-- escalation's dedupe key holds only while its queue row exists; once the
-- Director acknowledged it, the next wave found the same idle owner and the
-- same unmoved ticket and enqueued it again at 15:44. A fail-safe that repeats
-- every wave is one an operator learns to ignore.
--
-- So: one action that records the reason AND the handoff in one transaction,
-- and a marker that outlives the acknowledgement so the escalation fires once
-- per stall. The marker is an identity rather than a flag -- ticket, stage,
-- assignee and the moment the owner went idle -- so a ticket that stalls again
-- later is escalated again, without a trigger to clear anything.

BEGIN;

ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS idle_escalation_key text NOT NULL DEFAULT '';

CREATE OR REPLACE FUNCTION ticket_board.idle_escalation_identity(
    p_ticket text,
    p_state text,
    p_assignee text,
    p_idle_since timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_ticket, '') || ':' || coalesce(p_state, '') || ':'
        || coalesce(p_assignee, '') || ':'
        || coalesce(extract(epoch FROM p_idle_since)::bigint::text, '');
$$;

CREATE OR REPLACE FUNCTION ticket_board.request_dependency(
    p_ticket text,
    p_awaiting_role text,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    reason text := btrim(coalesce(p_reason, ''));
    normalized_role text := lower(btrim(coalesce(p_awaiting_role, '')));
BEGIN
    IF reason = '' THEN
        -- The reason is why this is recorded here rather than as a bare flag:
        -- the awaited role has to be able to act on it.
        RAISE EXCEPTION 'requesting a dependency requires a reason';
    END IF;
    IF normalized_role = '' THEN
        RAISE EXCEPTION 'requesting a dependency requires the role it waits on';
    END IF;
    -- The comment first, so a wait never exists without the sentence that
    -- explains it. One transaction, so neither can exist without the other.
    PERFORM ticket_board.add_comment(p_ticket, reason);
    PERFORM ticket_board.set_awaiting_role(p_ticket, normalized_role);
END;
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
                notification_scope.id,
                notification_scope.state,
                notification_scope.assignee,
                notification_scope.ticket_number,
                notification_scope.entered_current_state_at,
                notification_scope.idle_reminder_count,
                notification_scope.owner_role,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'escalation'
                    ELSE 'idle_reminder'
                END AS kind,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'director'
                    ELSE notification_scope.owner_role
                END AS target_role,
                notification_scope.idle_since_at,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN -1
                    WHEN notification_scope.state = 'analysis' THEN 0
                    WHEN notification_scope.state = 'inspection' THEN 2
                    WHEN notification_scope.state = 'in_progress' THEN 3
                    WHEN notification_scope.state = 'audit' THEN 4
                    WHEN notification_scope.state = 'dat' THEN 5
                    WHEN notification_scope.state = 'director_review' THEN 5
                    ELSE 9
                END AS priority,
                row_number() OVER (
                    PARTITION BY notification_scope.active_work_partition
                    ORDER BY sent.last_sent_at DESC NULLS LAST,
                        notification_scope.ticket_number DESC
                ) AS in_progress_rank
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
                        WHEN t.state = 'in_progress'
                            THEN t.state || ':' || ticket_board.transition_target_role(t.state, t.assignee)
                        ELSE NULL
                    END AS active_work_partition,
                    (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at
                FROM ticket_board.tickets t
                JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
                WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
                  AND NOT t.manually_controlled
                  AND ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
                  AND p_idle_since_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
                  AND greatest(
                        ns.entered_current_state_at,
                        (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                      ) <= p_now
                  -- await-role means the owner has explicitly handed this off.
                  -- Do not tell that same owner they may be stuck while the wait is active.
                  AND NOT ticket_board.ticket_awaiting_role_is_active(
                      ns.awaiting_role,
                      ns.awaiting_since_at,
                      p_now
                  )
                  -- A durable capacity wait is not unattended work. While the
                  -- reservation this ticket names still holds that implementer, the
                  -- director cannot legally route it into that lane, so "advance it
                  -- or hand it off" asks for something the board itself refuses. The
                  -- backlog guard beside this one answers the same question for a
                  -- ticket whose own assignee is the reserved implementer; a
                  -- declarative queue destination is usually analysis/director, so
                  -- only the durable fields know what it is waiting for (SYRD-109).
                  AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                      t.id,
                      t.queued_for_assignee,
                      t.queued_behind_ticket
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_blockers tb
                      LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                      WHERE tb.ticket_id = t.id
                        AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_comments c
                      WHERE c.ticket_id = t.id
                        AND c.who = ticket_board.transition_target_role(t.state, t.assignee)
                        AND c.ts IS NOT NULL
                        AND c.ts >= ns.entered_current_state_at
                        AND c.ts >= ns.last_activity_at
                        AND c.ts >= (
                            (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                            - interval '5 minutes'
                        )
                        AND c.ts <= p_now
                  )
            ) AS notification_scope
            LEFT JOIN LATERAL (
                SELECT max(trace.ts) AS last_sent_at
                FROM ticket_board.notification_trace trace
                WHERE trace.ticket_id = notification_scope.id
                  AND trace.target_role = notification_scope.owner_role
                  AND trace.kind = 'transition'
                  AND trace.event = 'send'
                  AND trace.ticket_state_at_event = notification_scope.state
            ) sent ON true
            WHERE greatest(
                    notification_scope.entered_current_state_at,
                    notification_scope.idle_since_at
                  ) <= p_now
              AND notification_scope.owner_role IS NOT NULL
        ) AS candidates
        WHERE (candidates.state <> 'in_progress' OR candidates.in_progress_rank = 1)
          AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidates.id
              AND q.target_role = candidates.target_role
              AND q.kind NOT IN ('idle_reminder', 'escalation')
        )
          -- One escalation per stall, not one per wave. The dedupe key holds
          -- only while the queue row does; once the director acknowledges it,
          -- the next wave finds the same idle owner and the same unmoved
          -- ticket and enqueues it again -- which on SYRD-131 it did, within a
          -- minute of delivering the first. This marker outlives the
          -- acknowledgement and re-arms when the stall's identity changes
          -- (SYRD-133).
          AND (
            candidates.kind <> 'escalation'
            OR NOT EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state escalated
                WHERE escalated.ticket_id = candidates.id
                  AND escalated.idle_escalation_key = ticket_board.idle_escalation_identity(
                      candidates.id,
                      candidates.state,
                      candidates.assignee,
                      candidates.idle_since_at
                  )
            )
        )
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
                WHEN candidate.owner_role = 'director' AND candidate.idle_reminder_count >= 1 THEN ticket_board.idle_without_advancing_problem_message(
                    candidate.id,
                    candidate.state,
                    'User'
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
        IF candidate.kind = 'escalation' THEN
            -- Claimed in the same transaction that enqueues it, so two waves
            -- racing cannot both decide they are the first.
            UPDATE ticket_board.ticket_notification_state
               SET idle_escalation_key = ticket_board.idle_escalation_identity(
                       candidate.id, candidate.state, candidate.assignee, candidate.idle_since_at
                   )
             WHERE ticket_id = candidate.id;
        END IF;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.request_dependency(text, text, text) TO ticket_board_service;

DO $grants$
DECLARE writer text;
BEGIN
    FOR writer IN
        SELECT rolname FROM pg_roles
        WHERE NOT rolsuper
          AND has_function_privilege(
                  rolname, 'ticket_board.force_move(text, text, text, boolean)', 'EXECUTE')
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.request_dependency(text, text, text) TO %I', writer);
    END LOOP;
END $grants$;

DO $listener$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.idle_escalation_identity(text, text, text, timestamptz)
            TO ticket_board_listener;
    END IF;
END $listener$;

COMMIT;
