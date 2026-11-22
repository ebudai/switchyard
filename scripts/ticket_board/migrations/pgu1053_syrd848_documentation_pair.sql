-- SYRD-848 (pgu1053): the board-recorded implementer + documentation layer, for the Publisher.
--
-- A Librarian's documentation sign-off (SYRD-624) makes the ticket's candidate a documentation commit whose sole
-- parent is the candidate it reviewed, so the candidate is at least two commits on the public tip, and the Publisher
-- (SYRD-625), which publishes one commit, returned every one: a squash, a resubmission and a whole review round, for
-- documentation the Librarian added and Audit had already reviewed. Audit's documentation-only return (SYRD-719)
-- keeps the audited candidate, so a correction after it is a further documentation commit on the first: the
-- documentation layer can be several commits, every one of them a Librarian's, recorded by the board as it was made.
--
-- ticket_documentation_layers keeps that layer, one row per documentation commit, in order, each with the
-- candidate it was layered on. Its trigger runs after record_audit_return (tickets_z_audit_return) and follows the
-- record ticket_audit_returns keeps (SYRD-719): a documentation pass that leaves a new commit on the record's base
-- starts the layer again; one on the layer's own top extends it; a pass that keeps the commit (no change) leaves it;
-- and anything that ends the record's documentation layer -- a producer recorded again, a revision made any other
-- way -- or a record the layer does not match, clears it.
--
-- publication_documentation_pair(ticket) reads it for the Publisher, and only when it is exactly the ticket's
-- candidate: the latest documentation decision is the holder's own (a documentation commit, or no change after
-- Audit's documentation return), its commit is the ticket's commit, it sits on the producer's own commit, and the
-- recorded layer runs unbroken from that commit to the candidate. Anything else is NULL, and the Publisher treats the
-- candidate as it always has.
--
-- schema.sql carries the same objects; rbac.sql grants the read to the listener alone.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_documentation_layers (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 1),
    commit_hash text NOT NULL,
    parent_commit text NOT NULL,
    assignee text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ticket_id, position)
);
REVOKE ALL ON ticket_board.ticket_documentation_layers FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.record_documentation_layer()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    r ticket_board.ticket_audit_returns;
    top ticket_board.ticket_documentation_layers;
    revised boolean := NEW.commit_hash IS DISTINCT FROM OLD.commit_hash OR NEW.commit_exempt IS DISTINCT FROM OLD.commit_exempt;
BEGIN
    IF NOT revised AND NOT EXISTS (SELECT FROM ticket_board.ticket_documentation_layers WHERE ticket_id = NEW.id) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = NEW.id;
    SELECT * INTO top FROM ticket_board.ticket_documentation_layers WHERE ticket_id = NEW.id ORDER BY position DESC LIMIT 1;
    IF r.documentation_stage IS NOT NULL AND NOT NEW.commit_exempt AND r.documentation_exempt IS FALSE
       AND r.documentation_commit = coalesce(NEW.commit_hash, '') AND r.base_commit IS NOT NULL THEN
        IF NOT revised THEN
            -- No change: the layer stands as recorded, when it is still the one under the candidate.
            IF top.ticket_id IS NULL OR top.commit_hash = NEW.commit_hash THEN
                RETURN NULL;
            END IF;
        ELSIF coalesce(OLD.commit_hash, '') = r.base_commit AND NOT OLD.commit_exempt THEN
            DELETE FROM ticket_board.ticket_documentation_layers WHERE ticket_id = NEW.id;
            INSERT INTO ticket_board.ticket_documentation_layers (ticket_id, position, commit_hash, parent_commit, assignee)
            VALUES (NEW.id, 1, NEW.commit_hash, OLD.commit_hash, r.documentation_assignee);
            RETURN NULL;
        ELSIF top.commit_hash = coalesce(OLD.commit_hash, '') AND NOT OLD.commit_exempt THEN
            INSERT INTO ticket_board.ticket_documentation_layers (ticket_id, position, commit_hash, parent_commit, assignee)
            VALUES (NEW.id, top.position + 1, NEW.commit_hash, OLD.commit_hash, r.documentation_assignee);
            RETURN NULL;
        END IF;
    END IF;
    DELETE FROM ticket_board.ticket_documentation_layers WHERE ticket_id = NEW.id;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.record_documentation_layer() FROM PUBLIC;
DROP TRIGGER IF EXISTS tickets_z_documentation_layer ON ticket_board.tickets;
CREATE TRIGGER tickets_z_documentation_layer AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.record_documentation_layer();

CREATE OR REPLACE FUNCTION ticket_board.publication_documentation_pair(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets;
    r ticket_board.ticket_audit_returns;
    layer ticket_board.ticket_documentation_layers;
    under text;
    layers jsonb := '[]'::jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('publication_documentation_pair');
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = p_ticket;
    IF t.id IS NULL OR r.ticket_id IS NULL OR t.commit_exempt
       OR r.documentation_action NOT IN ('documentation_sign_off', 'documentation_no_change')
       OR coalesce(r.documentation_commit, '') !~ '^[0-9a-f]{40}$' OR coalesce(r.base_commit, '') !~ '^[0-9a-f]{40}$'
       OR r.documentation_commit <> coalesce(t.commit_hash, '')
       OR r.base_commit <> r.producer_commit OR r.producer_exempt OR r.base_commit = r.documentation_commit THEN
        RETURN NULL;
    END IF;
    under := r.base_commit;
    FOR layer IN SELECT * FROM ticket_board.ticket_documentation_layers WHERE ticket_id = p_ticket ORDER BY position LOOP
        IF layer.parent_commit <> under THEN
            RETURN NULL;
        END IF;
        layers := layers || jsonb_build_object('commit', layer.commit_hash, 'role', layer.assignee);
        under := layer.commit_hash;
    END LOOP;
    IF under <> r.documentation_commit THEN
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object(
        'implementer', r.base_commit, 'implementer_role', r.producer_assignee,
        'documentation', r.documentation_commit, 'documentation_role', r.documentation_assignee,
        'decision', r.documentation_action, 'layers', layers,
        'decided_at', ticket_board.utc_text(r.documentation_at));
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.publication_documentation_pair(text) FROM PUBLIC;

COMMIT;
