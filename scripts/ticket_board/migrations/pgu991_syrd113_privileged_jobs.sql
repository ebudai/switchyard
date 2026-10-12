-- SYRD-113 slice B: the board service records root's privileged jobs on their
-- originating tickets. schema.sql carries the same text.
BEGIN;

-- SYRD-113: a privileged action's job, as root recorded it, on its originating
-- ticket. Root's helper writes each run to its rollout journal (slice A); the
-- board service reads those root-owned records and records each job here. A
-- job is keyed by its attempt id, which root assigns once, so a record read
-- again -- after a restart, an outage, or on the next pass -- changes nothing.
CREATE TABLE IF NOT EXISTS ticket_board.privileged_jobs (
    job_id text PRIMARY KEY CHECK (job_id ~ '^[0-9]{4,}-[0-9]{8}T[0-9]{6}Z$'),
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9-]{0,63}$'),
    action_id text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(arguments) = 'object'),
    requester_role text NOT NULL DEFAULT '',
    release_commit text NOT NULL DEFAULT '' CHECK (release_commit = '' OR release_commit ~ '^[0-9a-f]{40}$'),
    state text NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'timed_out')),
    exit_status integer,
    started_at text NOT NULL DEFAULT '',
    finished_at text NOT NULL DEFAULT '',
    record_dir text NOT NULL,
    artifacts jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_sha256 text NOT NULL DEFAULT '',
    capture jsonb NOT NULL DEFAULT '{}'::jsonb,
    final_lines jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(final_lines) = 'array'),
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS privileged_jobs_ticket_idx ON ticket_board.privileged_jobs (ticket_id, job_id);

-- The one write. Only the board service's reconciler may make it: the
-- service's database role AND the reconciler's own session setting, which no
-- request path sets -- every role request, the listener and the tenant report
-- token reach the database by other functions. A running job is recorded as
-- running; a job seen finished is recorded once, and that first recording is
-- the only one that writes the ticket's event and wakes anybody. A finished
-- job never changes again.
CREATE OR REPLACE FUNCTION ticket_board.record_privileged_job(p_job jsonb)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    v_id text := p_job->>'job_id';
    v_state text := p_job->>'state';
    v_existing ticket_board.privileged_jobs%ROWTYPE;
    v_seen boolean;
    v_ticket ticket_board.tickets%ROWTYPE;
    v_outcome text;
    v_event text;
    v_target text;
    v_targets text[];
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service'
       OR current_setting('ticket_board.privileged_job_reconciler', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'only the board service''s privileged-job reconciler records a privileged job'
            USING ERRCODE = '42501';
    END IF;
    IF v_state IS NULL OR v_state NOT IN ('running', 'succeeded', 'failed', 'timed_out') THEN
        RAISE EXCEPTION 'privileged job % has no recordable state: %', v_id, v_state;
    END IF;
    SELECT * INTO v_ticket FROM ticket_board.tickets WHERE id = p_job->>'ticket' FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'privileged job % names no ticket on this board: %', v_id, p_job->>'ticket';
    END IF;
    SELECT * INTO v_existing FROM ticket_board.privileged_jobs WHERE job_id = v_id FOR UPDATE;
    v_seen := FOUND;
    IF v_seen THEN
        IF v_existing.ticket_id <> v_ticket.id OR v_existing.action <> p_job->>'action' THEN
            RAISE EXCEPTION 'privileged job % was recorded for % (%), not % (%)',
                v_id, v_existing.ticket_id, v_existing.action, v_ticket.id, p_job->>'action';
        END IF;
        IF v_existing.state <> 'running' OR v_state = 'running' THEN
            RETURN 'unchanged';
        END IF;
        UPDATE ticket_board.privileged_jobs SET
            state = v_state,
            exit_status = (p_job->>'exit_status')::integer,
            finished_at = coalesce(p_job->>'finished_at', ''),
            artifacts = coalesce(p_job->'artifacts', '{}'::jsonb),
            result_sha256 = coalesce(p_job->>'result_sha256', ''),
            capture = coalesce(p_job->'capture', '{}'::jsonb),
            final_lines = coalesce(p_job->'final_lines', '[]'::jsonb),
            completed_at = clock_timestamp()
        WHERE job_id = v_id;
    ELSE
        INSERT INTO ticket_board.privileged_jobs (
            job_id, ticket_id, action, action_id, arguments, requester_role, release_commit, state,
            exit_status, started_at, finished_at, record_dir, artifacts, result_sha256, capture,
            final_lines, completed_at)
        VALUES (
            v_id, v_ticket.id, p_job->>'action', coalesce(p_job->>'action_id', ''),
            coalesce(p_job->'arguments', '{}'::jsonb), coalesce(p_job->>'requester_role', ''),
            coalesce(p_job->>'release_commit', ''), v_state, (p_job->>'exit_status')::integer,
            coalesce(p_job->>'started_at', ''), coalesce(p_job->>'finished_at', ''), p_job->>'record_dir',
            coalesce(p_job->'artifacts', '{}'::jsonb), coalesce(p_job->>'result_sha256', ''),
            coalesce(p_job->'capture', '{}'::jsonb), coalesce(p_job->'final_lines', '[]'::jsonb),
            CASE WHEN v_state = 'running' THEN NULL ELSE clock_timestamp() END);
    END IF;
    IF v_state = 'running' THEN
        RETURN 'running';
    END IF;

    v_outcome := CASE v_state
        WHEN 'succeeded' THEN 'succeeded'
        WHEN 'timed_out' THEN 'timed out and was stopped'
        ELSE format('failed with exit status %s', coalesce(p_job->>'exit_status', 'unknown')) END;
    v_event := format(
        E'Privileged job %s: %s %s.\n'
        || E'- Asked by %s, with %s.\n'
        || E'- Root''s record: %s (stdout.log sha256 %s, stderr.log sha256 %s; result.json sha256 %s).\n'
        || E'- Last lines, redacted:\n```\n%s\n```',
        v_id, p_job->>'action', v_outcome,
        coalesce(nullif(p_job->>'requester_role', ''), 'an unrecorded role'),
        coalesce((SELECT string_agg(format('%s=%s', key, value), ' ' ORDER BY key)
                  FROM jsonb_each_text(coalesce(p_job->'arguments', '{}'::jsonb))), 'no arguments'),
        p_job->>'record_dir',
        coalesce(p_job->'artifacts'->>'stdout.log', '?'), coalesce(p_job->'artifacts'->>'stderr.log', '?'),
        coalesce(nullif(p_job->>'result_sha256', ''), '?'),
        coalesce((SELECT string_agg(line, E'\n' ORDER BY n)
                  FROM jsonb_array_elements_text(coalesce(p_job->'final_lines', '[]'::jsonb))
                       WITH ORDINALITY AS l(line, n)), '(no output)'));
    PERFORM ticket_board.append_ticket_comment(v_ticket.id, 'privileged-job', v_event, v_state <> 'succeeded');

    -- Who acts next: the ticket's owner; on a failure or a timeout, the
    -- controller as well. Only a role with a pane is woken -- a notice to one
    -- without is requeued for ever (SYRD-549).
    v_targets := ARRAY[ticket_board.transition_target_role(v_ticket.state, v_ticket.assignee)];
    IF v_state <> 'succeeded' THEN
        v_targets := v_targets || ARRAY(
            SELECT r.name FROM ticket_board.workflow_roles r
            WHERE ticket_board.role_controls_project(r.name) ORDER BY r.name LIMIT 1);
        IF ticket_board.declared_workflow() IS NULL THEN
            v_targets := v_targets || 'director'::text;
        END IF;
    END IF;
    FOR v_target IN
        SELECT DISTINCT target FROM unnest(v_targets) AS target
        WHERE target IS NOT NULL AND ticket_board.wait_target_kind(target) = 'pane'
    LOOP
        PERFORM ticket_board.enqueue_notification(
            v_ticket.id,
            'ticket_update',
            v_target,
            format('%s -- %s: privileged job %s, %s %s. Root''s record and its last lines are on the ticket.',
                   v_ticket.id, v_ticket.title, v_id, p_job->>'action', v_outcome),
            jsonb_build_object(
                'kind', 'ticket_update', 'id', v_ticket.id, 'title', v_ticket.title, 'state', v_ticket.state,
                'assignee', v_ticket.assignee, 'updated_at', v_ticket.updated_at,
                'ticket_number', v_ticket.ticket_number, 'target_role', v_target,
                'privileged_job', v_id, 'job_state', v_state),
            format('privileged-job:%s:%s', v_id, v_target));
    END LOOP;
    RETURN 'completed';
END;
$$;

-- A ticket's privileged jobs, oldest first, for its JSON and its page.
CREATE OR REPLACE FUNCTION ticket_board.ticket_privileged_jobs(p_ticket text)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'job_id', j.job_id, 'action', j.action, 'state', j.state, 'exit_status', j.exit_status,
        'requester_role', j.requester_role, 'release_commit', j.release_commit, 'arguments', j.arguments,
        'started_at', j.started_at, 'finished_at', j.finished_at, 'record_dir', j.record_dir,
        'artifacts', j.artifacts, 'result_sha256', j.result_sha256, 'capture', j.capture,
        'final_lines', j.final_lines) ORDER BY j.job_id), '[]'::jsonb)
    FROM ticket_board.privileged_jobs j
    WHERE j.ticket_id = p_ticket;
$$;

-- The service's set of jobs already finished, so a pass does not re-read them.
CREATE OR REPLACE FUNCTION ticket_board.finished_privileged_jobs()
RETURNS SETOF text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT job_id FROM ticket_board.privileged_jobs WHERE state <> 'running';
$$;

REVOKE ALL ON ticket_board.privileged_jobs FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_privileged_job(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.ticket_privileged_jobs(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.finished_privileged_jobs() FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        GRANT EXECUTE ON FUNCTION ticket_board.record_privileged_job(jsonb) TO ticket_board_service;
        GRANT EXECUTE ON FUNCTION ticket_board.ticket_privileged_jobs(text) TO ticket_board_service;
        GRANT EXECUTE ON FUNCTION ticket_board.finished_privileged_jobs() TO ticket_board_service;
    END IF;
END $$;

COMMIT;
