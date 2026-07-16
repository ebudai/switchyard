ALTER TABLE ticket_board.ticket_comments
    ADD COLUMN IF NOT EXISTS urgent boolean NOT NULL DEFAULT false;

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

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text,
    urgent boolean
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
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text, urgent);
    PERFORM ticket_board.touch_ticket(id);
    IF urgent THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text
)
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.add_comment(id, text, false);
$$;
