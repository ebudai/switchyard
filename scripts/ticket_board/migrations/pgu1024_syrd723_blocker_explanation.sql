BEGIN;

-- SYRD-723 (pgu1024): the implementer that owns a ticket keeps the ticket's
-- blocker explanation, its blocked_reason, current. It may set, replace or
-- clear that text and nothing else: no blocker, state, assignee or wait. The
-- Director's set_blockers and the board UI's delegated edit are unchanged.
--
-- The caller must be the ticket's assignee and an owner of its stage, in a
-- stage the workflow declares as implementation, and the calling process --
-- named by the board server from the kernel -- must be that role's registered
-- live runtime, so a retired, replaced or detached process is refused. A board
-- that declares no workflow registers no runtime, so it has no such caller.
-- Each change is in the field audit, with the role, the runtime and the old
-- and new text.
--
-- The text is a note: saving one makes no wait (SYRD-285), and clearing one
-- ends none. While a blocker stands its explanation can be revised, not
-- removed, because set_blockers never lets a standing blocker go unexplained.
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
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'edit_fields');
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
