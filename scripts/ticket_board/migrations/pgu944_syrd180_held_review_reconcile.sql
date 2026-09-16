-- SYRD-180: a review approval taken under a manual hold was consumed into a
-- boolean and never paid. The declared executor computes the transition an
-- approval earns and then discarded it along with the stage, nothing replayed it
-- when the hold was cleared, and recover_stalled_ticket sent the Director to
-- route/reassign/defer -- none of which can finish a completed review.
--
-- The deferred transition is now recorded when the hold defers it, replayed
-- exactly once when the hold is released (as the role that earned it, through
-- the same actor seam a bounded recovery uses), and either paid or named by the
-- recovery command. Idempotent: CREATE TABLE IF NOT EXISTS and CREATE OR REPLACE
-- throughout, so a second application changes nothing.
BEGIN;

-- SYRD-180: a review approval given while a ticket is manually controlled.
--
-- The hold is meant to DEFER a reviewer's decision, not to consume it. The
-- declared executor already computes the transition the approval earned, and
-- then throws it away to keep the deliberate stage/owner hold. This table is
-- where it is kept instead, so releasing the hold can replay exactly that one
-- transition, taken by the role that earned it, rather than a destination
-- re-derived later from a parallel table that could disagree.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_deferred_review (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    action_name text NOT NULL,
    actor text NOT NULL,
    to_stage text NOT NULL,
    deferred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- What a released hold still owes, by name. NULL when nothing is owed, which is
-- the normal case: only an approval taken under a hold records anything here.
CREATE OR REPLACE FUNCTION ticket_board.pending_held_review(p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
-- Definer, like every other reader of an internal table: the deferral store is
-- written only by the functions above and carries no role grants of its own, so
-- a caller's answer must not depend on whether this board was built from the
-- schema or reached this state through the migration.
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT d.action_name
    FROM ticket_board.ticket_deferred_review d
    WHERE d.ticket_id = p_ticket_id;
$$;

-- Pay it. Exactly once, because the row is deleted with the move; and only when
-- the move is still the one the workflow allows from where the ticket now is.
CREATE OR REPLACE FUNCTION ticket_board.reconcile_released_hold(p_ticket_id text)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    deferred ticket_board.ticket_deferred_review;
    held boolean;
    current_state text;
BEGIN
    SELECT * INTO deferred FROM ticket_board.ticket_deferred_review d
    WHERE d.ticket_id = p_ticket_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT t.manually_controlled, t.state INTO held, current_state
    FROM ticket_board.tickets t WHERE t.id = p_ticket_id FOR UPDATE;
    IF held THEN
        RETURN NULL;
    END IF;
    -- The ticket may have been moved by hand while it was held -- a director
    -- edit, an override, a return. The deferred step is then no longer a step
    -- this ticket can take, and replaying it would either fail or move it
    -- somewhere nobody chose. Drop it rather than carry it.
    IF NOT ticket_board.workflow_transition_allowed_config(current_state, deferred.to_stage) THEN
        DELETE FROM ticket_board.ticket_deferred_review d WHERE d.ticket_id = p_ticket_id;
        RETURN NULL;
    END IF;
    -- A blocked ticket is not promoted by releasing a hold, and its approval is
    -- not spent either: it stays owed until the blocker clears, which is what
    -- the workflow does with every other forward promotion (SYRD-148).
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN NULL;
    END IF;
    -- Taken by the role that earned it, through the same seam a recovery uses,
    -- so the transition is authorized, gated, assigned and notified by the one
    -- implementation that does that -- not by a second copy of it here.
    PERFORM set_config('ticket_board.workflow_action', deferred.action_name, true);
    PERFORM set_config('ticket_board.workflow_actor', deferred.actor, true);
    UPDATE ticket_board.tickets
    SET state = deferred.to_stage
    WHERE tickets.id = p_ticket_id
      AND tickets.state = current_state;
    PERFORM set_config('ticket_board.workflow_action', '', true);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    DELETE FROM ticket_board.ticket_deferred_review d WHERE d.ticket_id = p_ticket_id;
    RETURN deferred.action_name;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.set_manually_controlled(
    id text,
    manually_controlled boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    UPDATE ticket_board.tickets
    SET manually_controlled = set_manually_controlled.manually_controlled
    WHERE tickets.id = set_manually_controlled.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    -- Releasing the hold pays what the hold deferred, and pays it once: the
    -- record is retired with the move, so a second release finds nothing owed
    -- (SYRD-180).
    IF NOT set_manually_controlled.manually_controlled THEN
        PERFORM ticket_board.reconcile_released_hold(id);
    END IF;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.inspector_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    was_approved boolean;
    held boolean;
    actor text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('inspector_sign_off', id);
    SELECT tickets.inspector_signoff,tickets.manually_controlled INTO was_approved,held
    FROM ticket_board.tickets WHERE tickets.id=inspector_sign_off.id AND tickets.state='inspection' FOR UPDATE;
    IF held AND was_approved THEN RETURN; END IF;
    UPDATE ticket_board.tickets
    SET inspector_signoff = true,
        state = CASE WHEN manually_controlled THEN 'inspection' ELSE 'audit' END
    WHERE tickets.id = inspector_sign_off.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
    END IF;
    -- Held: the move this approval earned is deferred, so it is recorded and
    -- paid when the hold is released. The legacy trigger re-evaluates audit and
    -- user review on the next unheld write but never inspection, so without this
    -- the one stage with a declared inspector is the one that strands (SYRD-180).
    IF held THEN
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(inspector_sign_off.id,'inspector_sign_off',actor,'audit')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT coalesce(was_approved,false) THEN
        PERFORM ticket_board.notify_held_review_completion(id,'inspection');
    END IF;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true);
    -- Whose transition this is. Normally the caller's own; on a recovery the
    -- executor names the OWNER here, because the step being taken has to be one
    -- the owner could have taken and this trigger asks exactly that question
    -- (SYRD-133). Set transaction-locally by the executor immediately before the
    -- update, in the same way `workflow_action` already is, and cleared after.
    actor text:=coalesce(
        nullif(current_setting('ticket_board.workflow_actor',true),''),
        ticket_board.current_app_actor()); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
    queued_for text; reserved_by text;
BEGIN
    IF actor IS NULL THEN RAISE EXCEPTION 'configured ticket writes require a registered actor' USING ERRCODE='42501'; END IF;
    -- A narrated override moves a ticket the declared workflow will not: that
    -- is what it is for, and the transition table cannot describe it without
    -- describing its own bypass. Only ticket_board.force_move sets this, it is
    -- transaction-local, and reaching it already required the control role's
    -- capabilities. The legacy trigger has always had this escape; the
    -- declarative one did not, so the operation was refused here even after the
    -- capability layer let it through (SYRD-78).
    --
    -- It does not license a sign-off. An override that raised one would be
    -- manufacturing the review it exists to route around, so a sign-off flag
    -- that moves under it is refused rather than written.
    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a forced move cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;

    -- Merge closes its own source, and only its own source. The window is one
    -- ticket, named by the operation itself, and one statement: it is opened
    -- immediately before the close and shut immediately after, so nothing else
    -- in the transaction can travel through it. The destination has to be a
    -- stage the workflow declares terminal, and no sign-off may move -- a merge
    -- that raised one would be manufacturing the review the target still owes
    -- (SYRD-79).
    IF nullif(current_setting('ticket_board.merge_source', true), '') = previous.id THEN
        IF NOT coalesce((
            SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
            WHERE x->>'name' = proposed.state
        ), false) THEN
            RAISE EXCEPTION 'merge may only close its source into a terminal stage, not %', proposed.state
                USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a merge cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- A Director edit changes fields the transition table has nothing to say
    -- about, at whatever stage the ticket is in. The window names one ticket
    -- and is open for one statement. It carries no sign-off: ticket_board
    -- .director_edit, the only operation that opens this window, refuses to
    -- raise one before it writes anything (SYRD-83).
    IF nullif(current_setting('ticket_board.director_edit_target', true), '') = previous.id THEN
        RETURN proposed;
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director' THEN
            RAISE EXCEPTION 'only director may reassign without transition' USING ERRCODE='42501'; END IF;
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND (flag.value->>'kind'='signoff' OR actor<>'director') THEN
                RAISE EXCEPTION 'flag change requires authorized workflow action'; END IF;
        END LOOP;
        -- SYRD-77: an owner change that is not a transition never reached the
        -- serial-focus redirect below, so a same-stage reassignment could leave
        -- one implementer holding two implementation tickets -- exactly the
        -- reservation the queue exists to protect. Resolving it here rather
        -- than in the caller means no write path can hand an implementer
        -- reserved work by declining to ask.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_stage_kind(proposed.state)='implementation'
               AND ticket_board.ticket_is_implementer_assignee(proposed.assignee)
               AND NOT proposed.manually_controlled
               AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL THEN
                IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN
                    RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
                queued_for:=proposed.assignee;
                reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
                proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
                PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
                proposed.queued_for_assignee:=queued_for;
                proposed.queued_behind_ticket:=coalesce(reserved_by,'');
                PERFORM set_config('ticket_board.serial_focus_queued',
                    jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                        'stage',proposed.state,'assignee',proposed.assignee)::text,true);
            ELSE
                -- A deliberate new owner ends the hold that named the old one,
                -- so the marker never outlives the reservation that set it.
                proposed.queued_for_assignee:='';
                proposed.queued_behind_ticket:='';
            END IF;
        END IF;
        RETURN proposed;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
            RAISE EXCEPTION 'flags cannot be patched alongside a transition'; END IF;
    END LOOP;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x
      WHERE x->>'from'=previous.state AND x->>'to'=proposed.state AND x->>'action'=action_name;
    IF tr IS NULL OR NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
        RAISE EXCEPTION 'unauthorized configured transition: % / %',actor,action_name USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=previous.state;
    IF (source_stage->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
    IF tr->>'primitive' NOT IN ('return','reopen') AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion'; END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    IF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
    ELSIF source_stage->>'signoff' IS NOT NULL AND tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    IF tr->>'primitive' IN ('return','reopen') THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
    IF (tr->>'allow_no_code')::boolean THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',true); END IF;
    target:=proposed.state;
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        EXIT WHEN (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners) THEN proposed.assignee:=owners[1]; END IF;
    IF previous.manually_controlled AND tr->>'primitive'='approve' THEN
        -- Record the decision but retain the deliberate stage/owner hold. The
        -- executor creates the durable handoff after activity triggers finish.
        PERFORM set_config('ticket_board.held_review_target',coalesce(nullif(ticket_board.transition_target_role(target,proposed.assignee),actor),'director'),true);
        -- And keep the transition the approval earned, instead of discarding it
        -- with the stage. A hold defers a decision; releasing it replays this
        -- exact step, as this actor, once (SYRD-180).
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(previous.id,action_name,actor,tr->>'to')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
        target:=previous.state; proposed.assignee:=previous.assignee;
    ELSE
        -- A move that actually happens leaves nothing owed, including the move
        -- that pays a deferral: nothing may be replayed twice.
        DELETE FROM ticket_board.ticket_deferred_review WHERE ticket_id=previous.id;
    END IF;
    doc:=doc||jsonb_build_object('state',target,'assignee',proposed.assignee);
    proposed:=jsonb_populate_record(proposed,doc);
    PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
    IF dest->>'kind'='implementation' AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
        IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
        queued_for:=proposed.assignee;
        reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
        proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        -- Serial focus still holds the ticket, but the outcome is now legible.
        -- The queue destination is frequently the stage the ticket came from,
        -- so without these the caller sees an unchanged ticket and a success.
        proposed.queued_for_assignee:=queued_for;
        proposed.queued_behind_ticket:=coalesce(reserved_by,'');
        PERFORM set_config('ticket_board.serial_focus_queued',
            jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                'stage',proposed.state,'assignee',proposed.assignee)::text,true);
    ELSE
        -- Any move that is not held clears the marker, so it never outlives the
        -- reservation that caused it.
        proposed.queued_for_assignee:='';
        proposed.queued_behind_ticket:='';
    END IF;
    -- Deferring is putting work down, so it has to stop looking like work
    -- somebody has. An owner left on a parked ticket keeps its implementer's
    -- serial reservation and keeps the board highlighting it as current, which
    -- is how a deferral would quietly go on nudging the person who deferred it.
    -- Everything the ticket is -- content, hierarchy, blockers, gates,
    -- sign-offs, comments -- is untouched; only who holds it changes (SYRD-92).
    IF ticket_board.declared_parking_stage(proposed.state) THEN
        -- queued_for is set only by the serial-focus redirect just above, whose
        -- holding destination is frequently this same stage. That ticket is
        -- waiting on a busy implementer rather than being put down, and its
        -- queue bookkeeping is what makes the outcome legible, so it is left
        -- exactly as it was.
        IF queued_for IS NULL THEN
            proposed.assignee:='unassigned';
            proposed.parked:=true;
            proposed.queued_for_assignee:='';
            proposed.queued_behind_ticket:='';
        END IF;
    ELSIF ticket_board.declared_parking_stage(previous.state) THEN
        -- Reviving it: the hold ends with the stage that carried it.
        proposed.parked:=false;
    END IF;
    RETURN proposed;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.recover_stalled_ticket(
    p_ticket text,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
    reason text := btrim(coalesce(p_reason, ''));
    cfg jsonb := ticket_board.declared_workflow();
    t ticket_board.tickets%ROWTYPE;
    candidates integer;
    chosen text;
    pending text;
BEGIN
    IF cfg IS NULL THEN
        RAISE EXCEPTION 'recovery requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.require_actor(ARRAY[]::text[], 'recover_stalled_ticket');
    caller := ticket_board.current_app_actor();
    -- Control authority decides, derived from capabilities. A tenant that hands
    -- the capability to a second role still gets one recoverer, and no rule
    -- here spells the name `director` (SYRD-49).
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'only the control role may recover a stalled ticket, not %', caller
            USING ERRCODE = '42501';
    END IF;
    IF reason = '' THEN
        RAISE EXCEPTION 'recovering a stalled ticket requires a reason';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % not found', p_ticket;
    END IF;
    -- A completed review that a hold deferred is not a stalled owner. Recovery
    -- takes the one declared step the owner could have taken (SYRD-133); here
    -- that is the step they DID take, so pay it rather than asking the reviewer
    -- to sign off a second time -- and when it cannot be paid yet, refuse naming
    -- what is actually in the way, without consuming the approval. Sending the
    -- Director to route/reassign/defer was the advice SYRD-146 followed into a
    -- dead end (SYRD-180).
    pending := ticket_board.pending_held_review(t.id);
    IF pending IS NOT NULL THEN
        IF t.manually_controlled THEN
            RAISE EXCEPTION
                '%/% has already recorded %; it is waiting on its hold, not on its owner. '
                'Release the hold to finish it: set_manually_controlled(''%'', false)',
                t.state, t.assignee, pending, t.id
                USING ERRCODE = '42501';
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(t.id) THEN
            RAISE EXCEPTION
                '%/% has already recorded %; it is waiting on an unresolved blocker, not on its '
                'owner. Resolve the blocker and recovery finishes it', t.state, t.assignee, pending
                USING ERRCODE = '42501';
        END IF;
        IF ticket_board.reconcile_released_hold(t.id) IS NOT NULL THEN
            PERFORM ticket_board.append_ticket_comment(t.id, caller, reason);
            RETURN;
        END IF;
    END IF;
    SELECT count(*) INTO candidates
      FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = t.state
       AND coalesce((x->>'allow_no_code')::boolean, false)
       AND x->'actors' ? t.assignee;
    IF candidates = 0 THEN
        RAISE EXCEPTION
            'no no-code transition out of %/% is available to its owner, so there is nothing to '
            'recover: route, reassign or defer it instead', t.state, t.assignee
            USING ERRCODE = '42501';
    END IF;
    IF candidates > 1 THEN
        RAISE EXCEPTION
            '%/% offers % no-code transitions to its owner; recovery will not choose between them',
            t.state, t.assignee, candidates
            USING ERRCODE = '42501';
    END IF;
    SELECT x->>'action' INTO chosen
      FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = t.state
       AND coalesce((x->>'allow_no_code')::boolean, false)
       AND x->'actors' ? t.assignee;
    -- The owner's transition, authorized and narrated by the control role. Every
    -- gate the owner would have met is met here, because this is the same
    -- executor taking the same declared step.
    PERFORM ticket_board.perform_workflow_action_as(
        t.assignee, caller, t.id, chosen,
        jsonb_build_object('reason', reason));
END;
$$;

COMMIT;
