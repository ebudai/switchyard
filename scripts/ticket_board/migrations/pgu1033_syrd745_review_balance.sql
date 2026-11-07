BEGIN;

-- SYRD-745 (pgu1033): waiting review work goes to an idle owner of the same review stage.
--
-- enforce_declared_ticket_update gains balance_review_stage's one-ticket window in its same-stage branch, beside
-- Router's (it is otherwise pgu1032's, with SYRD-746's restore window): the window names one waiting review for one statement and admits only an owner
-- change between two owners of the stage (require_stage_owner_assignee and the flag checks still apply). balance_
-- review_stage is new. schema.sql carries both; rbac.sql grants the
-- function to the listener. Nothing is moved by the migration itself.

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
    queued_for text; reserved_by text; origin jsonb;
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

    -- SYRD-746: a Director's restore of a mistaken reopen puts back exactly the closed disposition the reopen took
    -- away, recorded when it did, and nothing else. The window names one ticket and is open for one statement,
    -- opened only by restore_reopened_ticket; what passes through it must equal the recorded disposition, sign-off
    -- by sign-off, so it can neither raise a sign-off that was not there nor carry any other commit.
    IF nullif(current_setting('ticket_board.restore_reopen', true), '') = previous.id THEN
        IF NOT ticket_board.reopen_restore_admits(previous, proposed) THEN
            RAISE EXCEPTION 'a restore may put back only the closed disposition recorded when % was reopened', previous.id
                USING ERRCODE='42501';
        END IF;
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
    -- SYRD-598: withdrawing one named sign-off. ticket_board.withdraw_signoff,
    -- the only operation that opens this window, names one ticket and one
    -- sign-off for one statement, and only that sign-off may fall: no stage,
    -- owner or other flag moves through it, and nothing is raised.
    IF split_part(nullif(current_setting('ticket_board.signoff_withdrawal', true), ''), ':', 1) = previous.id THEN
        IF previous.state IS DISTINCT FROM proposed.state OR previous.assignee IS DISTINCT FROM proposed.assignee THEN
            RAISE EXCEPTION 'withdrawing a sign-off moves nothing' USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND NOT (flag.key = split_part(current_setting('ticket_board.signoff_withdrawal', true), ':', 2)
                        AND NOT ticket_board.workflow_flag(doc,flag.key,cfg)) THEN
                RAISE EXCEPTION 'withdrawing a sign-off changes only that sign-off, not %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- SYRD-599: a Router operation's window names one ticket and one statement,
    -- and admits exactly its own change (require_router_change). Everything
    -- after this still applies to it: the stage-owner check, serial focus, the
    -- declared transition and its bookkeeping.
    IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
        PERFORM ticket_board.require_router_change(previous, proposed, actor);
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director'
           AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id
           AND nullif(current_setting('ticket_board.review_balance_ticket', true), '') IS DISTINCT FROM previous.id THEN  -- SYRD-745
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
                -- SYRD-599: Router changes the owner and nothing else. Diverting its
                -- pick into the ready stage would move the ticket it was told to
                -- leave where it is, so it is refused instead: Router chooses a
                -- free owner or hands the ticket back.
                IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
                    RAISE EXCEPTION '% already has % active; on this pull-scheduled board Router cannot queue work behind it: choose an owner with no active ticket, or return % to the Director',
                        proposed.assignee, ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id), previous.id
                        USING ERRCODE='42501'; END IF;
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
    -- SYRD-599: router_return is declared so it is visible, owner-scoped and
    -- bookkept like any move, but only its own operation may take it: it
    -- carries the revision check and the Director's handoff.
    IF tr->>'action'='router_return'
       AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id THEN
        RAISE EXCEPTION 'router_return is taken only through ticket_board.router_return' USING ERRCODE='42501'; END IF;
    -- SYRD-673: Audit's kick-back returns the work to the stage and owner that
    -- produced the revision under review, as the board recorded it -- never to
    -- the declared `to`, the current assignee or a guess. With no valid record
    -- it is refused, and nothing moves.
    IF ticket_board.audit_return_transition(tr) THEN
        origin:=ticket_board.audit_return_status(previous.id);
        IF NOT coalesce((origin->>'valid')::boolean,false) THEN
            RAISE EXCEPTION '% cannot be kicked back: %. Nothing was moved. Ask the Director (ticket-board-write request-dependency % --role director); the repair: %',
                previous.id, coalesce(origin->>'problem','the board has no record of who produced its revision'), previous.id,
                coalesce(origin->>'repair','the Director records where it returns with set-audit-return') USING ERRCODE='55000'; END IF;
    -- SYRD-719: Audit's documentation return goes to the Documentation stage and
    -- owner the ticket passed with the revision under review, keeping it as the
    -- candidate the correction builds on. Without that record it is refused, and
    -- nothing moves: the ordinary kick-back is the safe way back.
    ELSIF ticket_board.audit_documentation_return_transition(tr) THEN
        origin:=ticket_board.audit_return_status(previous.id)->'documentation';
        IF NOT coalesce((origin->>'valid')::boolean,false) THEN
            RAISE EXCEPTION '% cannot be returned to Documentation: %. Nothing was moved. A finding that is not documentation-only, or that Documentation cannot take, goes back to its substantive origin with audit-kick-back',
                previous.id, coalesce(origin->>'problem','the board has no record that it passed through Documentation')
                USING ERRCODE='55000'; END IF;
    END IF;
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
       -- SYRD-629: the User handing their review to the Director promotes nothing.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-599: Router handing work back to triage promotes nothing.
       AND tr->>'action'<>'router_return'
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
       -- SYRD-629: nor is the User handing it to the Director, in a stage they
       -- alone hold to decide it: the User's message about a candidate keeps the
       -- candidate, its commit and Audit's sign-off, for the Director to return
       -- to the User or send on. Only from User Review: a move into Analysis
       -- from any other review still needs that review's sign-off.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    -- SYRD-719: work returned to its substantive origin is changed work, so the
    -- Documentation stage this Audit stage returns documentation to owes its
    -- sign-off again: the next submission passes the documentation gate anew.
    IF ticket_board.audit_return_transition(tr) THEN
        FOR reset IN SELECT s->>'signoff' FROM jsonb_array_elements(cfg->'transitions') x, jsonb_array_elements(cfg->'stages') s
                      WHERE x->>'from'=previous.state AND ticket_board.audit_documentation_return_transition(x)
                        AND s->>'name'=x->>'to' AND s->>'signoff' IS NOT NULL LOOP
            doc:=ticket_board.set_workflow_flag(doc,reset,false);
        END LOOP;
    END IF;
    -- A return hands work back to be redone, so the candidate goes -- except to
    -- a review stage, whose owner produced it and reviews on top of it (SYRD-673).
    IF tr->>'primitive' IN ('return','reopen') AND ticket_board.declared_stage_kind(origin->>'stage') IS DISTINCT FROM 'review' THEN
        doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
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
    target:=coalesce(origin->>'stage',proposed.state);
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        -- The producer's stage is exact: no gate skips past it (SYRD-673).
        EXIT WHEN origin IS NOT NULL OR (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    -- SYRD-599: on a Router-enabled board the Director's ordinary route into
    -- Implementation is Router's, unless the Director names an Implementation
    -- owner: that is the direct route, and every rule below applies to it. A
    -- Router that cannot be told is refused, never replaced by an implementer:
    -- going direct is the Director's choice, not a fallback.
    IF dest->>'kind'='implementation' AND actor='director' AND tr->>'primitive'='move'
       AND source_stage->>'kind' NOT IN ('implementation','review')
       AND ticket_board.router_holds(NULL,'router') AND NOT proposed.assignee=ANY(owners) THEN
        IF NOT ticket_board.router_available() THEN
            RAISE EXCEPTION 'Router is not running, so % cannot be routed through it; route it to an Implementation owner directly (%), or start Router',
                previous.id, array_to_string(owners, ', ') USING ERRCODE='55000';
        END IF;
        proposed.assignee:='router';
    END IF;
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF origin IS NOT NULL THEN
        proposed.assignee:=origin->>'assignee';
    ELSIF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners)
       AND NOT ticket_board.router_holds(target,proposed.assignee) THEN proposed.assignee:=owners[1]; END IF;
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
       AND dest->>'kind'='implementation' AND NOT ticket_board.router_holds(dest->>'name',proposed.assignee)
       AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
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

-- SYRD-745: waiting review work goes to an idle owner of the same review stage.
--
-- A transition into a stage names an owner only when it says so; otherwise the declared update gives the ticket the
-- stage's first owner. On MEFP, Audit is owned by audit and audit-2, so every submission went to audit and waited
-- behind its current review ("finish current") while audit-2 sat live, idle and empty.
--
-- The listener says which owners are live and idle (pull_pickup.why_not_eligible) and asks this, once per such
-- owner, per pass. It moves the oldest review that is only waiting -- behind its reviewer's current ticket
-- (finish_current_stage_blocker) -- to that owner, which must hold nothing in the stage and not be provider-held.
-- Never moved: a reviewer's current ticket; a manually controlled one, which is never waiting (finish_current_stage_
-- blocker counts it for nobody); a blocked or awaited one; and a re-review its reviewer already began (it was handed to them in this stage before). The move is recorded on the ticket, and
-- the new owner is handed it as a reassignment is.
CREATE OR REPLACE FUNCTION ticket_board.balance_review_stage(p_stage text, p_to text, p_now timestamptz)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); owners text[]; candidate record; busy_with text;
    moved ticket_board.tickets;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('balance_review_stage');
    SELECT ARRAY(SELECT jsonb_array_elements_text(x->'owners')) INTO owners
      FROM jsonb_array_elements(coalesce(cfg->'stages', '[]'::jsonb)) x WHERE x->>'name' = p_stage AND x->>'kind' = 'review';
    IF coalesce(cardinality(owners), 0) < 2 OR NOT p_to = ANY(owners) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'not an owner of a shared review stage');
    END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE t.state = p_stage AND t.assignee = p_to) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'it already holds work in this stage');
    END IF;
    IF ticket_board.role_provider_hold_is_current(p_to, p_now) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'its provider is held');
    END IF;
    FOR candidate IN
        SELECT t.id, t.assignee FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE t.state = p_stage AND t.assignee = ANY(owners) AND t.assignee <> p_to
           AND coalesce(btrim(ns.awaiting_role), '') = ''
           AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
           AND NOT EXISTS (SELECT FROM ticket_board.notification_trace nt
                            WHERE nt.ticket_id = t.id AND nt.target_role = t.assignee AND nt.ticket_state_at_event = p_stage
                              AND nt.kind = 'transition' AND nt.event = 'send')
         ORDER BY ns.entered_current_state_at, t.ticket_number
    LOOP
        busy_with := nullif(ticket_board.finish_current_stage_blocker(candidate.id, candidate.assignee, p_stage, p_now), '');
        CONTINUE WHEN busy_with IS NULL;  -- its reviewer's current ticket: never taken from it
        PERFORM set_config('ticket_board.review_balance_ticket', candidate.id, true);
        PERFORM set_config('ticket_board.workflow_actor', 'board', true);
        UPDATE ticket_board.tickets SET assignee = p_to
         WHERE id = candidate.id AND state = p_stage AND assignee = candidate.assignee
        RETURNING * INTO moved;
        PERFORM set_config('ticket_board.workflow_actor', '', true);
        PERFORM set_config('ticket_board.review_balance_ticket', '', true);
        CONTINUE WHEN moved.id IS NULL;  -- moved by someone else meanwhile
        PERFORM ticket_board.append_ticket_comment(moved.id, 'ticket_board_service', format(
            'Board balance in %s: %s -> %s. %s is reviewing %s, and %s was live and idle with nothing in %s, so this '
            'waiting review goes to it.', p_stage, candidate.assignee, p_to, candidate.assignee, busy_with, p_to, p_stage));
        PERFORM ticket_board.enqueue_transition_notification(
            moved.id, moved.title, p_stage, moved.state, moved.assignee, moved.updated_at, moved.ticket_number,
            moved.id || CASE WHEN moved.title <> '' THEN ' -- ' || moved.title ELSE '' END || ' is now assigned to you');
        RETURN jsonb_build_object('moved', true, 'ticket', moved.id, 'from', candidate.assignee, 'to', p_to, 'behind', busy_with);
    END LOOP;
    RETURN jsonb_build_object('moved', false, 'reason', 'no review is waiting');
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.balance_review_stage(text, text, timestamptz) FROM PUBLIC;

COMMIT;
