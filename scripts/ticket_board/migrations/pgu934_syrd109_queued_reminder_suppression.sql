-- SYRD-109: a ticket held by a serial-focus reservation was still being told
-- to advance itself.
--
-- SYRD-31 made the capacity wait durable -- queued_for_assignee and
-- queued_behind_ticket say which implementer the work is reserved for and which
-- ticket is holding them -- and the director is notified once, where it is
-- waiting. The reminder selectors never learned to read those fields. On
-- SYRD-107 the ticket sat in analysis/director queued for main behind SYRD-93,
-- SYRD-93 was still in_progress/main, and notification 1324 told the director
-- "SYRD-107 is waiting in your analysis queue. Advance it or hand it off."
-- There was nothing the director could legally do: routing it into main's lane
-- is the move the board itself refuses, which is why the ticket is queued at
-- all.
--
-- Three things change. Every generic reminder generator now declines a ticket
-- whose named reservation is still current, beside the guard each of them
-- already had for the pre-declarative backlog shape. The predicate is live and
-- fails open, so a reservation that ends -- finished, cancelled, superseded,
-- or never real -- restores ordinary reminders by itself rather than silencing
-- the ticket for good. And because "by itself" would reach the director as the
-- same generic reminder that started this, one specific hand-off is announced
-- when the wait ends, once per reservation identity, claimed durably so a
-- listener restart cannot mint a second.


-- Which reservation identity this ticket has already been woken out of. On
-- ticket_notification_state rather than on the ticket: it is notification
-- bookkeeping, and it must survive the queue row being acknowledged away.
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS serial_focus_wake_key text NOT NULL DEFAULT '';

CREATE OR REPLACE FUNCTION ticket_board.ticket_serial_focus_reservation_is_current(
    p_ticket_id text,
    p_queued_for_assignee text,
    p_queued_behind_ticket text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    -- Whether the reservation a queued ticket names is the one still holding
    -- that implementer. The sibling above infers the hold live from a backlog
    -- ticket's own assignee; a declarative queue destination is frequently
    -- analysis/director, so there is no implementer on the row to infer from
    -- and the durable fields are the only record of what it is waiting for.
    --
    -- Every way of not being current answers false -- fields cleared, the named
    -- ticket finished, cancelled, never existed, or superseded by other work on
    -- the same implementer. That is the safe direction: a stale identity
    -- restores ordinary reminders rather than silencing a ticket forever
    -- (SYRD-109).
    -- Every operand is made non-null before it is compared. An unguarded
    -- `reserved_ticket = queued_behind` is NULL, not false, when the
    -- implementer holds nothing -- and `AND NOT NULL` is NULL, so the callers'
    -- WHERE clauses would drop exactly the tickets whose wait had just ended.
    -- The predicate that exists to stop a ticket being silenced forever would
    -- have been the thing silencing it.
    SELECT coalesce(btrim(p_queued_for_assignee), '') <> ''
       AND coalesce(btrim(p_queued_behind_ticket), '') <> ''
       AND coalesce(
               ticket_board.ticket_current_reserved_ticket(
                   btrim(p_queued_for_assignee),
                   p_ticket_id
               ),
               ''
           ) = btrim(p_queued_behind_ticket)
       -- A ticket already owned by the implementer it names is not waiting for
       -- them, whatever the fields say. `ticket_current_reserved_ticket`
       -- excludes the ticket it is asked about, so without this a marker left
       -- on a ticket that has since reached its implementer's own lane would
       -- match some other reservation and silence work somebody is doing.
       AND NOT EXISTS (
           SELECT 1
           FROM ticket_board.tickets held
           WHERE held.id = p_ticket_id
             AND btrim(lower(held.assignee)) = btrim(lower(p_queued_for_assignee))
       );
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_due_nudges(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 3
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    target_role text;
    payload jsonb;
BEGIN
    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    WHEN 'director_review' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
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
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'backlog' THEN 6
                    ELSE 7
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (
                    (
                        t.state = 'analysis'
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
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
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at THEN
            target_role := 'director';
            payload := jsonb_build_object(
                'kind', 'escalation',
                'id', candidate.id,
                'title', candidate.title,
                'target_role', target_role,
                'message', 'PRIORITY ' || candidate.id || ' -- ' || candidate.title || ' appears stuck for ' || candidate.assignee || '; check/reassign'
            );
        ELSE
            payload := jsonb_build_object(
                'kind', 'nudge',
                'id', candidate.id,
                'title', candidate.title,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', target_role,
                'message', ticket_board.nudge_message(candidate.id, candidate.title, candidate.state, candidate.assignee)
            );
            RAISE WARNING 'wedged-pane nudge backstop fired for % targeting % (%s since transition)',
                candidate.id,
                target_role,
                floor(extract(epoch FROM p_now - candidate.entered_current_state_at))::integer || 's';
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN candidate.last_activity_at > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_stall_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_grace interval DEFAULT interval '45 seconds',
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 2,
    p_work_observed_at_by_role jsonb DEFAULT '{}'::jsonb
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
    target_role text;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_stall_nudges');

    p_idle_since_by_role := coalesce(p_idle_since_by_role, '{}'::jsonb);
    p_work_observed_at_by_role := coalesce(p_work_observed_at_by_role, '{}'::jsonb);

    IF p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.idle_since_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.work_observed_at,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
                        THEN (p_work_observed_at_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('in_progress', 'inspection', 'audit', 'dat') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND p_idle_since_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
              AND greatest(
                    ns.entered_current_state_at,
                    (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                  ) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
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
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> 'director')::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? 'director'
                        THEN (p_work_observed_at_by_role ->> 'director')::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'backlog' THEN 6
                    ELSE 7
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (
                    (
                        t.state = 'analysis'
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND p_idle_since_by_role ? 'director'
              AND greatest(ns.entered_current_state_at, (p_idle_since_by_role ->> 'director')::timestamptz) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
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
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at
           AND coalesce(candidate.work_observed_at, '-infinity'::timestamptz) <= candidate.last_nudged_at THEN
            target_role := 'director';
            payload := jsonb_build_object(
                'kind', 'escalation',
                'id', candidate.id,
                'title', candidate.title,
                'target_role', target_role,
                'message', 'PRIORITY ' || candidate.id || ' -- ' || candidate.title || ' appears stuck for ' || candidate.assignee || '; check/reassign'
            );
        ELSE
            payload := jsonb_build_object(
                'kind', 'nudge',
                'id', candidate.id,
                'title', candidate.title,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', target_role,
                'message', ticket_board.nudge_message(candidate.id, candidate.title, candidate.state, candidate.assignee)
            );
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN greatest(
                    candidate.last_activity_at,
                    coalesce(candidate.work_observed_at, '-infinity'::timestamptz)
                ) > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
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
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.serial_focus_available_message(
    p_ticket_id text,
    p_title text,
    p_queued_for text,
    p_queued_behind text,
    p_now_reserved text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- Two different facts, said differently. "Capacity is available" is only
    -- true when the implementer holds nothing else; when another ticket has
    -- taken the reservation, saying so is what lets the director re-queue
    -- against the right identity instead of routing into a full lane again.
    SELECT p_ticket_id
        || CASE WHEN coalesce(p_title, '') <> '' THEN ' -- ' || p_title ELSE '' END
        || CASE
            WHEN coalesce(p_now_reserved, '') = '' THEN
                ' can be routed to ' || p_queued_for || ' now: ' || p_queued_behind
                || ' no longer holds their serial focus and they hold no other reserved work.'
            ELSE
                ' is still queued for ' || p_queued_for || ', but ' || p_queued_behind
                || ' no longer holds their serial focus -- ' || p_now_reserved
                || ' does now. Route it again behind ' || p_now_reserved
                || ', or route it to a free implementer.'
        END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_serial_focus_queue_wakeups(
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
    now_reserved text;
    notice text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_serial_focus_queue_wakeups');

    FOR candidate IN
        SELECT
            t.id,
            t.title,
            t.state,
            t.assignee,
            t.ticket_number,
            btrim(t.queued_for_assignee) AS queued_for,
            btrim(t.queued_behind_ticket) AS queued_behind,
            btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket) AS wake_key
        FROM ticket_board.tickets t
        JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE btrim(t.queued_for_assignee) <> ''
          AND btrim(t.queued_behind_ticket) <> ''
          AND NOT t.manually_controlled
          -- The wait has genuinely ended. Everything else about this ticket --
          -- blockers, holds, its own stage -- is the director's to read; the
          -- one thing they cannot see from the board is that the reservation
          -- they were told to wait for is over.
          AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
              t.id,
              t.queued_for_assignee,
              t.queued_behind_ticket
          )
          AND ns.serial_focus_wake_key IS DISTINCT FROM
              (btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket))
          -- Blocked work cannot be routed either, and announcing capacity for
          -- it would be this ticket's own complaint in a new costume. The
          -- claim is left unmade, so the hand-off is announced when the
          -- blocker clears rather than lost -- which is what every other
          -- generator here does with a blocked ticket.
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
        ORDER BY t.ticket_number
    LOOP
        now_reserved := ticket_board.ticket_current_reserved_ticket(
            candidate.queued_for,
            candidate.id
        );
        notice := ticket_board.serial_focus_available_message(
            candidate.id,
            candidate.title,
            candidate.queued_for,
            candidate.queued_behind,
            now_reserved
        );
        -- Claimed before it is enqueued. enqueue_notification dedupes a row
        -- that is still waiting, but a delivered row is gone, and this function
        -- runs on every listener pass: without the claim the same wake would be
        -- minted again on the next pass after the first was acknowledged.
        UPDATE ticket_board.ticket_notification_state
        SET serial_focus_wake_key = candidate.wake_key
        WHERE ticket_id = candidate.id;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'ticket_update',
            'director',
            notice,
            -- `queued_for` and `reserved_by` are the queue announcement's own
            -- vocabulary, and deliberately so. The listener reads those two
            -- keys off any notification that carries them and discards it when
            -- the ticket's queue columns have moved on (SYRD-108). A wake is a
            -- statement about one reservation identity, so it should be
            -- discarded on a reroute for exactly that reason -- and naming the
            -- fields anything else would not avoid the question, it would just
            -- exempt this notification from an answer it needs. The claim that
            -- makes a wake once-per-identity is cleared by the same reroute, so
            -- the new identity gets its own wake when its own wait ends.
            jsonb_build_object(
                'kind', 'ticket_update',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', 'director',
                'queued_for', candidate.queued_for,
                'reserved_by', candidate.queued_behind,
                'now_reserved', coalesce(now_reserved, ''),
                'message', notice
            ),
            'serial-focus-available:' || candidate.id || ':' || candidate.wake_key
        );
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_serial_focus_wake_key()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- A new queue identity is a new wait, so the claim that announced the old
    -- one must not silence it. Rerouting to another implementer, re-queueing
    -- behind a different ticket, and clearing the fields outright all land
    -- here, which is what makes a reroute's wake condition move with it and a
    -- cleared queue fall straight back to ordinary reminders (SYRD-109).
    UPDATE ticket_board.ticket_notification_state
    SET serial_focus_wake_key = ''
    WHERE ticket_id = NEW.id
      AND serial_focus_wake_key <> '';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzzz_reset_serial_focus_wake_key ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzz_reset_serial_focus_wake_key
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
WHEN (
    OLD.queued_for_assignee IS DISTINCT FROM NEW.queued_for_assignee
    OR OLD.queued_behind_ticket IS DISTINCT FROM NEW.queued_behind_ticket
)
EXECUTE FUNCTION ticket_board.reset_serial_focus_wake_key();

GRANT EXECUTE ON FUNCTION ticket_board.notify_serial_focus_queue_wakeups(timestamptz) TO ticket_board_listener;
