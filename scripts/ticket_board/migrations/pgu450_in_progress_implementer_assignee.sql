CREATE OR REPLACE FUNCTION ticket_board.ticket_is_implementer_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_assignee IN ('main', 'app', 'perf', 'ops', 'research');
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_can_auto_advance_analysis(
    p_state text,
    p_assignee text,
    p_implementation text,
    p_manually_controlled boolean,
    p_ticket_id text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT p_state = 'analysis'
       AND ticket_board.ticket_is_implementer_assignee(p_assignee)
       AND btrim(coalesce(p_implementation, '')) <> ''
       AND NOT coalesce(p_manually_controlled, false)
       AND NOT ticket_board.ticket_has_unresolved_blockers(p_ticket_id);
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
        RAISE EXCEPTION 'in_progress tickets require an implementer assignee (main, app, ops, perf, or research)';
    END IF;
    RETURN NEW;
END;
$$;

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

    IF NEW.state = 'in_progress' AND NOT ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN
        RAISE EXCEPTION 'in_progress tickets require an implementer assignee (main, app, ops, perf, or research)';
    END IF;

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'eric_review' AND NOT NEW.needs_eric_signoff THEN
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
        NEW.state := CASE WHEN NEW.needs_eric_signoff THEN 'eric_review' ELSE 'director_review' END;
        NEW.assignee := 'director';
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        IF NOT (
            (OLD.state = 'backlog' AND NEW.state IN ('analysis', 'cancelled')) OR
            (OLD.state = 'analysis' AND NEW.state IN ('in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'in_progress' AND NEW.state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'inspection' AND NEW.state IN ('audit', 'in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'audit' AND NEW.state IN ('eric_review', 'director_review', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'eric_review' AND NEW.state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'director_review' AND NEW.state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'done' AND NEW.state IN ('analysis', 'backlog')) OR
            (OLD.state = 'cancelled' AND NEW.state IN ('analysis', 'backlog'))
        ) THEN
            RAISE EXCEPTION 'illegal state transition: % -> %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF OLD.state IN ('inspection', 'audit', 'eric_review', 'director_review', 'done', 'cancelled')
       AND NEW.state IN ('backlog', 'analysis', 'in_progress') THEN
        NEW.inspector_signoff := false;
        NEW.audit_signoff := false;
        NEW.commit_hash := '';
    END IF;

    IF OLD.state = 'inspection' AND NEW.state = 'in_progress' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_comments
            WHERE ticket_id = NEW.id
              AND btrim(text) <> ''
              AND xmin = pg_current_xact_id()::xid
        ) THEN
            RAISE EXCEPTION 'inspector kickback requires a non-empty recommendations comment';
        END IF;
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

    IF OLD.state <> 'director_review' AND NEW.state = 'director_review' AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can enter director_review';
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'tickets requiring inspection must pass through inspection before audit';
    END IF;
    IF OLD.state NOT IN ('audit', 'eric_review', 'director_review') AND NEW.state = 'director_review' THEN
        RAISE EXCEPTION 'tickets must pass through audit before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state = 'director_review' AND NEW.needs_eric_signoff THEN
        RAISE EXCEPTION 'tickets requiring Eric signoff must pass through eric_review before entering director_review';
    END IF;
    IF OLD.state = 'audit' AND NEW.state IN ('eric_review', 'director_review', 'done') AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can leave audit';
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

DROP TRIGGER IF EXISTS tickets_enforce_workflow_insert ON ticket_board.tickets;
CREATE TRIGGER tickets_enforce_workflow_insert
BEFORE INSERT ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.enforce_ticket_workflow_insert();

ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_in_progress_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_in_progress_assignee_check CHECK (
        state <> 'in_progress'
        OR assignee IN ('main', 'app', 'perf', 'ops', 'research')
    ) NOT VALID;
