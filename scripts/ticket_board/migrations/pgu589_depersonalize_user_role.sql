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
