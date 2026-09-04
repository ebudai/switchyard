-- PGU-913: let an implementer submit finished work that produced no commit,
-- instead of borrowing an unrelated hash. Adds one function and one grant;
-- changes no existing behaviour and no data.

-- Submit finished work that produced no commit.
--
-- submit_to_audit already accepts an empty hash when commit_exempt is set; the
-- gap PGU-913 closes is that an implementer could not set it. The alternative
-- on offer was request_commit_exempt, which throws the ticket back to analysis
-- and unassigns it -- correct for "I cannot do this", wrong for "this is done
-- and there is no diff". With no usable path, three tickets reached audit
-- carrying a BORROWED hash pointing at unrelated work (PGU-889, PGU-903,
-- PGU-911), which is worse than an empty one: it resolves, looks
-- authoritative, and sends a reviewer into someone else's change.
--
-- This does NOT hand implementers the commit_exempt field. The exemption can
-- only be set as part of submitting, it requires a reason, and the reason is
-- recorded as an attributed comment before the transition, so audit sees an
-- explicit claim and who made it rather than an absence. Audit can kick it
-- back like any other submission.
CREATE OR REPLACE FUNCTION ticket_board.submit_to_audit_without_commit(
    id text,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'submit_to_audit_without_commit requires a non-empty reason';
    END IF;
    -- Authorized as the ordinary submit, because that is what this is: the same
    -- transition, by the same roles, minus a diff. require_workflow_transition_actor
    -- is a pure check, so submit_to_audit re-running it below is harmless.
    actor := ticket_board.require_workflow_transition_actor('submit_to_audit', id);
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Submitted to audit with no commit: ' || normalized_reason
    );
    UPDATE ticket_board.tickets
    SET commit_exempt = true
    WHERE tickets.id = submit_to_audit_without_commit.id;
    -- Everything else -- state machine, kicked-back-commit rule, signoff reset --
    -- stays in submit_to_audit. If it refuses, this whole call rolls back and no
    -- exemption is left behind on a ticket that never moved.
    PERFORM ticket_board.submit_to_audit(id, '');
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit_without_commit(text, text) TO ticket_board_service;

-- The action needs its transition row to exist, or the new verb fails with
-- "submit_to_audit_without_commit is not configured from in_progress". The
-- schema.sql seed uses ON CONFLICT (from_stage, to_stage, action_name) DO
-- UPDATE, so repeating it here is idempotent whether or not the seed also runs
-- on this database. owner_scoped = true: waiving the commit requirement is a
-- claim about your own work.
INSERT INTO ticket_board.workflow_transitions
    (from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override)
VALUES
    ('in_progress', 'audit', 'submit_to_audit_without_commit',
     ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false)
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;
