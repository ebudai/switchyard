-- SYRD-118: a published verdict the board never saw.
--
-- Publication request 17 was recorded published while the ref it named was on
-- neither GitHub nor this tenant's trusted commit cache. The implementer was
-- notified to submit, and the same board then refused the commit, because
-- `submit_to_audit` verifies commit_hash against that cache and publication
-- verified nothing at all. The record and the submittable commit could disagree
-- because `resolve_publication` accepted 'published' on the control role's word.
--
-- The word is no longer enough. The board resolves the requested ref through
-- the trusted publication path -- `refs/remotes/origin/<ref>` in the tenant's
-- commit cache, which only the privileged publisher writes, and only after it
-- has pushed and read the exact commit back from the public remote -- and hands
-- what it found in. This function refuses the verdict unless that is exactly
-- the commit the implementer asked for, and stores it on the row, so a
-- publication record says what was seen rather than what was claimed.
--
-- A refused verdict records nothing and leaves the request open: a request that
-- is neither published nor rejected is one the control role can still act on,
-- and a false 'published' is one nobody can undo. Rejections are untouched --
-- a refusal needs no push to be true.

BEGIN;

-- What the board saw. Empty on every outcome but a publication.
ALTER TABLE ticket_board.publication_requests
    ADD COLUMN IF NOT EXISTS verified_commit text NOT NULL DEFAULT '';

DO $constraint$
BEGIN
    ALTER TABLE ticket_board.publication_requests
        ADD CONSTRAINT publication_requests_verified_commit_check
        CHECK (verified_commit = '' OR verified_commit ~ '^[0-9a-f]{40}$');
EXCEPTION WHEN duplicate_object THEN
    NULL;
END $constraint$;

-- The signature changes, so the old one is removed rather than left beside it:
-- two overloads would let a caller reach the version that proves nothing.
DROP FUNCTION IF EXISTS ticket_board.resolve_publication(bigint, text, text);

-- Who may answer an ask, asked on its own so the board can settle that before
-- it runs git on the caller's behalf.
CREATE OR REPLACE FUNCTION ticket_board.require_publication_control()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        RAISE EXCEPTION 'publication requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.require_actor(ARRAY[]::text[], 'resolve_publication');
    caller := ticket_board.current_app_actor();
    -- The capability admits the caller; control authority is what decides. A
    -- tenant that hands this capability to a second role still gets one
    -- publisher.
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'only the control role may resolve a publication request, not %', caller
            USING ERRCODE = '42501';
    END IF;
    RETURN caller;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.resolve_publication(
    p_request bigint,
    p_outcome text,
    p_detail text,
    p_verified_commit text DEFAULT ''
)
RETURNS ticket_board.publication_requests
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
    request ticket_board.publication_requests%ROWTYPE;
    ticket ticket_board.tickets%ROWTYPE;
    outcome text := lower(btrim(coalesce(p_outcome, '')));
    note text := btrim(coalesce(p_detail, ''));
    proof text := lower(btrim(coalesce(p_verified_commit, '')));
    updated ticket_board.publication_requests%ROWTYPE;
BEGIN
    caller := ticket_board.require_publication_control();
    IF outcome NOT IN ('published', 'rejected') THEN
        RAISE EXCEPTION 'publication outcome must be published or rejected, not %', p_outcome;
    END IF;
    IF outcome = 'rejected' AND note = '' THEN
        RAISE EXCEPTION 'rejecting a publication request requires a reason';
    END IF;
    SELECT * INTO request FROM ticket_board.publication_requests
     WHERE id = p_request FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'publication request % not found', p_request;
    END IF;
    IF request.state = outcome THEN
        -- Already recorded. A helper that pushed and then lost its connection
        -- re-runs safely rather than reporting a failure it did not have.
        RETURN request;
    END IF;
    IF request.state <> 'requested' THEN
        RAISE EXCEPTION 'publication request % is already %', request.id, request.state;
    END IF;
    -- The one thing a control role may not decide on its own authority.
    IF outcome = 'published' AND proof <> request.commit_hash THEN
        RAISE EXCEPTION
            'publication request % cannot be recorded published: % has to be proven at %, %',
            request.id, request.ref, request.commit_hash,
            CASE WHEN proof = '' THEN 'and the board proved nothing'
                 ELSE format('and the board proved %s instead', proof) END
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = request.ticket_id FOR UPDATE;

    UPDATE ticket_board.publication_requests
       SET state = outcome,
           detail = note,
           decided_by = caller,
           verified_commit = CASE WHEN outcome = 'published' THEN proof ELSE '' END,
           decided_at = clock_timestamp()
     WHERE id = request.id
    RETURNING * INTO updated;

    UPDATE ticket_board.ticket_notification_state
       SET awaiting_role = '',
           awaiting_since_at = NULL,
           last_activity_at = clock_timestamp(),
           nudge_count = 0
     WHERE ticket_id = request.ticket_id;

    PERFORM ticket_board.enqueue_notification(
        request.ticket_id,
        'publication',
        request.requested_by,
        CASE WHEN outcome = 'published'
             THEN format('%s -- %s: %s is published at %s. Submit when you are ready.',
                         request.ticket_id, ticket.title, request.ref,
                         left(request.commit_hash, 12))
             ELSE format('%s -- %s: publication of %s was rejected: %s',
                         request.ticket_id, ticket.title, request.ref, note)
        END,
        jsonb_build_object(
            'kind', 'publication_' || outcome, 'id', request.ticket_id,
            'request_id', request.id, 'ref', request.ref,
            'commit', request.commit_hash, 'detail', note),
        format('publication:%s:%s', request.id, outcome));
    PERFORM ticket_board.add_comment(
        request.ticket_id,
        CASE WHEN outcome = 'published'
             THEN format('Published %s at %s.%s', request.ref, left(request.commit_hash, 12),
                         CASE WHEN note = '' THEN '' ELSE ' ' || note END)
             ELSE format('Publication of %s refused: %s', request.ref, note)
        END);
    PERFORM ticket_board.touch_ticket(request.ticket_id);
    RETURN updated;
END;
$$;

-- The writer's grant follows the new signature. As in pgu930 the writer role
-- cannot be spelled -- each project board runs under its own generated name --
-- so it is found by the privilege that already identifies it.
DO $grants$
DECLARE writer text;
BEGIN
    FOR writer IN
        SELECT rolname FROM pg_roles
        WHERE NOT rolsuper
          AND has_function_privilege(
                  rolname, 'ticket_board.force_move(text, text, text, boolean)', 'EXECUTE')
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.require_publication_control() TO %I', writer);
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.resolve_publication(bigint, text, text, text) TO %I',
            writer);
    END LOOP;
END $grants$;

COMMIT;
