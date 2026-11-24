-- SYRD-873 (pgu1059): the review balancer keeps the Director's placement, never hands a review to a reviewer whose
-- hand-off would park, and a parked review is the Director's, with a fresh review as one supported recovery.
--
-- MEFP-1234: audit-2 (ephemeral, Codex, under a pull policy) held the ticket's saved conversation from its kick-back,
-- and its pane had since been cleared for another ticket. The balancer moved the resubmission to idle audit-2; its
-- hand-off needed a restore, Codex has no verified in-session resume, and the delivery parked it. The Director's
-- reassignment to audit was undone 23 seconds later: nothing recorded that the Director had placed it.
--
-- * review_placements: a same-stage owner change the Director makes in a review stage (trigger
--   tickets_z_review_placement). Current while the ticket stays in that stage with that owner (review_placement_current);
--   leaving the stage, or the Director moving it again, ends or replaces it. The balancer (actor 'board') records none.
-- * balance_review_stage_skipping: SYRD-745's balancer, with p_skip -- the tickets whose hand-off to p_to the listener's
--   delivery would park (scripts/ticket_board/review_balance.py) -- and never a placed ticket. A parked hand-off is its
--   reviewer's current ticket, which SYRD-745 never takes. balance_review_stage, the older listener's call, is that
--   with nothing skipped.
-- * resolve_ticket_context_restore: a parked review's one Director notice names the reviewer, the reason, the exact
--   candidate and the two recoveries.
-- * request_fresh_ticket_context (force-fresh-context): for a parked review, sets that reviewer's records of the
--   ticket's conversation aside in fresh_review_archive and hands the review over afresh.
-- * board_stall_runnable: a parked hand-off is the Director's next action.
--
-- schema.sql carries the same; rbac.sql grants the listener the new function.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.review_placements (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    stage text NOT NULL,
    role text NOT NULL,
    placed_by text NOT NULL,
    placed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON ticket_board.review_placements FROM PUBLIC;

CREATE TABLE IF NOT EXISTS ticket_board.fresh_review_archive (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL,
    stage text NOT NULL,
    candidate text,
    requested_by text NOT NULL,
    reason text NOT NULL,
    set_aside jsonb NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON ticket_board.fresh_review_archive FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.record_review_placement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- The Director's own same-stage owner change in a review stage. The actor is the delegated one when the board UI lent
-- the Director's authority (SYRD-598), else the caller; the balancer's moves run as 'board' and are never placements.
DECLARE who text := coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''),
                             nullif(current_setting('ticket_board.caller_role', true), ''));
BEGIN
    IF who = 'director' AND ticket_board.declared_stage_kind(NEW.state) = 'review' THEN
        INSERT INTO ticket_board.review_placements(ticket_id, stage, role, placed_by)
        VALUES (NEW.id, NEW.state, NEW.assignee, who)
        ON CONFLICT (ticket_id) DO UPDATE SET stage = EXCLUDED.stage, role = EXCLUDED.role, placed_by = EXCLUDED.placed_by,
                                              placed_at = clock_timestamp();
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.record_review_placement() FROM PUBLIC;
DROP TRIGGER IF EXISTS tickets_z_review_placement ON ticket_board.tickets;
CREATE TRIGGER tickets_z_review_placement AFTER UPDATE OF assignee ON ticket_board.tickets
    FOR EACH ROW WHEN (OLD.state = NEW.state AND OLD.assignee IS DISTINCT FROM NEW.assignee)
    EXECUTE FUNCTION ticket_board.record_review_placement();

-- Whether the Director placed this ticket since it entered its current stage. Its owner since then is the one the
-- Director chose: the balancer never moves a placed ticket, and the Director's next move replaces the placement.
CREATE OR REPLACE FUNCTION ticket_board.review_placement_current(p_ticket text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.review_placements p
                     JOIN ticket_board.tickets t ON t.id = p.ticket_id
                     LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
                    WHERE p.ticket_id = p_ticket AND p.stage = t.state
                      AND p.placed_at >= coalesce(ns.entered_current_state_at, '-infinity'::timestamptz));
$$;
REVOKE ALL ON FUNCTION ticket_board.review_placement_current(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.balance_review_stage_skipping(p_stage text, p_to text, p_now timestamptz,
                                                                     p_skip text[])
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); owners text[]; candidate record; busy_with text;
    moved ticket_board.tickets;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('balance_review_stage_skipping');
    SELECT ARRAY(SELECT jsonb_array_elements_text(x->'owners')) INTO owners
      FROM jsonb_array_elements(coalesce(cfg->'stages', '[]'::jsonb)) x WHERE x->>'name' = p_stage AND x->>'kind' = 'review';
    IF coalesce(cardinality(owners), 0) < 2 OR NOT p_to = ANY(owners) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'not an owner of a shared review stage');
    END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE t.state = p_stage AND t.assignee = p_to) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'it already holds work in this stage');
    END IF;
    IF ticket_board.role_provider_hold_is_current(p_to, p_now) THEN
        RETURN jsonb_build_object('moved', false, 'reason', 'its provider is held');
    END IF;
    FOR candidate IN
        SELECT t.id, t.assignee FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE t.state = p_stage AND t.assignee = ANY(owners) AND t.assignee <> p_to
           AND coalesce(btrim(ns.awaiting_role), '') = ''
           AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
           -- SYRD-873: the listener's own judgement of p_to: tickets whose hand-off to it would park.
           AND NOT t.id = ANY(coalesce(p_skip, '{}'::text[]))
           -- SYRD-873: the reviewer the Director placed it with keeps it while it stays in this stage.
           AND NOT ticket_board.review_placement_current(t.id)
           AND NOT EXISTS (SELECT FROM ticket_board.notification_trace nt
                            WHERE nt.ticket_id = t.id AND nt.target_role = t.assignee AND nt.ticket_state_at_event = p_stage
                              AND nt.kind = 'transition' AND nt.event = 'send')
         ORDER BY ns.entered_current_state_at, t.ticket_number
    LOOP
        busy_with := nullif(ticket_board.finish_current_stage_blocker(candidate.id, candidate.assignee, p_stage, p_now), '');
        CONTINUE WHEN busy_with IS NULL;  -- its reviewer's current ticket: never taken from it
        PERFORM set_config('ticket_board.review_balance_ticket', candidate.id, true);
        PERFORM set_config('ticket_board.workflow_actor', 'board', true);
        UPDATE ticket_board.tickets SET assignee = p_to
         WHERE id = candidate.id AND state = p_stage AND assignee = candidate.assignee
        RETURNING * INTO moved;
        PERFORM set_config('ticket_board.workflow_actor', '', true);
        PERFORM set_config('ticket_board.review_balance_ticket', '', true);
        CONTINUE WHEN moved.id IS NULL;  -- moved by someone else meanwhile
        PERFORM ticket_board.append_ticket_comment(moved.id, 'ticket_board_service', format(
            'Board balance in %s: %s -> %s. %s is reviewing %s, and %s was live and idle with nothing in %s, so this '
            'waiting review goes to it.', p_stage, candidate.assignee, p_to, candidate.assignee, busy_with, p_to, p_stage));
        PERFORM ticket_board.enqueue_transition_notification(
            moved.id, moved.title, p_stage, moved.state, moved.assignee, moved.updated_at, moved.ticket_number,
            moved.id || CASE WHEN moved.title <> '' THEN ' -- ' || moved.title ELSE '' END || ' is now assigned to you');
        RETURN jsonb_build_object('moved', true, 'ticket', moved.id, 'from', candidate.assignee, 'to', p_to, 'behind', busy_with);
    END LOOP;
    RETURN jsonb_build_object('moved', false, 'reason', 'no review is waiting');
END;
$$;


-- The older listener's call (SYRD-745): the same balancer, skipping nothing the listener judged; a placed ticket is
-- still never moved.
CREATE OR REPLACE FUNCTION ticket_board.balance_review_stage(p_stage text, p_to text, p_now timestamptz)
RETURNS jsonb LANGUAGE sql VOLATILE SET search_path = ticket_board, pg_temp AS $$
    SELECT ticket_board.balance_review_stage_skipping(p_stage, p_to, p_now, '{}'::text[]);
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.balance_review_stage(text, text, timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.balance_review_stage_skipping(text, text, timestamptz, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.balance_review_stage(text, text, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.balance_review_stage_skipping(text, text, timestamptz, text[]) TO ticket_board_listener;

CREATE OR REPLACE FUNCTION ticket_board.resolve_ticket_context_restore(p_attempt bigint, p_outcome text, p_detail jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempt ticket_board.ticket_context_restores; t ticket_board.tickets;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('resolve_ticket_context_restore');
    UPDATE ticket_board.ticket_context_restores SET outcome=p_outcome, resolved_at=clock_timestamp(),
           detail=detail || coalesce(p_detail, '{}'::jsonb)
     WHERE id=p_attempt AND outcome IS NULL RETURNING * INTO attempt;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = attempt.ticket_id;
    IF p_outcome='failed' AND ticket_board.declared_stage_kind(t.state) = 'review' AND t.assignee = attempt.role THEN
        -- SYRD-873: a parked review. Its recoveries are the stage's other owners and a fresh review of the exact candidate.
        PERFORM ticket_board.notify_ticket_context_exception(attempt.ticket_id, attempt.role, 'restore_failed',
            format('%s''s review by %s is parked: %s''s saved conversation of %s could not be restored (%s), so %s is sent '
                   'nothing about it and the conversation%s is kept. Recover it: reassign %s to another owner of %s '
                   '(ticket-board-write reassign %s --assignee <owner> --reason ...), or start a fresh review by %s from the '
                   'ticket''s record -- the exact candidate %s and the earlier findings in its comments -- with '
                   'ticket-board-write force-fresh-context %s --reason ....',
                   attempt.ticket_id, attempt.role, attempt.role, attempt.ticket_id, coalesce(p_detail->>'reason', 'unknown'),
                   attempt.role, coalesce(' ' || attempt.session_id, ''), attempt.ticket_id, t.state, attempt.ticket_id,
                   attempt.role, coalesce(nullif(t.commit_hash, ''), '(none recorded)'), attempt.ticket_id),
            'ticket-context-restore:' || attempt.id);
    ELSIF p_outcome='failed' THEN
        PERFORM ticket_board.notify_ticket_context_exception(attempt.ticket_id, attempt.role, 'restore_failed',
            format('%s''s saved conversation for %s could not be restored (%s). Its hand-off is parked: %s is sent nothing about %s, '
                   'and its saved conversation%s is kept. To retry once the cause is fixed, defer %s and admit it again; '
                   'started on another implementer it begins in that implementer''s own new conversation.',
                   attempt.role, attempt.ticket_id, coalesce(p_detail->>'reason', 'unknown'), attempt.role, attempt.ticket_id,
                   coalesce(' ' || attempt.session_id, ''), attempt.ticket_id),
            'ticket-context-restore:' || attempt.id);
    END IF;
    RETURN true;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.request_fresh_ticket_context(p_ticket text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE who text; decided ticket_board.ticket_context_continuations; saved ticket_board.ticket_context_fresh_requests;
    t ticket_board.tickets; set_aside jsonb;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    -- The same lock decide_ticket_context_continuation takes first: a request and
    -- a decision never both see the other absent.
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'force_fresh_context needs a reason';
    END IF;
    -- SYRD-873: a review parked on a saved conversation its reviewer cannot restore starts fresh instead. That
    -- reviewer's records of this ticket's conversation are set aside -- kept, whole, in fresh_review_archive -- so its
    -- next hand-off is a first one: the pane is cleared at an idle boundary, as for any new ticket, and handed the
    -- review of the exact candidate. Nothing else about the ticket changes: stage, owner, candidate, sign-offs.
    IF ticket_board.declared_stage_kind(t.state) = 'review' AND ticket_board.ticket_context_parked(p_ticket, t.assignee) THEN
        set_aside := jsonb_build_object(
            'contexts', (SELECT coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) FROM ticket_board.ticket_role_contexts x
                          WHERE x.ticket_id = p_ticket AND x.role = t.assignee),
            'clears', (SELECT coalesce(jsonb_agg(to_jsonb(c)), '[]'::jsonb) FROM ticket_board.ticket_role_session_clears c
                        WHERE c.ticket_id = p_ticket AND c.role = t.assignee),
            'restores', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM ticket_board.ticket_context_restores r
                          WHERE r.ticket_id = p_ticket AND r.role = t.assignee),
            'continuations', (SELECT coalesce(jsonb_agg(to_jsonb(k)), '[]'::jsonb) FROM ticket_board.ticket_context_continuations k
                               WHERE k.ticket_id = p_ticket AND k.role = t.assignee));
        INSERT INTO ticket_board.fresh_review_archive(ticket_id, role, stage, candidate, requested_by, reason, set_aside)
        VALUES (p_ticket, t.assignee, t.state, t.commit_hash, who, btrim(p_reason), set_aside);
        DELETE FROM ticket_board.ticket_context_restores WHERE ticket_id = p_ticket AND role = t.assignee;
        DELETE FROM ticket_board.ticket_role_contexts WHERE ticket_id = p_ticket AND role = t.assignee;
        DELETE FROM ticket_board.ticket_role_session_clears WHERE ticket_id = p_ticket AND role = t.assignee;
        DELETE FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket AND role = t.assignee;
        PERFORM ticket_board.append_ticket_comment(p_ticket, who, format(
            'Director fresh review in %s: %s''s saved conversation of %s could not be restored, so it is set aside (kept '
            'in the board''s record) and %s reviews %s afresh: the exact candidate %s, with the earlier findings in these '
            'comments. Reason: %s', t.state, t.assignee, p_ticket, t.assignee, p_ticket,
            coalesce(nullif(t.commit_hash, ''), '(none recorded)'), btrim(p_reason)));
        PERFORM ticket_board.enqueue_transition_notification(
            t.id, t.title, t.state, t.state, t.assignee, t.updated_at, t.ticket_number,
            t.id || CASE WHEN t.title <> '' THEN ' -- ' || t.title ELSE '' END || ' is now assigned to you for a fresh '
            'review of the exact candidate ' || coalesce(nullif(t.commit_hash, ''), '(none recorded)')
            || '; the earlier findings are in its comments');
        RETURN jsonb_build_object('fresh_review', true, 'ticket', p_ticket, 'role', t.assignee, 'stage', t.state,
                                  'candidate', t.commit_hash);
    END IF;
    SELECT * INTO decided FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket ORDER BY decided_at LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION '%''s context was already decided for % at %: % (%)', p_ticket, decided.role, decided.decided_at,
            decided.decision, decided.reason;
    END IF;
    INSERT INTO ticket_board.ticket_context_fresh_requests(ticket_id, requested_by, reason)
    VALUES (p_ticket, who, btrim(p_reason))
    ON CONFLICT (ticket_id) DO UPDATE SET requested_by = EXCLUDED.requested_by, reason = EXCLUDED.reason,
                                          requested_at = clock_timestamp()
    RETURNING * INTO saved;
    RETURN to_jsonb(saved);
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.board_stall_runnable(p_now timestamptz)
RETURNS TABLE (ticket_id text, state text, role text, why_not text, waiting_since timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); policy jsonb := ticket_board.declared_scheduling();
    r record; owners text[]; responsible text; why text; kinds jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('board_stall_runnable');
    SELECT coalesce(jsonb_object_agg(x->>'name', x->>'kind'), '{}'::jsonb) INTO kinds
      FROM jsonb_array_elements(coalesce(cfg->'roles', '[]'::jsonb)) x;
    FOR r IN
        SELECT t.*, ns.entered_current_state_at, ns.current_assignment_at, ns.awaiting_role, ns.awaiting_since_at
          FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE ticket_board.board_stall_stage_counts(t.state)
         ORDER BY ns.entered_current_state_at, t.ticket_number
    LOOP
        why := NULL;
        responsible := NULL;
        SELECT ARRAY(SELECT jsonb_array_elements_text(x->'owners')) INTO owners
          FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = r.state;
        IF policy IS NOT NULL AND r.state = policy->>'ready_stage' THEN
            SELECT string_agg(c, ', ' ORDER BY c) INTO responsible
              FROM ticket_board.pull_claimant_pool(cfg) c WHERE ticket_board.ticket_ready_for_pull(r.id, c);
            IF responsible IS NULL THEN why := 'no claimant may claim it'; END IF;
        ELSIF r.assignee = ANY(owners) THEN
            responsible := r.assignee;
        ELSE
            why := 'nobody who owns its stage holds it';
        END IF;
        -- SYRD-873: a hand-off parked with its owner -- a saved conversation that cannot be restored -- reaches it only
        -- through the Director's recovery, so the next action is the Director's, not a stall of that owner.
        IF why IS NULL AND responsible IS NOT NULL AND ticket_board.ticket_context_parked(r.id, responsible) THEN
            responsible := 'director';
        END IF;
        IF why IS NULL THEN
            why := CASE
                WHEN responsible = 'director' THEN 'its next action is the Director''s'
                WHEN kinds->>responsible = 'user' THEN 'its next action is the User''s'
                WHEN r.manually_controlled THEN 'held by the Director'
                WHEN r.parked THEN 'parked'
                WHEN ticket_board.ticket_has_unresolved_blockers(r.id) THEN
                    'blocked by ' || ticket_board.unresolved_blocker_list(r.id)
                -- SYRD-696: a recorded wait on the User is the User's next action for as long as it stands, as the
                -- User's own stage is: no role on this board can move it, and nothing makes it due. A wait on a pane
                -- role keeps its handoff's window, below.
                WHEN kinds->>nullif(r.awaiting_role, '') = 'user' THEN
                    'waiting on ' || r.awaiting_role || ', the User, until the wait is cleared'
                WHEN ticket_board.ticket_awaiting_role_is_active(r.awaiting_role, r.awaiting_since_at, p_now) THEN
                    'waiting on ' || r.awaiting_role
                -- SYRD-735: an owner whose provider is out of quota cannot move it until the reset, when its hold
                -- delivers the work; the hold's own notice told the Director (SYRD-644). The question the reminders ask.
                WHEN ticket_board.role_account_hold(responsible) IS NOT NULL THEN
                    responsible || '''s provider account is held after a safety refusal, until it is cleared'
                WHEN ticket_board.role_provider_hold_is_current(responsible, p_now) THEN
                    responsible || '''s provider is out of quota, and its work waits for the reset'
                WHEN ticket_board.ticket_reminders_snoozed(r.id, p_now) THEN 'its reminders are snoozed'
                WHEN btrim(coalesce(r.queued_for_assignee, '')) <> '' THEN
                    'queued for ' || r.queued_for_assignee || ' behind ' || coalesce(nullif(r.queued_behind_ticket, ''), 'its work')
                -- SYRD-595's episode is this ticket's one recipient: its owner while being
                -- prompted, the Director once escalated. Never both about one stall.
                WHEN EXISTS (SELECT FROM ticket_board.role_stall_episodes e
                              WHERE e.ticket_id = r.id AND e.closed_at IS NULL AND e.phase = 'director') THEN
                    'its own stall is already with the Director'
                WHEN EXISTS (SELECT FROM ticket_board.role_stall_episodes e
                              WHERE e.ticket_id = r.id AND e.closed_at IS NULL) THEN
                    'its owner is being prompted about its own stall'
            END;
        END IF;
        IF why IS NULL AND NOT (policy IS NOT NULL AND r.state = policy->>'ready_stage') THEN
            why := nullif(ticket_board.finish_current_stage_blocker(r.id, responsible, r.state, p_now), '');
            IF why IS NOT NULL THEN
                why := 'waiting behind ' || responsible || '''s current ' || why;
            END IF;
        END IF;
        ticket_id := r.id; state := r.state; role := responsible; why_not := why;
        waiting_since := greatest(r.entered_current_state_at, r.current_assignment_at);
        RETURN NEXT;
    END LOOP;
END;
$$;


COMMIT;
