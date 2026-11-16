-- SYRD-799 (pgu1046): root tells each tenant's Director, once, that a new release is ready.
--
-- The User, 2026-11-15: "what do you think about the ./install script automatically informing all active directors
-- that a new version is ready for them to upgrade to? I find its a lot going to each one and telling them by hand".
-- At its end, ./install (scripts/release_notice.py, as root) tells each registered tenant whose board runs another
-- release. This is the one write it makes to a tenant's board: note_release_available, for the database superuser
-- alone, carrying the release, the tenant's current one and the notice. Nothing is upgraded, restarted or deployed.
--
-- schema.sql carries the same table and function.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.release_notices (
    -- SYRD-799: each release root has told this board's Director about, once: a reinstall or a retried telling of the
    -- same release finds its row and sends nothing.
    commit text PRIMARY KEY CHECK (commit ~ '^[0-9a-f]{40}$'),
    current_release text NOT NULL CHECK (current_release ~ '^[0-9a-f]{40}$'),
    noticed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    anchor_ticket text NOT NULL,
    notification_id bigint
);

CREATE OR REPLACE FUNCTION ticket_board.note_release_available(p_commit text, p_current text, p_message text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-799: root tells this board's Director that a release is ready, once per release. Executable by the database
-- superuser alone -- root, through `runuser -u postgres` on the board's own database, named by root's provisioning
-- record -- and no board role: rbac.sql grants it to none. It upgrades nothing; it queues one Director notice, as any
-- Director notice, held while the pane is busy. 'told', 'already' when this release was told before, or 'no_anchor'
-- when the board has no ticket to carry a Director notice (nothing is recorded, so a later install tells it).
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
    INSERT INTO ticket_board.release_notices (commit, current_release, anchor_ticket, notification_id)
    VALUES (p_commit, p_current, anchor, queued);
    RETURN 'told';
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.note_release_available(text, text, text) FROM PUBLIC;

COMMIT;
