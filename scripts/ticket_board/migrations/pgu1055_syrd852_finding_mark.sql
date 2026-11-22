-- SYRD-852 (pgu1055): a finding's acceptance mark goes when the finding stops being one in publication.
--
-- SYRD-849's publish_finding marks the round's finding as accepted for publication (ticket_research_briefs.finding
-- ? 'publication'), and the Publisher takes the finding's moves -- on to done, no Audit code sign-off -- for a marked
-- research ticket (publication_is_finding). The mark was cleared only when the ticket entered Research again, so a
-- finding deferred out of publication, made an ordinary ticket (needs_research off) and taken through Audit into
-- publication still carried it; only publication_is_finding's needs_research condition kept the Publisher from
-- treating that code ticket as a finding.
--
-- note_research_round now clears the mark when needs_research goes off; when the ticket leaves publication and
-- publication_failed by anything but finding_published (a defer, a cancel, a route, a force-move; the finding's own
-- returns enter Research, which clears it as before); and when it enters publication by anything but publish_finding
-- or publication_retry.
--
-- schema.sql carries the same definition at its tail.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.note_research_round()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE action text := nullif(current_setting('ticket_board.workflow_action', true), ''); cfg jsonb;
BEGIN
    IF NOT ticket_board.research_declared() THEN
        RETURN NULL;
    END IF;
    -- SYRD-852: the finding's acceptance mark holds only while it is a research ticket in the publication stages by
    -- the finding's own moves. It goes when needs_research goes off; when the ticket leaves those stages by anything
    -- but finding_published; and when it enters publication by anything but publish_finding or publication_retry.
    cfg := ticket_board.declared_workflow();
    IF (ticket_board.workflow_flag(to_jsonb(OLD), 'needs_research', cfg)
            AND NOT ticket_board.workflow_flag(to_jsonb(NEW), 'needs_research', cfg))
       OR (OLD.state IN ('publication', 'publication_failed') AND NEW.state NOT IN ('publication', 'publication_failed')
           AND action IS DISTINCT FROM 'finding_published')
       OR (NEW.state = 'publication' AND OLD.state IS DISTINCT FROM 'publication'
           AND coalesce(action, '') NOT IN ('publish_finding', 'publication_retry')) THEN
        UPDATE ticket_board.ticket_research_briefs SET finding = finding - 'publication'
         WHERE ticket_id = NEW.id AND finding ? 'publication';
    END IF;
    IF OLD.state IS NOT DISTINCT FROM NEW.state THEN
        RETURN NULL;
    END IF;
    IF NEW.state = 'research' THEN
        UPDATE ticket_board.ticket_research_briefs
           SET round_started_at = clock_timestamp(), timebox_noticed_at = NULL, timebox_notification_id = NULL,
               finding = NULL
         WHERE ticket_id = NEW.id;
    ELSIF NEW.state = 'publication' AND action = 'publish_finding' THEN
        -- SYRD-849: the Director accepted this finding for publication; the Publisher's moves for it are the finding's.
        UPDATE ticket_board.ticket_research_briefs
           SET finding = finding || jsonb_build_object('publication', jsonb_build_object(
                   'by', coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''), 'director'),
                   'at', clock_timestamp()))
         WHERE ticket_id = NEW.id;
    ELSIF OLD.state = 'research' AND action IN ('submit_finding', 'submit_finding_without_commit') THEN
        UPDATE ticket_board.ticket_research_briefs
           SET finding = jsonb_build_object('action', action,
                   'commit_hash', CASE WHEN action = 'submit_finding' THEN NEW.commit_hash ELSE '' END,
                   'actor', coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''), OLD.assignee),
                   'at', clock_timestamp())
         WHERE ticket_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;


COMMIT;
