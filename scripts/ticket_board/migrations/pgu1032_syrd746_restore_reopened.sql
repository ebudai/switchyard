BEGIN;

-- SYRD-746 (pgu1032): a Director can restore a mistaken reopen, bounded, in one step.
--
-- MEFP-922, 2026-11-07: a Director reopened a ticket already done, audited and publicly integrated, after comparing the
-- wrong repository. The reopen cleared its commit and sign-offs, as a reopen must. From Analysis the workflow offered
-- no way back: an override to Done restored the stage but not the commit; director-edit and edit-fields rightly
-- refused the protected commit field; it took a second override, to Director Review, and an ordinary mark-done.
--
-- Now each time a ticket leaves a terminal stage the board records the closed disposition it left
-- (ticket_reopen_records), and restore_reopened_ticket puts exactly that back -- stage, owner, commit provenance and
-- sign-offs, atomically -- while nothing has happened since. Everything else about the ticket stays as it is. The
-- update passes through a window in enforce_declared_ticket_update that admits only the recorded values, so the
-- repair cannot raise a sign-off that was not there or carry any other commit.
--
-- enforce_declared_ticket_update below is pgu1022's with that one window added after the merge window; this migration
-- now owns it, and schema.sql's copy is the same, character for character.

-- What a reopen took away. One row each time a ticket leaves a stage the declared workflow calls terminal -- a reopen,
-- or a narrated override out of one -- holding the closed disposition it left: the stage, its owner, the commit and
-- its exemption, and every declared sign-off, as they were. Any later move of the ticket supersedes an unrestored row:
-- what it would put back is no longer the ticket's last word.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_reopen_records (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    reopened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reopened_by text NOT NULL,
    from_state text NOT NULL,
    to_state text NOT NULL,
    assignee text NOT NULL,
    commit_hash text NOT NULL,
    commit_exempt boolean NOT NULL,
    signoffs jsonb NOT NULL,
    superseded_at timestamptz,
    restored_at timestamptz,
    restored_by text,
    restore_reason text,
    CHECK ((restored_at IS NULL) = (restored_by IS NULL) AND (restored_at IS NULL) = (restore_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS ticket_reopen_records_ticket ON ticket_board.ticket_reopen_records (ticket_id, id);

-- Record a reopen, and supersede an unrestored one when the ticket moves again. Its actor is named from the session's
-- own settings -- never by current_app_actor(), which refuses a system actor with no caller role (SYRD-744).
CREATE OR REPLACE FUNCTION ticket_board.record_ticket_reopen()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); flag record; signoffs jsonb := '{}'::jsonb;
BEGIN
    IF cfg IS NULL OR nullif(current_setting('ticket_board.restore_reopen', true), '') = NEW.id THEN
        RETURN NEW;  -- no declared workflow; or the restore itself, which marks its own row
    END IF;
    UPDATE ticket_board.ticket_reopen_records SET superseded_at = clock_timestamp()
     WHERE ticket_id = NEW.id AND restored_at IS NULL AND superseded_at IS NULL;
    IF NOT coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = OLD.state), false)
       OR coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = NEW.state), false) THEN
        RETURN NEW;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF flag.value->>'kind' = 'signoff' THEN
            signoffs := signoffs || jsonb_build_object(flag.key, ticket_board.workflow_flag(to_jsonb(OLD), flag.key, cfg));
        END IF;
    END LOOP;
    INSERT INTO ticket_board.ticket_reopen_records (ticket_id, reopened_by, from_state, to_state, assignee, commit_hash,
                                                    commit_exempt, signoffs)
    VALUES (NEW.id, coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''),
                             nullif(current_setting('ticket_board.caller_role', true), ''), session_user::text),
            OLD.state, NEW.state, OLD.assignee, OLD.commit_hash, OLD.commit_exempt, signoffs);
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tickets_zz_reopen_record ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_reopen_record
    AFTER UPDATE OF state ON ticket_board.tickets
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION ticket_board.record_ticket_reopen();

-- Whether this update is exactly the restore of the ticket's live reopen record: the recorded stage, owner, commit,
-- exemption and every declared sign-off, and every other declared flag as it was.
CREATE OR REPLACE FUNCTION ticket_board.reopen_restore_admits(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); rec ticket_board.ticket_reopen_records; flag record;
BEGIN
    SELECT * INTO rec FROM ticket_board.ticket_reopen_records r WHERE r.ticket_id = previous.id ORDER BY r.id DESC LIMIT 1;
    IF rec.id IS NULL OR rec.restored_at IS NOT NULL OR rec.superseded_at IS NOT NULL
       OR proposed.state IS DISTINCT FROM rec.from_state OR proposed.assignee IS DISTINCT FROM rec.assignee
       OR proposed.commit_hash IS DISTINCT FROM rec.commit_hash OR proposed.commit_exempt IS DISTINCT FROM rec.commit_exempt THEN
        RETURN false;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF ticket_board.workflow_flag(to_jsonb(proposed), flag.key, cfg) IS DISTINCT FROM (CASE
               WHEN flag.value->>'kind' = 'signoff' THEN coalesce((rec.signoffs->>flag.key)::boolean, false)
               ELSE ticket_board.workflow_flag(to_jsonb(previous), flag.key, cfg) END) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

-- SYRD-746: the Director's bounded repair of a mistaken reopen. It puts back the closed disposition the ticket's last
-- reopen recorded -- its stage, owner, commit provenance and sign-offs, all at once -- and only while nothing has
-- happened since: the ticket still where the reopen left it, with no other candidate and no sign-off the closed ticket
-- did not hold (an override out of Done keeps both, and only its stage is put back). It records the
-- repair: a comment, a field-audit row for every field it moves, and the record marked restored. It never makes a
-- sign-off that was not there, nor a commit that was not recorded, and it starts no review cycle.
CREATE OR REPLACE FUNCTION ticket_board.restore_reopened_ticket(p_ticket text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); actor text; t ticket_board.tickets;
    rec ticket_board.ticket_reopen_records; doc jsonb; flag record; field text; before jsonb; after jsonb; why text;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    actor := ticket_board.current_app_actor();  -- the caller's role, which the record and comment name
    why := btrim(coalesce(p_reason, ''));
    IF why = '' THEN
        RAISE EXCEPTION 'restoring a reopened ticket needs a reason: what the reopen mistook' USING ERRCODE = '22023';
    END IF;
    IF cfg IS NULL THEN
        RAISE EXCEPTION 'this board has no declared workflow, so no reopen was recorded to restore' USING ERRCODE = '55000';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF t.id IS NULL THEN RAISE EXCEPTION 'no ticket %', p_ticket USING ERRCODE = '22023'; END IF;
    SELECT * INTO rec FROM ticket_board.ticket_reopen_records r WHERE r.ticket_id = t.id ORDER BY r.id DESC LIMIT 1;
    IF rec.id IS NULL THEN
        RAISE EXCEPTION '% has no recorded reopen to restore', t.id USING ERRCODE = '55000';
    ELSIF rec.restored_at IS NOT NULL THEN
        RAISE EXCEPTION '%''s last reopen was already restored, by % at %', t.id, rec.restored_by, rec.restored_at
            USING ERRCODE = '55000';
    ELSIF rec.superseded_at IS NOT NULL OR t.state IS DISTINCT FROM rec.to_state THEN
        RAISE EXCEPTION '% has moved since % reopened it (now in %): restoring would undo what happened since',
            t.id, rec.reopened_by, t.state USING ERRCODE = '55000';
    ELSIF btrim(t.commit_hash) <> '' AND t.commit_hash IS DISTINCT FROM rec.commit_hash THEN
        RAISE EXCEPTION '% carries a new candidate (%) since it was reopened: restoring would discard it', t.id, t.commit_hash
            USING ERRCODE = '55000';
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF flag.value->>'kind' = 'signoff' AND ticket_board.workflow_flag(to_jsonb(t), flag.key, cfg)
           AND NOT coalesce((rec.signoffs->>flag.key)::boolean, false) THEN
            RAISE EXCEPTION '% has a sign-off of its own since it was reopened (%): a review is under way', t.id, flag.key
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
    before := to_jsonb(t);
    doc := before || jsonb_build_object('state', rec.from_state, 'assignee', rec.assignee, 'commit_hash', rec.commit_hash,
                                        'commit_exempt', rec.commit_exempt);
    FOR flag IN SELECT * FROM jsonb_each(rec.signoffs) LOOP
        doc := ticket_board.set_workflow_flag(doc, flag.key, (flag.value #>> '{}')::boolean);
    END LOOP;
    PERFORM set_config('ticket_board.restore_reopen', t.id, true);
    UPDATE ticket_board.tickets
       SET state = doc->>'state', assignee = doc->>'assignee', commit_hash = doc->>'commit_hash',
           commit_exempt = (doc->>'commit_exempt')::boolean,
           audit_signoff = coalesce((doc->>'audit_signoff')::boolean, audit_signoff),
           inspector_signoff = coalesce((doc->>'inspector_signoff')::boolean, inspector_signoff),
           user_signoff = coalesce((doc->>'user_signoff')::boolean, user_signoff),
           workflow_flags = coalesce(doc->'workflow_flags', workflow_flags)
     WHERE id = t.id;
    PERFORM set_config('ticket_board.restore_reopen', '', true);
    SELECT * INTO t FROM ticket_board.tickets WHERE id = t.id;
    after := to_jsonb(t);
    FOREACH field IN ARRAY ARRAY['state', 'assignee', 'commit_hash', 'commit_exempt', 'audit_signoff', 'inspector_signoff',
                                 'user_signoff', 'workflow_flags'] LOOP
        IF before->field IS DISTINCT FROM after->field THEN
            INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
            VALUES (t.id, actor, 'restore_reopened: ' || why, field, before->field, after->field);
        END IF;
    END LOOP;
    UPDATE ticket_board.ticket_reopen_records SET restored_at = clock_timestamp(), restored_by = actor, restore_reason = why
     WHERE id = rec.id;
    PERFORM ticket_board.append_ticket_comment(t.id, actor, format(
        'Restored the closed disposition that %s''s reopen at %s took away: %s, owned by %s, commit %s, sign-offs %s. '
        'No review was repeated or made. Reason: %s',
        rec.reopened_by, to_char(rec.reopened_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS "UTC"'), rec.from_state,
        rec.assignee, CASE WHEN rec.commit_hash = '' THEN CASE WHEN rec.commit_exempt THEN 'none (no code)' ELSE 'none' END
                           ELSE rec.commit_hash END,
        coalesce((SELECT string_agg(key || CASE WHEN value = 'true'::jsonb THEN ' yes' ELSE ' no' END, ', ' ORDER BY key)
                    FROM jsonb_each(rec.signoffs)), 'none declared'), why));
    RETURN jsonb_build_object('restored', true, 'id', t.id, 'state', t.state, 'assignee', t.assignee,
                              'commit_hash', t.commit_hash, 'commit_exempt', t.commit_exempt, 'signoffs', rec.signoffs,
                              'reopened_by', rec.reopened_by, 'reopened_at', rec.reopened_at);
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
           AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id THEN
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

REVOKE ALL ON ticket_board.ticket_reopen_records FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.ticket_reopen_records_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_ticket_reopen() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.reopen_restore_admits(ticket_board.tickets, ticket_board.tickets) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.restore_reopened_ticket(text, text) FROM PUBLIC;
GRANT SELECT ON ticket_board.ticket_reopen_records TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.restore_reopened_ticket(text, text) TO ticket_board_service;

COMMIT;
