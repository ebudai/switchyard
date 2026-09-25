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

CREATE SEQUENCE IF NOT EXISTS ticket_board.schema_migrations_seq AS bigint;

CREATE TABLE IF NOT EXISTS ticket_board.schema_migrations (
    name text PRIMARY KEY,
    seq bigint NOT NULL DEFAULT nextval('ticket_board.schema_migrations_seq'),
    applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER SEQUENCE ticket_board.schema_migrations_seq
    OWNED BY ticket_board.schema_migrations.seq;

CREATE UNIQUE INDEX IF NOT EXISTS schema_migrations_seq_idx
    ON ticket_board.schema_migrations (seq);

CREATE TABLE IF NOT EXISTS ticket_board.tickets (
    id text PRIMARY KEY
        CHECK (id ~ '^[A-Z][A-Z0-9]*-[0-9]+$'),
    ticket_number integer GENERATED ALWAYS AS ((substring(id from '^[A-Z][A-Z0-9]*-([0-9]+)$'))::integer) STORED,
    CONSTRAINT tickets_ticket_number_check CHECK (ticket_number > 0),

    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    state text NOT NULL
        CHECK (state IN (
            'draft',
            'backlog',
            'analysis',
            'in_progress',
            'inspection',
            'audit',
            'user_review',
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
            'inspector',
            'agent',
            'director',
            'research',
            'user'
        )),

    -- Empty string is preserved because the JSON store uses "" rather than null
    -- for "no parent" on newer tickets, while older tickets omit parent_id.
    parent_id text NOT NULL DEFAULT ''
        CHECK (parent_id = '' OR parent_id ~ '^[A-Z][A-Z0-9]*-[0-9]+$'),
    origin_project text NOT NULL DEFAULT '',
    external_source_ref text NOT NULL DEFAULT '',
    blocked_reason text NOT NULL DEFAULT '',

    implementation text NOT NULL DEFAULT '',
    audit_prompt text NOT NULL DEFAULT '',
    audit_signoff boolean NOT NULL DEFAULT false,
    needs_audit boolean NOT NULL DEFAULT true,
    needs_inspection boolean NOT NULL DEFAULT false,
    inspector_signoff boolean NOT NULL DEFAULT false,
    needs_user_signoff boolean NOT NULL DEFAULT false,
    user_signoff boolean NOT NULL DEFAULT false,
    manually_controlled boolean NOT NULL DEFAULT false,
    parked boolean NOT NULL DEFAULT false,

    -- Historical tickets include both full 40-char hashes and short hashes.
    commit_hash text NOT NULL DEFAULT ''
        CHECK (commit_hash = '' OR commit_hash ~ '^[0-9A-Fa-f]{7,40}$'),
    last_rejected_commit text
        CHECK (
            last_rejected_commit IS NULL
            OR last_rejected_commit = ''
            OR last_rejected_commit ~ '^[0-9A-Fa-f]{7,40}$'
        ),
    commit_exempt boolean NOT NULL DEFAULT false,
    regression boolean NOT NULL DEFAULT false,

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

ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS parked boolean NOT NULL DEFAULT false;
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS needs_inspection boolean NOT NULL DEFAULT false;
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS needs_audit boolean NOT NULL DEFAULT true;
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS inspector_signoff boolean NOT NULL DEFAULT false;
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS regression boolean NOT NULL DEFAULT false;
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS origin_project text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS external_source_ref text NOT NULL DEFAULT '';
-- SYRD-31: when serial focus redirects a route away from a reserved
-- implementer, remember which implementer the router actually asked for.
-- Without it the redirect is indistinguishable from a no-op: the configured
-- queue destination is the stage the ticket already sat in, so the action
-- returned the unchanged ticket and the intent was lost.
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS queued_for_assignee text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.tickets
    ADD COLUMN IF NOT EXISTS queued_behind_ticket text NOT NULL DEFAULT '';
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
        'user_review',
        'director_review',
        'done',
        'cancelled'
    ));
ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_assignee_check;
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_assignee_check CHECK (assignee IN (
        'unassigned',
        'main',
        'app',
        'perf',
        'ops',
        'audit',
        'inspector',
        'agent',
        'director',
        'research',
        'user'
    ));
ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_in_progress_assignee_check;
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
        CHECK (blocker_ticket_id ~ '^([a-z][a-z0-9_]*:)?[A-Z][A-Z0-9]*-[0-9]+$|^operator:[a-z][a-z0-9_]*$'),
    position integer NOT NULL CHECK (position >= 0),
    resolved boolean NOT NULL DEFAULT false,
    PRIMARY KEY (ticket_id, blocker_ticket_id),
    UNIQUE (ticket_id, position),
    CHECK (ticket_id <> blocker_ticket_id)
);

ALTER TABLE ticket_board.ticket_blockers
    ADD COLUMN IF NOT EXISTS resolved boolean NOT NULL DEFAULT false;

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
    urgent boolean NOT NULL DEFAULT false,
    source_json jsonb NOT NULL,
    UNIQUE (ticket_id, position),
    CHECK (source_json ? 'who'),
    CHECK (source_json ? 'ts'),
    CHECK (source_json ? 'text')
);
ALTER TABLE ticket_board.ticket_comments
    ADD COLUMN IF NOT EXISTS urgent boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS ticket_comments_ticket_ts_idx
    ON ticket_board.ticket_comments (ticket_id, ts NULLS LAST, position);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_attachments (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    path text NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    source_field text NOT NULL
        CHECK (source_field IN ('screenshots', 'screenshot')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ticket_id, position),
    UNIQUE (ticket_id, path)
);
ALTER TABLE ticket_board.ticket_attachments
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS ticket_attachments_path_idx
    ON ticket_board.ticket_attachments (path);

CREATE TABLE IF NOT EXISTS ticket_board.workflow_stages (
    name text PRIMARY KEY
        CHECK (name IN (
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
        )),
    display_label text NOT NULL CHECK (btrim(display_label) <> ''),
    rank integer NOT NULL UNIQUE CHECK (rank >= 0),
    owner_roles text[] NOT NULL DEFAULT ARRAY[]::text[],
    entry_gate_field text,
    gate_skip_to text REFERENCES ticket_board.workflow_stages(name),
    exit_signoff_field text,
    is_terminal boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS ticket_board.workflow_transitions (
    from_stage text NOT NULL REFERENCES ticket_board.workflow_stages(name),
    to_stage text NOT NULL REFERENCES ticket_board.workflow_stages(name),
    action_name text NOT NULL CHECK (btrim(action_name) <> ''),
    allowed_roles text[] NOT NULL DEFAULT ARRAY[]::text[],
    owner_scoped boolean NOT NULL DEFAULT false,
    director_override boolean NOT NULL DEFAULT false,
    PRIMARY KEY (from_stage, to_stage, action_name)
);

ALTER TABLE ticket_board.workflow_transitions
    ADD COLUMN IF NOT EXISTS owner_scoped boolean NOT NULL DEFAULT false;

ALTER TABLE ticket_board.workflow_transitions
    ADD COLUMN IF NOT EXISTS director_override boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS ticket_board.workflow_transition_shadow_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT clock_timestamp(),
    ticket_id text,
    from_state text,
    to_state text,
    actor text,
    hardcoded_allowed boolean NOT NULL,
    config_allowed boolean NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_board.workflow_transition_rbac_shadow_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT clock_timestamp(),
    ticket_id text,
    action_name text NOT NULL,
    actor text,
    ticket_assignee text,
    hardcoded_allowed boolean NOT NULL,
    config_allowed boolean NOT NULL,
    owner_scoped boolean NOT NULL
);

-- The built-in workflow is the DEFAULT for a board that does not have one yet,
-- not an authority over a board that does. A provisioned project replaces these
-- rows with its own and narrows workflow_stages_name_check to the stage names it
-- declares, so replaying this file on such a board tried to insert 'backlog' --
-- a name that board no longer admits -- and the supported recovery died on
-- `new row for relation "workflow_stages" violates check constraint
-- "workflow_stages_name_check"` (SYRD-160). The constraint was right. The seed
-- was wrong to assert itself over a configured board, and where the names did
-- happen to match it would have silently reset that board's labels and
-- owner_roles to the built-in ones -- the same defect without an error message.
--
-- So it seeds an empty board and leaves a configured one alone. Changes to the
-- built-in workflow reach boards that already have it through migrations, which
-- is how 'dat' was added, and that is also what lets those boards keep the
-- constraint as tight as they declared it.
DO $seed_builtin_stages$
BEGIN
IF EXISTS (SELECT 1 FROM ticket_board.workflow_stages) THEN
    RETURN;
END IF;
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
    ('draft', 'Draft', 0, ARRAY[]::text[], NULL, NULL, NULL, false),
    ('backlog', 'Backlog', 1, ARRAY[]::text[], NULL, NULL, NULL, false),
    ('analysis', 'Triage', 2, ARRAY['director']::text[], NULL, NULL, NULL, false),
    ('in_progress', 'Implementation', 3, ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], NULL, NULL, NULL, false),
    ('inspection', 'Inspection', 4, ARRAY['inspector']::text[], 'needs_inspection', 'audit', 'inspector_signoff', false),
    ('audit', 'Audit', 5, ARRAY['audit']::text[], 'needs_audit', 'dat', 'audit_signoff', false),
    ('dat', 'DAT', 6, ARRAY['director']::text[], 'needs_user_signoff', 'director_review', NULL, false),
    ('user_review', 'UAT', 7, ARRAY['user']::text[], 'needs_user_signoff', 'director_review', 'user_signoff', false),
    ('director_review', 'Final Sign-Off', 8, ARRAY['director']::text[], NULL, NULL, NULL, false),
    ('done', 'Done', 9, ARRAY[]::text[], NULL, NULL, NULL, true),
    ('cancelled', 'Cancelled', 10, ARRAY[]::text[], NULL, NULL, NULL, true)
ON CONFLICT (name) DO UPDATE
SET display_label = EXCLUDED.display_label,
    rank = EXCLUDED.rank,
    owner_roles = EXCLUDED.owner_roles,
    entry_gate_field = EXCLUDED.entry_gate_field,
    gate_skip_to = EXCLUDED.gate_skip_to,
    exit_signoff_field = EXCLUDED.exit_signoff_field,
    is_terminal = EXCLUDED.is_terminal;
END
$seed_builtin_stages$;

-- Guarded for the same reason, and on its own table, because a board's
-- transitions are its own the moment it has any: replaying the built-in set
-- would reinstate moves a configured board deliberately does not offer, and
-- most of them name a stage such a board may not have at all.
DO $seed_builtin_transitions$
BEGIN
IF EXISTS (SELECT 1 FROM ticket_board.workflow_transitions) THEN
    RETURN;
END IF;
INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override)
VALUES
    ('draft', 'analysis', 'release_draft', ARRAY['director', 'user']::text[], false, false),
    ('draft', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('backlog', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('backlog', 'analysis', 'start_task', ARRAY['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector']::text[], true, true),
    ('backlog', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('backlog', 'in_progress', 'start_work', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], false, false),
    ('backlog', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('analysis', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('analysis', 'in_progress', 'start_work', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], false, false),
    ('analysis', 'user_review', 'route', ARRAY['director']::text[], false, false),
    ('analysis', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('analysis', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('in_progress', 'inspection', 'submit_to_inspection', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false),
    ('in_progress', 'audit', 'submit_to_audit', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], false, false),
    ('in_progress', 'audit', 'submit_to_audit_without_commit', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false),
    ('in_progress', 'analysis', 'request_commit_exempt', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false),
    ('in_progress', 'analysis', 'implementer_kick_back', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[], true, false),
    ('in_progress', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('in_progress', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('in_progress', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('inspection', 'audit', 'inspector_sign_off', ARRAY['inspector']::text[], false, false),
    ('inspection', 'audit', 'route', ARRAY['director']::text[], false, false),
    ('inspection', 'in_progress', 'inspector_kick_back', ARRAY['inspector']::text[], false, false),
    ('inspection', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('inspection', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('inspection', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('audit', 'dat', 'audit_sign_off', ARRAY['audit']::text[], false, false),
    ('audit', 'director_review', 'audit_sign_off', ARRAY['audit']::text[], false, false),
    ('audit', 'in_progress', 'audit_kick_back', ARRAY['audit']::text[], false, false),
    ('audit', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('audit', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('audit', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('audit', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('dat', 'director_review', 'entry_gate_skip', ARRAY[]::text[], false, false),
    ('dat', 'user_review', 'director_dat_sign_off', ARRAY['director']::text[], false, false),
    ('dat', 'in_progress', 'director_dat_kick_back', ARRAY['director']::text[], false, false),
    ('dat', 'analysis', 'director_dat_kick_back', ARRAY['director']::text[], false, false),
    ('dat', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('dat', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('dat', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('dat', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('user_review', 'inspection', 'route', ARRAY['director']::text[], false, false),
    ('user_review', 'director_review', 'user_sign_off', ARRAY['user']::text[], false, false),
    ('user_review', 'audit', 'route', ARRAY['director']::text[], false, false),
    ('user_review', 'analysis', 'user_reopen', ARRAY['user']::text[], false, false),
    ('user_review', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('user_review', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('user_review', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('director_review', 'done', 'mark_done', ARRAY['director']::text[], false, false),
    ('director_review', 'in_progress', 'route', ARRAY['director']::text[], false, false),
    ('director_review', 'analysis', 'user_reopen', ARRAY['user']::text[], false, false),
    ('director_review', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('director_review', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('director_review', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),
    ('done', 'analysis', 'user_reopen', ARRAY['user']::text[], false, false),
    ('done', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('done', 'backlog', 'defer', ARRAY['director']::text[], false, false),
    ('cancelled', 'analysis', 'route', ARRAY['director']::text[], false, false),
    ('cancelled', 'backlog', 'defer', ARRAY['director']::text[], false, false)
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles,
    owner_scoped = EXCLUDED.owner_scoped,
    director_override = EXCLUDED.director_override;
END
$seed_builtin_transitions$;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_notification_state (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    current_state text NOT NULL,
    current_assignee text NOT NULL,
    previous_state text,
    entered_current_state_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_activity_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_transition_notified_at timestamptz,
    last_nudged_at timestamptz,
    nudge_count integer NOT NULL DEFAULT 0 CHECK (nudge_count >= 0),
    idle_reminder_count integer NOT NULL DEFAULT 0 CHECK (idle_reminder_count >= 0),
    -- WHEN the last idle reminder was delivered, beside how many there were.
    -- The count alone cannot say whether the owner has had any time to act on
    -- it, and an escalation that says "was reminded ... and still hasn't
    -- advanced" is a claim about elapsed time (SYRD-163).
    last_idle_reminder_at timestamptz,
    awaiting_role text NOT NULL DEFAULT ''
        CHECK (
            awaiting_role = ''
            OR awaiting_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research')
        ),
    awaiting_since_at timestamptz,
    last_implementer_assignee text NOT NULL DEFAULT ''
        CHECK (
            last_implementer_assignee = ''
            OR last_implementer_assignee IN ('main', 'app', 'perf', 'ops', 'research')
        )
);
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS last_implementer_assignee text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS last_idle_reminder_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_role text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_since_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS awaiting_notified_since_at timestamptz;
-- Which serial-focus reservation this ticket has already been woken out of,
-- as `<queued_for>:<queued_behind>`. The wake is one notification per
-- reservation identity, and the identity has to outlive a listener restart --
-- the queue row does not (SYRD-109).
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS serial_focus_wake_key text NOT NULL DEFAULT '';

-- Which stall the director has already been escalated about (SYRD-133). The
-- escalation's dedupe key holds only while its queue row exists; once the
-- director acknowledges it the row is deleted, and the next wave -- still
-- finding an idle owner and an unmoved ticket -- enqueues the same escalation
-- again. On SYRD-131 that produced a delivered escalation at 15:43:19 and
-- another enqueued at 15:44:15. This marker survives the acknowledgement, so
-- the fail-safe fires once per stall rather than once per wave, and re-arms
-- when the stall's identity changes.
ALTER TABLE ticket_board.ticket_notification_state
    ADD COLUMN IF NOT EXISTS idle_escalation_key text NOT NULL DEFAULT '';
ALTER TABLE ticket_board.ticket_notification_state
    DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
ALTER TABLE ticket_board.ticket_notification_state
    ADD CONSTRAINT ticket_notification_state_awaiting_role_check
    CHECK (
        awaiting_role = ''
        OR awaiting_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research')
    );

CREATE INDEX IF NOT EXISTS ticket_notification_state_due_idx
    ON ticket_board.ticket_notification_state (
        current_state,
        current_assignee,
        entered_current_state_at,
        last_transition_notified_at,
        last_nudged_at
    );

CREATE TABLE IF NOT EXISTS ticket_board.ticket_notification_queue (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder')),
    target_role text NOT NULL
        CHECK (target_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research')),
    message text NOT NULL CHECK (btrim(message) <> ''),
    payload jsonb NOT NULL,
    dedupe_key text NOT NULL UNIQUE,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    last_error text,
    dead_lettered_at timestamptz,
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS dead_lettered_at timestamptz;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS terminal_reason text;
-- SYRD-159: which version of this row exists, and which version a listener took
-- to deliver. A collapsing dedupe key means a pending row can be refreshed while
-- it is being delivered -- that is what the collapse is for -- so removing it by
-- id alone deletes the refreshed payload as the acknowledgement of the payload
-- it replaced, and the update that arrived mid-flight is never sent to anybody.
-- The claim records what it took; removal refuses when the row has moved on.
--
-- Recorded by the claim rather than carried by the caller, so no function
-- changes shape: a listener cannot be wrong about what it delivered, and every
-- historical migration that recreates these functions still applies.
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 1;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD COLUMN IF NOT EXISTS claimed_revision bigint;

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_target_role_check
    CHECK (target_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research'));

ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder', 'awaiting_role'));

CREATE INDEX IF NOT EXISTS ticket_notification_queue_due_idx
    ON ticket_board.ticket_notification_queue (next_attempt_at, id)
    WHERE claimed_at IS NULL AND dead_lettered_at IS NULL;

CREATE INDEX IF NOT EXISTS ticket_notification_queue_claimed_idx
    ON ticket_board.ticket_notification_queue (claimed_at, next_attempt_at, id)
    WHERE claimed_at IS NOT NULL AND dead_lettered_at IS NULL;

-- SYRD-135: one row per (ticket, role) whose CLI session was cleared before the
-- ticket's first handoff to that role. The listener asks before it clears and
-- records only after the clear actually succeeded, so a failed clear is retried
-- and a succeeded one is never repeated -- across retries, restarts, duplicate
-- notifications, comments, same-stage reroutes, and a later return of the same
-- ticket to the same role. A different ticket is a different row, which is what
-- makes "once per ticket" mean what it says.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_role_session_clears (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (btrim(role) <> ''),
    cleared_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ticket_id, role)
);

CREATE TABLE IF NOT EXISTS ticket_board.notification_trace (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT clock_timestamp(),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    notification_id bigint,
    target_role text,
    kind text,
    event text NOT NULL CHECK (btrim(event) <> ''),
    ticket_state_at_event text,
    ticket_assignee_at_event text,
    pane_busy_determination text,
    busy_reason text,
    region_digest text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS notification_trace_ticket_ts_idx
    ON ticket_board.notification_trace (ticket_id, ts DESC, id DESC);

CREATE INDEX IF NOT EXISTS notification_trace_notification_idx
    ON ticket_board.notification_trace (notification_id, ts, id)
    WHERE notification_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notification_trace_send_lookup_idx
    ON ticket_board.notification_trace (
        ticket_id,
        target_role,
        ticket_state_at_event,
        ts DESC,
        id DESC
    )
    WHERE kind = 'transition' AND event = 'send';

CREATE OR REPLACE FUNCTION ticket_board.record_notification_trace(
    p_ticket_id text,
    p_notification_id bigint,
    p_target_role text,
    p_kind text,
    p_event text,
    p_pane_busy_determination text DEFAULT NULL,
    p_busy_reason text DEFAULT NULL,
    p_region_digest text DEFAULT NULL,
    p_detail jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    current_state text;
    current_assignee text;
BEGIN
    IF btrim(coalesce(p_event, '')) = '' THEN
        RAISE EXCEPTION 'notification trace event must be non-empty';
    END IF;

    SELECT t.state, t.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets t
    WHERE t.id = p_ticket_id;

    INSERT INTO ticket_board.notification_trace (
        ticket_id,
        notification_id,
        target_role,
        kind,
        event,
        ticket_state_at_event,
        ticket_assignee_at_event,
        pane_busy_determination,
        busy_reason,
        region_digest,
        detail
    ) VALUES (
        p_ticket_id,
        p_notification_id,
        p_target_role,
        p_kind,
        p_event,
        current_state,
        current_assignee,
        p_pane_busy_determination,
        p_busy_reason,
        p_region_digest,
        coalesce(p_detail, '{}'::jsonb)
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.prune_notification_trace(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_diagnostic_retention interval DEFAULT interval '1 hour'
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    deleted_count bigint;
BEGIN
    DELETE FROM ticket_board.notification_trace
    WHERE ts < p_now - p_diagnostic_retention
      AND event IN ('claim', 'listener_claim', 'requeue', 'gate_defer');

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

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
        OR (p_from_state = 'analysis' AND p_to_state IN ('in_progress', 'user_review', 'backlog', 'cancelled'))
        OR (p_from_state = 'in_progress' AND p_to_state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'inspection' AND p_to_state IN ('audit', 'in_progress', 'backlog', 'cancelled'))
        OR (p_from_state = 'audit' AND p_to_state IN ('dat', 'director_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'dat' AND p_to_state IN ('director_review', 'user_review', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'user_review' AND p_to_state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'director_review' AND p_to_state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled'))
        OR (p_from_state = 'done' AND p_to_state IN ('analysis', 'backlog'))
        OR (p_from_state = 'cancelled' AND p_to_state IN ('analysis', 'backlog'));
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_allowed_config(
    p_from_state text,
    p_to_state text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_transitions wt
        WHERE wt.from_stage = p_from_state
          AND wt.to_stage = p_to_state
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.log_workflow_transition_shadow_mismatch(
    p_ticket_id text,
    p_from_state text,
    p_to_state text,
    p_actor text,
    p_hardcoded_allowed boolean,
    p_config_allowed boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    IF p_hardcoded_allowed IS DISTINCT FROM p_config_allowed THEN
        INSERT INTO ticket_board.workflow_transition_shadow_log (
            ticket_id,
            from_state,
            to_state,
            actor,
            hardcoded_allowed,
            config_allowed
        ) VALUES (
            p_ticket_id,
            p_from_state,
            p_to_state,
            p_actor,
            p_hardcoded_allowed,
            p_config_allowed
        );
        RAISE WARNING 'workflow transition shadow mismatch for %: % -> % actor=% hardcoded=% config=%',
            p_ticket_id,
            p_from_state,
            p_to_state,
            p_actor,
            p_hardcoded_allowed,
            p_config_allowed;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_is_implementer_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_stages ws
        WHERE ws.name = 'in_progress'
          AND btrim(lower(coalesce(p_assignee, ''))) = ANY(ws.owner_roles)
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_valid_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH normalized AS (
        SELECT btrim(lower(coalesce(p_assignee, ''))) AS role
    )
    SELECT role = 'unassigned'
        OR role = 'agent'
        OR EXISTS (
            SELECT 1
            FROM ticket_board.workflow_stages ws
            WHERE role = ANY(ws.owner_roles)
        )
    FROM normalized;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_stage_owner_assignee(p_state text, p_assignee text)
RETURNS void
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    owners text[];
    normalized_assignee text := btrim(lower(coalesce(p_assignee, '')));
BEGIN
    SELECT ws.owner_roles
    INTO owners
    FROM ticket_board.workflow_stages ws
    WHERE ws.name = p_state;

    IF NOT FOUND OR coalesce(cardinality(owners), 0) = 0 THEN
        RETURN;
    END IF;

    IF normalized_assignee = 'unassigned' OR normalized_assignee = ANY(owners) THEN
        RETURN;
    END IF;

    RAISE EXCEPTION 'assignee % is not an owner of stage % (owners: %)',
        coalesce(nullif(btrim(coalesce(p_assignee, '')), ''), '<empty>'),
        p_state,
        array_to_string(owners, ', ');
END;
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
        WHEN p_state = 'user_review' AND p_assignee = 'user' THEN NULL
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
              AND NOT (t.state = 'user_review' AND t.assignee = 'user')
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_kickback_target_assignee(
    p_ticket_id text,
    p_target_assignee text DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    normalized_target text := btrim(lower(coalesce(p_target_assignee, '')));
    tracked_target text;
    current_target text;
BEGIN
    IF normalized_target <> '' THEN
        IF NOT ticket_board.ticket_is_implementer_assignee(normalized_target) THEN
            RAISE EXCEPTION 'kickback target assignee must be an implementation-stage owner';
        END IF;
        RETURN normalized_target;
    END IF;

    SELECT nullif(ns.last_implementer_assignee, '')
    INTO tracked_target
    FROM ticket_board.ticket_notification_state ns
    WHERE ns.ticket_id = p_ticket_id;
    IF ticket_board.ticket_is_implementer_assignee(tracked_target) THEN
        RETURN tracked_target;
    END IF;

    SELECT t.assignee
    INTO current_target
    FROM ticket_board.tickets t
    WHERE t.id = p_ticket_id;
    IF ticket_board.ticket_is_implementer_assignee(current_target) THEN
        RETURN current_target;
    END IF;

    RETURN 'ops';
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state = 'draft' THEN
        NEW.assignee := coalesce(ticket_board.stage_default_assignee('draft'), 'unassigned');
        NEW.parked := false;
    END IF;
    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);
    IF NEW.state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(NEW.assignee)
       AND ticket_board.ticket_current_reserved_ticket(NEW.assignee, NEW.id) IS NOT NULL THEN
        NEW.state := 'backlog';
        NEW.parked := false;
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
    hardcoded_transition_allowed boolean;
    config_transition_allowed boolean;
    shadow_actor text;
    transition_check_state text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        RETURN ticket_board.enforce_declared_ticket_update(OLD,NEW);
    END IF;
    IF NEW.state = 'draft' THEN
        IF OLD.state IS DISTINCT FROM NEW.state THEN
            NEW.assignee := 'unassigned';
        END IF;
        NEW.parked := false;
    END IF;

    IF NEW.state <> 'backlog' OR OLD.state IN ('done', 'cancelled') THEN
        NEW.parked := false;
    END IF;

    IF current_setting('ticket_board.force_move', true) = 'on'
       OR nullif(current_setting('ticket_board.director_edit_target', true), '') = NEW.id THEN
        RETURN NEW;
    END IF;

    IF OLD.state = 'audit' AND NEW.state = 'analysis' THEN
        NEW.assignee := 'unassigned';
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'dat'
       AND NOT NEW.needs_user_signoff
       AND (OLD.needs_user_signoff OR OLD.state IS DISTINCT FROM NEW.state) THEN
        NEW.state := 'director_review';
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
        NEW.state := 'director_review';
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
    IF NOT NEW.needs_audit THEN
        NEW.audit_signoff := false;
    END IF;
    -- A sign-off is a review of one commit (SYRD-271), on this undeclared path
    -- as on the declared one: entering audit already signed skips it below.
    -- Different work leaving implementation is reviewed afresh. submit_to_audit
    -- here already clears Audit's and the Inspector's sign-offs, but never
    -- the User's, which skips user_review below just the same.
    IF OLD.state = 'in_progress' AND NEW.state IS DISTINCT FROM OLD.state
       AND (NEW.commit_hash IS DISTINCT FROM OLD.commit_hash OR NEW.commit_exempt IS DISTINCT FROM OLD.commit_exempt)
       AND (btrim(NEW.commit_hash) <> '' OR NEW.commit_exempt) THEN
        -- Carried from the old commit, not granted by this write.
        NEW.audit_signoff := NEW.audit_signoff AND NOT OLD.audit_signoff;
        NEW.inspector_signoff := NEW.inspector_signoff AND NOT OLD.inspector_signoff;
        NEW.user_signoff := NEW.user_signoff AND NOT OLD.user_signoff;
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        NEW.state := 'inspection';
        NEW.inspector_signoff := false;
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    transition_check_state := NEW.state;
    IF OLD.state IN ('in_progress', 'inspection') AND NEW.state = 'audit' AND NOT NEW.needs_audit THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.audit_signoff := false;
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
        transition_check_state := NEW.state;
    END IF;
    IF NEW.state = 'audit' AND NEW.audit_signoff THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.assignee := 'director';
        transition_check_state := NEW.state;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF NEW.state = 'done'
       AND OLD.state IN ('backlog', 'analysis')
       AND current_setting('ticket_board.utility_task_complete', true) = 'on'
       AND NEW.commit_exempt THEN
        RETURN NEW;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        transition_check_state := coalesce(transition_check_state, NEW.state);
        hardcoded_transition_allowed := ticket_board.workflow_transition_allowed_hardcoded(OLD.state, transition_check_state);
        config_transition_allowed := ticket_board.workflow_transition_allowed_config(OLD.state, transition_check_state);
        shadow_actor := coalesce(nullif(current_setting('ticket_board.caller_role', true), ''), current_user);
        PERFORM ticket_board.log_workflow_transition_shadow_mismatch(
            NEW.id,
            OLD.state,
            transition_check_state,
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
       AND NEW.needs_audit
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
       AND NOT (coalesce(transition_check_state, NEW.state) = 'audit' AND NOT NEW.needs_audit)
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
    IF NEW.state = 'done'
       AND OLD.state <> 'done'
       AND NOT ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state) THEN
        RAISE EXCEPTION 'tickets can only enter done through a configured transition';
    END IF;
    IF OLD.state <> 'done' AND NEW.state = 'done' AND NOT NEW.commit_exempt AND btrim(NEW.commit_hash) = '' THEN
        RAISE EXCEPTION 'commit_hash is required before a ticket can enter done';
    END IF;

    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_transition_notification(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_assignee text,
    p_updated_at timestamptz,
    p_ticket_number integer,
    p_message text
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_role text;
    message text;
    source_role text;
    dedupe_key text;
BEGIN
    target_role := ticket_board.transition_target_role(p_new_state, p_assignee);
    IF target_role IS NULL AND p_new_state IN ('done', 'cancelled') THEN
        target_role := ticket_board.transition_target_role(p_old_state, p_assignee);
    END IF;
    message := p_message;
    source_role := nullif(current_setting('ticket_board.notification_source_role', true), '');
    IF source_role IS NULL THEN
        source_role := nullif(current_setting('ticket_board.caller_role', true), '');
    END IF;
    IF target_role IS NOT NULL AND message IS NOT NULL AND source_role IS DISTINCT FROM target_role THEN
        dedupe_key := 'transition:' || p_ticket_id || ':' || coalesce(p_old_state, 'insert') || ':' || p_new_state || ':' || pg_current_xact_id()::text;
        IF p_new_state IN ('analysis', 'in_progress')
           AND message LIKE 'New ticket for you: %' THEN
            dedupe_key := 'transition:new_ticket:' || p_ticket_id || ':' || target_role || ':' || pg_current_xact_id()::text;
        END IF;
        PERFORM ticket_board.enqueue_notification(
            p_ticket_id,
            'transition',
            target_role,
            message,
            jsonb_build_object(
                'kind', 'transition',
                'id', p_ticket_id,
                'title', p_title,
                'old_state', p_old_state,
                'new_state', p_new_state,
                'assignee', p_assignee,
                'updated_at', p_updated_at,
                'ticket_number', p_ticket_number,
                'target_role', target_role,
                'message', message
            ),
            dedupe_key
        );
    ELSE
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object('kind', 'wake', 'id', p_ticket_id, 'new_state', p_new_state)::text
        );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_state text;
    current_assignee text;
    message text;
    recommendation text;
BEGIN
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF current_setting('ticket_board.suppress_transition_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.state IS NOT DISTINCT FROM NEW.state THEN
            RETURN NULL;
        END IF;
        -- Manual control is a deliberate silence: no nudges, no reminders, no
        -- announcement of every edit while the Director steers a ticket by
        -- hand. It was also silencing the one message that is not a reminder.
        -- An actionable transition that moves work to a DIFFERENT owner is a
        -- handoff, and the new owner is the one person who cannot be expected
        -- to find out any other way -- on the live case, Audit sat idle while
        -- the work waited for it, and a Director had to hand-deliver a comment
        -- (SYRD-107).
        --
        -- Narrow on purpose, and every clause is a case that must stay silent:
        -- a destination nobody owns or that notifies nobody has no target at
        -- all, and a move that keeps the same owner is not a handoff.
        IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
            IF ticket_board.transition_target_role(NEW.state, NEW.assignee) IS NULL
               OR ticket_board.transition_target_role(NEW.state, NEW.assignee)
                  IS NOT DISTINCT FROM ticket_board.transition_target_role(OLD.state, OLD.assignee) THEN
                RETURN NULL;
            END IF;
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(NEW.id) THEN
            RETURN NULL;
        END IF;
        message := ticket_board.transition_message(
            NEW.id,
            NEW.title,
            OLD.state,
            NEW.state,
            ticket_board.ticket_state_already_announced(
                NEW.id,
                ticket_board.transition_target_role(NEW.state, NEW.assignee),
                NEW.state
            )
        );
        IF OLD.state = 'inspection' AND NEW.state = 'in_progress' THEN
            SELECT c.text
            INTO recommendation
            FROM ticket_board.ticket_comments AS c
            WHERE c.ticket_id = NEW.id
              AND btrim(c.text) <> ''
              AND c.xmin = pg_current_xact_id()::xid
            ORDER BY c.position DESC
            LIMIT 1;
            IF recommendation IS NOT NULL THEN
                message := message || ': ' || recommendation;
            END IF;
        END IF;

        PERFORM ticket_board.enqueue_transition_notification(
            NEW.id,
            NEW.title,
            OLD.state,
            NEW.state,
            NEW.assignee,
            NEW.updated_at,
            NEW.ticket_number,
            message
        );
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF coalesce(NEW.manually_controlled, false) THEN
            RETURN NULL;
        END IF;

        SELECT state, assignee
        INTO current_state, current_assignee
        FROM ticket_board.tickets
        WHERE id = NEW.id;
        IF current_state IS DISTINCT FROM NEW.state OR current_assignee IS DISTINCT FROM NEW.assignee THEN
            RETURN NULL;
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(NEW.id) THEN
            RETURN NULL;
        END IF;

        IF ticket_board.transition_target_role(NEW.state, NEW.assignee) IS NOT NULL THEN
            PERFORM ticket_board.enqueue_transition_notification(
                NEW.id,
                NEW.title,
                NULL,
                NEW.state,
                NEW.assignee,
                NEW.updated_at,
                NEW.ticket_number,
                ticket_board.transition_message(NEW.id, NEW.title, NULL, NEW.state, false)
            );
        END IF;
        RETURN NULL;
    END IF;

    RAISE EXCEPTION 'unsupported trigger operation for notify_ticket_state_transition: %', TG_OP;
END;
$$;
DROP FUNCTION IF EXISTS ticket_board.notify_ticket_insert_transition();

CREATE OR REPLACE FUNCTION ticket_board.notify_unblocked_dependents()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    dependent record;
BEGIN
    IF TG_OP <> 'UPDATE'
       OR OLD.state IS NOT DISTINCT FROM NEW.state
       OR OLD.state IN ('done', 'cancelled')
       OR NEW.state NOT IN ('done', 'cancelled') THEN
        RETURN NULL;
    END IF;

    FOR dependent IN
        SELECT t.id, t.title, t.state, t.assignee, t.updated_at, t.ticket_number
        FROM ticket_board.ticket_blockers b
        JOIN ticket_board.tickets t ON t.id = b.ticket_id
        WHERE b.blocker_ticket_id = NEW.id
          AND NOT t.manually_controlled
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
          AND ticket_board.unblock_transition_target_role(t.state, t.assignee) IS NOT NULL
        ORDER BY t.ticket_number
    LOOP
        PERFORM ticket_board.enqueue_unblock_notification(
            dependent.id,
            dependent.title,
            NULL,
            dependent.state,
            dependent.assignee,
            dependent.updated_at,
            dependent.ticket_number,
            ticket_board.unblock_transition_message(
                dependent.id,
                dependent.title,
                dependent.state,
                ticket_board.ticket_state_already_announced(
                    dependent.id,
                    ticket_board.unblock_transition_target_role(dependent.state, dependent.assignee),
                    dependent.state
                )
            )
        );
    END LOOP;

    RETURN NULL;
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

CREATE OR REPLACE FUNCTION ticket_board.upsert_ticket_notification_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.updated_at, NEW.row_updated_at, clock_timestamp());
    reset_idle_reminder boolean := false;
    activation_reset boolean := false;
    comment_touch boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_board.ticket_notification_state (
            ticket_id,
            current_state,
            current_assignee,
            previous_state,
            entered_current_state_at,
            last_activity_at,
            last_transition_notified_at,
            last_implementer_assignee
        ) VALUES (
            NEW.id,
            NEW.state,
            NEW.assignee,
            NULL,
            activity_at,
            activity_at,
            activity_at,
            CASE
                WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
                ELSE ''
            END
        )
        ON CONFLICT (ticket_id) DO UPDATE
        SET current_state = EXCLUDED.current_state,
            current_assignee = EXCLUDED.current_assignee,
            previous_state = EXCLUDED.previous_state,
            entered_current_state_at = EXCLUDED.entered_current_state_at,
            last_activity_at = EXCLUDED.last_activity_at,
            last_transition_notified_at = EXCLUDED.last_transition_notified_at,
            last_nudged_at = NULL,
            nudge_count = 0,
            idle_reminder_count = 0,
            last_implementer_assignee = EXCLUDED.last_implementer_assignee;
        RETURN NULL;
    END IF;

    activation_reset := (
        (OLD.state = 'backlog' OR OLD.parked)
        AND NOT NEW.parked
        AND NEW.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
    );
    reset_idle_reminder := OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee OR activation_reset;
    comment_touch := current_setting('ticket_board.awaiting_role_comment_touch', true) = 'on';

    INSERT INTO ticket_board.ticket_notification_state (
        ticket_id,
        current_state,
        current_assignee,
        previous_state,
        entered_current_state_at,
        last_activity_at,
        last_transition_notified_at,
        last_nudged_at,
        nudge_count,
        last_implementer_assignee
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
        0,
        CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ''
        END
    )
    ON CONFLICT (ticket_id) DO UPDATE
    SET current_state = EXCLUDED.current_state,
        current_assignee = EXCLUDED.current_assignee,
        previous_state = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE ticket_board.ticket_notification_state.previous_state
        END,
        entered_current_state_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN EXCLUDED.entered_current_state_at
            ELSE ticket_board.ticket_notification_state.entered_current_state_at
        END,
        last_activity_at = EXCLUDED.last_activity_at,
        last_transition_notified_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_transition_notified_at
        END,
        last_nudged_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_nudged_at
        END,
        nudge_count = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN 0
            ELSE ticket_board.ticket_notification_state.nudge_count
        END,
        idle_reminder_count = CASE
            WHEN reset_idle_reminder THEN 0
            ELSE ticket_board.ticket_notification_state.idle_reminder_count
        END,
        awaiting_role = CASE
            WHEN reset_idle_reminder THEN ''
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN ''
            ELSE ticket_board.ticket_notification_state.awaiting_role
        END,
        awaiting_since_at = CASE
            WHEN reset_idle_reminder THEN NULL
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN NULL
            ELSE ticket_board.ticket_notification_state.awaiting_since_at
        END,
        last_implementer_assignee = CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ticket_board.ticket_notification_state.last_implementer_assignee
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
    -- Deliberately does NOT clear awaiting_role. Until PGU-909 this cleared it
    -- whenever the awaited role commented (`WHEN awaiting_role = NEW.who`), on
    -- the theory that the awaited role's comment IS the awaited deliverable --
    -- PGU-453 built it for perf posting a measurement.
    --
    -- That theory is right about half the comments and wrong about the other
    -- half, and the wrong half fails silently. A director replying "seen, still
    -- blocked" cleared the escalation while the blocking condition was entirely
    -- unchanged: the ticket vanished from the director's own queue and its nudges
    -- resumed, naming the implementer as stuck. An acknowledgement is not an
    -- action, and nothing here can tell the two apart.
    --
    -- So the awaited role now clears the flag explicitly, with
    -- clear_awaiting_role. The cost is a stale flag when someone forgets, and
    -- that is the better failure: it is visible, because the ticket stays in that
    -- role's queue, and it stops suppressing nudges by itself once
    -- ticket_awaiting_role_is_active's window expires. A dropped escalation is
    -- visible to nobody and expires never.
    --
    -- Note for anyone tracing this: add_comment touches ticket_board.tickets to
    -- refresh updated_at after inserting the comment. The tickets trigger uses
    -- ticket_board.awaiting_role_comment_touch to distinguish that bookkeeping
    -- touch from a real non-transition ticket edit by the awaited role.
    UPDATE ticket_board.ticket_notification_state
    SET last_activity_at = activity_at,
        nudge_count = 0
    WHERE ticket_id = NEW.ticket_id;
    RETURN NULL;
END;
$$;

-- SYRD-148: which unresolved blockers stand between a ticket and a handoff.
-- Returned as text so the refusal names them: "this is blocked" sends the
-- reader back to the board, "blocked by SYRD-147" sends them to the ticket
-- that has to move first.
CREATE OR REPLACE FUNCTION ticket_board.unresolved_blocker_list(p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT string_agg(b.blocker_ticket_id, ', ' ORDER BY b.position)
    FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = p_ticket_id AND NOT b.resolved;
$$;


CREATE OR REPLACE FUNCTION ticket_board.ticket_awaiting_role_is_active(
    p_awaiting_role text,
    p_awaiting_since_at timestamptz,
    p_now timestamptz,
    p_timeout interval DEFAULT interval '4 hours'
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(p_awaiting_role, '') IS NOT NULL
       AND p_awaiting_since_at IS NOT NULL
       AND p_awaiting_since_at > p_now - p_timeout;
$$;

-- Persist the entire bounded schedule in the existing queue. The marker survives
-- ACK/deletion, so repeated requests and migration retries cannot recreate it.
CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF t.state NOT IN ('in_progress', 'inspection', 'audit')
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    -- SYRD-148: a handoff says a named role can act now. While a blocker is
    -- unresolved that is false, and the whole schedule below -- four
    -- notifications ending in an escalation to the Director -- would be false
    -- with it. The write boundary refuses to create such a wait; this is the
    -- delivery side of the same rule, and it is what catches a wait that was
    -- legitimate when it was made and was overtaken by a blocker.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.enqueue_awaiting_role_handoff(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF normalized_role NOT IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- SYRD-148: an awaiting-role handoff is an actionable instruction to
    -- somebody else. A ticket whose own dependency has not resolved cannot
    -- give one: the named role would be told to act on work that cannot
    -- proceed, and after thirty minutes the Director would be told they had
    -- ignored it. Refused rather than accepted quietly, and the blockers are
    -- named so the caller knows what has to move first.
    IF ticket_board.ticket_has_unresolved_blockers(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents an awaiting-role handoff: %',
            ticket_board.unresolved_blocker_list(set_awaiting_role.id);
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND t.state IN ('in_progress', 'inspection', 'audit')
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND t.state IN ('in_progress', 'inspection', 'audit');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'clear_awaiting_role'
    );
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = clear_awaiting_role.id FOR UPDATE;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL,
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    WHERE ticket_id = clear_awaiting_role.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role_from_ticket_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    actor text := nullif(current_setting('ticket_board.caller_role', true), '');
BEGIN
    IF current_setting('ticket_board.awaiting_role_comment_touch', true) = 'on' THEN
        RETURN NULL;
    END IF;

    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL
    WHERE ticket_id = NEW.id
      AND awaiting_role <> ''
      AND (
          OLD.state IS DISTINCT FROM NEW.state
          OR OLD.assignee IS DISTINCT FROM NEW.assignee
          OR actor = awaiting_role
      );
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

CREATE OR REPLACE FUNCTION ticket_board.ticket_is_queued_for_reserved_implementer(
    p_ticket_id text,
    p_state text,
    p_assignee text,
    p_parked boolean,
    p_manually_controlled boolean
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT p_state = 'backlog'
       AND ticket_board.ticket_is_implementer_assignee(p_assignee)
       AND NOT coalesce(p_parked, false)
       AND NOT coalesce(p_manually_controlled, false)
       AND NOT ticket_board.ticket_has_unresolved_blockers(p_ticket_id)
       AND ticket_board.ticket_current_reserved_ticket(p_assignee, p_ticket_id) IS NOT NULL;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_serial_focus_reservation_is_current(
    p_ticket_id text,
    p_queued_for_assignee text,
    p_queued_behind_ticket text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    -- Whether the reservation a queued ticket names is the one still holding
    -- that implementer. The sibling above infers the hold live from a backlog
    -- ticket's own assignee; a declarative queue destination is frequently
    -- analysis/director, so there is no implementer on the row to infer from
    -- and the durable fields are the only record of what it is waiting for.
    --
    -- Every way of not being current answers false -- fields cleared, the named
    -- ticket finished, cancelled, never existed, or superseded by other work on
    -- the same implementer. That is the safe direction: a stale identity
    -- restores ordinary reminders rather than silencing a ticket forever
    -- (SYRD-109).
    -- Every operand is made non-null before it is compared. An unguarded
    -- `reserved_ticket = queued_behind` is NULL, not false, when the
    -- implementer holds nothing -- and `AND NOT NULL` is NULL, so the callers'
    -- WHERE clauses would drop exactly the tickets whose wait had just ended.
    -- The predicate that exists to stop a ticket being silenced forever would
    -- have been the thing silencing it.
    SELECT coalesce(btrim(p_queued_for_assignee), '') <> ''
       AND coalesce(btrim(p_queued_behind_ticket), '') <> ''
       AND coalesce(
               ticket_board.ticket_current_reserved_ticket(
                   btrim(p_queued_for_assignee),
                   p_ticket_id
               ),
               ''
           ) = btrim(p_queued_behind_ticket)
       -- A ticket already owned by the implementer it names is not waiting for
       -- them, whatever the fields say. `ticket_current_reserved_ticket`
       -- excludes the ticket it is asked about, so without this a marker left
       -- on a ticket that has since reached its implementer's own lane would
       -- match some other reservation and silence work somebody is doing.
       AND NOT EXISTS (
           SELECT 1
           FROM ticket_board.tickets held
           WHERE held.id = p_ticket_id
             AND btrim(lower(held.assignee)) = btrim(lower(p_queued_for_assignee))
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
       AND ticket_board.ticket_is_implementer_assignee(p_assignee)
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
        SET state = 'in_progress'
        WHERE id = NEW.id
          AND state = 'analysis';
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.activate_next_queued_ticket(p_implementer text)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_ticket_id text;
BEGIN
    IF NOT ticket_board.ticket_is_implementer_assignee(p_implementer) THEN
        RETURN NULL;
    END IF;

    IF ticket_board.ticket_current_reserved_ticket(p_implementer) IS NOT NULL THEN
        RETURN NULL;
    END IF;

    SELECT t.id
    INTO next_ticket_id
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.state = 'backlog'
      AND t.assignee = p_implementer
      AND NOT t.parked
      AND NOT t.manually_controlled
      AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
    ORDER BY ns.entered_current_state_at, t.ticket_number
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1;

    IF next_ticket_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE ticket_board.tickets
    SET state = 'in_progress',
        parked = false
    WHERE id = next_ticket_id
      AND state = 'backlog'
      AND assignee = p_implementer
      AND NOT parked;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    PERFORM ticket_board.touch_ticket(next_ticket_id);
    RETURN next_ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_implementer_and_activate_next()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    released_implementer text;
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    IF NOT (
        NEW.state IN ('done', 'cancelled')
        OR (NEW.state = 'backlog' AND NEW.parked AND OLD.state IS DISTINCT FROM 'backlog')
    ) THEN
        RETURN NULL;
    END IF;

    released_implementer := ticket_board.ticket_reserved_implementer(OLD.id, OLD.state, OLD.assignee);
    IF released_implementer IS NULL THEN
        SELECT nullif(ns.last_implementer_assignee, '')
        INTO released_implementer
        FROM ticket_board.ticket_notification_state ns
        WHERE ns.ticket_id = NEW.id;
    END IF;

    IF ticket_board.ticket_is_implementer_assignee(released_implementer) THEN
        PERFORM ticket_board.activate_next_queued_ticket(released_implementer);
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

CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN (
            SELECT CASE
                WHEN NULLIF(p_assignee, 'unassigned') = ANY(ws.owner_roles) THEN NULLIF(p_assignee, 'unassigned')
                WHEN cardinality(ws.owner_roles) = 1 THEN ws.owner_roles[1]
                ELSE NULL
            END
            FROM ticket_board.workflow_stages ws
            WHERE ws.name = 'audit'
        )
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN NULL
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.stage_default_assignee(p_state text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN cardinality(owner_roles) = 1 THEN owner_roles[1]
        ELSE NULL
    END
    FROM ticket_board.workflow_stages
    WHERE name = p_state;
$$;

CREATE OR REPLACE FUNCTION ticket_board.stage_entry_assignee(p_state text, p_current_assignee text, p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN cardinality(owner_roles) = 1 THEN owner_roles[1]
        WHEN p_state = 'audit'
             AND cardinality(owner_roles) > 1
             AND NULLIF(btrim(coalesce(p_current_assignee, '')), 'unassigned') = ANY(owner_roles)
            THEN NULLIF(btrim(coalesce(p_current_assignee, '')), 'unassigned')
        WHEN p_state = 'audit' AND cardinality(owner_roles) > 1 THEN
            owner_roles[
                ((coalesce((substring(p_ticket_id FROM '^[A-Z][A-Z0-9]*-([0-9]+)$'))::integer, 1) - 1)
                % cardinality(owner_roles)) + 1
            ]
        ELSE NULL
    END
    FROM ticket_board.workflow_stages
    WHERE name = p_state;
$$;

CREATE OR REPLACE FUNCTION ticket_board.apply_stage_default_assignee_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state
       AND current_setting('ticket_board.force_move', true) IS DISTINCT FROM 'on'
       AND NOT coalesce(OLD.manually_controlled, false)
       AND NOT coalesce(NEW.manually_controlled, false)
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.state_rank(p_state text)
RETURNS integer
LANGUAGE sql
STABLE
AS $$
    SELECT rank
    FROM ticket_board.workflow_stages
    WHERE name = p_state;
$$;

-- Has this role already been told, at some point, that the ticket entered this
-- state? Reads only `send` rows, which prune_notification_trace never deletes --
-- it removes claim/listener_claim/requeue/gate_defer only -- so this is durable
-- rather than a window into recent logs. Matches notification_trace_send_lookup_idx
-- (ticket_id, target_role, ticket_state_at_event) exactly, so it is an index probe
-- and not a scan of history.
CREATE OR REPLACE FUNCTION ticket_board.ticket_state_already_announced(
    p_ticket_id text,
    p_target_role text,
    p_state text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.notification_trace nt
        WHERE nt.ticket_id = p_ticket_id
          AND nt.target_role = p_target_role
          AND nt.ticket_state_at_event = p_state
          AND nt.kind = 'transition'
          AND nt.event = 'send'
    );
$$;

-- The 4-argument form is dropped rather than overloaded: a 5th argument with a
-- DEFAULT would make every existing 4-argument call ambiguous.
DROP FUNCTION IF EXISTS ticket_board.transition_message(text, text, text, text);

CREATE OR REPLACE FUNCTION ticket_board.transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_already_announced boolean
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
        WHEN p_new_state = 'in_progress' AND p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is active again'
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
        -- SYRD-159: the wake is identified by what it is about -- this ticket,
        -- this role -- not by the transaction that happened to raise it. Keyed
        -- on pg_current_xact_id(), one user-visible change delivered as two
        -- writes (set the blockers, then say why) minted two rows with two keys
        -- and woke the assignee twice for one thing to read. The queue already
        -- knows how to collapse: ON CONFLICT refreshes the pending row's message
        -- and re-arms it, and a delivered row is deleted by ack_notification, so
        -- a stable key means "at most one unread wake per ticket per role" and
        -- the next change after delivery still mints its own.
        --
        -- The role belongs in the key for a second reason: without it, two
        -- notifications raised in one transaction for two different roles
        -- collide, and ON CONFLICT ... SET target_role = EXCLUDED.target_role
        -- would silently retarget the first role's wake to the second.
        'ticket_update:' || p_ticket_id || ':' || target_role
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT ticket_board.transition_target_role(p_state, p_assignee);
$$;

-- The 3-argument form is dropped rather than overloaded, for the reason the
-- 4-argument transition_message was: a defaulted extra argument would make
-- every existing call ambiguous.
DROP FUNCTION IF EXISTS ticket_board.unblock_transition_message(text, text, text);

-- SYRD-124: nothing transitions when a blocker clears. The ticket is where it
-- was, with the owner it had; what changed is that it may now be worked. Told
-- for the first time, an owner needs to know it is theirs, so the ordinary
-- arrival wording is right. Told again -- and on the live board this reached a
-- role that was already working the ticket in that stage -- "entered" is a
-- claim the ticket moved, and it did not.
CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_message(
    p_ticket_id text,
    p_title text,
    p_new_state text,
    p_already_announced boolean
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is unblocked'
        WHEN p_new_state = 'backlog'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        ELSE ticket_board.transition_message(p_ticket_id, p_title, NULL, p_new_state, false)
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_unblock_notification(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_assignee text,
    p_updated_at timestamptz,
    p_ticket_number integer,
    p_message text
)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    target_role text;
    message text;
BEGIN
    target_role := ticket_board.unblock_transition_target_role(p_new_state, p_assignee);
    message := p_message;
    IF target_role IS NOT NULL AND message IS NOT NULL THEN
        PERFORM ticket_board.enqueue_notification(
            p_ticket_id,
            'transition',
            target_role,
            message,
            jsonb_build_object(
                'kind', 'transition',
                'id', p_ticket_id,
                'title', p_title,
                'old_state', p_old_state,
                'new_state', p_new_state,
                'assignee', p_assignee,
                'updated_at', p_updated_at,
                'ticket_number', p_ticket_number,
                'target_role', target_role,
                'message', message
            ),
            'transition:' || p_ticket_id || ':' || coalesce(p_old_state, 'insert') || ':' || p_new_state || ':' || pg_current_xact_id()::text
        );
    ELSE
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object('kind', 'wake', 'id', p_ticket_id, 'new_state', p_new_state)::text
        );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_notification(
    p_ticket_id text,
    p_kind text,
    p_target_role text,
    p_message text,
    p_payload jsonb,
    p_dedupe_key text,
    p_next_attempt_at timestamptz DEFAULT clock_timestamp()
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    notification_id bigint;
BEGIN
    IF p_target_role IS NULL OR p_message IS NULL OR btrim(p_message) = '' THEN
        RETURN NULL;
    END IF;

    INSERT INTO ticket_board.ticket_notification_queue (
        ticket_id,
        kind,
        target_role,
        message,
        payload,
        dedupe_key,
        next_attempt_at
    ) VALUES (
        p_ticket_id,
        p_kind,
        p_target_role,
        p_message,
        p_payload,
        p_dedupe_key,
        p_next_attempt_at
    )
    ON CONFLICT (dedupe_key) DO UPDATE
    SET payload = EXCLUDED.payload,
        target_role = EXCLUDED.target_role,
        message = EXCLUDED.message,
        -- A refreshed row is a different thing to deliver, so it is a new
        -- version, and the claim that is delivering the old one can no longer
        -- remove it (SYRD-159).
        revision = ticket_board.ticket_notification_queue.revision + 1,
        claimed_at = NULL,
        next_attempt_at = EXCLUDED.next_attempt_at,
        last_error = NULL,
        dead_lettered_at = NULL,
        terminal_reason = NULL,
        updated_at = clock_timestamp()
    RETURNING id INTO notification_id;

    PERFORM ticket_board.record_notification_trace(
        p_ticket_id,
        notification_id,
        p_target_role,
        p_kind,
        'enqueue',
        NULL,
        NULL,
        NULL,
        jsonb_build_object(
            'dedupe_key', p_dedupe_key,
            'next_attempt_at', p_next_attempt_at,
            'payload', p_payload
        )
    );

    PERFORM pg_notify(
        'ticket_board_state_transition',
        jsonb_build_object('kind', 'wake', 'notification_id', notification_id)::text
    );
    RETURN notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.claim_notification(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS TABLE(notification_id bigint, ticket_id text, target_role text, message text, payload jsonb, attempts integer)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('claim_notification');
    RETURN QUERY
    WITH candidate AS (
        SELECT q.id
        FROM ticket_board.ticket_notification_queue q
        WHERE q.next_attempt_at <= p_now
          AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
          AND q.dead_lettered_at IS NULL
        ORDER BY q.next_attempt_at, q.id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    ),
    claimed AS (
        UPDATE ticket_board.ticket_notification_queue q
        SET claimed_at = p_now,
            -- What this delivery is of. Set in the same statement that takes
            -- the claim, so nothing can slip between choosing the row and
            -- recording which version of it left (SYRD-159).
            claimed_revision = q.revision,
            attempts = q.attempts + 1,
            updated_at = p_now
        FROM candidate
        WHERE q.id = candidate.id
        RETURNING q.id, q.ticket_id, q.target_role, q.message, q.payload, q.attempts
    ),
    traced AS (
        SELECT ticket_board.record_notification_trace(
            claimed.ticket_id,
            claimed.id,
            claimed.target_role,
            claimed.payload ->> 'kind',
            'claim',
            NULL,
            NULL,
            NULL,
            jsonb_build_object('attempts', claimed.attempts, 'payload', claimed.payload)
        )
        FROM claimed
    )
    SELECT claimed.id, claimed.ticket_id, claimed.target_role, claimed.message, claimed.payload, claimed.attempts
    FROM claimed, traced;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.next_notification_attempt(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS timestamptz
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_attempt timestamptz;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('next_notification_attempt');
    SELECT min(q.next_attempt_at)
    INTO next_attempt
    FROM ticket_board.ticket_notification_queue q
    WHERE (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
      AND q.dead_lettered_at IS NULL;
    RETURN next_attempt;
END;
$$;

-- SYRD-37: the same question for any stage a role is serialised in, not only
-- the implementation stage. A bench of interchangeable workers submits into one
-- review lane, and a reviewer holding several tickets at once has no current
-- one: the notifications are all deliverable, all addressed to the same pane,
-- and nothing says which is next. Naming the earlier ticket makes the lane a
-- queue with a head, and the later notification is DEFERRED rather than dropped
-- -- the caller requeues it, which is what keeps review load from going silent.
CREATE OR REPLACE FUNCTION ticket_board.finish_current_stage_blocker(
    p_ticket_id text,
    p_target_role text,
    p_state text,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    blocker_ticket_id text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('finish_current_stage_blocker');

    WITH current_ticket AS (
        SELECT
            t.ticket_number,
            ns.entered_current_state_at
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.id = p_ticket_id
          AND t.state = p_state
          AND t.assignee = p_target_role
          AND NOT t.manually_controlled
    ),
    blocker_candidates AS (
        SELECT
            t.id,
            t.ticket_number,
            ns.entered_current_state_at
        FROM current_ticket c
        JOIN ticket_board.tickets t ON t.state = p_state
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.assignee = p_target_role
          AND t.id <> p_ticket_id
          AND NOT t.manually_controlled
          AND (
              (
                  ns.entered_current_state_at IS NOT NULL
                  AND c.entered_current_state_at IS NOT NULL
                  AND ns.entered_current_state_at < c.entered_current_state_at
              )
              OR (
                  (
                      ns.entered_current_state_at IS NULL
                      OR c.entered_current_state_at IS NULL
                      OR ns.entered_current_state_at = c.entered_current_state_at
                  )
                  AND t.ticket_number < c.ticket_number
              )
          )
    )
    SELECT b.id
    INTO blocker_ticket_id
    FROM blocker_candidates b
    WHERE NOT (
        EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
            ORDER BY q.next_attempt_at, q.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
    )
    ORDER BY
        CASE
            WHEN b.entered_current_state_at IS NOT NULL
                THEN b.entered_current_state_at
            ELSE NULL
        END NULLS LAST,
        b.ticket_number
    LIMIT 1;

    RETURN coalesce(blocker_ticket_id, '');
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.finish_current_blocker(
    p_ticket_id text,
    p_target_role text,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    -- The implementation stage, asked the way it has always been asked. Kept as
    -- its own entry point so an older listener and an older grant both keep
    -- working across the upgrade that generalised it (SYRD-37).
    SELECT ticket_board.finish_current_stage_blocker(
        p_ticket_id, p_target_role, 'in_progress', p_now, p_claim_timeout
    );
$$;


-- SYRD-135: the two halves of "clear this role's session once for this ticket".
-- They are deliberately separate. Claiming the pair before the clear is sent
-- would mark a clear that never happened, and a pane would then be handed a
-- ticket on top of the previous one's context with the board believing
-- otherwise; recording only after the send succeeded is what keeps a failed
-- clear retryable.
CREATE OR REPLACE FUNCTION ticket_board.role_session_clear_pending(p_ticket_id text, p_role text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('role_session_clear_pending');
    RETURN NOT EXISTS (
        SELECT FROM ticket_board.ticket_role_session_clears
        WHERE ticket_id = p_ticket_id AND role = p_role
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.record_role_session_clear(p_ticket_id text, p_role text)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE inserted integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('record_role_session_clear');
    INSERT INTO ticket_board.ticket_role_session_clears(ticket_id, role)
    VALUES (p_ticket_id, p_role)
    ON CONFLICT (ticket_id, role) DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    RETURN inserted > 0;
END;
$$;

-- SYRD-159: one place that decides whether a removal is still removing what was
-- delivered. A collapsing dedupe key means a pending row can be refreshed while
-- a listener is delivering it -- that is what the collapse is for -- so removing
-- it by id alone would delete a payload nobody has been told about. Both removal
-- paths ask this, so the rule cannot drift between them, and both stop rather
-- than re-arming: the enqueue that superseded the row already cleared its claim
-- and made it due, and clearing it a second time could steal a claim a later
-- pass has legitimately taken, which is how one update becomes two deliveries.
--
-- True means "stop": the row has moved on, or it is already gone.
CREATE OR REPLACE FUNCTION ticket_board.notification_delivery_superseded(
    p_notification_id bigint,
    p_event text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    current_revision bigint;
    delivered_revision bigint;
BEGIN
    SELECT q.revision, q.claimed_revision INTO current_revision, delivered_revision
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN true;
    END IF;
    IF delivered_revision IS NULL OR delivered_revision = current_revision THEN
        RETURN false;
    END IF;
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id, q.id, q.target_role, q.kind, p_event,
        NULL, NULL, NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload,
                           'delivered_revision', delivered_revision,
                           'current_revision', current_revision)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ack_notification(p_notification_id bigint)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ack_notification');
    IF ticket_board.notification_delivery_superseded(p_notification_id, 'ack_superseded') THEN
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET idle_reminder_count = CASE
            WHEN q.kind = 'idle_reminder' THEN ns.idle_reminder_count + 1
            ELSE ns.idle_reminder_count
        END,
        last_idle_reminder_at = CASE
            WHEN q.kind = 'idle_reminder' THEN clock_timestamp()
            ELSE ns.last_idle_reminder_at
        END,
        last_transition_notified_at = CASE
            WHEN q.kind = 'transition' THEN clock_timestamp()
            ELSE ns.last_transition_notified_at
        END
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
      AND ns.ticket_id = q.ticket_id;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'ack',
        NULL,
        NULL,
        NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.discard_notification(
    p_notification_id bigint,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('discard_notification');
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'discard_notification requires a non-empty reason';
    END IF;
    IF ticket_board.notification_delivery_superseded(p_notification_id, 'discard_superseded') THEN
        RETURN;
    END IF;

    -- Removes a queued notification that was never delivered, WITHOUT the
    -- delivery accounting in ack_notification. That function bumps
    -- ticket_notification_state.idle_reminder_count for an idle_reminder, and
    -- notify_idle_turn_end_nudges reads idle_reminder_count >= 1 on a
    -- non-director owner as "already reminded", switching the next wave to an
    -- escalation addressed to the director. Acking a reminder that was
    -- suppressed because its owner is working would therefore tell the director
    -- the owner was reminded and ignored it, when nothing was ever delivered
    -- (SYRD-32).
    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'discard',
        NULL,
        left(p_reason, 500),
        NULL,
        jsonb_build_object('attempts', q.attempts, 'payload', q.payload, 'reason', p_reason)
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.dead_letter_notification(
    p_notification_id bigint,
    p_reason text,
    p_detail jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('dead_letter_notification');
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'dead_letter_notification requires a non-empty reason';
    END IF;

    UPDATE ticket_board.ticket_notification_queue
    SET claimed_at = NULL,
        dead_lettered_at = now_at,
        terminal_reason = left(p_reason, 500),
        last_error = left(p_reason, 500),
        updated_at = now_at
    WHERE id = p_notification_id;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'dead_letter',
        NULL,
        p_reason,
        NULL,
        jsonb_build_object(
            'reason', p_reason,
            'attempts', q.attempts,
            'payload', q.payload,
            'detail', coalesce(p_detail, '{}'::jsonb)
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.dismiss_notification(
    p_notification_id bigint,
    p_reason text DEFAULT ''
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
    actor := ticket_board.require_actor(ARRAY['director'], 'dismiss_notification');

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'director_dismiss',
        NULL,
        coalesce(nullif(btrim(p_reason), ''), 'director dismissed notification'),
        NULL,
        jsonb_build_object(
            'actor', nullif(current_setting('ticket_board.caller_role', true), ''),
            'reason', p_reason,
            'attempts', q.attempts,
            'last_error', q.last_error,
            'terminal_reason', q.terminal_reason,
            'dead_lettered_at', q.dead_lettered_at,
            'payload', q.payload
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'notification % not found', p_notification_id;
    END IF;

    DELETE FROM ticket_board.ticket_notification_queue
    WHERE id = p_notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.dismiss_notification_by_key(
    p_ticket_id text,
    p_target_role text,
    p_kind text DEFAULT 'transition',
    p_reason text DEFAULT ''
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    notification_id bigint;
    normalized_ticket_id text := upper(btrim(coalesce(p_ticket_id, '')));
    normalized_target_role text := lower(btrim(coalesce(p_target_role, '')));
    normalized_kind text := lower(coalesce(nullif(btrim(p_kind), ''), 'transition'));
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'dismiss_notification');
    IF normalized_ticket_id = '' THEN
        RAISE EXCEPTION 'dismiss_notification_by_key requires a non-empty ticket_id';
    END IF;
    IF normalized_target_role = '' THEN
        RAISE EXCEPTION 'dismiss_notification_by_key requires a non-empty target_role';
    END IF;

    SELECT q.id
    INTO notification_id
    FROM ticket_board.ticket_notification_queue q
    WHERE q.ticket_id = normalized_ticket_id
      AND q.target_role = normalized_target_role
      AND q.kind = normalized_kind
    ORDER BY q.dead_lettered_at NULLS LAST, q.created_at, q.id
    LIMIT 1
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'notification for ticket %, target %, kind % not found',
            normalized_ticket_id,
            normalized_target_role,
            normalized_kind;
    END IF;

    PERFORM ticket_board.dismiss_notification(notification_id, p_reason);
    RETURN notification_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.requeue_notification(
    p_notification_id bigint,
    p_delay interval DEFAULT interval '30 seconds',
    p_error text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    now_at timestamptz := clock_timestamp();
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('requeue_notification');
    IF coalesce(p_error, '') <> 'pane busy' THEN
        PERFORM ticket_board.record_notification_trace(
            q.ticket_id,
            q.id,
            q.target_role,
            q.kind,
            'requeue',
            NULL,
            p_error,
            NULL,
            jsonb_build_object('delay', p_delay, 'attempts', q.attempts, 'payload', q.payload)
        )
        FROM ticket_board.ticket_notification_queue q
        WHERE q.id = p_notification_id;
    END IF;

    PERFORM ticket_board.record_notification_trace(
        q.ticket_id,
        q.id,
        q.target_role,
        q.kind,
        'retry_loud',
        NULL,
        p_error,
        NULL,
        jsonb_build_object(
            'attempts', q.attempts,
            'age_seconds', floor(extract(epoch FROM now_at - q.created_at))::integer,
            'payload', q.payload
        )
    )
    FROM ticket_board.ticket_notification_queue q
    WHERE q.id = p_notification_id
      AND (q.attempts >= 12 OR q.created_at <= now_at - interval '1 hour')
      AND NOT EXISTS (
          SELECT 1
          FROM ticket_board.notification_trace nt
          WHERE nt.notification_id = q.id
            AND nt.event = 'retry_loud'
      );

    UPDATE ticket_board.ticket_notification_queue
    SET claimed_at = NULL,
        next_attempt_at = now_at + p_delay,
        last_error = p_error,
        updated_at = now_at
    WHERE id = p_notification_id
      AND dead_lettered_at IS NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_notification_backoff_for_idle_roles(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    reset_count integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('reset_notification_backoff_for_idle_roles');

    WITH idle_roles AS (
        SELECT lower(btrim(key)) AS role
        FROM jsonb_each_text(coalesce(p_idle_since_by_role, '{}'::jsonb))
        WHERE btrim(key) <> ''
    ),
    reset_rows AS (
        UPDATE ticket_board.ticket_notification_queue q
        SET claimed_at = NULL,
            next_attempt_at = p_now,
            last_error = NULL,
            updated_at = p_now
        FROM idle_roles i
        WHERE q.target_role = i.role
          AND q.next_attempt_at > p_now
          -- Must match PANE_BUSY_REQUEUE_ERROR in notify_listener.py.
          AND q.last_error = 'pane busy'
          AND q.dead_lettered_at IS NULL
        RETURNING q.id
    )
    SELECT count(*)::integer
    INTO reset_count
    FROM reset_rows;

    RETURN coalesce(reset_count, 0);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_finish_current_notifications_for_released_ticket()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    reset_at timestamptz := clock_timestamp();
    reset_count integer;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RETURN NULL;
    END IF;
    IF OLD.state <> 'in_progress'
       OR NOT ticket_board.ticket_is_implementer_assignee(OLD.assignee)
       OR coalesce(OLD.manually_controlled, false) THEN
        RETURN NULL;
    END IF;
    IF NEW.state = 'in_progress'
       AND NEW.assignee IS NOT DISTINCT FROM OLD.assignee
       AND NOT coalesce(NEW.manually_controlled, false) THEN
        RETURN NULL;
    END IF;

    UPDATE ticket_board.ticket_notification_queue q
    SET claimed_at = NULL,
        next_attempt_at = reset_at,
        last_error = NULL,
        updated_at = reset_at
    WHERE q.target_role = OLD.assignee
      AND q.next_attempt_at > reset_at
      -- Must match FINISH_CURRENT_REQUEUE_ERROR in notify_listener.py.
      AND q.last_error = 'finish current';
    GET DIAGNOSTICS reset_count = ROW_COUNT;

    IF reset_count > 0 THEN
        PERFORM pg_notify(
            'ticket_board_state_transition',
            jsonb_build_object(
                'kind', 'wake',
                'reason', 'finish_current_released',
                'ticket_id', OLD.id,
                'target_role', OLD.assignee,
                'reset_count', reset_count
            )::text
        );
    END IF;

    RETURN NULL;
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
    WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
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
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'dat' THEN 'director'
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
        WHEN p_state = 'in_progress' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is still in progress'
        WHEN p_state = 'inspection' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for inspection'
        WHEN p_state = 'audit' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for audit'
        WHEN p_state = 'dat' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for DAT'
        WHEN p_state = 'director_review' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for your review'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notification_delivery_in_backoff(
    p_ticket_id text,
    p_target_role text,
    p_now timestamptz,
    p_recent interval DEFAULT interval '5 minutes'
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH latest_delivery_event AS (
        SELECT nt.event, nt.ts
        FROM ticket_board.notification_trace nt
        WHERE nt.ticket_id = p_ticket_id
          AND nt.target_role = p_target_role
          AND nt.event IN (
              'gate_defer',
              'send_failed',
              'send',
              'listener_ack',
              'drop',
              'max_defer_force_deliver',
              'ack'
          )
        ORDER BY nt.ts DESC, nt.id DESC
        LIMIT 1
    )
    SELECT EXISTS (
        SELECT 1
        FROM latest_delivery_event lde
        WHERE lde.event IN ('gate_defer', 'send_failed')
          AND lde.ts >= p_now - p_recent
    )
    OR EXISTS (
        SELECT 1
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = p_ticket_id
          AND q.target_role = p_target_role
          AND q.claimed_at IS NULL
          AND q.next_attempt_at > p_now
          AND btrim(coalesce(q.last_error, '')) <> ''
    )
    OR EXISTS (
        SELECT 1
        FROM ticket_board.ticket_notification_state ns
        WHERE ns.ticket_id = p_ticket_id
          AND ticket_board.ticket_awaiting_role_is_active(
              ns.awaiting_role,
              ns.awaiting_since_at,
              p_now
          )
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is waiting in your '
        || p_state
        || ' queue. Advance it or hand it off. If you cannot move it forward, tell the director what is wrong.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_director_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is waiting in your '
        || p_state
        || ' queue. Advance it or hand it off. If you cannot move it forward, tell User what is wrong.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_problem_message(
    p_ticket_id text,
    p_state text,
    p_contact text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is still in '
        || p_state
        || ' and you haven''t advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell '
        || p_contact
        || ' directly what is wrong. Do NOT do nothing.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_escalation_message(
    p_owner_role text,
    p_ticket_id text,
    p_state text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_owner_role
        || ' was reminded about '
        || p_ticket_id
        || ' (in '
        || p_state
        || ') and still hasn''t advanced it -- may be stuck.';
$$;

-- The 2-argument form is dropped rather than overloaded: a defaulted extra
-- argument would make every existing call ambiguous, and the listener is
-- updated in the same change (SYRD-163).
DROP FUNCTION IF EXISTS ticket_board.notify_idle_turn_end_nudges(jsonb, timestamptz);

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_turn_end_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz,
    p_active_grace interval,
    p_work_observed_at jsonb
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_turn_end_nudges');

    IF p_idle_since_by_role IS NULL OR p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.target_role)
            candidates.id,
            candidates.state,
            candidates.assignee,
            candidates.kind,
            candidates.owner_role,
            candidates.target_role,
            candidates.idle_reminder_count,
            candidates.idle_since_at
        FROM (
            SELECT
                notification_scope.id,
                notification_scope.state,
                notification_scope.assignee,
                notification_scope.ticket_number,
                notification_scope.entered_current_state_at,
                notification_scope.idle_reminder_count,
                notification_scope.owner_role,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'escalation'
                    ELSE 'idle_reminder'
                END AS kind,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'director'
                    ELSE notification_scope.owner_role
                END AS target_role,
                notification_scope.idle_since_at,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN -1
                    WHEN notification_scope.state = 'analysis' THEN 0
                    WHEN notification_scope.state = 'inspection' THEN 2
                    WHEN notification_scope.state = 'in_progress' THEN 3
                    WHEN notification_scope.state = 'audit' THEN 4
                    WHEN notification_scope.state = 'dat' THEN 5
                    WHEN notification_scope.state = 'director_review' THEN 5
                    ELSE 9
                END AS priority,
                row_number() OVER (
                    PARTITION BY notification_scope.active_work_partition
                    ORDER BY sent.last_sent_at DESC NULLS LAST,
                        notification_scope.ticket_number DESC
                ) AS in_progress_rank
            FROM (
                SELECT
                    t.id,
                    t.state,
                    t.assignee,
                    t.ticket_number,
                    ns.entered_current_state_at,
                    ns.idle_reminder_count,
                    ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
                    CASE
                        WHEN t.state = 'in_progress'
                            THEN t.state || ':' || ticket_board.transition_target_role(t.state, t.assignee)
                        ELSE NULL
                    END AS active_work_partition,
                    (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at
                FROM ticket_board.tickets t
                JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
                WHERE t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review')
                  AND NOT t.manually_controlled
                  AND ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
                  AND p_idle_since_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
                  AND greatest(
                        ns.entered_current_state_at,
                        (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                      ) <= p_now
                  -- await-role means the owner has explicitly handed this off.
                  -- Do not tell that same owner they may be stuck while the wait is active.
                  AND NOT ticket_board.ticket_awaiting_role_is_active(
                      ns.awaiting_role,
                      ns.awaiting_since_at,
                      p_now
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_blockers tb
                      LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                      WHERE tb.ticket_id = t.id
                        AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_comments c
                      WHERE c.ticket_id = t.id
                        AND c.who = ticket_board.transition_target_role(t.state, t.assignee)
                        AND c.ts IS NOT NULL
                        AND c.ts >= ns.entered_current_state_at
                        AND c.ts >= ns.last_activity_at
                        AND c.ts >= (
                            (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                            - interval '5 minutes'
                        )
                        AND c.ts <= p_now
                  )
            ) AS notification_scope
            LEFT JOIN LATERAL (
                SELECT max(trace.ts) AS last_sent_at
                FROM ticket_board.notification_trace trace
                WHERE trace.ticket_id = notification_scope.id
                  AND trace.target_role = notification_scope.owner_role
                  AND trace.kind = 'transition'
                  AND trace.event = 'send'
                  AND trace.ticket_state_at_event = notification_scope.state
            ) sent ON true
            -- A turn ending is not a stall. A review takes several turns, and
            -- between two of them the pane is idle by every measure this
            -- generator had: the hook says idle and the live gate agrees,
            -- because at that instant nothing is running. Audit was told it
            -- had not advanced SYRD-162 seconds after being handed it, and the
            -- next turn boundary escalated that to the Director as "may be
            -- stuck" -- inside a minute, while the review was happening
            -- (SYRD-163).
            --
            -- So the clock a reminder is measured against starts at the LAST
            -- OBSERVED WORK as well as at the stage and the idle hook, and the
            -- role has to have been quiet for the grace period before any of
            -- this is due. Same rule for every role: the stall generator
            -- beside this one has taken both inputs since SYRD-58, and the
            -- asymmetry is what let this through.
            WHERE greatest(
                    notification_scope.entered_current_state_at,
                    notification_scope.idle_since_at,
                    coalesce(
                        (p_work_observed_at ->> notification_scope.owner_role)::timestamptz,
                        '-infinity'::timestamptz
                    )
                  ) + p_active_grace <= p_now
              AND notification_scope.owner_role IS NOT NULL
        ) AS candidates
        WHERE (candidates.state <> 'in_progress' OR candidates.in_progress_rank = 1)
          AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidates.id
              AND q.target_role = candidates.target_role
              AND q.kind NOT IN ('idle_reminder', 'escalation')
        )
          -- "was reminded about X and still hasn't advanced it" has to be TRUE
          -- when it is said. The counter alone could not make it true: a
          -- reminder that was enqueued and never delivered still incremented
          -- it, and two turn boundaries a few seconds apart still reached it.
          -- So an escalation waits for a reminder that was actually SENT to
          -- that owner, in this stage, and for the grace period to pass
          -- afterwards -- which is the time the reminder was asking for
          -- (SYRD-163).
          AND (
            candidates.kind <> 'escalation'
            OR EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state reminded
                WHERE reminded.ticket_id = candidates.id
                  AND reminded.last_idle_reminder_at IS NOT NULL
                  AND reminded.last_idle_reminder_at + p_active_grace <= p_now
            )
        )
        ORDER BY candidates.target_role, candidates.priority, candidates.ticket_number
    LOOP
        payload := jsonb_build_object(
            'kind', candidate.kind,
            'id', candidate.id,
            'state', candidate.state,
            'assignee', candidate.assignee,
            'owner_role', candidate.owner_role,
            'target_role', candidate.target_role,
            'message', CASE
                WHEN candidate.kind = 'escalation' THEN ticket_board.idle_without_advancing_escalation_message(
                    candidate.owner_role,
                    candidate.id,
                    candidate.state
                )
                WHEN candidate.owner_role = 'director' AND candidate.idle_reminder_count >= 1 THEN ticket_board.idle_without_advancing_problem_message(
                    candidate.id,
                    candidate.state,
                    'User'
                )
                WHEN candidate.owner_role = 'director' THEN ticket_board.idle_without_advancing_director_message(
                    candidate.id,
                    candidate.state
                )
                ELSE ticket_board.idle_without_advancing_message(candidate.id, candidate.state)
            END,
            'idle_since', candidate.idle_since_at
        );

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            candidate.kind,
            candidate.target_role,
            payload ->> 'message',
            payload,
            CASE
                WHEN candidate.kind = 'escalation' THEN
                    'idle-reminder-escalation:' || candidate.id || ':' || candidate.owner_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
                ELSE
                    'idle-reminder:' || candidate.id || ':' || candidate.target_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
            END
        );
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_due_nudges(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_cadence interval DEFAULT interval '30 minutes',
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
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    WHEN 'director_review' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE t.state IN ('inspection', 'audit', 'dat', 'director_review')
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
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
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
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
            RAISE WARNING 'wedged-pane nudge backstop fired for % targeting % (%s since transition)',
                candidate.id,
                target_role,
                floor(extract(epoch FROM p_now - candidate.entered_current_state_at))::integer || 's';
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
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

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_stall_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_grace interval DEFAULT interval '45 seconds',
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 2,
    p_work_observed_at_by_role jsonb DEFAULT '{}'::jsonb
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    target_role text;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_stall_nudges');

    p_idle_since_by_role := coalesce(p_idle_since_by_role, '{}'::jsonb);
    p_work_observed_at_by_role := coalesce(p_work_observed_at_by_role, '{}'::jsonb);

    IF p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.idle_since_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.work_observed_at,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
                        THEN (p_work_observed_at_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE t.state IN ('in_progress', 'inspection', 'audit', 'dat')
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND p_idle_since_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
              AND greatest(
                    ns.entered_current_state_at,
                    (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                  ) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> 'director')::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? 'director'
                        THEN (p_work_observed_at_by_role ->> 'director')::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
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
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND p_idle_since_by_role ? 'director'
              AND greatest(ns.entered_current_state_at, (p_idle_since_by_role ->> 'director')::timestamptz) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at
           AND coalesce(candidate.work_observed_at, '-infinity'::timestamptz) <= candidate.last_nudged_at THEN
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

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN greatest(
                    candidate.last_activity_at,
                    coalesce(candidate.work_observed_at, '-infinity'::timestamptz)
                ) > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
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

CREATE OR REPLACE FUNCTION ticket_board.install_notification_trace_prune_cron()
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron') THEN
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_cron;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_cron extension for ticket_board_notification_trace_prune not installed: %', SQLERRM;
            RETURN;
        END;
        BEGIN
            PERFORM cron.unschedule('ticket_board_notification_trace_prune');
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        BEGIN
            PERFORM cron.schedule(
                'ticket_board_notification_trace_prune',
                '17 * * * *',
                $cron$SELECT ticket_board.prune_notification_trace();$cron$
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pg_cron schedule for ticket_board_notification_trace_prune not installed: %', SQLERRM;
        END;
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS tickets_enforce_workflow_insert ON ticket_board.tickets;
CREATE TRIGGER tickets_enforce_workflow_insert
BEFORE INSERT ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.enforce_ticket_workflow_insert();

DROP TRIGGER IF EXISTS tickets_enforce_workflow_update ON ticket_board.tickets;
CREATE TRIGGER tickets_enforce_workflow_update
BEFORE UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.enforce_ticket_workflow_update();

DROP TRIGGER IF EXISTS tickets_zzzz_stage_default_assignee_update ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzz_stage_default_assignee_update
BEFORE UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.apply_stage_default_assignee_update();

DROP TRIGGER IF EXISTS tickets_notify_state_transition ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzz_notify_insert_transition ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzz_notify_transition ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzy_resolve_completed_blockers ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzz_notify_unblocked_dependents ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzzx_reset_finish_current_notifications ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzzz_release_implementer ON ticket_board.tickets;
DROP TRIGGER IF EXISTS ticket_blockers_sync_resolved ON ticket_board.ticket_blockers;


CREATE OR REPLACE FUNCTION ticket_board.announce_serial_focus_queue()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    queued jsonb;
    queued_for text;
    reserved_by text;
    actor text;
    notice text;
BEGIN
    queued := nullif(current_setting('ticket_board.serial_focus_queued', true), '')::jsonb;
    IF queued IS NULL OR queued->>'queued_for' IS NULL THEN
        RETURN NEW;
    END IF;
    -- Clear before writing. append_ticket_comment touches the ticket, which
    -- re-enters this trigger; the cleared marker makes that pass a no-op.
    PERFORM set_config('ticket_board.serial_focus_queued', '', true);
    queued_for := queued->>'queued_for';
    reserved_by := coalesce(nullif(queued->>'reserved_by', ''), 'active work');
    actor := coalesce(nullif(ticket_board.current_app_actor(), ''), 'director');

    notice := NEW.id || ' is queued for ' || queued_for || ': serial focus is held by '
        || reserved_by || '. It is waiting in ' || (queued->>'stage') || '/' || (queued->>'assignee')
        || '. Route it again once ' || reserved_by || ' leaves that implementer, or route it to a free implementer now.';

    PERFORM ticket_board.append_ticket_comment(NEW.id, actor, notice);
    PERFORM ticket_board.enqueue_notification(
        NEW.id,
        'ticket_update',
        'director',
        notice,
        jsonb_build_object(
            'kind', 'ticket_update',
            'id', NEW.id,
            'state', NEW.state,
            'assignee', NEW.assignee,
            'target_role', 'director',
            'queued_for', queued_for,
            'reserved_by', reserved_by,
            'message', notice
        ),
        'serial-focus-queued:' || NEW.id || ':' || queued_for || ':' || reserved_by
    );
    RETURN NEW;
END;
$$;

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

DROP TRIGGER IF EXISTS tickets_zzzzzz_serial_focus_queue ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_serial_focus_queue
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.announce_serial_focus_queue();

CREATE TRIGGER tickets_zzz_notify_transition
AFTER INSERT OR UPDATE ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.notify_ticket_state_transition();

CREATE TRIGGER tickets_zzzy_resolve_completed_blockers
AFTER UPDATE OF state ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.resolve_completed_blockers();

CREATE TRIGGER tickets_zzzz_notify_unblocked_dependents
AFTER UPDATE OF state ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.notify_unblocked_dependents();

CREATE TRIGGER tickets_zzzzx_reset_finish_current_notifications
AFTER UPDATE OF state, assignee, manually_controlled ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.reset_finish_current_notifications_for_released_ticket();

CREATE TRIGGER tickets_zzzzz_release_implementer
AFTER UPDATE OF state, parked ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.release_implementer_and_activate_next();

CREATE TRIGGER ticket_blockers_sync_resolved
BEFORE INSERT OR UPDATE OF blocker_ticket_id ON ticket_board.ticket_blockers
FOR EACH ROW
EXECUTE FUNCTION ticket_board.sync_blocker_resolved_from_state();

DROP TRIGGER IF EXISTS ticket_comments_notification_activity ON ticket_board.ticket_comments;
CREATE TRIGGER ticket_comments_notification_activity
AFTER INSERT ON ticket_board.ticket_comments
FOR EACH ROW
EXECUTE FUNCTION ticket_board.touch_ticket_notification_activity();

DROP TRIGGER IF EXISTS tickets_clear_awaiting_role_activity ON ticket_board.tickets;
CREATE TRIGGER tickets_clear_awaiting_role_activity
AFTER UPDATE OF state, assignee, updated_at ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.clear_awaiting_role_from_ticket_activity();

INSERT INTO ticket_board.ticket_notification_state (
    ticket_id,
    current_state,
    current_assignee,
    previous_state,
    entered_current_state_at,
    last_activity_at,
    last_transition_notified_at,
    last_implementer_assignee
)
SELECT
    id,
    state,
    assignee,
    NULL,
    coalesce(updated_at, row_updated_at, clock_timestamp()),
    coalesce(updated_at, row_updated_at, clock_timestamp()),
    coalesce(updated_at, row_updated_at, clock_timestamp()),
    CASE
        WHEN state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(assignee) THEN assignee
        ELSE ''
    END
FROM ticket_board.tickets
ON CONFLICT (ticket_id) DO NOTHING;

SELECT ticket_board.install_nudge_cron();
SELECT ticket_board.install_notification_trace_prune_cron();
SELECT ticket_board.prune_notification_trace();

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

--
-- The capabilities that make a role the one controlling a project. One
-- definition, so the declarative authority layer and everything else that asks
-- "who controls this project" agree by construction rather than by both
-- spelling 'director' (SYRD-49).
CREATE OR REPLACE FUNCTION ticket_board.control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['set_manually_controlled', 'merge']::text[];
$$;

-- The narrated overrides that belong to that role and to nobody else. The API
-- advertises both names for the one database entry point; both are named here
-- so the two layers cannot drift apart (SYRD-78).
CREATE OR REPLACE FUNCTION ticket_board.control_override_actions()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['force_move', 'override_move']::text[];
$$;

-- What a declared document may never take away from the director. A tenant
-- configures its own pipeline; it does not get to leave the project without a
-- controller. `control_capabilities()` above is the subset that identifies
-- which role that is, and is contained in this one by construction -- a
-- discriminator naming something the floor does not guarantee would stop
-- finding the director the moment a tenant dropped it (SYRD-82).
CREATE OR REPLACE FUNCTION ticket_board.director_control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY[
        'reassign', 'set_manually_controlled', 'set_blockers',
        'merge', 'edit_fields', 'dismiss_notification', 'director_edit',
        'resolve_publication',
        -- The control role has to be able to unstick work its owner left
        -- behind. Without it the documented recovery is a control the document
        -- does not grant, which is what SYRD-131 ran into (SYRD-133).
        'recover_stalled_ticket'
    ]::text[];
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
    caller_role text;
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_service is the only database writer', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    caller_role := nullif(current_setting('ticket_board.caller_role', true), '');
    IF caller_role IS NOT NULL
       AND cardinality(coalesce(p_allowed_roles, ARRAY[]::text[])) > 0
       AND caller_role <> ALL(p_allowed_roles)
       AND NOT (
           p_allowed_roles && ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]
           AND ticket_board.ticket_is_implementer_assignee(caller_role)
       ) THEN
        RAISE EXCEPTION 'role % cannot call %', caller_role, p_action
            USING ERRCODE = '42501';
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_allowed_config(
    p_action_name text,
    p_actor text,
    p_ticket_assignee text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_transitions wt
        WHERE wt.action_name = p_action_name
          AND p_actor = ANY(wt.allowed_roles)
          AND (
              NOT wt.owner_scoped
              OR p_actor = p_ticket_assignee
              OR (wt.director_override AND p_actor = 'director')
          )
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_owner_scoped_config(
    p_action_name text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(bool_or(wt.owner_scoped), false)
    FROM ticket_board.workflow_transitions wt
    WHERE wt.action_name = p_action_name;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_transition_rbac_director_override_config(
    p_action_name text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(bool_or(wt.director_override), false)
    FROM ticket_board.workflow_transitions wt
    WHERE wt.action_name = p_action_name;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_workflow_transition_actor(
    p_action_name text,
    p_ticket_id text DEFAULT NULL,
    p_ticket_assignee text DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    resolved_assignee text := p_ticket_assignee;
BEGIN
    actor := ticket_board.current_app_actor();
    IF actor IS NULL THEN
        RAISE EXCEPTION 'missing ticket_board.caller_role for %', p_action_name
            USING ERRCODE = '42501';
    END IF;

    IF resolved_assignee IS NULL AND p_ticket_id IS NOT NULL THEN
        SELECT t.assignee
        INTO resolved_assignee
        FROM ticket_board.tickets t
        WHERE t.id = p_ticket_id;
    END IF;

    IF NOT ticket_board.workflow_transition_rbac_allowed_config(
        p_action_name,
        actor,
        resolved_assignee
    ) THEN
        RAISE EXCEPTION 'role % cannot call %', actor, p_action_name
            USING ERRCODE = '42501';
    END IF;

    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.log_workflow_transition_rbac_shadow_mismatch(
    p_ticket_id text,
    p_action_name text,
    p_actor text,
    p_ticket_assignee text,
    p_hardcoded_allowed boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    resolved_assignee text := p_ticket_assignee;
    config_allowed boolean;
    config_owner_scoped boolean;
BEGIN
    IF resolved_assignee IS NULL AND p_ticket_id IS NOT NULL THEN
        SELECT t.assignee
        INTO resolved_assignee
        FROM ticket_board.tickets t
        WHERE t.id = p_ticket_id;
    END IF;

    config_allowed := ticket_board.workflow_transition_rbac_allowed_config(
        p_action_name,
        p_actor,
        resolved_assignee
    );
    config_owner_scoped := ticket_board.workflow_transition_rbac_owner_scoped_config(p_action_name);

    IF p_hardcoded_allowed IS DISTINCT FROM config_allowed THEN
        INSERT INTO ticket_board.workflow_transition_rbac_shadow_log (
            ticket_id,
            action_name,
            actor,
            ticket_assignee,
            hardcoded_allowed,
            config_allowed,
            owner_scoped
        ) VALUES (
            p_ticket_id,
            p_action_name,
            p_actor,
            resolved_assignee,
            p_hardcoded_allowed,
            config_allowed,
            config_owner_scoped
        );
        RAISE WARNING 'workflow transition RBAC shadow mismatch for % action=% actor=% assignee=% hardcoded=% config=% owner_scoped=%',
            p_ticket_id,
            p_action_name,
            p_actor,
            resolved_assignee,
            p_hardcoded_allowed,
            config_allowed,
            config_owner_scoped;
    END IF;
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
    IF NOT EXISTS (
        SELECT 1
        FROM (
            SELECT unnest(wt.allowed_roles) AS role
            FROM ticket_board.workflow_transitions wt
            UNION
            SELECT unnest(ws.owner_roles) AS role
            FROM ticket_board.workflow_stages ws
        ) AS valid_roles
        WHERE valid_roles.role = actor
    ) THEN
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

CREATE OR REPLACE FUNCTION ticket_board.ticket_id_pattern()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT '^[A-Z][A-Z0-9]*-[0-9]+$';
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_prefix()
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    raw_prefix text := upper(regexp_replace(coalesce(nullif(current_setting('ticket_board.ticket_prefix', true), ''), 'PGU'), '[^A-Z0-9]', '', 'g'));
BEGIN
    IF raw_prefix = '' THEN
        raw_prefix := 'PGU';
    END IF;
    IF raw_prefix ~ '^[0-9]' THEN
        raw_prefix := 'T' || raw_prefix;
    END IF;
    RETURN raw_prefix;
END;
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
    RETURN ticket_board.ticket_prefix() || '-' || next_number::text;
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
        'origin_project', ticket_row.origin_project,
        'external_source_ref', ticket_row.external_source_ref,
        'blocked_reason', ticket_row.blocked_reason,
        'queued_for_assignee', ticket_row.queued_for_assignee,
        'queued_behind_ticket', ticket_row.queued_behind_ticket,
        'implementation', ticket_row.implementation,
        'audit_prompt', ticket_row.audit_prompt,
        'audit_signoff', ticket_row.audit_signoff,
        'needs_audit', ticket_row.needs_audit,
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

-- SYRD-270: blocked_by may also name work on another board.
CREATE OR REPLACE FUNCTION ticket_board.external_blocker_pattern()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- Work on another board, `project:PREFIX-N` (SYRD-270), or a person,
    -- `operator:name` (SYRD-273). `operator` is reserved: it is never a
    -- project, and a name is never shaped like a ticket id.
    SELECT '^((?!operator:)[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+|operator:[a-z][a-z0-9_]*)$';
$$;

CREATE OR REPLACE FUNCTION ticket_board.is_external_blocker(p_ref text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_ref, '') ~ ticket_board.external_blocker_pattern();
$$;

CREATE OR REPLACE FUNCTION ticket_board.normalize_blocker_ref(p_raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- A local id is upper case. A qualified reference keeps its project in
    -- lower case, as project names are written, and its ticket id in upper;
    -- a person (`operator:name`) is lower case throughout.
    SELECT CASE
        WHEN lower(split_part(btrim(coalesce(p_raw, '')), ':', 1)) = 'operator'
             AND position(':' IN btrim(coalesce(p_raw, ''))) > 0 THEN
            lower(btrim(coalesce(p_raw, '')))
        WHEN position(':' IN btrim(coalesce(p_raw, ''))) > 0 THEN
            lower(btrim(split_part(btrim(p_raw), ':', 1))) || ':'
            || upper(btrim(substr(btrim(p_raw), position(':' IN btrim(p_raw)) + 1)))
        ELSE upper(btrim(coalesce(p_raw, '')))
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
    own_board_ref text;
    missing_id text;
    cycle_id text;
BEGIN
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT coalesce(nullif(ticket_board.normalize_blocker_ref(raw_id), ''), '<empty>')
    INTO invalid_id
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    WHERE raw_id IS NULL
       OR btrim(raw_id) = ''
       OR NOT (
           ticket_board.normalize_blocker_ref(raw_id) ~ ticket_board.ticket_id_pattern()
           OR ticket_board.is_external_blocker(ticket_board.normalize_blocker_ref(raw_id))
       )
       OR ticket_board.normalize_blocker_ref(raw_id) = p_ticket_id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    -- SYRD-270: a qualified reference is for work this board does not hold.
    -- One naming this board's own ticket would never resolve when that ticket
    -- is done, which is exactly what a local blocker is for.
    SELECT ref
    INTO own_board_ref
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS ref
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    ) AS refs
    WHERE ticket_board.is_external_blocker(ref)
      AND split_part(ref, ':', 2) ~ ('^' || ticket_board.ticket_prefix() || '-[0-9]+$')
    LIMIT 1;
    IF own_board_ref IS NOT NULL THEN
        RAISE EXCEPTION 'external blocker % names a ticket on this board; block on % instead',
            own_board_ref, split_part(own_board_ref, ':', 2);
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = p_ticket_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    WITH normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    )
    SELECT normalized.blocker_id
    INTO missing_id
    FROM normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    WHERE blocker.id IS NULL
      AND NOT ticket_board.is_external_blocker(normalized.blocker_id)
    LIMIT 1;
    IF missing_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocker ticket not found: %', missing_id;
    END IF;

    WITH RECURSIVE normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
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

    -- An external blocker has no ticket here, so it is unresolved by
    -- construction; only release_external_blocker removes it.
    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
    SELECT p_ticket_id, normalized.blocker_id, min(normalized.ord)::integer - 1,
           coalesce(bool_or(blocker.state IN ('done', 'cancelled')), false)
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id, ord
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    GROUP BY normalized.blocker_id
    ORDER BY min(normalized.ord);

    UPDATE ticket_board.tickets
    SET blocked_reason = normalized_reason
    WHERE tickets.id = p_ticket_id;

    -- SYRD-148: a wait made before the blocker is not made true by it. Cleared
    -- here rather than beside the callers, so it happens in the same write that
    -- creates the blocker and no reader can see the pair disagree; and cleared
    -- rather than left for the delivery side to skip, because the wait is also
    -- what suppresses nudges and what the board shows the Director.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        UPDATE ticket_board.ticket_notification_state
        SET awaiting_role = '',
            awaiting_since_at = NULL,
            awaiting_notified_since_at = NULL
        WHERE ticket_id = p_ticket_id
          AND (awaiting_role <> '' OR awaiting_since_at IS NOT NULL);
    END IF;

    PERFORM ticket_board.refresh_ticket_source_json(p_ticket_id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.create_ticket(
    title text,
    body text,
    initial_state text,
    assignee text,
    blocked_by text[],
    blocked_reason text,
    needs_user_signoff boolean,
    needs_audit boolean
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
    normalized_assignee text := btrim(coalesce(assignee, 'unassigned'));
    normalized_parked boolean := false;
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'user'], 'create_ticket');
    IF normalized_assignee = '' THEN
        normalized_assignee := 'unassigned';
    END IF;
    IF NOT coalesce(needs_audit, true) AND ticket_board.current_app_actor() <> 'director' THEN
        RAISE EXCEPTION 'needs_audit can only be set to false by director' USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_state = '' THEN
        normalized_state := 'analysis';
    END IF;
    IF normalized_state NOT IN ('draft', 'analysis', 'backlog') THEN
        RAISE EXCEPTION 'invalid create state: %; allowed: draft, analysis, backlog', normalized_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(normalized_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', normalized_assignee;
    END IF;
    IF normalized_state = 'draft' AND normalized_assignee <> 'unassigned' THEN
        RAISE EXCEPTION 'draft tickets cannot be created with an assignee; release and route the draft instead';
    END IF;
    IF normalized_state = 'backlog' AND normalized_assignee = 'unassigned' THEN
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
        needs_audit,
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
        coalesce(needs_audit, true),
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
            'needs_audit', coalesce(needs_audit, true),
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
    blocked_reason text,
    needs_user_signoff boolean,
    needs_audit boolean
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, 'unassigned', blocked_by, blocked_reason, needs_user_signoff, needs_audit);
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
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.create_ticket(title, body, initial_state, blocked_by, blocked_reason, needs_user_signoff, true);
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
    SELECT ticket_board.create_ticket(title, body, initial_state, blocked_by, blocked_reason, false, true);
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

CREATE OR REPLACE FUNCTION ticket_board.file_report(
    title text,
    body text,
    origin_project text,
    external_source_ref text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    ticket_id text;
    normalized_origin_project text := btrim(coalesce(origin_project, ''));
    normalized_external_source_ref text := btrim(coalesce(external_source_ref, ''));
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
    actor text := ticket_board.current_actor_role();
BEGIN
    -- Report-only authority, deliberately NOT require_actor (SYRD-256).
    --
    -- A tenant report arrives on its own HTTP action, authenticated by the
    -- report token and by nothing else: it carries no caller role, because the
    -- whole point of that credential is that it is not one. require_actor
    -- resolves a role. On a legacy board it tolerated a missing one; on a
    -- declared-workflow board it asks current_app_actor(), which refuses NULL,
    -- so every report filed against a declared board failed with
    -- `invalid configured caller role: <NULL>` after the token had already been
    -- accepted -- the MEFP Director's live upstream report.
    --
    -- Resolving a role here would be wrong in either direction. Refusing the
    -- NULL role breaks the one caller this function has; admitting it by
    -- borrowing some role's capabilities would hand a report token that role's
    -- authority. So the role is not consulted at all: the database writer must
    -- be the service, and what the function may do is fixed below -- a new
    -- ticket in analysis, unassigned, audit required, no protected field
    -- settable by the caller. A role that somehow reached this function would
    -- gain nothing by carrying one.
    IF actor IS DISTINCT FROM 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call file_report; ticket_board_service is the only database writer', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF normalized_origin_project = '' THEN
        RAISE EXCEPTION 'origin_project must be non-empty';
    END IF;

    ticket_id := ticket_board.next_ticket_id();
    INSERT INTO ticket_board.tickets (
        id,
        title,
        body,
        state,
        assignee,
        parent_id,
        origin_project,
        external_source_ref,
        needs_audit,
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
        '',
        normalized_origin_project,
        normalized_external_source_ref,
        true,
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
            'parent_id', '',
            'origin_project', normalized_origin_project,
            'external_source_ref', normalized_external_source_ref,
            'needs_audit', true,
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    -- The export projection is rebuilt the way every writer rebuilds it, and
    -- that is an UPDATE, which on a declared board runs
    -- enforce_declared_ticket_update. That trigger requires a named actor
    -- before anything else and, with none, asks current_app_actor() -- which
    -- refuses the report path's missing role a second time.
    --
    -- So the actor is named, for this one statement, as REPORT_ACTOR: not a
    -- role, and unable ever to become one, because workflow_roles.name is
    -- CHECKed against ^[a-z][a-z0-9_-]{0,63}$ and this contains a colon. It
    -- satisfies "some actor is named" and nothing more: it is in no
    -- transition's actor list, it is not 'director', and so it can neither
    -- move, reassign, nor flip a flag. Every structural check the trigger
    -- makes still runs. It is set immediately before the refresh and cleared
    -- immediately after, exactly as the workflow executor scopes the same
    -- setting around its own update.
    PERFORM set_config('ticket_board.workflow_actor', 'report:tenant', true);
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text,
    assignee text,
    blocked_by text[],
    blocked_reason text,
    needs_audit boolean
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
    target_assignee text := btrim(coalesce(assignee, 'unassigned'));
    blocker_count integer;
    created_at_value timestamptz := clock_timestamp();
    created_text_value text := ticket_board.utc_text(created_at_value);
BEGIN
    IF target_assignee = '' THEN
        target_assignee := 'unassigned';
    END IF;
    actor := ticket_board.current_actor_role();
    IF actor = 'ticket_board_service' THEN
        PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research', 'audit'], 'file_bug');
        actor := ticket_board.current_app_actor();
    END IF;
    IF NOT coalesce(needs_audit, true) AND actor <> 'director' THEN
        RAISE EXCEPTION 'needs_audit can only be set to false by director' USING ERRCODE = '42501';
    END IF;
    IF actor <> 'ticket_board_service'
       AND actor <> 'audit'
       AND NOT ticket_board.ticket_is_implementer_assignee(actor) THEN
        RAISE EXCEPTION 'role % cannot call file_bug', actor
            USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(title, '')) = '' THEN
        RAISE EXCEPTION 'title must be non-empty';
    END IF;
    IF source_id !~ ticket_board.ticket_id_pattern() THEN
        RAISE EXCEPTION 'source_ticket_id must look like PREFIX-N';
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(target_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', target_assignee;
    END IF;
    IF NOT (
        target_assignee = 'unassigned' OR EXISTS (
        SELECT 1
        FROM ticket_board.workflow_stages ws
        WHERE ws.name = 'analysis'
          AND target_assignee = ANY(ws.owner_roles)
        )
    ) THEN
        target_assignee := 'unassigned';
    END IF;
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(blocked_by, ARRAY[]::text[])) AS raw_id;
    PERFORM 1 FROM ticket_board.tickets WHERE id = source_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'source_ticket_id ticket not found: %', source_id;
    END IF;

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
        parent_id,
        needs_audit,
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
        target_assignee,
        source_id,
        coalesce(needs_audit, true),
        created_text_value,
        created_text_value,
        created_at_value,
        created_at_value,
        jsonb_build_object(
            'id', ticket_id,
            'title', btrim(title),
            'body', coalesce(body, ''),
            'state', 'analysis',
            'assignee', target_assignee,
            'needs_audit', coalesce(needs_audit, true),
            'comments', '[]'::jsonb,
            'created', created_text_value,
            'updated', created_text_value
        ),
        ticket_id || '.json'
    );
    IF blocker_count > 0 THEN
        PERFORM ticket_board.apply_blockers(ticket_id, blocked_by, blocked_reason);
    END IF;
    IF actor <> 'ticket_board_service' THEN
        PERFORM ticket_board.append_ticket_comment(ticket_id, actor, 'Filed bug against ' || source_id || '.');
    END IF;
    PERFORM ticket_board.refresh_ticket_source_json(ticket_id);
    RETURN ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text,
    assignee text,
    blocked_by text[],
    blocked_reason text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_bug(title, body, source_ticket_id, assignee, blocked_by, blocked_reason, true);
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text,
    assignee text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_bug(title, body, source_ticket_id, assignee, ARRAY[]::text[], '');
$$;

CREATE OR REPLACE FUNCTION ticket_board.file_bug(
    title text,
    body text,
    source_ticket_id text
)
RETURNS text
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.file_bug(title, body, source_ticket_id, 'unassigned', ARRAY[]::text[], '');
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
    IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = new_state) THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
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
    PERFORM ticket_board.require_stage_owner_assignee(new_state, assignee);

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

CREATE OR REPLACE FUNCTION ticket_board.release_draft(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_state text;
BEGIN
    SELECT tickets.state
    INTO current_state
    FROM ticket_board.tickets
    WHERE tickets.id = release_draft.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('release_draft', id);
    IF current_state <> 'draft' THEN
        RAISE EXCEPTION 'release_draft requires a draft ticket';
    END IF;

    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'unassigned',
        parked = false
    WHERE tickets.id = release_draft.id;
    PERFORM ticket_board.touch_ticket(id);
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
    IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = new_state) THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
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

-- SYRD-77: ordinary director same-stage reassignment. This is deliberately not
-- a workflow transition: it never advances or returns a ticket, and it never
-- sets or clears a gate or a sign-off. Choosing a stage is what the configured
-- actions are for. The one stage change it can produce is the declared
-- serial-focus queue redirect, which the update trigger resolves from the
-- reservation itself so no write path can reach an implementation stage by
-- going around it.
CREATE OR REPLACE FUNCTION ticket_board.reassign(
    id text,
    assignee text,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text := ticket_board.current_app_actor();
    current_state text;
    current_assignee text;
    normalized_assignee text := btrim(lower(coalesce(reassign.assignee, '')));
    normalized_reason text := btrim(coalesce(reassign.reason, ''));
    ticket_row ticket_board.tickets%ROWTYPE;
    message text;
BEGIN
    -- Director-only at the database boundary, not merely at the API. A missing
    -- caller role is a refusal here rather than a fallback to the service
    -- identity: ownership changes are attributed or they do not happen.
    IF ticket_board.current_actor_role() <> 'ticket_board_service' OR actor IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'role % cannot call reassign',
            coalesce(nullif(actor, ''), ticket_board.current_actor_role())
            USING ERRCODE = '42501';
    END IF;
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'reassign requires a reason';
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(normalized_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', reassign.assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = reassign.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;
    IF current_assignee = normalized_assignee THEN
        RAISE EXCEPTION 'ticket % is already assigned to %', id, normalized_assignee;
    END IF;
    -- Refuse an owner the stage cannot have instead of moving the ticket to
    -- suit the owner.
    PERFORM ticket_board.require_stage_owner_assignee(current_state, normalized_assignee);

    -- Recorded before the write, so the reason survives even when the serial
    -- reservation then redirects the ticket and appends its own notice.
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director reassignment in ' || current_state || ': '
        || current_assignee || ' -> ' || normalized_assignee
        || '. Reason: ' || normalized_reason
    );

    UPDATE ticket_board.tickets
    SET assignee = normalized_assignee
    WHERE tickets.id = reassign.id;

    SELECT * INTO ticket_row FROM ticket_board.tickets WHERE tickets.id = reassign.id;
    -- Serial focus held it. The queue announcement has already told the
    -- director, and the target implementer has no actionable work yet, so a
    -- second notification here would be wrong rather than merely noisy.
    IF ticket_row.state IS DISTINCT FROM current_state OR ticket_row.queued_for_assignee <> '' THEN
        RETURN;
    END IF;
    -- Held or blocked work is not actionable either. It stays reassigned; the
    -- existing unblock and release paths announce it when it becomes real work.
    IF coalesce(ticket_row.manually_controlled, false) OR ticket_board.ticket_has_unresolved_blockers(id) THEN
        RETURN;
    END IF;
    message := id
        || CASE WHEN ticket_row.title <> '' THEN ' -- ' || ticket_row.title ELSE '' END
        || ' is now assigned to you';
    -- One notification, through the same durable queue every other handoff
    -- uses: it dedupes per transaction, suppresses a director self-handoff, and
    -- stays silent for stages that declare no notification.
    PERFORM ticket_board.enqueue_transition_notification(
        id,
        ticket_row.title,
        current_state,
        ticket_row.state,
        ticket_row.assignee,
        ticket_row.updated_at,
        ticket_row.ticket_number,
        message
    );
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
    actor := ticket_board.require_workflow_transition_actor('start_work', id);
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
    ticket_commit_exempt boolean;
    ticket_last_rejected_commit text;
    ticket_state text;
    target_state text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('submit_to_audit', id);
    SELECT tickets.commit_exempt, tickets.last_rejected_commit, tickets.state
    INTO ticket_commit_exempt, ticket_last_rejected_commit, ticket_state
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_audit.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    SELECT wt.to_stage
    INTO target_state
    FROM ticket_board.workflow_transitions wt
    WHERE wt.from_stage = ticket_state
      AND wt.action_name = 'submit_to_audit'
    ORDER BY wt.to_stage
    LIMIT 1;
    IF target_state IS NULL THEN
        RAISE EXCEPTION 'submit_to_audit is not configured from %', ticket_state;
    END IF;
    IF normalized_commit <> '' AND normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF normalized_commit = '' AND NOT ticket_commit_exempt THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF ticket_last_rejected_commit IS NOT NULL
       AND ticket_last_rejected_commit <> ''
       AND normalized_commit = ticket_last_rejected_commit THEN
        RAISE EXCEPTION 'cannot submit: commit % was just kicked back with no change -- do the fix and submit a NEW commit.',
            coalesce(nullif(ticket_last_rejected_commit, ''), '<none>');
    END IF;
    UPDATE ticket_board.tickets
    SET state = target_state,
        commit_hash = normalized_commit,
        audit_signoff = false,
        inspector_signoff = false,
        last_rejected_commit = NULL
    WHERE tickets.id = submit_to_audit.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.submit_to_inspection(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    ticket_state text;
    ticket_assignee text;
    ticket_needs_inspection boolean;
BEGIN
    SELECT tickets.state, tickets.assignee, tickets.needs_inspection
    INTO ticket_state, ticket_assignee, ticket_needs_inspection
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_inspection.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('submit_to_inspection', id, ticket_assignee);
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'submit_to_inspection requires an in_progress ticket';
    END IF;
    IF NOT ticket_needs_inspection THEN
        RAISE EXCEPTION 'submit_to_inspection requires needs_inspection=true';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'inspection',
        assignee = 'inspector'
    WHERE tickets.id = submit_to_inspection.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

-- Submit finished work that produced no commit.
--
-- submit_to_audit already accepts an empty hash when commit_exempt is set; the
-- gap PGU-913 closes is that an implementer could not set it. The alternative
-- on offer was request_commit_exempt, which throws the ticket back to analysis
-- and unassigns it -- correct for "I cannot do this", wrong for "this is done
-- and there is no diff". With no usable path, three tickets reached audit
-- carrying a BORROWED hash pointing at unrelated work (PGU-889, PGU-903,
-- PGU-911), which is worse than an empty one: it resolves, looks
-- authoritative, and sends a reviewer into someone else's change.
--
-- This does NOT hand implementers the commit_exempt field. The exemption can
-- only be set as part of submitting, it requires a reason, and the reason is
-- recorded as an attributed comment before the transition, so audit sees an
-- explicit claim and who made it rather than an absence. Audit can kick it
-- back like any other submission.
CREATE OR REPLACE FUNCTION ticket_board.submit_to_audit_without_commit(
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
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'submit_to_audit_without_commit requires a non-empty reason';
    END IF;
    -- Authorized on its OWN action, owner_scoped, rather than borrowing
    -- submit_to_audit's config. submit_to_audit is owner_scoped = false -- any
    -- implementer can submit any ticket, with the assignee gate applied at the
    -- server -- but waiving the commit requirement is a claim about your own
    -- work, so it is scoped in the database the way request_commit_exempt is.
    -- submit_to_audit re-checks its own looser rule below, which a caller who
    -- passed this one always satisfies.
    actor := ticket_board.require_workflow_transition_actor('submit_to_audit_without_commit', id);
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Submitted to audit with no commit: ' || normalized_reason
    );
    UPDATE ticket_board.tickets
    SET commit_exempt = true
    WHERE tickets.id = submit_to_audit_without_commit.id;
    -- Everything else -- state machine, kicked-back-commit rule, signoff reset --
    -- stays in submit_to_audit. If it refuses, this whole call rolls back and no
    -- exemption is left behind on a ticket that never moved.
    PERFORM ticket_board.submit_to_audit(id, '');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.request_commit_exempt(
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
    ticket_assignee text;
    ticket_state text;
    ticket_commit_exempt boolean;
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'request_commit_exempt requires a non-empty reason';
    END IF;

    SELECT tickets.assignee, tickets.state, tickets.commit_exempt
    INTO ticket_assignee, ticket_state, ticket_commit_exempt
    FROM ticket_board.tickets
    WHERE tickets.id = request_commit_exempt.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    actor := ticket_board.require_workflow_transition_actor('request_commit_exempt', id, ticket_assignee);
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'request_commit_exempt requires an in_progress ticket';
    END IF;
    IF ticket_commit_exempt THEN
        RAISE EXCEPTION 'ticket is already commit_exempt';
    END IF;

    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Commit exemption requested: ' || normalized_reason
    );
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'unassigned',
        parked = false
    WHERE tickets.id = request_commit_exempt.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.start_task(
    id text,
    note text DEFAULT ''
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
    ticket_assignee text;
    normalized_note text := btrim(coalesce(note, ''));
BEGIN
    SELECT tickets.state, tickets.assignee
    INTO ticket_state, ticket_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = start_task.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    comment_actor := ticket_board.require_workflow_transition_actor('start_task', id, ticket_assignee);
    IF ticket_state <> 'backlog' THEN
        RAISE EXCEPTION 'start_task requires a backlog ticket';
    END IF;
    PERFORM ticket_board.require_stage_owner_assignee('analysis', ticket_assignee);
    IF normalized_note <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_note);
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        parked = false
    WHERE tickets.id = start_task.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.complete_task(
    id text,
    completion_note text
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
    ticket_assignee text;
    normalized_note text := btrim(coalesce(completion_note, ''));
    blocker_id text;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector'],
        'complete_task'
    );
    comment_actor := ticket_board.current_app_actor();
    IF normalized_note = '' THEN
        RAISE EXCEPTION 'complete_task requires a non-empty completion_note';
    END IF;
    SELECT tickets.state, tickets.assignee
    INTO ticket_state, ticket_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = complete_task.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF ticket_assignee <> comment_actor AND comment_actor <> 'director' THEN
        RAISE EXCEPTION '% cannot call complete_task for ticket assigned to %', comment_actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    IF ticket_state NOT IN ('backlog', 'analysis') THEN
        RAISE EXCEPTION 'complete_task requires a backlog or analysis ticket';
    END IF;
    IF ticket_board.ticket_has_unresolved_blockers(id) THEN
        SELECT b.blocker_ticket_id
        INTO blocker_id
        FROM ticket_board.ticket_blockers b
        WHERE b.ticket_id = complete_task.id
          AND NOT b.resolved
        ORDER BY b.position
        LIMIT 1;
        RAISE EXCEPTION 'unresolved blocker prevents task completion: %', blocker_id;
    END IF;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_note);
    PERFORM set_config('ticket_board.utility_task_complete', 'on', true);
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_exempt = true,
        commit_hash = '',
        last_rejected_commit = NULL,
        parked = false
    WHERE tickets.id = complete_task.id;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

DROP FUNCTION IF EXISTS ticket_board.audit_sign_off(text);
CREATE OR REPLACE FUNCTION ticket_board.audit_sign_off(
    id text,
    comment_text text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    was_approved boolean;
    held boolean;
    actor text;
    comment_actor text;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('audit_sign_off', id);
    SELECT tickets.audit_signoff,tickets.manually_controlled INTO was_approved,held
    FROM ticket_board.tickets WHERE tickets.id=audit_sign_off.id AND tickets.state='audit' FOR UPDATE;
    IF held AND was_approved THEN RETURN; END IF;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    UPDATE ticket_board.tickets
    SET audit_signoff = true
    WHERE tickets.id = audit_sign_off.id
      AND tickets.state = 'audit';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT coalesce(was_approved,false) THEN
        PERFORM ticket_board.notify_held_review_completion(id,'audit');
    END IF;
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
    comment_actor := ticket_board.require_workflow_transition_actor('audit_kick_back', id);
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
    comment_actor := ticket_board.require_workflow_transition_actor('audit_kick_back', id);
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

CREATE OR REPLACE FUNCTION ticket_board.implementer_kick_back(
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
    comment_actor text;
    normalized_reason text := btrim(coalesce(reason, ''));
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('implementer_kick_back', id);
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'implementer_kick_back requires a non-empty reason';
    END IF;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, normalized_reason);
    UPDATE ticket_board.tickets
    SET state = 'analysis',
        assignee = 'director',
        parked = false
    WHERE tickets.id = implementer_kick_back.id
      AND tickets.state = 'in_progress';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'in_progress ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
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
        assignee = 'user'
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

CREATE OR REPLACE FUNCTION ticket_board.inspector_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    was_approved boolean;
    held boolean;
    actor text;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('inspector_sign_off', id);
    SELECT tickets.inspector_signoff,tickets.manually_controlled INTO was_approved,held
    FROM ticket_board.tickets WHERE tickets.id=inspector_sign_off.id AND tickets.state='inspection' FOR UPDATE;
    IF held AND was_approved THEN RETURN; END IF;
    UPDATE ticket_board.tickets
    SET inspector_signoff = true,
        state = CASE WHEN manually_controlled THEN 'inspection' ELSE 'audit' END
    WHERE tickets.id = inspector_sign_off.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
    END IF;
    -- Held: the move this approval earned is deferred, so it is recorded and
    -- paid when the hold is released. The legacy trigger re-evaluates audit and
    -- user review on the next unheld write but never inspection, so without this
    -- the one stage with a declared inspector is the one that strands (SYRD-180).
    IF held THEN
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(inspector_sign_off.id,'inspector_sign_off',actor,'audit')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT coalesce(was_approved,false) THEN
        PERFORM ticket_board.notify_held_review_completion(id,'inspection');
    END IF;
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
    comment_actor := ticket_board.require_workflow_transition_actor('inspector_kick_back', id);
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
    comment_actor := ticket_board.require_workflow_transition_actor('inspector_kick_back', id);
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
    was_approved boolean;
    held boolean;
    actor text;
    comment_actor text;
    ticket_state text;
    requires_signoff boolean;
BEGIN
    comment_actor := ticket_board.require_workflow_transition_actor('user_sign_off', id);
    SELECT tickets.user_signoff,tickets.manually_controlled INTO was_approved,held
    FROM ticket_board.tickets WHERE tickets.id=user_sign_off.id AND tickets.state='user_review' FOR UPDATE;
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
    IF held AND was_approved THEN RETURN; END IF;
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
    UPDATE ticket_board.tickets
    SET user_signoff = true
    WHERE tickets.id = user_sign_off.id;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT coalesce(was_approved,false) THEN
        PERFORM ticket_board.notify_held_review_completion(id,'user_review');
    END IF;
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
    SET state = 'analysis',
        assignee = 'director'
    WHERE tickets.id = user_reopen.id
      AND tickets.state IN ('user_review', 'director_review', 'done');
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
    ticket_commit_exempt boolean;
BEGIN
    actor := ticket_board.require_workflow_transition_actor('mark_done', id);
    SELECT tickets.commit_exempt
    INTO ticket_commit_exempt
    FROM ticket_board.tickets
    WHERE tickets.id = mark_done.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF normalized_commit <> '' AND normalized_commit !~ '^[0-9A-Fa-f]{7,40}$' THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    IF normalized_commit = '' AND NOT ticket_commit_exempt THEN
        RAISE EXCEPTION 'commit_hash must be a 7-40 character hex commit';
    END IF;
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_hash = normalized_commit,
        last_rejected_commit = NULL
    WHERE tickets.id = mark_done.id;
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
    actor := ticket_board.require_workflow_transition_actor('defer', id);
    UPDATE ticket_board.tickets
    SET state = 'backlog',
        parked = true
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
    comment_actor := ticket_board.require_workflow_transition_actor('cancel', id);
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    UPDATE ticket_board.tickets
    SET state = 'cancelled',
        parked = false
    WHERE tickets.id = cancel.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
END;
$$;

-- SYRD-180: a review approval given while a ticket is manually controlled.
--
-- The hold is meant to DEFER a reviewer's decision, not to consume it. The
-- declared executor already computes the transition the approval earned, and
-- then throws it away to keep the deliberate stage/owner hold. This table is
-- where it is kept instead, so releasing the hold can replay exactly that one
-- transition, taken by the role that earned it, rather than a destination
-- re-derived later from a parallel table that could disagree.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_deferred_review (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    action_name text NOT NULL,
    actor text NOT NULL,
    to_stage text NOT NULL,
    deferred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- What a released hold still owes, by name. NULL when nothing is owed, which is
-- the normal case: only an approval taken under a hold records anything here.
CREATE OR REPLACE FUNCTION ticket_board.pending_held_review(p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
-- Definer, like every other reader of an internal table: the deferral store is
-- written only by the functions above and carries no role grants of its own, so
-- a caller's answer must not depend on whether this board was built from the
-- schema or reached this state through the migration.
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT d.action_name
    FROM ticket_board.ticket_deferred_review d
    WHERE d.ticket_id = p_ticket_id;
$$;

-- Pay it. Exactly once, because the row is deleted with the move; and only when
-- the move is still the one the workflow allows from where the ticket now is.
CREATE OR REPLACE FUNCTION ticket_board.reconcile_released_hold(p_ticket_id text)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    deferred ticket_board.ticket_deferred_review;
    held boolean;
    current_state text;
BEGIN
    SELECT * INTO deferred FROM ticket_board.ticket_deferred_review d
    WHERE d.ticket_id = p_ticket_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT t.manually_controlled, t.state INTO held, current_state
    FROM ticket_board.tickets t WHERE t.id = p_ticket_id FOR UPDATE;
    IF held THEN
        RETURN NULL;
    END IF;
    -- The ticket may have been moved by hand while it was held -- a director
    -- edit, an override, a return. The deferred step is then no longer a step
    -- this ticket can take, and replaying it would either fail or move it
    -- somewhere nobody chose. Drop it rather than carry it.
    IF NOT ticket_board.workflow_transition_allowed_config(current_state, deferred.to_stage) THEN
        DELETE FROM ticket_board.ticket_deferred_review d WHERE d.ticket_id = p_ticket_id;
        RETURN NULL;
    END IF;
    -- A blocked ticket is not promoted by releasing a hold, and its approval is
    -- not spent either: it stays owed until the blocker clears, which is what
    -- the workflow does with every other forward promotion (SYRD-148).
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN NULL;
    END IF;
    -- Taken by the role that earned it, through the same seam a recovery uses,
    -- so the transition is authorized, gated, assigned and notified by the one
    -- implementation that does that -- not by a second copy of it here.
    PERFORM set_config('ticket_board.workflow_action', deferred.action_name, true);
    PERFORM set_config('ticket_board.workflow_actor', deferred.actor, true);
    UPDATE ticket_board.tickets
    SET state = deferred.to_stage
    WHERE tickets.id = p_ticket_id
      AND tickets.state = current_state;
    PERFORM set_config('ticket_board.workflow_action', '', true);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    DELETE FROM ticket_board.ticket_deferred_review d WHERE d.ticket_id = p_ticket_id;
    RETURN deferred.action_name;
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
    -- Releasing the hold pays what the hold deferred, and pays it once: the
    -- record is retired with the move, so a second release finds nothing owed
    -- (SYRD-180).
    IF NOT set_manually_controlled.manually_controlled THEN
        PERFORM ticket_board.reconcile_released_hold(id);
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
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id, min(ord) AS first_ord
        FROM unnest(coalesce(ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    ) AS normalized;

    PERFORM ticket_board.apply_blockers(id, ids, reason);
    PERFORM ticket_board.touch_ticket(id);
    IF current_blockers IS DISTINCT FROM next_blockers OR coalesce(current_reason, '') IS DISTINCT FROM next_reason THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'blockers');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_external_blocker(
    p_ticket text,
    p_ref text,
    p_reason text,
    p_evidence text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    v_ticket text := upper(btrim(coalesce(p_ticket, '')));
    ref text := ticket_board.normalize_blocker_ref(p_ref);
    reason text := btrim(coalesce(p_reason, ''));
    evidence text := btrim(coalesce(p_evidence, ''));
    released_text text;
BEGIN
    -- The authority that sets a blocker is the one that releases it: the
    -- Director's, or on a declared board whoever holds set_blockers.
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    IF reason = '' THEN
        RAISE EXCEPTION 'releasing an external blocker requires a reason';
    END IF;
    IF NOT ticket_board.is_external_blocker(ref) THEN
        RAISE EXCEPTION 'not an external blocker: %; a blocker on this board resolves when its ticket is done', p_ref;
    END IF;
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = v_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', v_ticket;
    END IF;

    DELETE FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = v_ticket
      AND b.blocker_ticket_id = ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % has no external blocker %', v_ticket, ref;
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b
                   WHERE b.ticket_id = v_ticket) THEN
        UPDATE ticket_board.tickets t SET blocked_reason = '' WHERE t.id = v_ticket;
    END IF;

    -- The release is on the record, with why and, where the caller verified
    -- one, what this board itself now recognizes.
    released_text := format('External blocker %s released: %s', ref, reason);
    IF evidence <> '' THEN
        released_text := released_text || E'\n' || evidence;
    END IF;
    PERFORM ticket_board.append_ticket_comment(v_ticket, ticket_board.current_app_actor(), released_text, false);
    PERFORM ticket_board.touch_ticket(v_ticket);
    PERFORM ticket_board.refresh_ticket_source_json(v_ticket);
    -- The owner is told, as for any change to the ticket's blockers. Nothing is
    -- moved: whatever the wait was holding back still goes through its gates.
    PERFORM ticket_board.notify_ticket_owner_in_place_change(v_ticket, 'external blocker released');
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
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', 'on', true);
    PERFORM ticket_board.touch_ticket(id);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', '', true);
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
    IF patch ? 'commit_hash' THEN
        RAISE EXCEPTION 'commit_hash must be written with submit_to_audit or mark_done, not edit_fields';
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
        'needs_audit',
        'needs_inspection',
        'needs_user_signoff',
        'commit_exempt',
        'regression',
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
    IF patch ? 'needs_audit'
       AND (patch->>'needs_audit')::boolean = false
       AND ticket_board.current_app_actor() <> 'director' THEN
        RAISE EXCEPTION 'needs_audit can only be set to false by director' USING ERRCODE = '42501';
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
            IF normalized_parent !~ ticket_board.ticket_id_pattern() THEN
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
        needs_audit = CASE WHEN patch ? 'needs_audit' THEN (patch->>'needs_audit')::boolean ELSE needs_audit END,
        needs_inspection = CASE WHEN patch ? 'needs_inspection' THEN (patch->>'needs_inspection')::boolean ELSE needs_inspection END,
        needs_user_signoff = CASE WHEN patch ? 'needs_user_signoff' THEN (patch->>'needs_user_signoff')::boolean ELSE needs_user_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
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

CREATE OR REPLACE FUNCTION ticket_board.append_ticket_attachment(
    p_id text,
    p_path text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_position integer;
    normalized_path text := btrim(coalesce(p_path, ''));
BEGIN
    IF normalized_path = '' THEN
        RAISE EXCEPTION 'attachment path must be non-empty';
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_id;
    END IF;

    SELECT COALESCE(max(position), -1) + 1
    INTO next_position
    FROM ticket_board.ticket_attachments
    WHERE ticket_id = p_id;

    INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field, metadata)
    VALUES (p_id, next_position, normalized_path, next_position = 0, 'screenshots', coalesce(p_metadata, '{}'::jsonb))
    ON CONFLICT (ticket_id, path) DO UPDATE
    SET metadata = EXCLUDED.metadata;

    UPDATE ticket_board.tickets
    SET screenshot = COALESCE(
        screenshot,
        (
            SELECT path
            FROM ticket_board.ticket_attachments
            WHERE ticket_id = p_id
            ORDER BY position
            LIMIT 1
        )
    )
    WHERE id = p_id;

    PERFORM ticket_board.touch_ticket(p_id);
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

    INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field, metadata)
    SELECT target_id,
           next_attachment_position + row_number() OVER (ORDER BY position)::integer - 1,
           source.path,
           false,
           source.source_field,
           source.metadata
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
    -- Merge's own terminal close, named for exactly one ticket and only for
    -- the statement that performs it. Without this the update is rechecked as
    -- an ordinary configured transition carrying no action, which no
    -- transition table can match, and a merge that had already copied the
    -- comments and attachments failed half-done (SYRD-79).
    PERFORM set_config('ticket_board.merge_source', source_id, true);
    UPDATE ticket_board.tickets
    SET state = 'done',
        commit_exempt = true,
        manually_controlled = source_ticket.manually_controlled,
        parked = false
    WHERE id = source_id;
    -- Closed immediately, so nothing later in this transaction inherits it.
    PERFORM set_config('ticket_board.merge_source', '', true);
    PERFORM ticket_board.append_ticket_comment(source_id, 'director', 'Merged into ' || target_id);
    PERFORM ticket_board.touch_ticket(source_id);
    PERFORM ticket_board.touch_ticket(target_id);
END;
$$;

INSERT INTO ticket_board.schema_migrations (name)
VALUES ('schema.sql')
ON CONFLICT (name) DO NOTHING;


-- SYRD-69: one Unix identity per project; runtime and process authority are data.
CREATE TABLE IF NOT EXISTS ticket_board.workflow_roles (
    name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{0,63}$'),
    definition jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignments (
    role text PRIMARY KEY REFERENCES ticket_board.workflow_roles(name) ON DELETE CASCADE,
    runtime text NOT NULL CHECK (runtime IN ('claude','codex','agy','hermes')),
    actual_target text NOT NULL CHECK (actual_target ~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'),
    worktree text NOT NULL CHECK (worktree LIKE '/%'),
    session_dir text NOT NULL CHECK (session_dir LIKE '/%'),
    process_pid bigint NOT NULL CHECK (process_pid > 1),
    process_start_time bigint NOT NULL CHECK (process_start_time > 0),
    process_uid bigint NOT NULL CHECK (process_uid >= 0),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(actual_target),
    UNIQUE(process_pid, process_start_time, process_uid)
);
CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignment_history (
    role text NOT NULL,
    generation bigint NOT NULL,
    runtime text NOT NULL,
    actual_target text NOT NULL,
    worktree text NOT NULL,
    session_dir text NOT NULL,
    process_pid bigint NOT NULL,
    process_start_time bigint NOT NULL,
    process_uid bigint NOT NULL,
    registered_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(role, generation)
);

DROP FUNCTION IF EXISTS ticket_board.register_role_runtime(text,text,text,text,text,bigint,bigint,bigint);
CREATE OR REPLACE FUNCTION ticket_board.register_role_runtime(
    p_role text,
    p_runtime text,
    p_actual_target text,
    p_worktree text,
    p_session_dir text,
    p_process_pid bigint,
    p_process_start_time bigint,
    p_process_uid bigint,
    p_expected_generation bigint
)
RETURNS SETOF ticket_board.role_runtime_assignments
LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE
    normalized_role text := lower(btrim(coalesce(p_role, '')));
    configured jsonb;
    published ticket_board.role_runtime_assignments%ROWTYPE;
BEGIN
    SELECT definition INTO configured
      FROM ticket_board.workflow_roles
     WHERE name=normalized_role
     FOR SHARE;
    IF configured IS NULL
       AND NOT EXISTS (SELECT FROM ticket_board.workflow_configuration)
       AND (
           EXISTS (SELECT FROM ticket_board.workflow_stages WHERE normalized_role=ANY(owner_roles))
           OR EXISTS (SELECT FROM ticket_board.workflow_transitions WHERE normalized_role=ANY(allowed_roles))
       ) THEN
        configured := jsonb_build_object(
            'name', normalized_role, 'active', true,
            'runtime', p_runtime, 'target', p_actual_target
        );
        INSERT INTO ticket_board.workflow_roles(name, definition)
        VALUES (normalized_role, configured)
        ON CONFLICT (name) DO NOTHING;
        SELECT definition INTO configured
          FROM ticket_board.workflow_roles WHERE name=normalized_role FOR SHARE;
    END IF;
    IF configured IS NULL OR NOT coalesce((configured->>'active')::boolean, false)
       OR configured->>'runtime' IS DISTINCT FROM p_runtime
       OR configured->>'target' IS DISTINCT FROM p_actual_target THEN
        RAISE EXCEPTION 'role/runtime/target is not active workflow configuration';
    END IF;
    IF p_actual_target !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'
       OR p_worktree NOT LIKE '/%' OR p_session_dir NOT LIKE '/%'
       OR p_process_pid <= 1 OR p_process_start_time <= 0 OR p_process_uid < 0 THEN
        RAISE EXCEPTION 'invalid role runtime registration';
    END IF;

    INSERT INTO ticket_board.role_runtime_assignments AS assignment(
        role, runtime, actual_target, worktree, session_dir,
        process_pid, process_start_time, process_uid
    ) VALUES (
        normalized_role, p_runtime, p_actual_target, p_worktree, p_session_dir,
        p_process_pid, p_process_start_time, p_process_uid
    )
    ON CONFLICT (role) DO UPDATE SET
        runtime=EXCLUDED.runtime,
        actual_target=EXCLUDED.actual_target,
        worktree=EXCLUDED.worktree,
        session_dir=EXCLUDED.session_dir,
        process_pid=EXCLUDED.process_pid,
        process_start_time=EXCLUDED.process_start_time,
        process_uid=EXCLUDED.process_uid,
        generation=assignment.generation + 1,
        registered_at=clock_timestamp()
    WHERE assignment.generation = p_expected_generation
    RETURNING assignment.* INTO published;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'runtime assignment generation changed';
    END IF;
    INSERT INTO ticket_board.role_runtime_assignment_history(
        role, generation, runtime, actual_target, worktree, session_dir,
        process_pid, process_start_time, process_uid, registered_at
    ) VALUES (
        published.role, published.generation, published.runtime,
        published.actual_target, published.worktree, published.session_dir,
        published.process_pid, published.process_start_time,
        published.process_uid, published.registered_at
    );
    RETURN NEXT published;
END;
$$;

REVOKE ALL ON ticket_board.role_runtime_assignments FROM PUBLIC;
REVOKE ALL ON ticket_board.role_runtime_assignment_history FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_service') THEN
        GRANT SELECT ON ticket_board.role_runtime_assignments TO ticket_board_service;
        GRANT SELECT ON ticket_board.role_runtime_assignment_history TO ticket_board_service;
        GRANT EXECUTE ON FUNCTION ticket_board.register_role_runtime(text,text,text,text,text,bigint,bigint,bigint,bigint)
        TO ticket_board_service;
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_listener') THEN
        GRANT SELECT ON ticket_board.role_runtime_assignments TO ticket_board_listener;
    END IF;
END $$;


-- SYRD-11 opt-in declarative foundation
-- Opt-in foundation. Merely installing this migration changes no tenant workflow.
CREATE TABLE IF NOT EXISTS ticket_board.workflow_configuration (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    revision bigint NOT NULL,
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_board.workflow_revisions (
    revision bigserial PRIMARY KEY,
    actor text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    document jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_board.workflow_roles (
    name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_-]{0,63}$'),
    definition jsonb NOT NULL
);
ALTER TABLE ticket_board.tickets ADD COLUMN IF NOT EXISTS workflow_flags jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION ticket_board.declared_workflow()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT document FROM ticket_board.workflow_configuration WHERE singleton;
$$;

-- The sign-off reset a transition performs, as a value two transitions can be
-- compared by. Ordered and de-nulled so that "clears the same sign-offs" does
-- not depend on the order somebody wrote them in (SYRD-217).
CREATE OR REPLACE FUNCTION ticket_board.signoff_reset_key(tr jsonb)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce((SELECT array_agg(x ORDER BY x)
                       FROM jsonb_array_elements_text(tr->'clear_signoffs') x),
                    ARRAY[]::text[]);
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_workflow(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r jsonb; s jsonb; tr jsonb; f record; cursor_name text; visited text[];
    names text[]; role_names text[]; active_roles text[]; flag_names text[];
BEGIN
    IF cfg->>'schema' IS DISTINCT FROM 'switchyard.workflow.v1'
       OR jsonb_typeof(cfg->'roles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'stages') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'transitions') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'flags') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid workflow document';
    END IF;
    SELECT array_agg(x->>'name') INTO names FROM jsonb_array_elements(cfg->'stages') x;
    SELECT array_agg(x->>'name'), coalesce(array_agg(x->>'name') FILTER (WHERE (x->>'active')::boolean),ARRAY[]::text[])
      INTO role_names, active_roles FROM jsonb_array_elements(cfg->'roles') x;
    SELECT array_agg(key) INTO flag_names FROM jsonb_each(cfg->'flags');
    IF cardinality(names) IS NULL OR cardinality(role_names) IS NULL
       OR cardinality(names) <> (SELECT count(DISTINCT n) FROM unnest(names) n)
       OR cardinality(role_names) <> (SELECT count(DISTINCT n) FROM unnest(role_names) n)
       OR NOT ARRAY['director','user','unassigned']::text[] <@ role_names
       OR NOT coalesce('director' = ANY(active_roles),false) THEN RAISE EXCEPTION 'missing or duplicate identities'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
        WHERE x->>'name'='director'
          AND x->'capabilities' ?& ticket_board.director_control_capabilities()) THEN
        RAISE EXCEPTION 'director must keep its control capabilities: %',
            (SELECT string_agg(c, ', ' ORDER BY c) FROM unnest(ticket_board.director_control_capabilities()) c
             WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
                 WHERE x->>'name'='director' AND x->'capabilities' ? c));
    END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        IF jsonb_typeof(r->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(r->'capabilities') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'role fields must have explicit types'; END IF;
        IF NOT r ?& ARRAY['name','label','kind','active','capabilities']
           OR (r->>'name') !~ '^[a-z][a-z0-9_-]{0,63}$' OR btrim(r->>'label') = ''
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign','director_edit','request_publication','resolve_publication',
                'recover_stalled_ticket')) THEN
            RAISE EXCEPTION 'invalid role policy: %', r->>'name';
        END IF;
        IF (r->>'runtime' IS NULL) <> (r->>'target' IS NULL)
           OR (r->>'runtime' IS NOT NULL AND (r->>'runtime' NOT IN ('claude','codex','agy','hermes')
               OR r->>'target' !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$')) THEN
            RAISE EXCEPTION 'invalid runtime/target'; END IF;
        -- SYRD-135: absent is false. Only a real boolean may say otherwise, so
        -- a quoted "false" cannot read as a value and behave as its truthiness.
        IF r ? 'ephemeral' AND jsonb_typeof(r->'ephemeral') <> 'boolean' THEN
            RAISE EXCEPTION 'ephemeral must be a boolean: %', r->>'name'; END IF;
        -- SYRD-37: whether this role is handed one ticket at a time in the
        -- stages it owns. Same rule and same reason as `ephemeral`: absent is
        -- "unchanged", and only a real boolean may say otherwise.
        IF r ? 'serial' AND jsonb_typeof(r->'serial') <> 'boolean' THEN
            RAISE EXCEPTION 'serial must be a boolean: %', r->>'name'; END IF;
        -- SYRD-141: what a person reads on this role's pane. Optional; a
        -- non-empty single-line string when present, because a terminal
        -- renders it and a newline in a title is an instruction, not text.
        IF r ? 'presentation_label' AND (jsonb_typeof(r->'presentation_label') <> 'string'
            OR btrim(r->>'presentation_label') = '' OR r->>'presentation_label' LIKE E'%\n%') THEN
            RAISE EXCEPTION 'presentation_label must be non-empty single-line text: %', r->>'name'; END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'slot' FROM jsonb_array_elements(cfg->'roles') x
       WHERE x->>'slot' IS NOT NULL GROUP BY x->>'slot' HAVING count(*) > 1)
       OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'slot' IS NOT NULL
          AND ((x->>'slot')::int NOT BETWEEN 0 AND 5 OR NOT (x->>'active')::boolean OR x->>'target' IS NULL)) THEN
        RAISE EXCEPTION 'invalid or duplicate visible slot'; END IF;
    FOR f IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF EXISTS (SELECT FROM pg_attribute WHERE attrelid='ticket_board.tickets'::regclass AND attname=f.key AND NOT attisdropped) AND f.key NOT IN ('needs_inspection','needs_audit','needs_user_signoff','inspector_signoff','audit_signoff','user_signoff') THEN RAISE EXCEPTION 'flag collides with protected ticket field'; END IF;
        IF jsonb_typeof(f.value->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'flag kind must be explicit'; END IF;
        IF f.key IN ('needs_inspection','needs_audit','needs_user_signoff') AND f.value->>'kind'<>'gate' OR f.key IN ('inspector_signoff','audit_signoff','user_signoff') AND f.value->>'kind'<>'signoff' THEN RAISE EXCEPTION 'cannot change builtin flag kind'; END IF;
        IF f.key !~ '^[a-z][a-z0-9_]{0,63}$' OR f.value->>'kind' NOT IN ('gate','signoff')
           OR jsonb_typeof(f.value->'default') IS DISTINCT FROM 'boolean'
           OR (f.value->>'kind' = 'signoff' AND (f.value->>'default')::boolean) THEN
            RAISE EXCEPTION 'invalid flag policy'; END IF;
    END LOOP;
    IF cfg->'queue' IS NOT NULL AND cfg->'queue'<>'null'::jsonb AND NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cfg->'queue'->>'stage'
        AND x->>'kind' IN ('draft','system') AND x->>'gate' IS NULL AND NOT (x->>'terminal')::boolean
        AND (jsonb_array_length(x->'owners')=0 OR x->'owners' ? (cfg->'queue'->>'assignee'))
        AND (cfg->'queue'->>'assignee'='unassigned' OR cfg->'queue'->>'assignee'=ANY(active_roles))) THEN RAISE EXCEPTION 'invalid configured holding destination'; END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind'='implementation') <> 1 THEN
        RAISE EXCEPTION 'exactly one implementation stage required'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF jsonb_typeof(s->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'terminal') IS DISTINCT FROM 'boolean' OR jsonb_typeof(s->'owners') IS DISTINCT FROM 'array' OR jsonb_typeof(s->'notify') IS DISTINCT FROM 'object' OR jsonb_typeof(s->'notify'->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'stage fields must have explicit types'; END IF;
        IF NOT s ?& ARRAY['name','label','owners','kind','gate','skip_to','signoff','terminal','notify']
           OR s->>'name' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(s->>'label') = ''
           OR s->>'kind' NOT IN ('draft','implementation','review','system')
           OR jsonb_typeof(s->'terminal') <> 'boolean' OR jsonb_typeof(s->'owners') <> 'array'
           OR NOT ARRAY(SELECT jsonb_array_elements_text(s->'owners')) <@ active_roles THEN
            RAISE EXCEPTION 'invalid stage/owner policy: %', s->>'name'; END IF;
        IF s->>'gate' IS NOT NULL AND (NOT coalesce(s->>'gate'=ANY(flag_names),false) OR cfg->'flags'->(s->>'gate')->>'kind' <> 'gate')
           OR s->>'signoff' IS NOT NULL AND (NOT coalesce(s->>'signoff'=ANY(flag_names),false) OR cfg->'flags'->(s->>'signoff')->>'kind' <> 'signoff')
           OR (s->>'gate' IS NULL) <> (s->>'skip_to' IS NULL) THEN
            RAISE EXCEPTION 'invalid gate/signoff'; END IF;
        IF s->>'kind'='implementation' AND (jsonb_array_length(s->'owners')=0 OR EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE (s->'owners') ? (x->>'name') AND x->>'kind'<>'implementer')) THEN
            RAISE EXCEPTION 'implementation owner must be implementer'; END IF;
        IF NOT (s->'notify') ?& ARRAY['kind','role'] OR s->'notify'->>'kind' NOT IN ('none','assignee','fixed_role','stage_owner_fallback')
           OR (s->'notify'->>'kind'='fixed_role' AND NOT coalesce(s->'notify'->>'role'=ANY(active_roles),false))
           OR (s->'notify'->>'kind'<>'fixed_role' AND s->'notify'->>'role' IS NOT NULL) THEN
            RAISE EXCEPTION 'notification routing or silence must be explicit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'target' IS NULL AND
          ((s->'notify'->>'kind'='fixed_role' AND x->>'name'=s->'notify'->>'role') OR
           (s->'notify'->>'kind' IN ('assignee','stage_owner_fallback') AND s->'owners' ? (x->>'name')))) THEN
            RAISE EXCEPTION 'notifying roles require a pane target'; END IF;
        visited := ARRAY[]::text[]; cursor_name := s->>'name';
        LOOP
            IF cursor_name=ANY(visited) OR NOT cursor_name=ANY(names) THEN RAISE EXCEPTION 'gate skip cycle or missing stage'; END IF;
            visited := visited || cursor_name;
            SELECT x->>'skip_to' INTO cursor_name FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cursor_name;
            EXIT WHEN cursor_name IS NULL;
        END LOOP;
    END LOOP;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        IF EXISTS (SELECT FROM unnest(ARRAY['from','to','action','label','primitive']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'string') OR EXISTS (SELECT FROM unnest(ARRAY['owner_scoped','require_commit','require_reason','allow_no_code']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'boolean') OR jsonb_typeof(tr->'clear_signoffs') IS DISTINCT FROM 'array' OR jsonb_typeof(tr->'actors') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'transition fields must have explicit types'; END IF;
        IF NOT tr ?& ARRAY['from','to','action','label','primitive','actors','owner_scoped','require_commit','require_reason','clear_signoffs']
           OR NOT tr->>'from'=ANY(names) OR NOT tr->>'to'=ANY(names) OR tr->>'from'=tr->>'to'
           OR tr->>'action' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(tr->>'label')=''
           OR tr->>'primitive' NOT IN ('move','approve','return','reopen')
           OR jsonb_typeof(tr->'actors') <> 'array' OR jsonb_array_length(tr->'actors')=0
           OR NOT ARRAY(SELECT jsonb_array_elements_text(tr->'actors')) <@ active_roles
           OR jsonb_typeof(tr->'owner_scoped') <> 'boolean'
           OR jsonb_typeof(tr->'require_commit') <> 'boolean'
           OR jsonb_typeof(tr->'require_reason') <> 'boolean' THEN RAISE EXCEPTION 'invalid transition policy'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from';
        IF (s->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
        IF tr->>'primitive'='approve' AND s->>'signoff' IS NULL THEN RAISE EXCEPTION 'approval requires signoff'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'to';
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation' THEN RAISE EXCEPTION 'return must target implementation'; END IF;
        IF (tr->>'allow_no_code')::boolean AND ((tr->>'require_commit')::boolean OR NOT (tr->>'require_reason')::boolean OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='implementation')) THEN RAISE EXCEPTION 'invalid no-code transition policy'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements_text(tr->'clear_signoffs') x
            WHERE cfg->'flags'->x->>'kind' IS DISTINCT FROM 'signoff') THEN RAISE EXCEPTION 'invalid signoff reset'; END IF;
        -- SYRD-214: a transition may record a decision its actor did not make,
        -- for a role that reached its verdict off the board. Absent means the
        -- ordinary case: the actor's own decision, which is every transition
        -- that predates relaying.
        --
        -- SYRD-217 widened this from "returns only". Refusing to relay the
        -- User's acceptance did not stop the acceptance being acted on; it
        -- pushed it into a narrated override, where the destination is
        -- whatever the controller types and no gate is checked at all. The
        -- fence that replaces it is tighter where it matters: a relay must
        -- mirror a move the relayed role can itself make out of this stage --
        -- same primitive, same destination, same sign-off clearing -- so it is
        -- that role's own step under another hand, never a route the role does
        -- not have. An approval additionally names what it accepts and may not
        -- be the move that ends the ticket.
        IF jsonb_typeof(tr->'relays_decision_of') IS NOT NULL
           AND jsonb_typeof(tr->'relays_decision_of') <> 'null' THEN
            IF jsonb_typeof(tr->'relays_decision_of') <> 'string'
               OR NOT coalesce(tr->>'relays_decision_of'=ANY(role_names),false)
               OR tr->>'primitive' NOT IN ('return','approve')
               OR NOT (tr->>'require_reason')::boolean
               OR tr->'actors' ? (tr->>'relays_decision_of')
               OR (tr->>'owner_scoped')::boolean
               OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of'))
               OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of')
                     AND (m->>'to' IS DISTINCT FROM tr->>'to'
                          OR ticket_board.signoff_reset_key(m) IS DISTINCT FROM ticket_board.signoff_reset_key(tr)))
               -- An approval is relayed only for a role with no pane of its
               -- own -- a reviewer that can sign off for itself must -- it
               -- names what it accepts, and it may not end the ticket.
               -- Approvals only: applied to returns this would retroactively
               -- invalidate what pgu952 legitimately produced (SYRD-217).
               OR (tr->>'primitive'='approve'
                   AND ((SELECT pane->>'target' FROM jsonb_array_elements(cfg->'roles') pane
                          WHERE pane->>'name'=tr->>'relays_decision_of') IS NOT NULL
                        OR NOT (tr->>'require_commit')::boolean
                        OR coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
                                      WHERE x->>'name'=tr->>'to'),false))) THEN
                RAISE EXCEPTION 'invalid relayed decision policy: %', tr->>'action'; END IF;
        END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'from',x->>'to',x->>'action' FROM jsonb_array_elements(cfg->'transitions') x
        GROUP BY 1,2,3 HAVING count(*)>1) THEN RAISE EXCEPTION 'duplicate transition'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF NOT (s->>'terminal')::boolean THEN
            IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=s->>'name')
              OR (s->>'kind'<>'draft' AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'to'=s->>'name')) THEN
                RAISE EXCEPTION 'stage lacks entry or exit: %', s->>'name'; END IF;
            IF NOT EXISTS (
                WITH RECURSIVE reachable(n) AS (
                    SELECT s->>'name' UNION
                    SELECT x->>'to' FROM reachable q, jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=q.n
                ) SELECT FROM reachable q, jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=q.n AND (x->>'terminal')::boolean
            ) THEN RAISE EXCEPTION 'stage cannot reach a terminal: %', s->>'name'; END IF;
        END IF;
    END LOOP;
    -- Queue, defer, cancel and reopen are transitions, and their names belong to
    -- the tenant, so the floor is the shape they must leave behind rather than a
    -- list of actions to look for: a director that cannot leave a stage has lost
    -- control of every ticket sitting in it, whatever the document calls the
    -- move (SYRD-82).
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to move work out of every stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director'));
    END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to reopen every terminal stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director'));
    END IF;
    -- The half of the floor that is a prohibition. An `approve` transition is
    -- what writes its source stage's sign-off flag, so listing the director
    -- among its actors is how a document would hand the controller the power to
    -- approve the work it directs.
    --
    -- SYRD-217 narrows this by exactly one case, and that is a weakening of
    -- SYRD-82's floor, said plainly. A relayed approval writes somebody else's
    -- sign-off rather than the director's, and the relay fence above has
    -- already required it to mirror a move that role can make from that stage,
    -- to name the commit it accepts, to carry its reason, and to be relayable
    -- only for a role with no way to act for itself. The director still holds
    -- no approval of its own. What this cannot do is make the director
    -- truthful -- but nor could the narrated override it replaces, which
    -- named no commit and checked no gate.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
        WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
          AND x->>'relays_decision_of' IS NULL) THEN
        RAISE EXCEPTION 'director must not be granted sign-off authority: %',
            (SELECT string_agg(x->>'action', ', ' ORDER BY x->>'action')
             FROM jsonb_array_elements(cfg->'transitions') x
             WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
               AND x->>'relays_decision_of' IS NULL);
    END IF;

    -- Somewhere to put work down, and a way to pick it back up. `cancel` leaves
    -- every stage, so a floor that only asked whether the director could leave
    -- was satisfied by a document that could only end work, never park it
    -- (SYRD-92).
    IF NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean
          AND jsonb_array_length(coalesce(x->'owners','[]'::jsonb))=0
          AND x->'notify'->>'kind'='none'
    ) THEN
        RAISE EXCEPTION 'workflow must keep a stage where deferred work can wait';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') held
        WHERE NOT (held->>'terminal')::boolean
          AND NOT (jsonb_array_length(coalesce(held->'owners','[]'::jsonb))=0
                   AND held->'notify'->>'kind'='none')
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=held->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))=0
                AND dest->'notify'->>'kind'='none')
    ) THEN
        RAISE EXCEPTION 'director must be able to defer work out of every active stage';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') parked
        WHERE NOT (parked->>'terminal')::boolean
          AND jsonb_array_length(coalesce(parked->'owners','[]'::jsonb))=0
          AND parked->'notify'->>'kind'='none'
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=parked->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))>0)
    ) THEN
        RAISE EXCEPTION 'deferred work must have an ordinary way back';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.apply_declared_workflow(cfg jsonb)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE r jsonb; s jsonb; tr jsonb; rev bigint; pos int:=0; old_cfg jsonb; item record;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'only director may configure workflow' USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    IF nullif(current_setting('ticket_board.project',true),'') IS NULL OR cfg->>'project' IS DISTINCT FROM current_setting('ticket_board.project',true) THEN
        RAISE EXCEPTION 'workflow must match selected project identity'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') runtime_role WHERE runtime_role->>'target' IS NOT NULL AND runtime_role->>'target' IS DISTINCT FROM (cfg->>'project')||'-'||(runtime_role->>'name')||':0.0') THEN
        RAISE EXCEPTION 'foreign project/role runtime target'; END IF;
    PERFORM ticket_board.validate_declared_workflow(cfg);
    LOCK TABLE ticket_board.tickets, ticket_board.workflow_stages, ticket_board.workflow_transitions IN SHARE ROW EXCLUSIVE MODE;
    old_cfg := ticket_board.declared_workflow();
    IF old_cfg IS NOT NULL AND old_cfg->>'project' IS DISTINCT FROM cfg->>'project' THEN RAISE EXCEPTION 'cannot change workflow tenant identity'; END IF;
    IF old_cfg=cfg THEN SELECT revision INTO rev FROM ticket_board.workflow_configuration; RETURN rev; END IF;
    -- Stage removal is deliberate, empty, and cannot erase outstanding review.
    IF jsonb_typeof(cfg->'remove_stages') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'explicit remove_stages list required'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws WHERE NOT EXISTS
        (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name)
        AND NOT cfg->'remove_stages' ? ws.name) THEN
        RAISE EXCEPTION 'configuration omits an existing stage; preserve it or explicitly request removal'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws CROSS JOIN ticket_board.tickets t
        JOIN ticket_board.workflow_stages current_stage ON current_stage.name=t.state
        WHERE ws.exit_signoff_field IS NOT NULL AND NOT current_stage.is_terminal
        AND (ws.entry_gate_field IS NULL OR ticket_board.workflow_flag(to_jsonb(t),ws.entry_gate_field,coalesce(old_cfg,cfg)))
        AND NOT ticket_board.workflow_flag(to_jsonb(t),ws.exit_signoff_field,coalesce(old_cfg,cfg))
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name
          AND x->>'signoff'=ws.exit_signoff_field AND x->>'gate' IS NOT DISTINCT FROM ws.entry_gate_field)) THEN
        RAISE EXCEPTION 'configuration would remove an outstanding review obligation'; END IF;
    -- Whole-graph application must carry existing stages and attribution identities.
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state)) THEN
        RAISE EXCEPTION 'configuration omits an occupied stage; preserve live/history state'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=t.assignee)) THEN
        RAISE EXCEPTION 'configuration omits an assigned role; retain it inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=r.name)) THEN
        RAISE EXCEPTION 'retain historical roles as inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_state ns
        WHERE ns.awaiting_role<>'' AND ns.awaiting_since_at>clock_timestamp()-interval '4 hours'
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') awaited WHERE awaited->>'name'=ns.awaiting_role
            AND (awaited->>'active')::boolean AND awaited->>'target' IS NOT NULL)) THEN
        RAISE EXCEPTION 'role has an unresolved handoff; clear or retarget it before retirement'; END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        INSERT INTO ticket_board.workflow_roles(name,definition) VALUES(r->>'name',r)
        ON CONFLICT(name) DO UPDATE SET definition=EXCLUDED.definition;
    END LOOP;
    -- Referenced identities are not removed, including historical comment actors.
    ALTER TABLE ticket_board.workflow_stages DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_state_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_assignee_check;
    ALTER TABLE ticket_board.ticket_notification_queue DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
    -- Empty notification-role sentinels stay supported; array and sentinel refs are validated below.
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='ticket_board.tickets'::regclass AND conname='tickets_workflow_state_fk') THEN
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_state_fk FOREIGN KEY(state) REFERENCES ticket_board.workflow_stages(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_role_fk FOREIGN KEY(assignee) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.ticket_notification_queue ADD CONSTRAINT notification_workflow_role_fk FOREIGN KEY(target_role) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    -- Only the reviewed whole-graph operation can replace stage/transition data.
    DELETE FROM ticket_board.workflow_transitions;
    UPDATE ticket_board.workflow_stages SET gate_skip_to=NULL;
    DELETE FROM ticket_board.workflow_stages ws WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name);
    -- Move ranks out of the desired range before writing the final contiguous order.
    UPDATE ticket_board.workflow_stages SET rank=rank+(SELECT coalesce(max(rank),0)+jsonb_array_length(cfg->'stages')+1 FROM ticket_board.workflow_stages);
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        INSERT INTO ticket_board.workflow_stages(name,display_label,rank,owner_roles,entry_gate_field,gate_skip_to,exit_signoff_field,is_terminal)
        VALUES(s->>'name',s->>'label',pos,ARRAY(SELECT jsonb_array_elements_text(s->'owners')),s->>'gate',NULL,s->>'signoff',(s->>'terminal')::boolean)
        ON CONFLICT(name) DO UPDATE SET display_label=EXCLUDED.display_label,rank=EXCLUDED.rank,owner_roles=EXCLUDED.owner_roles,
            entry_gate_field=EXCLUDED.entry_gate_field,gate_skip_to=NULL,exit_signoff_field=EXCLUDED.exit_signoff_field,is_terminal=EXCLUDED.is_terminal;
        pos:=pos+1;
    END LOOP;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        UPDATE ticket_board.workflow_stages SET gate_skip_to=s->>'skip_to' WHERE name=s->>'name';
    END LOOP;
    DELETE FROM ticket_board.workflow_transitions;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        INSERT INTO ticket_board.workflow_transitions(from_stage,to_stage,action_name,allowed_roles,owner_scoped,director_override)
        VALUES(tr->>'from',tr->>'to',tr->>'action',ARRAY(SELECT jsonb_array_elements_text(tr->'actors')),(tr->>'owner_scoped')::boolean,false);
    END LOOP;
    INSERT INTO ticket_board.workflow_revisions(actor,document) VALUES(ticket_board.current_app_actor(),cfg) RETURNING revision INTO rev;
    INSERT INTO ticket_board.workflow_configuration(singleton,revision,document) VALUES(true,rev,cfg)
      ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,document=EXCLUDED.document;
    FOR item IN SELECT * FROM jsonb_each_text(coalesce(cfg->'reassign','{}'::jsonb)) LOOP
        IF NOT EXISTS (SELECT FROM ticket_board.workflow_stages WHERE name=item.key AND item.value=ANY(owner_roles)) THEN
            RAISE EXCEPTION 'invalid deliberate reassignment'; END IF;
        UPDATE ticket_board.tickets SET assignee=item.value WHERE state=item.key AND assignee<>item.value;
    END LOOP;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_stages ws ON ws.name=t.state
       WHERE cardinality(ws.owner_roles)>0 AND t.assignee<>'unassigned' AND NOT t.assignee=ANY(ws.owner_roles)) THEN
        RAISE EXCEPTION 'configuration would orphan live ticket ownership; supply deliberate reassignment'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_roles r ON r.name=t.assignee
        JOIN ticket_board.workflow_stages ws ON ws.name=t.state WHERE NOT ws.is_terminal AND NOT (r.definition->>'active')::boolean AND t.assignee<>'unassigned') THEN
        RAISE EXCEPTION 'inactive role still owns active tickets'; END IF;
    PERFORM pg_notify('ticket_board_state_transition',jsonb_build_object('kind','workflow','revision',rev)::text);
    RETURN rev;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_flag(t jsonb, flag text, cfg jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce((t->>flag)::boolean,(t->'workflow_flags'->>flag)::boolean,(cfg->'flags'->flag->>'default')::boolean,false);
$$;
CREATE OR REPLACE FUNCTION ticket_board.set_workflow_flag(t jsonb, flag text, value boolean)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN t ? flag THEN jsonb_set(t,ARRAY[flag],to_jsonb(value))
    ELSE jsonb_set(t,'{workflow_flags}',coalesce(t->'workflow_flags','{}'::jsonb)||jsonb_build_object(flag,value)) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true);
    -- Whose transition this is. Normally the caller's own; on a recovery the
    -- executor names the OWNER here, because the step being taken has to be one
    -- the owner could have taken and this trigger asks exactly that question
    -- (SYRD-133). Set transaction-locally by the executor immediately before the
    -- update, in the same way `workflow_action` already is, and cleared after.
    actor text:=coalesce(
        nullif(current_setting('ticket_board.workflow_actor',true),''),
        ticket_board.current_app_actor()); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
    queued_for text; reserved_by text;
BEGIN
    IF actor IS NULL THEN RAISE EXCEPTION 'configured ticket writes require a registered actor' USING ERRCODE='42501'; END IF;
    -- A narrated override moves a ticket the declared workflow will not: that
    -- is what it is for, and the transition table cannot describe it without
    -- describing its own bypass. Only ticket_board.force_move sets this, it is
    -- transaction-local, and reaching it already required the control role's
    -- capabilities. The legacy trigger has always had this escape; the
    -- declarative one did not, so the operation was refused here even after the
    -- capability layer let it through (SYRD-78).
    --
    -- It does not license a sign-off. An override that raised one would be
    -- manufacturing the review it exists to route around, so a sign-off flag
    -- that moves under it is refused rather than written.
    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a forced move cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;

    -- Merge closes its own source, and only its own source. The window is one
    -- ticket, named by the operation itself, and one statement: it is opened
    -- immediately before the close and shut immediately after, so nothing else
    -- in the transaction can travel through it. The destination has to be a
    -- stage the workflow declares terminal, and no sign-off may move -- a merge
    -- that raised one would be manufacturing the review the target still owes
    -- (SYRD-79).
    IF nullif(current_setting('ticket_board.merge_source', true), '') = previous.id THEN
        IF NOT coalesce((
            SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
            WHERE x->>'name' = proposed.state
        ), false) THEN
            RAISE EXCEPTION 'merge may only close its source into a terminal stage, not %', proposed.state
                USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a merge cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- A Director edit changes fields the transition table has nothing to say
    -- about, at whatever stage the ticket is in. The window names one ticket
    -- and is open for one statement. It carries no sign-off: ticket_board
    -- .director_edit, the only operation that opens this window, refuses to
    -- raise one before it writes anything (SYRD-83).
    IF nullif(current_setting('ticket_board.director_edit_target', true), '') = previous.id THEN
        RETURN proposed;
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director' THEN
            RAISE EXCEPTION 'only director may reassign without transition' USING ERRCODE='42501'; END IF;
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND (flag.value->>'kind'='signoff' OR actor<>'director') THEN
                RAISE EXCEPTION 'flag change requires authorized workflow action'; END IF;
        END LOOP;
        -- SYRD-77: an owner change that is not a transition never reached the
        -- serial-focus redirect below, so a same-stage reassignment could leave
        -- one implementer holding two implementation tickets -- exactly the
        -- reservation the queue exists to protect. Resolving it here rather
        -- than in the caller means no write path can hand an implementer
        -- reserved work by declining to ask.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_stage_kind(proposed.state)='implementation'
               AND ticket_board.ticket_is_implementer_assignee(proposed.assignee)
               AND NOT proposed.manually_controlled
               AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL THEN
                IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN
                    RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
                queued_for:=proposed.assignee;
                reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
                proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
                PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
                proposed.queued_for_assignee:=queued_for;
                proposed.queued_behind_ticket:=coalesce(reserved_by,'');
                PERFORM set_config('ticket_board.serial_focus_queued',
                    jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                        'stage',proposed.state,'assignee',proposed.assignee)::text,true);
            ELSE
                -- A deliberate new owner ends the hold that named the old one,
                -- so the marker never outlives the reservation that set it.
                proposed.queued_for_assignee:='';
                proposed.queued_behind_ticket:='';
            END IF;
        END IF;
        RETURN proposed;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
            RAISE EXCEPTION 'flags cannot be patched alongside a transition'; END IF;
    END LOOP;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x
      WHERE x->>'from'=previous.state AND x->>'to'=proposed.state AND x->>'action'=action_name;
    IF tr IS NULL OR NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
        RAISE EXCEPTION 'unauthorized configured transition: % / %',actor,action_name USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=previous.state;
    IF (source_stage->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
    -- A blocker stops work going FORWARD. Putting work down is not going
    -- forward: a parking stage is not terminal, owns nobody and notifies
    -- nobody, so nothing is promoted by landing there and the blocker is
    -- exactly the reason to park. Refusing it forced the director to clear the
    -- blocker, defer, and restore it -- three writes, a window where the ticket
    -- looked unblocked, and a blocked ticket holding an implementer's serial
    -- slot in the meantime (SYRD-192).
    --
    -- Keyed on the DESTINATION's shape rather than on an action name, because
    -- the name is the tenant's: `declared_parking_stage` is the same predicate
    -- `workflow_config.parking_stage_names` uses, so a tenant that calls it
    -- something other than `backlog` gets this for free.
    IF tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    IF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
    -- A sign-off gates leaving a review FORWARD. Parking is not a verdict: the
    -- stage owns nobody, notifies nobody and promotes nothing, and the review's
    -- record rides along untouched -- no sign-off is granted, none is cleared,
    -- the commit stays. Requiring the missing sign-off made an unaccepted User
    -- Review impossible to put down without deciding it (SYRD-263). Keyed on
    -- the destination's shape, like the blocker exemption above (SYRD-192).
    ELSIF source_stage->>'signoff' IS NOT NULL AND tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    IF tr->>'primitive' IN ('return','reopen') THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
    IF (tr->>'allow_no_code')::boolean THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',true); END IF;
    -- A sign-off is a review of one commit (SYRD-271). MEFP-4 was approved for
    -- one commit, routed back by a plain `move` that clears nothing, and
    -- resubmitted with another -- and the audit stage, already signed, was
    -- skipped: the old verdict stood in for a review nobody had done. So when
    -- work leaves implementation carrying a different commit, every review
    -- sign-off is cleared here, before the gates are walked, and it enters
    -- review for exactly the commit it carries.
    -- The same commit resubmitted keeps its review: that exact work was seen.
    -- A no-code submission has no commit to show it is the same work, so it
    -- is always reviewed as new.
    IF source_stage->>'kind' = 'implementation'
       AND ((tr->>'allow_no_code')::boolean
            OR (btrim(coalesce(doc->>'commit_hash','')) <> ''
                AND (doc->>'commit_hash') IS DISTINCT FROM previous.commit_hash)) THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind' = 'signoff' THEN
                doc:=ticket_board.set_workflow_flag(doc,flag.key,false);
            END IF;
        END LOOP;
    END IF;
    target:=proposed.state;
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        EXIT WHEN (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners) THEN proposed.assignee:=owners[1]; END IF;
    IF previous.manually_controlled AND tr->>'primitive'='approve' THEN
        -- Record the decision but retain the deliberate stage/owner hold. The
        -- executor creates the durable handoff after activity triggers finish.
        PERFORM set_config('ticket_board.held_review_target',coalesce(nullif(ticket_board.transition_target_role(target,proposed.assignee),actor),'director'),true);
        -- And keep the transition the approval earned, instead of discarding it
        -- with the stage. A hold defers a decision; releasing it replays this
        -- exact step, as this actor, once (SYRD-180).
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(previous.id,action_name,actor,tr->>'to')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
        target:=previous.state; proposed.assignee:=previous.assignee;
    ELSE
        -- A move that actually happens leaves nothing owed, including the move
        -- that pays a deferral: nothing may be replayed twice.
        DELETE FROM ticket_board.ticket_deferred_review WHERE ticket_id=previous.id;
    END IF;
    doc:=doc||jsonb_build_object('state',target,'assignee',proposed.assignee);
    proposed:=jsonb_populate_record(proposed,doc);
    PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
    IF dest->>'kind'='implementation' AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
        IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
        queued_for:=proposed.assignee;
        reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
        proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        -- Serial focus still holds the ticket, but the outcome is now legible.
        -- The queue destination is frequently the stage the ticket came from,
        -- so without these the caller sees an unchanged ticket and a success.
        proposed.queued_for_assignee:=queued_for;
        proposed.queued_behind_ticket:=coalesce(reserved_by,'');
        PERFORM set_config('ticket_board.serial_focus_queued',
            jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                'stage',proposed.state,'assignee',proposed.assignee)::text,true);
    ELSE
        -- Any move that is not held clears the marker, so it never outlives the
        -- reservation that caused it.
        proposed.queued_for_assignee:='';
        proposed.queued_behind_ticket:='';
    END IF;
    -- Deferring is putting work down, so it has to stop looking like work
    -- somebody has. An owner left on a parked ticket keeps its implementer's
    -- serial reservation and keeps the board highlighting it as current, which
    -- is how a deferral would quietly go on nudging the person who deferred it.
    -- Everything the ticket is -- content, hierarchy, blockers, gates,
    -- sign-offs, comments -- is untouched; only who holds it changes (SYRD-92).
    IF ticket_board.declared_parking_stage(proposed.state) THEN
        -- queued_for is set only by the serial-focus redirect just above, whose
        -- holding destination is frequently this same stage. That ticket is
        -- waiting on a busy implementer rather than being put down, and its
        -- queue bookkeeping is what makes the outcome legible, so it is left
        -- exactly as it was.
        IF queued_for IS NULL THEN
            proposed.assignee:='unassigned';
            proposed.parked:=true;
            proposed.queued_for_assignee:='';
            proposed.queued_behind_ticket:='';
        END IF;
    ELSIF ticket_board.declared_parking_stage(previous.state) THEN
        -- Reviving it: the hold ends with the stage that carried it.
        proposed.parked:=false;
    END IF;
    RETURN proposed;
END;
$$;

-- The executor, with the acting role and the narrating role named rather than
-- read from the session. `perform_workflow_action` passes the caller as both,
-- which is every ordinary transition. The recovery path passes the OWNER as the
-- actor -- because the transition has to be one the owner could have taken --
-- and the control role as the narrator, so the comment is in the name of
-- whoever authorized it rather than words put in the owner's mouth (SYRD-133).
--
-- Granted to nobody: it takes the actor as an argument, so reaching it would be
-- reaching past the actor check. Its two callers are SECURITY DEFINER functions
-- in this schema, and both establish who they are first.
CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action_as(
    p_actor text, p_narrator text, id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; tr jsonb; actor text:=p_actor; candidates int; handoff text; source_stage jsonb;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR actor IS NULL THEN RAISE EXCEPTION 'workflow action requires registered service actor' USING ERRCODE='42501'; END IF;
    SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=perform_workflow_action_as.id FOR UPDATE;
    SELECT count(*) INTO candidates FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF candidates<>1 THEN RAISE EXCEPTION 'unknown or ambiguous workflow action; specify target'; END IF;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND t.assignee<>actor) THEN
        RAISE EXCEPTION 'actor cannot perform workflow action' USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state;
    IF t.manually_controlled AND tr->>'primitive'='approve' AND ticket_board.workflow_flag(to_jsonb(t),source_stage->>'signoff',cfg) THEN
        -- Repeated decisions neither mint a new wait generation nor reopen a resolved one.
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
        RETURN;
    END IF;
    IF (tr->>'require_reason')::boolean AND btrim(coalesce(payload->>'text',payload->>'reason',''))='' THEN RAISE EXCEPTION 'reason required'; END IF;
    IF payload ? 'commit_hash' AND (payload->>'commit_hash') !~ '^[0-9a-fA-F]{7,40}$' THEN RAISE EXCEPTION 'invalid commit hash'; END IF;
    -- SYRD-217: an approval accepted on somebody else's word is held to more
    -- than that role's own sign-off would be. The stage's gate must actually
    -- be open, the acceptance must name the exact candidate already recorded
    -- -- naming a different one approves something nobody tested, and
    -- silently replacing it is worse -- and every other review this ticket is
    -- subject to must already have been given.
    --
    -- Stricter than the role's own sign-off, deliberately. The User signing
    -- off an unaudited ticket is their own mistake to make, in front of the
    -- work; the same thing relayed is a mistake nobody in the conversation is
    -- placed to catch.
    IF tr->>'relays_decision_of' IS NOT NULL AND tr->>'primitive'='approve' THEN
        IF source_stage->>'gate' IS NOT NULL
           AND NOT ticket_board.workflow_flag(to_jsonb(t),source_stage->>'gate',cfg) THEN
            RAISE EXCEPTION 'relayed approval requires this stage to be gated on: %', source_stage->>'gate'; END IF;
        IF t.commit_exempt AND btrim(coalesce(t.commit_hash,''))='' THEN
            -- Nothing was built, so there is nothing to name; naming one
            -- anyway would be accepting an artefact this ticket never had.
            IF payload ? 'commit_hash' THEN
                RAISE EXCEPTION 'relayed approval cannot name a commit on a commit-exempt ticket'; END IF;
        ELSIF btrim(coalesce(t.commit_hash,''))='' OR payload->>'commit_hash' IS DISTINCT FROM t.commit_hash THEN
            RAISE EXCEPTION 'relayed approval must name the recorded candidate commit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
                    WHERE x->>'name'<>t.state
                      AND x->>'signoff' IS NOT NULL AND x->>'gate' IS NOT NULL
                      AND ticket_board.workflow_flag(to_jsonb(t),x->>'gate',cfg)
                      AND NOT ticket_board.workflow_flag(to_jsonb(t),x->>'signoff',cfg)) THEN
            RAISE EXCEPTION 'relayed approval requires the earlier reviews this ticket is subject to'; END IF;
    END IF;
    IF btrim(coalesce(payload->>'text',payload->>'reason',''))<>'' THEN
        -- SYRD-214: a relayed decision says whose it was, in the record itself.
        --
        -- What the preamble adds is the one thing a reader cannot otherwise
        -- tell: that the decision came from somebody who does not operate the
        -- board, and that the role exercising the action is not claiming to
        -- have made it or to have done the review behind it. It names the
        -- actor rather than the narrator because the actor is whose authority
        -- the transition's actor list was checked against. (The two differ
        -- only on the SYRD-133 recovery path, which cannot reach a relay: its
        -- transitions are allow_no_code and so leave implementation, while a
        -- relay is a return and so enters it.) Composed here rather than left
        -- to whoever types the reason: provenance that depends on wording is
        -- provenance that goes missing the first time somebody is in a hurry.
        PERFORM ticket_board.append_ticket_comment(
            t.id,
            coalesce(p_narrator,actor),
            CASE
            WHEN tr->>'relays_decision_of' IS NULL THEN
                coalesce(payload->>'text',payload->>'reason')
            -- One E-string per branch: only the first of a run of adjacent
            -- literals takes the E prefix, so a continuation carrying \n or an
            -- escaped quote would be neither escaped nor balanced.
            WHEN tr->>'primitive'='approve' THEN
                -- A relayed approval IS the sign-off, so the record says so
                -- rather than disclaiming it. What it still will not say is
                -- that the relayer reviewed anything (SYRD-217).
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as %2$s sign-off, decided by %2$s and entered by %1$s, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            ELSE
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as the decision of %2$s, it is not %2$s sign-off, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            END);
    END IF;
    PERFORM set_config('ticket_board.held_review_target','',true);
    PERFORM set_config('ticket_board.workflow_action',action,true);
    PERFORM set_config('ticket_board.workflow_actor',actor,true);
    UPDATE ticket_board.tickets SET state=tr->>'to',
        assignee=coalesce(nullif(payload->>'assignee',''),assignee),
        commit_hash=coalesce(payload->>'commit_hash',commit_hash)
        WHERE tickets.id=t.id;
    PERFORM set_config('ticket_board.workflow_action','',true);
    PERFORM set_config('ticket_board.workflow_actor','',true);
    handoff:=nullif(current_setting('ticket_board.held_review_target',true),'');
    PERFORM set_config('ticket_board.held_review_target','',true);
    IF handoff IS NOT NULL THEN
        UPDATE ticket_board.ticket_notification_state SET awaiting_role=handoff, awaiting_since_at=clock_timestamp(),
            last_activity_at=clock_timestamp(), nudge_count=0 WHERE ticket_id=t.id;
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
    END IF;
END;
$$;

-- The public entry point: the caller acts as themselves and narrates as
-- themselves, which is every ordinary transition.
CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action(id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
BEGIN
    PERFORM ticket_board.perform_workflow_action_as(
        ticket_board.current_app_actor(), NULL, id, action, payload);
END;
$$;

--
-- SYRD-133: the recovery the Director skill promised and the document did not
-- grant.
--
-- On SYRD-131 Ops finished the work, said so in a comment, and left the ticket
-- in `in_progress/ops`. The owner's own no-code submission is `owner_scoped`, so
-- the Director could not take it for them, and `force_move` and `override_move`
-- were refused outright -- this tenant's document grants neither, so the
-- documented last resort did not exist. The Director had to narrate an override.
--
-- This is the bounded version of that move, and every limit is deliberate:
--
-- * it performs a DECLARED transition the owner could have taken, so it cannot
--   skip a gate, a sign-off or a blocker -- all of those are enforced by the
--   executor it calls, not reimplemented here;
-- * exactly one such transition must exist, because choosing between two would
--   be the board deciding what the owner meant;
-- * it requires a reason, which is recorded in the control role's own name;
-- * it advances to that transition's destination and no further, so the next
--   required gate is where the work lands and that gate's owner is notified by
--   the ordinary transition notification -- once.
--
CREATE OR REPLACE FUNCTION ticket_board.recover_stalled_ticket(
    p_ticket text,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
    reason text := btrim(coalesce(p_reason, ''));
    cfg jsonb := ticket_board.declared_workflow();
    t ticket_board.tickets%ROWTYPE;
    candidates integer;
    chosen text;
    pending text;
BEGIN
    IF cfg IS NULL THEN
        RAISE EXCEPTION 'recovery requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.require_actor(ARRAY[]::text[], 'recover_stalled_ticket');
    caller := ticket_board.current_app_actor();
    -- Control authority decides, derived from capabilities. A tenant that hands
    -- the capability to a second role still gets one recoverer, and no rule
    -- here spells the name `director` (SYRD-49).
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'only the control role may recover a stalled ticket, not %', caller
            USING ERRCODE = '42501';
    END IF;
    IF reason = '' THEN
        RAISE EXCEPTION 'recovering a stalled ticket requires a reason';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % not found', p_ticket;
    END IF;
    -- A completed review that a hold deferred is not a stalled owner. Recovery
    -- takes the one declared step the owner could have taken (SYRD-133); here
    -- that is the step they DID take, so pay it rather than asking the reviewer
    -- to sign off a second time -- and when it cannot be paid yet, refuse naming
    -- what is actually in the way, without consuming the approval. Sending the
    -- Director to route/reassign/defer was the advice SYRD-146 followed into a
    -- dead end (SYRD-180).
    pending := ticket_board.pending_held_review(t.id);
    IF pending IS NOT NULL THEN
        IF t.manually_controlled THEN
            RAISE EXCEPTION
                '%/% has already recorded %; it is waiting on its hold, not on its owner. '
                'Release the hold to finish it: set_manually_controlled(''%'', false)',
                t.state, t.assignee, pending, t.id
                USING ERRCODE = '42501';
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(t.id) THEN
            RAISE EXCEPTION
                '%/% has already recorded %; it is waiting on an unresolved blocker, not on its '
                'owner. Resolve the blocker and recovery finishes it', t.state, t.assignee, pending
                USING ERRCODE = '42501';
        END IF;
        IF ticket_board.reconcile_released_hold(t.id) IS NOT NULL THEN
            PERFORM ticket_board.append_ticket_comment(t.id, caller, reason);
            RETURN;
        END IF;
    END IF;
    SELECT count(*) INTO candidates
      FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = t.state
       AND coalesce((x->>'allow_no_code')::boolean, false)
       AND x->'actors' ? t.assignee;
    IF candidates = 0 THEN
        RAISE EXCEPTION
            'no no-code transition out of %/% is available to its owner, so there is nothing to '
            'recover: route, reassign or defer it instead', t.state, t.assignee
            USING ERRCODE = '42501';
    END IF;
    IF candidates > 1 THEN
        RAISE EXCEPTION
            '%/% offers % no-code transitions to its owner; recovery will not choose between them',
            t.state, t.assignee, candidates
            USING ERRCODE = '42501';
    END IF;
    SELECT x->>'action' INTO chosen
      FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = t.state
       AND coalesce((x->>'allow_no_code')::boolean, false)
       AND x->'actors' ? t.assignee;
    -- The owner's transition, authorized and narrated by the control role. Every
    -- gate the owner would have met is met here, because this is the same
    -- executor taking the same declared step.
    PERFORM ticket_board.perform_workflow_action_as(
        t.assignee, caller, t.id, chosen,
        jsonb_build_object('reason', reason));
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.perform_workflow_action_as(text, text, text, text, jsonb) FROM PUBLIC;



CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    blocker_id text;
    hardcoded_transition_allowed boolean;
    config_transition_allowed boolean;
    shadow_actor text;
    transition_check_state text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        RETURN ticket_board.enforce_declared_ticket_update(OLD,NEW);
    END IF;
    IF NEW.state = 'draft' THEN
        IF OLD.state IS DISTINCT FROM NEW.state THEN
            NEW.assignee := 'unassigned';
        END IF;
        NEW.parked := false;
    END IF;

    IF NEW.state <> 'backlog' OR OLD.state IN ('done', 'cancelled') THEN
        NEW.parked := false;
    END IF;

    IF current_setting('ticket_board.force_move', true) = 'on'
       OR nullif(current_setting('ticket_board.director_edit_target', true), '') = NEW.id THEN
        RETURN NEW;
    END IF;

    IF OLD.state = 'audit' AND NEW.state = 'analysis' THEN
        NEW.assignee := 'unassigned';
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'dat'
       AND NOT NEW.needs_user_signoff
       AND (OLD.needs_user_signoff OR OLD.state IS DISTINCT FROM NEW.state) THEN
        NEW.state := 'director_review';
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
        NEW.state := 'director_review';
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
    IF NOT NEW.needs_audit THEN
        NEW.audit_signoff := false;
    END IF;
    -- A sign-off is a review of one commit (SYRD-271), on this undeclared path
    -- as on the declared one: entering audit already signed skips it below.
    -- Different work leaving implementation is reviewed afresh. submit_to_audit
    -- here already clears Audit's and the Inspector's sign-offs, but never
    -- the User's, which skips user_review below just the same.
    IF OLD.state = 'in_progress' AND NEW.state IS DISTINCT FROM OLD.state
       AND (NEW.commit_hash IS DISTINCT FROM OLD.commit_hash OR NEW.commit_exempt IS DISTINCT FROM OLD.commit_exempt)
       AND (btrim(NEW.commit_hash) <> '' OR NEW.commit_exempt) THEN
        -- Carried from the old commit, not granted by this write.
        NEW.audit_signoff := NEW.audit_signoff AND NOT OLD.audit_signoff;
        NEW.inspector_signoff := NEW.inspector_signoff AND NOT OLD.inspector_signoff;
        NEW.user_signoff := NEW.user_signoff AND NOT OLD.user_signoff;
    END IF;
    IF OLD.state = 'in_progress' AND NEW.state = 'audit' AND NEW.needs_inspection AND NOT NEW.inspector_signoff THEN
        NEW.state := 'inspection';
        NEW.inspector_signoff := false;
    END IF;
    IF OLD.state = 'inspection' AND NEW.state = 'audit' AND NOT NEW.inspector_signoff THEN
        RAISE EXCEPTION 'inspector_signoff must be true before a ticket can enter audit from inspection';
    END IF;
    transition_check_state := NEW.state;
    IF OLD.state IN ('in_progress', 'inspection') AND NEW.state = 'audit' AND NOT NEW.needs_audit THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.audit_signoff := false;
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
        transition_check_state := NEW.state;
    END IF;
    IF NEW.state = 'audit' AND NEW.audit_signoff THEN
        NEW.state := CASE WHEN NEW.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
        NEW.assignee := 'director';
        transition_check_state := NEW.state;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;

    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);

    IF NEW.state = 'done'
       AND OLD.state IN ('backlog', 'analysis')
       AND current_setting('ticket_board.utility_task_complete', true) = 'on'
       AND NEW.commit_exempt THEN
        RETURN NEW;
    END IF;

    IF OLD.state IS DISTINCT FROM NEW.state THEN
        transition_check_state := coalesce(transition_check_state, NEW.state);
        hardcoded_transition_allowed := ticket_board.workflow_transition_allowed_hardcoded(OLD.state, transition_check_state);
        config_transition_allowed := ticket_board.workflow_transition_allowed_config(OLD.state, transition_check_state);
        shadow_actor := coalesce(nullif(current_setting('ticket_board.caller_role', true), ''), current_user);
        PERFORM ticket_board.log_workflow_transition_shadow_mismatch(
            NEW.id,
            OLD.state,
            transition_check_state,
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
       AND NEW.needs_audit
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
       AND NOT (coalesce(transition_check_state, NEW.state) = 'audit' AND NOT NEW.needs_audit)
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
    IF NEW.state = 'done'
       AND OLD.state <> 'done'
       AND NOT ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state) THEN
        RAISE EXCEPTION 'tickets can only enter done through a configured transition';
    END IF;
    IF OLD.state <> 'done' AND NEW.state = 'done' AND NOT NEW.commit_exempt AND btrim(NEW.commit_hash) = '' THEN
        RAISE EXCEPTION 'commit_hash is required before a ticket can enter done';
    END IF;

    RETURN NEW;
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
    caller_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        actor := ticket_board.current_actor_role();
        caller_role := ticket_board.current_app_actor();
        IF actor <> 'ticket_board_service' OR NOT EXISTS (
            SELECT FROM ticket_board.workflow_roles r
            WHERE r.name = caller_role
              AND (r.definition->>'active')::boolean
              AND (
                  r.definition->'capabilities' ? CASE p_action
                      WHEN 'set_awaiting_role' THEN 'await_role' ELSE p_action END
                  -- A narrated override is not an ordinary stage capability.
                  -- These two exist to move a ticket the declared workflow
                  -- will not, so a workflow that declared them as capabilities
                  -- would be declaring its own bypass -- and every project
                  -- would need a document migration before its Director could
                  -- use an operation the API has always advertised. The
                  -- authority is the control role, derived from the
                  -- capabilities that define one rather than from the name
                  -- 'director', so a project whose control role is called
                  -- something else is answered the same way (SYRD-49,
                  -- SYRD-78). Every other role is refused here, before any of
                  -- the operation's own safety checks run.
                  OR (
                      p_action = ANY (ticket_board.control_override_actions())
                      AND r.definition->'capabilities' ?& ticket_board.control_capabilities()
                  )
              )
        ) THEN
            RAISE EXCEPTION 'role % cannot call %', caller_role,p_action USING ERRCODE='42501';
        END IF;
        RETURN actor;
    END IF;
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_service is the only database writer', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    caller_role := nullif(current_setting('ticket_board.caller_role', true), '');
    IF caller_role IS NOT NULL
       AND cardinality(coalesce(p_allowed_roles, ARRAY[]::text[])) > 0
       AND caller_role <> ALL(p_allowed_roles)
       AND NOT (
           p_allowed_roles && ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]
           AND ticket_board.ticket_is_implementer_assignee(caller_role)
       ) THEN
        RAISE EXCEPTION 'role % cannot call %', caller_role, p_action
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
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        actor := nullif(current_setting('ticket_board.caller_role',true),'');
        IF actor IS NULL OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles WHERE name=actor AND (definition->>'active')::boolean) THEN
            RAISE EXCEPTION 'invalid configured caller role: %',actor USING ERRCODE='42501';
        END IF;
        RETURN actor;
    END IF;
    actor := nullif(current_setting('ticket_board.caller_role', true), '');
    IF actor IS NULL THEN
        RETURN ticket_board.current_actor_role();
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM (
            SELECT unnest(wt.allowed_roles) AS role
            FROM ticket_board.workflow_transitions wt
            UNION
            SELECT unnest(ws.owner_roles) AS role
            FROM ticket_board.workflow_stages ws
        ) AS valid_roles
        WHERE valid_roles.role = actor
    ) THEN
        RAISE EXCEPTION 'invalid ticket_board.caller_role: %', actor;
    END IF;
    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.legacy_transition_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN (
            SELECT CASE
                WHEN NULLIF(p_assignee, 'unassigned') = ANY(ws.owner_roles) THEN NULLIF(p_assignee, 'unassigned')
                WHEN cardinality(ws.owner_roles) = 1 THEN ws.owner_roles[1]
                ELSE NULL
            END
            FROM ticket_board.workflow_stages ws
            WHERE ws.name = 'audit'
        )
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'user_review' THEN NULL
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text,p_assignee text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); s jsonb; owners text[];
BEGIN
    IF cfg IS NULL THEN RETURN ticket_board.legacy_transition_target_role(p_state,p_assignee); END IF;
    SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=p_state;
    owners:=ARRAY(SELECT jsonb_array_elements_text(s->'owners'));
    RETURN CASE s->'notify'->>'kind'
      WHEN 'none' THEN NULL WHEN 'fixed_role' THEN s->'notify'->>'role'
      WHEN 'assignee' THEN CASE WHEN p_assignee=ANY(owners) THEN p_assignee END
      WHEN 'stage_owner_fallback' THEN CASE WHEN p_assignee=ANY(owners) THEN p_assignee ELSE owners[1] END
      ELSE NULL END;
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.apply_declared_workflow(cfg jsonb, expected_revision bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE actual bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    SELECT coalesce(max(revision),0) INTO actual FROM ticket_board.workflow_configuration;
    IF expected_revision IS DISTINCT FROM actual THEN RAISE EXCEPTION 'workflow revision changed; reread before applying'; END IF;
    RETURN ticket_board.apply_declared_workflow(cfg);
END;
$$;

-- SYRD-262: correct which process binds to a declared role, and nothing else.
--
-- A declared role names the runtime and pane target its process registers
-- with, and the board serves that role's assignment -- and grants the process
-- its authority -- only when the two agree. A legacy tenant's first
-- declaration copied an example's bindings, so MEFP's Codex Director and Ops
-- matched nothing: hidden, and the Director without authority. The Director
-- cannot repair that itself, and nobody may act as the Director to do it.
--
-- So this is not a workflow edit and not a Director write. It is root's, run
-- through the database owner's connection that deploys and migrations already
-- use, and it is not granted to any role: rbac.sql revokes every function from
-- PUBLIC and grants back an explicit list this is not on, so neither the board
-- service nor any pane can reach it. What it may do is fixed here, not by the
-- caller:
--   * the caller never supplies a document -- this builds the new one from
--     the live one, changing only runtime, target and slot of roles that
--     already exist, which cannot touch capabilities, owners, stages,
--     transitions or tickets;
--   * the live revision AND document must be exactly the ones reviewed;
--   * the new revision is attributed to the rebind, and the previous
--     document stays in workflow_revisions as its rollback evidence.
CREATE OR REPLACE FUNCTION ticket_board.rebind_declared_pane_roles(
    expected_revision bigint, expected_document jsonb, bindings jsonb, attribution text
) RETURNS bigint LANGUAGE plpgsql SET search_path=ticket_board,pg_temp AS $$
DECLARE
    current_revision bigint;
    current_document jsonb;
    rebound jsonb;
    binding record;
    runtime_value text;
    target_value text;
    slot_value jsonb;
    rev bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    SELECT revision, document INTO current_revision, current_document
      FROM ticket_board.workflow_configuration WHERE singleton;
    IF current_document IS NULL THEN
        RAISE EXCEPTION 'the board runs no declared workflow to rebind'; END IF;
    IF current_revision IS DISTINCT FROM expected_revision
       OR current_document IS DISTINCT FROM expected_document THEN
        RAISE EXCEPTION 'workflow changed since it was reviewed; reread before rebinding'; END IF;
    IF btrim(coalesce(attribution, '')) = '' THEN
        RAISE EXCEPTION 'a rebind must say what it repairs'; END IF;
    IF jsonb_typeof(bindings) IS DISTINCT FROM 'object' OR bindings = '{}'::jsonb THEN
        RAISE EXCEPTION 'a rebind names at least one role'; END IF;
    rebound := current_document;
    FOR binding IN SELECT key, value FROM jsonb_each(bindings) LOOP
        IF jsonb_typeof(binding.value) IS DISTINCT FROM 'object'
           OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(binding.value) k)
              IS DISTINCT FROM ARRAY['runtime', 'slot', 'target'] THEN
            RAISE EXCEPTION 'a rebind sets exactly runtime, target and slot: %', binding.key; END IF;
        IF NOT EXISTS (SELECT FROM jsonb_array_elements(current_document->'roles') r
                       WHERE r->>'name' = binding.key) THEN
            RAISE EXCEPTION 'no declared role %', binding.key; END IF;
        runtime_value := binding.value->>'runtime';
        target_value := binding.value->>'target';
        slot_value := binding.value->'slot';
        IF (runtime_value IS NULL) <> (target_value IS NULL) THEN
            RAISE EXCEPTION 'runtime and target are set together: %', binding.key; END IF;
        IF runtime_value IS NOT NULL AND runtime_value NOT IN ('claude', 'codex', 'agy', 'hermes') THEN
            RAISE EXCEPTION 'unknown runtime % for %', runtime_value, binding.key; END IF;
        IF target_value IS NOT NULL AND (
            target_value !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'
            OR left(split_part(target_value, ':', 1), length(current_document->>'project') + 1)
               <> (current_document->>'project') || '-') THEN
            RAISE EXCEPTION 'target % is not a pane of this project', target_value; END IF;
        IF jsonb_typeof(slot_value) <> 'null' AND (
            jsonb_typeof(slot_value) <> 'number' OR slot_value::text !~ '^[0-5]$' OR target_value IS NULL) THEN
            RAISE EXCEPTION 'invalid visible slot for %', binding.key; END IF;
        rebound := jsonb_set(rebound, '{roles}', (
            SELECT jsonb_agg(CASE WHEN r->>'name' = binding.key
                                  THEN r || jsonb_build_object('runtime', binding.value->'runtime',
                                                               'target', binding.value->'target',
                                                               'slot', slot_value)
                                  ELSE r END ORDER BY ordinal)
              FROM jsonb_array_elements(rebound->'roles') WITH ORDINALITY AS e(r, ordinal)));
    END LOOP;
    IF EXISTS (SELECT r->>'target' FROM jsonb_array_elements(rebound->'roles') r
               WHERE (r->>'active')::boolean AND r->>'target' IS NOT NULL
               GROUP BY r->>'target' HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'active pane targets must be unique'; END IF;
    IF EXISTS (SELECT r->'slot' FROM jsonb_array_elements(rebound->'roles') r
               WHERE jsonb_typeof(r->'slot') = 'number'
               GROUP BY r->'slot' HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'visible slots must be unique'; END IF;
    IF rebound = current_document THEN
        RETURN current_revision;
    END IF;
    INSERT INTO ticket_board.workflow_revisions(actor, document)
        VALUES('rebind: ' || btrim(attribution), rebound) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = rebound WHERE singleton;
    UPDATE ticket_board.workflow_roles w SET definition = r
      FROM jsonb_array_elements(rebound->'roles') r
     WHERE w.name = r->>'name' AND bindings ? w.name;
    RETURN rev;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.rebind_declared_pane_roles(bigint, jsonb, jsonb, text) FROM PUBLIC;


-- Stages that hold work nobody is doing: not finished, nobody owns them, and
-- they notify nobody. Named by shape and not by label, which is the tenant's to
-- choose, and kept identical to workflow_config.parking_stage_names so the two
-- layers agree by construction rather than by both spelling 'backlog'
-- (SYRD-92).
CREATE OR REPLACE FUNCTION ticket_board.declared_parking_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce((SELECT NOT (x->>'terminal')::boolean
   AND jsonb_array_length(coalesce(x->'owners','[]'::jsonb))=0
   AND x->'notify'->>'kind'='none'
  FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
  WHERE x->>'name'=stage), false);
$$;

CREATE OR REPLACE FUNCTION ticket_board.declared_stage_kind(stage text)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT x->>'kind' FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x WHERE x->>'name'=stage;
$$;
CREATE OR REPLACE FUNCTION ticket_board.remember_declared_implementer()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ticket_board.declared_stage_kind(NEW.state)='implementation' THEN
   UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee=NEW.assignee WHERE ticket_id=NEW.id;
 END IF;
 RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_declared_implementer ON ticket_board.tickets;
CREATE TRIGGER tickets_z_declared_implementer AFTER INSERT OR UPDATE ON ticket_board.tickets
 FOR EACH ROW EXECUTE FUNCTION ticket_board.remember_declared_implementer();

CREATE OR REPLACE FUNCTION ticket_board.auto_advance_analysis_ticket()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN RETURN NULL; END IF;
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
        SET state = 'in_progress'
        WHERE id = NEW.id
          AND state = 'analysis';
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_implementer_and_activate_next()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    released_implementer text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN RETURN NULL; END IF;
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    IF NOT (
        NEW.state IN ('done', 'cancelled')
        OR (NEW.state = 'backlog' AND NEW.parked AND OLD.state IS DISTINCT FROM 'backlog')
    ) THEN
        RETURN NULL;
    END IF;

    released_implementer := ticket_board.ticket_reserved_implementer(OLD.id, OLD.state, OLD.assignee);
    IF released_implementer IS NULL THEN
        SELECT nullif(ns.last_implementer_assignee, '')
        INTO released_implementer
        FROM ticket_board.ticket_notification_state ns
        WHERE ns.ticket_id = NEW.id;
    END IF;

    IF ticket_board.ticket_is_implementer_assignee(released_implementer) THEN
        PERFORM ticket_board.activate_next_queued_ticket(released_implementer);
    END IF;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.legacy_transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_already_announced boolean
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
        WHEN p_new_state = 'in_progress' AND p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is active again'
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

-- SYRD-124: the declared-workflow branch used to drop `p_already_announced` on
-- the floor and say "entered" every time. The legacy branch has always drawn
-- the distinction -- "New ticket for you" the first time, "is active again"
-- afterwards -- and it matters most on a board that runs a declared workflow,
-- because that is where a role is told a second time about a stage it is
-- already working in. "Entered" is a claim that the ticket moved; on a repeat
-- it did not, and the owner spends a whole read of the ticket finding that out.
CREATE OR REPLACE FUNCTION ticket_board.transition_message(p_ticket_id text,p_title text,p_old_state text,p_new_state text,p_already_announced boolean)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_transition_message(p_ticket_id,p_title,p_old_state,p_new_state,p_already_announced)
 WHEN p_already_announced THEN p_ticket_id || ' -- ' || p_title || ' is active again in ' || coalesce((SELECT display_label FROM ticket_board.workflow_stages WHERE name=p_new_state),p_new_state)
 ELSE p_ticket_id || ' -- ' || p_title || ' entered ' || coalesce((SELECT display_label FROM ticket_board.workflow_stages WHERE name=p_new_state),p_new_state) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_target_role(p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NOT NULL THEN ticket_board.transition_target_role(p_state,p_assignee) ELSE CASE
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'dat' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'
        ELSE NULL
    END END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.nudge_message(p_ticket_id text, p_title text, p_state text, p_assignee text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NOT NULL THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is waiting in ' || p_state ELSE CASE
        WHEN p_state = 'analysis' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' needs director triage in analysis'
        WHEN p_state = 'backlog' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is assigned in backlog; triage or defer explicitly'
        WHEN p_state = 'in_progress' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' is still in progress'
        WHEN p_state = 'inspection' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for inspection'
        WHEN p_state = 'audit' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for audit'
        WHEN p_state = 'dat' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for DAT'
        WHEN p_state = 'director_review' THEN 'NUDGE ' || p_ticket_id || ' -- ' || p_title || ' ready for your review'
        ELSE NULL
    END END;
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
    WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
      AND NOT t.manually_controlled
      AND (ns.last_transition_notified_at IS NULL OR ns.last_transition_notified_at < ns.entered_current_state_at)
    ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_due_nudges(
    p_now timestamptz DEFAULT clock_timestamp(),
    p_cadence interval DEFAULT interval '30 minutes',
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
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    WHEN 'director_review' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              -- A durable capacity wait is not unattended work. While the
              -- reservation this ticket names still holds that implementer, the
              -- director cannot legally route it into that lane, so "advance it
              -- or hand it off" asks for something the board itself refuses. The
              -- backlog guard beside this one answers the same question for a
              -- ticket whose own assignee is the reserved implementer; a
              -- declarative queue destination is usually analysis/director, so
              -- only the durable fields know what it is waiting for (SYRD-109).
              AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                  t.id,
                  t.queued_for_assignee,
                  t.queued_behind_ticket
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                ns.last_nudged_at,
                ns.nudge_count,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
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
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND ns.entered_current_state_at <= p_now - p_cadence
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              -- A durable capacity wait is not unattended work. While the
              -- reservation this ticket names still holds that implementer, the
              -- director cannot legally route it into that lane, so "advance it
              -- or hand it off" asks for something the board itself refuses. The
              -- backlog guard beside this one answers the same question for a
              -- ticket whose own assignee is the reserved implementer; a
              -- declarative queue destination is usually analysis/director, so
              -- only the durable fields know what it is waiting for (SYRD-109).
              AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                  t.id,
                  t.queued_for_assignee,
                  t.queued_behind_ticket
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
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
            RAISE WARNING 'wedged-pane nudge backstop fired for % targeting % (%s since transition)',
                candidate.id,
                target_role,
                floor(extract(epoch FROM p_now - candidate.entered_current_state_at))::integer || 's';
        END IF;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
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

-- The 2-argument form is dropped rather than overloaded: a defaulted extra
-- argument would make every existing call ambiguous, and the listener is
-- updated in the same change (SYRD-163).
DROP FUNCTION IF EXISTS ticket_board.notify_idle_turn_end_nudges(jsonb, timestamptz);

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_turn_end_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz,
    p_active_grace interval,
    p_work_observed_at jsonb
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_turn_end_nudges');

    IF p_idle_since_by_role IS NULL OR p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.target_role)
            candidates.id,
            candidates.state,
            candidates.assignee,
            candidates.kind,
            candidates.owner_role,
            candidates.target_role,
            candidates.idle_reminder_count,
            candidates.idle_since_at
        FROM (
            SELECT
                notification_scope.id,
                notification_scope.state,
                notification_scope.assignee,
                notification_scope.ticket_number,
                notification_scope.entered_current_state_at,
                notification_scope.idle_reminder_count,
                notification_scope.owner_role,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'escalation'
                    ELSE 'idle_reminder'
                END AS kind,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN 'director'
                    ELSE notification_scope.owner_role
                END AS target_role,
                notification_scope.idle_since_at,
                CASE
                    WHEN notification_scope.owner_role <> 'director'
                         AND notification_scope.idle_reminder_count >= 1 THEN -1
                    WHEN notification_scope.state = 'analysis' THEN 0
                    WHEN notification_scope.state = 'inspection' THEN 2
                    WHEN notification_scope.state = 'in_progress' THEN 3
                    WHEN notification_scope.state = 'audit' THEN 4
                    WHEN notification_scope.state = 'dat' THEN 5
                    WHEN notification_scope.state = 'director_review' THEN 5
                    ELSE 9
                END AS priority,
                row_number() OVER (
                    PARTITION BY notification_scope.active_work_partition
                    ORDER BY sent.last_sent_at DESC NULLS LAST,
                        notification_scope.ticket_number DESC
                ) AS in_progress_rank
            FROM (
                SELECT
                    t.id,
                    t.state,
                    t.assignee,
                    t.ticket_number,
                    ns.entered_current_state_at,
                    ns.idle_reminder_count,
                    ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
                    CASE
                        WHEN t.state = 'in_progress'
                            THEN t.state || ':' || ticket_board.transition_target_role(t.state, t.assignee)
                        ELSE NULL
                    END AS active_work_partition,
                    (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz AS idle_since_at
                FROM ticket_board.tickets t
                JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
                WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
                  AND NOT t.manually_controlled
                  AND ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
                  AND p_idle_since_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
                  AND greatest(
                        ns.entered_current_state_at,
                        (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                      ) <= p_now
                  -- await-role means the owner has explicitly handed this off.
                  -- Do not tell that same owner they may be stuck while the wait is active.
                  AND NOT ticket_board.ticket_awaiting_role_is_active(
                      ns.awaiting_role,
                      ns.awaiting_since_at,
                      p_now
                  )
                  -- A durable capacity wait is not unattended work. While the
                  -- reservation this ticket names still holds that implementer, the
                  -- director cannot legally route it into that lane, so "advance it
                  -- or hand it off" asks for something the board itself refuses. The
                  -- backlog guard beside this one answers the same question for a
                  -- ticket whose own assignee is the reserved implementer; a
                  -- declarative queue destination is usually analysis/director, so
                  -- only the durable fields know what it is waiting for (SYRD-109).
                  AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                      t.id,
                      t.queued_for_assignee,
                      t.queued_behind_ticket
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_blockers tb
                      LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                      WHERE tb.ticket_id = t.id
                        AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM ticket_board.ticket_comments c
                      WHERE c.ticket_id = t.id
                        AND c.who = ticket_board.transition_target_role(t.state, t.assignee)
                        AND c.ts IS NOT NULL
                        AND c.ts >= ns.entered_current_state_at
                        AND c.ts >= ns.last_activity_at
                        AND c.ts >= (
                            (p_idle_since_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))::timestamptz
                            - interval '5 minutes'
                        )
                        AND c.ts <= p_now
                  )
            ) AS notification_scope
            LEFT JOIN LATERAL (
                SELECT max(trace.ts) AS last_sent_at
                FROM ticket_board.notification_trace trace
                WHERE trace.ticket_id = notification_scope.id
                  AND trace.target_role = notification_scope.owner_role
                  AND trace.kind = 'transition'
                  AND trace.event = 'send'
                  AND trace.ticket_state_at_event = notification_scope.state
            ) sent ON true
            -- A turn ending is not a stall. A review takes several turns, and
            -- between two of them the pane is idle by every measure this
            -- generator had: the hook says idle and the live gate agrees,
            -- because at that instant nothing is running. Audit was told it
            -- had not advanced SYRD-162 seconds after being handed it, and the
            -- next turn boundary escalated that to the Director as "may be
            -- stuck" -- inside a minute, while the review was happening
            -- (SYRD-163).
            --
            -- So the clock a reminder is measured against starts at the LAST
            -- OBSERVED WORK as well as at the stage and the idle hook, and the
            -- role has to have been quiet for the grace period before any of
            -- this is due. Same rule for every role: the stall generator
            -- beside this one has taken both inputs since SYRD-58, and the
            -- asymmetry is what let this through.
            WHERE greatest(
                    notification_scope.entered_current_state_at,
                    notification_scope.idle_since_at,
                    coalesce(
                        (p_work_observed_at ->> notification_scope.owner_role)::timestamptz,
                        '-infinity'::timestamptz
                    )
                  ) + p_active_grace <= p_now
              AND notification_scope.owner_role IS NOT NULL
        ) AS candidates
        WHERE (candidates.state <> 'in_progress' OR candidates.in_progress_rank = 1)
          AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = candidates.id
              AND q.target_role = candidates.target_role
              AND q.kind NOT IN ('idle_reminder', 'escalation', 'unresolved_turn')
        )
          -- "was reminded about X and still hasn't advanced it" has to be TRUE
          -- when it is said. The counter alone could not make it true: a
          -- reminder that was enqueued and never delivered still incremented
          -- it, and two turn boundaries a few seconds apart still reached it.
          -- So an escalation waits for a reminder that was actually SENT to
          -- that owner, in this stage, and for the grace period to pass
          -- afterwards -- which is the time the reminder was asking for
          -- (SYRD-163).
          AND (
            candidates.kind <> 'escalation'
            OR EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state reminded
                WHERE reminded.ticket_id = candidates.id
                  AND reminded.last_idle_reminder_at IS NOT NULL
                  AND reminded.last_idle_reminder_at + p_active_grace <= p_now
            )
        )
          -- One escalation per stall, not one per wave. The dedupe key holds
          -- only while the queue row does; once the director acknowledges it,
          -- the next wave finds the same idle owner and the same unmoved
          -- ticket and enqueues it again -- which on SYRD-131 it did, within a
          -- minute of delivering the first. This marker outlives the
          -- acknowledgement and re-arms when the stall's identity changes
          -- (SYRD-133).
          AND (
            candidates.kind <> 'escalation'
            OR NOT EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_state escalated
                WHERE escalated.ticket_id = candidates.id
                  AND escalated.idle_escalation_key = ticket_board.idle_escalation_identity(
                      candidates.id,
                      candidates.state,
                      candidates.assignee,
                      escalated.last_activity_at
                  )
            )
        )
        ORDER BY candidates.target_role, candidates.priority, candidates.ticket_number
    LOOP
        payload := jsonb_build_object(
            'kind', candidate.kind,
            'id', candidate.id,
            'state', candidate.state,
            'assignee', candidate.assignee,
            'owner_role', candidate.owner_role,
            'target_role', candidate.target_role,
            'message', CASE
                WHEN candidate.kind = 'escalation' THEN ticket_board.idle_without_advancing_escalation_message(
                    candidate.owner_role,
                    candidate.id,
                    candidate.state
                )
                WHEN candidate.owner_role = 'director' AND candidate.idle_reminder_count >= 1 THEN ticket_board.idle_without_advancing_problem_message(
                    candidate.id,
                    candidate.state,
                    'User'
                )
                WHEN candidate.owner_role = 'director' THEN ticket_board.idle_without_advancing_director_message(
                    candidate.id,
                    candidate.state
                )
                ELSE ticket_board.idle_without_advancing_message(candidate.id, candidate.state)
            END,
            'idle_since', candidate.idle_since_at
        );

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            candidate.kind,
            candidate.target_role,
            payload ->> 'message',
            payload,
            CASE
                WHEN candidate.kind = 'escalation' THEN
                    'idle-reminder-escalation:' || candidate.id || ':' || candidate.owner_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
                ELSE
                    'idle-reminder:' || candidate.id || ':' || candidate.target_role || ':' || extract(epoch FROM candidate.idle_since_at)::bigint::text
            END
        );
        IF candidate.kind = 'escalation' THEN
            -- Claimed in the same transaction that enqueues it, so two waves
            -- racing cannot both decide they are the first.
            UPDATE ticket_board.ticket_notification_state
               SET idle_escalation_key = ticket_board.idle_escalation_identity(
                       candidate.id, candidate.state, candidate.assignee,
                       ticket_notification_state.last_activity_at
                   )
             WHERE ticket_id = candidate.id;
        END IF;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_idle_stall_nudges(
    p_idle_since_by_role jsonb,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_grace interval DEFAULT interval '45 seconds',
    p_cadence interval DEFAULT interval '30 minutes',
    p_escalate_after integer DEFAULT 2,
    p_work_observed_at_by_role jsonb DEFAULT '{}'::jsonb
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    target_role text;
    payload jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_idle_stall_nudges');

    p_idle_since_by_role := coalesce(p_idle_since_by_role, '{}'::jsonb);
    p_work_observed_at_by_role := coalesce(p_work_observed_at_by_role, '{}'::jsonb);

    IF p_idle_since_by_role = '{}'::jsonb THEN
        RETURN 0;
    END IF;

    FOR candidate IN
        SELECT DISTINCT ON (candidates.owner_role)
            candidates.id,
            candidates.title,
            candidates.state,
            candidates.assignee,
            candidates.ticket_number,
            candidates.last_activity_at,
            candidates.entered_current_state_at,
            candidates.idle_since_at,
            candidates.last_nudged_at,
            candidates.nudge_count,
            candidates.work_observed_at,
            candidates.dedupe_key,
            candidates.owner_role,
            candidates.target_role
        FROM (
            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
                        THEN (p_work_observed_at_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                ticket_board.nudge_target_role(t.state, t.assignee) AS owner_role,
                'director' AS target_role,
                CASE t.state
                    WHEN 'inspection' THEN 2
                    WHEN 'audit' THEN 4
                    WHEN 'dat' THEN 5
                    ELSE 5
                END AS priority
            FROM ticket_board.tickets t
            JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE (CASE WHEN ticket_board.declared_workflow() IS NULL THEN t.state IN ('in_progress', 'inspection', 'audit', 'dat') ELSE ticket_board.transition_target_role(t.state,t.assignee) IS NOT NULL AND NOT (SELECT is_terminal FROM ticket_board.workflow_stages WHERE name=t.state) END)
              AND NOT t.manually_controlled
              AND ticket_board.nudge_target_role(t.state, t.assignee) IS NOT NULL
              AND p_idle_since_by_role ? ticket_board.nudge_target_role(t.state, t.assignee)
              AND greatest(
                    ns.entered_current_state_at,
                    (p_idle_since_by_role ->> ticket_board.nudge_target_role(t.state, t.assignee))::timestamptz
                  ) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              AND NOT ticket_board.ticket_awaiting_role_is_active(
                  ns.awaiting_role,
                  ns.awaiting_since_at,
                  p_now
              )
              -- A durable capacity wait is not unattended work. While the
              -- reservation this ticket names still holds that implementer, the
              -- director cannot legally route it into that lane, so "advance it
              -- or hand it off" asks for something the board itself refuses. The
              -- backlog guard beside this one answers the same question for a
              -- ticket whose own assignee is the reserved implementer; a
              -- declarative queue destination is usually analysis/director, so
              -- only the durable fields know what it is waiting for (SYRD-109).
              AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                  t.id,
                  t.queued_for_assignee,
                  t.queued_behind_ticket
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  ticket_board.nudge_target_role(t.state, t.assignee),
                  p_now,
                  p_cadence
              )
              AND NOT ticket_board.notification_delivery_in_backoff(
                  t.id,
                  'director',
                  p_now,
                  p_cadence
              )

            UNION ALL

            SELECT
                t.id,
                t.title,
                t.state,
                t.assignee,
                t.ticket_number,
                ns.last_activity_at,
                ns.entered_current_state_at,
                (p_idle_since_by_role ->> 'director')::timestamptz AS idle_since_at,
                ns.last_nudged_at,
                ns.nudge_count,
                CASE
                    WHEN p_work_observed_at_by_role ? 'director'
                        THEN (p_work_observed_at_by_role ->> 'director')::timestamptz
                    ELSE NULL
                END AS work_observed_at,
                'nudge:' || t.id || ':director' AS dedupe_key,
                'director' AS owner_role,
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
                        AND NOT t.manually_controlled
                        AND NOT ticket_board.ticket_can_auto_advance_analysis(
                            t.state,
                            t.assignee,
                            t.implementation,
                            t.manually_controlled,
                            t.id
                        )
                    )
                    OR (
                        t.state = 'backlog'
                        AND t.assignee <> 'unassigned'
                        AND NOT t.parked
                        AND NOT ticket_board.ticket_is_queued_for_reserved_implementer(
                            t.id,
                            t.state,
                            t.assignee,
                            t.parked,
                            t.manually_controlled
                        )
                    )
                )
              AND p_idle_since_by_role ? 'director'
              AND greatest(ns.entered_current_state_at, (p_idle_since_by_role ->> 'director')::timestamptz) <= p_now - p_grace
              AND (ns.last_nudged_at IS NULL OR ns.last_nudged_at <= p_now - p_cadence)
              AND NOT EXISTS (
                  SELECT 1
                  FROM ticket_board.ticket_blockers tb
                  LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
                  WHERE tb.ticket_id = t.id
                    AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
              )
              -- A durable capacity wait is not unattended work. While the
              -- reservation this ticket names still holds that implementer, the
              -- director cannot legally route it into that lane, so "advance it
              -- or hand it off" asks for something the board itself refuses. The
              -- backlog guard beside this one answers the same question for a
              -- ticket whose own assignee is the reserved implementer; a
              -- declarative queue destination is usually analysis/director, so
              -- only the durable fields know what it is waiting for (SYRD-109).
              AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
                  t.id,
                  t.queued_for_assignee,
                  t.queued_behind_ticket
              )
              AND NOT ticket_board.notification_delivery_in_backoff(t.id, 'director', p_now, p_cadence)
        ) AS candidates
        ORDER BY candidates.owner_role,
            candidates.priority,
            candidates.ticket_number
    LOOP
        target_role := candidate.target_role;
        IF candidate.nudge_count >= p_escalate_after
           AND candidate.last_nudged_at IS NOT NULL
           AND candidate.last_activity_at <= candidate.last_nudged_at
           AND coalesce(candidate.work_observed_at, '-infinity'::timestamptz) <= candidate.last_nudged_at THEN
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

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            (payload ->> 'kind'),
            target_role,
            (payload ->> 'message'),
            payload,
            CASE
                WHEN (payload ->> 'kind') = 'escalation' THEN 'escalation:' || candidate.id || ':' || target_role
                ELSE candidate.dedupe_key
            END
        );
        UPDATE ticket_board.ticket_notification_state
        SET last_nudged_at = p_now,
            nudge_count = CASE
                WHEN greatest(
                    candidate.last_activity_at,
                    coalesce(candidate.work_observed_at, '-infinity'::timestamptz)
                ) > coalesce(candidate.last_nudged_at, '-infinity'::timestamptz) THEN 1
                ELSE nudge_count + 1
            END
        WHERE ticket_id = candidate.id;
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_notification_roles()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ticket_board.declared_workflow() IS NOT NULL THEN
  IF NEW.awaiting_role<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.awaiting_role) THEN RAISE EXCEPTION 'unknown awaited role'; END IF;
  IF NEW.last_implementer_assignee<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.last_implementer_assignee AND definition->>'kind'='implementer') THEN RAISE EXCEPTION 'unknown implementation owner'; END IF;
 END IF;
 RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS notification_declared_roles ON ticket_board.ticket_notification_state;
CREATE TRIGGER notification_declared_roles BEFORE INSERT OR UPDATE ON ticket_board.ticket_notification_state
 FOR EACH ROW EXECUTE FUNCTION ticket_board.validate_declared_notification_roles();


CREATE OR REPLACE FUNCTION ticket_board.legacy_current_reserved_ticket(
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
              AND NOT (t.state = 'user_review' AND t.assignee = 'user')
          )
      )
    ORDER BY ns.entered_current_state_at, t.ticket_number
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_current_reserved_ticket(p_implementer text,p_excluding_ticket_id text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_current_reserved_ticket(p_implementer,p_excluding_ticket_id)
 ELSE (SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
 WHERE (p_excluding_ticket_id IS NULL OR t.id<>p_excluding_ticket_id) AND NOT t.manually_controlled
 AND ticket_board.declared_stage_kind(t.state) IN ('implementation','review')
 AND (CASE WHEN ticket_board.declared_stage_kind(t.state)='implementation' THEN t.assignee ELSE ns.last_implementer_assignee END)=p_implementer
 ORDER BY t.ticket_number LIMIT 1) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        IF ticket_board.declared_stage_kind(NEW.state)='draft' THEN NEW.assignee:=coalesce(ticket_board.stage_default_assignee(NEW.state),NEW.assignee); END IF;
        IF ticket_board.declared_stage_kind(NEW.state)='implementation' AND ticket_board.ticket_current_reserved_ticket(NEW.assignee,NEW.id) IS NOT NULL AND NOT NEW.manually_controlled THEN
            IF ticket_board.declared_workflow()->'queue' IS NULL OR ticket_board.declared_workflow()->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
            NEW.state:=ticket_board.declared_workflow()->'queue'->>'stage'; NEW.assignee:=ticket_board.declared_workflow()->'queue'->>'assignee';
        END IF;
        PERFORM ticket_board.require_stage_owner_assignee(NEW.state,NEW.assignee);
        RETURN NEW;
    END IF;
    IF NEW.state = 'draft' THEN
        NEW.assignee := coalesce(ticket_board.stage_default_assignee('draft'), 'unassigned');
        NEW.parked := false;
    END IF;
    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);
    IF NEW.state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(NEW.assignee)
       AND ticket_board.ticket_current_reserved_ticket(NEW.assignee, NEW.id) IS NOT NULL THEN
        NEW.state := 'backlog';
        NEW.parked := false;
    END IF;
    RETURN NEW;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.set_declared_flags(id text, patch jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; item record; doc jsonb;
BEGIN
 IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN RAISE EXCEPTION 'only director may set gates' USING ERRCODE='42501'; END IF;
 IF cfg IS NULL OR jsonb_typeof(patch) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'configured gates object required'; END IF;
 SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=set_declared_flags.id FOR UPDATE;
 doc:=to_jsonb(t);
 FOR item IN SELECT * FROM jsonb_each(patch) LOOP
  IF cfg->'flags'->item.key->>'kind' IS DISTINCT FROM 'gate' OR jsonb_typeof(item.value) IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'only declared boolean gates may be edited'; END IF;
  doc:=ticket_board.set_workflow_flag(doc,item.key,(item.value::text)::boolean);
 END LOOP;
 t:=jsonb_populate_record(t,doc);
 UPDATE ticket_board.tickets SET needs_inspection=t.needs_inspection,needs_audit=t.needs_audit,needs_user_signoff=t.needs_user_signoff,workflow_flags=t.workflow_flags WHERE tickets.id=t.id;
 PERFORM ticket_board.append_ticket_comment(t.id,'director','Updated workflow gates: '||patch::text);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.workflow_wait_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN stage IN ('in_progress','inspection','audit','user_review')
 ELSE EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s WHERE s->>'name'=stage AND NOT (s->>'terminal')::boolean AND s->>'kind'<>'draft') END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF NOT ticket_board.workflow_wait_stage(t.state)
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    -- SYRD-148: the same rule as the legacy definition above. This is the one
    -- a board with a declared workflow actually runs, and it overrides that
    -- one, so a guard added only there is a guard this board never reaches.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;
-- Which stall an escalation was about. Identity rather than a flag: the same
-- ticket can stall again, and a second stall deserves a second escalation
-- (SYRD-133).
--
-- Deliberately NOT the moment the owner's pane went idle, which was the first
-- version of this. That value is recomputed by the listener on every pass, so
-- any jitter in it -- a restart, a re-probe, a clock that moved -- reads as a
-- new stall and the escalation repeats, which is the defect this exists to
-- stop. What makes a stall a different stall is that something happened on the
-- ticket since the last one: a new stage, a new owner, or any activity at all.
CREATE OR REPLACE FUNCTION ticket_board.idle_escalation_identity(
    p_ticket text,
    p_state text,
    p_assignee text,
    p_last_activity timestamptz
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_ticket, '') || ':' || coalesce(p_state, '') || ':'
        || coalesce(p_assignee, '') || ':'
        || coalesce(extract(epoch FROM p_last_activity)::bigint::text, '');
$$;

--
-- SYRD-133: the dependency and the handoff, in one act.
--
-- On SYRD-131 Ops wrote that the next step was a privileged Director one and
-- stopped. The sentence was on the ticket; the wait was not. `awaiting_role`
-- stayed empty, so no durable handoff existed, and the work sat in
-- `in_progress/ops` with nobody holding it.
--
-- Two calls where one will do is how that happens: the comment is the part a
-- role naturally writes, and the wait is the part that is easy to mean and not
-- do. This is both, in one transaction -- either the ticket records the reason
-- AND the handoff, or it records neither.
--
-- It deliberately does not reassign. The work is still the requesting role's;
-- it is waiting on somebody, which is what `awaiting_role` says. When the wait
-- clears, the ticket is already where it belongs.
--
CREATE OR REPLACE FUNCTION ticket_board.request_dependency(
    p_ticket text,
    p_awaiting_role text,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    reason text := btrim(coalesce(p_reason, ''));
    normalized_role text := lower(btrim(coalesce(p_awaiting_role, '')));
BEGIN
    IF reason = '' THEN
        -- The reason is why this is recorded here rather than as a bare flag:
        -- the awaited role has to be able to act on it.
        RAISE EXCEPTION 'requesting a dependency requires a reason';
    END IF;
    IF normalized_role = '' THEN
        RAISE EXCEPTION 'requesting a dependency requires the role it waits on';
    END IF;
    -- The comment first, so a wait never exists without the sentence that
    -- explains it. One transaction, so neither can exist without the other.
    PERFORM ticket_board.add_comment(p_ticket, reason);
    PERFORM ticket_board.set_awaiting_role(p_ticket, normalized_role);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF NOT (CASE WHEN ticket_board.declared_workflow() IS NULL THEN normalized_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') ELSE EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=normalized_role AND (r.definition->>'active')::boolean AND r.definition->>'target' IS NOT NULL) END) THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- SYRD-148: an awaiting-role handoff is an actionable instruction to
    -- somebody else. A ticket whose own dependency has not resolved cannot
    -- give one: the named role would be told to act on work that cannot
    -- proceed, and after thirty minutes the Director would be told they had
    -- ignored it. Refused rather than accepted quietly, and the blockers are
    -- named so the caller knows what has to move first.
    IF ticket_board.ticket_has_unresolved_blockers(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents an awaiting-role handoff: %',
            ticket_board.unresolved_blocker_list(set_awaiting_role.id);
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND ticket_board.workflow_wait_stage(t.state)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND ticket_board.workflow_wait_stage(t.state);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;
CREATE OR REPLACE FUNCTION ticket_board.ticket_is_implementer_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_stages ws
        WHERE CASE WHEN ticket_board.declared_workflow() IS NULL THEN ws.name='in_progress' ELSE ticket_board.declared_stage_kind(ws.name)='implementation' END
          AND btrim(lower(coalesce(p_assignee, ''))) = ANY(ws.owner_roles)
    );
$$;
REVOKE ALL ON FUNCTION ticket_board.workflow_wait_stage(text),ticket_board.enqueue_awaiting_role_handoff(text) FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_service') THEN
 GRANT EXECUTE ON FUNCTION ticket_board.workflow_wait_stage(text) TO ticket_board_service;
 END IF;
 IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_listener') THEN
 GRANT EXECUTE ON FUNCTION ticket_board.workflow_wait_stage(text) TO ticket_board_listener;
 END IF;
END $$;

-- Legacy decisions reuse the existing durable wait schedule. The configured
-- executor owns its own held approval path; never emit a second handoff there.
CREATE OR REPLACE FUNCTION ticket_board.notify_held_review_completion(p_id text, p_stage text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=ticket_board,pg_temp AS $$
DECLARE t ticket_board.tickets; destination text; recipient text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN RETURN; END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR NOT t.manually_controlled OR t.state<>p_stage THEN RETURN; END IF;
    IF p_stage='inspection' AND t.inspector_signoff THEN
        destination:=CASE WHEN t.needs_audit THEN 'audit' WHEN t.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
    ELSIF p_stage='audit' AND t.audit_signoff THEN
        destination:=CASE WHEN t.needs_user_signoff THEN 'dat' ELSE 'director_review' END;
    ELSIF p_stage='user_review' AND t.user_signoff THEN
        destination:='director_review';
    ELSE RETURN;
    END IF;
    recipient:=coalesce(ticket_board.transition_target_role(destination,
        ticket_board.stage_entry_assignee(destination,t.assignee,t.id)),'director');
    -- Called after all ticket touches by the authenticated decision operation.
    -- Supersede any previous wait once for the new approval; repeats never call
    -- this helper, so ACK/clear/retarget cannot recreate an obsolete generation.
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role=recipient, awaiting_since_at=clock_timestamp(),
        last_activity_at=clock_timestamp(), nudge_count=0 WHERE ticket_id=p_id;
    PERFORM ticket_board.enqueue_awaiting_role_handoff(p_id);
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.notify_held_review_completion(text,text) FROM PUBLIC;

COMMIT;


--
-- SYRD-83: review provenance is the one thing a Director edit may not
-- manufacture, and ticket_board.director_edit refuses to raise one. A
-- table-level trigger enforcing the same rule for paths that never call that
-- operation is NOT installed here: it would arrive, on an upgrading tenant,
-- before the sign-off functions were refreshed to take its grant, and would
-- refuse every legitimate review until they were. This list is what the
-- operation checks against.
--
CREATE OR REPLACE FUNCTION ticket_board.signoff_fields()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['audit_signoff', 'inspector_signoff', 'user_signoff']::text[];
$$;

--
-- What a Director edit leaves behind: the reason, and every field it moved,
-- old and new. Append-only from the operation's point of view; nothing here
-- rewrites a row.
--
CREATE TABLE IF NOT EXISTS ticket_board.ticket_field_audit (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    actor text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    field text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    ts timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ticket_field_audit_ticket_idx
    ON ticket_board.ticket_field_audit (ticket_id, id);

--
-- Whether a role is the one controlling this project. Under a declarative
-- workflow that is the role holding the control capabilities, derived rather
-- than named, so a project whose control role is not called 'director' is
-- answered the same way (SYRD-49). Without one, the legacy name stands.
--
CREATE OR REPLACE FUNCTION ticket_board.role_controls_project(p_role text)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow();
BEGIN
    IF p_role IS NULL OR btrim(p_role) = '' THEN
        RETURN false;
    END IF;
    IF cfg IS NULL THEN
        RETURN p_role = 'director';
    END IF;
    RETURN EXISTS (
        SELECT FROM ticket_board.workflow_roles r
        WHERE r.name = p_role
          AND (r.definition->>'active')::boolean
          AND r.definition->'capabilities' ?& ARRAY['set_manually_controlled', 'merge']::text[]
    );
END;
$$;

--
-- SYRD-83: one Director-only generic edit. It may move any mutable field at any
-- stage and for any owner, and it records why. What it may not do is
-- manufacture a review: raising a sign-off is refused here, before anything is
-- written, for every field the workflow declares a sign-off.
--
-- Fields with their own invariants reuse them rather than going round them: an
-- assignee is checked by the same predicate the workflow uses, a state must be a
-- declared stage, a parent may not close a cycle, and moving a ticket into an
-- implementer's in-progress slot takes the same serial-focus redirect a routed
-- move would.
--
CREATE OR REPLACE FUNCTION ticket_board.director_edit(
    id text,
    patch jsonb,
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
    caller text;
    invalid_field text;
    current_ticket ticket_board.tickets%ROWTYPE;
    field text;
    old_doc jsonb;
    new_doc jsonb;
    target_state text;
    target_assignee text;
    normalized_parent text;
    seen text[];
    loops int := 0;
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call director_edit; ticket_board_service is the only database writer', actor
            USING ERRCODE = '42501';
    END IF;
    caller := ticket_board.current_app_actor();
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'role % cannot call director_edit', coalesce(caller, '<none>')
            USING ERRCODE = '42501';
    END IF;
    IF patch IS NULL OR jsonb_typeof(patch) <> 'object' THEN
        RAISE EXCEPTION 'director_edit patch must be an object';
    END IF;
    IF btrim(coalesce(reason, '')) = '' THEN
        RAISE EXCEPTION 'director_edit requires a reason';
    END IF;

    SELECT key INTO invalid_field
    FROM jsonb_object_keys(patch) AS key
    WHERE key NOT IN (
        'title','body','parent_id','implementation','audit_prompt','origin_project',
        'external_source_ref','blocked_reason','assignee','state','needs_audit',
        'needs_inspection','needs_user_signoff','commit_exempt','regression',
        'manually_controlled','queued_for_assignee','queued_behind_ticket',
        'audit_signoff','inspector_signoff','user_signoff'
    )
    ORDER BY key LIMIT 1;
    IF invalid_field IS NOT NULL THEN
        -- Identity and creation provenance are not editable by anybody.
        RAISE EXCEPTION 'director_edit cannot update: %', invalid_field;
    END IF;

    FOREACH field IN ARRAY ticket_board.signoff_fields() LOOP
        IF patch ? field AND coalesce((patch->>field)::boolean, false) THEN
            RAISE EXCEPTION
                'director_edit cannot create sign-off %; only its own sign-off action may', field
                USING ERRCODE = '42501';
        END IF;
    END LOOP;

    SELECT * INTO current_ticket FROM ticket_board.tickets WHERE tickets.id = director_edit.id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    old_doc := to_jsonb(current_ticket);

    IF patch ? 'assignee' THEN
        target_assignee := patch->>'assignee';
        IF NOT ticket_board.ticket_valid_assignee(target_assignee) THEN
            RAISE EXCEPTION 'invalid assignee: %', target_assignee;
        END IF;
    END IF;
    IF patch ? 'state' THEN
        target_state := patch->>'state';
        IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = target_state) THEN
            RAISE EXCEPTION 'invalid state: %', target_state;
        END IF;
    END IF;
    IF patch ? 'parent_id' THEN
        normalized_parent := nullif(upper(btrim(coalesce(patch->>'parent_id', ''))), '');
        IF normalized_parent = director_edit.id THEN
            RAISE EXCEPTION 'a ticket cannot be its own parent';
        END IF;
        IF normalized_parent IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM ticket_board.tickets WHERE tickets.id = normalized_parent) THEN
                RAISE EXCEPTION 'parent ticket not found: %', normalized_parent;
            END IF;
            seen := ARRAY[director_edit.id];
            WHILE normalized_parent IS NOT NULL LOOP
                loops := loops + 1;
                IF loops > 64 THEN RAISE EXCEPTION 'parent chain is too deep'; END IF;
                IF normalized_parent = ANY (seen) THEN
                    RAISE EXCEPTION 'parent change would create a cycle';
                END IF;
                seen := seen || normalized_parent;
                SELECT nullif(btrim(parent_id), '') INTO normalized_parent
                FROM ticket_board.tickets WHERE tickets.id = normalized_parent;
            END LOOP;
            normalized_parent := upper(btrim(patch->>'parent_id'));
        END IF;
    END IF;

    -- Serial focus is an invariant of moving into an implementer's slot, not a
    -- property of the operation that moved it there.
    IF target_state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(coalesce(target_assignee, current_ticket.assignee))
       AND ticket_board.ticket_current_reserved_ticket(
               coalesce(target_assignee, current_ticket.assignee), director_edit.id) IS NOT NULL THEN
        target_state := 'backlog';
    END IF;

    -- Its own window, named for this ticket and open for this statement only.
    -- Deliberately not the narrated-move one: a Director edit should not
    -- inherit everything a forced move may bypass, and the sign-off boundary
    -- is enforced independently of either.
    PERFORM set_config('ticket_board.director_edit_target', director_edit.id, true);
    UPDATE ticket_board.tickets SET
        title = CASE WHEN patch ? 'title' THEN patch->>'title' ELSE title END,
        body = CASE WHEN patch ? 'body' THEN patch->>'body' ELSE body END,
        parent_id = CASE WHEN patch ? 'parent_id' THEN coalesce(normalized_parent, '') ELSE parent_id END,
        implementation = CASE WHEN patch ? 'implementation' THEN patch->>'implementation' ELSE implementation END,
        audit_prompt = CASE WHEN patch ? 'audit_prompt' THEN patch->>'audit_prompt' ELSE audit_prompt END,
        origin_project = CASE WHEN patch ? 'origin_project' THEN patch->>'origin_project' ELSE origin_project END,
        external_source_ref = CASE WHEN patch ? 'external_source_ref' THEN patch->>'external_source_ref' ELSE external_source_ref END,
        blocked_reason = CASE WHEN patch ? 'blocked_reason' THEN patch->>'blocked_reason' ELSE blocked_reason END,
        queued_for_assignee = CASE WHEN patch ? 'queued_for_assignee' THEN patch->>'queued_for_assignee' ELSE queued_for_assignee END,
        queued_behind_ticket = CASE WHEN patch ? 'queued_behind_ticket' THEN patch->>'queued_behind_ticket' ELSE queued_behind_ticket END,
        assignee = coalesce(target_assignee, assignee),
        state = coalesce(target_state, state),
        needs_audit = CASE WHEN patch ? 'needs_audit' THEN (patch->>'needs_audit')::boolean ELSE needs_audit END,
        needs_inspection = CASE WHEN patch ? 'needs_inspection' THEN (patch->>'needs_inspection')::boolean ELSE needs_inspection END,
        needs_user_signoff = CASE WHEN patch ? 'needs_user_signoff' THEN (patch->>'needs_user_signoff')::boolean ELSE needs_user_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
        manually_controlled = CASE WHEN patch ? 'manually_controlled' THEN (patch->>'manually_controlled')::boolean ELSE manually_controlled END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN false ELSE audit_signoff END,
        inspector_signoff = CASE WHEN patch ? 'inspector_signoff' THEN false ELSE inspector_signoff END,
        user_signoff = CASE WHEN patch ? 'user_signoff' THEN false ELSE user_signoff END
    WHERE tickets.id = director_edit.id;
    PERFORM set_config('ticket_board.director_edit_target', '', true);

    SELECT to_jsonb(tickets) INTO new_doc FROM ticket_board.tickets WHERE tickets.id = director_edit.id;
    FOR field IN SELECT jsonb_object_keys(patch) LOOP
        IF old_doc->field IS DISTINCT FROM new_doc->field THEN
            INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
            VALUES (director_edit.id, caller, btrim(reason), field, old_doc->field, new_doc->field);
        END IF;
    END LOOP;
    PERFORM ticket_board.append_ticket_comment(
        director_edit.id, caller,
        'Director edit: ' || btrim(reason)
    );
    PERFORM ticket_board.touch_ticket(director_edit.id);
END;
$$;

--
-- SYRD-93: publication is a handoff, not a credential.
--
-- Every role runs as one Unix account now (SYRD-69), so "let the role push" and
-- "let every role push" are the same sentence. The way out is to stop treating
-- publication as something an implementer does with a credential and start
-- treating it as something an implementer ASKS FOR, durably, on the board: the
-- request is a row, it survives a crash on either side, and the only thing that
-- can act on it is the process the board already knows is the control role.
--
-- Nothing here pushes anything. This is the record and the routing; the
-- credential lives behind a boundary the shared account cannot cross, and the
-- program that holds it reads the row rather than its caller's arguments.
--
-- A publication ask is a delivered notification like any other, so the queue
-- has to be allowed to carry one. Written as a replace-the-constraint step, the
-- way the awaiting_role kind was added, so it lands the same on a fresh install
-- and on a board that already has a queue full of rows.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder',
                    'awaiting_role', 'publication', 'triage'));

CREATE TABLE IF NOT EXISTS ticket_board.publication_requests (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    requested_by text NOT NULL,
    ref text NOT NULL,
    commit_hash text NOT NULL CHECK (commit_hash ~ '^[0-9a-f]{40}$'),
    bundle_path text NOT NULL CHECK (bundle_path LIKE '/%'),
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested', 'published', 'rejected', 'superseded')),
    detail text NOT NULL DEFAULT '',
    decided_by text NOT NULL DEFAULT '',
    -- What the board itself saw before it accepted a published verdict, not
    -- what the deciding role said it had done (SYRD-118). Empty on every other
    -- outcome, because a rejection needs no push to be true.
    verified_commit text NOT NULL DEFAULT ''
        CHECK (verified_commit = '' OR verified_commit ~ '^[0-9a-f]{40}$'),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz
);
CREATE INDEX IF NOT EXISTS publication_requests_ticket_idx
    ON ticket_board.publication_requests (ticket_id, id);
-- One open ask per role per ticket. A second one would leave the Director
-- choosing between two claims about the same work, which is exactly the
-- ambiguity a durable handoff exists to remove.
CREATE UNIQUE INDEX IF NOT EXISTS publication_requests_one_open_idx
    ON ticket_board.publication_requests (ticket_id, requested_by)
    WHERE state = 'requested';

--
-- Refs a role may ask to publish: its own namespace, nothing that integrates.
-- Returned rather than raised so both the request path and the helper can ask
-- the same question and phrase their own refusal.
--
-- Two namespaces are accepted, for a reason that is not cosmetic. `<role>/...`
-- is what the project already publishes, and it works for every role whose name
-- is not also an integration branch. One is: the implementer called `main`
-- cannot publish `main/anything`, because git stores refs as paths and
-- refs/heads/main and refs/heads/main/x cannot both exist. `roles/<role>/...`
-- always works, so no role is left without a way to publish its own work.
--
CREATE OR REPLACE FUNCTION ticket_board.publication_ref_problem(p_ref text, p_role text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    name text := btrim(coalesce(p_ref, ''));
    role_name text := lower(btrim(coalesce(p_role, '')));
    segments text[];
    segment text;
    protected text[] := ARRAY['main', 'master', 'trunk', 'release', 'head'];
BEGIN
    IF name = '' THEN
        RETURN 'a ref name is required';
    END IF;
    IF name LIKE 'refs/%' THEN
        RETURN 'pass a branch name, not a full ref path';
    END IF;
    segments := string_to_array(name, '/');
    FOREACH segment IN ARRAY segments LOOP
        IF segment !~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' OR segment LIKE '%.lock' THEN
            RETURN format('%s is not a valid branch name', name);
        END IF;
    END LOOP;
    -- The whole name, and the directory it would create. refs/heads/main/x
    -- cannot exist beside refs/heads/main, so a first segment that names an
    -- integration branch is refused whether or not it is also a role name.
    IF lower(name) = ANY (protected) OR lower(segments[1]) = ANY (protected) THEN
        RETURN format(
            '%s collides with an integration branch; roles publish their own feature refs '
            'and the control role integrates them. Use roles/%s/<name>.', name, role_name);
    END IF;
    IF segments[1] = 'roles' THEN
        IF array_length(segments, 1) < 3 OR segments[2] <> role_name THEN
            RETURN format('%s may only publish refs under roles/%s/, not %s', role_name, role_name, name);
        END IF;
        RETURN NULL;
    END IF;
    IF array_length(segments, 1) < 2 OR segments[1] <> role_name THEN
        RETURN format(
            '%s may only publish refs under %s/ or roles/%s/, not %s',
            role_name, role_name, role_name, name);
    END IF;
    RETURN NULL;
END;
$$;

--
-- Which role integrates. Derived from the capabilities that define control
-- rather than the name 'director', so a project whose control role is called
-- something else routes the same way (SYRD-49).
--
CREATE OR REPLACE FUNCTION ticket_board.publication_control_role()
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    found text;
BEGIN
    SELECT r.name INTO found FROM ticket_board.workflow_roles r
     WHERE (r.definition->>'active')::boolean
       AND r.definition->'capabilities' ?& ticket_board.control_capabilities()
     -- Deterministic when a document gives more than one role control
     -- authority: the one that also declares the operation is the one that can
     -- act on what it is told about.
     ORDER BY (r.definition->'capabilities' ? 'resolve_publication') DESC, r.name
     LIMIT 1;
    -- NULL, not 'director'. A stored document that declares no controller is a
    -- broken document, and routing an ask to a familiar name would hand
    -- publication authority to whoever holds that name (SYRD-93).
    RETURN found;
END;
$$;

--
-- The wait the Director sees, written here rather than through set_awaiting_role
-- so asking for publication does not also require the capability to park a
-- ticket on another role.
--
CREATE OR REPLACE FUNCTION ticket_board.publication_await_control(
    p_ticket text,
    p_rearm boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    control_role text := ticket_board.publication_control_role();
    already boolean;
BEGIN
    IF control_role IS NULL THEN
        RAISE EXCEPTION 'this workflow declares no role with control authority'
            USING ERRCODE = '42501';
    END IF;
    SELECT ns.awaiting_role = control_role AND ns.awaiting_since_at IS NOT NULL
      INTO already
      FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = p_ticket;
    IF NOT coalesce(already, false) THEN
        UPDATE ticket_board.ticket_notification_state
           SET awaiting_role = control_role,
               awaiting_since_at = clock_timestamp(),
               last_activity_at = clock_timestamp(),
               nudge_count = 0
         WHERE ticket_id = p_ticket;
    ELSIF p_rearm THEN
        UPDATE ticket_board.ticket_notification_state
           SET awaiting_notified_since_at = NULL
         WHERE ticket_id = p_ticket;
    END IF;
    PERFORM ticket_board.enqueue_awaiting_role_handoff(p_ticket);
END;
$$;

--
-- An implementer asks. This is the only half of publication an implementer can
-- reach, and it needs no credential at all: a missing key, an unreachable
-- remote or a Director who is asleep cannot stop the commit being made or the
-- ask being recorded.
--
CREATE OR REPLACE FUNCTION ticket_board.request_publication(
    p_ticket text,
    p_ref text,
    p_commit text,
    p_bundle text
)
RETURNS ticket_board.publication_requests
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller text;
    ticket ticket_board.tickets%ROWTYPE;
    problem text;
    normalized_commit text := lower(btrim(coalesce(p_commit, '')));
    normalized_ref text := btrim(coalesce(p_ref, ''));
    normalized_bundle text := btrim(coalesce(p_bundle, ''));
    existing ticket_board.publication_requests%ROWTYPE;
    created ticket_board.publication_requests%ROWTYPE;
    control_role text;
BEGIN
    -- Admission is the declared capability and nothing else. A board with no
    -- declared workflow has no capabilities to check, and admitting by role
    -- name instead would be exactly the reusable-contract violation this
    -- operation exists to avoid -- so it is refused, not guessed at.
    IF ticket_board.declared_workflow() IS NULL THEN
        RAISE EXCEPTION 'publication requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    actor := ticket_board.require_actor(ARRAY[]::text[], 'request_publication');
    caller := ticket_board.current_app_actor();
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    -- The work being published is the work this role holds. Anything else is a
    -- role asking to publish somebody else's ticket.
    IF ticket.assignee IS DISTINCT FROM caller THEN
        RAISE EXCEPTION '% may only request publication for its own ticket; % is assigned to %',
            caller, ticket.id, ticket.assignee USING ERRCODE = '42501';
    END IF;
    problem := ticket_board.publication_ref_problem(normalized_ref, caller);
    IF problem IS NOT NULL THEN
        RAISE EXCEPTION '%', problem USING ERRCODE = '42501';
    END IF;
    IF normalized_commit !~ '^[0-9a-f]{40}$' THEN
        RAISE EXCEPTION 'publication needs the full 40-character commit, not %', p_commit;
    END IF;
    IF normalized_bundle !~ '^/[^\0]+$' OR normalized_bundle LIKE '%/../%'
       OR normalized_bundle LIKE '%/..' OR normalized_bundle LIKE '%/' THEN
        RAISE EXCEPTION 'bundle must be an absolute path to a file, not %', p_bundle;
    END IF;

    SELECT * INTO existing FROM ticket_board.publication_requests
     WHERE ticket_id = ticket.id AND requested_by = caller AND state = 'requested'
     FOR UPDATE;
    IF FOUND THEN
        IF existing.ref = normalized_ref
           AND existing.commit_hash = normalized_commit
           AND existing.bundle_path = normalized_bundle THEN
            -- The same ask again: a retry after an interruption, or a lost
            -- notification. Re-arm the handoff, record nothing new.
            PERFORM ticket_board.publication_await_control(ticket.id, true);
            RETURN existing;
        END IF;
        -- The role amended its work. Both asks stay on the record, and the one
        -- the Director sees is the current one.
        UPDATE ticket_board.publication_requests
           SET state = 'superseded',
               decided_at = clock_timestamp(),
               decided_by = caller,
               detail = format('superseded by a later request for %s at %s',
                               normalized_ref, left(normalized_commit, 12))
         WHERE id = existing.id;
    END IF;

    INSERT INTO ticket_board.publication_requests
        (ticket_id, requested_by, ref, commit_hash, bundle_path)
    VALUES (ticket.id, caller, normalized_ref, normalized_commit, normalized_bundle)
    RETURNING * INTO created;

    control_role := ticket_board.publication_control_role();
    IF control_role IS NULL THEN
        RAISE EXCEPTION
            'this workflow declares no role with control authority, so no publication could be answered'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.publication_await_control(ticket.id, false);
    PERFORM ticket_board.enqueue_notification(
        ticket.id,
        'publication',
        control_role,
        format('%s -- %s: %s asks to publish %s at %s. Read the ticket, then publish or reject with a reason.',
               ticket.id, ticket.title, caller, normalized_ref, left(normalized_commit, 12)),
        jsonb_build_object(
            'kind', 'publication_request', 'id', ticket.id, 'request_id', created.id,
            'role', caller, 'ref', normalized_ref, 'commit', normalized_commit),
        format('publication:%s:%s', created.id, 'requested'));
    PERFORM ticket_board.add_comment(
        ticket.id,
        format('Publication requested: %s at %s. Awaiting the control role; no submission until it lands.',
               normalized_ref, left(normalized_commit, 12)));
    RETURN created;
END;
$$;

--
-- Who may answer an ask, asked on its own so the board can settle that question
-- before it does any work on the caller's behalf (SYRD-118). Proving a
-- publication means running git; a caller with no authority to decide anything
-- should be refused for that reason, and told that, rather than handed the
-- result of a check that was never theirs to trigger.
--
CREATE OR REPLACE FUNCTION ticket_board.require_publication_control()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        RAISE EXCEPTION 'publication requires a declared workflow; this board has none'
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.require_actor(ARRAY[]::text[], 'resolve_publication');
    caller := ticket_board.current_app_actor();
    -- The capability admits the caller; control authority is what decides. A
    -- tenant that hands this capability to a second role still gets one
    -- publisher.
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'only the control role may resolve a publication request, not %', caller
            USING ERRCODE = '42501';
    END IF;
    RETURN caller;
END;
$$;

--
-- The control role records what happened. It does not move the ticket: the
-- implementer submits its own work, and a publication that submitted for them
-- would be the control role signing the implementer's name.
--
-- A published verdict is not taken on anybody's word (SYRD-118). Request 17 was
-- recorded published while its ref was on neither the remote nor the trusted
-- commit cache, so the implementer was told to submit a commit this same board
-- would then refuse. The board resolves the ref through the trusted publication
-- path first and hands what it found in as p_verified_commit; this function
-- refuses the verdict unless that is exactly the commit the implementer asked
-- for, and stores it, so the record says what was seen rather than what was
-- claimed. Nothing about rejection changes: a refusal needs no push to be true.
--
CREATE OR REPLACE FUNCTION ticket_board.resolve_publication(
    p_request bigint,
    p_outcome text,
    p_detail text,
    p_verified_commit text DEFAULT ''
)
RETURNS ticket_board.publication_requests
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    caller text;
    request ticket_board.publication_requests%ROWTYPE;
    ticket ticket_board.tickets%ROWTYPE;
    outcome text := lower(btrim(coalesce(p_outcome, '')));
    note text := btrim(coalesce(p_detail, ''));
    proof text := lower(btrim(coalesce(p_verified_commit, '')));
    updated ticket_board.publication_requests%ROWTYPE;
BEGIN
    caller := ticket_board.require_publication_control();
    IF outcome NOT IN ('published', 'rejected') THEN
        RAISE EXCEPTION 'publication outcome must be published or rejected, not %', p_outcome;
    END IF;
    IF outcome = 'rejected' AND note = '' THEN
        RAISE EXCEPTION 'rejecting a publication request requires a reason';
    END IF;
    SELECT * INTO request FROM ticket_board.publication_requests
     WHERE id = p_request FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'publication request % not found', p_request;
    END IF;
    IF request.state = outcome THEN
        -- Already recorded. A helper that pushed and then lost its connection
        -- re-runs safely rather than reporting a failure it did not have.
        RETURN request;
    END IF;
    IF request.state <> 'requested' THEN
        RAISE EXCEPTION 'publication request % is already %', request.id, request.state;
    END IF;
    -- The one thing a control role may not decide on its own authority.
    IF outcome = 'published' AND proof <> request.commit_hash THEN
        RAISE EXCEPTION
            'publication request % cannot be recorded published: % has to be proven at %, %',
            request.id, request.ref, request.commit_hash,
            CASE WHEN proof = '' THEN 'and the board proved nothing'
                 ELSE format('and the board proved %s instead', proof) END
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = request.ticket_id FOR UPDATE;

    UPDATE ticket_board.publication_requests
       SET state = outcome,
           detail = note,
           decided_by = caller,
           verified_commit = CASE WHEN outcome = 'published' THEN proof ELSE '' END,
           decided_at = clock_timestamp()
     WHERE id = request.id
    RETURNING * INTO updated;

    UPDATE ticket_board.ticket_notification_state
       SET awaiting_role = '',
           awaiting_since_at = NULL,
           last_activity_at = clock_timestamp(),
           nudge_count = 0
     WHERE ticket_id = request.ticket_id;

    PERFORM ticket_board.enqueue_notification(
        request.ticket_id,
        'publication',
        request.requested_by,
        CASE WHEN outcome = 'published'
             THEN format('%s -- %s: %s is published at %s. Submit when you are ready.',
                         request.ticket_id, ticket.title, request.ref,
                         left(request.commit_hash, 12))
             ELSE format('%s -- %s: publication of %s was rejected: %s',
                         request.ticket_id, ticket.title, request.ref, note)
        END,
        jsonb_build_object(
            'kind', 'publication_' || outcome, 'id', request.ticket_id,
            'request_id', request.id, 'ref', request.ref,
            'commit', request.commit_hash, 'detail', note),
        format('publication:%s:%s', request.id, outcome));
    PERFORM ticket_board.add_comment(
        request.ticket_id,
        CASE WHEN outcome = 'published'
             THEN format('Published %s at %s.%s', request.ref, left(request.commit_hash, 12),
                         CASE WHEN note = '' THEN '' ELSE ' ' || note END)
             ELSE format('Publication of %s refused: %s', request.ref, note)
        END);
    PERFORM ticket_board.touch_ticket(request.ticket_id);
    RETURN updated;
END;
$$;

-- SYRD-109: a queued ticket must not be silenced for good.
--
-- The reminder suppression above is a live predicate, so ordinary reminders
-- resume by themselves the moment the named reservation stops holding. That is
-- correct but slow and unaddressed: the director is the one who has to route
-- the work, and the next thing they would otherwise see is a generic "advance
-- it or hand it off" that says nothing about why it was quiet or what changed.
-- This says the specific thing, once per reservation identity.
--
-- The identity is recorded on the ticket's notification state rather than
-- inferred from the queue, because the queue row is gone the moment it is
-- acknowledged and a listener restart must not mint a second wake for a
-- reservation that has already been announced.
CREATE OR REPLACE FUNCTION ticket_board.serial_focus_available_message(
    p_ticket_id text,
    p_title text,
    p_queued_for text,
    p_queued_behind text,
    p_now_reserved text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- Two different facts, said differently. "Capacity is available" is only
    -- true when the implementer holds nothing else; when another ticket has
    -- taken the reservation, saying so is what lets the director re-queue
    -- against the right identity instead of routing into a full lane again.
    SELECT p_ticket_id
        || CASE WHEN coalesce(p_title, '') <> '' THEN ' -- ' || p_title ELSE '' END
        || CASE
            WHEN coalesce(p_now_reserved, '') = '' THEN
                ' can be routed to ' || p_queued_for || ' now: ' || p_queued_behind
                || ' no longer holds their serial focus and they hold no other reserved work.'
            ELSE
                ' is still queued for ' || p_queued_for || ', but ' || p_queued_behind
                || ' no longer holds their serial focus -- ' || p_now_reserved
                || ' does now. Route it again behind ' || p_now_reserved
                || ', or route it to a free implementer.'
        END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_serial_focus_queue_wakeups(
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    now_reserved text;
    notice text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_serial_focus_queue_wakeups');

    FOR candidate IN
        SELECT
            t.id,
            t.title,
            t.state,
            t.assignee,
            t.ticket_number,
            btrim(t.queued_for_assignee) AS queued_for,
            btrim(t.queued_behind_ticket) AS queued_behind,
            btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket) AS wake_key
        FROM ticket_board.tickets t
        JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE btrim(t.queued_for_assignee) <> ''
          AND btrim(t.queued_behind_ticket) <> ''
          AND NOT t.manually_controlled
          -- The wait has genuinely ended. Everything else about this ticket --
          -- blockers, holds, its own stage -- is the director's to read; the
          -- one thing they cannot see from the board is that the reservation
          -- they were told to wait for is over.
          AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
              t.id,
              t.queued_for_assignee,
              t.queued_behind_ticket
          )
          AND ns.serial_focus_wake_key IS DISTINCT FROM
              (btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket))
          -- Blocked work cannot be routed either, and announcing capacity for
          -- it would be this ticket's own complaint in a new costume. The
          -- claim is left unmade, so the hand-off is announced when the
          -- blocker clears rather than lost -- which is what every other
          -- generator here does with a blocked ticket.
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
        ORDER BY t.ticket_number
    LOOP
        now_reserved := ticket_board.ticket_current_reserved_ticket(
            candidate.queued_for,
            candidate.id
        );
        notice := ticket_board.serial_focus_available_message(
            candidate.id,
            candidate.title,
            candidate.queued_for,
            candidate.queued_behind,
            now_reserved
        );
        -- Claimed before it is enqueued. enqueue_notification dedupes a row
        -- that is still waiting, but a delivered row is gone, and this function
        -- runs on every listener pass: without the claim the same wake would be
        -- minted again on the next pass after the first was acknowledged.
        UPDATE ticket_board.ticket_notification_state
        SET serial_focus_wake_key = candidate.wake_key
        WHERE ticket_id = candidate.id;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'ticket_update',
            'director',
            notice,
            -- `queued_for` and `reserved_by` are the queue announcement's own
            -- vocabulary, and deliberately so. The listener reads those two
            -- keys off any notification that carries them and discards it when
            -- the ticket's queue columns have moved on (SYRD-108). A wake is a
            -- statement about one reservation identity, so it should be
            -- discarded on a reroute for exactly that reason -- and naming the
            -- fields anything else would not avoid the question, it would just
            -- exempt this notification from an answer it needs. The claim that
            -- makes a wake once-per-identity is cleared by the same reroute, so
            -- the new identity gets its own wake when its own wait ends.
            jsonb_build_object(
                'kind', 'ticket_update',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', 'director',
                'queued_for', candidate.queued_for,
                'reserved_by', candidate.queued_behind,
                'now_reserved', coalesce(now_reserved, ''),
                'message', notice
            ),
            'serial-focus-available:' || candidate.id || ':' || candidate.wake_key
        );
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reset_serial_focus_wake_key()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- A new queue identity is a new wait, so the claim that announced the old
    -- one must not silence it. Rerouting to another implementer, re-queueing
    -- behind a different ticket, and clearing the fields outright all land
    -- here, which is what makes a reroute's wake condition move with it and a
    -- cleared queue fall straight back to ordinary reminders (SYRD-109).
    UPDATE ticket_board.ticket_notification_state
    SET serial_focus_wake_key = ''
    WHERE ticket_id = NEW.id
      AND serial_focus_wake_key <> '';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzzz_reset_serial_focus_wake_key ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzz_reset_serial_focus_wake_key
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
WHEN (
    OLD.queued_for_assignee IS DISTINCT FROM NEW.queued_for_assignee
    OR OLD.queued_behind_ticket IS DISTINCT FROM NEW.queued_behind_ticket
)
EXECUTE FUNCTION ticket_board.reset_serial_focus_wake_key();

--
-- SYRD-120: work the board was showing the Director and had not told them about.
--
-- Live on 2026-09-13, SYRD-111, SYRD-118 and SYRD-119 all sat in analysis with
-- assignee `unassigned`. The board drew all three cards in the analysis column,
-- which the declared workflow gives the Director to own, `ticket-board-read
-- queue director` returned nothing, and no notification had ever fired. The
-- board was displaying somebody's work while denying, when asked directly, that
-- it was theirs.
--
-- Both halves came from reading ASSIGNMENT where the actionable fact is
-- OWNERSHIP OF THE STAGE. A stage whose notify policy is `assignee` resolves to
-- the assignee, `unassigned` owns nothing, and the ordinary transition
-- notification therefore has no target and stays silent. That silence is
-- correct for a stage nobody owns and wrong for one somebody does: untriaged
-- work in a stage with a single owner is that owner's to route, and it is
-- exactly the work nobody can be expected to discover by other means.
--
-- The handoff below fires precisely where the ordinary one is silent, which is
-- why it cannot double-announce anything: `unassigned_stage_owner` returns a
-- role only when `transition_target_role` returns none.
--

-- Who untriaged work in this stage belongs to, or NULL when there is no
-- answer to derive. Every way of having no answer is a case that must stay
-- silent, and none of them is a role name.
CREATE OR REPLACE FUNCTION ticket_board.unassigned_stage_owner(p_state text, p_assignee text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    cfg jsonb := ticket_board.declared_workflow();
    stage jsonb;
    owners text[];
BEGIN
    -- A board with no declared workflow has no stage ownership to read, and
    -- its legacy table already routes analysis to a fixed role.
    IF cfg IS NULL THEN
        RETURN NULL;
    END IF;
    -- Somebody is already being told. This handoff exists only in the gap the
    -- ordinary notification leaves, so the two can never both fire.
    IF ticket_board.transition_target_role(p_state, p_assignee) IS NOT NULL THEN
        RETURN NULL;
    END IF;
    SELECT x INTO stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = p_state;
    IF stage IS NULL OR coalesce((stage->>'terminal')::boolean, false) THEN
        RETURN NULL;
    END IF;
    -- A stage the document says notifies nobody is a deliberate silence, not a
    -- gap: user_review is owned and announces nothing on purpose.
    IF stage->'notify'->>'kind' = 'none' THEN
        RETURN NULL;
    END IF;
    -- Somewhere to put work down is not somewhere work is waiting for anyone.
    IF ticket_board.declared_parking_stage(p_state) THEN
        RETURN NULL;
    END IF;
    owners := ARRAY(SELECT jsonb_array_elements_text(stage->'owners'));
    -- Nobody owns it, or several do. Choosing between several owners would be
    -- this function inventing an assignment; the whole point is that nobody has
    -- made one yet.
    IF coalesce(array_length(owners, 1), 0) <> 1 THEN
        RETURN NULL;
    END IF;
    RETURN owners[1];
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.unassigned_triage_message(
    p_ticket_id text,
    p_title text,
    p_state text,
    p_owner text
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT format(
        '%s -- %s is in %s with no assignee, and %s owns that stage. '
        'Route it to an implementer, defer it, or take it yourself.',
        p_ticket_id, p_title, p_state, p_owner);
$$;

-- One durable handoff for one stay in the stage. Durable because it goes
-- through the same queue as every other notification: retries, busy deferral,
-- restart recovery and exactly-once delivery are that queue's, not a second
-- mechanism's, and the dedupe key means a trigger that fires twice for one
-- entry still leaves one notice.
CREATE OR REPLACE FUNCTION ticket_board.enqueue_unassigned_triage_handoff(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    recipient := ticket_board.unassigned_stage_owner(t.state, t.assignee);
    IF recipient IS NULL THEN
        RETURN;
    END IF;
    -- Manual control is a deliberate silence the Director asked for, and a
    -- ticket waiting on work that is not finished is not actionable yet. Both
    -- are answered again when they end: clearing the hold and resolving the
    -- last blocker each bring the ticket back through this function.
    IF coalesce(t.manually_controlled, false)
       OR ticket_board.ticket_has_unresolved_blockers(t.id) THEN
        RETURN;
    END IF;
    PERFORM ticket_board.enqueue_notification(
        t.id,
        'triage',
        recipient,
        ticket_board.unassigned_triage_message(t.id, t.title, t.state, recipient),
        jsonb_build_object(
            'kind', 'triage',
            'id', t.id,
            'title', t.title,
            'state', t.state,
            'assignee', t.assignee,
            'target_role', recipient,
            'ticket_number', t.ticket_number,
            'updated_at', t.updated_at
        ),
        format('triage:%s:%s:%s', t.id, t.state, recipient));
END;
$$;

-- A pending notice that no longer describes the ticket is removed rather than
-- left to be discarded at delivery. The listener would drop it either way --
-- the payload carries the stage and assignee it was written for -- but a notice
-- sitting in the queue beside a ticket that has moved on is the shape SYRD-108
-- had to clean up after, and it reads to anyone looking at the queue as work
-- still waiting to be announced.
CREATE OR REPLACE FUNCTION ticket_board.supersede_unassigned_triage_handoff(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    stale record;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    FOR stale IN
        SELECT q.id, q.ticket_id, q.target_role, q.kind, q.attempts, q.payload
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = p_ticket_id
          AND q.kind = 'triage'
          AND (q.payload->>'state' IS DISTINCT FROM t.state
               OR q.payload->>'assignee' IS DISTINCT FROM t.assignee
               OR coalesce(t.manually_controlled, false))
    LOOP
        PERFORM ticket_board.record_notification_trace(
            stale.ticket_id, stale.id, stale.target_role, stale.kind, 'discard',
            NULL, 'superseded_unassigned_triage', NULL,
            jsonb_build_object(
                'attempts', stale.attempts,
                'payload', stale.payload,
                'reason', 'superseded_unassigned_triage',
                'current_state', t.state,
                'current_assignee', t.assignee,
                'manually_controlled', coalesce(t.manually_controlled, false)));
        DELETE FROM ticket_board.ticket_notification_queue WHERE id = stale.id;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_unassigned_triage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- The same two suppressions every other notification honours, so a seeded
    -- fixture or a bulk import does not wake anyone.
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on'
       OR current_setting('ticket_board.suppress_transition_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- Routing, deferring, cancelling and taking manual control all arrive
        -- here as a change of the shape the notice was written for.
        PERFORM ticket_board.supersede_unassigned_triage_handoff(NEW.id);
    END IF;
    PERFORM ticket_board.enqueue_unassigned_triage_handoff(NEW.id);
    RETURN NULL;
END;
$$;

-- Entry, not residence: a ticket is announced when it arrives in a stage
-- nobody is holding it in, and an unrelated edit while it sits there is not a
-- second arrival. Two triggers rather than one because a WHEN condition on an
-- INSERT cannot speak about OLD, and the condition is the whole point of the
-- UPDATE half. Manual control is in it because clearing a deliberate hold is
-- the moment the work becomes actionable again.
DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage_insert ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_unassigned_triage_insert
AFTER INSERT ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.notify_unassigned_triage();

DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage_update ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_unassigned_triage_update
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
WHEN (
    OLD.state IS DISTINCT FROM NEW.state
    OR OLD.assignee IS DISTINCT FROM NEW.assignee
    OR OLD.manually_controlled IS DISTINCT FROM NEW.manually_controlled
)
EXECUTE FUNCTION ticket_board.notify_unassigned_triage();


-- SYRD-194: the unresolved-turn guard and its one-turn continuation lease.
-- One row per (ticket, owner, turn). `turn_id` is the completed-turn identity
-- the pane hook mints; it is opaque here on purpose, because the board must not
-- care which runtime produced it.
CREATE TABLE IF NOT EXISTS ticket_board.turn_continuation_lease (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    owner_role text NOT NULL,
    turn_id text NOT NULL,
    reason text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    PRIMARY KEY (ticket_id, owner_role, turn_id)
);

CREATE INDEX IF NOT EXISTS turn_continuation_lease_open_idx
    ON ticket_board.turn_continuation_lease (owner_role, expires_at)
    WHERE consumed_at IS NULL;

-- A fourth reason a notification exists. Named rather than folded into
-- 'escalation' so the two can be told apart in the trace: an escalation says
-- "was reminded and still has not moved", this says "stopped without saying
-- anything", and they call for different responses.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation',
                    'idle_reminder', 'awaiting_role', 'unresolved_turn',
                    'unresolved_turn_repair'));

-- The identity a repeat is measured against: same ticket, same stage, same
-- assignee, same completed turn. A fresh turn changes it and re-arms; an
-- acknowledgement does not, so acknowledging cannot cause a repeat for the turn
-- that was already reported.
CREATE OR REPLACE FUNCTION ticket_board.turn_unresolved_identity(
    p_ticket text,
    p_state text,
    p_assignee text,
    p_turn_id text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT 'unresolved-turn:' || coalesce(p_ticket, '') || ':' || coalesce(p_state, '')
           || ':' || coalesce(p_assignee, '') || ':' || coalesce(p_turn_id, '');
$$;

-- Is this ticket's active work durably resolved, by any of the means that end a
-- turn legitimately? Written once and used by the guard, so "resolved" has one
-- definition rather than one per call site.
CREATE OR REPLACE FUNCTION ticket_board.ticket_turn_is_resolved(
    p_ticket text,
    p_now timestamptz
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT
        t.manually_controlled
        OR t.state IS NULL
        OR ticket_board.transition_target_role(t.state, t.assignee) IS NULL
        OR (CASE WHEN ticket_board.declared_workflow() IS NULL
                 THEN t.state IN ('done', 'cancelled')
                 ELSE coalesce((SELECT is_terminal FROM ticket_board.workflow_stages WHERE name = t.state), false)
            END)
        OR ticket_board.ticket_awaiting_role_is_active(ns.awaiting_role, ns.awaiting_since_at, p_now)
        OR ticket_board.ticket_serial_focus_reservation_is_current(
               t.id, t.queued_for_assignee, t.queued_behind_ticket)
        OR EXISTS (
            SELECT 1
            FROM ticket_board.ticket_blockers tb
            LEFT JOIN ticket_board.tickets blocker ON blocker.id = tb.blocker_ticket_id
            WHERE tb.ticket_id = t.id
              AND (blocker.id IS NULL OR blocker.state NOT IN ('done', 'cancelled'))
        )
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id = p_ticket;
$$;

-- The owner's deliberate "I am continuing", recorded the way request_dependency
-- records a dependency: the sentence and the state in one transaction, so a
-- lease never exists without the reason for it. Granting is the owner's to do
-- and nobody else's, and it covers exactly the turn named.
CREATE OR REPLACE FUNCTION ticket_board.grant_turn_continuation(
    p_ticket text,
    p_turn_id text,
    p_reason text,
    p_grace interval DEFAULT interval '10 minutes'
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    -- Prefixed, because `reason`, `owner_role` and `turn_id` are also column
    -- names on turn_continuation_lease and an unprefixed local makes the
    -- ON CONFLICT target ambiguous -- Postgres refuses it rather than guessing.
    v_reason text := btrim(coalesce(p_reason, ''));
    v_turn text := btrim(coalesce(p_turn_id, ''));
    v_owner_role text;
    v_caller text;
BEGIN
    IF v_turn = '' THEN
        RAISE EXCEPTION 'continuing work requires the turn it continues from';
    END IF;
    IF v_reason = '' THEN
        -- Same reasoning as request_dependency: the Director may still read
        -- this, and "continuing" with no sentence is indistinguishable from
        -- silence.
        RAISE EXCEPTION 'continuing work requires a reason';
    END IF;
    PERFORM ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'grant_turn_continuation'
    );
    v_caller := nullif(current_setting('ticket_board.caller_role', true), '');
    SELECT ticket_board.transition_target_role(t.state, t.assignee) INTO v_owner_role
    FROM ticket_board.tickets t WHERE t.id = p_ticket;
    IF v_owner_role IS NULL THEN
        RAISE EXCEPTION 'ticket % has no active owner to continue', p_ticket;
    END IF;
    IF v_caller IS NOT NULL AND v_caller <> v_owner_role THEN
        -- Continuation is the owner's statement about its own next turn.
        RAISE EXCEPTION 'role % cannot continue work owned by %', v_caller, v_owner_role;
    END IF;
    PERFORM ticket_board.add_comment(p_ticket, v_reason);
    INSERT INTO ticket_board.turn_continuation_lease AS l
        (ticket_id, owner_role, turn_id, reason, expires_at)
    VALUES (p_ticket, v_owner_role, v_turn, v_reason, clock_timestamp() + p_grace)
    ON CONFLICT (ticket_id, owner_role, turn_id) DO UPDATE
        SET reason = excluded.reason,
            expires_at = excluded.expires_at
        WHERE l.consumed_at IS NULL;
END;
$$;

-- A new turn beginning consumes whatever the previous one leased. This is what
-- makes the lease one-shot: it buys the boundary it was taken at and nothing
-- beyond it.
CREATE OR REPLACE FUNCTION ticket_board.consume_turn_continuation(
    p_owner_role text,
    p_before_turn_id text DEFAULT NULL
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    consumed integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('consume_turn_continuation');
    UPDATE ticket_board.turn_continuation_lease
    SET consumed_at = clock_timestamp()
    WHERE owner_role = p_owner_role
      AND consumed_at IS NULL
      AND (p_before_turn_id IS NULL OR turn_id <> p_before_turn_id);
    GET DIAGNOSTICS consumed = ROW_COUNT;
    RETURN consumed;
END;
$$;

-- The guard. A sibling of notify_idle_turn_end_nudges, not a change to it: that
-- one still decides reminders and escalations on exactly the terms SYRD-163
-- gave it. This one answers a different question -- did an owner just stop
-- without resolving? -- and answers it at the boundary rather than after a
-- timeout.
--
-- `p_turn_by_role` is {role: turn_id} for the roles whose turn just ended. A
-- role absent from it is not being judged; silence about a role is not evidence
-- about its work.
-- Dropped, not merely replaced. CREATE OR REPLACE matches on parameter types,
-- so adding the grace interval makes a SECOND function rather than a new
-- version of this one -- and because the new parameter has a default, a
-- two-argument call then matches both and Postgres refuses it as ambiguous.
-- The old signature has to go, and going takes its grant with it, so rbac.sql
-- grants the new one and this migration restates it for a board that arrives
-- here by upgrade (SYRD-203 review).
DROP FUNCTION IF EXISTS ticket_board.notify_unresolved_turn_end(jsonb, timestamptz);
CREATE OR REPLACE FUNCTION ticket_board.notify_unresolved_turn_end(
    p_turn_by_role jsonb,
    p_now timestamptz,
    -- How long the owner has to answer their own prompt before the Director is
    -- told. Staged recovery: one unresolved turn is one nudge to the person who
    -- can fix it, and an escalation only if that does not work (SYRD-203).
    p_grace interval DEFAULT interval '10 minutes'
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    enqueued integer := 0;
    identity text;
    escalate record;
    owner_reported boolean;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_unresolved_turn_end');
    -- An empty map means no turn ended on this pass. That skips the prompting
    -- half and NOT the escalation half: the case the escalation exists for is
    -- an owner who has gone quiet, and a quiet owner produces no turn ends at
    -- all. Returning here is what would make the grace period unreachable
    -- (SYRD-203).
    FOR candidate IN
        SELECT * FROM (SELECT 1) AS _guard WHERE coalesce(p_turn_by_role, '{}'::jsonb) <> '{}'::jsonb
    LOOP
    END LOOP;

    FOR candidate IN
        SELECT
            t.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee)) AS turn_id
        FROM ticket_board.tickets t
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND p_turn_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
          -- Owned, active, and not resolved by any of the legitimate means.
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
          -- A lease the owner took for THIS turn, still open and not yet
          -- expired, is the one thing that buys silence here. An expired lease
          -- buys nothing, which is what stops it becoming a way to stall: the
          -- owner promised a next turn and none arrived.
          AND NOT EXISTS (
              SELECT 1
              FROM ticket_board.turn_continuation_lease l
              WHERE l.ticket_id = t.id
                AND l.owner_role = ticket_board.transition_target_role(t.state, t.assignee)
                AND l.turn_id = (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))
                AND l.consumed_at IS NULL
                AND l.expires_at > p_now
          )
        ORDER BY t.id
    LOOP
        identity := ticket_board.turn_unresolved_identity(
            candidate.id, candidate.state, candidate.assignee, candidate.turn_id
        );
        -- Reported once per turn identity, per audience. The marker is the
        -- dedupe key itself, which outlives the queue row, so acknowledging or
        -- delivering cannot cause a repeat for the same unresolved end
        -- (SYRD-133's lesson, applied to a different identity).
        --
        -- Per audience, because these are two notifications about one event and
        -- each has its own key: skipping the candidate on the Director's marker
        -- alone would silence the owner's repair prompt for a turn nobody ever
        -- prompted them about (SYRD-203).
        owner_reported := EXISTS (
            SELECT 1 FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = candidate.id
              AND tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
              AND tr.detail ->> 'dedupe_key' = 'repair:' || identity
        );
        -- No combined skip here. Each audience is guarded by its own marker
        -- below, so a `CONTINUE` on both would be a third guard that can only
        -- ever agree with them -- and a guard no test can distinguish is one no
        -- reader can trust (the SYRD-159 lesson).
        -- The turn-end pass nudges the OWNER and nobody else. A turn that
        -- ended without resolving a ticket is first of all news for the person
        -- who can resolve it, and telling the Director in the same breath makes
        -- every ordinary forgotten turn an escalation. The Director is told by
        -- the second half of this function, and only when the prompt did not
        -- work (SYRD-203, correcting the immediate dual delivery this shipped
        -- with).
        -- The owner's single repair prompt: one per turn identity, on its own
        -- dedupe key. It is guarded by its OWN marker and by nothing the
        -- Director has heard -- news the Director already had used to silence
        -- the owner's only prompt to repair the turn, which was half of
        -- SYRD-203.
        IF NOT owner_reported THEN
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'unresolved_turn_repair',
            candidate.owner_role,
            format(
                '%s is still yours and this turn ended without resolving it. Do one of: '
                || 'transition it if the work is done; `request-dependency` if you need '
                || 'another role; record a blocker if something else must land first; or '
                || 'continue work, which covers the next turn only. The Director has '
                || 'already been told.',
                candidate.id
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn_repair',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'owner_role', candidate.owner_role,
                'target_role', candidate.owner_role,
                'turn_id', candidate.turn_id,
                'choices', jsonb_build_array(
                    'transition', 'request-dependency', 'blocker', 'continue'
                ),
                'message', format(
                    '%s is still yours and this turn ended without resolving it.',
                    candidate.id
                ),
                'grace', extract(epoch from p_grace)
            ),
            'repair:' || identity
        );
        -- Counted here, because on this pass the owner's prompt IS the event.
        -- The escalation half counts its own, so a caller's number is "how many
        -- unresolved turns did this pass act on", either way.
        enqueued := enqueued + 1;
        END IF;
        -- Counted once per TICKET reported, not once per row: the two
        -- notifications are one event with two audiences.
    END LOOP;

    -- SECOND HALF: the escalation, staged behind the owner's prompt.
    --
    -- The Director hears about an unresolved turn only when the prompt did not
    -- work: the ticket is still unresolved and the owner has had the grace
    -- period to answer, or the prompt could not be delivered to them at all. It
    -- runs on every pass rather than on a turn-end, because "the owner did not
    -- answer in time" is a statement about elapsed time and there is no second
    -- turn-end to hang it on -- a silent owner produces no events at all, which
    -- is exactly the case this exists for.
    FOR escalate IN
        WITH prompted AS (
            SELECT
                tr.ticket_id AS id,
                tr.detail ->> 'dedupe_key' AS repair_key,
                min(tr.ts) AS prompted_at
            FROM ticket_board.notification_trace tr
            WHERE tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
            GROUP BY tr.ticket_id, tr.detail ->> 'dedupe_key'
        )
        SELECT
            prompted.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            prompted.repair_key,
            prompted.prompted_at,
            EXISTS (
                SELECT 1 FROM ticket_board.ticket_notification_queue q
                WHERE q.dedupe_key = prompted.repair_key
                  AND q.dead_lettered_at IS NOT NULL
            ) AS undeliverable
        FROM prompted
        JOIN ticket_board.tickets t ON t.id = prompted.id
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
        ORDER BY prompted.id
    LOOP
        CONTINUE WHEN NOT (
            escalate.undeliverable OR escalate.prompted_at <= p_now - p_grace
        );
        identity := replace(escalate.repair_key, 'repair:', '');
        -- Not at all if the Director has already been told about this
        -- ticket since the owner was prompted -- whether that telling is still
        -- queued or has been delivered and acknowledged. Reading only the queue
        -- made an acknowledged escalation invisible, so this generator followed
        -- the reminder path with the same news a moment later: the Director is
        -- owed one notification about a ticket, not one per generator.
        CONTINUE WHEN EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = escalate.id
              AND q.target_role = 'director'
              AND q.kind IN ('escalation', 'unresolved_turn')
        ) OR EXISTS (
            SELECT 1
            FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = escalate.id
              AND tr.target_role = 'director'
              AND tr.kind IN ('escalation', 'unresolved_turn')
              AND tr.event = 'enqueue'
              AND tr.ts >= escalate.prompted_at
        );
        PERFORM ticket_board.enqueue_notification(
            escalate.id,
            'unresolved_turn',
            'director',
            format(
                '%s ended a turn without resolving %s (%s), and %s after being prompted it is '
                || 'still unresolved.',
                escalate.owner_role, escalate.id, escalate.state,
                CASE WHEN escalate.undeliverable
                     THEN 'the prompt could not be delivered'
                     ELSE 'the grace period has passed' END
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn',
                'id', escalate.id,
                'state', escalate.state,
                'assignee', escalate.assignee,
                'owner_role', escalate.owner_role,
                'target_role', 'director',
                'undeliverable', escalate.undeliverable,
                'prompted_at', escalate.prompted_at,
                'message', format(
                    '%s is still unresolved after %s was prompted.',
                    escalate.id, escalate.owner_role
                )
            ),
            identity
        );
        enqueued := enqueued + 1;
    END LOOP;
    RETURN enqueued;
END;
$$;

-- SYRD-199: restore the notification kinds pgu948 dropped.
--
-- ticket_notification_queue_kind_check is DROP/ADDed by several migrations in
-- sequence, so only the LAST definition survives. pgu936 (SYRD-120) had extended
-- the list with 'publication' and 'triage'. pgu948 (SYRD-194) rebuilt it from the
-- list as it stood BEFORE pgu936 and dropped both, while the code that enqueues
-- them stayed exactly where it was:
--
--     request_publication          -> 'publication'
--     publication resolution       -> 'publication'
--     unassigned triage notice     -> 'triage'
--
-- The visible effect was not a missing notification. enqueue_notification runs
-- inside the caller's transaction, so the CHECK violation aborted the whole
-- statement: filing a bug creates a ticket in 'analysis' with no assignee, which
-- fires the triage notice, which rejected, which rolled the new ticket back.
-- file-bug failed outright.
--
-- This restores the UNION of every kind the schema enqueues, not any previous
-- list -- rebuilding from a snapshot is the mistake that caused this.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation',
                    'idle_reminder', 'awaiting_role', 'publication', 'triage',
                    'unresolved_turn', 'unresolved_turn_repair'));

-- SYRD-207: the owner prompt must not claim the Director already knows.
--
-- Staged recovery means the owner is told FIRST and alone, and the Director only
-- if the grace expires without the ticket being resolved. The message carried
-- over from pgu948, where both audiences were told at once, and it still ended
-- "The Director has already been told." Under staging that sentence is false at
-- the moment it is delivered, and false in the direction that matters: it tells
-- the person who can still fix this quietly that the escalation has already
-- happened, which removes the reason to act before it does.
--
-- Live on SYRD-206: the owner prompt said the Director had been told, and the
-- trace showed zero Director rows for that ticket at that instant.
--
-- CREATE OR REPLACE rather than DROP + CREATE: the signature is unchanged, so
-- replacing it keeps the privileges pgu950 granted. The grant is restated anyway
-- because it costs nothing and a board arriving here another way should not have
-- to depend on that reasoning.


CREATE OR REPLACE FUNCTION ticket_board.notify_unresolved_turn_end(
    p_turn_by_role jsonb,
    p_now timestamptz,
    -- How long the owner has to answer their own prompt before the Director is
    -- told. Staged recovery: one unresolved turn is one nudge to the person who
    -- can fix it, and an escalation only if that does not work (SYRD-203).
    p_grace interval DEFAULT interval '10 minutes'
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    enqueued integer := 0;
    identity text;
    escalate record;
    owner_reported boolean;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_unresolved_turn_end');
    -- An empty map means no turn ended on this pass. That skips the prompting
    -- half and NOT the escalation half: the case the escalation exists for is
    -- an owner who has gone quiet, and a quiet owner produces no turn ends at
    -- all. Returning here is what would make the grace period unreachable
    -- (SYRD-203).
    FOR candidate IN
        SELECT * FROM (SELECT 1) AS _guard WHERE coalesce(p_turn_by_role, '{}'::jsonb) <> '{}'::jsonb
    LOOP
    END LOOP;

    FOR candidate IN
        SELECT
            t.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee)) AS turn_id
        FROM ticket_board.tickets t
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND p_turn_by_role ? ticket_board.transition_target_role(t.state, t.assignee)
          -- Owned, active, and not resolved by any of the legitimate means.
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
          -- A lease the owner took for THIS turn, still open and not yet
          -- expired, is the one thing that buys silence here. An expired lease
          -- buys nothing, which is what stops it becoming a way to stall: the
          -- owner promised a next turn and none arrived.
          AND NOT EXISTS (
              SELECT 1
              FROM ticket_board.turn_continuation_lease l
              WHERE l.ticket_id = t.id
                AND l.owner_role = ticket_board.transition_target_role(t.state, t.assignee)
                AND l.turn_id = (p_turn_by_role ->> ticket_board.transition_target_role(t.state, t.assignee))
                AND l.consumed_at IS NULL
                AND l.expires_at > p_now
          )
        ORDER BY t.id
    LOOP
        identity := ticket_board.turn_unresolved_identity(
            candidate.id, candidate.state, candidate.assignee, candidate.turn_id
        );
        -- Reported once per turn identity, per audience. The marker is the
        -- dedupe key itself, which outlives the queue row, so acknowledging or
        -- delivering cannot cause a repeat for the same unresolved end
        -- (SYRD-133's lesson, applied to a different identity).
        --
        -- Per audience, because these are two notifications about one event and
        -- each has its own key: skipping the candidate on the Director's marker
        -- alone would silence the owner's repair prompt for a turn nobody ever
        -- prompted them about (SYRD-203).
        owner_reported := EXISTS (
            SELECT 1 FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = candidate.id
              AND tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
              AND tr.detail ->> 'dedupe_key' = 'repair:' || identity
        );
        -- No combined skip here. Each audience is guarded by its own marker
        -- below, so a `CONTINUE` on both would be a third guard that can only
        -- ever agree with them -- and a guard no test can distinguish is one no
        -- reader can trust (the SYRD-159 lesson).
        -- The turn-end pass nudges the OWNER and nobody else. A turn that
        -- ended without resolving a ticket is first of all news for the person
        -- who can resolve it, and telling the Director in the same breath makes
        -- every ordinary forgotten turn an escalation. The Director is told by
        -- the second half of this function, and only when the prompt did not
        -- work (SYRD-203, correcting the immediate dual delivery this shipped
        -- with).
        -- The owner's single repair prompt: one per turn identity, on its own
        -- dedupe key. It is guarded by its OWN marker and by nothing the
        -- Director has heard -- news the Director already had used to silence
        -- the owner's only prompt to repair the turn, which was half of
        -- SYRD-203.
        IF NOT owner_reported THEN
        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'unresolved_turn_repair',
            candidate.owner_role,
            format(
                '%s is still yours and this turn ended without resolving it. Do one of: '
                || 'transition it if the work is done; `request-dependency` if you need '
                || 'another role; record a blocker if something else must land first; or '
                || 'continue work, which covers the next turn only. The Director has NOT '
                || 'been told, and will be if it is still unresolved in %s.',
                candidate.id,
                p_grace
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn_repair',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'owner_role', candidate.owner_role,
                'target_role', candidate.owner_role,
                'turn_id', candidate.turn_id,
                'choices', jsonb_build_array(
                    'transition', 'request-dependency', 'blocker', 'continue'
                ),
                'message', format(
                    '%s is still yours and this turn ended without resolving it.',
                    candidate.id
                ),
                'grace', extract(epoch from p_grace)
            ),
            'repair:' || identity
        );
        -- Counted here, because on this pass the owner's prompt IS the event.
        -- The escalation half counts its own, so a caller's number is "how many
        -- unresolved turns did this pass act on", either way.
        enqueued := enqueued + 1;
        END IF;
        -- Counted once per TICKET reported, not once per row: the two
        -- notifications are one event with two audiences.
    END LOOP;

    -- SECOND HALF: the escalation, staged behind the owner's prompt.
    --
    -- The Director hears about an unresolved turn only when the prompt did not
    -- work: the ticket is still unresolved and the owner has had the grace
    -- period to answer, or the prompt could not be delivered to them at all. It
    -- runs on every pass rather than on a turn-end, because "the owner did not
    -- answer in time" is a statement about elapsed time and there is no second
    -- turn-end to hang it on -- a silent owner produces no events at all, which
    -- is exactly the case this exists for.
    FOR escalate IN
        WITH prompted AS (
            SELECT
                tr.ticket_id AS id,
                tr.detail ->> 'dedupe_key' AS repair_key,
                min(tr.ts) AS prompted_at
            FROM ticket_board.notification_trace tr
            WHERE tr.kind = 'unresolved_turn_repair'
              AND tr.event = 'enqueue'
            GROUP BY tr.ticket_id, tr.detail ->> 'dedupe_key'
        )
        SELECT
            prompted.id,
            t.state,
            t.assignee,
            ticket_board.transition_target_role(t.state, t.assignee) AS owner_role,
            prompted.repair_key,
            prompted.prompted_at,
            EXISTS (
                SELECT 1 FROM ticket_board.ticket_notification_queue q
                WHERE q.dedupe_key = prompted.repair_key
                  AND q.dead_lettered_at IS NOT NULL
            ) AS undeliverable
        FROM prompted
        JOIN ticket_board.tickets t ON t.id = prompted.id
        WHERE ticket_board.transition_target_role(t.state, t.assignee) IS NOT NULL
          AND NOT ticket_board.ticket_turn_is_resolved(t.id, p_now)
        ORDER BY prompted.id
    LOOP
        CONTINUE WHEN NOT (
            escalate.undeliverable OR escalate.prompted_at <= p_now - p_grace
        );
        identity := replace(escalate.repair_key, 'repair:', '');
        -- Not at all if the Director has already been told about this
        -- ticket since the owner was prompted -- whether that telling is still
        -- queued or has been delivered and acknowledged. Reading only the queue
        -- made an acknowledged escalation invisible, so this generator followed
        -- the reminder path with the same news a moment later: the Director is
        -- owed one notification about a ticket, not one per generator.
        CONTINUE WHEN EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = escalate.id
              AND q.target_role = 'director'
              AND q.kind IN ('escalation', 'unresolved_turn')
        ) OR EXISTS (
            SELECT 1
            FROM ticket_board.notification_trace tr
            WHERE tr.ticket_id = escalate.id
              AND tr.target_role = 'director'
              AND tr.kind IN ('escalation', 'unresolved_turn')
              AND tr.event = 'enqueue'
              AND tr.ts >= escalate.prompted_at
        );
        PERFORM ticket_board.enqueue_notification(
            escalate.id,
            'unresolved_turn',
            'director',
            format(
                '%s ended a turn without resolving %s (%s), and %s after being prompted it is '
                || 'still unresolved.',
                escalate.owner_role, escalate.id, escalate.state,
                CASE WHEN escalate.undeliverable
                     THEN 'the prompt could not be delivered'
                     ELSE 'the grace period has passed' END
            ),
            jsonb_build_object(
                'kind', 'unresolved_turn',
                'id', escalate.id,
                'state', escalate.state,
                'assignee', escalate.assignee,
                'owner_role', escalate.owner_role,
                'target_role', 'director',
                'undeliverable', escalate.undeliverable,
                'prompted_at', escalate.prompted_at,
                'message', format(
                    '%s is still unresolved after %s was prompted.',
                    escalate.id, escalate.owner_role
                )
            ),
            identity
        );
        enqueued := enqueued + 1;
    END LOOP;
    RETURN enqueued;
END;
$$;




-- SYRD-234: tell the Director about a pane stopped on a permission prompt.
--
-- A Claude role launched in bypass mode still meets one confirmation that no
-- permission mode skips: a recursive removal aimed at a critical path. A hook
-- answers that for a pane already in bypass, but anything it does not answer
-- leaves the pane waiting on a human who is not watching. The pane hook records
-- `blocked`, and the listener's activity gate reads blocked as busy and requeues
-- behind it -- so the role looks like it is working, for as long as nobody
-- looks. Every existing generator fires off the idle path, and a pane stopped on
-- a prompt never goes idle, so none of them ever fire for it.
--
-- This is the one that does. Deliberately the `escalation` kind, which is
-- already director-bound and already an allowed kind: what is missing is a
-- generator, not a new sort of notification.
CREATE OR REPLACE FUNCTION ticket_board.notify_permission_prompt_waits(
    -- {role: ISO-8601 instant the pane went blocked on a prompt}
    p_blocked_since jsonb,
    p_now timestamptz,
    -- The same staged-recovery shape the unresolved-turn escalation uses: a
    -- prompt answered quickly by a passing human is not worth the Director's
    -- attention, and one that is not is exactly what this exists for.
    p_grace interval DEFAULT interval '2 minutes'
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    waiting record;
    enqueued integer := 0;
    blocked_since timestamptz;
    identity text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_permission_prompt_waits');
    FOR waiting IN
        SELECT
            key AS role,
            value #>> '{}' AS since
        FROM jsonb_each(coalesce(p_blocked_since, '{}'::jsonb))
        ORDER BY key
    LOOP
        BEGIN
            blocked_since := waiting.since::timestamptz;
        EXCEPTION WHEN others THEN
            -- An unparseable instant is not a reason to escalate; it is a
            -- reason to say nothing about a pane nobody can describe.
            CONTINUE;
        END;
        CONTINUE WHEN blocked_since > p_now - p_grace;
        FOR identity IN
            SELECT t.id
            FROM ticket_board.tickets t
            WHERE t.assignee = waiting.role
              AND ticket_board.transition_target_role(t.state, t.assignee) = waiting.role
            ORDER BY t.id
        LOOP
            -- One per role per blocked episode. The key carries the instant the
            -- pane went blocked, so a prompt answered and re-raised later is a
            -- new episode and a repeat of the same one is not.
            CONTINUE WHEN EXISTS (
                SELECT 1
                FROM ticket_board.ticket_notification_queue q
                WHERE q.dedupe_key = format('permission-prompt:%s:%s', waiting.role, waiting.since)
            ) OR EXISTS (
                -- The queue row is gone once it is delivered and acknowledged,
                -- so the queue alone would let the same episode be reported
                -- again on the next pass. The trace outlives the row, and the
                -- dedupe key is carried in its detail (SYRD-133's lesson).
                SELECT 1
                FROM ticket_board.notification_trace tr
                WHERE tr.event = 'enqueue'
                  AND tr.detail ->> 'dedupe_key'
                      = format('permission-prompt:%s:%s', waiting.role, waiting.since)
            );
            PERFORM ticket_board.enqueue_notification(
                identity,
                'escalation',
                'director',
                format(
                    '%s is stopped on a permission prompt in its pane and cannot answer it '
                    || 'itself. It has been waiting since %s. Nothing it was asked to do is '
                    || 'progressing until somebody answers the prompt or restarts the role.',
                    waiting.role, waiting.since
                ),
                jsonb_build_object(
                    'kind', 'escalation',
                    'reason', 'permission_prompt',
                    'role', waiting.role,
                    'target_role', 'director',
                    'blocked_since', waiting.since,
                    'ticket_id', identity
                ),
                format('permission-prompt:%s:%s', waiting.role, waiting.since)
            );
            enqueued := enqueued + 1;
        END LOOP;
    END LOOP;
    RETURN enqueued;
END;
$$;
