-- SYRD-719: Audit's kick-back tells the work's substantive origin from the
-- Librarian's documentation layer, and Audit chooses which a finding goes to.
--
-- SYRD-673 recorded one producer per revision, so the Librarian's documentation
-- commit became the producer, and every Audit finding on that combined
-- candidate -- code, tests, configuration, security -- went to the Librarian.
-- Now:
--   * a pass through Documentation (a documentation commit, or no change) is
--     recorded beside the producer as the documentation layer, with the base
--     revision under it, and never replaces the producer;
--   * audit_kick_back returns to the producer (the substantive origin) for the
--     revision under review or the base of its documentation layer, and owes
--     Documentation's sign-off again; a review stage is never that origin;
--   * audit_documentation_kick_back, declared with the documentation gate out
--     of each stage audit_kick_back leaves, returns a documentation-only
--     finding to the Documentation stage and owner the ticket passed, keeping
--     the candidate; it is refused, moving nothing, without that record;
--   * a board with the documentation gate declares it here (a new workflow
--     revision);
--   * a record where the Librarian's commit stood as the producer becomes the
--     documentation layer, and its substantive origin is unrecorded: nothing is
--     guessed. A Director's record naming a review stage is unrecorded too.
--     Each such ticket in Audit is the Director's to repair with
--     set-audit-return, told once; nothing is moved.
--
-- schema.sql carries the same objects.

BEGIN;

-- The documentation layer beside the producer (identical to schema.sql's columns).
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_stage text;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_assignee text;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_action text;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_at timestamptz;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_commit text;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS documentation_exempt boolean;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS base_commit text;
ALTER TABLE ticket_board.ticket_audit_returns ADD COLUMN IF NOT EXISTS base_exempt boolean;

-- Audit's documentation-only return (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.audit_documentation_return_transition(tr jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(tr->>'action' = 'audit_documentation_kick_back' AND tr->>'primitive' = 'return', false);
$$;

-- a documentation decision that passes the work on (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.documentation_pass_action(p_stage text, p_action text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT p_action IN ('documentation_sign_off', 'documentation_no_change')
       AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
                    WHERE x->>'from' = p_stage AND x->>'action' = p_action);
$$;

-- a review stage is no substantive origin (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.substantive_origin_problem(p_stage text)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT format('%s is a review stage, which produces no substantive work for Audit''s kick-back to return; '
                  'a documentation-only finding goes back with audit-documentation-kick-back', p_stage)
     WHERE ticket_board.declared_stage_kind(p_stage) = 'review';
$$;

-- the trigger records the documentation layer beside the producer (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.record_audit_return()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    action_name text := nullif(current_setting('ticket_board.workflow_action', true), '');
    how text;
    revised boolean;
    own boolean;
    moved boolean := OLD.state IS DISTINCT FROM NEW.state;
    r ticket_board.ticket_audit_returns;
    base text;
    base_none boolean;
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
    IF own AND moved AND ticket_board.documentation_pass_action(OLD.state, action_name)
       AND (btrim(coalesce(NEW.commit_hash, '')) <> '' OR NEW.commit_exempt) THEN
        SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = NEW.id;
        IF r.documentation_commit = coalesce(OLD.commit_hash, '') AND r.documentation_exempt = OLD.commit_exempt
           AND r.base_commit IS NOT NULL THEN
            base := r.base_commit; base_none := r.base_exempt;
        ELSE
            base := coalesce(OLD.commit_hash, ''); base_none := OLD.commit_exempt;
        END IF;
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_action, producer_at, producer_commit,
                                                       producer_exempt, documentation_stage, documentation_assignee,
                                                       documentation_action, documentation_at, documentation_commit,
                                                       documentation_exempt, base_commit, base_exempt)
        VALUES (NEW.id, 'unrecorded', clock_timestamp(), '', false, OLD.state, OLD.assignee, how, clock_timestamp(),
                coalesce(NEW.commit_hash, ''), NEW.commit_exempt, base, base_none)
        ON CONFLICT (ticket_id) DO UPDATE SET documentation_stage = EXCLUDED.documentation_stage,
            documentation_assignee = EXCLUDED.documentation_assignee, documentation_action = EXCLUDED.documentation_action,
            documentation_at = EXCLUDED.documentation_at, documentation_commit = EXCLUDED.documentation_commit,
            documentation_exempt = EXCLUDED.documentation_exempt, base_commit = EXCLUDED.base_commit,
            base_exempt = EXCLUDED.base_exempt;
    ELSIF (revised AND own) OR (NOT revised AND moved AND ticket_board.producing_holder(OLD.state, OLD.assignee)) THEN
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action,
                                                       producer_at, producer_commit, producer_exempt)
        VALUES (NEW.id, OLD.state, OLD.assignee, how, clock_timestamp(), coalesce(NEW.commit_hash, ''), NEW.commit_exempt)
        ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = EXCLUDED.producer_stage,
            producer_assignee = EXCLUDED.producer_assignee, producer_action = EXCLUDED.producer_action,
            producer_at = EXCLUDED.producer_at, producer_commit = EXCLUDED.producer_commit,
            producer_exempt = EXCLUDED.producer_exempt, recorded_by = '', reason = '',
            documentation_stage = NULL, documentation_assignee = NULL, documentation_action = NULL,
            documentation_at = NULL, documentation_commit = NULL, documentation_exempt = NULL,
            base_commit = NULL, base_exempt = NULL;
    ELSIF revised THEN
        INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action,
                                                       producer_at, producer_commit, producer_exempt)
        VALUES (NEW.id, NULL, NULL, how, clock_timestamp(), coalesce(NEW.commit_hash, ''), NEW.commit_exempt)
        ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = NULL, producer_assignee = NULL,
            producer_action = EXCLUDED.producer_action, producer_at = EXCLUDED.producer_at,
            producer_commit = EXCLUDED.producer_commit, producer_exempt = EXCLUDED.producer_exempt,
            recorded_by = '', reason = '',
            documentation_stage = NULL, documentation_assignee = NULL, documentation_action = NULL,
            documentation_at = NULL, documentation_commit = NULL, documentation_exempt = NULL,
            base_commit = NULL, base_exempt = NULL;
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

-- both layers, for the ticket's JSON and the executor (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.audit_return_status(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets;
    r ticket_board.ticket_audit_returns;
    revision text;
    problem text;
    layered boolean;
    produced boolean;
    returned jsonb;
    documented text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    IF NOT FOUND OR NOT ticket_board.audit_return_stage(t.state) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = p_ticket;
    revision := coalesce(nullif(t.commit_hash, ''), CASE WHEN t.commit_exempt THEN 'no code' ELSE 'none' END);
    layered := coalesce(r.documentation_stage IS NOT NULL AND r.documentation_commit = coalesce(t.commit_hash, '')
                        AND r.documentation_exempt = t.commit_exempt, false);
    produced := coalesce((r.producer_commit = coalesce(t.commit_hash, '') AND r.producer_exempt = t.commit_exempt)
                         OR (layered AND r.producer_commit = r.base_commit AND r.producer_exempt = r.base_exempt), false);
    problem := CASE
        WHEN r.producer_action IS DISTINCT FROM 'unrecorded' AND produced AND r.producer_stage IS NULL
        THEN format('its revision (%s) was made by %s, not by the holder of a stage that produces work', revision,
                    r.producer_action)
        WHEN (r.producer_stage IS NULL OR NOT produced) AND layered AND r.documentation_commit IS DISTINCT FROM r.base_commit
        THEN format('its revision (%s) is the documentation commit %s made by %s, and the board has no record of who '
                    'produced the work under it (%s)', revision, r.documentation_assignee, r.documentation_action,
                    coalesce(nullif(r.base_commit, ''), CASE WHEN r.base_exempt THEN 'no code' ELSE 'unknown' END))
        WHEN r.producer_stage IS NULL OR NOT produced
        THEN format('the board has no record of who produced the revision it carries (%s)', revision)
        ELSE coalesce(ticket_board.audit_return_problem(t.state, r.producer_stage, r.producer_assignee),
                      ticket_board.substantive_origin_problem(r.producer_stage)) END;
    SELECT x INTO returned FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
     WHERE x->>'from' = t.state AND ticket_board.audit_documentation_return_transition(x) LIMIT 1;
    IF returned IS NOT NULL THEN
        documented := CASE
            WHEN NOT layered
            THEN format('the board has no record that it passed through %s with the revision it carries (%s)',
                        returned->>'to', revision)
            WHEN r.documentation_stage IS DISTINCT FROM returned->>'to'
            THEN format('it passed through %s, not %s, where this return goes', r.documentation_stage, returned->>'to')
            ELSE ticket_board.audit_return_problem(t.state, r.documentation_stage, r.documentation_assignee) END;
    END IF;
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
            '--reason TEXT), and Audit then kicks it back as usual', p_ticket) END,
        'documentation', CASE WHEN returned IS NOT NULL THEN jsonb_build_object(
            'valid', documented IS NULL, 'action', returned->>'action',
            'stage', CASE WHEN layered THEN r.documentation_stage END,
            'assignee', CASE WHEN layered THEN r.documentation_assignee END,
            'by', CASE WHEN layered THEN r.documentation_action END,
            'since', CASE WHEN layered THEN ticket_board.utc_text(r.documentation_at) END,
            'commit', CASE WHEN layered THEN r.documentation_commit END,
            'base', CASE WHEN layered THEN r.base_commit END,
            'problem', documented,
            'otherwise', CASE WHEN documented IS NOT NULL THEN
                'a finding goes back to its substantive origin with audit-kick-back' END) END);
END;
$$;

-- the Director records the substantive origin only (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.set_audit_return(p_ticket text, p_stage text, p_assignee text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    caller text := ticket_board.current_app_actor();
    reason text := btrim(coalesce(p_reason, ''));
    stage text := lower(btrim(coalesce(p_stage, '')));
    owner text := lower(btrim(coalesce(p_assignee, '')));
    t ticket_board.tickets;
    r ticket_board.ticket_audit_returns;
    problem text;
    made text;
    made_none boolean;
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
    problem := coalesce(ticket_board.audit_return_problem(t.state, stage, owner),
                        ticket_board.substantive_origin_problem(stage));
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION 'set-audit-return refused for %: %', t.id, problem;
    END IF;
    SELECT * INTO r FROM ticket_board.ticket_audit_returns WHERE ticket_id = t.id;
    IF r.documentation_commit = coalesce(t.commit_hash, '') AND r.documentation_exempt = t.commit_exempt
       AND r.base_commit IS NOT NULL THEN
        made := r.base_commit; made_none := r.base_exempt;
    ELSE
        made := coalesce(t.commit_hash, ''); made_none := t.commit_exempt;
    END IF;
    INSERT INTO ticket_board.ticket_audit_returns (ticket_id, producer_stage, producer_assignee, producer_action, producer_at,
                                                   producer_commit, producer_exempt, recorded_by, reason)
    VALUES (t.id, stage, owner, 'set_audit_return', clock_timestamp(), made, made_none, caller, reason)
    ON CONFLICT (ticket_id) DO UPDATE SET producer_stage = EXCLUDED.producer_stage,
        producer_assignee = EXCLUDED.producer_assignee, producer_action = EXCLUDED.producer_action,
        producer_at = EXCLUDED.producer_at, producer_commit = EXCLUDED.producer_commit,
        producer_exempt = EXCLUDED.producer_exempt, recorded_by = EXCLUDED.recorded_by, reason = EXCLUDED.reason;
    PERFORM ticket_board.append_ticket_comment(t.id, caller,
        format('Audit''s kick-back now returns this ticket to %s/%s: %s', stage, owner, reason));
    RETURN ticket_board.audit_return_status(t.id);
END;
$$;

-- admits Audit's documentation return into the documentation stage (identical to schema.sql).
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
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user','router')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign','director_edit','request_publication','resolve_publication',
                'recover_stalled_ticket','router_assign')) THEN
            RAISE EXCEPTION 'invalid role policy: %', r->>'name';
        END IF;
        IF (r->>'runtime' IS NULL) <> (r->>'target' IS NULL)
           OR (r->>'runtime' IS NOT NULL AND (r->>'runtime' NOT IN ('claude','codex','agy','hermes','copilot')
               OR r->>'target' !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$')) THEN
            RAISE EXCEPTION 'invalid runtime/target'; END IF;
        -- SYRD-135: absent is false. Only a real boolean may say otherwise, so
        -- a quoted "false" cannot read as a value and behave as its truthiness.
        IF r ? 'ephemeral' AND jsonb_typeof(r->'ephemeral') <> 'boolean' THEN
            RAISE EXCEPTION 'ephemeral must be a boolean: %', r->>'name'; END IF;
        -- SYRD-37: whether this role is handed one ticket at a time in the
        -- stages it owns. Same rule and same reason as `ephemeral`: absent is
        -- "unchanged", and only a real boolean may say otherwise.
        IF r ? 'serial' AND jsonb_typeof(r->'serial') <> 'boolean' THEN
            RAISE EXCEPTION 'serial must be a boolean: %', r->>'name'; END IF;
        -- SYRD-141: what a person reads on this role's pane. Optional; a
        -- non-empty single-line string when present, because a terminal
        -- renders it and a newline in a title is an instruction, not text.
        IF r ? 'presentation_label' AND (jsonb_typeof(r->'presentation_label') <> 'string'
            OR btrim(r->>'presentation_label') = '' OR r->>'presentation_label' LIKE E'%\n%') THEN
            RAISE EXCEPTION 'presentation_label must be non-empty single-line text: %', r->>'name'; END IF;
        -- SYRD-678: a draft role (a designer) may also implement, and nothing
        -- else may be combined; a routing scope names a declared gate flag and
        -- narrows Router's choice of an Implementation owner.
        IF r ? 'additional_kinds' AND (jsonb_typeof(r->'additional_kinds') <> 'array'
            OR jsonb_array_length(r->'additional_kinds') = 0
            OR EXISTS (SELECT FROM jsonb_array_elements(r->'additional_kinds') k WHERE jsonb_typeof(k) <> 'string')
            OR (SELECT count(DISTINCT k) FROM jsonb_array_elements_text(r->'additional_kinds') k) <> jsonb_array_length(r->'additional_kinds')
            OR r->>'kind' <> 'draft' OR r->'additional_kinds' <> '["implementer"]'::jsonb) THEN
            RAISE EXCEPTION 'invalid additional_kinds: only a draft role may also be an implementer: %', r->>'name'; END IF;
        IF r ? 'routing_scope' AND (jsonb_typeof(r->'routing_scope') <> 'object'
            OR (SELECT array_agg(k) FROM jsonb_object_keys(r->'routing_scope') k) <> ARRAY['flag']
            OR jsonb_typeof(r->'routing_scope'->'flag') <> 'string'
            OR cfg->'flags'->(r->'routing_scope'->>'flag')->>'kind' IS DISTINCT FROM 'gate'
            OR NOT ticket_board.role_has_kind(r, 'implementer')) THEN
            RAISE EXCEPTION 'invalid routing_scope: it names a declared gate flag, on a role that implements: %', r->>'name'; END IF;
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
    -- SYRD-276: how long an implementer stays reserved by work it has touched.
    -- Absent is 'review', the behaviour every tenant has always had.
    IF cfg ? 'reservation' AND (jsonb_typeof(cfg->'reservation') IS DISTINCT FROM 'string'
        OR cfg->>'reservation' NOT IN ('review','lifecycle')) THEN
        RAISE EXCEPTION 'invalid reservation policy'; END IF;
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
            SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE (s->'owners') ? (x->>'name') AND NOT ticket_board.role_has_kind(x, 'implementer'))) THEN
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
        -- SYRD-567: or a review hands work back to the non-review stage that
        -- submits into it (a design review returning a spec to its designer).
        -- workflow_config._review_hands_back is the same rule.
        -- SYRD-719: or Audit returns a documentation-only finding, out of a stage
        -- its kick-back leaves, to the Documentation stage
        -- (documentation_gate.audit_documentation_return is the same rule).
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation'
           AND NOT (EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='review')
                    AND s->>'kind'<>'review'
                    AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                                WHERE m->>'from'=tr->>'to' AND m->>'to'=tr->>'from' AND m->>'primitive' IN ('move','approve')))
           AND NOT (tr->>'action'='audit_documentation_kick_back' AND tr->>'to'='documentation'
                    AND s->>'kind'='review' AND s->>'gate'='needs_documentation'
                    AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                                WHERE m->>'from'=tr->>'from' AND m->>'action'='audit_kick_back' AND m->>'primitive'='return'))
        THEN RAISE EXCEPTION 'return must target implementation'; END IF;
        IF (tr->>'allow_no_code')::boolean AND ((tr->>'require_commit')::boolean OR NOT (tr->>'require_reason')::boolean OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='implementation')) THEN RAISE EXCEPTION 'invalid no-code transition policy'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements_text(tr->'clear_signoffs') x
            WHERE cfg->'flags'->x->>'kind' IS DISTINCT FROM 'signoff') THEN RAISE EXCEPTION 'invalid signoff reset'; END IF;
        -- SYRD-214: a transition may record a decision its actor did not make,
        -- for a role that reached its verdict off the board. Absent means the
        -- ordinary case: the actor's own decision, which is every transition
        -- that predates relaying.
        --
        -- SYRD-217 widened this from "returns only". Refusing to relay the
        -- User's acceptance did not stop the acceptance being acted on; it
        -- pushed it into a narrated override, where the destination is
        -- whatever the controller types and no gate is checked at all. The
        -- fence that replaces it is tighter where it matters: a relay must
        -- mirror a move the relayed role can itself make out of this stage --
        -- same primitive, same destination, same sign-off clearing -- so it is
        -- that role's own step under another hand, never a route the role does
        -- not have. An approval additionally names what it accepts and may not
        -- be the move that ends the ticket.
        IF jsonb_typeof(tr->'relays_decision_of') IS NOT NULL
           AND jsonb_typeof(tr->'relays_decision_of') <> 'null' THEN
            IF jsonb_typeof(tr->'relays_decision_of') <> 'string'
               OR NOT coalesce(tr->>'relays_decision_of'=ANY(role_names),false)
               OR tr->>'primitive' NOT IN ('return','approve')
               OR NOT (tr->>'require_reason')::boolean
               OR tr->'actors' ? (tr->>'relays_decision_of')
               OR (tr->>'owner_scoped')::boolean
               OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of'))
               OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of')
                     AND (m->>'to' IS DISTINCT FROM tr->>'to'
                          OR ticket_board.signoff_reset_key(m) IS DISTINCT FROM ticket_board.signoff_reset_key(tr)))
               -- An approval is relayed only for a role with no pane of its
               -- own -- a reviewer that can sign off for itself must -- it
               -- names what it accepts, and it may not end the ticket.
               -- Approvals only: applied to returns this would retroactively
               -- invalidate what pgu952 legitimately produced (SYRD-217).
               OR (tr->>'primitive'='approve'
                   AND ((SELECT pane->>'target' FROM jsonb_array_elements(cfg->'roles') pane
                          WHERE pane->>'name'=tr->>'relays_decision_of') IS NOT NULL
                        OR NOT (tr->>'require_commit')::boolean
                        OR coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
                                      WHERE x->>'name'=tr->>'to'),false))) THEN
                RAISE EXCEPTION 'invalid relayed decision policy: %', tr->>'action'; END IF;
        END IF;
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
    --
    -- SYRD-217 narrows this by exactly one case, and that is a weakening of
    -- SYRD-82's floor, said plainly. A relayed approval writes somebody else's
    -- sign-off rather than the director's, and the relay fence above has
    -- already required it to mirror a move that role can make from that stage,
    -- to name the commit it accepts, to carry its reason, and to be relayable
    -- only for a role with no way to act for itself. The director still holds
    -- no approval of its own. What this cannot do is make the director
    -- truthful -- but nor could the narrated override it replaces, which
    -- named no commit and checked no gate.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
        WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
          AND x->>'relays_decision_of' IS NULL) THEN
        RAISE EXCEPTION 'director must not be granted sign-off authority: %',
            (SELECT string_agg(x->>'action', ', ' ORDER BY x->>'action')
             FROM jsonb_array_elements(cfg->'transitions') x
             WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
               AND x->>'relays_decision_of' IS NULL);
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
    PERFORM ticket_board.validate_router_declaration(cfg);  -- SYRD-599
END;
$$;

-- the executor takes Audit's documentation return (identical to schema.sql).
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

REVOKE ALL ON ticket_board.ticket_audit_returns FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.audit_return_problem(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_audit_return() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.set_audit_return(text, text, text, text) FROM PUBLIC;

-- Records made before the two layers. The Librarian's own documentation commit
-- was recorded as the producer: it is the documentation layer, with no known
-- base, and the work under it has no recorded origin. A Director's record of a
-- review stage is no substantive origin either. Nothing else is inferred.
CREATE TEMPORARY TABLE syrd719_unrecorded ON COMMIT DROP AS
    SELECT ticket_id FROM ticket_board.ticket_audit_returns
     WHERE producer_action IN ('documentation_sign_off', 'documentation_no_change')
        OR (producer_stage IS NOT NULL AND ticket_board.declared_stage_kind(producer_stage) = 'review');
UPDATE ticket_board.ticket_audit_returns
   SET documentation_stage = producer_stage, documentation_assignee = producer_assignee,
       documentation_action = producer_action, documentation_at = producer_at, documentation_commit = producer_commit,
       documentation_exempt = producer_exempt, base_commit = NULL, base_exempt = NULL
 WHERE producer_action IN ('documentation_sign_off', 'documentation_no_change');
UPDATE ticket_board.ticket_audit_returns
   SET producer_stage = NULL, producer_assignee = NULL, producer_action = 'unrecorded', producer_at = clock_timestamp(),
       producer_commit = '', producer_exempt = false, recorded_by = '', reason = ''
 WHERE ticket_id IN (SELECT ticket_id FROM syrd719_unrecorded);

-- A board with the documentation gate gets Audit's documentation return, out of
-- each stage audit_kick_back leaves, exactly as documentation_gate composes it.
DO $layers$
DECLARE cfg jsonb; rev bigint; added jsonb;
BEGIN
    SELECT document, revision INTO cfg, rev FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL
       OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
                       WHERE s->>'name' = 'documentation' AND s->>'kind' = 'review' AND s->>'gate' = 'needs_documentation')
       OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'action' = 'audit_documentation_kick_back') THEN
        RETURN;
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'from', k->>'from', 'to', 'documentation', 'action', 'audit_documentation_kick_back',
               'label', 'Return documentation-only findings', 'actors', k->'actors', 'primitive', 'return',
               'owner_scoped', k->'owner_scoped', 'require_commit', false, 'require_reason', true,
               'clear_signoffs', (SELECT jsonb_agg(f ORDER BY f COLLATE "C")
                                    FROM (SELECT jsonb_array_elements_text(k->'clear_signoffs')
                                          UNION SELECT 'documentation_signoff') q(f)),
               'allow_no_code', false, 'relays_decision_of', NULL) ORDER BY k->>'from'), '[]'::jsonb) INTO added
      FROM jsonb_array_elements(cfg->'transitions') k
     WHERE k->>'action' = 'audit_kick_back' AND k->>'primitive' = 'return';
    IF jsonb_array_length(added) = 0 THEN
        RETURN;
    END IF;
    cfg := jsonb_set(cfg, ARRAY['transitions'], (cfg->'transitions') || added);
    PERFORM ticket_board.validate_declared_workflow(cfg);
    rev := rev + 1;
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('pgu1022', cfg);
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $layers$;

-- A ticket in Audit whose substantive origin is now unrecorded: the Director is
-- told once, with the repair. Nothing is moved.
DO $$
DECLARE rec record; status jsonb;
BEGIN
    FOR rec IN SELECT t.id, t.title, t.state, t.assignee FROM ticket_board.tickets t
                WHERE t.id IN (SELECT ticket_id FROM syrd719_unrecorded) ORDER BY t.id LOOP
        status := ticket_board.audit_return_status(rec.id);
        IF NOT coalesce((status->>'valid')::boolean, true) THEN
            PERFORM ticket_board.enqueue_notification(
                rec.id, 'ticket_update', 'director',
                format('%s -- %s is in %s (%s), and Audit''s kick-back cannot return it: %s. Its documentation layer '
                       'is kept; nothing was guessed and nothing was moved. Repair: %s.', rec.id, rec.title, rec.state,
                       rec.assignee, status->>'problem', status->>'repair'),
                jsonb_build_object('kind', 'audit_return_missing', 'id', rec.id, 'state', rec.state,
                                   'assignee', rec.assignee, 'target_role', 'director'),
                format('audit_return_layers:%s', rec.id));
        END IF;
    END LOOP;
END $$;

COMMIT;
