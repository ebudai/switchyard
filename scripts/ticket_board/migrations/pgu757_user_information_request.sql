INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override)
VALUES
    ('analysis', 'user_review', 'route', ARRAY['director']::text[], false, false)
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;

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
        OR (p_from_state = 'analysis' AND p_to_state IN ('in_progress', 'user_review', 'backlog', 'cancelled'))
        OR (p_from_state = 'in_progress' AND p_to_state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'inspection' AND p_to_state IN ('audit', 'in_progress', 'backlog', 'cancelled'))
        OR (p_from_state = 'audit' AND p_to_state IN ('dat', 'director_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'dat' AND p_to_state IN ('user_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'user_review' AND p_to_state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'director_review' AND p_to_state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'done' AND p_to_state IN ('analysis', 'backlog'))
        OR (p_from_state = 'cancelled' AND p_to_state IN ('analysis', 'backlog'));
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
        IF OLD.state IS DISTINCT FROM NEW.state THEN
            NEW.assignee := 'unassigned';
        END IF;
        NEW.parked := false;
    END IF;

    IF NEW.state <> 'backlog' OR OLD.state IN ('done', 'cancelled') THEN
        NEW.parked := false;
    END IF;

    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
        RAISE EXCEPTION 'in_progress tickets require an implementation-stage owner assignee';
    END IF;

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'dat'
       AND NOT NEW.needs_user_signoff
       AND (OLD.needs_user_signoff OR OLD.state IS DISTINCT FROM NEW.state) THEN
        NEW.state := 'audit';
        NEW.audit_signoff := false;
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
        NEW.state := 'audit';
        NEW.audit_signoff := false;
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

    IF OLD.state <> 'director_review'
       AND NEW.state = 'director_review'
       AND NOT NEW.audit_signoff
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
    IF NEW.state = 'done' AND OLD.state NOT IN ('done', 'director_review') THEN
        RAISE EXCEPTION 'tickets can only enter done from director_review';
    END IF;
    IF OLD.state <> 'done' AND NEW.state = 'done' AND NOT NEW.commit_exempt AND btrim(NEW.commit_hash) = '' THEN
        RAISE EXCEPTION 'commit_hash is required before a ticket can enter done';
    END IF;

    RETURN NEW;
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
    ticket_state text;
    requires_signoff boolean;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('user_sign_off', id);
    SELECT tickets.state, tickets.needs_user_signoff
    INTO ticket_state, requires_signoff
    FROM ticket_board.tickets
    WHERE tickets.id = user_sign_off.id
    FOR UPDATE;
    IF NOT FOUND OR ticket_state <> 'user_review' THEN
        RAISE EXCEPTION 'user_review ticket not found: %', id;
    END IF;
    IF NOT requires_signoff THEN
        RAISE EXCEPTION 'user_sign_off requires needs_user_signoff=true';
    END IF;
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
    UPDATE ticket_board.tickets
    SET user_signoff = true
    WHERE tickets.id = user_sign_off.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;
