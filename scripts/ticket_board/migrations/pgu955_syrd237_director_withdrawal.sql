-- SYRD-237: the Director could not withdraw a candidate from User Review.
--
-- The Director moved an Audit-approved candidate from DAT to User Review and
-- then found an implementation defect while preparing the live UAT, before the
-- User had accepted or rejected anything. `director_dat_kick_back` correctly
-- refuses outside DAT, and User Review offered only the User's own kick-back,
-- a relay that would put the Director's finding in the User's mouth, cancel,
-- defer, and an emergency override. So the Director narrated an override back
-- to DAT and took the ordinary kick-back from there -- twice, on SYRD-233.
--
-- This grants the missing move: the Director's OWN withdrawal from User
-- Review, returning the candidate the way every other kick-back returns it.
--
-- It is deliberately NOT a relay. `relays_decision_of` is absent, which is the
-- whole point: a relay records what the User decided, and this records what the
-- Director found. The three outcomes stay distinguishable in history by their
-- action names -- `user_kick_back`, `relay_user_kick_back`,
-- `director_withdraw` -- and an override remains what it always was, the thing
-- you reach for when none of these fits.
--
-- Nothing about the mechanics is restated here. The User's own kick-back from
-- the same stage is read as the model, so destination and sign-off clearing
-- agree with it by construction rather than by my copying them; the `return`
-- primitive already clears the candidate hash and notifies the destination
-- owner exactly once.
--
-- Idempotent, and narrow: it does nothing on a tenant that already has such an
-- action, nothing on a tenant with no User Review stage to withdraw from, and
-- nothing where the control role is ambiguous -- handing the authority to
-- withdraw a candidate to a guessed role is worse than leaving a tenant to
-- grant it deliberately.
BEGIN;

DO $withdraw$
DECLARE cfg jsonb; rev bigint; model jsonb; withdrawal jsonb; director text; controllers int;
BEGIN
    SELECT document, revision INTO cfg, rev FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;

    -- Already granted: nothing to do, and no revision nobody asked for. Keyed
    -- on the shape -- a non-relayed return from user_review by somebody other
    -- than the user -- rather than on the action name, so a tenant that spells
    -- it differently is left alone.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
                WHERE x->>'from' = 'user_review'
                  AND x->>'primitive' = 'return'
                  AND x->>'relays_decision_of' IS NULL
                  AND NOT (x->'actors' ? 'user')) THEN
        RETURN;
    END IF;

    -- Only where the shape this repairs actually exists. A tenant with no user
    -- review has nothing to withdraw from. The User's own kick-back is the
    -- model, so the withdrawal lands where a rejection lands and clears what a
    -- rejection clears.
    SELECT x INTO model FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = 'user_review'
       AND x->>'primitive' = 'return'
       AND x->'actors' ? 'user'
     ORDER BY x->>'action'
     LIMIT 1;
    IF model IS NULL THEN RETURN; END IF;

    -- The control role, named by what it can do rather than by what it is
    -- called (SYRD-49). Where more than one active role qualifies this declines
    -- to guess, for the same reason the relay migration does.
    SELECT count(*), min(r->>'name') INTO controllers, director
      FROM jsonb_array_elements(cfg->'roles') r
     WHERE (r->>'active')::boolean
       AND r->'capabilities' ?& ticket_board.control_capabilities();
    IF controllers <> 1 THEN RETURN; END IF;

    withdrawal := jsonb_build_object(
        'from', 'user_review',
        'to', model->>'to',
        'action', 'director_withdraw',
        'label', 'Withdraw the candidate (Director found a defect)',
        'actors', jsonb_build_array(director),
        'primitive', 'return',
        'owner_scoped', false,
        'require_commit', false,
        -- The reason is the whole record of why a candidate the Audit passed
        -- went back. Without it the history shows a withdrawal and no finding.
        'require_reason', true,
        'clear_signoffs', coalesce(model->'clear_signoffs', '[]'::jsonb),
        'allow_no_code', false);

    -- A tenant that already spells this action something else keeps its own.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
                WHERE x->>'action' = withdrawal->>'action') THEN
        RETURN;
    END IF;

    cfg := jsonb_set(cfg, ARRAY['transitions'], (cfg->'transitions') || jsonb_build_array(withdrawal));
    -- jsonb_set creates the leaf, never the object above it.
    cfg := jsonb_set(cfg, ARRAY['migrations'], coalesce(cfg->'migrations', '{}'::jsonb), true);
    cfg := jsonb_set(cfg, ARRAY['migrations', 'director_withdrawal'], 'true'::jsonb, true);
    PERFORM ticket_board.validate_declared_workflow(cfg);
    rev := rev + 1;
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('pgu955', cfg);
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    PERFORM pg_notify('ticket_board_state_transition',
        jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $withdraw$;

COMMIT;
