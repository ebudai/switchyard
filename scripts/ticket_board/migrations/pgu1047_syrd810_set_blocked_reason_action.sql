-- SYRD-810 (pgu1047): set_blocked_reason authorizes under its own action name.
--
-- SYRD-723 (pgu1024) authorized a blocker explanation as `require_actor(<implementers>, 'edit_fields')`, while every
-- other caller of edit_fields names the roles that edit fields -- the Director among them. One action, two role lists:
-- the invariant ticket_board_workflow_config_equivalence_test holds (each named action has a single role list) was
-- broken, and its regex hid it. The Director's call (SYRD-810): keep one role list per action, and give
-- set_blocked_reason its own name.
--
-- Behaviour is unchanged. Its own action is still checked as the edit_fields capability a role declares
-- (require_actor maps it, as it maps set_awaiting_role to await_role), the implementation owner may still call it,
-- and the Director keeps edit_fields. Only the authorization check, and its refusal, now name set_blocked_reason.

BEGIN;

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
        -- role's permission, in its own operation's function, which entered
        -- the grant consumed in this transaction, and for nothing else.
        IF actor <> 'ticket_board_service' OR NOT EXISTS (
            SELECT FROM ticket_board.workflow_roles r
            WHERE r.name IN (caller_role, ticket_board.ui_entered_authority(p_action))
              AND (r.definition->>'active')::boolean
              AND (
                  r.definition->'capabilities' ? CASE p_action
                      WHEN 'set_awaiting_role' THEN 'await_role'
                      -- SYRD-810: its own action, checked as the capability it is (a blocker explanation
                      -- is the ticket's text, as edit_fields edits its notes).
                      WHEN 'set_blocked_reason' THEN 'edit_fields' ELSE p_action END
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
       AND NOT coalesce(ticket_board.ui_entered_authority(p_action) = ANY(p_allowed_roles), false)  -- SYRD-598
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

CREATE OR REPLACE FUNCTION ticket_board.set_blocked_reason(
    p_ticket text, p_reason text, p_pid bigint, p_start_time bigint, p_uid bigint)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    v_caller text;
    v_reason text := btrim(coalesce(p_reason, ''));
    v_ticket ticket_board.tickets%ROWTYPE;
    v_generation bigint;
    v_standing text;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'set_blocked_reason');
    v_caller := ticket_board.current_app_actor();
    -- The row lock orders this against a transition or a reassignment: whichever commits first, the other sees it.
    SELECT * INTO v_ticket FROM ticket_board.tickets t WHERE t.id = upper(btrim(coalesce(p_ticket, ''))) FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    IF v_caller IS NULL OR v_ticket.assignee IS DISTINCT FROM v_caller OR NOT EXISTS (
        SELECT FROM ticket_board.workflow_stages ws
         WHERE ws.name = v_ticket.state
           AND ticket_board.declared_stage_kind(ws.name) = 'implementation'
           AND v_caller = ANY (ws.owner_roles)) THEN
        RAISE EXCEPTION '% may explain the blocker only of a ticket it owns in implementation; % is %/%',
            coalesce(v_caller, '<none>'), v_ticket.id, v_ticket.state, v_ticket.assignee USING ERRCODE = '42501';
    END IF;
    -- The registration the board server resolves a pane by: the process, for the runtime and target the role
    -- declares now. One the role's declaration has since moved on from is as stale as a replaced one.
    SELECT a.generation INTO v_generation
      FROM ticket_board.role_runtime_assignments a JOIN ticket_board.workflow_roles r ON r.name = a.role
     WHERE a.role = v_caller AND a.process_pid = p_pid AND a.process_start_time = p_start_time
       AND a.process_uid = p_uid AND r.definition->>'runtime' = a.runtime
       AND r.definition->>'target' = a.actual_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'process % is not %''s registered live runtime: a retired, replaced or detached process '
            'explains no blocker', p_pid, v_caller USING ERRCODE = '42501';
    END IF;
    v_standing := ticket_board.unresolved_blocker_list(v_ticket.id);
    IF v_reason = '' AND v_standing IS NOT NULL THEN
        RAISE EXCEPTION '% waits on %: a standing blocker''s explanation can be revised, not removed', v_ticket.id,
            v_standing;
    END IF;
    IF v_reason = v_ticket.blocked_reason THEN
        RETURN jsonb_build_object('ticket', v_ticket.id, 'blocked_reason', v_reason, 'changed', false);
    END IF;
    UPDATE ticket_board.tickets t SET blocked_reason = v_reason WHERE t.id = v_ticket.id;
    INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
    VALUES (v_ticket.id, v_caller,
            format('blocker explanation, from %s''s registered runtime: generation %s, process %s', v_caller,
                   v_generation, p_pid),
            'blocked_reason', to_jsonb(v_ticket.blocked_reason), to_jsonb(v_reason));
    -- Bookkeeping, like a comment's: an edit of the text is not the action a wait is for. The touch also
    -- refreshes the ticket's stored document, so an open board redraws it.
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', 'on', true);
    PERFORM ticket_board.touch_ticket(v_ticket.id);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', '', true);
    RETURN jsonb_build_object('ticket', v_ticket.id, 'blocked_reason', v_reason, 'changed', true,
                              'runtime_generation', v_generation);
END;
$$;

COMMIT;
