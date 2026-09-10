-- SYRD-93: publication as a handoff, for a board that already exists.
--
-- schema.sql is applied once, at install; everything after that reaches a
-- tenant through this directory. What arrives here is the whole board half of
-- publication: the record of what an implementer asked for, the two operations
-- that write it, the vocabulary that lets a document name them, and the control
-- floor that keeps a project from configuring away the only path its work has
-- to the outside.
--
-- It follows pgu929, which restored Director defer and the Backlog stage
-- (SYRD-92). The two functions it re-creates are the current ones, carrying
-- that release's parking floor as well as this one's vocabulary: a migration
-- that re-created an older body would silently roll back the release before it.
-- The function bodies below are copies of the ones in schema.sql, and
-- ticket_board_schema_function_migration_test holds them equal.

BEGIN;


-- A publication ask is a delivered notification like any other, so the queue
-- has to be allowed to carry one. Written as a replace-the-constraint step, the
-- way the awaiting_role kind was added, so it lands the same on a fresh install
-- and on a board that already has a queue full of rows.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder',
                    'awaiting_role', 'publication'));

CREATE TABLE IF NOT EXISTS ticket_board.publication_requests (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    requested_by text NOT NULL,
    ref text NOT NULL,
    commit_hash text NOT NULL CHECK (commit_hash ~ '^[0-9a-f]{40}$'),
    bundle_path text NOT NULL CHECK (bundle_path LIKE '/%'),
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'published', 'rejected', 'superseded')),
    detail text NOT NULL DEFAULT '',
    decided_by text NOT NULL DEFAULT '',
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz
);
CREATE INDEX IF NOT EXISTS publication_requests_ticket_idx
    ON ticket_board.publication_requests (ticket_id, id);
-- One open ask per role per ticket. A second one would leave the Director
-- choosing between two claims about the same work, which is exactly the
-- ambiguity a durable handoff exists to remove.
CREATE UNIQUE INDEX IF NOT EXISTS publication_requests_one_open_idx
    ON ticket_board.publication_requests (ticket_id, requested_by)
    WHERE state = 'requested';

--
-- Refs a role may ask to publish: its own namespace, nothing that integrates.
-- Returned rather than raised so both the request path and the helper can ask
-- the same question and phrase their own refusal.
--
-- Two namespaces are accepted, for a reason that is not cosmetic. `<role>/...`
-- is what the project already publishes, and it works for every role whose name
-- is not also an integration branch. One is: the implementer called `main`
-- cannot publish `main/anything`, because git stores refs as paths and
-- refs/heads/main and refs/heads/main/x cannot both exist. `roles/<role>/...`
-- always works, so no role is left without a way to publish its own work.
--


CREATE OR REPLACE FUNCTION ticket_board.publication_ref_problem(p_ref text, p_role text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    name text := btrim(coalesce(p_ref, ''));
    role_name text := lower(btrim(coalesce(p_role, '')));
    segments text[];
    segment text;
    protected text[] := ARRAY['main', 'master', 'trunk', 'release', 'head'];
BEGIN
    IF name = '' THEN
        RETURN 'a ref name is required';
    END IF;
    IF name LIKE 'refs/%' THEN
        RETURN 'pass a branch name, not a full ref path';
    END IF;
    segments := string_to_array(name, '/');
    FOREACH segment IN ARRAY segments LOOP
        IF segment !~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' OR segment LIKE '%.lock' THEN
            RETURN format('%s is not a valid branch name', name);
        END IF;
    END LOOP;
    -- The whole name, and the directory it would create. refs/heads/main/x
    -- cannot exist beside refs/heads/main, so a first segment that names an
    -- integration branch is refused whether or not it is also a role name.
    IF lower(name) = ANY (protected) OR lower(segments[1]) = ANY (protected) THEN
        RETURN format(
            '%s collides with an integration branch; roles publish their own feature refs '
            'and the control role integrates them. Use roles/%s/<name>.', name, role_name);
    END IF;
    IF segments[1] = 'roles' THEN
        IF array_length(segments, 1) < 3 OR segments[2] <> role_name THEN
            RETURN format('%s may only publish refs under roles/%s/, not %s', role_name, role_name, name);
        END IF;
        RETURN NULL;
    END IF;
    IF array_length(segments, 1) < 2 OR segments[1] <> role_name THEN
        RETURN format(
            '%s may only publish refs under %s/ or roles/%s/, not %s',
            role_name, role_name, role_name, name);
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.publication_control_role()
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    found text;
BEGIN
    SELECT r.name INTO found FROM ticket_board.workflow_roles r
     WHERE (r.definition->>'active')::boolean
       AND r.definition->'capabilities' ?& ticket_board.control_capabilities()
     -- Deterministic when a document gives more than one role control
     -- authority: the one that also declares the operation is the one that can
     -- act on what it is told about.
     ORDER BY (r.definition->'capabilities' ? 'resolve_publication') DESC, r.name
     LIMIT 1;
    -- NULL, not 'director'. A stored document that declares no controller is a
    -- broken document, and routing an ask to a familiar name would hand
    -- publication authority to whoever holds that name (SYRD-93).
    RETURN found;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.publication_await_control(
    p_ticket text,
    p_rearm boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    control_role text := ticket_board.publication_control_role();
    already boolean;
BEGIN
    IF control_role IS NULL THEN
        RAISE EXCEPTION 'this workflow declares no role with control authority'
            USING ERRCODE = '42501';
    END IF;
    SELECT ns.awaiting_role = control_role AND ns.awaiting_since_at IS NOT NULL
      INTO already
      FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = p_ticket;
    IF NOT coalesce(already, false) THEN
        UPDATE ticket_board.ticket_notification_state
           SET awaiting_role = control_role,
               awaiting_since_at = clock_timestamp(),
               last_activity_at = clock_timestamp(),
               nudge_count = 0
         WHERE ticket_id = p_ticket;
    ELSIF p_rearm THEN
        UPDATE ticket_board.ticket_notification_state
           SET awaiting_notified_since_at = NULL
         WHERE ticket_id = p_ticket;
    END IF;
    PERFORM ticket_board.enqueue_awaiting_role_handoff(p_ticket);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.request_publication(
    p_ticket text,
    p_ref text,
    p_commit text,
    p_bundle text
)
RETURNS ticket_board.publication_requests
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller text;
    ticket ticket_board.tickets%ROWTYPE;
    problem text;
    normalized_commit text := lower(btrim(coalesce(p_commit, '')));
    normalized_ref text := btrim(coalesce(p_ref, ''));
    normalized_bundle text := btrim(coalesce(p_bundle, ''));
    existing ticket_board.publication_requests%ROWTYPE;
    created ticket_board.publication_requests%ROWTYPE;
    control_role text;
BEGIN
    -- Admission is the declared capability and nothing else. A board with no
    -- declared workflow has no capabilities to check, and admitting by role
    -- name instead would be exactly the reusable-contract violation this
    -- operation exists to avoid -- so it is refused, not guessed at.
    IF ticket_board.declared_workflow() IS NULL THEN
        RAISE EXCEPTION 'publication requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    actor := ticket_board.require_actor(ARRAY[]::text[], 'request_publication');
    caller := ticket_board.current_app_actor();
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    -- The work being published is the work this role holds. Anything else is a
    -- role asking to publish somebody else's ticket.
    IF ticket.assignee IS DISTINCT FROM caller THEN
        RAISE EXCEPTION '% may only request publication for its own ticket; % is assigned to %',
            caller, ticket.id, ticket.assignee USING ERRCODE = '42501';
    END IF;
    problem := ticket_board.publication_ref_problem(normalized_ref, caller);
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION '%', problem USING ERRCODE = '42501';
    END IF;
    IF normalized_commit !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'publication needs the full 40-character commit, not %', p_commit;
    END IF;
    IF normalized_bundle !~ '^/[^\0]+$' OR normalized_bundle LIKE '%/../%'
       OR normalized_bundle LIKE '%/..' OR normalized_bundle LIKE '%/' THEN
        RAISE EXCEPTION 'bundle must be an absolute path to a file, not %', p_bundle;
    END IF;

    SELECT * INTO existing FROM ticket_board.publication_requests
     WHERE ticket_id = ticket.id AND requested_by = caller AND state = 'requested'
     FOR UPDATE;
    IF FOUND THEN
        IF existing.ref = normalized_ref
           AND existing.commit_hash = normalized_commit
           AND existing.bundle_path = normalized_bundle THEN
            -- The same ask again: a retry after an interruption, or a lost
            -- notification. Re-arm the handoff, record nothing new.
            PERFORM ticket_board.publication_await_control(ticket.id, true);
            RETURN existing;
        END IF;
        -- The role amended its work. Both asks stay on the record, and the one
        -- the Director sees is the current one.
        UPDATE ticket_board.publication_requests
           SET state = 'superseded',
               decided_at = clock_timestamp(),
               decided_by = caller,
               detail = format('superseded by a later request for %s at %s',
                               normalized_ref, left(normalized_commit, 12))
         WHERE id = existing.id;
    END IF;

    INSERT INTO ticket_board.publication_requests
        (ticket_id, requested_by, ref, commit_hash, bundle_path)
    VALUES (ticket.id, caller, normalized_ref, normalized_commit, normalized_bundle)
    RETURNING * INTO created;

    control_role := ticket_board.publication_control_role();
    IF control_role IS NULL THEN
        RAISE EXCEPTION
            'this workflow declares no role with control authority, so no publication could be answered'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.publication_await_control(ticket.id, false);
    PERFORM ticket_board.enqueue_notification(
        ticket.id,
        'publication',
        control_role,
        format('%s -- %s: %s asks to publish %s at %s. Read the ticket, then publish or reject with a reason.',
               ticket.id, ticket.title, caller, normalized_ref, left(normalized_commit, 12)),
        jsonb_build_object(
            'kind', 'publication_request', 'id', ticket.id, 'request_id', created.id,
            'role', caller, 'ref', normalized_ref, 'commit', normalized_commit),
        format('publication:%s:%s', created.id, 'requested'));
    PERFORM ticket_board.add_comment(
        ticket.id,
        format('Publication requested: %s at %s. Awaiting the control role; no submission until it lands.',
               normalized_ref, left(normalized_commit, 12)));
    RETURN created;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.resolve_publication(
    p_request bigint,
    p_outcome text,
    p_detail text
)
RETURNS ticket_board.publication_requests
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller text;
    request ticket_board.publication_requests%ROWTYPE;
    ticket ticket_board.tickets%ROWTYPE;
    outcome text := lower(btrim(coalesce(p_outcome, '')));
    note text := btrim(coalesce(p_detail, ''));
    updated ticket_board.publication_requests%ROWTYPE;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        RAISE EXCEPTION 'publication requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    actor := ticket_board.require_actor(ARRAY[]::text[], 'resolve_publication');
    caller := ticket_board.current_app_actor();
    -- The capability admits the caller; control authority is what decides. A
    -- tenant that hands this capability to a second role still gets one
    -- publisher.
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'only the control role may resolve a publication request, not %', caller
            USING ERRCODE = '42501';
    END IF;
    IF outcome NOT IN ('published', 'rejected') THEN
        RAISE EXCEPTION 'publication outcome must be published or rejected, not %', p_outcome;
    END IF;
    IF outcome = 'rejected' AND note = '' THEN
        RAISE EXCEPTION 'rejecting a publication request requires a reason';
    END IF;
    SELECT * INTO request FROM ticket_board.publication_requests
     WHERE id = p_request FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'publication request % not found', p_request;
    END IF;
    IF request.state = outcome THEN
        -- Already recorded. A helper that pushed and then lost its connection
        -- re-runs safely rather than reporting a failure it did not have.
        RETURN request;
    END IF;
    IF request.state <> 'requested' THEN
        RAISE EXCEPTION 'publication request % is already %', request.id, request.state;
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = request.ticket_id FOR UPDATE;

    UPDATE ticket_board.publication_requests
       SET state = outcome,
           detail = note,
           decided_by = caller,
           decided_at = clock_timestamp()
     WHERE id = request.id
    RETURNING * INTO updated;

    UPDATE ticket_board.ticket_notification_state
       SET awaiting_role = '',
           awaiting_since_at = NULL,
           last_activity_at = clock_timestamp(),
           nudge_count = 0
     WHERE ticket_id = request.ticket_id;

    PERFORM ticket_board.enqueue_notification(
        request.ticket_id,
        'publication',
        request.requested_by,
        CASE WHEN outcome = 'published'
             THEN format('%s -- %s: %s is published at %s. Submit when you are ready.',
                         request.ticket_id, ticket.title, request.ref,
                         left(request.commit_hash, 12))
             ELSE format('%s -- %s: publication of %s was rejected: %s',
                         request.ticket_id, ticket.title, request.ref, note)
        END,
        jsonb_build_object(
            'kind', 'publication_' || outcome, 'id', request.ticket_id,
            'request_id', request.id, 'ref', request.ref,
            'commit', request.commit_hash, 'detail', note),
        format('publication:%s:%s', request.id, outcome));
    PERFORM ticket_board.add_comment(
        request.ticket_id,
        CASE WHEN outcome = 'published'
             THEN format('Published %s at %s.%s', request.ref, left(request.commit_hash, 12),
                         CASE WHEN note = '' THEN '' ELSE ' ' || note END)
             ELSE format('Publication of %s refused: %s', request.ref, note)
        END);
    PERFORM ticket_board.touch_ticket(request.ticket_id);
    RETURN updated;
END;
$$;

-- The control floor, with the operation that answers an ask in it: a project
-- may configure its own pipeline, but not a controller who cannot publish.
CREATE OR REPLACE FUNCTION ticket_board.director_control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'reassign', 'set_manually_controlled', 'set_blockers',
        'merge', 'edit_fields', 'dismiss_notification', 'director_edit',
        'resolve_publication'
    ]::text[];
$$;

-- The declared vocabulary, so a document may name either half at all. This
-- body is the current one: it keeps SYRD-92's parking floor, which a copy
-- taken before that release would have quietly removed.
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
                'reassign','director_edit','request_publication','resolve_publication')) THEN
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


-- Bring an existing tenant's document up to the new vocabulary rather than
-- leaving its roles unable to name what the board now does. Without this, every
-- implementer on an upgraded board is refused at the handler before
-- ticket_board.request_publication runs, and the control role cannot answer.
--
-- Who gets what is derived, not named: control authority is the capabilities
-- that define it (SYRD-49), and the ask goes to the roles the document itself
-- calls implementers. Marked, so a tenant that later removes one is not
-- silently overruled on the next upgrade.
DO $migration$
DECLARE cfg jsonb; roles jsonb; rev bigint; r jsonb;
BEGIN
    IF to_regclass('ticket_board.workflow_configuration') IS NULL THEN RETURN; END IF;
    SELECT document INTO cfg FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    IF coalesce((cfg->'migrations'->>'publication_capabilities')::boolean, false) THEN
        RETURN;
    END IF;
    SELECT jsonb_agg(
        CASE
            WHEN entry.value->'capabilities' ?& ARRAY['set_manually_controlled', 'merge']::text[]
                 AND NOT entry.value->'capabilities' ? 'resolve_publication'
                THEN jsonb_set(entry.value, '{capabilities}',
                         (entry.value->'capabilities') || '["resolve_publication"]'::jsonb)
            WHEN entry.value->>'kind' = 'implementer'
                 AND NOT entry.value->'capabilities' ? 'request_publication'
                THEN jsonb_set(entry.value, '{capabilities}',
                         (entry.value->'capabilities') || '["request_publication"]'::jsonb)
            ELSE entry.value
        END
        ORDER BY entry.ordinality)
    INTO roles
    FROM jsonb_array_elements(cfg->'roles') WITH ORDINALITY AS entry(value, ordinality);
    -- A tenant that already names both halves -- a fresh install, or one whose
    -- Director configured them -- is left exactly as it is. Writing a revision
    -- that changes nothing would look like somebody edited the workflow.
    IF roles IS NOT DISTINCT FROM cfg->'roles' THEN
        RETURN;
    END IF;
    cfg := jsonb_set(cfg, '{roles}', roles);
    cfg := jsonb_set(
        cfg,
        '{migrations}',
        coalesce(cfg->'migrations', '{}'::jsonb) || jsonb_build_object('publication_capabilities', true));
    -- The current validator, which is this migration's own: every floor the
    -- releases before it added has to still hold after this one runs.
    PERFORM ticket_board.validate_declared_workflow(cfg);
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('migration', cfg) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    FOR r IN SELECT value FROM jsonb_array_elements(roles) LOOP
        UPDATE ticket_board.workflow_roles SET definition = r WHERE name = r->>'name';
    END LOOP;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $migration$;

-- The board's writer may record an ask and record a decision. It cannot push:
-- nothing granted here is a credential, and the program that holds one runs as
-- another account entirely.
--
-- Which role the writer is cannot be spelled -- a per-project board runs under
-- its own generated role name -- so the grant follows the privilege that
-- already identifies it: execute on force_move, which rbac.sql gives to the
-- writer and to nobody else.
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
            'GRANT EXECUTE ON FUNCTION ticket_board.request_publication(text, text, text, text) TO %I', writer);
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.resolve_publication(bigint, text, text) TO %I', writer);
        EXECUTE format('GRANT SELECT ON ticket_board.publication_requests TO %I', writer);
    END LOOP;
END $grants$;

COMMIT;
