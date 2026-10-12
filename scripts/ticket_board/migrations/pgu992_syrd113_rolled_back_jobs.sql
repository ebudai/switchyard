-- SYRD-113 slice C: a privileged job can end rolled back. `deploy-release`
-- whose health gate failed after the deploy, and whose rollback restored the
-- previous release, exits 3, and root records the job as `rolled_back`. The
-- board accepts that state, says so in the ticket's event, and wakes the owner
-- and the controller as for any job that did not succeed. The state check is
-- rebuilt as the union of every state the board records. schema.sql carries
-- the same.
BEGIN;

ALTER TABLE ticket_board.privileged_jobs DROP CONSTRAINT IF EXISTS privileged_jobs_state_check;
ALTER TABLE ticket_board.privileged_jobs ADD CONSTRAINT privileged_jobs_state_check
    CHECK (state IN ('running', 'succeeded', 'failed', 'timed_out', 'rolled_back'));

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
    IF v_state IS NULL OR v_state NOT IN ('running', 'succeeded', 'failed', 'timed_out', 'rolled_back') THEN
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
        WHEN 'rolled_back' THEN 'failed its health gate after the deploy and was rolled back to the previous release'
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

COMMIT;
