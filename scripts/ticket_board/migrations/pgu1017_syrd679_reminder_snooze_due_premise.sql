-- SYRD-679: a reminder snooze's deadline notice is about the tickets still snoozed, when it is written and when it
-- is sent.
--
-- Batch 50 snoozed SYRD-677 and batch 51 SYRD-684. Each ticket reached Done before its batch's deadline, and its
-- snooze showed `invalidated` with what changed. At the deadline the Director was still sent "Reminder snooze batch
-- ... is due. Ready: none. Changed while snoozed: ...". emit_due_reminder_snoozes wrote a notice for every batch
-- with a member, and the delivery check passed any reminder_snooze_due notice without asking anything.
--
--   * emit_due_reminder_snoozes writes the notice only for a batch with a ticket still the one that was snoozed
--     (its snapshot unchanged: the same stage, owner, candidate, sign-offs, holds, waits and blockers). A batch
--     whose every member changed closes as before -- every member released, attributed -- with no notice and
--     close_reason saying so, so no later pass writes one. The notice hangs on a ticket it names as ready;
--   * reminder_snooze_due_stale_reason(payload) is asked at claim, before the send and after a hold: NULL while the
--     notice's batch closed with exactly one notice and a ticket it released as ready is still the one that was
--     snoozed; otherwise why not -- 'batch_missing', 'batch_mismatch' (the batch is still open, was cleared, or
--     closed without a notice), or 'no_ticket_still_snoozed' (each ready ticket has changed since -- completed,
--     reassigned, returned, blocked -- or was snoozed again in a later batch). The listener then drops it,
--     acknowledged and traced with that reason, and it is never retried;
--   * hold_reminder_snooze_due(payload) extends the delivery hold (SYRD-631) from the anchor's row to every ticket the
--     batch released as ready -- the rows that reason reads -- so a write to any of them waits for the send.
-- A batch with a ready ticket still gets its one notice, which also says what changed while snoozed.
--
-- schema.sql carries the same objects.

BEGIN;

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
            IF cardinality(changed) = 0 THEN
                anchor := coalesce(anchor, member.ticket_id);
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
        -- SYRD-679: only a ticket still the one that was snoozed makes the notice. A changed one's ordinary
        -- reminders resumed when it changed; a batch of nothing but those has nothing to report.
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
               close_reason = CASE WHEN notice IS NULL AND jsonb_array_length(moved) > 0
                                   THEN 'due; every ticket changed while snoozed, so no notice' ELSE 'due' END,
               notification_id = notice
         WHERE id = batch.id;
    END LOOP;
    RETURN emitted;
END;
$$;

-- Why a queued reminder_snooze_due notice is no longer to be sent, or NULL while it is: its batch closed at the
-- deadline with this one notice, and a ticket it released as ready is still the one that was snoozed.
CREATE OR REPLACE FUNCTION ticket_board.reminder_snooze_due_stale_reason(p_payload jsonb)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT CASE
        WHEN b.id IS NULL THEN 'batch_missing'
        -- Still open, cleared, or closed with nothing ready: no notice of this batch is to be sent.
        WHEN b.notification_id IS NULL THEN 'batch_mismatch'
        WHEN NOT EXISTS (
            SELECT 1 FROM ticket_board.reminder_snooze_members m
            WHERE m.batch_id = b.id AND m.release_reason = 'due'
              AND m.snapshot = ticket_board.reminder_snooze_snapshot(m.ticket_id)
              -- Snoozed again since, in a later batch: that batch speaks for it now.
              AND NOT EXISTS (SELECT 1 FROM ticket_board.reminder_snooze_members later
                              WHERE later.ticket_id = m.ticket_id AND later.batch_id > b.id)
        ) THEN 'no_ticket_still_snoozed'
    END
    FROM (SELECT 1) one
    LEFT JOIN ticket_board.reminder_snooze_batches b
      ON b.id = CASE WHEN p_payload ->> 'batch' ~ '^[0-9]{1,18}$' THEN (p_payload ->> 'batch')::bigint END;
$$;

-- The delivery hold (SYRD-631) for a deadline notice, taken in the hold's own transaction after the anchor's row.
-- hold_for_delivery holds only the anchor, but the notice stays current while ANY ticket the batch released as ready
-- is still the one snoozed: so each of their rows is held FOR SHARE too, and a write that would change one --
-- completing it, returning it, snoozing it again -- waits for the send. False at once (NOWAIT) when one is being
-- written now: the notice waits a pass.
CREATE OR REPLACE FUNCTION ticket_board.hold_reminder_snooze_due(p_payload jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id IN (SELECT m.ticket_id FROM ticket_board.reminder_snooze_members m
                   WHERE m.release_reason = 'due'
                     AND m.batch_id = CASE WHEN p_payload ->> 'batch' ~ '^[0-9]{1,18}$'
                                           THEN (p_payload ->> 'batch')::bigint END)
    FOR SHARE OF t NOWAIT;
    RETURN true;
EXCEPTION WHEN lock_not_available THEN
    RETURN false;
END;
$$;

REVOKE EXECUTE ON FUNCTION ticket_board.reminder_snooze_due_stale_reason(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.hold_reminder_snooze_due(jsonb) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT EXECUTE ON FUNCTION ticket_board.reminder_snooze_due_stale_reason(jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.hold_reminder_snooze_due(jsonb) TO ticket_board_listener;

COMMIT;
