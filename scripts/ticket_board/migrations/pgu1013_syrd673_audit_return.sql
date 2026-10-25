-- SYRD-673: Audit's kick-back returns work to the producer of the revision it
-- audited, not always to Implementation.
--
-- audit_kick_back always landed in the declared Implementation stage with an
-- Implementation owner, so a ticket that reached Audit from a designer's draft
-- went to an implementer who never touched it. Now:
--   * a trigger records, on every change of stage or revision, who produced the
--     revision a ticket carries: the holder whose own transition carried a new
--     one, or the implementer or draft owner whose stage it left. Review,
--     Director and system stages pass it on unchanged; a revision made any other
--     way has no known producer. Each entry into the stage Audit's kick-back
--     leaves is recorded too, as evidence;
--   * audit_kick_back returns to that stage and owner, with the sign-off
--     clearing its declaration names, through the same executor; a return into
--     a review stage (the Librarian's) keeps the candidate it reviews on;
--   * with no known producer of the revision under review, or one the workflow
--     no longer admits, the kick-back is refused and moves nothing, and the
--     refusal names the repair: the Director's set_audit_return, which records
--     the return and moves nothing;
--   * a ticket already in that stage has no record: the Director is told once
--     here, and nothing is moved.
--
-- schema.sql carries the same objects.

BEGIN;

-- SYRD-673: Audit's kick-back returns work to the producer of the revision it audited.
--
-- audit_kick_back always went to the declared Implementation stage, and the
-- executor then picked an Implementation owner. A ticket that reached Audit
-- from a designer's draft (pie's design-review loop) went to an implementer who
-- never touched it, and the designer never saw the findings.
--
-- The board now records who produced each ticket's current revision: the stage
-- and owner that made it. That is the holder whose own transition carried a new
-- revision (a commit, or a no-code completion) -- an implementer's submission,
-- the Librarian's documentation commit -- or the implementer or draft owner
-- whose stage the ticket left. Review stages and the Director's and system
-- stages produce nothing: the ticket passes through them with its producer
-- unchanged. A new revision made any other way (an edit, an override) leaves
-- the producer unknown. Each entry into Audit is recorded too, as evidence.
-- Audit's kick-back returns to the producer, with the sign-off clearing its
-- declaration names. When the producer is unknown, did not produce the
-- revision now under review, or the workflow no longer admits it there, the
-- kick-back is refused and moves nothing: the Director records the return with
-- set_audit_return, and Audit kicks back as usual.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_audit_returns (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    -- Who produced the revision (commit, or no-code completion) in producer_commit/exempt:
    -- its stage and owner, by which action, or the Director's record of it
    -- (recorded_by, reason). producer_stage is NULL when it was made otherwise.
    producer_stage text,
    producer_assignee text,
    producer_action text NOT NULL,
    producer_at timestamptz NOT NULL,
    producer_commit text NOT NULL,
    producer_exempt boolean NOT NULL,
    recorded_by text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    -- Its latest entry into the stage Audit's kick-back leaves: from where, by
    -- which action, carrying which revision. Evidence; NULL outside that stage.
    audit_stage text,
    entered_from_stage text,
    entered_from_assignee text,
    entry_action text,
    entered_at timestamptz,
    entry_commit text,
    entry_exempt boolean
);

-- The transition that returns work to its producer: the declared
-- audit_kick_back, which stays declared as before so clients see the same
-- action; only where it lands is the ticket's own.
CREATE OR REPLACE FUNCTION ticket_board.audit_return_transition(tr jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(tr->>'action' = 'audit_kick_back' AND tr->>'primitive' = 'return', false);
$$;

-- A stage that kick-back leaves.
CREATE OR REPLACE FUNCTION ticket_board.audit_return_stage(p_stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
                    WHERE x->>'from' = p_stage AND ticket_board.audit_return_transition(x));
$$;

-- Whether p_role, holding a ticket in p_stage, produces its work there: an
-- implementer or a draft owner (a designer) that the stage declares as an
-- owner, outside review. Everyone else's stage is a connector.
CREATE OR REPLACE FUNCTION ticket_board.producing_holder(p_stage text, p_role text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s,
                               jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
                    WHERE s->>'name' = p_stage AND s->>'kind' <> 'review' AND coalesce(s->'owners' ? p_role, false)
                      AND r->>'name' = p_role AND r->>'kind' IN ('implementer', 'draft'));
$$;

-- Why work in p_audit_stage cannot be returned to p_stage and p_owner under the
-- declared workflow, or NULL: back to a live stage that is not this one, to an
-- active role that owns it.
CREATE OR REPLACE FUNCTION ticket_board.audit_return_problem(p_audit_stage text, p_stage text, p_owner text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); s jsonb; r jsonb;
BEGIN
    SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = p_stage;
    IF s IS NULL THEN
        RETURN format('the workflow does not declare a stage %s', coalesce(nullif(p_stage, ''), '<none>'));
    END IF;
    IF p_stage = p_audit_stage OR coalesce((s->>'terminal')::boolean, false) OR ticket_board.declared_parking_stage(p_stage) THEN
        RETURN format('%s is not a stage work is returned to: it is the stage being left, a parking stage or a closed one',
                      p_stage);
    END IF;
    SELECT x INTO r FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name' = p_owner;
    IF r IS NULL OR NOT coalesce((r->>'active')::boolean, false) THEN
        RETURN format('%s is not an active role in the workflow', coalesce(nullif(p_owner, ''), '<nobody>'));
    END IF;
    IF NOT coalesce(s->'owners' ? p_owner, false) THEN
        RETURN format('%s does not own %s (owners: %s)', p_owner, p_stage,
                      coalesce((SELECT string_agg(o, ', ') FROM jsonb_array_elements_text(s->'owners') o), 'none'));
    END IF;
    RETURN NULL;
END;
$$;

-- Every change of stage or revision. A new revision carried by its holder's own
-- declared transition records that holder as its producer; a new revision made
-- any other way records that nobody known produced it, even when the same
-- update also leaves a producing stage (a Director's edit that changes the
-- revision and moves the ticket is the Director's revision). Leaving a stage its
-- holder produces work in, with the revision unchanged, records that holder.
-- Clearing the revision (a return) records nothing. An entry into the stage
-- Audit's kick-back leaves records where it came from.
CREATE OR REPLACE FUNCTION ticket_board.record_audit_return()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    action_name text := nullif(current_setting('ticket_board.workflow_action', true), '');
    how text;
    revised boolean;
    own boolean;
    moved boolean := OLD.state IS DISTINCT FROM NEW.state;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        RETURN NULL;
    END IF;
    revised := (NEW.commit_hash IS DISTINCT FROM OLD.commit_hash OR NEW.commit_exempt IS DISTINCT FROM OLD.commit_exempt)
               AND (btrim(coalesce(NEW.commit_hash, '')) <> '' OR NEW.commit_exempt);
    IF NOT moved AND NOT revised THEN
        RETURN NULL;
    END IF;
    own := action_name IS NOT NULL AND nullif(current_setting('ticket_board.workflow_actor', true), '') = OLD.assignee;
    how := coalesce(action_name,
                    CASE WHEN current_setting('ticket_board.force_move', true) = 'on' THEN 'force_move'
                         WHEN nullif(current_setting('ticket_board.director_edit_target', true), '') = NEW.id THEN 'director_edit'
                         WHEN nullif(current_setting('ticket_board.merge_source', true), '') = NEW.id THEN 'merge'
                         WHEN nullif(current_setting('ticket_board.router_ticket', true), '') = NEW.id THEN 'router'
                         ELSE 'update' END);
    IF (revised AND own) OR (NOT revised AND moved AND ticket_board.producing_holder(OLD.state, OLD.assignee)) THEN
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action,
                                                       producer_at, producer_commit, producer_exempt)
        VALUES (NEW.id, OLD.state, OLD.assignee, how, clock_timestamp(), coalesce(NEW.commit_hash, ''), NEW.commit_exempt)
        ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = EXCLUDED.producer_stage,
            producer_assignee = EXCLUDED.producer_assignee, producer_action = EXCLUDED.producer_action,
            producer_at = EXCLUDED.producer_at, producer_commit = EXCLUDED.producer_commit,
            producer_exempt = EXCLUDED.producer_exempt, recorded_by = '', reason = '';
    ELSIF revised THEN
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action,
                                                       producer_at, producer_commit, producer_exempt)
        VALUES (NEW.id, NULL, NULL, how, clock_timestamp(), coalesce(NEW.commit_hash, ''), NEW.commit_exempt)
        ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = NULL, producer_assignee = NULL,
            producer_action = EXCLUDED.producer_action, producer_at = EXCLUDED.producer_at,
            producer_commit = EXCLUDED.producer_commit, producer_exempt = EXCLUDED.producer_exempt,
            recorded_by = '', reason = '';
    END IF;
    IF moved AND ticket_board.audit_return_stage(NEW.state) THEN
        -- A ticket whose producer the board never saw is entered with none.
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_action, producer_at, producer_commit,
                                                       producer_exempt, audit_stage, entered_from_stage,
                                                       entered_from_assignee, entry_action, entered_at, entry_commit,
                                                       entry_exempt)
        VALUES (NEW.id, 'unrecorded', clock_timestamp(), '', false, NEW.state, OLD.state, OLD.assignee, how,
                clock_timestamp(), coalesce(NEW.commit_hash, ''), NEW.commit_exempt)
        ON CONFLICT (ticket_id) DO UPDATE SET audit_stage = EXCLUDED.audit_stage,
            entered_from_stage = EXCLUDED.entered_from_stage, entered_from_assignee = EXCLUDED.entered_from_assignee,
            entry_action = EXCLUDED.entry_action, entered_at = EXCLUDED.entered_at,
            entry_commit = EXCLUDED.entry_commit, entry_exempt = EXCLUDED.entry_exempt;
    ELSIF moved THEN
        UPDATE ticket_board.ticket_audit_returns SET audit_stage = NULL, entered_from_stage = NULL,
            entered_from_assignee = NULL, entry_action = NULL, entered_at = NULL, entry_commit = NULL, entry_exempt = NULL
         WHERE ticket_id = NEW.id AND audit_stage IS NOT NULL;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_audit_return ON ticket_board.tickets;
CREATE TRIGGER tickets_z_audit_return AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.record_audit_return();

-- Where Audit's kick-back would return a ticket, for its JSON and for the
-- executor; NULL outside a stage that kick-back leaves. Valid only when the
-- board knows who produced the revision the ticket carries now, and the
-- workflow still admits returning work there; otherwise it says what is wrong
-- and the repair.
CREATE OR REPLACE FUNCTION ticket_board.audit_return_status(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets;
    r ticket_board.ticket_audit_returns;
    revision text;
    problem text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    IF NOT FOUND OR NOT ticket_board.audit_return_stage(t.state) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = p_ticket;
    revision := coalesce(nullif(t.commit_hash, ''), CASE WHEN t.commit_exempt THEN 'no code' ELSE 'none' END);
    problem := CASE
        WHEN r.producer_action IS DISTINCT FROM 'unrecorded' AND r.producer_commit = coalesce(t.commit_hash, '')
             AND r.producer_exempt = t.commit_exempt AND r.producer_stage IS NULL
        THEN format('its revision (%s) was made by %s, not by the holder of a stage that produces work', revision,
                    r.producer_action)
        WHEN r.producer_stage IS NULL OR r.producer_commit IS DISTINCT FROM coalesce(t.commit_hash, '')
             OR r.producer_exempt IS DISTINCT FROM t.commit_exempt
        THEN format('the board has no record of who produced the revision it carries (%s)', revision)
        ELSE ticket_board.audit_return_problem(t.state, r.producer_stage, r.producer_assignee) END;
    RETURN jsonb_build_object(
        'valid', problem IS NULL, 'stage', r.producer_stage, 'assignee', r.producer_assignee,
        'action', r.producer_action, 'since', ticket_board.utc_text(r.producer_at), 'commit', r.producer_commit,
        'recorded_by', nullif(r.recorded_by, ''), 'reason', nullif(r.reason, ''),
        'entered_from', CASE WHEN r.audit_stage = t.state THEN jsonb_build_object(
            'stage', r.entered_from_stage, 'assignee', r.entered_from_assignee, 'action', r.entry_action,
            'at', ticket_board.utc_text(r.entered_at), 'commit', r.entry_commit) END,
        'problem', problem,
        'repair', CASE WHEN problem IS NOT NULL THEN format(
            'the Director records where it returns (ticket-board-write set-audit-return %s --stage STAGE --assignee ROLE '
            '--reason TEXT), and Audit then kicks it back as usual', p_ticket) END);
END;
$$;

-- The Director's record of where Audit's kick-back returns a ticket in that
-- stage: for a revision with no known producer, or to send Audit's findings
-- somewhere else. It moves and reassigns nothing, and holds only for the
-- revision the ticket carries now.
CREATE OR REPLACE FUNCTION ticket_board.set_audit_return(p_ticket text, p_stage text, p_assignee text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    caller text := ticket_board.current_app_actor();
    reason text := btrim(coalesce(p_reason, ''));
    stage text := lower(btrim(coalesce(p_stage, '')));
    owner text := lower(btrim(coalesce(p_assignee, '')));
    t ticket_board.tickets;
    problem text;
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service'
       OR NOT coalesce(ticket_board.role_controls_project(caller), false) THEN
        RAISE EXCEPTION 'only the control role may record where a kick-back returns a ticket, not %', caller
            USING ERRCODE = '42501';
    END IF;
    IF reason = '' THEN
        RAISE EXCEPTION 'recording where a kick-back returns a ticket requires a reason';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % not found', p_ticket;
    END IF;
    IF NOT ticket_board.audit_return_stage(t.state) THEN
        RAISE EXCEPTION '% is in %, which Audit''s kick-back does not leave; its return is recorded while it is there',
            t.id, t.state;
    END IF;
    problem := ticket_board.audit_return_problem(t.state, stage, owner);
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'set-audit-return refused for %: %', t.id, problem;
    END IF;
    INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action, producer_at,
                                                   producer_commit, producer_exempt, recorded_by, reason)
    VALUES (t.id, stage, owner, 'set_audit_return', clock_timestamp(), coalesce(t.commit_hash, ''), t.commit_exempt,
            caller, reason)
    ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = EXCLUDED.producer_stage,
        producer_assignee = EXCLUDED.producer_assignee, producer_action = EXCLUDED.producer_action,
        producer_at = EXCLUDED.producer_at, producer_commit = EXCLUDED.producer_commit,
        producer_exempt = EXCLUDED.producer_exempt, recorded_by = EXCLUDED.recorded_by, reason = EXCLUDED.reason;
    PERFORM ticket_board.append_ticket_comment(t.id, caller,
        format('Audit''s kick-back now returns this ticket to %s/%s: %s', stage, owner, reason));
    RETURN ticket_board.audit_return_status(t.id);
END;
$$;

REVOKE ALL ON ticket_board.ticket_audit_returns FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.audit_return_problem(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_audit_return() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.set_audit_return(text, text, text, text) FROM PUBLIC;
-- audit_return_status is closed and granted in rbac.sql, like user_wait_status:
-- the ticket read only probes that it exists.

-- The executor: an audit_kick_back lands on the recorded origin (identical to schema.sql).
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

-- Tickets already where Audit's kick-back leaves have no record of where their
-- review began. Nothing is moved: the Director is told once each.
DO $$
DECLARE rec record; status jsonb;
BEGIN
    FOR rec IN SELECT t.id, t.title, t.state, t.assignee FROM ticket_board.tickets t
                WHERE ticket_board.audit_return_stage(t.state) ORDER BY t.ticket_number LOOP
        status := ticket_board.audit_return_status(rec.id);
        IF NOT coalesce((status->>'valid')::boolean, true) THEN
            PERFORM ticket_board.enqueue_notification(
                rec.id, 'ticket_update', 'director',
                format('%s -- %s is in %s (%s), and Audit''s kick-back cannot return it: %s. Nothing was moved. '
                       'Repair: %s.', rec.id, rec.title, rec.state, rec.assignee, status->>'problem', status->>'repair'),
                jsonb_build_object('kind', 'audit_return_missing', 'id', rec.id, 'state', rec.state,
                                   'assignee', rec.assignee, 'target_role', 'director'),
                format('audit_return_missing:%s', rec.id));
        END IF;
    END LOOP;
END $$;

COMMIT;
