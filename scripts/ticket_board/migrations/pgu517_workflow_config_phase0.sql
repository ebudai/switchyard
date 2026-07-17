CREATE TABLE IF NOT EXISTS ticket_board.workflow_stages (
    name text PRIMARY KEY
        CHECK (name IN (
            'draft',
            'backlog',
            'analysis',
            'in_progress',
            'inspection',
            'audit',
            'eric_review',
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
    PRIMARY KEY (from_stage, to_stage, action_name)
);

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
    ('audit', 'Audit', 5, ARRAY['audit']::text[], NULL, NULL, 'audit_signoff', false),
    ('eric_review', 'UAT', 6, ARRAY['director']::text[], 'needs_eric_signoff', 'director_review', 'eric_signoff', false),
    ('director_review', 'Final Sign-Off', 7, ARRAY['director']::text[], NULL, NULL, NULL, false),
    ('done', 'Done', 8, ARRAY[]::text[], NULL, NULL, NULL, true),
    ('cancelled', 'Cancelled', 9, ARRAY[]::text[], NULL, NULL, NULL, true)
ON CONFLICT (name) DO UPDATE
SET display_label = EXCLUDED.display_label,
    rank = EXCLUDED.rank,
    owner_roles = EXCLUDED.owner_roles,
    entry_gate_field = EXCLUDED.entry_gate_field,
    gate_skip_to = EXCLUDED.gate_skip_to,
    exit_signoff_field = EXCLUDED.exit_signoff_field,
    is_terminal = EXCLUDED.is_terminal;

INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles)
VALUES
    ('draft', 'analysis', 'release_draft', ARRAY['director', 'eric']::text[]),
    ('draft', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('backlog', 'analysis', 'route', ARRAY['director']::text[]),
    ('backlog', 'analysis', 'start_task', ARRAY['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector']::text[]),
    ('backlog', 'in_progress', 'route', ARRAY['director']::text[]),
    ('backlog', 'in_progress', 'start_work', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]),
    ('backlog', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('analysis', 'in_progress', 'route', ARRAY['director']::text[]),
    ('analysis', 'in_progress', 'start_work', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]),
    ('analysis', 'backlog', 'defer', ARRAY['director']::text[]),
    ('analysis', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('in_progress', 'inspection', 'submit_to_inspection', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]),
    ('in_progress', 'audit', 'submit_to_audit', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]),
    ('in_progress', 'analysis', 'request_commit_exempt', ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]),
    ('in_progress', 'analysis', 'route', ARRAY['director']::text[]),
    ('in_progress', 'backlog', 'defer', ARRAY['director']::text[]),
    ('in_progress', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('inspection', 'audit', 'inspector_sign_off', ARRAY['inspector']::text[]),
    ('inspection', 'audit', 'route', ARRAY['director']::text[]),
    ('inspection', 'in_progress', 'inspector_kick_back', ARRAY['inspector']::text[]),
    ('inspection', 'in_progress', 'route', ARRAY['director']::text[]),
    ('inspection', 'backlog', 'defer', ARRAY['director']::text[]),
    ('inspection', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('audit', 'eric_review', 'audit_sign_off', ARRAY['audit']::text[]),
    ('audit', 'director_review', 'audit_sign_off', ARRAY['audit']::text[]),
    ('audit', 'in_progress', 'audit_kick_back', ARRAY['audit']::text[]),
    ('audit', 'in_progress', 'route', ARRAY['director']::text[]),
    ('audit', 'analysis', 'route', ARRAY['director']::text[]),
    ('audit', 'backlog', 'defer', ARRAY['director']::text[]),
    ('audit', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('eric_review', 'inspection', 'route', ARRAY['director']::text[]),
    ('eric_review', 'director_review', 'eric_sign_off', ARRAY['eric']::text[]),
    ('eric_review', 'audit', 'route', ARRAY['director']::text[]),
    ('eric_review', 'analysis', 'eric_reopen', ARRAY['eric']::text[]),
    ('eric_review', 'analysis', 'route', ARRAY['director']::text[]),
    ('eric_review', 'backlog', 'defer', ARRAY['director']::text[]),
    ('eric_review', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('director_review', 'done', 'mark_done', ARRAY['director']::text[]),
    ('director_review', 'in_progress', 'route', ARRAY['director']::text[]),
    ('director_review', 'analysis', 'eric_reopen', ARRAY['eric']::text[]),
    ('director_review', 'analysis', 'route', ARRAY['director']::text[]),
    ('director_review', 'backlog', 'defer', ARRAY['director']::text[]),
    ('director_review', 'cancelled', 'cancel', ARRAY['director']::text[]),
    ('done', 'analysis', 'eric_reopen', ARRAY['eric']::text[]),
    ('done', 'analysis', 'route', ARRAY['director']::text[]),
    ('done', 'backlog', 'defer', ARRAY['director']::text[]),
    ('cancelled', 'analysis', 'route', ARRAY['director']::text[]),
    ('cancelled', 'backlog', 'defer', ARRAY['director']::text[])
ON CONFLICT (from_stage, to_stage, action_name) DO UPDATE
SET allowed_roles = EXCLUDED.allowed_roles;

DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'director',
        'eric',
        'ops',
        'app',
        'audit',
        'inspector',
        'perf',
        'research',
        'main',
        'ticket_board_service',
        'ticket_board_listener'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'GRANT SELECT ON ticket_board.workflow_stages, ticket_board.workflow_transitions TO %I',
                role_name
            );
        END IF;
    END LOOP;
END;
$$;
