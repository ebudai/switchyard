-- SYRD-194: an owner that returns to an idle prompt without durably resolving
-- its ticket is itself an actionable event.
--
-- SYRD-133 built the escalation and SYRD-163 taught it patience, and both were
-- right for what they were fixing. What neither covers is the case SYRD-193
-- reproduced live: App printed a Director question, returned to the prompt, and
-- touched nothing. `notify_idle_turn_end_nudges` could only reach the Director
-- through `idle_reminder_count >= 1` -- the owner has to be nudged and ignore it
-- first -- so the Board had no event until a human happened to read the pane.
--
-- This adds a SIBLING generator rather than loosening that one. Every
-- suppression SYRD-163 added stays exactly where it is and keeps governing
-- reminders and escalations; nothing here changes when an idle_reminder is due.
-- What changes is that an unresolved turn END reaches the Director immediately,
-- on its own kind, and the thing that holds it back is not elapsed time but a
-- LEASE the owner takes deliberately.
--
-- The lease is scoped to one completed-turn identity and consumed when the next
-- turn begins, so "I am still working" is a statement about the next turn and
-- cannot become a way to stall indefinitely: a lease that is never followed by
-- a turn expires, and an expired lease notifies. Every fresh turn mints a fresh
-- identity and re-arms the guard, so a role that keeps working keeps renewing,
-- and a role that stops is escalated once.
BEGIN;

-- One row per (ticket, owner, turn). `turn_id` is the completed-turn identity
-- the pane hook mints; it is opaque here on purpose, because the board must not
-- care which runtime produced it.
CREATE TABLE IF NOT EXISTS ticket_board.turn_continuation_lease (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    owner_role text NOT NULL,
    turn_id text NOT NULL,
    reason text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    PRIMARY KEY (ticket_id, owner_role, turn_id)
);

CREATE INDEX IF NOT EXISTS turn_continuation_lease_open_idx
    ON ticket_board.turn_continuation_lease (owner_role, expires_at)
    WHERE consumed_at IS NULL;

-- A fourth reason a notification exists. Named rather than folded into
-- 'escalation' so the two can be told apart in the trace: an escalation says
-- "was reminded and still has not moved", this says "stopped without saying
-- anything", and they call for different responses.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation',
                    'idle_reminder', 'awaiting_role', 'unresolved_turn'));

-- The identity a repeat is measured against: same ticket, same stage, same
-- assignee, same completed turn. A fresh turn changes it and re-arms; an
-- acknowledgement does not, so acknowledging cannot cause a repeat for the turn
-- that was already reported.
CREATE OR REPLACE FUNCTION ticket_board.turn_unresolved_identity(
    p_ticket text,
    p_state text,
    p_assignee text,
    p_turn_id text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT 'unresolved-turn:' || coalesce(p_ticket, '') || ':' || coalesce(p_state, '')
           || ':' || coalesce(p_assignee, '') || ':' || coalesce(p_turn_id, '');
$$;

-- Is this ticket's active work durably resolved, by any of the means that end a
-- turn legitimately? Written once and used by the guard, so "resolved" has one
-- definition rather than one per call site.
CREATE OR REPLACE FUNCTION ticket_board.ticket_turn_is_resolved(
    p_ticket text,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT
        t.manually_controlled
        OR t.state IS NULL
        OR ticket_board.transition_target_role(t.state, t.assignee) IS NULL
        OR (CASE WHEN ticket_board.declared_workflow() IS NULL
                 THEN t.state IN ('done', 'cancelled')
                 ELSE coalesce((SELECT is_terminal FROM ticket_board.workflow_stages WHERE name = t.state), false)
            END)
        OR ticket_board.ticket_awaiting_role_is_active(ns.awaiting_role, ns.awaiting_since_at, p_now)
        OR ticket_board.ticket_serial_focus_reservation_is_current(
               t.id, t.queued_for_assignee, t.queued_behind_ticket)
        OR EXISTS (
            SELECT 1
            FROM ticket_board.ticket_blockers tb
            LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
            WHERE tb.ticket_id = t.id
              AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
        )
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id = p_ticket;
$$;

-- The owner's deliberate "I am continuing", recorded the way request_dependency
-- records a dependency: the sentence and the state in one transaction, so a
-- lease never exists without the reason for it. Granting is the owner's to do
-- and nobody else's, and it covers exactly the turn named.
CREATE OR REPLACE FUNCTION ticket_board.grant_turn_continuation(
    p_ticket text,
    p_turn_id text,
    p_reason text,
    p_grace interval DEFAULT interval '10 minutes'
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    -- Prefixed, because `reason`, `owner_role` and `turn_id` are also column
    -- names on turn_continuation_lease and an unprefixed local makes the
    -- ON CONFLICT target ambiguous -- Postgres refuses it rather than guessing.
    v_reason text := btrim(coalesce(p_reason, ''));
    v_turn text := btrim(coalesce(p_turn_id, ''));
    v_owner_role text;
    v_caller text;
BEGIN
    IF v_turn = '' THEN
        RAISE EXCEPTION 'continuing work requires the turn it continues from';
    END IF;
    IF v_reason = '' THEN
        -- Same reasoning as request_dependency: the Director may still read
        -- this, and "continuing" with no sentence is indistinguishable from
        -- silence.
        RAISE EXCEPTION 'continuing work requires a reason';
    END IF;
    PERFORM ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'grant_turn_continuation'
    );
    v_caller := nullif(current_setting('ticket_board.caller_role', true), '');
    SELECT ticket_board.transition_target_role(t.state, t.assignee) INTO v_owner_role
    FROM ticket_board.tickets t WHERE t.id = p_ticket;
    IF v_owner_role IS NULL THEN
        RAISE EXCEPTION 'ticket % has no active owner to continue', p_ticket;
    END IF;
    IF v_caller IS NOT NULL AND v_caller <> v_owner_role THEN
        -- Continuation is the owner's statement about its own next turn.
        RAISE EXCEPTION 'role % cannot continue work owned by %', v_caller, v_owner_role;
    END IF;
    PERFORM ticket_board.add_comment(p_ticket, v_reason);
    INSERT INTO ticket_board.turn_continuation_lease AS l
        (ticket_id, owner_role, turn_id, reason, expires_at)
    VALUES (p_ticket, v_owner_role, v_turn, v_reason, clock_timestamp() + p_grace)
    ON CONFLICT (ticket_id, owner_role, turn_id) DO UPDATE
        SET reason = excluded.reason,
            expires_at = excluded.expires_at
        WHERE l.consumed_at IS NULL;
END;
$$;

-- A new turn beginning consumes whatever the previous one leased. This is what
-- makes the lease one-shot: it buys the boundary it was taken at and nothing
-- beyond it.
CREATE OR REPLACE FUNCTION ticket_board.consume_turn_continuation(
    p_owner_role text,
    p_before_turn_id text DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    consumed integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('consume_turn_continuation');
    UPDATE ticket_board.turn_continuation_lease
    SET consumed_at = clock_timestamp()
    WHERE owner_role = p_owner_role
      AND consumed_at IS NULL
      AND (p_before_turn_id IS NULL OR turn_id <> p_before_turn_id);
    GET DIAGNOSTICS consumed = ROW_COUNT;
    RETURN consumed;
END;
$$;

-- The guard. A sibling of notify_idle_turn_end_nudges, not a change to it: that
-- one still decides reminders and escalations on exactly the terms SYRD-163
-- gave it. This one answers a different question -- did an owner just stop
-- without resolving? -- and answers it at the boundary rather than after a
-- timeout.
--
-- `p_turn_by_role` is {role: turn_id} for the roles whose turn just ended. A
-- role absent from it is not being judged; silence about a role is not evidence
-- about its work.
CREATE OR REPLACE FUNCTION ticket_board.notify_unresolved_turn_end(
    p_turn_by_role jsonb,
    p_now timestamptz
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    enqueued integer := 0;
    identity text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_unresolved_turn_end');
    IF p_turn_by_role IS NULL OR p_turn_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT
            t.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee)) AS turn_id
        FROM ticket_board.tickets t
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND p_turn_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
          -- Owned, active, and not resolved by any of the legitimate means.
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
          -- A lease the owner took for THIS turn, still open and not yet
          -- expired, is the one thing that buys silence here. An expired lease
          -- buys nothing, which is what stops it becoming a way to stall: the
          -- owner promised a next turn and none arrived.
          AND NOT EXISTS (
              SELECT 1
              FROM ticket_board.turn_continuation_lease l
              WHERE l.ticket_id = t.id
                AND l.owner_role = ticket_board.transition_target_role(t.state, t.assignee)
                AND l.turn_id = (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))
                AND l.consumed_at IS NULL
                AND l.expires_at > p_now
          )
        ORDER BY t.id
    LOOP
        identity := ticket_board.turn_unresolved_identity(
            candidate.id, candidate.state, candidate.assignee, candidate.turn_id
        );
        -- Reported once per turn identity. The marker is the dedupe key itself,
        -- which outlives the queue row, so acknowledging or delivering cannot
        -- cause a repeat for the same unresolved end (SYRD-133's lesson, applied
        -- to a different identity).
        CONTINUE WHEN EXISTS (
            SELECT 1 FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = candidate.id
              AND tr.kind = 'unresolved_turn'
              AND tr.event = 'enqueue'
              AND tr.detail ->> 'dedupe_key' = identity
        );
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'unresolved_turn',
            'director',
            format(
                '%s ended a turn without resolving %s (%s). It did not transition it, '
                || 'request a dependency, record a blocker, or declare it was continuing.',
                candidate.owner_role, candidate.id, candidate.state
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'owner_role', candidate.owner_role,
                'target_role', 'director',
                'turn_id', candidate.turn_id,
                'message', format(
                    '%s ended a turn without resolving %s (%s).',
                    candidate.owner_role, candidate.id, candidate.state
                )
            ),
            identity
        );
        enqueued := enqueued + 1;
    END LOOP;
    RETURN enqueued;
END;
$$;


COMMIT;
