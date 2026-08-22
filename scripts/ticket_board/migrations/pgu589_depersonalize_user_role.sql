BEGIN;

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_state_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_state_check CHECK (state IN (
        'draft',
        'backlog',
        'analysis',
        'in_progress',
        'inspection',
        'audit',
        'dat',
        'eric_review',
        'user_review',
        'director_review',
        'done',
        'cancelled'
    ));

ALTER TABLE ticket_board.workflow_transitions
    DROP CONSTRAINT IF EXISTS workflow_transitions_from_stage_fkey;
ALTER TABLE ticket_board.workflow_transitions
    DROP CONSTRAINT IF EXISTS workflow_transitions_to_stage_fkey;
ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT IF EXISTS workflow_stages_gate_skip_to_fkey;
ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_name_check CHECK (name IN (
        'draft',
        'backlog',
        'analysis',
        'in_progress',
        'inspection',
        'audit',
        'dat',
        'eric_review',
        'user_review',
        'director_review',
        'done',
        'cancelled'
    ));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'ticket_board'
          AND table_name = 'tickets'
          AND column_name = 'needs_eric_signoff'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'ticket_board'
          AND table_name = 'tickets'
          AND column_name = 'needs_user_signoff'
    ) THEN
        ALTER TABLE ticket_board.tickets RENAME COLUMN needs_eric_signoff TO needs_user_signoff;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'ticket_board'
          AND table_name = 'tickets'
          AND column_name = 'eric_signoff'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'ticket_board'
          AND table_name = 'tickets'
          AND column_name = 'user_signoff'
    ) THEN
        ALTER TABLE ticket_board.tickets RENAME COLUMN eric_signoff TO user_signoff;
    END IF;
END $$;

-- Install new function bodies before any row updates can fire triggers against renamed columns.
DROP FUNCTION IF EXISTS ticket_board.create_ticket(text, text, text, text[], text, boolean);

CREATE OR REPLACE FUNCTION ticket_board.ticket_is_forward_promotion(
    p_old_state text,
    p_new_state text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT (p_old_state = 'backlog' AND p_new_state = 'analysis')
        OR (p_old_state = 'draft' AND p_new_state = 'analysis')
        OR (p_old_state = 'backlog' AND p_new_state = 'in_progress')
        OR (p_old_state = 'analysis' AND p_new_state = 'in_progress')
        OR (p_old_state = 'in_progress' AND p_new_state IN ('inspection', 'audit'))
        OR (p_old_state = 'inspection' AND p_new_state = 'audit')
        OR (p_old_state = 'audit' AND p_new_state IN ('dat', 'director_review', 'done'))
        OR (p_old_state = 'dat' AND p_new_state = 'user_review')
        OR (p_old_state = 'user_review' AND p_new_state = 'director_review')
        OR (p_old_state = 'director_review' AND p_new_state = 'done');
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_allowed_hardcoded(
    p_from_state text,
    p_to_state text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT (p_from_state = 'backlog' AND p_to_state IN ('analysis', 'in_progress', 'cancelled'))
        OR (p_from_state = 'draft' AND p_to_state IN ('analysis', 'cancelled'))
        OR (p_from_state = 'analysis' AND p_to_state IN ('in_progress', 'backlog', 'cancelled'))
        OR (p_from_state = 'in_progress' AND p_to_state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'inspection' AND p_to_state IN ('audit', 'in_progress', 'backlog', 'cancelled'))
        OR (p_from_state = 'audit' AND p_to_state IN ('dat', 'director_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'dat' AND p_to_state IN ('user_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'user_review' AND p_to_state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'director_review' AND p_to_state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'done' AND p_to_state IN ('analysis', 'backlog'))
        OR (p_from_state = 'cancelled' AND p_to_state IN ('analysis', 'backlog'));
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_reserved_implementer(
    p_ticket_id text,
    p_state text,
    p_assignee text
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(p_assignee) THEN p_assignee
        WHEN p_state IN ('inspection', 'audit', 'dat', 'user_review', 'director_review') THEN (
            SELECT nullif(ns.last_implementer_assignee, '')
            FROM ticket_board.ticket_notification_state ns
            WHERE ns.ticket_id = p_ticket_id
              AND ticket_board.ticket_is_implementer_assignee(nullif(ns.last_implementer_assignee, ''))
        )
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_current_reserved_ticket(
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
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
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
BEGIN
    IF NEW.state = 'draft' THEN
        NEW.assignee := 'unassigned';
        NEW.parked := false;
    END IF;

    IF NEW.state <> 'backlog' OR OLD.state IN ('done', 'cancelled') THEN
        NEW.parked := false;
    END IF;

    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
        RAISE EXCEPTION 'in_progress tickets require an implementer assignee (main, app, ops, perf, or research)';
    END IF;

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state IN ('dat', 'user_review') AND NOT NEW.needs_user_signoff THEN
        NEW.state := 'audit';
        NEW.audit_signoff := false;
    END IF;
    IF NOT NEW.needs_user_signoff THEN
        NEW.user_signoff := false;
    END IF;
    IF NOT NEW.needs_inspection THEN
        NEW.inspector_signoff := false;
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        NEW.state := 'inspection';
        NEW.inspector_signoff := false;
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
    END IF;
    IF NEW.state = 'audit' AND NEW.audit_signoff THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.assignee := 'director';
    END IF;

    IF NEW.state = 'done'
       AND OLD.state IN ('backlog', 'analysis')
       AND current_setting('ticket_board.utility_task_complete', true) = 'on'
       AND NEW.commit_exempt THEN
        RETURN NEW;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        hardcoded_transition_allowed := ticket_board.workflow_transition_allowed_hardcoded(OLD.state, NEW.state);
        config_transition_allowed := ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state);
        shadow_actor := coalesce(nullif(current_setting('ticket_board.caller_role', true), ''), current_user);
        PERFORM ticket_board.log_workflow_transition_shadow_mismatch(
            NEW.id,
            OLD.state,
            NEW.state,
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

    IF OLD.state <> 'director_review' AND NEW.state = 'director_review' AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can enter director_review';
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'tickets requiring inspection must pass through inspection before audit';
    END IF;
    IF OLD.state NOT IN ('audit', 'dat', 'user_review', 'director_review') AND NEW.state = 'director_review' THEN
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
    IF NEW.state = 'done' AND OLD.state NOT IN ('done', 'director_review') THEN
        RAISE EXCEPTION 'tickets can only enter done from director_review';
    END IF;
    IF OLD.state <> 'done' AND NEW.state = 'done' AND NOT NEW.commit_exempt AND btrim(NEW.commit_hash) = '' THEN
        RAISE EXCEPTION 'commit_hash is required before a ticket can enter done';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text
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
    WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled') THEN
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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled') THEN
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

CREATE OR REPLACE FUNCTION ticket_board.director_dat_sign_off(
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
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('director_dat_sign_off', id);
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'user_review',
        assignee = 'director'
    WHERE tickets.id = director_dat_sign_off.id
      AND tickets.state = 'dat'
      AND tickets.needs_user_signoff;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'DAT ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.director_dat_kick_back(
    id text,
    reason text,
    target_assignee text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    comment_actor text;
    normalized_reason text := btrim(coalesce(reason, ''));
    resolved_assignee text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('director_dat_kick_back', id);
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'director_dat_kick_back requires a non-empty reason';
    END IF;
    resolved_assignee := ticket_board.ticket_kickback_target_assignee(id, target_assignee);
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_reason);
    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        assignee = resolved_assignee,
        audit_signoff = false,
        inspector_signoff = false,
        user_signoff = false,
        last_rejected_commit = commit_hash
    WHERE tickets.id = director_dat_kick_back.id
      AND tickets.state = 'dat';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'DAT ticket not found: %', id;
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
    actor := ticket_board.require_actor(ARRAY['director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text, urgent);
    PERFORM ticket_board.touch_ticket(id);
    IF urgent OR comment_actor IN ('director', 'user') THEN
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
        ARRAY['director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
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
        'needs_user_signoff',
        'commit_exempt',
        'regression',
        'commit_hash',
        'audit_signoff',
        'inspector_signoff',
        'user_signoff'
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
    IF patch @> '{"user_signoff": true}'::jsonb THEN
        RAISE EXCEPTION 'user_signoff=true requires user_sign_off';
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
        needs_user_signoff = CASE WHEN patch ? 'needs_user_signoff' THEN (patch->>'needs_user_signoff')::boolean ELSE needs_user_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
        commit_hash = CASE WHEN patch ? 'commit_hash' THEN btrim(coalesce(patch->>'commit_hash', '')) ELSE commit_hash END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN (patch->>'audit_signoff')::boolean ELSE audit_signoff END,
        inspector_signoff = CASE WHEN patch ? 'inspector_signoff' THEN (patch->>'inspector_signoff')::boolean ELSE inspector_signoff END,
        user_signoff = CASE WHEN patch ? 'user_signoff' THEN (patch->>'user_signoff')::boolean ELSE user_signoff END,
        screenshot = CASE WHEN patch ? 'screenshot' THEN nullif(patch->>'screenshot', '') ELSE screenshot END
    WHERE tickets.id = edit_fields.id;

    IF patch ? 'screenshots' THEN
        IF jsonb_typeof(patch->'screenshots') <> 'array' THEN
            RAISE EXCEPTION 'screenshots must be an array';
        END IF;
        WITH existing AS (
            SELECT path, metadata
            FROM ticket_board.ticket_attachments
            WHERE ticket_id = edit_fields.id
        ),
        deleted AS (
            DELETE FROM ticket_board.ticket_attachments
            WHERE ticket_id = edit_fields.id
        ),
        requested AS (
            SELECT btrim(path) AS path, ord::integer - 1 AS position
            FROM jsonb_array_elements_text(patch->'screenshots') WITH ORDINALITY AS item(path, ord)
            WHERE btrim(path) <> ''
        )
        INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field, metadata)
        SELECT edit_fields.id,
               requested.position,
               requested.path,
               requested.position = 0,
               'screenshots',
               coalesce(existing.metadata, '{}'::jsonb)
        FROM requested
        LEFT JOIN existing ON existing.path = requested.path
        ORDER BY requested.position;

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
DROP FUNCTION IF EXISTS ticket_board.eric_sign_off(text, text);
DROP FUNCTION IF EXISTS ticket_board.eric_reopen(text, text);

SET LOCAL session_replication_role = replica;

UPDATE ticket_board.tickets
SET state = 'user_review'
WHERE state = 'eric_review';

UPDATE ticket_board.ticket_notification_state
SET current_state = 'user_review'
WHERE current_state = 'eric_review';

UPDATE ticket_board.ticket_notification_state
SET previous_state = 'user_review'
WHERE previous_state = 'eric_review';

UPDATE ticket_board.ticket_notification_state
SET awaiting_role = 'user'
WHERE awaiting_role = 'eric';

UPDATE ticket_board.ticket_comments
SET who = 'user'
WHERE who = 'eric';

UPDATE ticket_board.ticket_comments
SET source_json = jsonb_set(source_json, '{who}', '"user"'::jsonb, true)
WHERE source_json->>'who' = 'eric';

UPDATE ticket_board.tickets
SET source_json = jsonb_set(source_json, '{state}', '"user_review"'::jsonb, true)
WHERE source_json->>'state' = 'eric_review';

UPDATE ticket_board.tickets
SET source_json = jsonb_set(source_json - 'needs_eric_signoff', '{needs_user_signoff}', source_json->'needs_eric_signoff', true)
WHERE source_json ? 'needs_eric_signoff';

UPDATE ticket_board.tickets
SET source_json = jsonb_set(source_json - 'eric_signoff', '{user_signoff}', source_json->'eric_signoff', true)
WHERE source_json ? 'eric_signoff';

UPDATE ticket_board.ticket_notification_queue
SET target_role = 'user'
WHERE target_role = 'eric';

UPDATE ticket_board.ticket_notification_queue
SET payload = replace(
        replace(
            replace(
                replace(
                    replace(
                        replace(payload::text, 'eric_review', 'user_review'),
                        'needs_eric_signoff',
                        'needs_user_signoff'
                    ),
                    'eric_signoff',
                    'user_signoff'
                ),
                'eric_sign_off',
                'user_sign_off'
            ),
            'eric_reopen',
            'user_reopen'
        ),
        '"eric"',
        '"user"'
    )::jsonb,
    dedupe_key = replace(dedupe_key, 'eric_review', 'user_review'),
    message = replace(message, 'Eric', 'User')
WHERE payload::text LIKE '%eric%'
   OR payload::text LIKE '%Eric%'
   OR dedupe_key LIKE '%eric_review%'
   OR message LIKE '%Eric%';

UPDATE ticket_board.notification_trace
SET target_role = CASE WHEN target_role = 'eric' THEN 'user' ELSE target_role END,
    ticket_state_at_event = CASE WHEN ticket_state_at_event = 'eric_review' THEN 'user_review' ELSE ticket_state_at_event END,
    ticket_assignee_at_event = CASE WHEN ticket_assignee_at_event = 'eric' THEN 'user' ELSE ticket_assignee_at_event END,
    detail = replace(
        replace(
            replace(
                replace(
                    replace(
                        replace(detail::text, 'eric_review', 'user_review'),
                        'needs_eric_signoff',
                        'needs_user_signoff'
                    ),
                    'eric_signoff',
                    'user_signoff'
                ),
                'eric_sign_off',
                'user_sign_off'
            ),
            'eric_reopen',
            'user_reopen'
        ),
        '"eric"',
        '"user"'
    )::jsonb
WHERE target_role = 'eric'
   OR ticket_state_at_event = 'eric_review'
   OR ticket_assignee_at_event = 'eric'
   OR detail::text LIKE '%eric%';

UPDATE ticket_board.workflow_stages
SET name = 'user_review'
WHERE name = 'eric_review'
  AND NOT EXISTS (
      SELECT 1 FROM ticket_board.workflow_stages existing WHERE existing.name = 'user_review'
  );

DELETE FROM ticket_board.workflow_stages
WHERE name = 'eric_review';

UPDATE ticket_board.workflow_stages
SET entry_gate_field = CASE entry_gate_field
        WHEN 'needs_eric_signoff' THEN 'needs_user_signoff'
        ELSE entry_gate_field
    END,
    gate_skip_to = CASE gate_skip_to
        WHEN 'eric_review' THEN 'user_review'
        ELSE gate_skip_to
    END,
    exit_signoff_field = CASE exit_signoff_field
        WHEN 'eric_signoff' THEN 'user_signoff'
        ELSE exit_signoff_field
    END;

UPDATE ticket_board.workflow_transitions
SET from_stage = CASE from_stage WHEN 'eric_review' THEN 'user_review' ELSE from_stage END,
    to_stage = CASE to_stage WHEN 'eric_review' THEN 'user_review' ELSE to_stage END,
    action_name = CASE action_name
        WHEN 'eric_sign_off' THEN 'user_sign_off'
        WHEN 'eric_reopen' THEN 'user_reopen'
        ELSE action_name
    END,
    allowed_roles = array_replace(allowed_roles, 'eric', 'user');

ALTER TABLE ticket_board.workflow_stages
    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_name_check CHECK (name IN (
        'draft',
        'backlog',
        'analysis',
        'in_progress',
        'inspection',
        'audit',
        'dat',
        'user_review',
        'director_review',
        'done',
        'cancelled'
    ));
ALTER TABLE ticket_board.workflow_stages
    ADD CONSTRAINT workflow_stages_gate_skip_to_fkey
    FOREIGN KEY (gate_skip_to) REFERENCES ticket_board.workflow_stages(name);
ALTER TABLE ticket_board.workflow_transitions
    ADD CONSTRAINT workflow_transitions_from_stage_fkey
    FOREIGN KEY (from_stage) REFERENCES ticket_board.workflow_stages(name);
ALTER TABLE ticket_board.workflow_transitions
    ADD CONSTRAINT workflow_transitions_to_stage_fkey
    FOREIGN KEY (to_stage) REFERENCES ticket_board.workflow_stages(name);

COMMIT;

\ir ../schema.sql
