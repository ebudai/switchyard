ALTER TABLE ticket_board.workflow_transitions
    ADD COLUMN IF NOT EXISTS owner_scoped boolean NOT NULL DEFAULT false;

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

UPDATE ticket_board.workflow_transitions
SET owner_scoped = action_name IN ('request_commit_exempt', 'start_task', 'submit_to_inspection');

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
              OR p_actor = 'director'
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
    actor := ticket_board.require_actor(ARRAY['director'], 'route');
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'eric_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'agent', 'director', 'research') THEN
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
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'route',
        ticket_board.current_app_actor(),
        current_assignee,
        true
    );

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
    actor := ticket_board.require_actor(ARRAY['director', 'eric'], 'release_draft');
    SELECT tickets.state
    INTO current_state
    FROM ticket_board.tickets
    WHERE tickets.id = release_draft.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF current_state <> 'draft' THEN
        RAISE EXCEPTION 'release_draft requires a draft ticket';
    END IF;
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'release_draft',
        ticket_board.current_app_actor(),
        NULL,
        true
    );

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
    IF new_state NOT IN ('draft', 'backlog', 'analysis', 'in_progress', 'inspection', 'audit', 'eric_review', 'director_review', 'done', 'cancelled') THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF assignee NOT IN ('unassigned', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'agent', 'director', 'research') THEN
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'start_work',
        ticket_board.current_app_actor(),
        NULL,
        true
    );
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
BEGIN
    actor := ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'submit_to_audit');
    SELECT tickets.commit_exempt, tickets.last_rejected_commit
    INTO ticket_commit_exempt, ticket_last_rejected_commit
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_audit.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'submit_to_audit',
        ticket_board.current_app_actor(),
        NULL,
        true
    );
    UPDATE ticket_board.tickets
    SET state = 'audit',
        commit_hash = normalized_commit,
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
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'submit_to_inspection');
    actor := ticket_board.current_app_actor();
    SELECT tickets.state, tickets.assignee, tickets.needs_inspection
    INTO ticket_state, ticket_assignee, ticket_needs_inspection
    FROM ticket_board.tickets
    WHERE tickets.id = submit_to_inspection.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF ticket_state <> 'in_progress' THEN
        RAISE EXCEPTION 'submit_to_inspection requires an in_progress ticket';
    END IF;
    IF ticket_assignee <> actor THEN
        RAISE EXCEPTION '% cannot submit_to_inspection for ticket assigned to %', actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'submit_to_inspection',
        actor,
        ticket_assignee,
        true
    );
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
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'request_commit_exempt');
    actor := ticket_board.current_app_actor();
    IF actor NOT IN ('main', 'app', 'ops', 'perf', 'research') THEN
        RAISE EXCEPTION '% cannot call request_commit_exempt', actor
            USING ERRCODE = '42501';
    END IF;
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
    IF ticket_assignee <> actor THEN
        RAISE EXCEPTION '% cannot call request_commit_exempt for ticket assigned to %', actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'request_commit_exempt',
        actor,
        ticket_assignee,
        true
    );
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
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector'],
        'start_task'
    );
    comment_actor := ticket_board.current_app_actor();
    SELECT tickets.state, tickets.assignee
    INTO ticket_state, ticket_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = start_task.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF ticket_assignee <> comment_actor AND comment_actor <> 'director' THEN
        RAISE EXCEPTION '% cannot call start_task for ticket assigned to %', comment_actor, ticket_assignee
            USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'start_task',
        comment_actor,
        ticket_assignee,
        true
    );
    IF ticket_state <> 'backlog' THEN
        RAISE EXCEPTION 'start_task requires a backlog ticket';
    END IF;
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
    actor text;
    comment_actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['audit'], 'audit_sign_off');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'audit_sign_off',
        comment_actor,
        NULL,
        true
    );
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'audit_kick_back',
        comment_actor,
        NULL,
        true
    );
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'audit_kick_back',
        comment_actor,
        NULL,
        true
    );
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

CREATE OR REPLACE FUNCTION ticket_board.inspector_sign_off(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['inspector'], 'inspector_sign_off');
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'inspector_sign_off',
        ticket_board.current_app_actor(),
        NULL,
        true
    );
    UPDATE ticket_board.tickets
    SET inspector_signoff = true,
        state = 'audit'
    WHERE tickets.id = inspector_sign_off.id
      AND tickets.state = 'inspection';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inspection ticket not found: %', id;
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'inspector_kick_back',
        comment_actor,
        NULL,
        true
    );
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'inspector_kick_back',
        comment_actor,
        NULL,
        true
    );
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

CREATE OR REPLACE FUNCTION ticket_board.eric_sign_off(
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
BEGIN
    actor := ticket_board.require_actor(ARRAY['eric'], 'eric_sign_off');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'eric_sign_off',
        comment_actor,
        NULL,
        true
    );
    IF btrim(coalesce(comment_text, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, comment_text);
    END IF;
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
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'eric_reopen',
        comment_actor,
        NULL,
        true
    );
    IF btrim(coalesce(reason, '')) <> '' THEN
        PERFORM ticket_board.append_ticket_comment(id, comment_actor, reason);
    END IF;
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
    ticket_commit_exempt boolean;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'mark_done');
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'mark_done',
        ticket_board.current_app_actor(),
        NULL,
        true
    );
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
    actor := ticket_board.require_actor(ARRAY['director'], 'defer');
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'defer',
        ticket_board.current_app_actor(),
        NULL,
        true
    );
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
    actor := ticket_board.require_actor(ARRAY['director'], 'cancel');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.log_workflow_transition_rbac_shadow_mismatch(
        id,
        'cancel',
        comment_actor,
        NULL,
        true
    );
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
                'GRANT SELECT ON ticket_board.workflow_transition_rbac_shadow_log TO %I',
                role_name
            );
        END IF;
    END LOOP;
END;
$$;
