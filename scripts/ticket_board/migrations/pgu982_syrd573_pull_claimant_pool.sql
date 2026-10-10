-- SYRD-573: who may claim ready work is read from the role declarations, not
-- from the claim transition's actor list.
--
-- MEFP enabled pull with the claim transition naming the eight workers then
-- running. A runtime rotation later stopped two of them and started two other
-- declared workers; the list was not touched, so the replacements were refused
-- ("role ... cannot claim ready work") and the stopped ones were reported as
-- idle claimants. The claimant pool is now every active, ephemeral implementer
-- that owns the implementation stage (pull_claimant_pool), asked by every claim
-- path through transition_allows_actor; the listener claims for, and reports as
-- idle, only the pool members whose registered provider process is running.
--
-- Copies of schema.sql's functions. Nothing moves and nothing is sent: a board
-- without a pull policy has an empty pool, and every check is unchanged for it.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.pull_claimant_pool(cfg jsonb)
RETURNS SETOF text LANGUAGE sql IMMUTABLE AS $$
    SELECT r->>'name' FROM jsonb_array_elements(cfg->'roles') r
     WHERE nullif(cfg->'scheduling','null'::jsonb) IS NOT NULL
       AND r->>'kind'='implementer' AND r->'active'='true'::jsonb AND r->'ephemeral'='true'::jsonb
       AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
                   WHERE s->>'kind'='implementation' AND s->'owners' ? (r->>'name'))
     ORDER BY r->>'name';
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_allows_actor(cfg jsonb, tr jsonb, actor text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT tr->'actors' ? actor
        OR (EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(cfg) x
                    WHERE x->>'action'=tr->>'action' AND x->>'from'=tr->>'from' AND x->>'to'=tr->>'to')
            AND actor IN (SELECT ticket_board.pull_claimant_pool(cfg)));
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
        --
        -- SYRD-568: only a pull policy still diverts, into its ready stage
        -- pinned to the busy author. A Director-dispatched board leaves the
        -- ticket where it was put, assigned, waiting in that implementer's
        -- queue; only the active ticket holds the slot.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_scheduling() IS NOT NULL
               AND ticket_board.declared_stage_kind(proposed.state)='implementation'
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
    IF tr IS NULL OR NOT ticket_board.transition_allows_actor(cfg,tr,actor) OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
        RAISE EXCEPTION 'unauthorized configured transition: % / %',actor,action_name USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=previous.state;
    IF (source_stage->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
    -- A blocker stops work going FORWARD. Putting work down is not going
    -- forward: a parking stage is not terminal, owns nobody and notifies
    -- nobody, so nothing is promoted by landing there and the blocker is
    -- exactly the reason to park. Refusing it forced the director to clear the
    -- blocker, defer, and restore it -- three writes, a window where the ticket
    -- looked unblocked, and a blocked ticket holding an implementer's serial
    -- slot in the meantime (SYRD-192).
    --
    -- Keyed on the DESTINATION's shape rather than on an action name, because
    -- the name is the tenant's: `declared_parking_stage` is the same predicate
    -- `workflow_config.parking_stage_names` uses, so a tenant that calls it
    -- something other than `backlog` gets this for free.
    IF tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-568: placing blocked work in its implementer's queue promotes
       -- nothing either -- it holds no slot and is handed to nobody until its
       -- blockers resolve and it is the active ticket. Submitting it still is.
       -- A pull policy queues in its ready stage instead, as before.
       AND NOT (ticket_board.declared_stage_kind(tr->>'to') = 'implementation'
                AND ticket_board.declared_scheduling() IS NULL)
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    -- SYRD-568: a ticket waiting in its implementer's queue is not that
    -- implementer's work yet, so it cannot be submitted for review. Putting it
    -- down, routing it back or cancelling it are not submissions.
    IF source_stage->>'kind'='implementation'
       AND (ticket_board.declared_stage_kind(tr->>'to')='review'
            OR coalesce((tr->>'require_commit')::boolean,false) OR coalesce((tr->>'allow_no_code')::boolean,false))
       AND ticket_board.ticket_serial_waiting(previous.id) THEN
        RAISE EXCEPTION '% is waiting in %''s queue behind %; it can be submitted once it is the active ticket',
            previous.id, previous.assignee, coalesce(ticket_board.ticket_current_reserved_ticket(previous.assignee),'its blockers')
            USING ERRCODE='42501'; END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    -- A declared return out of review hands the work back: it needs no
    -- verdict, and the sign-off stays exactly as it was (SYRD-536). Its own
    -- branch, so SYRD-263's parking exemption below is unchanged.
    IF tr->>'primitive' NOT IN ('approve','return','reopen')
       AND ticket_board.declared_review_return(cfg, previous.state, tr->>'to') THEN
        NULL;
    ELSIF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
    -- A sign-off gates leaving a review FORWARD. Parking is not a verdict: the
    -- stage owns nobody, notifies nobody and promotes nothing, and the review's
    -- record rides along untouched -- no sign-off is granted, none is cleared,
    -- the commit stays. Requiring the missing sign-off made an unaccepted User
    -- Review impossible to put down without deciding it (SYRD-263). Keyed on
    -- the destination's shape, like the blocker exemption above (SYRD-192).
    ELSIF source_stage->>'signoff' IS NOT NULL AND tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    IF tr->>'primitive' IN ('return','reopen') THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
    IF (tr->>'allow_no_code')::boolean THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',true); END IF;
    -- A sign-off is a review of one commit (SYRD-271). MEFP-4 was approved for
    -- one commit, routed back by a plain `move` that clears nothing, and
    -- resubmitted with another -- and the audit stage, already signed, was
    -- skipped: the old verdict stood in for a review nobody had done. So when
    -- work leaves implementation carrying a different commit, every review
    -- sign-off is cleared here, before the gates are walked, and it enters
    -- review for exactly the commit it carries.
    -- The same commit resubmitted keeps its review: that exact work was seen.
    -- A no-code submission has no commit to show it is the same work, so it
    -- is always reviewed as new.
    IF source_stage->>'kind' = 'implementation'
       AND ((tr->>'allow_no_code')::boolean
            OR (btrim(coalesce(doc->>'commit_hash','')) <> ''
                AND (doc->>'commit_hash') IS DISTINCT FROM previous.commit_hash)) THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind' = 'signoff' THEN
                doc:=ticket_board.set_workflow_flag(doc,flag.key,false);
            END IF;
        END LOOP;
    END IF;
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
    -- SYRD-568: on a Director-dispatched board a move into an implementation
    -- stage lands there, assigned, even when that implementer already has
    -- active work, and waits in their queue until it is the active ticket. A
    -- pull policy keeps its ready stage as the queue (SYRD-539): there the
    -- move is still diverted, pinned to the busy author.
    IF ticket_board.declared_scheduling() IS NOT NULL
       AND dest->>'kind'='implementation' AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
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

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action_as(
    p_actor text, p_narrator text, id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; tr jsonb; actor text:=p_actor; candidates int; handoff text; source_stage jsonb;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR actor IS NULL THEN RAISE EXCEPTION 'workflow action requires registered service actor' USING ERRCODE='42501'; END IF;
    SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=perform_workflow_action_as.id FOR UPDATE;
    SELECT count(*) INTO candidates FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF candidates<>1 THEN RAISE EXCEPTION 'unknown or ambiguous workflow action; specify target'; END IF;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF NOT ticket_board.transition_allows_actor(cfg,tr,actor) OR ((tr->>'owner_scoped')::boolean AND t.assignee<>actor) THEN
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
    UPDATE ticket_board.tickets SET state=tr->>'to',
        assignee=coalesce(nullif(payload->>'assignee',''),assignee),
        commit_hash=coalesce(payload->>'commit_hash',commit_hash)
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

CREATE OR REPLACE FUNCTION ticket_board.lock_pull_assignment()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE role_name text; actor text:=nullif(current_setting('ticket_board.workflow_actor',true),'');
BEGIN
    IF ticket_board.declared_scheduling() IS NULL OR ticket_board.declared_stage_kind(NEW.state) IS DISTINCT FROM 'implementation' THEN
        RETURN NEW; END IF;
    -- A claim is the claimant's: the declared claim transition, taken through
    -- the canonical action with no assignee, would otherwise land on the
    -- implementation stage's default owner, whoever claimed it.
    IF TG_OP='UPDATE' AND actor IS NOT NULL AND OLD.state=ticket_board.declared_scheduling()->>'ready_stage'
       AND EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(ticket_board.declared_workflow()) x
                   WHERE x->>'action'=current_setting('ticket_board.workflow_action',true)
                     AND ticket_board.transition_allows_actor(ticket_board.declared_workflow(), x, actor)) THEN
        NEW.assignee := actor;
    END IF;
    IF TG_OP='UPDATE' AND OLD.state IS NOT DISTINCT FROM NEW.state AND OLD.assignee IS NOT DISTINCT FROM NEW.assignee THEN
        RETURN NEW; END IF;
    -- The proposed assignee, and the author a return would be sent to.
    FOR role_name IN
        SELECT DISTINCT r FROM unnest(ARRAY[NEW.assignee,
            (SELECT ns.last_implementer_assignee FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id=NEW.id)]) r
        WHERE r IS NOT NULL AND r<>'' ORDER BY r
    LOOP
        PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(role_name));
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.claim_ready_ticket(p_role text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); policy jsonb:=ticket_board.declared_scheduling(); caller text:=ticket_board.current_actor_role();
    tr jsonb; held text; candidate record; via text;
BEGIN
    IF caller='ticket_board_service' THEN
        -- A worker claims only for itself: its role is the process-bound caller.
        IF p_role IS DISTINCT FROM ticket_board.current_app_actor() THEN
            RAISE EXCEPTION 'a worker can claim only for itself' USING ERRCODE='42501'; END IF;
        via:='self';
    ELSIF caller='ticket_board_listener' THEN
        via:='listener';
    ELSE
        RAISE EXCEPTION 'claim requires the board service or listener' USING ERRCODE='42501';
    END IF;
    IF policy IS NULL THEN RAISE EXCEPTION 'this workflow does not use pull scheduling'; END IF;
    SELECT x INTO tr FROM ticket_board.scheduling_claim_transitions(cfg) x WHERE ticket_board.transition_allows_actor(cfg, x, p_role);
    IF tr IS NULL OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=p_role AND (r.definition->>'active')::boolean) THEN
        RAISE EXCEPTION 'role % cannot claim ready work', p_role USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(p_role));
    held:=ticket_board.ticket_current_reserved_ticket(p_role);
    IF held IS NOT NULL THEN
        RETURN jsonb_build_object('claimed', false, 'ticket', held, 'reason', 'holding');
    END IF;
    FOR candidate IN
        SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
        WHERE ticket_board.ticket_ready_for_pull(t.id, p_role)
        ORDER BY (coalesce(btrim(t.queued_for_assignee),'')=p_role) DESC,
                 coalesce((t.workflow_flags->>'priority')::int, 0) DESC,
                 ns.entered_current_state_at, t.ticket_number
        FOR UPDATE OF t SKIP LOCKED
    LOOP
        BEGIN
            PERFORM set_config('ticket_board.workflow_action', tr->>'action', true);
            PERFORM set_config('ticket_board.workflow_actor', p_role, true);
            UPDATE ticket_board.tickets SET state=tr->>'to', assignee=p_role, queued_for_assignee='', queued_behind_ticket=''
             WHERE id=candidate.id;
            PERFORM set_config('ticket_board.workflow_action', '', true);
            PERFORM set_config('ticket_board.workflow_actor', '', true);
            IF (SELECT state FROM ticket_board.tickets WHERE id=candidate.id) IS DISTINCT FROM tr->>'to' THEN
                RAISE EXCEPTION 'claim diverted';
            END IF;
            INSERT INTO ticket_board.pull_claims(ticket_id, role, via) VALUES (candidate.id, p_role, via);
            RETURN jsonb_build_object('claimed', true, 'ticket', candidate.id, 'via', via);
        EXCEPTION WHEN OTHERS THEN
            -- A gate or rule refused this one; it stays where it was, and the next is tried.
            PERFORM set_config('ticket_board.workflow_action', '', true);
            PERFORM set_config('ticket_board.workflow_actor', '', true);
        END;
    END LOOP;
    RETURN jsonb_build_object('claimed', false, 'ticket', NULL, 'reason', 'nothing ready');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_pull_idle_capacity(p_idle jsonb, p_now timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb:=ticket_board.declared_scheduling(); waiting record; free jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_pull_idle_capacity');
    IF policy IS NULL THEN RETURN 0; END IF;
    SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb) INTO free
      FROM jsonb_each_text(coalesce(p_idle,'{}'::jsonb)) AS e(k, v)
     -- Only a declared claimant is idle capacity; the listener names the
     -- running ones (SYRD-573), and nothing else it might send is believed.
     WHERE k IN (SELECT ticket_board.pull_claimant_pool(ticket_board.declared_workflow()))
       AND ticket_board.ticket_current_reserved_ticket(k) IS NULL;
    IF free='{}'::jsonb THEN RETURN 0; END IF;
    SELECT t.id, t.state, t.assignee, ns.entered_current_state_at AS since INTO waiting
      FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
     WHERE t.state=policy->>'ready_stage'
       AND ns.entered_current_state_at <= p_now - make_interval(secs => coalesce((policy->>'idle_alert_seconds')::int, 900))
       AND EXISTS (SELECT FROM jsonb_object_keys(free) k WHERE ticket_board.ticket_ready_for_pull(t.id, k))
     ORDER BY ns.entered_current_state_at, t.ticket_number LIMIT 1;
    IF waiting.id IS NULL THEN RETURN 0; END IF;
    -- Once per episode, delivered or not: the queue's own dedupe forgets a
    -- row once it is acknowledged; the trace of its enqueue does not.
    IF EXISTS (SELECT FROM ticket_board.notification_trace tr
               WHERE tr.ticket_id=waiting.id AND tr.event='enqueue'
                 AND tr.detail->>'dedupe_key'='pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint) THEN
        RETURN 0; END IF;
    PERFORM ticket_board.enqueue_notification(
        waiting.id, 'ticket_update', 'director',
        format('%s has been ready to claim since %s, and these implementers are idle without claiming it: %s.',
               waiting.id, to_char(waiting.since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'),
               (SELECT string_agg(k || ' (' || v || ')', ', ' ORDER BY k) FROM jsonb_each_text(free) AS e(k, v))),
        jsonb_build_object('kind', 'pull_idle_capacity', 'id', waiting.id,
                           'state', waiting.state, 'assignee', waiting.assignee, 'target_role', 'director',
                           'idle', free, 'ready_since', waiting.since),
        'pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint);
    RETURN 1;
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.pull_claimant_pool(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.transition_allows_actor(jsonb, jsonb, text) FROM PUBLIC;

COMMIT;
