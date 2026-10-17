-- SYRD-598: the User's confirmed action in the board UI may use the control
-- role's operation permission, for that one action, and is recorded as the
-- User's.
--
-- The board UI used to act as the Director by saying so: its requests named
-- `director` in a header the HTTP server trusts. This lends the permission
-- instead, through a grant the server mints here when the User confirms a
-- concrete action, and consumes in the transaction that performs it:
--
--   * a grant is single-use, short-lived, and bound to one ticket, one
--     operation, the exact payload, and the ticket as the User saw it;
--   * only operations with a meaningful UI control are lendable, never an
--     approval, a relayed decision, a workflow bypass or a review gate;
--   * the Director-only checks accept the User only for the operation a grant
--     consumed in this same transaction names, so changing only the HTTP layer
--     does not get past them;
--   * the decision stays the User's: comments, field audits and "changed by"
--     name the User, and the ticket's history says the board UI lent the
--     control role's operation permission.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.ui_delegations (
    id bigserial PRIMARY KEY,
    -- The token is never stored; its digest is how a later request finds the grant.
    token_sha256 text NOT NULL UNIQUE,
    -- The board page session it was issued to, likewise as a digest.
    session_sha256 text NOT NULL,
    ticket_id text NOT NULL,
    operation text NOT NULL,
    payload jsonb NOT NULL,
    -- The ticket as the User saw it when confirming (ui_ticket_version).
    ticket_version text NOT NULL,
    decided_by text NOT NULL,
    -- The role whose operation permission is lent.
    authority text NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    consumed_txid bigint
);
CREATE INDEX IF NOT EXISTS ui_delegations_consumed_txid ON ticket_board.ui_delegations (consumed_txid)
    WHERE consumed_txid IS NOT NULL;

-- The role whose operation permission a board-UI action may use: the declared
-- control role, found by its capabilities (SYRD-49), or a legacy board's director.
CREATE OR REPLACE FUNCTION ticket_board.ui_delegation_control_role()
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN 'director' ELSE (
        SELECT r.name FROM ticket_board.workflow_roles r
        WHERE (r.definition->>'active')::boolean AND ticket_board.role_controls_project(r.name)
        ORDER BY r.name LIMIT 1) END;
$$;

-- Whether an operation on a ticket may be lent at all. Only what the board UI has
-- a meaningful control for, and never an approval, a relayed decision, a move
-- that bypasses the workflow (force_move, override_move), a review gate, size
-- review, or the workflow itself.
CREATE OR REPLACE FUNCTION ticket_board.ui_delegable(p_ticket_id text, p_operation text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE
        WHEN ticket_board.declared_workflow() IS NULL THEN
            p_operation IN ('route', 'reassign', 'set_blockers', 'release_external_blocker', 'set_manually_controlled',
                            'dismiss_notification', 'cancel', 'defer', 'mark_done', 'merge', 'withdraw_signoff')
        ELSE
            p_operation IN ('reassign', 'set_blockers', 'release_external_blocker', 'set_manually_controlled',
                            'recover_stalled_ticket', 'dismiss_notification', 'resolve_publication', 'merge',
                            'withdraw_signoff')
            OR EXISTS (
                SELECT FROM ticket_board.tickets t,
                     jsonb_array_elements(ticket_board.declared_workflow()->'transitions') tr
                WHERE t.id = p_ticket_id AND tr->>'from' = t.state AND tr->>'action' = p_operation
                  AND tr->'actors' ? ticket_board.ui_delegation_control_role()
                  AND coalesce(tr->>'primitive', '') <> 'approve'
                  AND tr->>'relays_decision_of' IS NULL
                  -- A move that names a commit is measured first, in a transaction of its own (SYRD-541).
                  AND NOT coalesce((tr->>'require_commit')::boolean, false))
    END;
$$;

-- The SQL authority checks an operation runs: releasing an external blocker
-- is checked as setting blockers.
CREATE OR REPLACE FUNCTION ticket_board.ui_delegation_covers(p_operation text, p_action text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT p_action = p_operation OR (p_operation = 'release_external_blocker' AND p_action = 'set_blockers');
$$;

-- The ticket as the board shows it: its row, its blockers, the role it awaits
-- and the workflow revision. Any change since the User confirmed refuses the action.
CREATE OR REPLACE FUNCTION ticket_board.ui_ticket_version(p_ticket_id text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT md5(concat_ws('|',
        (SELECT to_jsonb(t)::text FROM ticket_board.tickets t WHERE t.id = p_ticket_id),
        (SELECT coalesce(jsonb_agg(to_jsonb(b) ORDER BY to_jsonb(b)::text), '[]'::jsonb)::text
           FROM ticket_board.ticket_blockers b WHERE b.ticket_id = p_ticket_id),
        (SELECT coalesce(ns.awaiting_role, '') FROM ticket_board.ticket_notification_state ns
          WHERE ns.ticket_id = p_ticket_id),
        (SELECT revision::text FROM ticket_board.workflow_configuration WHERE singleton)));
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_ui_delegation_caller()
RETURNS void LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service'
       OR nullif(current_setting('ticket_board.caller_role', true), '') IS DISTINCT FROM 'user' THEN
        RAISE EXCEPTION 'board UI delegation is only for the User acting through the board service'
            USING ERRCODE = '42501';
    END IF;
END;
$$;

-- Minted when the User confirms a concrete action in the board UI. Returns when it expires.
CREATE OR REPLACE FUNCTION ticket_board.mint_ui_delegation(
    p_token text, p_session text, p_ticket_id text, p_operation text, p_payload jsonb, p_ticket_version text,
    p_ttl_seconds integer DEFAULT 120)
RETURNS timestamptz LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    authority text := ticket_board.ui_delegation_control_role();
    expires timestamptz := clock_timestamp() + make_interval(secs => least(greatest(coalesce(p_ttl_seconds, 120), 1), 300));
BEGIN
    PERFORM ticket_board.require_ui_delegation_caller();
    IF length(coalesce(p_token, '')) < 32 OR length(coalesce(p_session, '')) < 32
       OR jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'a board UI delegation needs a token, a board session and a payload object'
            USING ERRCODE = '22023';
    END IF;
    -- It lends permission for one ticket, so what it acts on must be that ticket's.
    IF p_payload ? 'commit_hash' THEN
        RAISE EXCEPTION 'a board UI delegation names no commit' USING ERRCODE = '42501';
    END IF;
    IF p_operation = 'resolve_publication' AND NOT EXISTS (
        SELECT FROM ticket_board.publication_requests pr
        WHERE pr.id::text = p_payload->>'request_id' AND pr.ticket_id = p_ticket_id) THEN
        RAISE EXCEPTION 'publication request % is not %''s', p_payload->>'request_id', p_ticket_id
            USING ERRCODE = '42501';
    END IF;
    IF p_operation = 'merge' AND NOT EXISTS (
        SELECT FROM ticket_board.tickets WHERE id = upper(coalesce(p_payload->>'target_id', '')) AND id <> p_ticket_id) THEN
        RAISE EXCEPTION 'a merge names another ticket to merge % into', p_ticket_id USING ERRCODE = '22023';
    END IF;
    IF p_operation = 'withdraw_signoff' AND (btrim(coalesce(p_payload->>'reason', '')) = ''
       OR NOT coalesce(ticket_board.signoff_raised(p_ticket_id, p_payload->>'signoff'), false)) THEN
        RAISE EXCEPTION 'withdrawing a sign-off names one that is given, and why' USING ERRCODE = '22023';
    END IF;
    IF p_operation = 'dismiss_notification' AND NOT (
        upper(coalesce(p_payload->>'ticket_id', '')) = p_ticket_id
        OR EXISTS (SELECT FROM ticket_board.ticket_notification_queue q
                   WHERE q.id::text = p_payload->>'notification_id' AND q.ticket_id = p_ticket_id)) THEN
        RAISE EXCEPTION 'that notification is not %''s', p_ticket_id USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.tickets WHERE id = p_ticket_id) THEN
        RAISE EXCEPTION 'ticket % not found', p_ticket_id USING ERRCODE = '22023';
    END IF;
    IF authority IS NULL OR NOT ticket_board.ui_delegable(p_ticket_id, p_operation) THEN
        RAISE EXCEPTION 'the board UI cannot lend % for %', p_operation, p_ticket_id USING ERRCODE = '42501';
    END IF;
    IF p_ticket_version IS DISTINCT FROM ticket_board.ui_ticket_version(p_ticket_id) THEN
        RAISE EXCEPTION '% changed since it was shown; reload it and confirm again', p_ticket_id USING ERRCODE = '40001';
    END IF;
    INSERT INTO ticket_board.ui_delegations
        (token_sha256, session_sha256, ticket_id, operation, payload, ticket_version, decided_by, authority, expires_at)
    VALUES (encode(sha256(convert_to(p_token, 'UTF8')), 'hex'), encode(sha256(convert_to(p_session, 'UTF8')), 'hex'),
            p_ticket_id, p_operation, p_payload, p_ticket_version, 'user', authority, expires);
    RETURN expires;
END;
$$;

-- Consumed in the transaction that performs the action, before it runs. Once.
CREATE OR REPLACE FUNCTION ticket_board.consume_ui_delegation(
    p_token text, p_session text, p_ticket_id text, p_operation text, p_payload jsonb)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    grant_row ticket_board.ui_delegations%ROWTYPE;
BEGIN
    PERFORM ticket_board.require_ui_delegation_caller();
    SELECT * INTO grant_row FROM ticket_board.ui_delegations
    WHERE token_sha256 = encode(sha256(convert_to(coalesce(p_token, ''), 'UTF8')), 'hex') FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown board UI confirmation' USING ERRCODE = '42501';
    END IF;
    IF grant_row.session_sha256 IS DISTINCT FROM encode(sha256(convert_to(coalesce(p_session, ''), 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'this board UI confirmation belongs to another board session' USING ERRCODE = '42501';
    END IF;
    IF grant_row.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'this board UI confirmation was already used' USING ERRCODE = '42501';
    END IF;
    IF clock_timestamp() >= grant_row.expires_at THEN
        RAISE EXCEPTION 'this board UI confirmation expired; confirm again' USING ERRCODE = '42501';
    END IF;
    IF grant_row.ticket_id IS DISTINCT FROM p_ticket_id OR grant_row.operation IS DISTINCT FROM p_operation THEN
        RAISE EXCEPTION 'this board UI confirmation was for % on %, not % on %',
            grant_row.operation, grant_row.ticket_id, p_operation, p_ticket_id USING ERRCODE = '42501';
    END IF;
    IF grant_row.payload IS DISTINCT FROM p_payload THEN
        RAISE EXCEPTION 'the action changed since it was confirmed' USING ERRCODE = '42501';
    END IF;
    IF grant_row.ticket_version IS DISTINCT FROM ticket_board.ui_ticket_version(p_ticket_id) THEN
        RAISE EXCEPTION '% changed since it was confirmed; reload it and confirm again', p_ticket_id
            USING ERRCODE = '40001';
    END IF;
    IF grant_row.authority IS DISTINCT FROM ticket_board.ui_delegation_control_role()
       OR NOT ticket_board.ui_delegable(p_ticket_id, p_operation) THEN
        RAISE EXCEPTION 'the board UI can no longer lend % for %', p_operation, p_ticket_id USING ERRCODE = '42501';
    END IF;
    UPDATE ticket_board.ui_delegations SET consumed_at = clock_timestamp(), consumed_txid = txid_current()
    WHERE id = grant_row.id;
    -- The record says who decided, and whose operation permission was used.
    PERFORM ticket_board.append_ticket_comment(
        p_ticket_id, 'user',
        format('Through the board UI the User confirmed %s %s, using the operation permission of %s. '
               || 'The decision is the User''s.', p_operation, p_payload::text, grant_row.authority));
    RETURN grant_row.id;
END;
$$;

-- Whether a ticket's named sign-off is given now; NULL when the board has no such sign-off.
CREATE OR REPLACE FUNCTION ticket_board.signoff_raised(p_ticket_id text, p_signoff text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE
        WHEN ticket_board.declared_workflow() IS NULL THEN
            CASE WHEN p_signoff IN ('inspector_signoff', 'audit_signoff', 'user_signoff')
                 THEN (to_jsonb(t)->>p_signoff)::boolean END
        WHEN ticket_board.declared_workflow()->'flags'->p_signoff->>'kind' = 'signoff' THEN
            ticket_board.workflow_flag(to_jsonb(t), p_signoff, ticket_board.declared_workflow())
    END
    FROM ticket_board.tickets t WHERE t.id = p_ticket_id;
$$;

-- Withdraw one sign-off, by name, with why. Only the control role (or the User's
-- confirmed board-UI action lending its permission) may, and it can only lower:
-- nothing here raises an approval, moves the ticket, or touches any other flag.
CREATE OR REPLACE FUNCTION ticket_board.withdraw_signoff(p_ticket_id text, p_signoff text, p_reason text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    cfg jsonb := ticket_board.declared_workflow();
    caller text := ticket_board.current_app_actor();
    reason text := btrim(coalesce(p_reason, ''));
    t ticket_board.tickets%ROWTYPE;
    doc jsonb;
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service'
       OR NOT (ticket_board.role_controls_project(caller) OR coalesce(ticket_board.role_controls_project(
               ticket_board.ui_delegated_authority('withdraw_signoff', p_ticket_id)), false)) THEN
        RAISE EXCEPTION 'only the control role may withdraw a sign-off, not %', caller USING ERRCODE = '42501';
    END IF;
    IF reason = '' THEN
        RAISE EXCEPTION 'withdrawing a sign-off requires a reason';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % not found', p_ticket_id;
    END IF;
    IF ticket_board.signoff_raised(p_ticket_id, p_signoff) IS NULL THEN
        RAISE EXCEPTION 'this board has no sign-off named %', p_signoff;
    END IF;
    IF NOT ticket_board.signoff_raised(p_ticket_id, p_signoff) THEN
        RAISE EXCEPTION '% has no % to withdraw', p_ticket_id, p_signoff;
    END IF;
    doc := CASE WHEN cfg IS NULL THEN jsonb_set(to_jsonb(t), ARRAY[p_signoff], 'false'::jsonb)
                ELSE ticket_board.set_workflow_flag(to_jsonb(t), p_signoff, false) END;
    t := jsonb_populate_record(t, doc);
    PERFORM set_config('ticket_board.signoff_withdrawal', p_ticket_id || ':' || p_signoff, true);
    UPDATE ticket_board.tickets
    SET inspector_signoff = t.inspector_signoff, audit_signoff = t.audit_signoff, user_signoff = t.user_signoff,
        workflow_flags = t.workflow_flags
    WHERE id = p_ticket_id;
    PERFORM set_config('ticket_board.signoff_withdrawal', '', true);
    PERFORM ticket_board.append_ticket_comment(p_ticket_id, caller, format('Withdrew %s: %s', p_signoff, reason));
END;
$$;

-- The role whose permission a delegation consumed in this transaction lends to
-- the User for this authority check, or NULL.
CREATE OR REPLACE FUNCTION ticket_board.ui_delegated_authority(p_action text, p_ticket_id text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT g.authority FROM ticket_board.ui_delegations g
    WHERE g.consumed_txid = txid_current()
      AND nullif(current_setting('ticket_board.caller_role', true), '') = g.decided_by
      AND ticket_board.ui_delegation_covers(g.operation, p_action)
      AND (p_ticket_id IS NULL OR g.ticket_id = p_ticket_id)
    ORDER BY g.id DESC LIMIT 1;
$$;

-- The authority checks that lend the control role's permission, and the owner's notice that
-- names the board UI. schema.sql's copies are this text.

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
        -- SYRD-598: the User's confirmed board-UI action may use the control
        -- role's permission, for the operation a delegation consumed in this
        -- transaction names, and for nothing else.
        IF actor <> 'ticket_board_service' OR NOT EXISTS (
            SELECT FROM ticket_board.workflow_roles r
            WHERE r.name IN (caller_role, ticket_board.ui_delegated_authority(p_action))
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
       AND NOT coalesce(ticket_board.ui_delegated_authority(p_action) = ANY(p_allowed_roles), false)  -- SYRD-598
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
    -- SYRD-598: or the control role's, lent by the User's confirmed board-UI action on this ticket.
    ) AND NOT coalesce(ticket_board.workflow_transition_rbac_allowed_config(
        p_action_name,
        ticket_board.ui_delegated_authority(p_action_name, p_ticket_id),
        resolved_assignee
    ), false) THEN
        RAISE EXCEPTION 'role % cannot call %', actor, p_action_name
            USING ERRCODE = '42501';
    END IF;

    RETURN actor;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action(id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE authority text := ticket_board.ui_delegated_authority(action, id);
BEGIN
    IF authority IS NOT NULL THEN
        -- SYRD-598: checked against the control role whose permission the
        -- User's confirmed board-UI action used, and narrated as the User.
        PERFORM ticket_board.perform_workflow_action_as(authority, ticket_board.current_app_actor(), id, action, payload);
        RETURN;
    END IF;
    PERFORM ticket_board.perform_workflow_action_as(
        ticket_board.current_app_actor(), NULL, id, action, payload);
END;
$$;

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
    IF ticket_board.current_actor_role() <> 'ticket_board_service' OR (actor IS DISTINCT FROM 'director'
       AND ticket_board.ui_delegated_authority('reassign', reassign.id) IS DISTINCT FROM 'director') THEN  -- SYRD-598
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

    -- SYRD-598: the declared update rule checks the role whose permission is used.
    PERFORM set_config('ticket_board.workflow_actor',
                       coalesce(ticket_board.ui_delegated_authority('reassign', reassign.id), ''), true);
    UPDATE ticket_board.tickets
    SET assignee = normalized_assignee
    WHERE tickets.id = reassign.id;
    PERFORM set_config('ticket_board.workflow_actor', '', true);

    SELECT * INTO ticket_row FROM ticket_board.tickets WHERE tickets.id = reassign.id;
    -- Serial focus held it. The queue announcement has already told the
    -- director, and the target implementer has no actionable work yet, so a
    -- second notification here would be wrong rather than merely noisy.
    IF ticket_row.state IS DISTINCT FROM current_state OR ticket_row.queued_for_assignee <> '' THEN
        RETURN;
    END IF;
    -- Held or blocked work is not actionable either. It stays reassigned; the
    -- existing unblock and release paths announce it when it becomes real work.
    -- SYRD-568: a ticket now waiting in its new owner's queue is not theirs to act on yet.
    IF coalesce(ticket_row.manually_controlled, false) OR ticket_board.ticket_has_unresolved_blockers(id)
       OR ticket_board.ticket_serial_waiting(id) THEN
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
    IF NOT ticket_board.role_controls_project(caller) AND NOT coalesce(ticket_board.role_controls_project(
           ticket_board.ui_delegated_authority('recover_stalled_ticket', p_ticket)), false) THEN  -- SYRD-598
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
    IF NOT ticket_board.role_controls_project(caller) AND NOT coalesce(ticket_board.role_controls_project(
           ticket_board.ui_delegated_authority('resolve_publication')), false) THEN  -- SYRD-598
        RAISE EXCEPTION 'only the control role may resolve a publication request, not %', caller
            USING ERRCODE = '42501';
    END IF;
    RETURN caller;
END;
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
    -- SYRD-598: withdrawing one named sign-off. ticket_board.withdraw_signoff,
    -- the only operation that opens this window, names one ticket and one
    -- sign-off for one statement, and only that sign-off may fall: no stage,
    -- owner or other flag moves through it, and nothing is raised.
    IF split_part(nullif(current_setting('ticket_board.signoff_withdrawal', true), ''), ':', 1) = previous.id THEN
        IF previous.state IS DISTINCT FROM proposed.state OR previous.assignee IS DISTINCT FROM proposed.assignee THEN
            RAISE EXCEPTION 'withdrawing a sign-off moves nothing' USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND NOT (flag.key = split_part(current_setting('ticket_board.signoff_withdrawal', true), ':', 2)
                        AND NOT ticket_board.workflow_flag(doc,flag.key,cfg)) THEN
                RAISE EXCEPTION 'withdrawing a sign-off changes only that sign-off, not %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
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
        --
        -- SYRD-568: only a pull policy still diverts, into its ready stage
        -- pinned to the busy author. A Director-dispatched board leaves the
        -- ticket where it was put, assigned, waiting in that implementer's
        -- queue; only the active ticket holds the slot.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_scheduling() IS NOT NULL
               AND ticket_board.declared_stage_kind(proposed.state)='implementation'
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
    IF tr IS NULL OR NOT ticket_board.transition_allows_actor(cfg,tr,actor) OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
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
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-568: placing blocked work in its implementer's queue promotes
       -- nothing either -- it holds no slot and is handed to nobody until its
       -- blockers resolve and it is the active ticket. Submitting it still is.
       -- A pull policy queues in its ready stage instead, as before.
       AND NOT (ticket_board.declared_stage_kind(tr->>'to') = 'implementation'
                AND ticket_board.declared_scheduling() IS NULL)
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    -- SYRD-568: a ticket waiting in its implementer's queue is not that
    -- implementer's work yet, so it cannot be submitted for review. Putting it
    -- down, routing it back or cancelling it are not submissions.
    IF source_stage->>'kind'='implementation'
       AND (ticket_board.declared_stage_kind(tr->>'to')='review'
            OR coalesce((tr->>'require_commit')::boolean,false) OR coalesce((tr->>'allow_no_code')::boolean,false))
       AND ticket_board.ticket_serial_waiting(previous.id) THEN
        RAISE EXCEPTION '% is waiting in %''s queue behind %; it can be submitted once it is the active ticket',
            previous.id, previous.assignee, coalesce(ticket_board.ticket_current_reserved_ticket(previous.assignee),'its blockers')
            USING ERRCODE='42501'; END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    -- A declared return out of review hands the work back: it needs no
    -- verdict, and the sign-off stays exactly as it was (SYRD-536). Its own
    -- branch, so SYRD-263's parking exemption below is unchanged.
    IF tr->>'primitive' NOT IN ('approve','return','reopen')
       AND ticket_board.declared_review_return(cfg, previous.state, tr->>'to') THEN
        NULL;
    ELSIF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
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
    -- SYRD-568: on a Director-dispatched board a move into an implementation
    -- stage lands there, assigned, even when that implementer already has
    -- active work, and waits in their queue until it is the active ticket. A
    -- pull policy keeps its ready stage as the queue (SYRD-539): there the
    -- move is still diverted, pinned to the busy author.
    IF ticket_board.declared_scheduling() IS NOT NULL
       AND dest->>'kind'='implementation' AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
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
    -- SYRD-598: the User decided; the board UI lent the control role's operation permission.
    IF EXISTS (SELECT FROM ticket_board.ui_delegations g WHERE g.consumed_txid = txid_current()
               AND g.decided_by = actor) THEN
        actor := actor || ' (through the board UI, using the control role''s operation permission)';
    END IF;
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
    -- SYRD-582: a ticket queued behind its implementer's active work is not
    -- their work yet. A comment, edit or reassignment on it woke them with it
    -- anyway (Otto's OTTO-134: the Director's routing comment arrived as work
    -- the board then refused to accept). It is handed over once, when it
    -- becomes active, and the ticket it hands over carries every change. A
    -- ticket held back by its own blocker still tells its owner what changed,
    -- as before: it cannot be submitted, and the change may be what frees it.
    IF ticket_board.ticket_serial_waiting(p_ticket_id)
       AND NOT ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
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

REVOKE ALL ON ticket_board.ui_delegations FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ui_delegation_control_role() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ui_delegable(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ui_ticket_version(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_ui_delegation_caller() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.mint_ui_delegation(text, text, text, text, jsonb, text, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.consume_ui_delegation(text, text, text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ui_delegated_authority(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.signoff_raised(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.withdraw_signoff(text, text, text) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.ui_delegations TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ui_delegable(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ui_ticket_version(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.mint_ui_delegation(text, text, text, text, jsonb, text, integer) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.consume_ui_delegation(text, text, text, text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.withdraw_signoff(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.signoff_raised(text, text) TO ticket_board_service;

COMMIT;
