-- Opt-in foundation. Merely installing this migration changes no tenant workflow.
CREATE TABLE IF NOT EXISTS ticket_board.workflow_configuration (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL,
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_board.workflow_revisions (
    revision bigserial PRIMARY KEY,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_board.workflow_roles (
    name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{0,63}$'),
    definition jsonb NOT NULL
);
ALTER TABLE ticket_board.tickets ADD COLUMN IF NOT EXISTS workflow_flags jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION ticket_board.declared_workflow()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT document FROM ticket_board.workflow_configuration WHERE singleton;
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
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        IF jsonb_typeof(r->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(r->'capabilities') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'role fields must have explicit types'; END IF;
        IF NOT r ?& ARRAY['name','label','kind','active','capabilities']
           OR (r->>'name') !~ '^[a-z][a-z0-9_-]{0,63}$' OR btrim(r->>'label') = ''
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification')) THEN
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

CREATE OR REPLACE FUNCTION ticket_board.apply_declared_workflow(cfg jsonb)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE r jsonb; s jsonb; tr jsonb; rev bigint; pos int:=0; old_cfg jsonb; item record;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'only director may configure workflow' USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    IF nullif(current_setting('ticket_board.project',true),'') IS NULL OR cfg->>'project' IS DISTINCT FROM current_setting('ticket_board.project',true) THEN
        RAISE EXCEPTION 'workflow must match selected project identity'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') runtime_role WHERE runtime_role->>'target' IS NOT NULL AND runtime_role->>'target' IS DISTINCT FROM (cfg->>'project')||'-'||(runtime_role->>'name')||':0.0') THEN
        RAISE EXCEPTION 'foreign project/role runtime target'; END IF;
    PERFORM ticket_board.validate_declared_workflow(cfg);
    LOCK TABLE ticket_board.tickets, ticket_board.workflow_stages, ticket_board.workflow_transitions IN SHARE ROW EXCLUSIVE MODE;
    old_cfg := ticket_board.declared_workflow();
    IF old_cfg IS NOT NULL AND old_cfg->>'project' IS DISTINCT FROM cfg->>'project' THEN RAISE EXCEPTION 'cannot change workflow tenant identity'; END IF;
    IF old_cfg=cfg THEN SELECT revision INTO rev FROM ticket_board.workflow_configuration; RETURN rev; END IF;
    -- Stage removal is deliberate, empty, and cannot erase outstanding review.
    IF jsonb_typeof(cfg->'remove_stages') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'explicit remove_stages list required'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws WHERE NOT EXISTS
        (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name)
        AND NOT cfg->'remove_stages' ? ws.name) THEN
        RAISE EXCEPTION 'configuration omits an existing stage; preserve it or explicitly request removal'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws CROSS JOIN ticket_board.tickets t
        JOIN ticket_board.workflow_stages current_stage ON current_stage.name=t.state
        WHERE ws.exit_signoff_field IS NOT NULL AND NOT current_stage.is_terminal
        AND (ws.entry_gate_field IS NULL OR ticket_board.workflow_flag(to_jsonb(t),ws.entry_gate_field,coalesce(old_cfg,cfg)))
        AND NOT ticket_board.workflow_flag(to_jsonb(t),ws.exit_signoff_field,coalesce(old_cfg,cfg))
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name
          AND x->>'signoff'=ws.exit_signoff_field AND x->>'gate' IS NOT DISTINCT FROM ws.entry_gate_field)) THEN
        RAISE EXCEPTION 'configuration would remove an outstanding review obligation'; END IF;
    -- Whole-graph application must carry existing stages and attribution identities.
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state)) THEN
        RAISE EXCEPTION 'configuration omits an occupied stage; preserve live/history state'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=t.assignee)) THEN
        RAISE EXCEPTION 'configuration omits an assigned role; retain it inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=r.name)) THEN
        RAISE EXCEPTION 'retain historical roles as inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_state ns
        WHERE ns.awaiting_role<>'' AND ns.awaiting_since_at>clock_timestamp()-interval '4 hours'
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') awaited WHERE awaited->>'name'=ns.awaiting_role
            AND (awaited->>'active')::boolean AND awaited->>'target' IS NOT NULL)) THEN
        RAISE EXCEPTION 'role has an unresolved handoff; clear or retarget it before retirement'; END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        INSERT INTO ticket_board.workflow_roles(name,definition) VALUES(r->>'name',r)
        ON CONFLICT(name) DO UPDATE SET definition=EXCLUDED.definition;
    END LOOP;
    -- Referenced identities are not removed, including historical comment actors.
    ALTER TABLE ticket_board.workflow_stages DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_state_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_assignee_check;
    ALTER TABLE ticket_board.ticket_notification_queue DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
    -- Empty notification-role sentinels stay supported; array and sentinel refs are validated below.
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='ticket_board.tickets'::regclass AND conname='tickets_workflow_state_fk') THEN
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_state_fk FOREIGN KEY(state) REFERENCES ticket_board.workflow_stages(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_role_fk FOREIGN KEY(assignee) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.ticket_notification_queue ADD CONSTRAINT notification_workflow_role_fk FOREIGN KEY(target_role) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    -- Only the reviewed whole-graph operation can replace stage/transition data.
    DELETE FROM ticket_board.workflow_transitions;
    UPDATE ticket_board.workflow_stages SET gate_skip_to=NULL;
    DELETE FROM ticket_board.workflow_stages ws WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name);
    -- Move ranks out of the desired range before writing the final contiguous order.
    UPDATE ticket_board.workflow_stages SET rank=rank+(SELECT coalesce(max(rank),0)+jsonb_array_length(cfg->'stages')+1 FROM ticket_board.workflow_stages);
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        INSERT INTO ticket_board.workflow_stages(name,display_label,rank,owner_roles,entry_gate_field,gate_skip_to,exit_signoff_field,is_terminal)
        VALUES(s->>'name',s->>'label',pos,ARRAY(SELECT jsonb_array_elements_text(s->'owners')),s->>'gate',NULL,s->>'signoff',(s->>'terminal')::boolean)
        ON CONFLICT(name) DO UPDATE SET display_label=EXCLUDED.display_label,rank=EXCLUDED.rank,owner_roles=EXCLUDED.owner_roles,
            entry_gate_field=EXCLUDED.entry_gate_field,gate_skip_to=NULL,exit_signoff_field=EXCLUDED.exit_signoff_field,is_terminal=EXCLUDED.is_terminal;
        pos:=pos+1;
    END LOOP;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        UPDATE ticket_board.workflow_stages SET gate_skip_to=s->>'skip_to' WHERE name=s->>'name';
    END LOOP;
    DELETE FROM ticket_board.workflow_transitions;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        INSERT INTO ticket_board.workflow_transitions(from_stage,to_stage,action_name,allowed_roles,owner_scoped,director_override)
        VALUES(tr->>'from',tr->>'to',tr->>'action',ARRAY(SELECT jsonb_array_elements_text(tr->'actors')),(tr->>'owner_scoped')::boolean,false);
    END LOOP;
    INSERT INTO ticket_board.workflow_revisions(actor,document) VALUES(ticket_board.current_app_actor(),cfg) RETURNING revision INTO rev;
    INSERT INTO ticket_board.workflow_configuration(singleton,revision,document) VALUES(true,rev,cfg)
      ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,document=EXCLUDED.document;
    FOR item IN SELECT * FROM jsonb_each_text(coalesce(cfg->'reassign','{}'::jsonb)) LOOP
        IF NOT EXISTS (SELECT FROM ticket_board.workflow_stages WHERE name=item.key AND item.value=ANY(owner_roles)) THEN
            RAISE EXCEPTION 'invalid deliberate reassignment'; END IF;
        UPDATE ticket_board.tickets SET assignee=item.value WHERE state=item.key AND assignee<>item.value;
    END LOOP;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_stages ws ON ws.name=t.state
       WHERE cardinality(ws.owner_roles)>0 AND t.assignee<>'unassigned' AND NOT t.assignee=ANY(ws.owner_roles)) THEN
        RAISE EXCEPTION 'configuration would orphan live ticket ownership; supply deliberate reassignment'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_roles r ON r.name=t.assignee
        JOIN ticket_board.workflow_stages ws ON ws.name=t.state WHERE NOT ws.is_terminal AND NOT (r.definition->>'active')::boolean AND t.assignee<>'unassigned') THEN
        RAISE EXCEPTION 'inactive role still owns active tickets'; END IF;
    PERFORM pg_notify('ticket_board_state_transition',jsonb_build_object('kind','workflow','revision',rev)::text);
    RETURN rev;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_flag(t jsonb, flag text, cfg jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce((t->>flag)::boolean,(t->'workflow_flags'->>flag)::boolean,(cfg->'flags'->flag->>'default')::boolean,false);
$$;
CREATE OR REPLACE FUNCTION ticket_board.set_workflow_flag(t jsonb, flag text, value boolean)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN t ? flag THEN jsonb_set(t,ARRAY[flag],to_jsonb(value))
    ELSE jsonb_set(t,'{workflow_flags}',coalesce(t->'workflow_flags','{}'::jsonb)||jsonb_build_object(flag,value)) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true); actor text:=ticket_board.current_app_actor(); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
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
        proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
    END IF;
    RETURN proposed;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action(id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; tr jsonb; actor text:=ticket_board.current_app_actor(); candidates int; handoff text; source_stage jsonb;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR actor IS NULL THEN RAISE EXCEPTION 'workflow action requires registered service actor' USING ERRCODE='42501'; END IF;
    SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=perform_workflow_action.id FOR UPDATE;
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
        PERFORM ticket_board.append_ticket_comment(t.id,actor,coalesce(payload->>'text',payload->>'reason'));
    END IF;
    PERFORM set_config('ticket_board.held_review_target','',true);
    PERFORM set_config('ticket_board.workflow_action',action,true);
    UPDATE ticket_board.tickets SET state=tr->>'to',
        assignee=coalesce(nullif(payload->>'assignee',''),assignee),
        commit_hash=coalesce(payload->>'commit_hash',commit_hash)
        WHERE tickets.id=t.id;
    PERFORM set_config('ticket_board.workflow_action','',true);
    handoff:=nullif(current_setting('ticket_board.held_review_target',true),'');
    PERFORM set_config('ticket_board.held_review_target','',true);
    IF handoff IS NOT NULL THEN
        UPDATE ticket_board.ticket_notification_state SET awaiting_role=handoff, awaiting_since_at=clock_timestamp(),
            last_activity_at=clock_timestamp(), nudge_count=0 WHERE ticket_id=t.id;
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
    END IF;
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

    IF current_setting('ticket_board.force_move', true) = 'on' THEN
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

CREATE OR REPLACE FUNCTION ticket_board.require_actor(
    p_allowed_roles text[],
    p_action text
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        actor := ticket_board.current_actor_role();
        caller_role := ticket_board.current_app_actor();
        IF actor <> 'ticket_board_service' OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=caller_role AND (r.definition->>'active')::boolean AND r.definition->'capabilities' ? CASE p_action WHEN 'set_awaiting_role' THEN 'await_role' ELSE p_action END) THEN
            RAISE EXCEPTION 'role % cannot call %', caller_role,p_action USING ERRCODE='42501';
        END IF;
        RETURN actor;
    END IF;
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_service is the only database writer', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    caller_role := nullif(current_setting('ticket_board.caller_role', true), '');
    IF caller_role IS NOT NULL
       AND cardinality(coalesce(p_allowed_roles, ARRAY[]::text[])) > 0
       AND caller_role <> ALL(p_allowed_roles)
       AND NOT (
           p_allowed_roles && ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]
           AND ticket_board.ticket_is_implementer_assignee(caller_role)
       ) THEN
        RAISE EXCEPTION 'role % cannot call %', caller_role, p_action
            USING ERRCODE = '42501';
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.current_app_actor()
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        actor := nullif(current_setting('ticket_board.caller_role',true),'');
        IF actor IS NULL OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles WHERE name=actor AND (definition->>'active')::boolean) THEN
            RAISE EXCEPTION 'invalid configured caller role: %',actor USING ERRCODE='42501';
        END IF;
        RETURN actor;
    END IF;
    actor := nullif(current_setting('ticket_board.caller_role', true), '');
    IF actor IS NULL THEN
        RETURN ticket_board.current_actor_role();
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM (
            SELECT unnest(wt.allowed_roles) AS role
            FROM ticket_board.workflow_transitions wt
            UNION
            SELECT unnest(ws.owner_roles) AS role
            FROM ticket_board.workflow_stages ws
        ) AS valid_roles
        WHERE valid_roles.role = actor
    ) THEN
        RAISE EXCEPTION 'invalid ticket_board.caller_role: %', actor;
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.legacy_transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN (
            SELECT CASE
                WHEN NULLIF(p_assignee, 'unassigned') = ANY(ws.owner_roles) THEN NULLIF(p_assignee, 'unassigned')
                WHEN cardinality(ws.owner_roles) = 1 THEN ws.owner_roles[1]
                ELSE NULL
            END
            FROM ticket_board.workflow_stages ws
            WHERE ws.name = 'audit'
        )
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN NULL
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text,p_assignee text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); s jsonb; owners text[];
BEGIN
    IF cfg IS NULL THEN RETURN ticket_board.legacy_transition_target_role(p_state,p_assignee); END IF;
    SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=p_state;
    owners:=ARRAY(SELECT jsonb_array_elements_text(s->'owners'));
    RETURN CASE s->'notify'->>'kind'
      WHEN 'none' THEN NULL WHEN 'fixed_role' THEN s->'notify'->>'role'
      WHEN 'assignee' THEN CASE WHEN p_assignee=ANY(owners) THEN p_assignee END
      WHEN 'stage_owner_fallback' THEN CASE WHEN p_assignee=ANY(owners) THEN p_assignee ELSE owners[1] END
      ELSE NULL END;
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.apply_declared_workflow(cfg jsonb, expected_revision bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE actual bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    SELECT coalesce(max(revision),0) INTO actual FROM ticket_board.workflow_configuration;
    IF expected_revision IS DISTINCT FROM actual THEN RAISE EXCEPTION 'workflow revision changed; reread before applying'; END IF;
    RETURN ticket_board.apply_declared_workflow(cfg);
END;
$$;

REVOKE ALL ON ticket_board.workflow_configuration,ticket_board.workflow_revisions,ticket_board.workflow_roles FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.apply_declared_workflow(jsonb),ticket_board.perform_workflow_action(text,text,jsonb) FROM PUBLIC;
GRANT SELECT ON ticket_board.workflow_configuration,ticket_board.workflow_revisions,ticket_board.workflow_roles TO ticket_board_service,ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.apply_declared_workflow(jsonb),ticket_board.perform_workflow_action(text,text,jsonb) TO ticket_board_service;

GRANT EXECUTE ON FUNCTION ticket_board.apply_declared_workflow(jsonb,bigint), ticket_board.declared_workflow(), ticket_board.workflow_flag(jsonb,text,jsonb), ticket_board.set_workflow_flag(jsonb,text,boolean), ticket_board.enforce_declared_ticket_update(ticket_board.tickets,ticket_board.tickets) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.declared_workflow() TO ticket_board_listener;


CREATE OR REPLACE FUNCTION ticket_board.declared_stage_kind(stage text)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT x->>'kind' FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x WHERE x->>'name'=stage;
$$;
CREATE OR REPLACE FUNCTION ticket_board.remember_declared_implementer()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ticket_board.declared_stage_kind(NEW.state)='implementation' THEN
   UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee=NEW.assignee WHERE ticket_id=NEW.id;
 END IF;
 RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_declared_implementer ON ticket_board.tickets;
CREATE TRIGGER tickets_z_declared_implementer AFTER INSERT OR UPDATE ON ticket_board.tickets
 FOR EACH ROW EXECUTE FUNCTION ticket_board.remember_declared_implementer();

CREATE OR REPLACE FUNCTION ticket_board.auto_advance_analysis_ticket()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN RETURN NULL; END IF;
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    IF ticket_board.ticket_can_auto_advance_analysis(
        NEW.state,
        NEW.assignee,
        NEW.implementation,
        NEW.manually_controlled,
        NEW.id
    ) THEN
        UPDATE ticket_board.tickets
        SET state = 'in_progress'
        WHERE id = NEW.id
          AND state = 'analysis';
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_implementer_and_activate_next()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    released_implementer text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN RETURN NULL; END IF;
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    IF NOT (
        NEW.state IN ('done', 'cancelled')
        OR (NEW.state = 'backlog' AND NEW.parked AND OLD.state IS DISTINCT FROM 'backlog')
    ) THEN
        RETURN NULL;
    END IF;

    released_implementer := ticket_board.ticket_reserved_implementer(OLD.id, OLD.state, OLD.assignee);
    IF released_implementer IS NULL THEN
        SELECT nullif(ns.last_implementer_assignee, '')
        INTO released_implementer
        FROM ticket_board.ticket_notification_state ns
        WHERE ns.ticket_id = NEW.id;
    END IF;

    IF ticket_board.ticket_is_implementer_assignee(released_implementer) THEN
        PERFORM ticket_board.activate_next_queued_ticket(released_implementer);
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.legacy_transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_already_announced boolean
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN ticket_board.state_rank(p_old_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) < ticket_board.state_rank(p_old_state)
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' kicked back to you'
        WHEN p_new_state = 'analysis'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'in_progress' AND p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is active again'
        WHEN p_new_state = 'in_progress'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'inspection'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for inspection'
        WHEN p_new_state = 'audit'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for audit'
        WHEN p_new_state = 'dat'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for Director Acceptance Testing'
        WHEN p_new_state = 'user_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for User UAT'
        WHEN p_new_state = 'director_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for your review'
        WHEN p_new_state = 'done'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' marked done'
        WHEN p_new_state = 'cancelled'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' cancelled'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_message(p_ticket_id text,p_title text,p_old_state text,p_new_state text,p_already_announced boolean)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_transition_message(p_ticket_id,p_title,p_old_state,p_new_state,p_already_announced)
 ELSE p_ticket_id || ' -- ' || p_title || ' entered ' || coalesce((SELECT display_label FROM ticket_board.workflow_stages WHERE name=p_new_state),p_new_state) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NOT NULL THEN ticket_board.transition_target_role(p_state,p_assignee) ELSE CASE
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_message(p_ticket_id text, p_title text, p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NOT NULL THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is waiting in ' || p_state ELSE CASE
        WHEN p_state = 'analysis' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' needs director triage in analysis'
        WHEN p_state = 'backlog' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is assigned in backlog; triage or defer explicitly'
        WHEN p_state = 'in_progress' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is still in progress'
        WHEN p_state = 'inspection' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for inspection'
        WHEN p_state = 'audit' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for audit'
        WHEN p_state = 'dat' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for DAT'
        WHEN p_state = 'director_review' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for your review'
        ELSE NULL
    END END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.pending_transition_notifications()
RETURNS TABLE(ticket_id text, payload jsonb)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('pending_transition_notifications');
    RETURN QUERY
    SELECT
        t.id,
        ticket_board.transition_notification_payload(t.id, ns.previous_state, t.state, t.assignee)
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_due_nudges(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 3
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    target_role text;
    payload jsonb;
BEGIN
    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    WHEN 'director_review' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'backlog' THEN 6
                    ELSE 7
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (
                    (
                        t.state = 'analysis'
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at THEN
            target_role := 'director';
            payload := jsonb_build_object(
                'kind', 'escalation',
                'id', candidate.id,
                'title', candidate.title,
                'target_role', target_role,
                'message', 'PRIORITY ' || candidate.id || ' -- ' || candidate.title || ' appears stuck for ' || candidate.assignee || '; check/reassign'
            );
        ELSE
            payload := jsonb_build_object(
                'kind', 'nudge',
                'id', candidate.id,
                'title', candidate.title,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', target_role,
                'message', ticket_board.nudge_message(candidate.id, candidate.title, candidate.state, candidate.assignee)
            );
            RAISE WARNING 'wedged-pane nudge backstop fired for % targeting % (%s since transition)',
                candidate.id,
                target_role,
                floor(extract(epoch FROM p_now - candidate.entered_current_state_at))::integer || 's';
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN candidate.last_activity_at > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
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
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_stall_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_grace interval DEFAULT interval '45 seconds',
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 2,
    p_work_observed_at_by_role jsonb DEFAULT '{}'::jsonb
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
    target_role text;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_stall_nudges');

    p_idle_since_by_role := coalesce(p_idle_since_by_role, '{}'::jsonb);
    p_work_observed_at_by_role := coalesce(p_work_observed_at_by_role, '{}'::jsonb);

    IF p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.idle_since_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.work_observed_at,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
                        THEN (p_work_observed_at_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('in_progress', 'inspection', 'audit', 'dat') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND p_idle_since_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
              AND greatest(
                    ns.entered_current_state_at,
                    (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                  ) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> 'director')::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? 'director'
                        THEN (p_work_observed_at_by_role ->> 'director')::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'backlog' THEN 6
                    ELSE 7
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (
                    (
                        t.state = 'analysis'
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND p_idle_since_by_role ? 'director'
              AND greatest(ns.entered_current_state_at, (p_idle_since_by_role ->> 'director')::timestamptz) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at
           AND coalesce(candidate.work_observed_at, '-infinity'::timestamptz) <= candidate.last_nudged_at THEN
            target_role := 'director';
            payload := jsonb_build_object(
                'kind', 'escalation',
                'id', candidate.id,
                'title', candidate.title,
                'target_role', target_role,
                'message', 'PRIORITY ' || candidate.id || ' -- ' || candidate.title || ' appears stuck for ' || candidate.assignee || '; check/reassign'
            );
        ELSE
            payload := jsonb_build_object(
                'kind', 'nudge',
                'id', candidate.id,
                'title', candidate.title,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', target_role,
                'message', ticket_board.nudge_message(candidate.id, candidate.title, candidate.state, candidate.assignee)
            );
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN greatest(
                    candidate.last_activity_at,
                    coalesce(candidate.work_observed_at, '-infinity'::timestamptz)
                ) > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_notification_roles()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ticket_board.declared_workflow() IS NOT NULL THEN
  IF NEW.awaiting_role<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.awaiting_role) THEN RAISE EXCEPTION 'unknown awaited role'; END IF;
  IF NEW.last_implementer_assignee<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.last_implementer_assignee AND definition->>'kind'='implementer') THEN RAISE EXCEPTION 'unknown implementation owner'; END IF;
 END IF;
 RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS notification_declared_roles ON ticket_board.ticket_notification_state;
CREATE TRIGGER notification_declared_roles BEFORE INSERT OR UPDATE ON ticket_board.ticket_notification_state
 FOR EACH ROW EXECUTE FUNCTION ticket_board.validate_declared_notification_roles();

CREATE OR REPLACE FUNCTION ticket_board.legacy_current_reserved_ticket(
    p_implementer text,
    p_excluding_ticket_id text DEFAULT NULL
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT t.id
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE ticket_board.ticket_is_implementer_assignee(p_implementer)
      AND (p_excluding_ticket_id IS NULL OR t.id <> p_excluding_ticket_id)
      AND NOT t.manually_controlled
      AND t.state IN ('in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
      AND (
          (t.state = 'in_progress' AND t.assignee = p_implementer)
          OR (
              t.state IN ('inspection', 'audit', 'dat', 'user_review', 'director_review')
              AND ns.last_implementer_assignee = p_implementer
              AND NOT (t.state = 'user_review' AND t.assignee = 'user')
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_current_reserved_ticket(p_implementer text,p_excluding_ticket_id text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_current_reserved_ticket(p_implementer,p_excluding_ticket_id)
 ELSE (SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
 WHERE (p_excluding_ticket_id IS NULL OR t.id<>p_excluding_ticket_id) AND NOT t.manually_controlled
 AND ticket_board.declared_stage_kind(t.state) IN ('implementation','review')
 AND (CASE WHEN ticket_board.declared_stage_kind(t.state)='implementation' THEN t.assignee ELSE ns.last_implementer_assignee END)=p_implementer
 ORDER BY t.ticket_number LIMIT 1) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        IF ticket_board.declared_stage_kind(NEW.state)='draft' THEN NEW.assignee:=coalesce(ticket_board.stage_default_assignee(NEW.state),NEW.assignee); END IF;
        IF ticket_board.declared_stage_kind(NEW.state)='implementation' AND ticket_board.ticket_current_reserved_ticket(NEW.assignee,NEW.id) IS NOT NULL AND NOT NEW.manually_controlled THEN
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


CREATE OR REPLACE FUNCTION ticket_board.set_declared_flags(id text, patch jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; item record; doc jsonb;
BEGIN
 IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN RAISE EXCEPTION 'only director may set gates' USING ERRCODE='42501'; END IF;
 IF cfg IS NULL OR jsonb_typeof(patch) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'configured gates object required'; END IF;
 SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=set_declared_flags.id FOR UPDATE;
 doc:=to_jsonb(t);
 FOR item IN SELECT * FROM jsonb_each(patch) LOOP
  IF cfg->'flags'->item.key->>'kind' IS DISTINCT FROM 'gate' OR jsonb_typeof(item.value) IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'only declared boolean gates may be edited'; END IF;
  doc:=ticket_board.set_workflow_flag(doc,item.key,(item.value::text)::boolean);
 END LOOP;
 t:=jsonb_populate_record(t,doc);
 UPDATE ticket_board.tickets SET needs_inspection=t.needs_inspection,needs_audit=t.needs_audit,needs_user_signoff=t.needs_user_signoff,workflow_flags=t.workflow_flags WHERE tickets.id=t.id;
 PERFORM ticket_board.append_ticket_comment(t.id,'director','Updated workflow gates: '||patch::text);
END;
$$;


REVOKE ALL ON FUNCTION ticket_board.set_declared_flags(text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.set_declared_flags(text,jsonb) TO ticket_board_service;

CREATE OR REPLACE FUNCTION ticket_board.workflow_wait_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN stage IN ('in_progress','inspection','audit')
 ELSE EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s WHERE s->>'name'=stage AND NOT (s->>'terminal')::boolean AND s->>'kind'<>'draft') END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF NOT ticket_board.workflow_wait_stage(t.state)
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF NOT (CASE WHEN ticket_board.declared_workflow() IS NULL THEN normalized_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') ELSE EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=normalized_role AND (r.definition->>'active')::boolean AND r.definition->>'target' IS NOT NULL) END) THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND ticket_board.workflow_wait_stage(t.state)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND ticket_board.workflow_wait_stage(t.state);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.ticket_is_implementer_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_stages ws
        WHERE CASE WHEN ticket_board.declared_workflow() IS NULL THEN ws.name='in_progress' ELSE ticket_board.declared_stage_kind(ws.name)='implementation' END
          AND btrim(lower(coalesce(p_assignee, ''))) = ANY(ws.owner_roles)
    );
$$;
REVOKE ALL ON FUNCTION ticket_board.workflow_wait_stage(text),ticket_board.enqueue_awaiting_role_handoff(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.workflow_wait_stage(text) TO ticket_board_service,ticket_board_listener;

    GRANT EXECUTE ON FUNCTION ticket_board.transition_target_role(text,text),ticket_board.legacy_transition_target_role(text,text) TO ticket_board_service;
