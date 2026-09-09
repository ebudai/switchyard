-- SYRD-83: the Director's generic ticket edit, for a board that already exists.
--
-- schema.sql is applied once, at install; everything after that reaches a tenant
-- through this directory. So an operation added to the schema alone is an
-- operation no running board has. This carries the whole of SYRD-83 across:
-- the audit table, the operation, the two triggers that must honour its window,
-- and -- because the HTTP handler decides a non-transition operation from the
-- capabilities the caller's workflow role declares -- the vocabulary and the
-- control floor that let the live Director reach it at all.
--
-- The function bodies below are copies of the ones in schema.sql, and
-- ticket_board_schema_function_migration_test holds them equal.

BEGIN;

-- What a Director edit records: one row per field that actually moved, with the
-- actor, the reason, and the old and new value.

CREATE TABLE IF NOT EXISTS ticket_board.ticket_field_audit (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    actor text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    field text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    ts timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ticket_field_audit_ticket_idx
    ON ticket_board.ticket_field_audit (ticket_id, id);

CREATE OR REPLACE FUNCTION ticket_board.signoff_fields()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['audit_signoff', 'inspector_signoff', 'user_signoff']::text[];
$$;

CREATE OR REPLACE FUNCTION ticket_board.role_controls_project(p_role text)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow();
BEGIN
    IF p_role IS NULL OR btrim(p_role) = '' THEN
        RETURN false;
    END IF;
    IF cfg IS NULL THEN
        RETURN p_role = 'director';
    END IF;
    RETURN EXISTS (
        SELECT FROM ticket_board.workflow_roles r
        WHERE r.name = p_role
          AND (r.definition->>'active')::boolean
          AND r.definition->'capabilities' ?& ARRAY['set_manually_controlled', 'merge']::text[]
    );
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
    IF target_state = 'in_progress'
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

-- The control floor, with the new operation in it: a tenant configures its
-- own pipeline, but not a Director who cannot rework a ticket.
CREATE OR REPLACE FUNCTION ticket_board.director_control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'reassign', 'set_manually_controlled', 'set_blockers',
        'merge', 'edit_fields', 'dismiss_notification', 'director_edit'
    ]::text[];
$$;

-- The declared vocabulary, so a document may name the capability at all.
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
                'reassign','director_edit')) THEN
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
END;
$$;

-- Both row triggers, which must recognise the edit's transaction-local window.
CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true); actor text:=ticket_board.current_app_actor(); target text;
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


-- Bring an existing tenant's document up to the new floor rather than locking it
-- out of its own workflow. A document written before this operation existed
-- cannot name it, and the handler reads that document -- so without this, the
-- Director on every upgraded board is refused before ticket_board.director_edit
-- runs, and the next configuration change is refused too, for a floor the
-- tenant never chose to break.
--
-- The capability goes to whichever role already holds control authority,
-- derived from the capabilities that define one rather than from the name
-- 'director' (SYRD-49). It is marked, so a Director who later removes it is not
-- silently overruled on the next upgrade, and it grants nothing to a role that
-- was not already the control role.
DO $migration$
DECLARE cfg jsonb; roles jsonb; rev bigint; r jsonb;
BEGIN
    IF to_regclass('ticket_board.workflow_configuration') IS NULL THEN RETURN; END IF;
    SELECT document INTO cfg FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    IF coalesce((cfg->'migrations'->>'director_edit_capability')::boolean, false) THEN
        RETURN;
    END IF;
    SELECT jsonb_agg(
        CASE
            WHEN entry.value->'capabilities' ?& ARRAY['set_manually_controlled', 'merge']::text[]
                 AND NOT entry.value->'capabilities' ? 'director_edit'
                THEN jsonb_set(
                    entry.value,
                    '{capabilities}',
                    (entry.value->'capabilities') || '["director_edit"]'::jsonb)
            ELSE entry.value
        END
        ORDER BY entry.ordinality)
    INTO roles
    FROM jsonb_array_elements(cfg->'roles') WITH ORDINALITY AS entry(value, ordinality);
    cfg := jsonb_set(cfg, '{roles}', roles);
    cfg := jsonb_set(
        cfg,
        '{migrations}',
        coalesce(cfg->'migrations', '{}'::jsonb) || jsonb_build_object('director_edit_capability', true));
    PERFORM ticket_board.validate_declared_workflow(cfg);
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('migration', cfg) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    FOR r IN SELECT value FROM jsonb_array_elements(roles) LOOP
        UPDATE ticket_board.workflow_roles SET definition = r WHERE name = r->>'name';
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $migration$;

-- The board's writer needs the new function and the audit table; it reaches the
-- tickets table only through definer-rights functions, and the one added here
-- refuses to raise a sign-off.
--
-- Which role that writer is cannot be spelled: a per-project board runs under
-- its own generated role name. So the grant follows the privilege that already
-- identifies the writer -- execute on force_move, which rbac.sql gives to it
-- and to nobody else -- rather than a name this file would have to guess, and
-- a board whose roles are not created yet is simply left alone.
DO $grants$
DECLARE writer text;
BEGIN
    FOR writer IN
        SELECT rolname FROM pg_roles
        WHERE NOT rolsuper
          AND has_function_privilege(
                  rolname,
                  'ticket_board.force_move(text, text, text, boolean)',
                  'EXECUTE')
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.director_edit(text, jsonb, text) TO %I', writer);
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.signoff_fields() TO %I', writer);
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.role_controls_project(text) TO %I', writer);
        EXECUTE format(
            'GRANT SELECT, INSERT ON ticket_board.ticket_field_audit TO %I', writer);
        EXECUTE format(
            'GRANT USAGE, SELECT ON SEQUENCE ticket_board.ticket_field_audit_id_seq TO %I', writer);
    END LOOP;
END $grants$;

COMMIT;
