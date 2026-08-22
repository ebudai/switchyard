-- Make review/rework kickback comments optional. Empty reasons still move the
-- ticket, but no placeholder comment is inserted.

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    blocker_id text;
BEGIN
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

    IF NEW.state = 'user_review' AND NOT NEW.needs_user_signoff THEN
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
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'user_review' ELSE 'director_review' END;
        NEW.assignee := 'director';
    END IF;

    IF NEW.state = 'done'
       AND OLD.state IN ('backlog', 'analysis')
       AND current_setting('ticket_board.utility_task_complete', true) = 'on'
       AND NEW.commit_exempt THEN
        RETURN NEW;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        IF NOT (
            (OLD.state = 'backlog' AND NEW.state IN ('analysis', 'in_progress', 'cancelled')) OR
            (OLD.state = 'analysis' AND NEW.state IN ('in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'in_progress' AND NEW.state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'inspection' AND NEW.state IN ('audit', 'in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'audit' AND NEW.state IN ('user_review', 'director_review', 'in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'user_review' AND NEW.state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'director_review' AND NEW.state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'done' AND NEW.state IN ('analysis', 'backlog')) OR
            (OLD.state = 'cancelled' AND NEW.state IN ('analysis', 'backlog'))
        ) THEN
            RAISE EXCEPTION 'illegal state transition: % -> %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF OLD.state IN ('inspection', 'audit', 'user_review', 'director_review', 'done', 'cancelled')
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
    IF OLD.state NOT IN ('audit', 'user_review', 'director_review') AND NEW.state = 'director_review' THEN
        RAISE EXCEPTION 'tickets must pass through audit before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state = 'director_review' AND NEW.needs_user_signoff THEN
        RAISE EXCEPTION 'tickets requiring User signoff must pass through user_review before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state IN ('user_review', 'director_review', 'done') AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can leave audit';
    END IF;
    IF OLD.state = 'user_review'
       AND NEW.state = 'director_review'
       AND NEW.needs_user_signoff
       AND NOT NEW.user_signoff THEN
        RAISE EXCEPTION 'user_signoff must be true before a ticket can leave user_review';
    END IF;
    IF OLD.state = 'director_review'
       AND NEW.state = 'done'
       AND NOT NEW.commit_exempt
       AND coalesce(NULLIF(btrim(NEW.commit_hash), ''), '') = '' THEN
        RAISE EXCEPTION 'commit_hash is required before marking done unless commit_exempt is true';
    END IF;

    RETURN NEW;
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
    actor := ticket_board.require_actor(ARRAY['audit'], 'audit_kick_back');
    comment_actor := ticket_board.current_app_actor();
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
    actor := ticket_board.require_actor(ARRAY['audit'], 'audit_kick_back');
    comment_actor := ticket_board.current_app_actor();
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
    actor := ticket_board.require_actor(ARRAY['inspector'], 'inspector_kick_back');
    comment_actor := ticket_board.current_app_actor();
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
    actor := ticket_board.require_actor(ARRAY['inspector'], 'inspector_kick_back');
    comment_actor := ticket_board.current_app_actor();
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
    actor := ticket_board.require_actor(ARRAY['user'], 'user_reopen');
    comment_actor := ticket_board.current_app_actor();
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
