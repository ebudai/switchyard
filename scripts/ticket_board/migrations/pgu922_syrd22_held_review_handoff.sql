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
    PERFORM ticket_board.touch_ticket(id);
    IF NOT coalesce(was_approved,false) THEN
        PERFORM ticket_board.notify_held_review_completion(id,'inspection');
    END IF;
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

CREATE OR REPLACE FUNCTION ticket_board.workflow_wait_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN stage IN ('in_progress','inspection','audit','user_review')
 ELSE EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s WHERE s->>'name'=stage AND NOT (s->>'terminal')::boolean AND s->>'kind'<>'draft') END;
$$;
