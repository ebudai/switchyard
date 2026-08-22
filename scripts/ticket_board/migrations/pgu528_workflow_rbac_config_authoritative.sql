ALTER TABLE ticket_board.workflow_transitions
    ADD COLUMN IF NOT EXISTS director_override boolean NOT NULL DEFAULT false;

UPDATE ticket_board.workflow_transitions
SET director_override = (action_name = 'start_task');

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_allowed_config(
    p_action_name text,
    p_actor text,
    p_ticket_assignee text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_transitions wt
        WHERE wt.action_name = p_action_name
          AND p_actor = ANY(wt.allowed_roles)
          AND (
              NOT wt.owner_scoped
              OR p_actor = p_ticket_assignee
              OR (wt.director_override AND p_actor = 'director')
          )
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_owner_scoped_config(
    p_action_name text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(bool_or(wt.owner_scoped), false)
    FROM ticket_board.workflow_transitions wt
    WHERE wt.action_name = p_action_name;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_director_override_config(
    p_action_name text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(bool_or(wt.director_override), false)
    FROM ticket_board.workflow_transitions wt
    WHERE wt.action_name = p_action_name;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_workflow_transition_actor(
    p_action_name text,
    p_ticket_id text DEFAULT NULL,
    p_ticket_assignee text DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    resolved_assignee text := p_ticket_assignee;
BEGIN
    actor := ticket_board.current_app_actor();
    IF actor IS NULL THEN
        RAISE EXCEPTION 'missing ticket_board.caller_role for %', p_action_name
            USING ERRCODE = '42501';
    END IF;

    IF resolved_assignee IS NULL AND p_ticket_id IS NOT NULL THEN
        SELECT t.assignee
        INTO resolved_assignee
        FROM ticket_board.tickets t
        WHERE t.id = p_ticket_id;
    END IF;

    IF NOT ticket_board.workflow_transition_rbac_allowed_config(
        p_action_name,
        actor,
        resolved_assignee
    ) THEN
        RAISE EXCEPTION 'role % cannot call %', actor, p_action_name
            USING ERRCODE = '42501';
    END IF;

    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.log_workflow_transition_rbac_shadow_mismatch(
    p_ticket_id text,
    p_action_name text,
    p_actor text,
    p_ticket_assignee text,
    p_hardcoded_allowed boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    resolved_assignee text := p_ticket_assignee;
    config_allowed boolean;
    config_owner_scoped boolean;
BEGIN
    IF resolved_assignee IS NULL AND p_ticket_id IS NOT NULL THEN
        SELECT t.assignee
        INTO resolved_assignee
        FROM ticket_board.tickets t
        WHERE t.id = p_ticket_id;
    END IF;

    config_allowed := ticket_board.workflow_transition_rbac_allowed_config(
        p_action_name,
        p_actor,
        resolved_assignee
    );
    config_owner_scoped := ticket_board.workflow_transition_rbac_owner_scoped_config(p_action_name);

    IF p_hardcoded_allowed IS DISTINCT FROM config_allowed THEN
        INSERT INTO ticket_board.workflow_transition_rbac_shadow_log (
            ticket_id,
            action_name,
            actor,
            ticket_assignee,
            hardcoded_allowed,
            config_allowed,
            owner_scoped
        ) VALUES (
            p_ticket_id,
            p_action_name,
            p_actor,
            resolved_assignee,
            p_hardcoded_allowed,
            config_allowed,
            config_owner_scoped
        );
        RAISE WARNING 'workflow transition RBAC shadow mismatch for % action=% actor=% assignee=% hardcoded=% config=% owner_scoped=%',
            p_ticket_id,
            p_action_name,
            p_actor,
            resolved_assignee,
            p_hardcoded_allowed,
            config_allowed,
            config_owner_scoped;
    END IF;
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
    actor := nullif(current_setting('ticket_board.caller_role', true), '');
    IF actor IS NULL THEN
        RETURN ticket_board.current_actor_role();
    END IF;
    IF actor NOT IN ('director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research') THEN
        RAISE EXCEPTION 'invalid ticket_board.caller_role: %', actor;
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.utc_text(p_ts timestamptz)
RETURNS text
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT to_char(p_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"');
$$;

CREATE OR REPLACE FUNCTION ticket_board.next_ticket_id()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_number integer;
BEGIN
    LOCK TABLE ticket_board.tickets IN EXCLUSIVE MODE;
    SELECT COALESCE(max(ticket_number), 0) + 1
    INTO next_number
    FROM ticket_board.tickets;
    RETURN 'PGU-' || next_number::text;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.build_ticket_source_json(p_ticket_id text)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    ticket_row ticket_board.tickets%ROWTYPE;
    blocked_by jsonb;
    blockers jsonb;
    comments jsonb;
    screenshots jsonb;
BEGIN
    SELECT *
    INTO ticket_row
    FROM ticket_board.tickets
    WHERE id = p_ticket_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    SELECT COALESCE(jsonb_agg(blocker_ticket_id ORDER BY position), '[]'::jsonb)
    INTO blocked_by
    FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id
      AND NOT resolved;

    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object('id', blocker_ticket_id, 'resolved', resolved)
            ORDER BY position
        ),
        '[]'::jsonb
    )
    INTO blockers
    FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id;

    SELECT COALESCE(
        jsonb_agg(
            jsonb_build_object('who', who, 'ts', ts_text, 'text', text, 'urgent', urgent)
            ORDER BY position
        ),
        '[]'::jsonb
    )
    INTO comments
    FROM ticket_board.ticket_comments
    WHERE ticket_id = p_ticket_id;

    SELECT COALESCE(jsonb_agg(path ORDER BY position), '[]'::jsonb)
    INTO screenshots
    FROM ticket_board.ticket_attachments
    WHERE ticket_id = p_ticket_id;

    RETURN jsonb_build_object(
        'id', ticket_row.id,
        'title', ticket_row.title,
        'body', ticket_row.body,
        'state', ticket_row.state,
        'assignee', ticket_row.assignee,
        'blocked_by', blocked_by,
        'blockers', blockers,
        'parent_id', ticket_row.parent_id,
        'blocked_reason', ticket_row.blocked_reason,
        'implementation', ticket_row.implementation,
        'audit_prompt', ticket_row.audit_prompt,
        'audit_signoff', ticket_row.audit_signoff,
        'needs_inspection', ticket_row.needs_inspection,
        'inspector_signoff', ticket_row.inspector_signoff,
        'needs_user_signoff', ticket_row.needs_user_signoff,
        'user_signoff', ticket_row.user_signoff,
        'commit_hash', ticket_row.commit_hash,
        'commit_exempt', ticket_row.commit_exempt,
        'regression', ticket_row.regression,
        'manually_controlled', ticket_row.manually_controlled,
        'parked', ticket_row.parked,
        'screenshot', ticket_row.screenshot,
        'screenshots', screenshots,
        'comments', comments,
        'created', ticket_row.created_text,
        'updated', ticket_row.updated_text
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.refresh_ticket_source_json(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    UPDATE ticket_board.tickets
    SET source_json = ticket_board.build_ticket_source_json(p_ticket_id),
        row_updated_at = now()
    WHERE id = p_ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.touch_ticket(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    updated_at_value timestamptz := clock_timestamp();
    updated_text_value text := ticket_board.utc_text(updated_at_value);
BEGIN
    UPDATE ticket_board.tickets
    SET updated_text = updated_text_value,
        updated_at = updated_at_value,
        row_updated_at = now()
    WHERE id = p_ticket_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    PERFORM ticket_board.refresh_ticket_source_json(p_ticket_id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.append_ticket_comment(
    p_id text,
    p_who text,
    p_text text,
    p_urgent boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_position integer;
    ts_value timestamptz := clock_timestamp();
    ts_text_value text := ticket_board.utc_text(ts_value);
BEGIN
    IF btrim(coalesce(p_text, '')) = '' THEN
        RAISE EXCEPTION 'comment text must be non-empty';
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_id;
    END IF;

    SELECT COALESCE(max(position), -1) + 1
    INTO next_position
    FROM ticket_board.ticket_comments
    WHERE ticket_id = p_id;

    INSERT INTO ticket_board.ticket_comments (
        ticket_id,
        position,
        who,
        ts_text,
        ts,
        text,
        urgent,
        source_json
    ) VALUES (
        p_id,
        next_position,
        p_who,
        ts_text_value,
        ts_value,
        p_text,
        coalesce(p_urgent, false),
        jsonb_build_object('who', p_who, 'ts', ts_text_value, 'text', p_text, 'urgent', coalesce(p_urgent, false))
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.apply_blockers(
    p_ticket_id text,
    p_blocker_ids text[],
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    normalized_reason text := coalesce(p_reason, '');
    blocker_count integer;
    invalid_id text;
    missing_id text;
    cycle_id text;
BEGIN
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT coalesce(nullif(upper(btrim(raw_id)), ''), '<empty>')
    INTO invalid_id
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    WHERE raw_id IS NULL
       OR btrim(raw_id) = ''
       OR upper(btrim(raw_id)) !~ '^PGU-[0-9]+$'
       OR upper(btrim(raw_id)) = p_ticket_id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = p_ticket_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    WITH normalized AS (
        SELECT upper(btrim(raw_id)) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY upper(btrim(raw_id))
    )
    SELECT normalized.blocker_id
    INTO missing_id
    FROM normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    WHERE blocker.id IS NULL
    LIMIT 1;
    IF missing_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocker ticket not found: %', missing_id;
    END IF;

    WITH RECURSIVE normalized AS (
        SELECT upper(btrim(raw_id)) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY upper(btrim(raw_id))
    ),
    blocker_chain(ticket_id) AS (
        SELECT blocker_id
        FROM normalized
        UNION
        SELECT tb.blocker_ticket_id
        FROM blocker_chain chain
        JOIN ticket_board.ticket_blockers tb ON tb.ticket_id = chain.ticket_id
    )
    SELECT ticket_id
    INTO cycle_id
    FROM blocker_chain
    WHERE ticket_id = p_ticket_id
    LIMIT 1;
    IF cycle_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocked_by cycle detected for %', p_ticket_id;
    END IF;

    DELETE FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id;

    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
    SELECT p_ticket_id, normalized.blocker_id, min(normalized.ord)::integer - 1, bool_or(blocker.state IN ('done', 'cancelled'))
    FROM (
        SELECT upper(btrim(raw_id)) AS blocker_id, ord
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    GROUP BY normalized.blocker_id
    ORDER BY min(normalized.ord);

    UPDATE ticket_board.tickets
    SET blocked_reason = normalized_reason
    WHERE tickets.id = p_ticket_id;

    PERFORM ticket_board.refresh_ticket_source_json(p_ticket_id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    blocked_by text[],
    blocked_reason text,
    needs_user_signoff boolean
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_id text;
    normalized_state text := btrim(coalesce(initial_state, 'analysis'));
    normalized_assignee text := 'unassigned';
    normalized_parked boolean := false;
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'user'], 'create_ticket');
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_state = '' THEN
        normalized_state := 'analysis';
    END IF;
    IF normalized_state NOT IN ('draft', 'analysis', 'backlog') THEN
        RAISE EXCEPTION 'invalid create state: %; allowed: draft, analysis, backlog', normalized_state;
    END IF;
    IF normalized_state = 'backlog' THEN
        normalized_parked := true;
    END IF;
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(blocked_by, ARRAY[]::text[])) AS raw_id;

    ticket_id := ticket_board.next_ticket_id();
    IF blocker_count > 0 THEN
        PERFORM set_config('ticket_board.suppress_create_notify', 'on', true);
    END IF;
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parked,
        needs_user_signoff,
        created_text,
        updated_text,
        created_at,
        updated_at,
        source_json,
        source_file_name
    ) VALUES (
        ticket_id,
        btrim(title),
        coalesce(body, ''),
        normalized_state,
        normalized_assignee,
        normalized_parked,
        coalesce(needs_user_signoff, false),
        created_text_value,
        created_text_value,
        created_at_value,
        created_at_value,
        jsonb_build_object(
            'id', ticket_id,
            'title', btrim(title),
            'body', coalesce(body, ''),
            'state', normalized_state,
            'assignee', normalized_assignee,
            'parked', normalized_parked,
            'needs_user_signoff', coalesce(needs_user_signoff, false),
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    IF blocker_count > 0 THEN
        PERFORM ticket_board.apply_blockers(ticket_id, blocked_by, blocked_reason);
    END IF;
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    blocked_by text[],
    blocked_reason text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, blocked_by, blocked_reason, false);
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, ARRAY[]::text[], '');
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, 'analysis');
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_id text;
    source_id text := upper(btrim(coalesce(source_ticket_id, '')));
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor = 'ticket_board_service' THEN
        PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research', 'audit'], 'file_bug');
        actor := ticket_board.current_app_actor();
    END IF;
    IF actor NOT IN ('ticket_board_service', 'main', 'app', 'ops', 'perf', 'research', 'audit') THEN
        RAISE EXCEPTION 'role % cannot call file_bug', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF source_id !~ '^PGU-[0-9]+$' THEN
        RAISE EXCEPTION 'source_ticket_id must look like PGU-N';
    END IF;
    PERFORM 1 FROM ticket_board.tickets WHERE id = source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source_ticket_id ticket not found: %', source_id;
    END IF;

    ticket_id := ticket_board.next_ticket_id();
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parent_id,
        created_text,
        updated_text,
        created_at,
        updated_at,
        source_json,
        source_file_name
    ) VALUES (
        ticket_id,
        btrim(title),
        coalesce(body, ''),
        'analysis',
        'unassigned',
        source_id,
        created_text_value,
        created_text_value,
        created_at_value,
        created_at_value,
        jsonb_build_object(
            'id', ticket_id,
            'title', btrim(title),
            'body', coalesce(body, ''),
            'state', 'analysis',
            'assignee', 'unassigned',
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.route(
    id text,
    new_state text,
    assignee text
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
BEGIN
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'user_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'agent', 'director', 'research') THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = route.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('route', id, current_assignee);
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;

    UPDATE ticket_board.tickets
    SET state = new_state,
        assignee = route.assignee,
        parked = false
    WHERE tickets.id = route.id;
    PERFORM ticket_board.touch_ticket(id);
    IF current_state IS NOT DISTINCT FROM new_state AND current_assignee IS DISTINCT FROM assignee THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'assignee');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_draft(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_state text;
BEGIN
    SELECT tickets.state
    INTO current_state
    FROM ticket_board.tickets
    WHERE tickets.id = release_draft.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('release_draft', id);
    IF current_state <> 'draft' THEN
        RAISE EXCEPTION 'release_draft requires a draft ticket';
    END IF;

    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'unassigned',
        parked = false
    WHERE tickets.id = release_draft.id;
    PERFORM ticket_board.touch_ticket(id);
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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'user_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'agent', 'director', 'research') THEN
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
    IF new_state = 'in_progress'
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

CREATE OR REPLACE FUNCTION ticket_board.start_work(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('start_work', id);
    UPDATE ticket_board.tickets
    SET state = 'in_progress'
    WHERE tickets.id = start_work.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.submit_to_audit(
    id text,
    commit_hash text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_commit text := btrim(coalesce(commit_hash, ''));
    ticket_commit_exempt boolean;
    ticket_last_rejected_commit text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('submit_to_audit', id);
    SELECT tickets.commit_exempt, tickets.last_rejected_commit
    INTO ticket_commit_exempt, ticket_last_rejected_commit
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_audit.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF normalized_commit <> '' AND normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF normalized_commit = '' AND NOT ticket_commit_exempt THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF ticket_last_rejected_commit IS NOT NULL
       AND ticket_last_rejected_commit <> ''
       AND normalized_commit = ticket_last_rejected_commit THEN
        RAISE EXCEPTION 'cannot submit: commit % was just kicked back with no change -- do the fix and submit a NEW commit.',
            coalesce(nullif(ticket_last_rejected_commit, ''), '<none>');
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'audit',
        commit_hash = normalized_commit,
        inspector_signoff = false,
        last_rejected_commit = NULL
    WHERE tickets.id = submit_to_audit.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.submit_to_inspection(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_state text;
    ticket_assignee text;
    ticket_needs_inspection boolean;
BEGIN
    SELECT tickets.state, tickets.assignee, tickets.needs_inspection
    INTO ticket_state, ticket_assignee, ticket_needs_inspection
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_inspection.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('submit_to_inspection', id, ticket_assignee);
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'submit_to_inspection requires an in_progress ticket';
    END IF;
    IF NOT ticket_needs_inspection THEN
        RAISE EXCEPTION 'submit_to_inspection requires needs_inspection=true';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'inspection',
        assignee = 'inspector'
    WHERE tickets.id = submit_to_inspection.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.request_commit_exempt(
    id text,
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
    ticket_assignee text;
    ticket_state text;
    ticket_commit_exempt boolean;
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'request_commit_exempt requires a non-empty reason';
    END IF;

    SELECT tickets.assignee, tickets.state, tickets.commit_exempt
    INTO ticket_assignee, ticket_state, ticket_commit_exempt
    FROM ticket_board.tickets
    WHERE tickets.id = request_commit_exempt.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('request_commit_exempt', id, ticket_assignee);
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'request_commit_exempt requires an in_progress ticket';
    END IF;
    IF ticket_commit_exempt THEN
        RAISE EXCEPTION 'ticket is already commit_exempt';
    END IF;

    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Commit exemption requested: ' || normalized_reason
    );
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'unassigned',
        parked = false
    WHERE tickets.id = request_commit_exempt.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.start_task(
    id text,
    note text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
    ticket_state text;
    ticket_assignee text;
    normalized_note text := btrim(coalesce(note, ''));
BEGIN
    SELECT tickets.state, tickets.assignee
    INTO ticket_state, ticket_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = start_task.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    comment_actor := ticket_board.require_workflow_transition_actor('start_task', id, ticket_assignee);
    IF ticket_state <> 'backlog' THEN
        RAISE EXCEPTION 'start_task requires a backlog ticket';
    END IF;
    IF normalized_note <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_note);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        parked = false
    WHERE tickets.id = start_task.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.complete_task(
    id text,
    completion_note text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
    ticket_state text;
    ticket_assignee text;
    normalized_note text := btrim(coalesce(completion_note, ''));
    blocker_id text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector'],
        'complete_task'
    );
    comment_actor := ticket_board.current_app_actor();
    IF normalized_note = '' THEN
        RAISE EXCEPTION 'complete_task requires a non-empty completion_note';
    END IF;
    SELECT tickets.state, tickets.assignee
    INTO ticket_state, ticket_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = complete_task.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF ticket_assignee <> comment_actor AND comment_actor <> 'director' THEN
        RAISE EXCEPTION '% cannot call complete_task for ticket assigned to %', comment_actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    IF ticket_state NOT IN ('backlog', 'analysis') THEN
        RAISE EXCEPTION 'complete_task requires a backlog or analysis ticket';
    END IF;
    IF ticket_board.ticket_has_unresolved_blockers(id) THEN
        SELECT b.blocker_ticket_id
        INTO blocker_id
        FROM ticket_board.ticket_blockers b
        WHERE b.ticket_id = complete_task.id
          AND NOT b.resolved
        ORDER BY b.position
        LIMIT 1;
        RAISE EXCEPTION 'unresolved blocker prevents task completion: %', blocker_id;
    END IF;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_note);
    PERFORM set_config('ticket_board.utility_task_complete', 'on', true);
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_exempt = true,
        commit_hash = '',
        last_rejected_commit = NULL,
        parked = false
    WHERE tickets.id = complete_task.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

DROP FUNCTION IF EXISTS ticket_board.audit_sign_off(text);
CREATE OR REPLACE FUNCTION ticket_board.audit_sign_off(
    id text,
    comment_text text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('audit_sign_off', id);
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    UPDATE ticket_board.tickets
    SET audit_signoff = true
    WHERE tickets.id = audit_sign_off.id
      AND tickets.state = 'audit';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.audit_kick_back(
    id text,
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
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('audit_kick_back', id);
    IF btrim(coalesce(reason, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        assignee = ticket_board.ticket_kickback_target_assignee(audit_kick_back.id),
        last_rejected_commit = commit_hash
    WHERE tickets.id = audit_kick_back.id
      AND tickets.state = 'audit';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.audit_kick_back(
    id text,
    reason text,
    target_assignee text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('audit_kick_back', id);
    IF btrim(coalesce(reason, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        assignee = ticket_board.ticket_kickback_target_assignee(audit_kick_back.id, target_assignee),
        last_rejected_commit = commit_hash
    WHERE tickets.id = audit_kick_back.id
      AND tickets.state = 'audit';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.inspector_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('inspector_sign_off', id);
    UPDATE ticket_board.tickets
    SET inspector_signoff = true,
        state = 'audit'
    WHERE tickets.id = inspector_sign_off.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.inspector_kick_back(
    id text,
    recommendations text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('inspector_kick_back', id);
    IF btrim(coalesce(recommendations, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, recommendations);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        assignee = ticket_board.ticket_kickback_target_assignee(inspector_kick_back.id),
        inspector_signoff = false,
        last_rejected_commit = commit_hash
    WHERE tickets.id = inspector_kick_back.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.inspector_kick_back(
    id text,
    recommendations text,
    target_assignee text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('inspector_kick_back', id);
    IF btrim(coalesce(recommendations, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, recommendations);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        assignee = ticket_board.ticket_kickback_target_assignee(inspector_kick_back.id, target_assignee),
        inspector_signoff = false,
        last_rejected_commit = commit_hash
    WHERE tickets.id = inspector_kick_back.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.user_sign_off(
    id text,
    comment_text text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('user_sign_off', id);
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
    UPDATE ticket_board.tickets
    SET user_signoff = true
    WHERE tickets.id = user_sign_off.id
      AND tickets.state = 'user_review';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'user_review ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.user_reopen(
    id text,
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
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('user_reopen', id);
    IF btrim(coalesce(reason, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'analysis'
    WHERE tickets.id = user_reopen.id
      AND tickets.state IN ('user_review', 'director_review', 'done');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reviewed ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.mark_done(
    id text,
    commit_hash text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_commit text := btrim(coalesce(commit_hash, ''));
    ticket_commit_exempt boolean;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('mark_done', id);
    SELECT tickets.commit_exempt
    INTO ticket_commit_exempt
    FROM ticket_board.tickets
    WHERE tickets.id = mark_done.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF normalized_commit <> '' AND normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF normalized_commit = '' AND NOT ticket_commit_exempt THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_hash = normalized_commit,
        last_rejected_commit = NULL
    WHERE tickets.id = mark_done.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.defer(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('defer', id);
    UPDATE ticket_board.tickets
    SET state = 'backlog',
        parked = true
    WHERE tickets.id = defer.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.cancel(
    id text,
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
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('cancel', id);
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    UPDATE ticket_board.tickets
    SET state = 'cancelled',
        parked = false
    WHERE tickets.id = cancel.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

