-- SYRD-827 (pgu1048): a reporting project's Director is told, once per close, when a ticket it filed upstream ends.
--
-- The User, 2026-11-16: "when another tenant files a ticket upstream, the board tells that project's director when
-- the ticket has been closed or cancelled". The reporting board's own notify listener asks each ancestor, with the
-- report credential it already holds, for its own reports' terminal states (scripts/ticket_board/report_closure.py),
-- and records each close here. A close is one terminal state of one upstream ticket at one reopen count: a reopened
-- and closed-again ticket is a new close, and a ticket still open is none. Nothing upstream writes to this board.
--
-- schema.sql carries the same table and function; rbac.sql grants the function to the listener alone.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.report_closures (
    -- SYRD-827: each close of a ticket this project filed upstream, as this board's listener saw it. Its row is the
    -- "told once" record: a later pass finds it and sends nothing.
    upstream text NOT NULL CHECK (upstream ~ '^[a-z0-9][a-z0-9_]{0,39}$'),
    ticket_id text NOT NULL CHECK (ticket_id ~ '^[A-Z][A-Z0-9]*-[0-9]+$'),
    close_key text NOT NULL CHECK (close_key ~ '^[a-z_]{1,40}#[0-9]{1,9}$'),
    state text NOT NULL,
    commit_hash text NOT NULL DEFAULT '',
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    told boolean NOT NULL,
    anchor_ticket text,
    notification_id bigint,
    PRIMARY KEY (upstream, ticket_id, close_key)
);

CREATE OR REPLACE FUNCTION ticket_board.note_report_closed(p_upstream text, p_ticket text, p_close_key text,
                                                           p_state text, p_commit text, p_message text, p_tell boolean)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-827: the reporting board's listener records one close of a ticket its project filed upstream, and, with
-- p_tell, queues one Director notice for it -- as any Director notice, held while the pane is busy. 'told';
-- 'recorded' (p_tell false: a close too old to announce, kept so it is never announced); 'already' when this close
-- was recorded before; or 'no_anchor' when the board has no ticket to carry a Director notice (nothing is recorded,
-- so a later pass tells it). Granted to ticket_board_listener alone (rbac.sql).
DECLARE anchor text; queued bigint;
BEGIN
    IF coalesce(p_upstream, '') !~ '^[a-z0-9][a-z0-9_]{0,39}$' OR coalesce(p_ticket, '') !~ '^[A-Z][A-Z0-9]*-[0-9]+$'
       OR coalesce(p_close_key, '') !~ '^[a-z_]{1,40}#[0-9]{1,9}$' OR btrim(coalesce(p_state, '')) = '' THEN
        RAISE EXCEPTION 'a report close names its upstream project, ticket, close and state';
    END IF;
    IF coalesce(p_commit, '') !~ '^([0-9a-f]{7,40})?$' THEN
        RAISE EXCEPTION 'a report close names its commit as a hash, or not at all';
    END IF;
    IF btrim(coalesce(p_message, '')) = '' OR length(p_message) > 4000 THEN
        RAISE EXCEPTION 'a report close notice carries a message of 1 to 4000 characters';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.report_closures'));
    IF EXISTS (SELECT FROM ticket_board.report_closures r
                WHERE r.upstream = p_upstream AND r.ticket_id = p_ticket AND r.close_key = p_close_key) THEN
        RETURN 'already';
    END IF;
    IF NOT coalesce(p_tell, false) THEN
        INSERT INTO ticket_board.report_closures (upstream, ticket_id, close_key, state, commit_hash, told)
        VALUES (p_upstream, p_ticket, p_close_key, p_state, coalesce(p_commit, ''), false);
        RETURN 'recorded';
    END IF;
    anchor := ticket_board.provider_hold_anchor('director');
    IF anchor IS NULL THEN
        RETURN 'no_anchor';
    END IF;
    queued := ticket_board.enqueue_notification(
        anchor, 'ticket_update', 'director', p_message,
        jsonb_build_object('kind', 'report_closed', 'id', anchor, 'target_role', 'director', 'upstream', p_upstream,
                           'upstream_ticket', p_ticket, 'upstream_state', p_state, 'commit', coalesce(p_commit, ''),
                           'message', p_message),
        'report_closed:' || p_upstream || ':' || p_ticket || ':' || p_close_key);
    INSERT INTO ticket_board.report_closures (upstream, ticket_id, close_key, state, commit_hash, told, anchor_ticket,
                                              notification_id)
    VALUES (p_upstream, p_ticket, p_close_key, p_state, coalesce(p_commit, ''), true, anchor, queued);
    RETURN 'told';
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.note_report_closed(text, text, text, text, text, text, boolean) FROM PUBLIC;

COMMIT;
