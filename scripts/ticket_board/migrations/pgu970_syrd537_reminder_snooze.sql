-- SYRD-537: a Director's reminder snooze for named tickets, until a deadline,
-- and one queue-ready notice per batch when it is due.
--
-- New: the batch and member tables, the snapshot that defines "still the
-- ticket that was snoozed", the predicate every reminder generator and the
-- listener's delivery check ask, the Director's snooze/clear (preview unless
-- applied), the reads, and the listener's due-time emission.
--
-- Redefined with ONE added condition each, otherwise exactly the newest copy
-- (schema.sql carries the same text):
--   ticket_turn_is_resolved (pgu968)       -- unresolved-turn prompt and grace escalation
--   notify_idle_turn_end_nudges (pgu948)   -- idle reminder and its escalation
--   notify_idle_stall_nudges (pgu934)      -- Director nudge and escalation
--   notify_due_nudges (pgu934)             -- the pg_cron backstop nudge
-- Initial handoffs (transition, triage, a wait's first step), publication and
-- in-place update notices, and permission-prompt escalations are untouched.
BEGIN;

-- SYRD-537: a reminder snooze. The Director names tickets and a deadline; until
-- then the OPTIONAL reminders about exactly those tickets -- idle reminders,
-- nudges, their escalations, the unresolved-turn prompt and its grace
-- escalation, and the later steps of a wait's schedule -- are neither generated
-- nor delivered. Nothing else changes: stage, assignee, gates, the candidate,
-- blockers, reservations, every initial handoff and every other ticket's
-- notices. At the deadline the listener writes ONE notice per batch to the role
-- that set it, and ordinary policy resumes.
--
-- Membership is the list the Director named, frozen with the ticket as it was
-- (the snapshot below). A ticket that changes in any way that matters -- it
-- moves, changes hands or candidate, gains or loses a sign-off, gate, hold,
-- wait or blocker -- is no longer the ticket that was snoozed, so the snooze
-- stops applying to it at that moment, without any writer having to remember
-- to end it. No rule ever adds a ticket: future tickets are never muted.
CREATE TABLE IF NOT EXISTS ticket_board.reminder_snooze_batches (
    id bigserial PRIMARY KEY,
    due_at timestamptz NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'snoozed' CHECK (state IN ('snoozed', 'due_emitted', 'cleared')),
    closed_by text,
    closed_at timestamptz,
    close_reason text,
    notification_id bigint
);

CREATE TABLE IF NOT EXISTS ticket_board.reminder_snooze_members (
    batch_id bigint NOT NULL REFERENCES ticket_board.reminder_snooze_batches(id),
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    position integer NOT NULL,
    snapshot jsonb NOT NULL,
    released_at timestamptz,
    released_by text,
    release_reason text,
    PRIMARY KEY (batch_id, ticket_id)
);

-- A ticket is in at most one batch that has not released it.
CREATE UNIQUE INDEX IF NOT EXISTS reminder_snooze_members_one_open
    ON ticket_board.reminder_snooze_members (ticket_id)
    WHERE released_at IS NULL;

-- What a snooze was granted for. Comparing it with the ticket now is the whole
-- of "has it changed": unresolved blockers count as they do for every reminder
-- generator (a missing blocker ticket is an unresolved external one).
CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_snapshot(p_ticket text)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT jsonb_build_object(
        'state', t.state,
        'assignee', t.assignee,
        'commit_hash', t.commit_hash,
        'audit_signoff', t.audit_signoff,
        'needs_audit', t.needs_audit,
        'needs_inspection', t.needs_inspection,
        'inspector_signoff', t.inspector_signoff,
        'needs_user_signoff', t.needs_user_signoff,
        'user_signoff', t.user_signoff,
        'manually_controlled', t.manually_controlled,
        'parked', t.parked,
        'workflow_flags', coalesce(to_jsonb(t) -> 'workflow_flags', '{}'::jsonb),
        'awaiting_role', coalesce(ns.awaiting_role, ''),
        'awaiting_since_at', ns.awaiting_since_at,
        'blockers', coalesce((
            SELECT jsonb_agg(tb.blocker_ticket_id ORDER BY tb.blocker_ticket_id)
            FROM ticket_board.ticket_blockers tb
            LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
            WHERE tb.ticket_id = t.id
              AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
        ), '[]'::jsonb)
    )
    FROM ticket_board.tickets t
    LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id = p_ticket;
$$;

-- Which parts of the snapshot no longer hold. Every key, for a ticket that is gone.
CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_changes(p_snapshot jsonb, p_ticket text)
RETURNS text[]
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT coalesce(array_agg(keys.k ORDER BY keys.k), ARRAY[]::text[])
    FROM (SELECT ticket_board.reminder_snooze_snapshot(p_ticket) AS now_snapshot) cur
    CROSS JOIN LATERAL (
        SELECT jsonb_object_keys(p_snapshot)
        UNION
        SELECT jsonb_object_keys(coalesce(cur.now_snapshot, '{}'::jsonb))
    ) AS keys(k)
    WHERE cur.now_snapshot IS NULL
       OR p_snapshot -> keys.k IS DISTINCT FROM cur.now_snapshot -> keys.k;
$$;

-- THE question every reminder generator and the listener's delivery check ask:
-- is this ticket's optional reminder snoozed right now? Only while the ticket
-- is still the one that was snoozed, and either
--   * its batch is open -- the deadline alone does not end it, because then a
--     producer or an older queued row could reach the role before the batch's
--     one notice does; or
--   * the deadline's notice released it as ready and is still queued, not
--     dead-lettered: the notice is the first optional word about the batch,
--     and ordinary reminders follow it. A dead-lettered notice releases them,
--     so a notice that cannot be delivered never silences them for good.
-- p_now is kept for the callers; time no longer decides it, the notice does.
CREATE OR REPLACE FUNCTION ticket_board.ticket_reminders_snoozed(p_ticket text, p_now timestamptz)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.reminder_snooze_members m
        JOIN ticket_board.reminder_snooze_batches b ON b.id = m.batch_id
        WHERE m.ticket_id = p_ticket
          AND m.snapshot = ticket_board.reminder_snooze_snapshot(p_ticket)
          AND (
                (m.released_at IS NULL AND b.state = 'snoozed')
             OR (m.release_reason = 'due' AND b.state = 'due_emitted' AND EXISTS (
                    SELECT 1 FROM ticket_board.ticket_notification_queue q
                    WHERE q.id = b.notification_id AND q.dead_lettered_at IS NULL))
          )
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_utc(p_at timestamptz)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
$$;

-- One member as a reader sees it: snoozed, invalidated (and by what), due and
-- not yet reported, or released (and why). Reading changes nothing.
CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_member_status(
    p_batch ticket_board.reminder_snooze_batches,
    p_member ticket_board.reminder_snooze_members,
    p_now timestamptz
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    changed text[] := ticket_board.reminder_snooze_changes(p_member.snapshot, p_member.ticket_id);
    status text;
BEGIN
    IF p_member.released_at IS NOT NULL OR p_batch.state <> 'snoozed' THEN
        status := 'released';
    ELSIF cardinality(changed) > 0 THEN
        status := 'invalidated';
    ELSIF p_batch.due_at <= p_now THEN
        status := 'due';
    ELSE
        status := 'snoozed';
    END IF;
    RETURN jsonb_build_object(
        'ticket', p_member.ticket_id,
        'status', status,
        'changed', CASE WHEN status = 'invalidated' THEN to_jsonb(changed) ELSE '[]'::jsonb END,
        'released_at', ticket_board.reminder_snooze_utc(p_member.released_at),
        'released_by', p_member.released_by,
        'release_reason', p_member.release_reason
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_batch_json(
    p_batch ticket_board.reminder_snooze_batches,
    p_now timestamptz
)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT jsonb_build_object(
        'batch', p_batch.id,
        'state', p_batch.state,
        'until', ticket_board.reminder_snooze_utc(p_batch.due_at),
        'reason', p_batch.reason,
        'by', p_batch.created_by,
        'created_at', ticket_board.reminder_snooze_utc(p_batch.created_at),
        'closed_by', p_batch.closed_by,
        'closed_at', ticket_board.reminder_snooze_utc(p_batch.closed_at),
        'close_reason', p_batch.close_reason,
        'notification_id', p_batch.notification_id,
        'members', coalesce((
            SELECT jsonb_agg(ticket_board.reminder_snooze_member_status(p_batch, m, p_now) ORDER BY m.position)
            FROM ticket_board.reminder_snooze_members m
            WHERE m.batch_id = p_batch.id
        ), '[]'::jsonb)
    );
$$;

-- The ticket's snooze, for its JSON and the ticket view; NULL when none. Open,
-- or released as ready while its batch's notice is still on its way ("due").
CREATE OR REPLACE FUNCTION ticket_board.ticket_reminder_snooze(p_ticket text, p_now timestamptz)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT jsonb_build_object(
               'batch', b.id,
               'until', ticket_board.reminder_snooze_utc(b.due_at),
               'by', b.created_by,
               'reason', b.reason
           ) || (ticket_board.reminder_snooze_member_status(b, m, p_now) - 'ticket')
             || CASE WHEN m.released_at IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('status', 'due') END
    FROM ticket_board.reminder_snooze_members m
    JOIN ticket_board.reminder_snooze_batches b ON b.id = m.batch_id
    WHERE m.ticket_id = p_ticket
      AND ((m.released_at IS NULL AND b.state = 'snoozed')
           OR (m.release_reason = 'due' AND b.state = 'due_emitted' AND EXISTS (
                  SELECT 1 FROM ticket_board.ticket_notification_queue q
                  WHERE q.id = b.notification_id AND q.dead_lettered_at IS NULL)))
    ORDER BY b.id DESC
    LIMIT 1;
$$;

-- Every open batch, and the most recent closed ones.
CREATE OR REPLACE FUNCTION ticket_board.reminder_snoozes(p_now timestamptz)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT coalesce(jsonb_agg(ticket_board.reminder_snooze_batch_json(b, p_now) ORDER BY b.id), '[]'::jsonb)
    FROM ticket_board.reminder_snooze_batches b
    WHERE b.state = 'snoozed'
       OR b.id IN (SELECT closed.id FROM ticket_board.reminder_snooze_batches closed
                   WHERE closed.state <> 'snoozed' ORDER BY closed.id DESC LIMIT 20);
$$;

CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_ticket_ids(p_tickets text[])
RETURNS text[]
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(array_agg(id ORDER BY first_seen), ARRAY[]::text[])
    FROM (
        SELECT upper(btrim(raw)) AS id, min(ord) AS first_seen
        FROM unnest(coalesce(p_tickets, ARRAY[]::text[])) WITH ORDINALITY AS given(raw, ord)
        WHERE btrim(coalesce(raw, '')) <> ''
        GROUP BY upper(btrim(raw))
    ) named;
$$;

-- Snooze the named tickets' optional reminders until p_due_at. Without
-- p_apply it writes nothing and returns exactly what applying would do: the
-- members, the deadline in UTC, the memberships it would release because their
-- ticket changed, and every refusal. With p_apply, any refusal refuses all.
-- The same authority as holding a ticket (set_manually_controlled), which
-- silences every one of these reminders and more; this silences less.
CREATE OR REPLACE FUNCTION ticket_board.snooze_reminders(
    p_tickets text[],
    p_due_at timestamptz,
    p_reason text,
    p_apply boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    who text;
    reason text := btrim(coalesce(p_reason, ''));
    ids text[] := ticket_board.reminder_snooze_ticket_ids(p_tickets);
    tid text;
    now_ts timestamptz := clock_timestamp();
    refusals jsonb := '[]'::jsonb;
    members jsonb := '[]'::jsonb;
    releases jsonb := '[]'::jsonb;
    tk record;
    open_member record;
    changed text[];
    new_batch bigint;
    terminal boolean;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    who := ticket_board.current_app_actor();
    IF reason = '' THEN
        refusals := refusals || jsonb_build_object('reason', 'a reason is required');
    END IF;
    IF p_due_at IS NULL THEN
        refusals := refusals || jsonb_build_object('reason', 'a deadline is required');
    ELSIF p_due_at <= now_ts THEN
        refusals := refusals || jsonb_build_object('reason', format('the deadline %s is not in the future', ticket_board.reminder_snooze_utc(p_due_at)));
    ELSIF p_due_at > now_ts + interval '7 days' THEN
        refusals := refusals || jsonb_build_object('reason', format('the deadline %s is more than 7 days away', ticket_board.reminder_snooze_utc(p_due_at)));
    END IF;
    IF cardinality(ids) = 0 THEN
        refusals := refusals || jsonb_build_object('reason', 'name at least one ticket');
    ELSIF cardinality(ids) > 100 THEN
        refusals := refusals || jsonb_build_object('reason', 'at most 100 tickets per batch');
    END IF;

    FOREACH tid IN ARRAY ids LOOP
        IF p_apply THEN
            SELECT t.id, t.state, t.assignee INTO tk FROM ticket_board.tickets t WHERE t.id = tid FOR UPDATE;
        ELSE
            SELECT t.id, t.state, t.assignee INTO tk FROM ticket_board.tickets t WHERE t.id = tid;
        END IF;
        IF NOT FOUND THEN
            refusals := refusals || jsonb_build_object('ticket', tid, 'reason', 'not found');
            CONTINUE;
        END IF;
        terminal := CASE WHEN ticket_board.declared_workflow() IS NULL
                         THEN tk.state IN ('done', 'cancelled')
                         ELSE coalesce((SELECT s.is_terminal FROM ticket_board.workflow_stages s WHERE s.name = tk.state), false)
                    END;
        IF terminal THEN
            refusals := refusals || jsonb_build_object('ticket', tid, 'reason', format('in terminal stage %s', tk.state));
            CONTINUE;
        END IF;
        SELECT m.batch_id, m.snapshot, b.due_at, b.state AS batch_state INTO open_member
        FROM ticket_board.reminder_snooze_members m
        JOIN ticket_board.reminder_snooze_batches b ON b.id = m.batch_id
        WHERE m.ticket_id = tid AND m.released_at IS NULL;
        IF FOUND THEN
            changed := ticket_board.reminder_snooze_changes(open_member.snapshot, tid);
            IF open_member.batch_state = 'snoozed' AND cardinality(changed) = 0 THEN
                refusals := refusals || jsonb_build_object('ticket', tid, 'reason', CASE
                    WHEN open_member.due_at > now_ts
                        THEN format('already snoozed in batch %s until %s', open_member.batch_id, ticket_board.reminder_snooze_utc(open_member.due_at))
                    ELSE format('batch %s is due and not yet reported', open_member.batch_id) END);
                CONTINUE;
            END IF;
            releases := releases || jsonb_build_object('ticket', tid, 'batch', open_member.batch_id, 'changed', to_jsonb(changed));
        END IF;
        members := members || jsonb_build_object('ticket', tid, 'state', tk.state, 'assignee', tk.assignee);
    END LOOP;

    IF p_apply AND jsonb_array_length(refusals) > 0 THEN
        RAISE EXCEPTION 'reminder snooze refused: %', refusals::text;
    END IF;
    IF p_apply THEN
        UPDATE ticket_board.reminder_snooze_members m
           SET released_at = now_ts,
               released_by = who,
               release_reason = 'invalidated: ' || array_to_string(ticket_board.reminder_snooze_changes(m.snapshot, m.ticket_id), ', ') || ' changed'
         WHERE m.released_at IS NULL
           AND m.ticket_id IN (SELECT r ->> 'ticket' FROM jsonb_array_elements(releases) r);
        INSERT INTO ticket_board.reminder_snooze_batches (due_at, reason, created_by, created_at)
        VALUES (p_due_at, reason, who, now_ts)
        RETURNING id INTO new_batch;
        INSERT INTO ticket_board.reminder_snooze_members (batch_id, ticket_id, position, snapshot)
        SELECT new_batch, e.value ->> 'ticket', e.ordinality::integer,
               ticket_board.reminder_snooze_snapshot(e.value ->> 'ticket')
        FROM jsonb_array_elements(members) WITH ORDINALITY AS e(value, ordinality);
    END IF;
    RETURN jsonb_build_object(
        'applied', p_apply,
        'batch', new_batch,
        'until', ticket_board.reminder_snooze_utc(p_due_at),
        'reason', reason,
        'by', who,
        'members', members,
        'releases', releases,
        'refusals', refusals
    );
END;
$$;

-- End a snooze early: the named members of one open batch, or all of it, with
-- why. Ordinary reminders resume at once. Without p_apply, a preview.
CREATE OR REPLACE FUNCTION ticket_board.clear_reminder_snooze(
    p_batch bigint,
    p_tickets text[],
    p_reason text,
    p_apply boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    who text;
    reason text := btrim(coalesce(p_reason, ''));
    ids text[] := ticket_board.reminder_snooze_ticket_ids(p_tickets);
    now_ts timestamptz := clock_timestamp();
    batch ticket_board.reminder_snooze_batches;
    open_ids text[];
    targets text[];
    refusals jsonb := '[]'::jsonb;
    tid text;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    who := ticket_board.current_app_actor();
    IF reason = '' THEN
        refusals := refusals || jsonb_build_object('reason', 'a reason is required');
    END IF;
    IF p_apply THEN
        SELECT * INTO batch FROM ticket_board.reminder_snooze_batches WHERE id = p_batch FOR UPDATE;
    ELSE
        SELECT * INTO batch FROM ticket_board.reminder_snooze_batches WHERE id = p_batch;
    END IF;
    IF NOT FOUND THEN
        refusals := refusals || jsonb_build_object('reason', format('batch %s not found', p_batch));
    ELSIF batch.state <> 'snoozed' THEN
        refusals := refusals || jsonb_build_object('reason', format('batch %s is already %s', p_batch, batch.state));
    ELSE
        SELECT coalesce(array_agg(m.ticket_id ORDER BY m.position), ARRAY[]::text[]) INTO open_ids
        FROM ticket_board.reminder_snooze_members m
        WHERE m.batch_id = p_batch AND m.released_at IS NULL;
        IF cardinality(ids) = 0 THEN
            targets := open_ids;
        ELSE
            targets := ARRAY[]::text[];
            FOREACH tid IN ARRAY ids LOOP
                IF tid = ANY (open_ids) THEN
                    targets := targets || tid;
                ELSE
                    refusals := refusals || jsonb_build_object('ticket', tid, 'reason', format('not snoozed in batch %s', p_batch));
                END IF;
            END LOOP;
        END IF;
        IF cardinality(targets) = 0 AND jsonb_array_length(refusals) = 0 THEN
            refusals := refusals || jsonb_build_object('reason', format('batch %s has no snoozed tickets', p_batch));
        END IF;
    END IF;

    IF p_apply AND jsonb_array_length(refusals) > 0 THEN
        RAISE EXCEPTION 'reminder snooze clear refused: %', refusals::text;
    END IF;
    IF p_apply THEN
        UPDATE ticket_board.reminder_snooze_members m
           SET released_at = now_ts, released_by = who, release_reason = 'cleared: ' || reason
         WHERE m.batch_id = p_batch AND m.released_at IS NULL AND m.ticket_id = ANY (targets);
        IF NOT EXISTS (SELECT 1 FROM ticket_board.reminder_snooze_members m
                       WHERE m.batch_id = p_batch AND m.released_at IS NULL) THEN
            UPDATE ticket_board.reminder_snooze_batches
               SET state = 'cleared', closed_by = who, closed_at = now_ts, close_reason = reason
             WHERE id = p_batch;
        END IF;
    END IF;
    RETURN jsonb_build_object(
        'applied', p_apply,
        'batch', p_batch,
        'reason', reason,
        'by', who,
        'releases', to_jsonb(coalesce(targets, ARRAY[]::text[])),
        'remaining', to_jsonb(coalesce(ARRAY(SELECT x FROM unnest(open_ids) x WHERE x <> ALL (coalesce(targets, ARRAY[]::text[]))), ARRAY[]::text[])),
        'refusals', refusals
    );
END;
$$;

-- At each batch's deadline, ONE notice to the role that set it: which tickets
-- are ready, which changed while snoozed. The batch closes, every member is
-- released, and ordinary policy resumes. The row lock, the state flip and the
-- notice commit together, so concurrent listener passes and restarts create it
-- at most once, and a pass that crashed before commit leaves it for the next.
-- Delivery is the queue's: receipt semantics, not a promise of human
-- exactly-once.
CREATE OR REPLACE FUNCTION ticket_board.emit_due_reminder_snoozes(p_now timestamptz)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    batch ticket_board.reminder_snooze_batches;
    member record;
    changed text[];
    ready jsonb;
    moved jsonb;
    anchor text;
    ready_text text;
    moved_text text;
    payload jsonb;
    notice bigint;
    emitted integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('emit_due_reminder_snoozes');
    FOR batch IN
        SELECT * FROM ticket_board.reminder_snooze_batches
        WHERE state = 'snoozed' AND due_at <= p_now
        ORDER BY due_at, id
        FOR UPDATE SKIP LOCKED
    LOOP
        ready := '[]'::jsonb;
        moved := '[]'::jsonb;
        anchor := NULL;
        FOR member IN
            SELECT m.ticket_id, m.snapshot, t.state, t.assignee
            FROM ticket_board.reminder_snooze_members m
            JOIN ticket_board.tickets t ON t.id = m.ticket_id
            WHERE m.batch_id = batch.id AND m.released_at IS NULL
            ORDER BY m.position
        LOOP
            changed := ticket_board.reminder_snooze_changes(member.snapshot, member.ticket_id);
            anchor := coalesce(anchor, member.ticket_id);
            IF cardinality(changed) = 0 THEN
                ready := ready || jsonb_build_object('ticket', member.ticket_id, 'state', member.state, 'assignee', member.assignee);
            ELSE
                moved := moved || jsonb_build_object('ticket', member.ticket_id, 'state', member.state,
                                                     'assignee', member.assignee, 'changed', to_jsonb(changed));
            END IF;
            UPDATE ticket_board.reminder_snooze_members
               SET released_at = p_now,
                   released_by = 'board',
                   release_reason = CASE WHEN cardinality(changed) = 0 THEN 'due'
                                         ELSE 'due; ' || array_to_string(changed, ', ') || ' changed' END
             WHERE batch_id = batch.id AND ticket_id = member.ticket_id;
        END LOOP;
        notice := NULL;
        IF anchor IS NOT NULL THEN
            SELECT coalesce(string_agg(format('%s (%s, %s)', r ->> 'ticket', r ->> 'state', r ->> 'assignee'), ', '), 'none')
              INTO ready_text FROM jsonb_array_elements(ready) r;
            SELECT string_agg(format('%s (%s changed; now %s, %s)', r ->> 'ticket',
                                     (SELECT string_agg(c, ', ') FROM jsonb_array_elements_text(r -> 'changed') c),
                                     r ->> 'state', r ->> 'assignee'), '; ')
              INTO moved_text FROM jsonb_array_elements(moved) r;
            payload := jsonb_build_object(
                'kind', 'reminder_snooze_due',
                'id', anchor,
                'batch', batch.id,
                'until', ticket_board.reminder_snooze_utc(batch.due_at),
                'reason', batch.reason,
                'target_role', batch.created_by,
                'queue_ready', ready,
                'changed', moved
            );
            notice := ticket_board.enqueue_notification(
                anchor,
                'ticket_update',
                batch.created_by,
                format('Reminder snooze batch %s is due (set by %s until %s: %s). Ready: %s.%s '
                       || 'Ordinary reminders resume for all of them.',
                       batch.id, batch.created_by, ticket_board.reminder_snooze_utc(batch.due_at), batch.reason,
                       ready_text,
                       CASE WHEN moved_text IS NULL THEN '' ELSE ' Changed while snoozed: ' || moved_text || '.' END),
                payload,
                'reminder-snooze-due:' || batch.id
            );
            emitted := emitted + 1;
        END IF;
        UPDATE ticket_board.reminder_snooze_batches
           SET state = 'due_emitted', closed_by = 'board', closed_at = p_now,
               close_reason = 'due', notification_id = notice
         WHERE id = batch.id;
    END LOOP;
    RETURN emitted;
END;
$$;

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
        -- SYRD-537: the Director snoozed this ticket's optional reminders, and it is
        -- still the ticket that was snoozed. Neither the prompt nor its escalation.
        OR ticket_board.ticket_reminders_snoozed(t.id, p_now)
        OR ticket_board.ticket_serial_focus_reservation_is_current(
               t.id, t.queued_for_assignee, t.queued_behind_ticket)
        -- SYRD-513: not yet handed to its owner. While the owner's transition
        -- notice for this stage is still queued -- held by serial review
        -- admission behind the ticket the owner is working on, or waiting for
        -- a busy pane -- the owner has never been told about this ticket, so
        -- no turn they end is about it. A dead-lettered notice is the failure
        -- path's to report, not this one's.
        OR EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = t.id
              AND q.kind = 'transition'
              AND q.target_role = ticket_board.transition_target_role(t.state, t.assignee)
              AND q.payload ->> 'new_state' = t.state
              AND q.dead_lettered_at IS NULL
        )
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

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_turn_end_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz,
    p_active_grace interval,
    p_work_observed_at jsonb
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
                WHERE NOT ticket_board.ticket_reminders_snoozed(t.id, p_now)  -- SYRD-537
                  AND (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
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
            -- A turn ending is not a stall. A review takes several turns, and
            -- between two of them the pane is idle by every measure this
            -- generator had: the hook says idle and the live gate agrees,
            -- because at that instant nothing is running. Audit was told it
            -- had not advanced SYRD-162 seconds after being handed it, and the
            -- next turn boundary escalated that to the Director as "may be
            -- stuck" -- inside a minute, while the review was happening
            -- (SYRD-163).
            --
            -- So the clock a reminder is measured against starts at the LAST
            -- OBSERVED WORK as well as at the stage and the idle hook, and the
            -- role has to have been quiet for the grace period before any of
            -- this is due. Same rule for every role: the stall generator
            -- beside this one has taken both inputs since SYRD-58, and the
            -- asymmetry is what let this through.
            WHERE greatest(
                    notification_scope.entered_current_state_at,
                    notification_scope.idle_since_at,
                    coalesce(
                        (p_work_observed_at ->> notification_scope.owner_role)::timestamptz,
                        '-infinity'::timestamptz
                    )
                  ) + p_active_grace <= p_now
              AND notification_scope.owner_role IS NOT NULL
        ) AS candidates
        WHERE (candidates.state <> 'in_progress' OR candidates.in_progress_rank = 1)
          AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidates.id
              AND q.target_role = candidates.target_role
              AND q.kind NOT IN ('idle_reminder', 'escalation', 'unresolved_turn')
        )
          -- "was reminded about X and still hasn't advanced it" has to be TRUE
          -- when it is said. The counter alone could not make it true: a
          -- reminder that was enqueued and never delivered still incremented
          -- it, and two turn boundaries a few seconds apart still reached it.
          -- So an escalation waits for a reminder that was actually SENT to
          -- that owner, in this stage, and for the grace period to pass
          -- afterwards -- which is the time the reminder was asking for
          -- (SYRD-163).
          AND (
            candidates.kind <> 'escalation'
            OR EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state reminded
                WHERE reminded.ticket_id = candidates.id
                  AND reminded.last_idle_reminder_at IS NOT NULL
                  AND reminded.last_idle_reminder_at + p_active_grace <= p_now
            )
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
                      escalated.last_activity_at
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
                       candidate.id, candidate.state, candidate.assignee,
                       ticket_notification_state.last_activity_at
                   )
             WHERE ticket_id = candidate.id;
        END IF;
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
            WHERE NOT ticket_board.ticket_reminders_snoozed(t.id, p_now)  -- SYRD-537
              AND (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('in_progress', 'inspection', 'audit', 'dat') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
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
            WHERE NOT ticket_board.ticket_reminders_snoozed(t.id, p_now)  -- SYRD-537
              AND (
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
            WHERE NOT ticket_board.ticket_reminders_snoozed(t.id, p_now)  -- SYRD-537
              AND (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
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
            WHERE NOT ticket_board.ticket_reminders_snoozed(t.id, p_now)  -- SYRD-537
              AND (
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

-- Deploys run rbac.sql after migrations (SYRD-530), and it carries these too.
REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snooze_snapshot(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snooze_changes(jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_reminders_snoozed(text, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snooze_member_status(ticket_board.reminder_snooze_batches, ticket_board.reminder_snooze_members, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snooze_batch_json(ticket_board.reminder_snooze_batches, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_reminder_snooze(text, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snoozes(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.snooze_reminders(text[], timestamptz, text, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.clear_reminder_snooze(bigint, text[], text, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.emit_due_reminder_snoozes(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.snooze_reminders(text[], timestamptz, text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.clear_reminder_snooze(bigint, text[], text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_reminder_snooze(text, timestamptz) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.reminder_snoozes(timestamptz) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_reminders_snoozed(text, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.emit_due_reminder_snoozes(timestamptz) TO ticket_board_listener;

COMMIT;
