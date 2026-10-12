-- SYRD-585: a Director could preview ending a reminder snooze, but applying a
-- clear that released a batch's last snoozed ticket failed inside the board:
--   column reference "reason" is ambiguous
-- clear_reminder_snooze kept the supplied reason in a variable named `reason`,
-- which is also a column of reminder_snooze_batches, and the batch close's
-- UPDATE named it. The function refused before committing anything, so no
-- batch could ever be cleared whole. A clear that left a ticket snoozed never
-- reached that UPDATE, which is why the one in SYRD-537's test passed.
--
-- The variable is renamed. The signature is unchanged, so no grant moves.
-- schema.sql carries the same text.
BEGIN;

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
    -- SYRD-585: not `reason`, which is also a reminder_snooze_batches column;
    -- the batch close below could not tell the two apart and refused to run.
    clear_reason text := btrim(coalesce(p_reason, ''));
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
    IF clear_reason = '' THEN
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
           SET released_at = now_ts, released_by = who, release_reason = 'cleared: ' || clear_reason
         WHERE m.batch_id = p_batch AND m.released_at IS NULL AND m.ticket_id = ANY (targets);
        IF NOT EXISTS (SELECT 1 FROM ticket_board.reminder_snooze_members m
                       WHERE m.batch_id = p_batch AND m.released_at IS NULL) THEN
            UPDATE ticket_board.reminder_snooze_batches
               SET state = 'cleared', closed_by = who, closed_at = now_ts, close_reason = clear_reason
             WHERE id = p_batch;
        END IF;
    END IF;
    RETURN jsonb_build_object(
        'applied', p_apply,
        'batch', p_batch,
        'reason', clear_reason,
        'by', who,
        'releases', to_jsonb(coalesce(targets, ARRAY[]::text[])),
        'remaining', to_jsonb(coalesce(ARRAY(SELECT x FROM unnest(open_ids) x WHERE x <> ALL (coalesce(targets, ARRAY[]::text[]))), ARRAY[]::text[])),
        'refusals', refusals
    );
END;
$$;

COMMIT;
