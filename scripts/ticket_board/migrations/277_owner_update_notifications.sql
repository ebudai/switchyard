-- Notify active ticket owners when someone else edits or comments without changing state.

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;

ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation'));

CREATE OR REPLACE FUNCTION ticket_board.ticket_update_message(
    p_ticket_id text,
    p_title text,
    p_state text,
    p_actor text,
    p_change_summary text DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    title_suffix text := CASE WHEN coalesce(p_title, '') <> '' THEN ' -- ' || p_title ELSE '' END;
    summary_suffix text := nullif(btrim(coalesce(p_change_summary, '')), '');
BEGIN
    RETURN
        p_ticket_id
        || title_suffix
        || ' (in '
        || p_state
        || ', that you own) was changed by '
        || p_actor
        || CASE
            WHEN summary_suffix IS NULL THEN ''
            ELSE ': ' || summary_suffix
        END
        || ' -- re-read it before continuing.';
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_owner_in_place_change(
    p_ticket_id text,
    p_change_summary text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    ticket_row ticket_board.tickets%ROWTYPE;
    actor text;
    target_role text;
    change_summary text := nullif(btrim(coalesce(p_change_summary, '')), '');
    message text;
BEGIN
    actor := ticket_board.current_app_actor();
    SELECT *
    INTO ticket_row
    FROM ticket_board.tickets
    WHERE id = p_ticket_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    target_role := ticket_board.transition_target_role(ticket_row.state, ticket_row.assignee);
    IF target_role IS NULL OR actor IS NULL OR actor = target_role THEN
        RETURN;
    END IF;

    message := ticket_board.ticket_update_message(
        p_ticket_id,
        ticket_row.title,
        ticket_row.state,
        actor,
        change_summary
    );

    PERFORM ticket_board.enqueue_notification(
        p_ticket_id,
        'ticket_update',
        target_role,
        message,
        jsonb_build_object(
            'kind', 'ticket_update',
            'id', p_ticket_id,
            'title', ticket_row.title,
            'state', ticket_row.state,
            'assignee', ticket_row.assignee,
            'updated_at', ticket_row.updated_at,
            'ticket_number', ticket_row.ticket_number,
            'target_role', target_role,
            'message', message,
            'actor', actor,
            'change_summary', change_summary
        ),
        'ticket_update:' || p_ticket_id || ':' || pg_current_xact_id()::text
    );
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
    actor := ticket_board.require_actor(ARRAY['director'], 'route');
    IF new_state NOT IN ('backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'eric_review', 'director_review', 'done', 'cancelled') THEN
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

CREATE OR REPLACE FUNCTION ticket_board.set_blockers(
    id text,
    ids text[],
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
    current_blockers text[];
    current_reason text;
    next_blockers text[];
    next_reason text := coalesce(reason, '');
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    SELECT tickets.blocked_reason
    INTO current_reason
    FROM ticket_board.tickets
    WHERE tickets.id = set_blockers.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    SELECT COALESCE(array_agg(blocker_ticket_id ORDER BY position), ARRAY[]::text[])
    INTO current_blockers
    FROM ticket_board.ticket_blockers
    WHERE ticket_id = set_blockers.id;

    SELECT COALESCE(array_agg(blocker_id ORDER BY first_ord), ARRAY[]::text[])
    INTO next_blockers
    FROM (
        SELECT upper(btrim(raw_id)) AS blocker_id, min(ord) AS first_ord
        FROM unnest(coalesce(ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
        GROUP BY upper(btrim(raw_id))
    ) AS normalized;

    PERFORM ticket_board.apply_blockers(id, ids, reason);
    PERFORM ticket_board.touch_ticket(id);
    IF current_blockers IS DISTINCT FROM next_blockers OR coalesce(current_reason, '') IS DISTINCT FROM next_reason THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'blockers');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text
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
    actor := ticket_board.require_actor(ARRAY['director', 'eric', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text);
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.edit_fields(
    id text,
    patch jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    invalid_field text;
    current_ticket ticket_board.tickets%ROWTYPE;
    normalized_parent text;
    current_parent text;
    changed_fields text[] := ARRAY[]::text[];
    seen text[];
    screenshot_value text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'eric', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'edit_fields'
    );
    IF patch IS NULL OR jsonb_typeof(patch) <> 'object' THEN
        RAISE EXCEPTION 'edit_fields patch must be an object';
    END IF;

    SELECT key
    INTO invalid_field
    FROM jsonb_object_keys(patch) AS key
    WHERE key NOT IN (
        'title',
        'body',
        'parent_id',
        'screenshots',
        'screenshot',
        'implementation',
        'audit_prompt',
        'needs_inspection',
        'needs_eric_signoff',
        'commit_exempt',
        'commit_hash',
        'audit_signoff',
        'inspector_signoff',
        'eric_signoff'
    )
    ORDER BY key
    LIMIT 1;
    IF invalid_field IS NOT NULL THEN
        RAISE EXCEPTION 'edit_fields cannot update: %', invalid_field;
    END IF;
    IF patch @> '{"audit_signoff": true}'::jsonb THEN
        RAISE EXCEPTION 'audit_signoff=true requires audit_sign_off';
    END IF;
    IF patch @> '{"inspector_signoff": true}'::jsonb THEN
        RAISE EXCEPTION 'inspector_signoff=true requires inspector_sign_off';
    END IF;
    IF patch @> '{"eric_signoff": true}'::jsonb THEN
        RAISE EXCEPTION 'eric_signoff=true requires eric_sign_off';
    END IF;
    IF patch ? 'needs_inspection' AND ticket_board.current_app_actor() <> 'director' THEN
        RAISE EXCEPTION 'needs_inspection can only be edited by director' USING ERRCODE = '42501';
    END IF;
    IF patch ? 'commit_exempt' AND ticket_board.current_app_actor() <> 'director' THEN
        RAISE EXCEPTION 'commit_exempt can only be edited by director' USING ERRCODE = '42501';
    END IF;
    SELECT *
    INTO current_ticket
    FROM ticket_board.tickets
    WHERE tickets.id = edit_fields.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    IF patch ? 'title' AND btrim(coalesce(patch->>'title', '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF patch ? 'title' AND btrim(patch->>'title') IS DISTINCT FROM current_ticket.title THEN
        changed_fields := array_append(changed_fields, 'title');
    END IF;
    IF patch ? 'body' AND coalesce(patch->>'body', '') IS DISTINCT FROM current_ticket.body THEN
        changed_fields := array_append(changed_fields, 'body');
    END IF;
    IF patch ? 'implementation' AND coalesce(patch->>'implementation', '') IS DISTINCT FROM current_ticket.implementation THEN
        changed_fields := array_append(changed_fields, 'implementation');
    END IF;
    IF patch ? 'parent_id' THEN
        normalized_parent := upper(btrim(coalesce(patch->>'parent_id', '')));
        IF normalized_parent <> '' THEN
            IF normalized_parent !~ '^PGU-[0-9]+$' THEN
                RAISE EXCEPTION 'invalid parent_id ticket id: %', patch->>'parent_id';
            END IF;
            IF normalized_parent = edit_fields.id THEN
                RAISE EXCEPTION 'ticket cannot parent_id itself';
            END IF;
            PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = normalized_parent;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'parent_id ticket not found: %', normalized_parent;
            END IF;
            seen := ARRAY[edit_fields.id];
            current_parent := normalized_parent;
            WHILE current_parent <> '' LOOP
                IF current_parent = ANY(seen) THEN
                    RAISE EXCEPTION 'parent_id would create a cycle through %', current_parent;
                END IF;
                seen := seen || current_parent;
                SELECT parent_id INTO current_parent FROM ticket_board.tickets WHERE tickets.id = current_parent;
                current_parent := coalesce(current_parent, '');
            END LOOP;
        END IF;
    END IF;

    UPDATE ticket_board.tickets
    SET title = CASE WHEN patch ? 'title' THEN btrim(patch->>'title') ELSE title END,
        body = CASE WHEN patch ? 'body' THEN coalesce(patch->>'body', '') ELSE body END,
        parent_id = CASE WHEN patch ? 'parent_id' THEN upper(btrim(coalesce(patch->>'parent_id', ''))) ELSE parent_id END,
        implementation = CASE WHEN patch ? 'implementation' THEN coalesce(patch->>'implementation', '') ELSE implementation END,
        audit_prompt = CASE WHEN patch ? 'audit_prompt' THEN coalesce(patch->>'audit_prompt', '') ELSE audit_prompt END,
        needs_inspection = CASE WHEN patch ? 'needs_inspection' THEN (patch->>'needs_inspection')::boolean ELSE needs_inspection END,
        needs_eric_signoff = CASE WHEN patch ? 'needs_eric_signoff' THEN (patch->>'needs_eric_signoff')::boolean ELSE needs_eric_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        commit_hash = CASE WHEN patch ? 'commit_hash' THEN btrim(coalesce(patch->>'commit_hash', '')) ELSE commit_hash END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN (patch->>'audit_signoff')::boolean ELSE audit_signoff END,
        inspector_signoff = CASE WHEN patch ? 'inspector_signoff' THEN (patch->>'inspector_signoff')::boolean ELSE inspector_signoff END,
        eric_signoff = CASE WHEN patch ? 'eric_signoff' THEN (patch->>'eric_signoff')::boolean ELSE eric_signoff END,
        screenshot = CASE WHEN patch ? 'screenshot' THEN nullif(patch->>'screenshot', '') ELSE screenshot END
    WHERE tickets.id = edit_fields.id;

    IF patch ? 'screenshots' THEN
        IF jsonb_typeof(patch->'screenshots') <> 'array' THEN
            RAISE EXCEPTION 'screenshots must be an array';
        END IF;
        DELETE FROM ticket_board.ticket_attachments WHERE ticket_id = edit_fields.id;
        INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field)
        SELECT edit_fields.id, ord::integer - 1, path, ord = 1, 'screenshots'
        FROM jsonb_array_elements_text(patch->'screenshots') WITH ORDINALITY AS item(path, ord)
        WHERE btrim(path) <> '';

        SELECT path
        INTO screenshot_value
        FROM ticket_board.ticket_attachments
        WHERE ticket_id = edit_fields.id
        ORDER BY position
        LIMIT 1;
        UPDATE ticket_board.tickets
        SET screenshot = screenshot_value
        WHERE tickets.id = edit_fields.id;
    END IF;

    PERFORM ticket_board.touch_ticket(id);
    IF cardinality(changed_fields) > 0 THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, array_to_string(changed_fields, ', '));
    END IF;
END;
$$;
