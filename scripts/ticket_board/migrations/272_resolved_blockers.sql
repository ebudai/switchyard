-- Persist blocker resolution and prevent forward promotion while blocked.

ALTER TABLE ticket_board.ticket_blockers
    ADD COLUMN IF NOT EXISTS resolved boolean;

UPDATE ticket_board.ticket_blockers b
SET resolved = coalesce(blocker.state IN ('done', 'cancelled'), false)
FROM ticket_board.tickets blocker
WHERE blocker.id = b.blocker_ticket_id
  AND b.resolved IS NULL;

UPDATE ticket_board.ticket_blockers
SET resolved = false
WHERE resolved IS NULL;

ALTER TABLE ticket_board.ticket_blockers
    ALTER COLUMN resolved SET DEFAULT false,
    ALTER COLUMN resolved SET NOT NULL;

CREATE OR REPLACE FUNCTION ticket_board.ticket_is_forward_promotion(
    p_old_state text,
    p_new_state text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT (p_old_state = 'backlog' AND p_new_state = 'analysis')
        OR (p_old_state = 'analysis' AND p_new_state = 'in_progress')
        OR (p_old_state = 'in_progress' AND p_new_state IN ('inspection', 'audit'))
        OR (p_old_state = 'inspection' AND p_new_state = 'audit')
        OR (p_old_state = 'audit' AND p_new_state IN ('eric_review', 'director_review', 'done'))
        OR (p_old_state = 'eric_review' AND p_new_state = 'director_review')
        OR (p_old_state = 'director_review' AND p_new_state = 'done');
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

    IF OLD.state <> 'in_progress' AND NEW.state = 'in_progress' THEN
        IF NEW.assignee = 'unassigned' THEN
            RAISE EXCEPTION 'assignee must not be unassigned before a ticket can enter in_progress';
        END IF;
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

CREATE OR REPLACE FUNCTION ticket_board.sync_blocker_resolved_from_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.resolved := EXISTS (
        SELECT 1
        FROM ticket_board.tickets t
        WHERE t.id = NEW.blocker_ticket_id
          AND t.state IN ('done', 'cancelled')
    );

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.resolve_completed_blockers()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    dependent_id text;
BEGIN
    IF TG_OP <> 'UPDATE'
       OR OLD.state IS NOT DISTINCT FROM NEW.state
       OR OLD.state IN ('done', 'cancelled')
       OR NEW.state NOT IN ('done', 'cancelled') THEN
        RETURN NULL;
    END IF;

    FOR dependent_id IN
        UPDATE ticket_board.ticket_blockers
        SET resolved = true
        WHERE blocker_ticket_id = NEW.id
          AND NOT resolved
        RETURNING ticket_id
    LOOP
        PERFORM ticket_board.refresh_ticket_source_json(dependent_id);
    END LOOP;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_has_unresolved_blockers(p_ticket_id text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.ticket_blockers b
        WHERE b.ticket_id = p_ticket_id
          AND NOT b.resolved
    );
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

DROP TRIGGER IF EXISTS tickets_zzzy_resolve_completed_blockers ON ticket_board.tickets;
DROP TRIGGER IF EXISTS ticket_blockers_sync_resolved ON ticket_board.ticket_blockers;

CREATE TRIGGER tickets_zzzy_resolve_completed_blockers
AFTER UPDATE OF state ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.resolve_completed_blockers();

CREATE TRIGGER ticket_blockers_sync_resolved
BEFORE INSERT OR UPDATE OF blocker_ticket_id ON ticket_board.ticket_blockers
FOR EACH ROW
EXECUTE FUNCTION ticket_board.sync_blocker_resolved_from_state();

UPDATE ticket_board.tickets
SET source_json = ticket_board.build_ticket_source_json(id),
    row_updated_at = now();
