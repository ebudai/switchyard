-- SYRD-565: what a notice's delivery proved, kept until it is settled.
--
-- On otto, four "receipt not witnessed" notices were real turns whose first act
-- was a slow preflight compression starting 15-16 s after the send -- just past
-- the listener's fixed witness window -- so they were recorded unconfirmed for
-- good. And a nudge typed into a Hermes composer was never submitted, for 50
-- minutes, while the board believed it delivered; only directorctl's unread
-- stderr said otherwise.
--
-- The listener now proves three things apart: the text went in (directorctl),
-- it left the composer (submission evidence, read from the pane), and the
-- recipient's runtime started a turn (receipt, its own hooks). This table keeps
-- whatever is still owed past the send, so a restarted listener carries on
-- instead of sending again:
--
--   unsubmitted     -- the text is still in the composer. The queue row is kept
--                      (never acknowledged) and parked; the listener presses
--                      submit only while the composer holds exactly what it
--                      held after the send, and tells the Director once if that
--                      never works.
--   receipt_pending -- the text left the composer, no turn started inside the
--                      short window. The queue row is acknowledged (the notice
--                      is in) and the receipt is watched up to a horizon; a
--                      late turn records the delivery, none tells the Director.
--
-- Settled rows are kept as the record: delivered, receipt_missed, abandoned.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.notification_proofs (
    notification_id bigint PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    target_role text NOT NULL,
    kind text NOT NULL,
    target text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL
        CHECK (state IN ('unsubmitted', 'receipt_pending', 'delivered', 'receipt_missed', 'abandoned')),
    sent_at timestamptz NOT NULL,
    composer_sha256 text NOT NULL DEFAULT '',
    submit_attempts integer NOT NULL DEFAULT 0 CHECK (submit_attempts >= 0),
    submitted_at timestamptz,
    horizon_at timestamptz,
    director_told_at timestamptz,
    detail text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS notification_proofs_open
    ON ticket_board.notification_proofs (state) WHERE state IN ('unsubmitted', 'receipt_pending');

-- The text is still in the composer: keep the notice, park it, remember what
-- the composer held. One statement, so no restart can find it acknowledged or
-- free to be typed again.
CREATE OR REPLACE FUNCTION ticket_board.record_unsubmitted_notification(
    p_notification_id bigint, p_target text, p_sent_at timestamptz, p_composer_sha256 text, p_park interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('record_unsubmitted_notification');
    INSERT INTO ticket_board.notification_proofs
        (notification_id, ticket_id, target_role, kind, target, payload, state, sent_at, composer_sha256)
    SELECT q.id, q.ticket_id, q.target_role, q.kind, p_target, q.payload, 'unsubmitted', p_sent_at, p_composer_sha256
    FROM ticket_board.ticket_notification_queue q WHERE q.id = p_notification_id
    ON CONFLICT (notification_id) DO UPDATE SET state = 'unsubmitted', target = EXCLUDED.target,
        sent_at = EXCLUDED.sent_at, composer_sha256 = EXCLUDED.composer_sha256, updated_at = clock_timestamp();
    PERFORM ticket_board.requeue_notification(p_notification_id, p_park, 'unsubmitted_in_composer');
END;
$$;

-- The text left the composer and no turn has started yet: the notice is in, so
-- it is acknowledged, and its receipt is watched. One statement, for the same
-- reason.
CREATE OR REPLACE FUNCTION ticket_board.watch_notification_receipt(
    p_notification_id bigint, p_target text, p_sent_at timestamptz, p_submitted_at timestamptz, p_horizon interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('watch_notification_receipt');
    INSERT INTO ticket_board.notification_proofs
        (notification_id, ticket_id, target_role, kind, target, payload, state, sent_at, submitted_at, horizon_at)
    SELECT q.id, q.ticket_id, q.target_role, q.kind, p_target, q.payload, 'receipt_pending', p_sent_at,
           p_submitted_at, p_submitted_at + p_horizon
    FROM ticket_board.ticket_notification_queue q WHERE q.id = p_notification_id
    ON CONFLICT (notification_id) DO UPDATE SET state = 'receipt_pending', target = EXCLUDED.target,
        submitted_at = EXCLUDED.submitted_at, horizon_at = EXCLUDED.horizon_at, updated_at = clock_timestamp();
    PERFORM ticket_board.ack_notification(p_notification_id);
END;
$$;

-- Everything still owed, oldest first, with whether its queue row is still there.
CREATE OR REPLACE FUNCTION ticket_board.open_notification_proofs()
RETURNS TABLE(notification_id bigint, ticket_id text, target_role text, kind text, target text, state text,
              sent_at timestamptz, composer_sha256 text, submit_attempts integer, submitted_at timestamptz,
              horizon_at timestamptz, director_told boolean, queued boolean, payload jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('open_notification_proofs');
    RETURN QUERY
    SELECT p.notification_id, p.ticket_id, p.target_role, p.kind, p.target, p.state, p.sent_at, p.composer_sha256,
           p.submit_attempts, p.submitted_at, p.horizon_at, p.director_told_at IS NOT NULL,
           EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                   WHERE q.id = p.notification_id AND q.dead_lettered_at IS NULL),
           p.payload
    FROM ticket_board.notification_proofs p
    WHERE p.state IN ('unsubmitted', 'receipt_pending')
    ORDER BY p.sent_at, p.notification_id;
END;
$$;

-- One more press of submit on text still in the composer.
CREATE OR REPLACE FUNCTION ticket_board.note_notification_submit_attempt(p_notification_id bigint)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempts integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_notification_submit_attempt');
    UPDATE ticket_board.notification_proofs SET submit_attempts = submit_attempts + 1, updated_at = clock_timestamp()
    WHERE notification_id = p_notification_id RETURNING submit_attempts INTO attempts;
    RETURN coalesce(attempts, 0);
END;
$$;

-- Settle, or report, what a notice proved. A Director notice is queued at most
-- once per notice, and only for a proof that failed or is stuck.
CREATE OR REPLACE FUNCTION ticket_board.resolve_notification_proof(
    p_notification_id bigint, p_state text, p_detail text DEFAULT '', p_director_notice text DEFAULT NULL)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE proof ticket_board.notification_proofs%ROWTYPE;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('resolve_notification_proof');
    UPDATE ticket_board.notification_proofs
    SET state = coalesce(nullif(p_state, ''), state), detail = coalesce(p_detail, ''), updated_at = clock_timestamp()
    WHERE notification_id = p_notification_id
    RETURNING * INTO proof;
    IF NOT FOUND OR p_director_notice IS NULL OR proof.director_told_at IS NOT NULL THEN
        RETURN;
    END IF;
    PERFORM ticket_board.enqueue_notification(
        proof.ticket_id, 'ticket_update', 'director', p_director_notice,
        jsonb_build_object('kind', 'delivery_proof', 'id', proof.ticket_id, 'target_role', 'director',
                           'notification_id', proof.notification_id, 'recipient', proof.target_role,
                           'proof', proof.state, 'message', p_director_notice),
        'delivery_proof:' || proof.notification_id);
    UPDATE ticket_board.notification_proofs SET director_told_at = clock_timestamp()
    WHERE notification_id = p_notification_id;
END;
$$;

REVOKE ALL ON ticket_board.notification_proofs FROM PUBLIC;
GRANT SELECT ON ticket_board.notification_proofs TO ticket_board_service;
REVOKE EXECUTE ON FUNCTION ticket_board.record_unsubmitted_notification(bigint, text, timestamptz, text, interval) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.watch_notification_receipt(bigint, text, timestamptz, timestamptz, interval) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.open_notification_proofs() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_notification_submit_attempt(bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.resolve_notification_proof(bigint, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.record_unsubmitted_notification(bigint, text, timestamptz, text, interval) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.watch_notification_receipt(bigint, text, timestamptz, timestamptz, interval) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.open_notification_proofs() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_notification_submit_attempt(bigint) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.resolve_notification_proof(bigint, text, text, text) TO ticket_board_listener;

COMMIT;
