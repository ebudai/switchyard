-- Add a convention-only regression flag to ticket metadata.

ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS regression boolean NOT NULL DEFAULT false;

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
    WHERE ticket_id = p_ticket_id;

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
            jsonb_build_object('who', who, 'ts', ts_text, 'text', text)
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
        'needs_eric_signoff', ticket_row.needs_eric_signoff,
        'eric_signoff', ticket_row.eric_signoff,
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
        'regression',
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
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
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
