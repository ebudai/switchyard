-- SYRD-82: keep the Director control capability floor in declarative workflows.
--
-- A tenant configures its own pipeline. What it does not get to do is leave the
-- project without a controller, or make the controller a reviewer of its own
-- work. Both halves are refused here, in the database, so a caller that reaches
-- PostgreSQL without going through the API validator is refused too.
--
-- Kept in sync with schema.sql: these two bodies are copies of the ones there,
-- and ticket_board_schema_function_migration_test holds them equal.

CREATE OR REPLACE FUNCTION ticket_board.director_control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'reassign', 'set_manually_controlled', 'set_blockers',
        'merge', 'edit_fields', 'dismiss_notification'
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

-- Bring an existing tenant up to the floor rather than locking it out of its own
-- workflow. A document written before this rule may leave the director short of a
-- capability; the next configuration change would then be refused for a state its
-- director never chose. Granting it here is the same one-time, marked repair the
-- reassign capability shipped with, and it is recorded so a later edit is not
-- silently re-applied over (SYRD-77, SYRD-82).
--
-- The structural half of the floor cannot be repaired this way: transitions are
-- the tenant's to name, and nothing here can invent a way out of a stage. The
-- validate call below is what reports that, by name, and fails the upgrade rather
-- than leaving a project whose director cannot move its work.
DO $$
DECLARE cfg jsonb; roles jsonb; rev bigint; r jsonb; missing text[];
BEGIN
    IF to_regclass('ticket_board.workflow_configuration') IS NULL THEN RETURN; END IF;
    SELECT document INTO cfg FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    SELECT array_agg(c) INTO missing FROM unnest(ticket_board.director_control_capabilities()) c
     WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
         WHERE x->>'name' = 'director' AND x->'capabilities' ? c);
    IF missing IS NULL
       AND coalesce((cfg->'migrations'->>'director_capability_floor')::boolean, false) THEN
        PERFORM ticket_board.validate_declared_workflow(cfg);
        RETURN;
    END IF;
    SELECT jsonb_agg(
        CASE
            WHEN entry.value->>'name' = 'director'
                THEN jsonb_set(
                    entry.value,
                    '{capabilities}',
                    (SELECT jsonb_agg(DISTINCT c) FROM (
                        SELECT jsonb_array_elements_text(entry.value->'capabilities') AS c
                        UNION SELECT unnest(ticket_board.director_control_capabilities())
                    ) merged))
            ELSE entry.value
        END
        ORDER BY entry.ordinality)
    INTO roles
    FROM jsonb_array_elements(cfg->'roles') WITH ORDINALITY AS entry(value, ordinality);
    cfg := jsonb_set(cfg, '{roles}', roles);
    cfg := jsonb_set(
        cfg,
        '{migrations}',
        coalesce(cfg->'migrations', '{}'::jsonb) || jsonb_build_object('director_capability_floor', true));
    PERFORM ticket_board.validate_declared_workflow(cfg);
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('migration', cfg) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    FOR r IN SELECT value FROM jsonb_array_elements(roles) LOOP
        UPDATE ticket_board.workflow_roles SET definition = r WHERE name = r->>'name';
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $$;
