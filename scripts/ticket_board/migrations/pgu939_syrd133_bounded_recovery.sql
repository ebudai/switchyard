-- SYRD-133 (second half): the recovery the Director skill documents, made real
-- and made bounded.
--
-- The first half of this ticket gave a role one action for a dependency and
-- stopped the idle escalation repeating. This is what the Director asked for
-- after reviewing it: when the owner answers that escalation with "it is done"
-- and still does not transition, there has to be a way to move the work that
-- does not require narrating an override.
--
-- There was not one. The owner's own no-code submission is `owner_scoped`, so
-- the Director cannot take it for them, and `force_move` and `override_move`
-- are capabilities this tenant's document does not grant -- both were refused
-- outright on SYRD-131. The skill advertised controls that did not exist.
--
-- `recover_stalled_ticket` is the bounded replacement, and every limit is
-- deliberate: it performs a DECLARED transition the owner could have taken, so
-- gates, sign-offs and blockers are enforced by the executor rather than
-- reimplemented; exactly one such transition must exist, because choosing
-- between two would be the board deciding what the owner meant; it requires a
-- reason, recorded in the control role's own name rather than the owner's; and
-- it advances to that transition's destination and no further, so the work
-- stops at its next required gate and that gate's owner is notified once.
--
-- The escalation marker is re-keyed here too. Keying it on the moment the
-- owner's pane went idle was wrong: the listener recomputes that value on every
-- pass, so any jitter in it reads as a new stall and the escalation repeats --
-- the very defect the marker exists to stop. What makes a stall a different
-- stall is that something happened on the ticket since the last one.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.director_control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'reassign', 'set_manually_controlled', 'set_blockers',
        'merge', 'edit_fields', 'dismiss_notification', 'director_edit',
        'resolve_publication',
        -- The control role has to be able to unstick work its owner left
        -- behind. Without it the documented recovery is a control the document
        -- does not grant, which is what SYRD-131 ran into (SYRD-133).
        'recover_stalled_ticket'
    ]::text[];
$$;

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
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
        WHERE x->>'name'='director'
          AND x->'capabilities' ?& ticket_board.director_control_capabilities()) THEN
        RAISE EXCEPTION 'director must keep its control capabilities: %',
            (SELECT string_agg(c, ', ' ORDER BY c) FROM unnest(ticket_board.director_control_capabilities()) c
             WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
                 WHERE x->>'name'='director' AND x->'capabilities' ? c));
    END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        IF jsonb_typeof(r->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(r->'capabilities') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'role fields must have explicit types'; END IF;
        IF NOT r ?& ARRAY['name','label','kind','active','capabilities']
           OR (r->>'name') !~ '^[a-z][a-z0-9_-]{0,63}$' OR btrim(r->>'label') = ''
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign','director_edit','request_publication','resolve_publication',
                'recover_stalled_ticket')) THEN
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
    -- Queue, defer, cancel and reopen are transitions, and their names belong to
    -- the tenant, so the floor is the shape they must leave behind rather than a
    -- list of actions to look for: a director that cannot leave a stage has lost
    -- control of every ticket sitting in it, whatever the document calls the
    -- move (SYRD-82).
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to move work out of every stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director'));
    END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to reopen every terminal stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director'));
    END IF;
    -- The half of the floor that is a prohibition. An `approve` transition is
    -- what writes its source stage's sign-off flag, so listing the director
    -- among its actors is how a document would hand the controller the power to
    -- approve the work it directs.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
        WHERE x->>'primitive'='approve' AND x->'actors' ? 'director') THEN
        RAISE EXCEPTION 'director must not be granted sign-off authority: %',
            (SELECT string_agg(x->>'action', ', ' ORDER BY x->>'action')
             FROM jsonb_array_elements(cfg->'transitions') x
             WHERE x->>'primitive'='approve' AND x->'actors' ? 'director');
    END IF;

    -- Somewhere to put work down, and a way to pick it back up. `cancel` leaves
    -- every stage, so a floor that only asked whether the director could leave
    -- was satisfied by a document that could only end work, never park it
    -- (SYRD-92).
    IF NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean
          AND jsonb_array_length(coalesce(x->'owners','[]'::jsonb))=0
          AND x->'notify'->>'kind'='none'
    ) THEN
        RAISE EXCEPTION 'workflow must keep a stage where deferred work can wait';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') held
        WHERE NOT (held->>'terminal')::boolean
          AND NOT (jsonb_array_length(coalesce(held->'owners','[]'::jsonb))=0
                   AND held->'notify'->>'kind'='none')
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=held->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))=0
                AND dest->'notify'->>'kind'='none')
    ) THEN
        RAISE EXCEPTION 'director must be able to defer work out of every active stage';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') parked
        WHERE NOT (parked->>'terminal')::boolean
          AND jsonb_array_length(coalesce(parked->'owners','[]'::jsonb))=0
          AND parked->'notify'->>'kind'='none'
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=parked->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))>0)
    ) THEN
        RAISE EXCEPTION 'deferred work must have an ordinary way back';
    END IF;
END;
$$;

-- pgu938 keyed this on p_idle_since, which the listener recomputes every pass,
-- so a jittery idle_since re-armed an escalation that had already been sent.
-- Re-keying on last activity renames the parameter, and CREATE OR REPLACE
-- cannot rename an input parameter, so the old signature has to go first.
DROP FUNCTION IF EXISTS ticket_board.idle_escalation_identity(text, text, text, timestamptz);

CREATE OR REPLACE FUNCTION ticket_board.idle_escalation_identity(
    p_ticket text,
    p_state text,
    p_assignee text,
    p_last_activity timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_ticket, '') || ':' || coalesce(p_state, '') || ':'
        || coalesce(p_assignee, '') || ':'
        || coalesce(extract(epoch FROM p_last_activity)::bigint::text, '');
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
    IF btrim(coalesce(payload->>'text',payload->>'reason',''))<>'' THEN
        PERFORM ticket_board.append_ticket_comment(t.id,coalesce(p_narrator,actor),coalesce(payload->>'text',payload->>'reason'));
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

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action(id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
BEGIN
    PERFORM ticket_board.perform_workflow_action_as(
        ticket_board.current_app_actor(), NULL, id, action, payload);
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

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_turn_end_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_turn_end_nudges');

    IF p_idle_since_by_role IS NULL OR p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.target_role)
            candidates.id,
            candidates.state,
            candidates.assignee,
            candidates.kind,
            candidates.owner_role,
            candidates.target_role,
            candidates.idle_reminder_count,
            candidates.idle_since_at
        FROM (
            SELECT
                notification_scope.id,
                notification_scope.state,
                notification_scope.assignee,
                notification_scope.ticket_number,
                notification_scope.entered_current_state_at,
                notification_scope.idle_reminder_count,
                notification_scope.owner_role,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'escalation'
                    ELSE 'idle_reminder'
                END AS kind,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'director'
                    ELSE notification_scope.owner_role
                END AS target_role,
                notification_scope.idle_since_at,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN -1
                    WHEN notification_scope.state = 'analysis' THEN 0
                    WHEN notification_scope.state = 'inspection' THEN 2
                    WHEN notification_scope.state = 'in_progress' THEN 3
                    WHEN notification_scope.state = 'audit' THEN 4
                    WHEN notification_scope.state = 'dat' THEN 5
                    WHEN notification_scope.state = 'director_review' THEN 5
                    ELSE 9
                END AS priority,
                row_number() OVER (
                    PARTITION BY notification_scope.active_work_partition
                    ORDER BY sent.last_sent_at DESC NULLS LAST,
                        notification_scope.ticket_number DESC
                ) AS in_progress_rank
            FROM (
                SELECT
                    t.id,
                    t.state,
                    t.assignee,
                    t.ticket_number,
                    ns.entered_current_state_at,
                    ns.idle_reminder_count,
                    ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
                    CASE
                        WHEN t.state = 'in_progress'
                            THEN t.state || ':' || ticket_board.transition_target_role(t.state, t.assignee)
                        ELSE NULL
                    END AS active_work_partition,
                    (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at
                FROM ticket_board.tickets t
                JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
                WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
                  AND NOT t.manually_controlled
                  AND ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
                  AND p_idle_since_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
                  AND greatest(
                        ns.entered_current_state_at,
                        (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                      ) <= p_now
                  -- await-role means the owner has explicitly handed this off.
                  -- Do not tell that same owner they may be stuck while the wait is active.
                  AND NOT ticket_board.ticket_awaiting_role_is_active(
                      ns.awaiting_role,
                      ns.awaiting_since_at,
                      p_now
                  )
                  -- A durable capacity wait is not unattended work. While the
                  -- reservation this ticket names still holds that implementer, the
                  -- director cannot legally route it into that lane, so "advance it
                  -- or hand it off" asks for something the board itself refuses. The
                  -- backlog guard beside this one answers the same question for a
                  -- ticket whose own assignee is the reserved implementer; a
                  -- declarative queue destination is usually analysis/director, so
                  -- only the durable fields know what it is waiting for (SYRD-109).
                  AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                      t.id,
                      t.queued_for_assignee,
                      t.queued_behind_ticket
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_blockers tb
                      LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                      WHERE tb.ticket_id = t.id
                        AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_comments c
                      WHERE c.ticket_id = t.id
                        AND c.who = ticket_board.transition_target_role(t.state, t.assignee)
                        AND c.ts IS NOT NULL
                        AND c.ts >= ns.entered_current_state_at
                        AND c.ts >= ns.last_activity_at
                        AND c.ts >= (
                            (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                            - interval '5 minutes'
                        )
                        AND c.ts <= p_now
                  )
            ) AS notification_scope
            LEFT JOIN LATERAL (
                SELECT max(trace.ts) AS last_sent_at
                FROM ticket_board.notification_trace trace
                WHERE trace.ticket_id = notification_scope.id
                  AND trace.target_role = notification_scope.owner_role
                  AND trace.kind = 'transition'
                  AND trace.event = 'send'
                  AND trace.ticket_state_at_event = notification_scope.state
            ) sent ON true
            WHERE greatest(
                    notification_scope.entered_current_state_at,
                    notification_scope.idle_since_at
                  ) <= p_now
              AND notification_scope.owner_role IS NOT NULL
        ) AS candidates
        WHERE (candidates.state <> 'in_progress' OR candidates.in_progress_rank = 1)
          AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidates.id
              AND q.target_role = candidates.target_role
              AND q.kind NOT IN ('idle_reminder', 'escalation')
        )
          -- One escalation per stall, not one per wave. The dedupe key holds
          -- only while the queue row does; once the director acknowledges it,
          -- the next wave finds the same idle owner and the same unmoved
          -- ticket and enqueues it again -- which on SYRD-131 it did, within a
          -- minute of delivering the first. This marker outlives the
          -- acknowledgement and re-arms when the stall's identity changes
          -- (SYRD-133).
          AND (
            candidates.kind <> 'escalation'
            OR NOT EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state escalated
                WHERE escalated.ticket_id = candidates.id
                  AND escalated.idle_escalation_key = ticket_board.idle_escalation_identity(
                      candidates.id,
                      candidates.state,
                      candidates.assignee,
                      escalated.last_activity_at
                  )
            )
        )
        ORDER BY candidates.target_role, candidates.priority, candidates.ticket_number
    LOOP
        payload := jsonb_build_object(
            'kind', candidate.kind,
            'id', candidate.id,
            'state', candidate.state,
            'assignee', candidate.assignee,
            'owner_role', candidate.owner_role,
            'target_role', candidate.target_role,
            'message', CASE
                WHEN candidate.kind = 'escalation' THEN ticket_board.idle_without_advancing_escalation_message(
                    candidate.owner_role,
                    candidate.id,
                    candidate.state
                )
                WHEN candidate.owner_role = 'director' AND candidate.idle_reminder_count >= 1 THEN ticket_board.idle_without_advancing_problem_message(
                    candidate.id,
                    candidate.state,
                    'User'
                )
                WHEN candidate.owner_role = 'director' THEN ticket_board.idle_without_advancing_director_message(
                    candidate.id,
                    candidate.state
                )
                ELSE ticket_board.idle_without_advancing_message(candidate.id, candidate.state)
            END,
            'idle_since', candidate.idle_since_at
        );

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            candidate.kind,
            candidate.target_role,
            payload ->> 'message',
            payload,
            CASE
                WHEN candidate.kind = 'escalation' THEN
                    'idle-reminder-escalation:' || candidate.id || ':' || candidate.owner_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
                ELSE
                    'idle-reminder:' || candidate.id || ':' || candidate.target_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
            END
        );
        IF candidate.kind = 'escalation' THEN
            -- Claimed in the same transaction that enqueues it, so two waves
            -- racing cannot both decide they are the first.
            UPDATE ticket_board.ticket_notification_state
               SET idle_escalation_key = ticket_board.idle_escalation_identity(
                       candidate.id, candidate.state, candidate.assignee,
                       ticket_notification_state.last_activity_at
                   )
             WHERE ticket_id = candidate.id;
        END IF;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

-- Reachable only from SECURITY DEFINER functions in this schema, each of which
-- establishes who the caller is before it decides whose transition to take.
REVOKE ALL ON FUNCTION ticket_board.perform_workflow_action_as(text, text, text, text, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION ticket_board.recover_stalled_ticket(text, text) TO ticket_board_service;

DO $grants$
DECLARE writer text;
BEGIN
    FOR writer IN
        SELECT rolname FROM pg_roles
        WHERE NOT rolsuper
          AND has_function_privilege(
                  rolname, 'ticket_board.force_move(text, text, text, boolean)', 'EXECUTE')
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.recover_stalled_ticket(text, text) TO %I', writer);
    END LOOP;
END $grants$;

DO $listener$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.idle_escalation_identity(text, text, text, timestamptz)
            TO ticket_board_listener;
    END IF;
END $listener$;

-- The control role must be able to run the recovery the skill documents, so the
-- capability is added to whichever role already holds control authority -- the
-- same shape SYRD-93 used for resolve_publication, and for the same reason: a
-- documented control the document does not grant is not a control.
DO $capability$
DECLARE
    cfg jsonb; rev bigint; roles jsonb; entry record; changed boolean := false;
BEGIN
    SELECT document, revision INTO cfg, rev FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    roles := cfg->'roles';
    FOR entry IN SELECT * FROM jsonb_array_elements(roles) WITH ORDINALITY AS x(value, position) LOOP
        IF entry.value->'capabilities' ?& ticket_board.control_capabilities()
           AND NOT entry.value->'capabilities' ? 'recover_stalled_ticket' THEN
            roles := jsonb_set(roles, ARRAY[(entry.position - 1)::text, 'capabilities'],
                     (entry.value->'capabilities') || '["recover_stalled_ticket"]'::jsonb);
            changed := true;
        END IF;
    END LOOP;
    IF NOT changed THEN RETURN; END IF;
    cfg := jsonb_set(cfg, ARRAY['roles'], roles);
    rev := rev + 1;
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('pgu939', cfg);
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    UPDATE ticket_board.workflow_roles SET definition = r
      FROM jsonb_array_elements(roles) r WHERE name = r->>'name';
    PERFORM pg_notify('ticket_board_state_transition',
        jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $capability$;

COMMIT;
