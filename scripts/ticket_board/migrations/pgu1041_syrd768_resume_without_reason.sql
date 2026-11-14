-- SYRD-768 (pgu1041): resuming work needs no reason (the User's UAT of SYRD-756).
--
-- A pause and a change of mode still say why work stopped, which is worth recording. A resume only puts things back
-- to normal, so its reason is optional: resume_work records who resumed and when, and the reason only when one is
-- given (end_reason NULL otherwise). The resume notice a role is given says "no reason given" rather than quoting an
-- empty string. pause_work is unchanged and still refuses a pause, or a change of mode, without a reason.

BEGIN;

-- End the pause, whichever mode, with who and, when given, why. What waited is due now, once each, and asked again
-- before it goes; each role a stop pause interrupted or told is told once that work resumed.
CREATE OR REPLACE FUNCTION ticket_board.resume_work(p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text;
    cur ticket_board.work_pauses;
    r record;
    anchor text;
    told text[] := '{}';
    released integer := 0;
    now_at timestamptz := clock_timestamp();
BEGIN
    actor := ticket_board.work_pause_actor('resume_work');
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.work_pauses'));
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE ended_at IS NULL FOR UPDATE;
    IF cur.id IS NULL THEN RAISE EXCEPTION 'work is not paused'; END IF;
    UPDATE ticket_board.work_pauses SET ended_at = now_at, ended_by = actor,
           end_reason = nullif(left(btrim(coalesce(p_reason, '')), 500), '')
     WHERE id = cur.id RETURNING * INTO cur;
    UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = now_at
     WHERE q.last_error LIKE 'work_paused%' AND q.claimed_at IS NULL AND q.dead_lettered_at IS NULL;
    GET DIAGNOSTICS released = ROW_COUNT;
    FOR r IN SELECT * FROM ticket_board.work_pause_roles
              WHERE pause_id = cur.id AND (interrupted_at IS NOT NULL OR noticed_at IS NOT NULL)
                AND resumed_notice_at IS NULL
              ORDER BY role LOOP
        -- One interrupted but never told -- its prompt came back to its composer, say -- is told all the same.
        anchor := coalesce(r.notice_ticket, ticket_board.provider_hold_anchor(r.role));
        CONTINUE WHEN anchor IS NULL;
        PERFORM ticket_board.enqueue_notification(
            anchor, 'ticket_update', r.role, ticket_board.work_pause_notice_text('work_resumed', cur, anchor),
            jsonb_build_object('kind', 'work_resumed', 'reason', 'work_resumed', 'pause', cur.id, 'role', r.role,
                               'id', anchor, 'target_role', r.role),
            format('work-resumed:%s:%s', cur.id, r.role));
        UPDATE ticket_board.work_pause_roles SET resumed_notice_at = now_at WHERE pause_id = cur.id AND role = r.role;
        told := told || r.role;
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition',
                      jsonb_build_object('kind', 'work_resumed', 'pause', cur.id)::text);
    RETURN jsonb_build_object('pause', cur.id, 'resumed', true, 'by', actor, 'mode', cur.mode,
                              'released_notices', released, 'told', to_jsonb(told));
END;
$$;

-- The pause notice's text and the resume notice's; a resume with no reason says so.
CREATE OR REPLACE FUNCTION ticket_board.work_pause_notice_text(p_kind text, p_pause ticket_board.work_pauses,
                                                               p_ticket text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE p_kind
        WHEN 'work_pause' THEN format(
            'WORK PAUSED (pause %s): %s stopped all work in this project -- "%s". Your turn was interrupted on '
            'purpose. Record where you are on %s now: one board comment saying what is done, what is half-done and '
            'what is next. Then start nothing new -- no new command, ticket or transition -- until you are told work '
            'has resumed.', p_pause.id, p_pause.began_by, left(p_pause.reason, 300), coalesce(p_ticket, 'your ticket'))
        ELSE format(
            'WORK RESUMED (pause %s ended): %s resumed work%s. Carry on with %s from where you recorded you were.',
            p_pause.id, p_pause.ended_by,
            CASE WHEN p_pause.end_reason IS NULL THEN ' (no reason given)'
                 ELSE ' -- "' || left(p_pause.end_reason, 300) || '"' END,
            coalesce(p_ticket, 'your ticket')) END;
$$;

REVOKE ALL ON FUNCTION ticket_board.resume_work(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.work_pause_notice_text(text, ticket_board.work_pauses, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.resume_work(text) TO ticket_board_service;

COMMIT;
