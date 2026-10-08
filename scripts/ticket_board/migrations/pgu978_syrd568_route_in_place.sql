-- SYRD-568, second unit (after pgu977): routing in place.
--
-- On a Director-dispatched declared workflow a route, a same-stage
-- reassignment, an insert, force_move/override_move and director_edit to a
-- busy implementer no longer divert into the holding destination: the ticket
-- lands where it was put, assigned, and waits in that implementer's queue
-- (pgu977). Blocked work may be placed there too, and a waiting ticket cannot
-- be submitted. A pull policy (SYRD-539) still diverts into its ready stage;
-- legacy boards are unchanged. schema.sql carries the same copies.
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

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        IF ticket_board.declared_stage_kind(NEW.state)='draft' THEN NEW.assignee:=coalesce(ticket_board.stage_default_assignee(NEW.state),NEW.assignee); END IF;
        -- SYRD-568: only a pull policy still diverts a new ticket for a busy
        -- implementer; otherwise it waits in that implementer's queue.
        IF ticket_board.declared_scheduling() IS NOT NULL
           AND ticket_board.declared_stage_kind(NEW.state)='implementation' AND ticket_board.ticket_current_reserved_ticket(NEW.assignee,NEW.id) IS NOT NULL AND NOT NEW.manually_controlled THEN
            IF ticket_board.declared_workflow()->'queue' IS NULL OR ticket_board.declared_workflow()->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
            NEW.state:=ticket_board.declared_workflow()->'queue'->>'stage'; NEW.assignee:=ticket_board.declared_workflow()->'queue'->>'assignee';
        END IF;
        PERFORM ticket_board.require_stage_owner_assignee(NEW.state,NEW.assignee);
        RETURN NEW;
    END IF;
    IF NEW.state = 'draft' THEN
        NEW.assignee := coalesce(ticket_board.stage_default_assignee('draft'), 'unassigned');
        NEW.parked := false;
    END IF;
    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);
    IF NEW.state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(NEW.assignee)
       AND ticket_board.ticket_current_reserved_ticket(NEW.assignee, NEW.id) IS NOT NULL THEN
        NEW.state := 'backlog';
        NEW.parked := false;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reassign(
    id text,
    assignee text,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text := ticket_board.current_app_actor();
    current_state text;
    current_assignee text;
    normalized_assignee text := btrim(lower(coalesce(reassign.assignee, '')));
    normalized_reason text := btrim(coalesce(reassign.reason, ''));
    ticket_row ticket_board.tickets%ROWTYPE;
    message text;
BEGIN
    -- Director-only at the database boundary, not merely at the API. A missing
    -- caller role is a refusal here rather than a fallback to the service
    -- identity: ownership changes are attributed or they do not happen.
    IF ticket_board.current_actor_role() <> 'ticket_board_service' OR actor IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'role % cannot call reassign',
            coalesce(nullif(actor, ''), ticket_board.current_actor_role())
            USING ERRCODE = '42501';
    END IF;
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'reassign requires a reason';
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(normalized_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', reassign.assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = reassign.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;
    IF current_assignee = normalized_assignee THEN
        RAISE EXCEPTION 'ticket % is already assigned to %', id, normalized_assignee;
    END IF;
    -- Refuse an owner the stage cannot have instead of moving the ticket to
    -- suit the owner.
    PERFORM ticket_board.require_stage_owner_assignee(current_state, normalized_assignee);

    -- Recorded before the write, so the reason survives even when the serial
    -- reservation then redirects the ticket and appends its own notice.
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director reassignment in ' || current_state || ': '
        || current_assignee || ' -> ' || normalized_assignee
        || '. Reason: ' || normalized_reason
    );

    UPDATE ticket_board.tickets
    SET assignee = normalized_assignee
    WHERE tickets.id = reassign.id;

    SELECT * INTO ticket_row FROM ticket_board.tickets WHERE tickets.id = reassign.id;
    -- Serial focus held it. The queue announcement has already told the
    -- director, and the target implementer has no actionable work yet, so a
    -- second notification here would be wrong rather than merely noisy.
    IF ticket_row.state IS DISTINCT FROM current_state OR ticket_row.queued_for_assignee <> '' THEN
        RETURN;
    END IF;
    -- Held or blocked work is not actionable either. It stays reassigned; the
    -- existing unblock and release paths announce it when it becomes real work.
    -- SYRD-568: a ticket now waiting in its new owner's queue is not theirs to act on yet.
    IF coalesce(ticket_row.manually_controlled, false) OR ticket_board.ticket_has_unresolved_blockers(id)
       OR ticket_board.ticket_serial_waiting(id) THEN
        RETURN;
    END IF;
    message := id
        || CASE WHEN ticket_row.title <> '' THEN ' -- ' || ticket_row.title ELSE '' END
        || ' is now assigned to you';
    -- One notification, through the same durable queue every other handoff
    -- uses: it dedupes per transaction, suppresses a director self-handoff, and
    -- stays silent for stages that declare no notification.
    PERFORM ticket_board.enqueue_transition_notification(
        id,
        ticket_row.title,
        current_state,
        ticket_row.state,
        ticket_row.assignee,
        ticket_row.updated_at,
        ticket_row.ticket_number,
        message
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.force_move(
    id text,
    new_state text,
    assignee text,
    suppress_notification boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_state text;
    current_assignee text;
    target_state text;
    serial_focus_queued boolean := false;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'force_move');
    IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = new_state) THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = force_move.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    target_state := new_state;
    -- SYRD-568: a declared workflow queues assigned implementation work in
    -- place, so only a legacy board still diverts it.
    IF new_state = 'in_progress'
       AND ticket_board.declared_workflow() IS NULL
       AND ticket_board.ticket_is_implementer_assignee(force_move.assignee)
       AND ticket_board.ticket_current_reserved_ticket(force_move.assignee, force_move.id) IS NOT NULL THEN
        target_state := 'backlog';
        serial_focus_queued := true;
    END IF;

    PERFORM set_config('ticket_board.force_move', 'on', true);
    IF suppress_notification THEN
        PERFORM set_config('ticket_board.suppress_transition_notify', 'on', true);
    END IF;
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director override: '
        || current_state
        || '/'
        || current_assignee
        || ' -> '
        || new_state
        || '/'
        || assignee
        || CASE
            WHEN serial_focus_queued THEN ' (queued: implementer already has active work)'
            ELSE ''
        END
        || CASE WHEN suppress_notification THEN ' (notification suppressed)' ELSE '' END
    );

    UPDATE ticket_board.tickets
    SET state = target_state,
        assignee = force_move.assignee,
        parked = false
    WHERE tickets.id = force_move.id;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT suppress_notification
       AND current_state IS NOT DISTINCT FROM target_state
       AND current_assignee IS DISTINCT FROM assignee THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'assignee');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.director_edit(
    id text,
    patch jsonb,
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
    caller text;
    invalid_field text;
    current_ticket ticket_board.tickets%ROWTYPE;
    field text;
    old_doc jsonb;
    new_doc jsonb;
    target_state text;
    target_assignee text;
    normalized_parent text;
    seen text[];
    loops int := 0;
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call director_edit; ticket_board_service is the only database writer', actor
            USING ERRCODE = '42501';
    END IF;
    caller := ticket_board.current_app_actor();
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'role % cannot call director_edit', coalesce(caller, '<none>')
            USING ERRCODE = '42501';
    END IF;
    IF patch IS NULL OR jsonb_typeof(patch) <> 'object' THEN
        RAISE EXCEPTION 'director_edit patch must be an object';
    END IF;
    IF btrim(coalesce(reason, '')) = '' THEN
        RAISE EXCEPTION 'director_edit requires a reason';
    END IF;

    SELECT key INTO invalid_field
    FROM jsonb_object_keys(patch) AS key
    WHERE key NOT IN (
        'title','body','parent_id','implementation','audit_prompt','origin_project',
        'external_source_ref','blocked_reason','assignee','state','needs_audit',
        'needs_inspection','needs_user_signoff','commit_exempt','regression',
        'manually_controlled','queued_for_assignee','queued_behind_ticket',
        'audit_signoff','inspector_signoff','user_signoff'
    )
    ORDER BY key LIMIT 1;
    IF invalid_field IS NOT NULL THEN
        -- Identity and creation provenance are not editable by anybody.
        RAISE EXCEPTION 'director_edit cannot update: %', invalid_field;
    END IF;

    FOREACH field IN ARRAY ticket_board.signoff_fields() LOOP
        IF patch ? field AND coalesce((patch->>field)::boolean, false) THEN
            RAISE EXCEPTION
                'director_edit cannot create sign-off %; only its own sign-off action may', field
                USING ERRCODE = '42501';
        END IF;
    END LOOP;

    SELECT * INTO current_ticket FROM ticket_board.tickets WHERE tickets.id = director_edit.id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    old_doc := to_jsonb(current_ticket);

    IF patch ? 'assignee' THEN
        target_assignee := patch->>'assignee';
        IF NOT ticket_board.ticket_valid_assignee(target_assignee) THEN
            RAISE EXCEPTION 'invalid assignee: %', target_assignee;
        END IF;
    END IF;
    IF patch ? 'state' THEN
        target_state := patch->>'state';
        IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = target_state) THEN
            RAISE EXCEPTION 'invalid state: %', target_state;
        END IF;
    END IF;
    IF patch ? 'parent_id' THEN
        normalized_parent := nullif(upper(btrim(coalesce(patch->>'parent_id', ''))), '');
        IF normalized_parent = director_edit.id THEN
            RAISE EXCEPTION 'a ticket cannot be its own parent';
        END IF;
        IF normalized_parent IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM ticket_board.tickets WHERE tickets.id = normalized_parent) THEN
                RAISE EXCEPTION 'parent ticket not found: %', normalized_parent;
            END IF;
            seen := ARRAY[director_edit.id];
            WHILE normalized_parent IS NOT NULL LOOP
                loops := loops + 1;
                IF loops > 64 THEN RAISE EXCEPTION 'parent chain is too deep'; END IF;
                IF normalized_parent = ANY (seen) THEN
                    RAISE EXCEPTION 'parent change would create a cycle';
                END IF;
                seen := seen || normalized_parent;
                SELECT nullif(btrim(parent_id), '') INTO normalized_parent
                FROM ticket_board.tickets WHERE tickets.id = normalized_parent;
            END LOOP;
            normalized_parent := upper(btrim(patch->>'parent_id'));
        END IF;
    END IF;

    -- Serial focus is an invariant of moving into an implementer's slot, not a
    -- property of the operation that moved it there.
    -- SYRD-568: only a legacy board still diverts; a declared one queues in place.
    IF target_state = 'in_progress'
       AND ticket_board.declared_workflow() IS NULL
       AND ticket_board.ticket_is_implementer_assignee(coalesce(target_assignee, current_ticket.assignee))
       AND ticket_board.ticket_current_reserved_ticket(
               coalesce(target_assignee, current_ticket.assignee), director_edit.id) IS NOT NULL THEN
        target_state := 'backlog';
    END IF;

    -- Its own window, named for this ticket and open for this statement only.
    -- Deliberately not the narrated-move one: a Director edit should not
    -- inherit everything a forced move may bypass, and the sign-off boundary
    -- is enforced independently of either.
    PERFORM set_config('ticket_board.director_edit_target', director_edit.id, true);
    UPDATE ticket_board.tickets SET
        title = CASE WHEN patch ? 'title' THEN patch->>'title' ELSE title END,
        body = CASE WHEN patch ? 'body' THEN patch->>'body' ELSE body END,
        parent_id = CASE WHEN patch ? 'parent_id' THEN coalesce(normalized_parent, '') ELSE parent_id END,
        implementation = CASE WHEN patch ? 'implementation' THEN patch->>'implementation' ELSE implementation END,
        audit_prompt = CASE WHEN patch ? 'audit_prompt' THEN patch->>'audit_prompt' ELSE audit_prompt END,
        origin_project = CASE WHEN patch ? 'origin_project' THEN patch->>'origin_project' ELSE origin_project END,
        external_source_ref = CASE WHEN patch ? 'external_source_ref' THEN patch->>'external_source_ref' ELSE external_source_ref END,
        blocked_reason = CASE WHEN patch ? 'blocked_reason' THEN patch->>'blocked_reason' ELSE blocked_reason END,
        queued_for_assignee = CASE WHEN patch ? 'queued_for_assignee' THEN patch->>'queued_for_assignee' ELSE queued_for_assignee END,
        queued_behind_ticket = CASE WHEN patch ? 'queued_behind_ticket' THEN patch->>'queued_behind_ticket' ELSE queued_behind_ticket END,
        assignee = coalesce(target_assignee, assignee),
        state = coalesce(target_state, state),
        needs_audit = CASE WHEN patch ? 'needs_audit' THEN (patch->>'needs_audit')::boolean ELSE needs_audit END,
        needs_inspection = CASE WHEN patch ? 'needs_inspection' THEN (patch->>'needs_inspection')::boolean ELSE needs_inspection END,
        needs_user_signoff = CASE WHEN patch ? 'needs_user_signoff' THEN (patch->>'needs_user_signoff')::boolean ELSE needs_user_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
        manually_controlled = CASE WHEN patch ? 'manually_controlled' THEN (patch->>'manually_controlled')::boolean ELSE manually_controlled END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN false ELSE audit_signoff END,
        inspector_signoff = CASE WHEN patch ? 'inspector_signoff' THEN false ELSE inspector_signoff END,
        user_signoff = CASE WHEN patch ? 'user_signoff' THEN false ELSE user_signoff END
    WHERE tickets.id = director_edit.id;
    PERFORM set_config('ticket_board.director_edit_target', '', true);

    SELECT to_jsonb(tickets) INTO new_doc FROM ticket_board.tickets WHERE tickets.id = director_edit.id;
    FOR field IN SELECT jsonb_object_keys(patch) LOOP
        IF old_doc->field IS DISTINCT FROM new_doc->field THEN
            INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
            VALUES (director_edit.id, caller, btrim(reason), field, old_doc->field, new_doc->field);
        END IF;
    END LOOP;
    PERFORM ticket_board.append_ticket_comment(
        director_edit.id, caller,
        'Director edit: ' || btrim(reason)
    );
    PERFORM ticket_board.touch_ticket(director_edit.id);
END;
$$;

COMMIT;
