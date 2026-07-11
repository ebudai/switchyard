-- PGU ticket-board PostgreSQL schema.
--
-- PGU-207 replaces the polling director watchdog's ticket notification duties
-- with trigger-maintained durable state, transition notifications, and a
-- pg_cron-compatible nudge function consumed by the tiny LISTEN sender.
--
-- Design goals:
--   * Lossless import of the existing JSON ticket files.
--   * Normalized columns/relations for the fields transition triggers need.
--   * Keep legacy JSON omissions representable while providing sane defaults for
--     future DB-authored rows.

BEGIN;

CREATE SCHEMA IF NOT EXISTS ticket_board;

CREATE TABLE IF NOT EXISTS ticket_board.schema_migrations (
    version integer PRIMARY KEY,
    description text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ticket_board.tickets (
    id text PRIMARY KEY
        CHECK (id ~ '^PGU-[0-9]+$'),
    ticket_number integer GENERATED ALWAYS AS ((substring(id from '^PGU-([0-9]+)$'))::integer) STORED,

    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    state text NOT NULL
        CHECK (state IN (
            'backlog',
            'analysis',
            'ready',
            'in_progress',
            'audit',
            'eric_review',
            'director_review',
            'done',
            'cancelled'
        )),
    assignee text NOT NULL
        CHECK (assignee IN (
            'unassigned',
            'main',
            'app',
            'perf',
            'ops',
            'audit',
            'agent',
            'director',
            'research'
        )),

    -- Empty string is preserved because the JSON store uses "" rather than null
    -- for "no parent" on newer tickets, while older tickets omit parent_id.
    parent_id text NOT NULL DEFAULT ''
        CHECK (parent_id = '' OR parent_id ~ '^PGU-[0-9]+$'),
    blocked_reason text NOT NULL DEFAULT '',

    implementation text NOT NULL DEFAULT '',
    audit_prompt text NOT NULL DEFAULT '',
    audit_signoff boolean NOT NULL DEFAULT false,
    needs_eric_signoff boolean NOT NULL DEFAULT false,
    eric_signoff boolean NOT NULL DEFAULT false,
    manually_controlled boolean NOT NULL DEFAULT false,

    -- Historical tickets include both full 40-char hashes and short hashes.
    commit_hash text NOT NULL DEFAULT ''
        CHECK (commit_hash = '' OR commit_hash ~ '^[0-9A-Fa-f]{7,40}$'),
    commit_exempt boolean NOT NULL DEFAULT false,

    -- Back-compat mirror of the legacy top-level "screenshot" field. The
    -- canonical attachment list is ticket_attachments.
    screenshot text,

    -- Store raw timestamp text for lossless JSON export. Parsed timestamptz
    -- columns are nullable so the importer can preserve a row even if a legacy
    -- hand-edited ticket contains an odd timestamp string.
    created_text text NOT NULL,
    updated_text text NOT NULL,
    created_at timestamptz,
    updated_at timestamptz,

    -- Exact JSON payload imported from PGU-N.json. This preserves missing-vs-
    -- defaulted field distinctions from legacy tickets and gives the import
    -- script an audit trail while normalized columns become the DB source of
    -- truth for future writes.
    source_json jsonb NOT NULL,
    source_file_name text,
    imported_at timestamptz NOT NULL DEFAULT now(),
    row_updated_at timestamptz NOT NULL DEFAULT now(),

    CHECK (ticket_number > 0),
    CHECK (source_json ? 'id'),
    CHECK (source_json ? 'title'),
    CHECK (source_json ? 'state'),
    CHECK (source_json ? 'assignee'),
    CHECK (source_json ? 'body'),
    CHECK (source_json ? 'comments'),
    CHECK (source_json ? 'created'),
    CHECK (source_json ? 'updated')
);

CREATE UNIQUE INDEX IF NOT EXISTS tickets_ticket_number_key
    ON ticket_board.tickets (ticket_number);
CREATE INDEX IF NOT EXISTS tickets_state_assignee_idx
    ON ticket_board.tickets (state, assignee, ticket_number);
CREATE INDEX IF NOT EXISTS tickets_updated_idx
    ON ticket_board.tickets (updated_at DESC NULLS LAST, updated_text DESC);
CREATE INDEX IF NOT EXISTS tickets_source_json_gin
    ON ticket_board.tickets USING gin (source_json);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_blockers (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    blocker_ticket_id text NOT NULL
        CHECK (blocker_ticket_id ~ '^PGU-[0-9]+$'),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (ticket_id, blocker_ticket_id),
    UNIQUE (ticket_id, position),
    CHECK (ticket_id <> blocker_ticket_id)
);

CREATE INDEX IF NOT EXISTS ticket_blockers_blocker_idx
    ON ticket_board.ticket_blockers (blocker_ticket_id);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_comments (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    who text NOT NULL,
    ts_text text NOT NULL,
    ts timestamptz,
    text text NOT NULL,
    source_json jsonb NOT NULL,
    UNIQUE (ticket_id, position),
    CHECK (source_json ? 'who'),
    CHECK (source_json ? 'ts'),
    CHECK (source_json ? 'text')
);

CREATE INDEX IF NOT EXISTS ticket_comments_ticket_ts_idx
    ON ticket_board.ticket_comments (ticket_id, ts NULLS LAST, position);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_attachments (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    path text NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    source_field text NOT NULL
        CHECK (source_field IN ('screenshots', 'screenshot')),
    PRIMARY KEY (ticket_id, position),
    UNIQUE (ticket_id, path)
);

CREATE INDEX IF NOT EXISTS ticket_attachments_path_idx
    ON ticket_board.ticket_attachments (path);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_notification_state (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    current_state text NOT NULL,
    current_assignee text NOT NULL,
    previous_state text,
    entered_current_state_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_activity_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_transition_notified_at timestamptz,
    last_nudged_at timestamptz,
    nudge_count integer NOT NULL DEFAULT 0 CHECK (nudge_count >= 0)
);

CREATE INDEX IF NOT EXISTS ticket_notification_state_due_idx
    ON ticket_board.ticket_notification_state (
        current_state,
        current_assignee,
        entered_current_state_at,
        last_transition_notified_at,
        last_nudged_at
    );

CREATE OR REPLACE FUNCTION ticket_board.ticket_cluster_root_id(
    p_ticket_id text,
    p_parent_id text
)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    current_root text := p_ticket_id;
    current_parent text := coalesce(nullif(p_parent_id, ''), '');
    next_parent text;
    seen text[] := ARRAY[p_ticket_id];
BEGIN
    WHILE current_parent <> '' LOOP
        IF current_parent = ANY(seen) THEN
            RETURN current_parent;
        END IF;
        seen := seen || current_parent;
        current_root := current_parent;

        SELECT parent_id
        INTO next_parent
        FROM ticket_board.tickets
        WHERE id = current_parent;

        current_parent := coalesce(nullif(next_parent, ''), '');
    END LOOP;

    RETURN current_root;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_root text;
    conflicting_ticket_id text;
BEGIN
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
            (OLD.state = 'backlog' AND NEW.state IN ('analysis', 'ready')) OR
            (OLD.state = 'analysis' AND NEW.state IN ('ready', 'backlog', 'cancelled')) OR
            (OLD.state = 'ready' AND NEW.state IN ('in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'in_progress' AND NEW.state IN ('audit', 'ready', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'audit' AND NEW.state IN ('eric_review', 'director_review', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'eric_review' AND NEW.state IN ('director_review', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'director_review' AND NEW.state IN ('done', 'ready', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'done' AND NEW.state IN ('analysis', 'backlog')) OR
            (OLD.state = 'cancelled' AND NEW.state IN ('analysis', 'backlog'))
        ) THEN
            RAISE EXCEPTION 'illegal state transition: % -> %', OLD.state, NEW.state;
        END IF;
    END IF;

    IF OLD.state IN ('audit', 'eric_review', 'director_review', 'done', 'cancelled')
       AND NEW.state IN ('backlog', 'analysis', 'ready', 'in_progress') THEN
        NEW.audit_signoff := false;
        NEW.commit_hash := '';
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

    IF OLD.state <> 'ready' AND NEW.state = 'ready' THEN
        IF NEW.assignee = 'unassigned' THEN
            RAISE EXCEPTION 'assignee must not be unassigned before a ticket can enter ready';
        END IF;
        IF btrim(NEW.implementation) = '' THEN
            RAISE EXCEPTION 'implementation must be non-empty before a ticket can enter ready';
        END IF;
    END IF;

    IF OLD.state <> 'in_progress' AND NEW.state = 'in_progress' THEN
        IF OLD.state = 'analysis' THEN
            RAISE EXCEPTION 'analysis tickets must move through ready before entering in_progress';
        END IF;
        IF NEW.assignee = 'unassigned' THEN
            RAISE EXCEPTION 'assignee must not be unassigned before a ticket can enter in_progress';
        END IF;
        IF btrim(NEW.implementation) = '' THEN
            RAISE EXCEPTION 'implementation must be non-empty before a ticket can enter in_progress';
        END IF;

        current_root := ticket_board.ticket_cluster_root_id(NEW.id, NEW.parent_id);
        SELECT other.id
        INTO conflicting_ticket_id
        FROM ticket_board.tickets AS other
        WHERE other.id <> NEW.id
          AND other.state = 'in_progress'
          AND other.assignee = NEW.assignee
          AND ticket_board.ticket_cluster_root_id(other.id, other.parent_id) <> current_root
        ORDER BY other.ticket_number
        LIMIT 1;

        IF conflicting_ticket_id IS NOT NULL THEN
            RAISE EXCEPTION '% already has an in-progress ticket %; finish or move it first',
                NEW.assignee,
                conflicting_ticket_id;
        END IF;
    END IF;

    IF OLD.state <> 'director_review' AND NEW.state = 'director_review' AND NOT NEW.audit_signoff THEN
        RAISE EXCEPTION 'audit_signoff must be true before a ticket can enter director_review';
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

CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NULL;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object(
                'id', NEW.id,
                'title', NEW.title,
                'old_state', OLD.state,
                'new_state', NEW.state,
                'assignee', NEW.assignee,
                'updated_at', NEW.updated_at,
                'ticket_number', NEW.ticket_number
            )::text
        );
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.upsert_ticket_notification_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.updated_at, NEW.row_updated_at, clock_timestamp());
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_board.ticket_notification_state (
            ticket_id,
            current_state,
            current_assignee,
            previous_state,
            entered_current_state_at,
            last_activity_at,
            last_transition_notified_at
        ) VALUES (
            NEW.id,
            NEW.state,
            NEW.assignee,
            NULL,
            activity_at,
            activity_at,
            activity_at
        )
        ON CONFLICT (ticket_id) DO UPDATE
        SET current_state = EXCLUDED.current_state,
            current_assignee = EXCLUDED.current_assignee,
            previous_state = EXCLUDED.previous_state,
            entered_current_state_at = EXCLUDED.entered_current_state_at,
            last_activity_at = EXCLUDED.last_activity_at,
            last_transition_notified_at = EXCLUDED.last_transition_notified_at,
            last_nudged_at = NULL,
            nudge_count = 0;
        RETURN NULL;
    END IF;

    INSERT INTO ticket_board.ticket_notification_state (
        ticket_id,
        current_state,
        current_assignee,
        previous_state,
        entered_current_state_at,
        last_activity_at,
        last_transition_notified_at,
        last_nudged_at,
        nudge_count
    ) VALUES (
        NEW.id,
        NEW.state,
        NEW.assignee,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE NULL
        END,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN activity_at
            ELSE activity_at
        END,
        activity_at,
        NULL,
        NULL,
        0
    )
    ON CONFLICT (ticket_id) DO UPDATE
    SET current_state = EXCLUDED.current_state,
        current_assignee = EXCLUDED.current_assignee,
        previous_state = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE ticket_board.ticket_notification_state.previous_state
        END,
        entered_current_state_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN EXCLUDED.entered_current_state_at
            ELSE ticket_board.ticket_notification_state.entered_current_state_at
        END,
        last_activity_at = EXCLUDED.last_activity_at,
        last_transition_notified_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN NULL
            ELSE ticket_board.ticket_notification_state.last_transition_notified_at
        END,
        last_nudged_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN NULL
            ELSE ticket_board.ticket_notification_state.last_nudged_at
        END,
        nudge_count = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN 0
            ELSE ticket_board.ticket_notification_state.nudge_count
        END;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.touch_ticket_notification_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.ts, clock_timestamp());
BEGIN
    UPDATE ticket_board.ticket_notification_state
    SET last_activity_at = activity_at,
        nudge_count = 0
    WHERE ticket_id = NEW.ticket_id;
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
        LEFT JOIN ticket_board.tickets blocker ON blocker.id = b.blocker_ticket_id
        WHERE b.ticket_id = p_ticket_id
          AND coalesce(blocker.state NOT IN ('done', 'cancelled'), true)
    );
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
       AND p_assignee <> 'unassigned'
       AND btrim(coalesce(p_implementation, '')) <> ''
       AND NOT coalesce(p_manually_controlled, false)
       AND NOT ticket_board.ticket_has_unresolved_blockers(p_ticket_id);
$$;

CREATE OR REPLACE FUNCTION ticket_board.auto_advance_analysis_ticket()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    IF ticket_board.ticket_can_auto_advance_analysis(
        NEW.state,
        NEW.assignee,
        NEW.implementation,
        NEW.manually_controlled,
        NEW.id
    ) THEN
        UPDATE ticket_board.tickets
        SET state = 'ready'
        WHERE id = NEW.id
          AND state = 'analysis';
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_ticket_board_listener(p_action text)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    role_setting text;
    actor text;
BEGIN
    role_setting := nullif(current_setting('role', true), '');
    IF role_setting IS NULL OR role_setting = 'none' THEN
        actor := session_user;
    ELSE
        actor := role_setting;
    END IF;
    IF actor <> 'ticket_board_listener' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_listener is the only notification reconciler', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_notification_payload(
    p_ticket_id text,
    p_old_state text,
    p_new_state text,
    p_assignee text
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('transition_notification_payload');
    SELECT jsonb_build_object(
        'kind', 'transition',
        'id', t.id,
        'title', t.title,
        'old_state', p_old_state,
        'new_state', p_new_state,
        'assignee', p_assignee,
        'updated_at', t.updated_at,
        'ticket_number', t.ticket_number
    )
    INTO payload
    FROM ticket_board.tickets t
    WHERE t.id = p_ticket_id;
    RETURN payload;
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
    WHERE t.state IN ('analysis', 'ready', 'in_progress', 'audit', 'director_review')
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.mark_transition_notified(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('mark_transition_notified');
    UPDATE ticket_board.ticket_notification_state
    SET last_transition_notified_at = clock_timestamp()
    WHERE ticket_id = p_ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_state IN ('ready', 'in_progress') THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
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
        WHEN p_state = 'ready' THEN 'NUDGE Ready ticket for you: ' || p_ticket_id || ' -- ' || p_title
        WHEN p_state = 'in_progress' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is still in progress'
        WHEN p_state = 'audit' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for audit'
        WHEN p_state = 'director_review' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for your review'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_due_nudges(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_cadence interval DEFAULT interval '5 minutes',
    p_escalate_after integer DEFAULT 3
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    target_role text;
    payload jsonb;
BEGIN
    FOR candidate IN
        SELECT DISTINCT ON (candidates.target_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.last_nudged_at,
                ns.nudge_count,
                ticket_board.nudge_target_role(t.state, t.assignee) AS target_role,
                CASE t.state
                    WHEN 'in_progress' THEN 1
                    WHEN 'ready' THEN 2
                    WHEN 'audit' THEN 3
                    WHEN 'director_review' THEN 4
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE t.state IN ('ready', 'in_progress', 'audit', 'director_review')
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'director' AS target_role,
                CASE t.state
                    WHEN 'analysis' THEN 0
                    WHEN 'backlog' THEN 6
                    ELSE 7
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (
                    (
                        t.state = 'analysis'
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (t.state = 'backlog' AND t.assignee <> 'unassigned')
                )
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
        ) AS candidates
        ORDER BY candidates.target_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at THEN
            target_role := 'director';
            payload := jsonb_build_object(
                'kind', 'escalation',
                'id', candidate.id,
                'title', candidate.title,
                'target_role', target_role,
                'message', 'PRIORITY ' || candidate.id || ' -- ' || candidate.title || ' appears stuck for ' || candidate.assignee || '; check/reassign'
            );
        ELSE
            payload := jsonb_build_object(
                'kind', 'nudge',
                'id', candidate.id,
                'title', candidate.title,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', target_role,
                'message', ticket_board.nudge_message(candidate.id, candidate.title, candidate.state, candidate.assignee)
            );
        END IF;

        PERFORM pg_notify('ticket_board_state_transition', payload::text);
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN candidate.last_activity_at > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.install_nudge_cron()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_cron;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_cron extension for ticket_board_due_nudges not installed: %', SQLERRM;
            RETURN;
        END;
        BEGIN
            PERFORM cron.unschedule('ticket_board_due_nudges');
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        BEGIN
            PERFORM cron.schedule(
                'ticket_board_due_nudges',
                '*/5 * * * *',
                $cron$SELECT ticket_board.notify_due_nudges();$cron$
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_cron schedule for ticket_board_due_nudges not installed: %', SQLERRM;
        END;
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS tickets_enforce_workflow_update ON ticket_board.tickets;
CREATE TRIGGER tickets_enforce_workflow_update
BEFORE UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.enforce_ticket_workflow_update();

DROP TRIGGER IF EXISTS tickets_notify_state_transition ON ticket_board.tickets;
CREATE TRIGGER tickets_notify_state_transition
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
WHEN (OLD.state IS DISTINCT FROM NEW.state)
EXECUTE FUNCTION ticket_board.notify_ticket_state_transition();

DROP TRIGGER IF EXISTS tickets_notification_state_insert ON ticket_board.tickets;
CREATE TRIGGER tickets_notification_state_insert
AFTER INSERT ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.upsert_ticket_notification_state();

DROP TRIGGER IF EXISTS tickets_notification_state_update ON ticket_board.tickets;
CREATE TRIGGER tickets_notification_state_update
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.upsert_ticket_notification_state();

DROP TRIGGER IF EXISTS tickets_zz_auto_advance_analysis ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_auto_advance_analysis
AFTER INSERT OR UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.auto_advance_analysis_ticket();

DROP TRIGGER IF EXISTS ticket_comments_notification_activity ON ticket_board.ticket_comments;
CREATE TRIGGER ticket_comments_notification_activity
AFTER INSERT ON ticket_board.ticket_comments
FOR EACH ROW
EXECUTE FUNCTION ticket_board.touch_ticket_notification_activity();

INSERT INTO ticket_board.ticket_notification_state (
    ticket_id,
    current_state,
    current_assignee,
    previous_state,
    entered_current_state_at,
    last_activity_at,
    last_transition_notified_at
)
SELECT
    id,
    state,
    assignee,
    NULL,
    coalesce(updated_at, row_updated_at, clock_timestamp()),
    coalesce(updated_at, row_updated_at, clock_timestamp()),
    coalesce(updated_at, row_updated_at, clock_timestamp())
FROM ticket_board.tickets
ON CONFLICT (ticket_id) DO NOTHING;

SELECT ticket_board.install_nudge_cron();

CREATE OR REPLACE FUNCTION ticket_board.current_actor_role()
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    role_setting text;
BEGIN
    role_setting := nullif(current_setting('role', true), '');
    IF role_setting IS NULL OR role_setting = 'none' THEN
        RETURN session_user;
    END IF;
    RETURN role_setting;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_actor(
    p_allowed_roles text[],
    p_action text
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_service is the only database writer', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    RETURN actor;
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
    IF actor NOT IN ('director', 'eric', 'main', 'app', 'ops', 'audit', 'perf', 'research') THEN
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
        'blocked_by', blockers,
        'parent_id', ticket_row.parent_id,
        'blocked_reason', ticket_row.blocked_reason,
        'implementation', ticket_row.implementation,
        'audit_prompt', ticket_row.audit_prompt,
        'audit_signoff', ticket_row.audit_signoff,
        'needs_eric_signoff', ticket_row.needs_eric_signoff,
        'eric_signoff', ticket_row.eric_signoff,
        'commit_hash', ticket_row.commit_hash,
        'commit_exempt', ticket_row.commit_exempt,
        'manually_controlled', ticket_row.manually_controlled,
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
    p_text text
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
        source_json
    ) VALUES (
        p_id,
        next_position,
        p_who,
        ts_text_value,
        ts_value,
        p_text,
        jsonb_build_object('who', p_who, 'ts', ts_text_value, 'text', p_text)
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text
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
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'eric'], 'create_ticket');
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;

    ticket_id := ticket_board.next_ticket_id();
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
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
    actor := ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'file_bug');
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
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'route');
    IF new_state NOT IN ('backlog', 'analysis', 'ready', 'in_progress', 'audit', 'eric_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'agent', 'director', 'research') THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    UPDATE ticket_board.tickets
    SET state = new_state,
        assignee = route.assignee
    WHERE tickets.id = route.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
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
    actor := ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'start_work');
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
BEGIN
    actor := ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'submit_to_audit');
    IF normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'audit',
        commit_hash = normalized_commit
    WHERE tickets.id = submit_to_audit.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.audit_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['audit'], 'audit_sign_off');
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
    actor := ticket_board.require_actor(ARRAY['audit'], 'audit_kick_back');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'unassigned'
    WHERE tickets.id = audit_kick_back.id
      AND tickets.state = 'audit';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.eric_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['eric'], 'eric_sign_off');
    UPDATE ticket_board.tickets
    SET eric_signoff = true
    WHERE tickets.id = eric_sign_off.id
      AND tickets.state = 'eric_review';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'eric_review ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.eric_reopen(
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
    actor := ticket_board.require_actor(ARRAY['eric'], 'eric_reopen');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    UPDATE ticket_board.tickets
    SET state = 'analysis'
    WHERE tickets.id = eric_reopen.id
      AND tickets.state IN ('eric_review', 'director_review', 'done');
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
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'mark_done');
    IF normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_hash = normalized_commit
    WHERE tickets.id = mark_done.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
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
    actor := ticket_board.require_actor(ARRAY['director'], 'defer');
    UPDATE ticket_board.tickets
    SET state = 'backlog'
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
    actor := ticket_board.require_actor(ARRAY['director'], 'cancel');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    UPDATE ticket_board.tickets
    SET state = 'cancelled'
    WHERE tickets.id = cancel.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_manually_controlled(
    id text,
    manually_controlled boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    UPDATE ticket_board.tickets
    SET manually_controlled = set_manually_controlled.manually_controlled
    WHERE tickets.id = set_manually_controlled.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
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
    normalized_reason text := coalesce(reason, '');
    blocker_count integer;
    invalid_id text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT upper(btrim(raw_id))
    INTO invalid_id
    FROM unnest(coalesce(ids, ARRAY[]::text[])) AS raw_id
    WHERE upper(btrim(raw_id)) !~ '^PGU-[0-9]+$'
       OR upper(btrim(raw_id)) = set_blockers.id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = set_blockers.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    DELETE FROM ticket_board.ticket_blockers
    WHERE ticket_id = set_blockers.id;

    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
    SELECT set_blockers.id, blocker_id, min(ord)::integer - 1
    FROM (
        SELECT upper(btrim(raw_id)) AS blocker_id, ord
        FROM unnest(coalesce(ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    GROUP BY blocker_id
    ORDER BY min(ord);

    UPDATE ticket_board.tickets
    SET blocked_reason = normalized_reason
    WHERE tickets.id = set_blockers.id;
    PERFORM ticket_board.touch_ticket(id);
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
    actor := ticket_board.require_actor(ARRAY['director', 'eric', 'main', 'app', 'ops', 'audit', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text);
    PERFORM ticket_board.touch_ticket(id);
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
    normalized_parent text;
    current_parent text;
    seen text[];
    screenshot_value text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'eric', 'main', 'app', 'ops', 'audit', 'perf', 'research'],
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
        'parent_id',
        'screenshots',
        'screenshot',
        'implementation',
        'audit_prompt',
        'needs_eric_signoff',
        'commit_exempt',
        'commit_hash',
        'audit_signoff',
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
    IF patch @> '{"eric_signoff": true}'::jsonb THEN
        RAISE EXCEPTION 'eric_signoff=true requires eric_sign_off';
    END IF;
    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = edit_fields.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    IF patch ? 'title' AND btrim(coalesce(patch->>'title', '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
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
        parent_id = CASE WHEN patch ? 'parent_id' THEN upper(btrim(coalesce(patch->>'parent_id', ''))) ELSE parent_id END,
        implementation = CASE WHEN patch ? 'implementation' THEN coalesce(patch->>'implementation', '') ELSE implementation END,
        audit_prompt = CASE WHEN patch ? 'audit_prompt' THEN coalesce(patch->>'audit_prompt', '') ELSE audit_prompt END,
        needs_eric_signoff = CASE WHEN patch ? 'needs_eric_signoff' THEN (patch->>'needs_eric_signoff')::boolean ELSE needs_eric_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        commit_hash = CASE WHEN patch ? 'commit_hash' THEN btrim(coalesce(patch->>'commit_hash', '')) ELSE commit_hash END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN (patch->>'audit_signoff')::boolean ELSE audit_signoff END,
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
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.merge(
    source_id text,
    target_id text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    source_ticket ticket_board.tickets%ROWTYPE;
    target_ticket ticket_board.tickets%ROWTYPE;
    next_position integer;
    next_attachment_position integer;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'merge');
    source_id := upper(btrim(coalesce(source_id, '')));
    target_id := upper(btrim(coalesce(target_id, '')));
    IF source_id = '' OR target_id = '' THEN
        RAISE EXCEPTION 'merge requires both source and target ticket IDs';
    END IF;
    IF source_id = target_id THEN
        RAISE EXCEPTION 'cannot merge a ticket into itself';
    END IF;

    SELECT * INTO source_ticket FROM ticket_board.tickets WHERE id = source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', source_id;
    END IF;
    SELECT * INTO target_ticket FROM ticket_board.tickets WHERE id = target_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', target_id;
    END IF;

    SELECT COALESCE(max(position), -1) + 1
    INTO next_position
    FROM ticket_board.ticket_comments
    WHERE ticket_id = target_id;

    INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, ts, text, source_json)
    SELECT
        target_id,
        next_position + row_number() OVER (ORDER BY position)::integer - 1,
        who,
        ts_text,
        ts,
        '[merged from ' || source_id || '] ' || text,
        jsonb_build_object('who', who, 'ts', ts_text, 'text', '[merged from ' || source_id || '] ' || text)
    FROM ticket_board.ticket_comments
    WHERE ticket_id = source_id
    ORDER BY position;

    PERFORM ticket_board.append_ticket_comment(target_id, 'director', 'Merged in ' || source_id || ': ' || source_ticket.title);

    SELECT COALESCE(max(position), -1) + 1
    INTO next_attachment_position
    FROM ticket_board.ticket_attachments
    WHERE ticket_id = target_id;

    INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field)
    SELECT target_id,
           next_attachment_position + row_number() OVER (ORDER BY position)::integer - 1,
           source.path,
           false,
           source.source_field
    FROM ticket_board.ticket_attachments AS source
    WHERE source.ticket_id = source_id
      AND NOT EXISTS (
          SELECT 1
          FROM ticket_board.ticket_attachments AS existing
          WHERE existing.ticket_id = target_id
            AND existing.path = source.path
      )
    ORDER BY source.position;

    UPDATE ticket_board.tickets
    SET manually_controlled = true
    WHERE id = source_id;
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_exempt = true,
        manually_controlled = source_ticket.manually_controlled
    WHERE id = source_id;
    PERFORM ticket_board.append_ticket_comment(source_id, 'director', 'Merged into ' || target_id);
    PERFORM ticket_board.touch_ticket(source_id);
    PERFORM ticket_board.touch_ticket(target_id);
END;
$$;

INSERT INTO ticket_board.schema_migrations (version, description)
VALUES (1, 'initial ticket-board schema for JSON ticket import')
ON CONFLICT (version) DO NOTHING;

COMMIT;
