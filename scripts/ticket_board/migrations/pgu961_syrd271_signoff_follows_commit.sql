-- SYRD-271: an Audit sign-off carried across a new commit on resubmission.
--
-- MEFP-4 was Audit-approved for 6d4ee1a, reached Director review, and was
-- routed back to Ops by a plain `move` -- which, like every move, clears no
-- sign-off. Ops pushed 4f9fd17 and submitted it. The declared executor skips a
-- review stage whose sign-off is already set, so the ticket went straight to
-- director_review with audit_signoff=true: a verdict on one commit presented
-- as a review of another. The undeclared trigger has the same shortcut
-- ("entering audit already signed goes on to director_review").
--
-- A sign-off reviews one commit. When work leaves implementation carrying a
-- different commit -- or as a no-code submission -- it is new work: every
-- review sign-off is cleared before the gates are walked, so the ticket enters
-- review for exactly what it carries. The undeclared submit_to_audit already
-- cleared Audit's and the Inspector's sign-offs but never the User's; the
-- trigger now clears all three.
--
-- Deliberately NOT changed: mark_done may record a commit other than the
-- audited candidate. The Director closes on the integration commit -- a
-- cherry-pick or merge of the audited candidate onto an advanced main, with
-- its provenance narrated (SYRD-163, SYRD-194, SYRD-226) -- and refusing that
-- would break integration, not protect review.
-- Resubmitting the SAME commit keeps its review: that exact work was seen. A
-- no-code submission has no commit to show it is the same work, so it is
-- always reviewed as new.
--
-- Redefines enforce_declared_ticket_update and enforce_ticket_workflow_update
-- whole, with bodies identical to schema.sql's (which now carries one body for
-- the undeclared trigger in both of its places); the drift guard compares them.
-- Idempotent: CREATE OR REPLACE only.
BEGIN;

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
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    IF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
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

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    blocker_id text;
    hardcoded_transition_allowed boolean;
    config_transition_allowed boolean;
    shadow_actor text;
    transition_check_state text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        RETURN ticket_board.enforce_declared_ticket_update(OLD,NEW);
    END IF;
    IF NEW.state = 'draft' THEN
        IF OLD.state IS DISTINCT FROM NEW.state THEN
            NEW.assignee := 'unassigned';
        END IF;
        NEW.parked := false;
    END IF;

    IF NEW.state <> 'backlog' OR OLD.state IN ('done', 'cancelled') THEN
        NEW.parked := false;
    END IF;

    IF current_setting('ticket_board.force_move', true) = 'on'
       OR nullif(current_setting('ticket_board.director_edit_target', true), '') = NEW.id THEN
        RETURN NEW;
    END IF;

    IF OLD.state = 'audit' AND NEW.state = 'analysis' THEN
        NEW.assignee := 'unassigned';
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'dat'
       AND NOT NEW.needs_user_signoff
       AND (OLD.needs_user_signoff OR OLD.state IS DISTINCT FROM NEW.state) THEN
        NEW.state := 'director_review';
    END IF;
    IF NEW.state = 'user_review'
       AND NOT NEW.needs_user_signoff
       AND (
           OLD.needs_user_signoff
           OR (
               OLD.state IS DISTINCT FROM NEW.state
               AND OLD.state IS DISTINCT FROM 'analysis'
           )
       ) THEN
        NEW.state := 'director_review';
    END IF;
    IF OLD.state = 'analysis' AND NEW.state = 'user_review' AND NEW.needs_user_signoff THEN
        RAISE EXCEPTION 'analysis -> user_review is only for user information requests with needs_user_signoff=false';
    END IF;
    IF NOT NEW.needs_user_signoff THEN
        NEW.user_signoff := false;
    END IF;
    IF NOT NEW.needs_inspection THEN
        NEW.inspector_signoff := false;
    END IF;
    IF NOT NEW.needs_audit THEN
        NEW.audit_signoff := false;
    END IF;
    -- A sign-off is a review of one commit (SYRD-271), on this undeclared path
    -- as on the declared one: entering audit already signed skips it below.
    -- Different work leaving implementation is reviewed afresh. submit_to_audit
    -- here already clears Audit's and the Inspector's sign-offs, but never
    -- the User's, which skips user_review below just the same.
    IF OLD.state = 'in_progress' AND NEW.state IS DISTINCT FROM OLD.state
       AND (NEW.commit_hash IS DISTINCT FROM OLD.commit_hash OR NEW.commit_exempt IS DISTINCT FROM OLD.commit_exempt)
       AND (btrim(NEW.commit_hash) <> '' OR NEW.commit_exempt) THEN
        -- Carried from the old commit, not granted by this write.
        NEW.audit_signoff := NEW.audit_signoff AND NOT OLD.audit_signoff;
        NEW.inspector_signoff := NEW.inspector_signoff AND NOT OLD.inspector_signoff;
        NEW.user_signoff := NEW.user_signoff AND NOT OLD.user_signoff;
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        NEW.state := 'inspection';
        NEW.inspector_signoff := false;
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    transition_check_state := NEW.state;
    IF OLD.state IN ('in_progress', 'inspection') AND NEW.state = 'audit' AND NOT NEW.needs_audit THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.audit_signoff := false;
    END IF;
    IF NEW.state = 'inspection' AND OLD.state IS DISTINCT FROM NEW.state THEN
        NEW.audit_signoff := false;
        NEW.inspector_signoff := false;
    END IF;
    IF NEW.state = 'inspection' AND NOT NEW.needs_inspection THEN
        RAISE EXCEPTION 'needs_inspection must be true before a ticket can enter inspection';
    END IF;
    IF NEW.state = 'user_review' AND NEW.user_signoff THEN
        NEW.state := 'director_review';
        NEW.assignee := 'director';
        transition_check_state := NEW.state;
    END IF;
    IF NEW.state = 'audit' AND NEW.audit_signoff THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.assignee := 'director';
        transition_check_state := NEW.state;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF NEW.state = 'done'
       AND OLD.state IN ('backlog', 'analysis')
       AND current_setting('ticket_board.utility_task_complete', true) = 'on'
       AND NEW.commit_exempt THEN
        RETURN NEW;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        transition_check_state := coalesce(transition_check_state, NEW.state);
        hardcoded_transition_allowed := ticket_board.workflow_transition_allowed_hardcoded(OLD.state, transition_check_state);
        config_transition_allowed := ticket_board.workflow_transition_allowed_config(OLD.state, transition_check_state);
        shadow_actor := coalesce(nullif(current_setting('ticket_board.caller_role', true), ''), current_user);
        PERFORM ticket_board.log_workflow_transition_shadow_mismatch(
            NEW.id,
            OLD.state,
            transition_check_state,
            shadow_actor,
            hardcoded_transition_allowed,
            config_transition_allowed
        );
        IF NOT config_transition_allowed THEN
            RAISE EXCEPTION 'illegal state transition: % -> %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF OLD.state IN ('inspection', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled')
       AND NEW.state IN ('backlog', 'analysis', 'in_progress') THEN
        NEW.inspector_signoff := false;
        NEW.audit_signoff := false;
        NEW.commit_hash := '';
    END IF;

    IF OLD.state = 'inspection' AND NEW.state = 'in_progress' THEN
        NEW.inspector_signoff := false;
    END IF;

    IF OLD.state = 'audit' AND NEW.state = 'analysis' THEN
        NEW.assignee := 'unassigned';
    END IF;

    IF OLD.state NOT IN ('done', 'cancelled') AND NEW.state = 'cancelled' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_comments
            WHERE ticket_id = NEW.id
              AND btrim(text) <> ''
              AND xmin = pg_current_xact_id()::xid
        ) THEN
            RAISE EXCEPTION 'cancelling a ticket requires a non-empty comment explaining why';
        END IF;
    ELSIF OLD.state IN ('done', 'cancelled') AND NEW.state = 'cancelled' AND OLD.state IS DISTINCT FROM NEW.state THEN
        RAISE EXCEPTION 'only active tickets can be cancelled';
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.ticket_is_forward_promotion(OLD.state, NEW.state)
       AND ticket_board.ticket_has_unresolved_blockers(NEW.id) THEN
        SELECT b.blocker_ticket_id
        INTO blocker_id
        FROM ticket_board.ticket_blockers b
        WHERE b.ticket_id = NEW.id
          AND NOT b.resolved
        ORDER BY b.position
        LIMIT 1;
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %', blocker_id;
    END IF;

    IF NEW.state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(NEW.assignee)
       AND ticket_board.ticket_current_reserved_ticket(NEW.assignee, NEW.id) IS NOT NULL THEN
        NEW.state := 'backlog';
        NEW.parked := false;
    END IF;

    IF OLD.state <> 'director_review'
       AND NEW.state = 'director_review'
       AND NOT NEW.audit_signoff
       AND NEW.needs_audit
       AND EXISTS (
           SELECT 1
           FROM ticket_board.workflow_stages ws
           WHERE ws.name = 'audit'
       ) THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can enter director_review';
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'tickets requiring inspection must pass through inspection before audit';
    END IF;
    IF OLD.state NOT IN ('audit', 'dat', 'user_review', 'director_review')
       AND NEW.state = 'director_review'
       AND NOT (coalesce(transition_check_state, NEW.state) = 'audit' AND NOT NEW.needs_audit)
       AND NOT ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state) THEN
        RAISE EXCEPTION 'tickets must pass through audit before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state = 'director_review' AND NEW.needs_user_signoff THEN
        RAISE EXCEPTION 'tickets requiring User signoff must pass through DAT and user_review before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state IN ('dat', 'user_review', 'director_review', 'done') AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can leave audit';
    END IF;
    IF OLD.state = 'dat' AND NEW.state = 'user_review' AND NOT NEW.needs_user_signoff THEN
        RAISE EXCEPTION 'DAT only applies to tickets requiring UAT';
    END IF;
    IF OLD.state = 'user_review'
       AND NEW.state = 'director_review'
       AND NEW.needs_user_signoff
       AND NOT NEW.user_signoff THEN
        RAISE EXCEPTION 'user_signoff must be true before a ticket can leave user_review';
    END IF;
    IF NEW.state = 'done'
       AND OLD.state <> 'done'
       AND NOT ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state) THEN
        RAISE EXCEPTION 'tickets can only enter done through a configured transition';
    END IF;
    IF OLD.state <> 'done' AND NEW.state = 'done' AND NOT NEW.commit_exempt AND btrim(NEW.commit_hash) = '' THEN
        RAISE EXCEPTION 'commit_hash is required before a ticket can enter done';
    END IF;

    RETURN NEW;
END;
$$;

COMMIT;
