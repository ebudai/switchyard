-- SYRD-225: a transition is stamped as activity in the update that makes it.
--
-- perform_workflow_action_as moved a ticket's state and assignee without
-- touching updated_at, updated_text or row_updated_at. Every trigger that fires
-- on that update -- the notification state and the queued transition notice --
-- reads the row's updated_at as the moment the change happened, so they all
-- recorded whatever last set it instead.
--
-- Live on SYRD-221: a Director DAT kickback handed the ticket back to ops, and
-- the transition notice carried updated_at from ops's own earlier submission,
-- as did entered_current_state_at and last_activity_at. A fresh handoff looked
-- to the board like old news.
--
-- Stamped in the same UPDATE rather than by a later touch_ticket(), because by
-- the time a separate touch runs the triggers have already built the notice
-- from the stale value.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action_as(
    p_actor text, p_narrator text, id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; tr jsonb; actor text:=p_actor; candidates int; handoff text; source_stage jsonb; acted_at timestamptz;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR actor IS NULL THEN RAISE EXCEPTION 'workflow action requires registered service actor' USING ERRCODE='42501'; END IF;
    SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=perform_workflow_action_as.id FOR UPDATE;
    SELECT count(*) INTO candidates FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF candidates<>1 THEN RAISE EXCEPTION 'unknown or ambiguous workflow action; specify target'; END IF;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND t.assignee<>actor) THEN
        RAISE EXCEPTION 'actor cannot perform workflow action' USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state;
    IF t.manually_controlled AND tr->>'primitive'='approve' AND ticket_board.workflow_flag(to_jsonb(t),source_stage->>'signoff',cfg) THEN
        -- Repeated decisions neither mint a new wait generation nor reopen a resolved one.
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
        RETURN;
    END IF;
    IF (tr->>'require_reason')::boolean AND btrim(coalesce(payload->>'text',payload->>'reason',''))='' THEN RAISE EXCEPTION 'reason required'; END IF;
    IF payload ? 'commit_hash' AND (payload->>'commit_hash') !~ '^[0-9a-fA-F]{7,40}$' THEN RAISE EXCEPTION 'invalid commit hash'; END IF;
    -- SYRD-217: an approval accepted on somebody else's word is held to more
    -- than that role's own sign-off would be. The stage's gate must actually
    -- be open, the acceptance must name the exact candidate already recorded
    -- -- naming a different one approves something nobody tested, and
    -- silently replacing it is worse -- and every other review this ticket is
    -- subject to must already have been given.
    --
    -- Stricter than the role's own sign-off, deliberately. The User signing
    -- off an unaudited ticket is their own mistake to make, in front of the
    -- work; the same thing relayed is a mistake nobody in the conversation is
    -- placed to catch.
    IF tr->>'relays_decision_of' IS NOT NULL AND tr->>'primitive'='approve' THEN
        IF source_stage->>'gate' IS NOT NULL
           AND NOT ticket_board.workflow_flag(to_jsonb(t),source_stage->>'gate',cfg) THEN
            RAISE EXCEPTION 'relayed approval requires this stage to be gated on: %', source_stage->>'gate'; END IF;
        IF t.commit_exempt AND btrim(coalesce(t.commit_hash,''))='' THEN
            -- Nothing was built, so there is nothing to name; naming one
            -- anyway would be accepting an artefact this ticket never had.
            IF payload ? 'commit_hash' THEN
                RAISE EXCEPTION 'relayed approval cannot name a commit on a commit-exempt ticket'; END IF;
        ELSIF btrim(coalesce(t.commit_hash,''))='' OR payload->>'commit_hash' IS DISTINCT FROM t.commit_hash THEN
            RAISE EXCEPTION 'relayed approval must name the recorded candidate commit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
                    WHERE x->>'name'<>t.state
                      AND x->>'signoff' IS NOT NULL AND x->>'gate' IS NOT NULL
                      AND ticket_board.workflow_flag(to_jsonb(t),x->>'gate',cfg)
                      AND NOT ticket_board.workflow_flag(to_jsonb(t),x->>'signoff',cfg)) THEN
            RAISE EXCEPTION 'relayed approval requires the earlier reviews this ticket is subject to'; END IF;
    END IF;
    IF btrim(coalesce(payload->>'text',payload->>'reason',''))<>'' THEN
        -- SYRD-214: a relayed decision says whose it was, in the record itself.
        --
        -- What the preamble adds is the one thing a reader cannot otherwise
        -- tell: that the decision came from somebody who does not operate the
        -- board, and that the role exercising the action is not claiming to
        -- have made it or to have done the review behind it. It names the
        -- actor rather than the narrator because the actor is whose authority
        -- the transition's actor list was checked against. (The two differ
        -- only on the SYRD-133 recovery path, which cannot reach a relay: its
        -- transitions are allow_no_code and so leave implementation, while a
        -- relay is a return and so enters it.) Composed here rather than left
        -- to whoever types the reason: provenance that depends on wording is
        -- provenance that goes missing the first time somebody is in a hurry.
        PERFORM ticket_board.append_ticket_comment(
            t.id,
            coalesce(p_narrator,actor),
            CASE
            WHEN tr->>'relays_decision_of' IS NULL THEN
                coalesce(payload->>'text',payload->>'reason')
            -- One E-string per branch: only the first of a run of adjacent
            -- literals takes the E prefix, so a continuation carrying \n or an
            -- escaped quote would be neither escaped nor balanced.
            WHEN tr->>'primitive'='approve' THEN
                -- A relayed approval IS the sign-off, so the record says so
                -- rather than disclaiming it. What it still will not say is
                -- that the relayer reviewed anything (SYRD-217).
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as %2$s sign-off, decided by %2$s and entered by %1$s, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            ELSE
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as the decision of %2$s, it is not %2$s sign-off, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            END);
    END IF;
    PERFORM set_config('ticket_board.held_review_target','',true);
    PERFORM set_config('ticket_board.workflow_action',action,true);
    PERFORM set_config('ticket_board.workflow_actor',actor,true);
    -- SYRD-225: a transition is activity, and it is stamped in the SAME update
    -- that moves the ticket. The triggers that fire on this update -- the
    -- notification state and the queued transition notice -- read the row's
    -- `updated_at` as the time this happened, and they read it now, not after
    -- some later touch. Left unstamped, a DAT kickback handed the implementer a
    -- notice timed to their own earlier submission, and the notification state
    -- recorded the ticket entering Implementation twelve minutes before it did.
    acted_at:=clock_timestamp();
    UPDATE ticket_board.tickets SET state=tr->>'to',
        assignee=coalesce(nullif(payload->>'assignee',''),assignee),
        commit_hash=coalesce(payload->>'commit_hash',commit_hash),
        updated_at=acted_at,
        updated_text=ticket_board.utc_text(acted_at),
        row_updated_at=now()
        WHERE tickets.id=t.id;
    PERFORM set_config('ticket_board.workflow_action','',true);
    PERFORM set_config('ticket_board.workflow_actor','',true);
    handoff:=nullif(current_setting('ticket_board.held_review_target',true),'');
    PERFORM set_config('ticket_board.held_review_target','',true);
    IF handoff IS NOT NULL THEN
        UPDATE ticket_board.ticket_notification_state SET awaiting_role=handoff, awaiting_since_at=clock_timestamp(),
            last_activity_at=clock_timestamp(), nudge_count=0 WHERE ticket_id=t.id;
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
    END IF;
END;
$$;

COMMIT;
