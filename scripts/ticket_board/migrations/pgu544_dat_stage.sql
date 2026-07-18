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
        'director_review',
        'done',
        'cancelled'
    ));

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
        'director_review',
        'done',
        'cancelled'
    ));

UPDATE ticket_board.workflow_stages
SET rank = 206
WHERE name = 'dat'
  AND rank IN (6, 106);

UPDATE ticket_board.workflow_stages
SET rank = rank + 100
WHERE name IN ('eric_review', 'director_review', 'done', 'cancelled');

INSERT INTO ticket_board.workflow_stages (
    name,
    display_label,
    rank,
    owner_roles,
    entry_gate_field,
    gate_skip_to,
    exit_signoff_field,
    is_terminal
) VALUES
    ('dat', 'DAT', 6, ARRAY['director']::text[], 'needs_eric_signoff', 'director_review', NULL, false)
ON CONFLICT (name) DO UPDATE
SET display_label = EXCLUDED.display_label,
    rank = EXCLUDED.rank,
    owner_roles = EXCLUDED.owner_roles,
    entry_gate_field = EXCLUDED.entry_gate_field,
    gate_skip_to = EXCLUDED.gate_skip_to,
    exit_signoff_field = EXCLUDED.exit_signoff_field,
    is_terminal = EXCLUDED.is_terminal;

UPDATE ticket_board.workflow_stages
SET rank = CASE name
    WHEN 'eric_review' THEN 7
    WHEN 'director_review' THEN 8
    WHEN 'done' THEN 9
    WHEN 'cancelled' THEN 10
    ELSE rank
END
WHERE name IN ('eric_review', 'director_review', 'done', 'cancelled');

DELETE FROM ticket_board.workflow_transitions
WHERE from_stage = 'audit'
  AND to_stage = 'eric_review'
  AND action_name = 'audit_sign_off';

INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override)
VALUES
    ('audit', 'dat', 'audit_sign_off', ARRAY['audit']::text[], false, false),
    ('dat', 'eric_review', 'director_dat_sign_off', ARRAY['director']::text[], false, false),
    ('dat', 'in_progress', 'director_dat_kick_back', ARRAY['director']::text[], false, false),
    ('dat', 'analysis', 'director_dat_kick_back', ARRAY['director']::text[], false, false),
    ('dat', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('dat', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('dat', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('dat', 'cancelled', 'cancel', ARRAY['director']::text[], false, false)
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;

CREATE OR REPLACE FUNCTION ticket_board.nudge_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
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
    SET state = 'eric_review',
        assignee = 'director'
    WHERE tickets.id = director_dat_sign_off.id
      AND tickets.state = 'dat'
      AND tickets.needs_eric_signoff;
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
        eric_signoff = false,
        last_rejected_commit = commit_hash
    WHERE tickets.id = director_dat_kick_back.id
      AND tickets.state = 'dat';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'DAT ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.director_dat_sign_off(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.director_dat_kick_back(text, text, text) TO ticket_board_service;

-- Keep upgraded databases aligned with the fresh DAT-aware schema definitions.
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
        OR (p_old_state = 'dat' AND p_new_state = 'eric_review')
        OR (p_old_state = 'eric_review' AND p_new_state = 'director_review')
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
        OR (p_from_state = 'dat' AND p_to_state IN ('eric_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'eric_review' AND p_to_state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled'))
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
        WHEN p_state IN ('inspection', 'audit', 'dat', 'eric_review', 'director_review') THEN (
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
      AND t.state IN ('in_progress', 'inspection', 'audit', 'dat', 'eric_review', 'director_review')
      AND (
          (t.state = 'in_progress' AND t.assignee = p_implementer)
          OR (
              t.state IN ('inspection', 'audit', 'dat', 'eric_review', 'director_review')
              AND ns.last_implementer_assignee = p_implementer
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
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
        WHEN p_state = 'eric_review' THEN 'director'
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
        WHEN p_new_state = 'eric_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for Eric UAT'
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
    WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'eric_review', 'director_review')
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_message(p_ticket_id text, p_title text, p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' needs director triage in analysis'
        WHEN p_state = 'backlog' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is assigned in backlog; triage or defer explicitly'
        WHEN p_state = 'in_progress' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is still in progress'
        WHEN p_state = 'inspection' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for inspection'
        WHEN p_state = 'audit' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for audit'
        WHEN p_state = 'dat' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for DAT'
        WHEN p_state = 'director_review' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for your review'
        ELSE NULL
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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'eric_review', 'director_review', 'done', 'cancelled') THEN
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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'dat', 'eric_review', 'director_review', 'done', 'cancelled') THEN
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

    IF NEW.state IN ('dat', 'eric_review') AND NOT NEW.needs_eric_signoff THEN
        NEW.state := 'audit';
        NEW.audit_signoff := false;
    END IF;
    IF NOT NEW.needs_eric_signoff THEN
        NEW.eric_signoff := false;
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
    IF NEW.state = 'eric_review' AND NEW.eric_signoff THEN
        NEW.state := 'director_review';
        NEW.assignee := 'director';
    END IF;
    IF NEW.state = 'audit' AND NEW.audit_signoff THEN
        NEW.state := CASE WHEN NEW.needs_eric_signoff THEN 'dat' ELSE 'director_review' END;
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

    IF OLD.state IN ('inspection', 'audit', 'dat', 'eric_review', 'director_review', 'done', 'cancelled')
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
    IF OLD.state NOT IN ('audit', 'dat', 'eric_review', 'director_review') AND NEW.state = 'director_review' THEN
        RAISE EXCEPTION 'tickets must pass through audit before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state = 'director_review' AND NEW.needs_eric_signoff THEN
        RAISE EXCEPTION 'tickets requiring Eric signoff must pass through DAT and eric_review before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state IN ('dat', 'eric_review', 'director_review', 'done') AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can leave audit';
    END IF;
    IF OLD.state = 'dat' AND NEW.state = 'eric_review' AND NOT NEW.needs_eric_signoff THEN
        RAISE EXCEPTION 'DAT only applies to tickets requiring UAT';
    END IF;
    IF OLD.state = 'eric_review'
       AND NEW.state = 'director_review'
       AND NEW.needs_eric_signoff
       AND NOT NEW.eric_signoff THEN
        RAISE EXCEPTION 'eric_signoff must be true before a ticket can leave eric_review';
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
