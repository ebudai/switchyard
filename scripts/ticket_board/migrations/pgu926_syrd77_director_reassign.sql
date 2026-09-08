-- SYRD-77: the director could not perform an ordinary same-stage
-- reassignment. `route` has no same-stage form and is not a granted
-- capability, `edit_fields` refuses assignee, and every override was rejected
-- by a different authority layer. This adds one narrow operation that changes
-- a ticket's owner and nothing else: no stage advance or return, no gate, no
-- sign-off. Serial focus is still hard, so reassigning implementation work to
-- an implementer who already holds reserved work redirects it to the declared
-- holding stage and records why, instead of quietly giving them two active
-- tickets.
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
    IF coalesce(ticket_row.manually_controlled, false) OR ticket_board.ticket_has_unresolved_blockers(id) THEN
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

GRANT EXECUTE ON FUNCTION ticket_board.reassign(text, text, text) TO ticket_board_service;

-- The same-stage branch of the configured update guard never ran the
-- serial-focus redirect, because that lived only on the transition path. An
-- owner change is now checked against the reservation wherever it comes from.
CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true); actor text:=ticket_board.current_app_actor(); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
    queued_for text; reserved_by text;
BEGIN
    IF actor IS NULL THEN RAISE EXCEPTION 'configured ticket writes require a registered actor' USING ERRCODE='42501'; END IF;
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
        target:=previous.state; proposed.assignee:=previous.assignee;
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
    RETURN proposed;
END;
$$;

-- `reassign` joins the granted capabilities a tenant document may name.
CREATE OR REPLACE FUNCTION ticket_board.validate_declared_workflow(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r jsonb; s jsonb; tr jsonb; f record; cursor_name text; visited text[];
    names text[]; role_names text[]; active_roles text[]; flag_names text[];
BEGIN
    IF cfg->>'schema' IS DISTINCT FROM 'switchyard.workflow.v1'
       OR jsonb_typeof(cfg->'roles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'stages') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'transitions') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'flags') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid workflow document';
    END IF;
    SELECT array_agg(x->>'name') INTO names FROM jsonb_array_elements(cfg->'stages') x;
    SELECT array_agg(x->>'name'), coalesce(array_agg(x->>'name') FILTER (WHERE (x->>'active')::boolean),ARRAY[]::text[])
      INTO role_names, active_roles FROM jsonb_array_elements(cfg->'roles') x;
    SELECT array_agg(key) INTO flag_names FROM jsonb_each(cfg->'flags');
    IF cardinality(names) IS NULL OR cardinality(role_names) IS NULL
       OR cardinality(names) <> (SELECT count(DISTINCT n) FROM unnest(names) n)
       OR cardinality(role_names) <> (SELECT count(DISTINCT n) FROM unnest(role_names) n)
       OR NOT ARRAY['director','user','unassigned']::text[] <@ role_names
       OR NOT coalesce('director' = ANY(active_roles),false) THEN RAISE EXCEPTION 'missing or duplicate identities'; END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        IF jsonb_typeof(r->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(r->'capabilities') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'role fields must have explicit types'; END IF;
        IF NOT r ?& ARRAY['name','label','kind','active','capabilities']
           OR (r->>'name') !~ '^[a-z][a-z0-9_-]{0,63}$' OR btrim(r->>'label') = ''
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign')) THEN
            RAISE EXCEPTION 'invalid role policy: %', r->>'name';
        END IF;
        IF (r->>'runtime' IS NULL) <> (r->>'target' IS NULL)
           OR (r->>'runtime' IS NOT NULL AND (r->>'runtime' NOT IN ('claude','codex','agy','hermes')
               OR r->>'target' !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$')) THEN
            RAISE EXCEPTION 'invalid runtime/target'; END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'slot' FROM jsonb_array_elements(cfg->'roles') x
       WHERE x->>'slot' IS NOT NULL GROUP BY x->>'slot' HAVING count(*) > 1)
       OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'slot' IS NOT NULL
          AND ((x->>'slot')::int NOT BETWEEN 0 AND 5 OR NOT (x->>'active')::boolean OR x->>'target' IS NULL)) THEN
        RAISE EXCEPTION 'invalid or duplicate visible slot'; END IF;
    FOR f IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF EXISTS (SELECT FROM pg_attribute WHERE attrelid='ticket_board.tickets'::regclass AND attname=f.key AND NOT attisdropped) AND f.key NOT IN ('needs_inspection','needs_audit','needs_user_signoff','inspector_signoff','audit_signoff','user_signoff') THEN RAISE EXCEPTION 'flag collides with protected ticket field'; END IF;
        IF jsonb_typeof(f.value->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'flag kind must be explicit'; END IF;
        IF f.key IN ('needs_inspection','needs_audit','needs_user_signoff') AND f.value->>'kind'<>'gate' OR f.key IN ('inspector_signoff','audit_signoff','user_signoff') AND f.value->>'kind'<>'signoff' THEN RAISE EXCEPTION 'cannot change builtin flag kind'; END IF;
        IF f.key !~ '^[a-z][a-z0-9_]{0,63}$' OR f.value->>'kind' NOT IN ('gate','signoff')
           OR jsonb_typeof(f.value->'default') IS DISTINCT FROM 'boolean'
           OR (f.value->>'kind' = 'signoff' AND (f.value->>'default')::boolean) THEN
            RAISE EXCEPTION 'invalid flag policy'; END IF;
    END LOOP;
    IF cfg->'queue' IS NOT NULL AND cfg->'queue'<>'null'::jsonb AND NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cfg->'queue'->>'stage'
        AND x->>'kind' IN ('draft','system') AND x->>'gate' IS NULL AND NOT (x->>'terminal')::boolean
        AND (jsonb_array_length(x->'owners')=0 OR x->'owners' ? (cfg->'queue'->>'assignee'))
        AND (cfg->'queue'->>'assignee'='unassigned' OR cfg->'queue'->>'assignee'=ANY(active_roles))) THEN RAISE EXCEPTION 'invalid configured holding destination'; END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind'='implementation') <> 1 THEN
        RAISE EXCEPTION 'exactly one implementation stage required'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF jsonb_typeof(s->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'terminal') IS DISTINCT FROM 'boolean' OR jsonb_typeof(s->'owners') IS DISTINCT FROM 'array' OR jsonb_typeof(s->'notify') IS DISTINCT FROM 'object' OR jsonb_typeof(s->'notify'->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'stage fields must have explicit types'; END IF;
        IF NOT s ?& ARRAY['name','label','owners','kind','gate','skip_to','signoff','terminal','notify']
           OR s->>'name' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(s->>'label') = ''
           OR s->>'kind' NOT IN ('draft','implementation','review','system')
           OR jsonb_typeof(s->'terminal') <> 'boolean' OR jsonb_typeof(s->'owners') <> 'array'
           OR NOT ARRAY(SELECT jsonb_array_elements_text(s->'owners')) <@ active_roles THEN
            RAISE EXCEPTION 'invalid stage/owner policy: %', s->>'name'; END IF;
        IF s->>'gate' IS NOT NULL AND (NOT coalesce(s->>'gate'=ANY(flag_names),false) OR cfg->'flags'->(s->>'gate')->>'kind' <> 'gate')
           OR s->>'signoff' IS NOT NULL AND (NOT coalesce(s->>'signoff'=ANY(flag_names),false) OR cfg->'flags'->(s->>'signoff')->>'kind' <> 'signoff')
           OR (s->>'gate' IS NULL) <> (s->>'skip_to' IS NULL) THEN
            RAISE EXCEPTION 'invalid gate/signoff'; END IF;
        IF s->>'kind'='implementation' AND (jsonb_array_length(s->'owners')=0 OR EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE (s->'owners') ? (x->>'name') AND x->>'kind'<>'implementer')) THEN
            RAISE EXCEPTION 'implementation owner must be implementer'; END IF;
        IF NOT (s->'notify') ?& ARRAY['kind','role'] OR s->'notify'->>'kind' NOT IN ('none','assignee','fixed_role','stage_owner_fallback')
           OR (s->'notify'->>'kind'='fixed_role' AND NOT coalesce(s->'notify'->>'role'=ANY(active_roles),false))
           OR (s->'notify'->>'kind'<>'fixed_role' AND s->'notify'->>'role' IS NOT NULL) THEN
            RAISE EXCEPTION 'notification routing or silence must be explicit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'target' IS NULL AND
          ((s->'notify'->>'kind'='fixed_role' AND x->>'name'=s->'notify'->>'role') OR
           (s->'notify'->>'kind' IN ('assignee','stage_owner_fallback') AND s->'owners' ? (x->>'name')))) THEN
            RAISE EXCEPTION 'notifying roles require a pane target'; END IF;
        visited := ARRAY[]::text[]; cursor_name := s->>'name';
        LOOP
            IF cursor_name=ANY(visited) OR NOT cursor_name=ANY(names) THEN RAISE EXCEPTION 'gate skip cycle or missing stage'; END IF;
            visited := visited || cursor_name;
            SELECT x->>'skip_to' INTO cursor_name FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cursor_name;
            EXIT WHEN cursor_name IS NULL;
        END LOOP;
    END LOOP;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        IF EXISTS (SELECT FROM unnest(ARRAY['from','to','action','label','primitive']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'string') OR EXISTS (SELECT FROM unnest(ARRAY['owner_scoped','require_commit','require_reason','allow_no_code']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'boolean') OR jsonb_typeof(tr->'clear_signoffs') IS DISTINCT FROM 'array' OR jsonb_typeof(tr->'actors') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'transition fields must have explicit types'; END IF;
        IF NOT tr ?& ARRAY['from','to','action','label','primitive','actors','owner_scoped','require_commit','require_reason','clear_signoffs']
           OR NOT tr->>'from'=ANY(names) OR NOT tr->>'to'=ANY(names) OR tr->>'from'=tr->>'to'
           OR tr->>'action' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(tr->>'label')=''
           OR tr->>'primitive' NOT IN ('move','approve','return','reopen')
           OR jsonb_typeof(tr->'actors') <> 'array' OR jsonb_array_length(tr->'actors')=0
           OR NOT ARRAY(SELECT jsonb_array_elements_text(tr->'actors')) <@ active_roles
           OR jsonb_typeof(tr->'owner_scoped') <> 'boolean'
           OR jsonb_typeof(tr->'require_commit') <> 'boolean'
           OR jsonb_typeof(tr->'require_reason') <> 'boolean' THEN RAISE EXCEPTION 'invalid transition policy'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from';
        IF (s->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
        IF tr->>'primitive'='approve' AND s->>'signoff' IS NULL THEN RAISE EXCEPTION 'approval requires signoff'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'to';
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation' THEN RAISE EXCEPTION 'return must target implementation'; END IF;
        IF (tr->>'allow_no_code')::boolean AND ((tr->>'require_commit')::boolean OR NOT (tr->>'require_reason')::boolean OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='implementation')) THEN RAISE EXCEPTION 'invalid no-code transition policy'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements_text(tr->'clear_signoffs') x
            WHERE cfg->'flags'->x->>'kind' IS DISTINCT FROM 'signoff') THEN RAISE EXCEPTION 'invalid signoff reset'; END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'from',x->>'to',x->>'action' FROM jsonb_array_elements(cfg->'transitions') x
        GROUP BY 1,2,3 HAVING count(*)>1) THEN RAISE EXCEPTION 'duplicate transition'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF NOT (s->>'terminal')::boolean THEN
            IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=s->>'name')
              OR (s->>'kind'<>'draft' AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'to'=s->>'name')) THEN
                RAISE EXCEPTION 'stage lacks entry or exit: %', s->>'name'; END IF;
            IF NOT EXISTS (
                WITH RECURSIVE reachable(n) AS (
                    SELECT s->>'name' UNION
                    SELECT x->>'to' FROM reachable q, jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=q.n
                ) SELECT FROM reachable q, jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=q.n AND (x->>'terminal')::boolean
            ) THEN RAISE EXCEPTION 'stage cannot reach a terminal: %', s->>'name'; END IF;
        END IF;
    END LOOP;
END;
$$;

-- One-time grant of the new capability to the tenant's director. Marked in the
-- document so a director who later removes it has made a decision the next
-- upgrade must not quietly undo. The projected role rows carry the same
-- definition, because that projection -- not the document -- is what the
-- database checks on every call.
DO $$
DECLARE cfg jsonb; roles jsonb; rev bigint; r jsonb;
BEGIN
    IF to_regclass('ticket_board.workflow_configuration') IS NULL THEN RETURN; END IF;
    SELECT document INTO cfg FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    IF coalesce((cfg->'migrations'->>'director_reassign_capability')::boolean, false) THEN RETURN; END IF;
    SELECT jsonb_agg(
        CASE
            WHEN entry.value->>'name' = 'director' AND NOT (entry.value->'capabilities' ? 'reassign')
                THEN jsonb_set(entry.value, '{capabilities}', (entry.value->'capabilities') || '["reassign"]'::jsonb)
            ELSE entry.value
        END
        ORDER BY entry.ordinality)
    INTO roles
    FROM jsonb_array_elements(cfg->'roles') WITH ORDINALITY AS entry(value, ordinality);
    cfg := jsonb_set(cfg, '{roles}', roles);
    cfg := jsonb_set(
        cfg,
        '{migrations}',
        coalesce(cfg->'migrations', '{}'::jsonb) || jsonb_build_object('director_reassign_capability', true));
    PERFORM ticket_board.validate_declared_workflow(cfg);
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('migration', cfg) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    FOR r IN SELECT value FROM jsonb_array_elements(roles) LOOP
        UPDATE ticket_board.workflow_roles SET definition = r WHERE name = r->>'name';
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $$;
