-- SYRD-870 (pgu1058): a release notice's record keeps what it said.
--
-- A report closed upstream on a fix neither the reporter's release nor the installed one carries is kept silent for
-- the next ./install, whose release notice names the reports it resolves (SYRD-868), and is told late if no notice
-- reached this board (SYRD-869). A notice can reach the board and still name nothing -- root's read of the upstream
-- board failed -- so only a notice that named the report may end that wait. The queued notice is deleted once
-- delivered, so the record keeps the message: release_notices.message, written by note_release_available (same
-- signature), read by this board's listener (scripts/ticket_board/report_closure.py). A record from before this has
-- none, and names nothing.
--
-- schema.sql carries the same column and function; rbac.sql already lets the listener read release_notices.

BEGIN;

ALTER TABLE ticket_board.release_notices ADD COLUMN IF NOT EXISTS message text;

CREATE OR REPLACE FUNCTION ticket_board.note_release_available(p_commit text, p_current text, p_message text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-799: root tells this board's Director that a release is ready, once per release. Executable by the database
-- superuser alone -- root, through `runuser -u postgres` on the board's own database, named by root's provisioning
-- record -- and no board role: rbac.sql grants it to none. It upgrades nothing; it queues one Director notice, as any
-- Director notice, held while the pane is busy. 'told', 'already' when this release was told before, or 'no_anchor'
-- when the board has no ticket to carry a Director notice (nothing is recorded, so a later install tells it).
-- SYRD-870 (pgu1058): the message is kept with the record, so this board's listener can tell which upstream reports
-- the notice named.
DECLARE anchor text; queued bigint;
BEGIN
    IF coalesce(p_commit, '') !~ '^[0-9a-f]{40}$' OR coalesce(p_current, '') !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'a release notice names two full commits';
    END IF;
    IF btrim(coalesce(p_message, '')) = '' OR length(p_message) > 4000 THEN
        RAISE EXCEPTION 'a release notice carries a message of 1 to 4000 characters';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.release_notices'));
    IF EXISTS (SELECT FROM ticket_board.release_notices r WHERE r.commit = p_commit) THEN
        RETURN 'already';
    END IF;
    anchor := ticket_board.provider_hold_anchor('director');
    IF anchor IS NULL THEN
        RETURN 'no_anchor';
    END IF;
    queued := ticket_board.enqueue_notification(
        anchor, 'ticket_update', 'director', p_message,
        jsonb_build_object('kind', 'release_available', 'id', anchor, 'target_role', 'director',
                           'commit', p_commit, 'current', p_current, 'message', p_message),
        'release_available:' || p_commit);
    INSERT INTO ticket_board.release_notices (commit, current_release, anchor_ticket, notification_id, message)
    VALUES (p_commit, p_current, anchor, queued, p_message);
    RETURN 'told';
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.note_release_available(text, text, text) FROM PUBLIC;

COMMIT;
