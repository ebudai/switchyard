-- SYRD-875 (pgu1060): a resubmitted review returns to the reviewer who kicked it back, not the first free owner.
--
-- SAPI-148: audit2 kicked round 1 back; round 2 landed with audit, the stage's first owner, as every entry into a
-- review stage did (docs/syrd-745-review-balance.md: "Where a submission first lands: still the first owner").
--
-- * review_returns: per ticket and review stage, the owner whose kick-back (a declared `return` out of the stage) last
--   sent it out (trigger tickets_z_review_return); any other exit from the stage ends the record.
-- * enforce_declared_ticket_update: work re-entering a shared review stage, with no owner named, lands with that
--   reviewer when it can take it (review_return_unavailable: still an owner, active, registered, not provider-held);
--   otherwise with the first owner as before, and the ticket says why in one line.
-- * review_return_held: such a rework stays with its reviewer. The balancer never moves it, handed over or not yet.
-- * review_return_fall_back: the listener's half -- a reviewer whose provider process is not running, or that became
--   unavailable while the rework waited, gives the return up before the hand-off: first owner, balanced as usual.
-- * resolve_ticket_context_restore: a rework back with its reviewer, whose saved conversation of it cannot be restored,
--   is reviewed afresh by that reviewer (set_aside_review_conversation, as SYRD-873's force-fresh-context) instead of
--   parking. request_fresh_ticket_context now shares that helper.
--
-- schema.sql carries the same; rbac.sql grants the listener review_return_fall_back.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.review_returns (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    stage text NOT NULL,
    role text NOT NULL,
    action text NOT NULL,
    returned_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ticket_id, stage)
);
REVOKE ALL ON ticket_board.review_returns FROM PUBLIC;

-- The reviewer whose kick-back last sent p_ticket out of review stage p_stage, or NULL: it never left that way, or its
-- last exit from the stage was something else (a sign-off), or the return has since been given up.
CREATE OR REPLACE FUNCTION ticket_board.review_return_reviewer(p_ticket text, p_stage text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT role FROM ticket_board.review_returns WHERE ticket_id = p_ticket AND stage = p_stage;
$$;
REVOKE ALL ON FUNCTION ticket_board.review_return_reviewer(text, text) FROM PUBLIC;

-- Whether p_ticket is with the reviewer whose kick-back last sent it out of the stage it is in now. Such a rework stays
-- with that reviewer, queued behind its current review while it is busy: the balancer never moves it.
CREATE OR REPLACE FUNCTION ticket_board.review_return_held(p_ticket text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.tickets t
                    WHERE t.id = p_ticket AND ticket_board.review_return_reviewer(t.id, t.state) = t.assignee);
$$;
REVOKE ALL ON FUNCTION ticket_board.review_return_held(text) FROM PUBLIC;

-- Why p_role cannot take back the rework it kicked out of p_stage, as far as the board itself knows, or NULL. Whether its
-- provider process is still running is the listener's to see (review_return_fall_back). A work pause is not a reason: it
-- holds every reviewer alike, and the rework waits for this one.
CREATE OR REPLACE FUNCTION ticket_board.review_return_unavailable(p_stage text, p_role text, p_now timestamptz)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE
        WHEN NOT EXISTS (SELECT FROM jsonb_array_elements(coalesce(ticket_board.declared_workflow()->'stages', '[]'::jsonb)) s
                          WHERE s->>'name' = p_stage AND coalesce(s->'owners' ? p_role, false)) THEN
            format('it no longer owns %s', p_stage)
        WHEN NOT EXISTS (SELECT FROM jsonb_array_elements(coalesce(ticket_board.declared_workflow()->'roles', '[]'::jsonb)) r
                          WHERE r->>'name' = p_role AND coalesce((r->>'active')::boolean, false)) THEN
            'it is not an active role'
        WHEN NOT EXISTS (SELECT FROM ticket_board.role_runtime_assignments a WHERE a.role = p_role) THEN
            'it is not registered'
        WHEN ticket_board.role_account_hold(p_role) IS NOT NULL THEN
            'its provider account is held'
        -- role_provider_hold_is_current's quota half (its other halves are a work pause and the account hold above).
        WHEN EXISTS (SELECT FROM ticket_board.provider_quota_holds h
                       JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
                      WHERE h.role = p_role
                        AND ((h.state = 'held' AND h.deadline > p_now)
                             OR (h.state = 'released' AND h.released_at > p_now - interval '10 minutes'))) THEN
            'its provider is out of quota'
    END;
$$;
REVOKE ALL ON FUNCTION ticket_board.review_return_unavailable(text, text, timestamptz) FROM PUBLIC;

-- Each exit from a review stage that more than one role owns: a kick-back (a declared `return` out of it) records the
-- owner that held the ticket as the reviewer the rework returns to; any other exit ends the record. And a rework that
-- could not return to its reviewer (enforce_declared_ticket_update) says so on the ticket, in one line, when it lands.
CREATE OR REPLACE FUNCTION ticket_board.record_review_return()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE action_name text := nullif(current_setting('ticket_board.workflow_action', true), '');
    fallback jsonb := nullif(current_setting('ticket_board.review_return_fallback', true), '')::jsonb;
BEGIN
    IF ticket_board.declared_stage_kind(OLD.state) = 'review' THEN
        IF action_name IS NOT NULL
           AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
                        WHERE x->>'from' = OLD.state AND x->>'action' = action_name AND x->>'primitive' = 'return')
           AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s
                        WHERE s->>'name' = OLD.state AND jsonb_array_length(coalesce(s->'owners', '[]'::jsonb)) > 1) THEN
            INSERT INTO ticket_board.review_returns(ticket_id, stage, role, action)
            VALUES (NEW.id, OLD.state, OLD.assignee, action_name)
            ON CONFLICT (ticket_id, stage) DO UPDATE SET role = EXCLUDED.role, action = EXCLUDED.action,
                                                         returned_at = clock_timestamp();
        ELSE
            DELETE FROM ticket_board.review_returns WHERE ticket_id = NEW.id AND stage = OLD.state;
        END IF;
    END IF;
    IF fallback->>'ticket' = NEW.id AND fallback->>'stage' = NEW.state THEN
        PERFORM set_config('ticket_board.review_return_fallback', '', true);
        DELETE FROM ticket_board.review_returns WHERE ticket_id = NEW.id AND stage = NEW.state;
        PERFORM ticket_board.append_ticket_comment(NEW.id, 'ticket_board_service', format(
            'Review return in %s: %s kicked this back, but cannot take the rework (%s), so it lands with %s, the first '
            'owner, and may be balanced.', NEW.state, fallback->>'role', fallback->>'why', NEW.assignee));
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.record_review_return() FROM PUBLIC;
DROP TRIGGER IF EXISTS tickets_z_review_return ON ticket_board.tickets;
CREATE TRIGGER tickets_z_review_return AFTER UPDATE OF state ON ticket_board.tickets
    FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION ticket_board.record_review_return();

-- The listener's half of the fallback: a rework waiting with the reviewer that kicked it back, not yet handed to it in
-- this round and not placed by the Director, whose reviewer cannot take it -- the board's reasons, or p_not_running
-- ({role: why}), the owners of p_stage the listener sees with no live registered provider process. The return is
-- given up: the rework goes to the stage's first owner, as a first submission does, and may be balanced; the ticket
-- says why in one line.
CREATE OR REPLACE FUNCTION ticket_board.review_return_fall_back(p_stage text, p_now timestamptz, p_not_running jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); owners text[]; r record; why text; moved ticket_board.tickets;
    fell jsonb := '[]'::jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('review_return_fall_back');
    SELECT ARRAY(SELECT jsonb_array_elements_text(x->'owners')) INTO owners
      FROM jsonb_array_elements(coalesce(cfg->'stages', '[]'::jsonb)) x WHERE x->>'name' = p_stage AND x->>'kind' = 'review';
    IF coalesce(cardinality(owners), 0) < 2 THEN
        RETURN fell;
    END IF;
    FOR r IN
        SELECT t.id, t.assignee FROM ticket_board.tickets t
          JOIN ticket_board.review_returns rr ON rr.ticket_id = t.id AND rr.stage = t.state AND rr.role = t.assignee
          JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE t.state = p_stage
           AND NOT ticket_board.review_placement_current(t.id)
           AND NOT EXISTS (SELECT FROM ticket_board.notification_trace nt
                            WHERE nt.ticket_id = t.id AND nt.target_role = t.assignee AND nt.ticket_state_at_event = p_stage
                              AND nt.kind = 'transition' AND nt.event = 'send'
                              AND nt.ts >= coalesce(ns.entered_current_state_at, '-infinity'::timestamptz))
         ORDER BY ns.entered_current_state_at, t.ticket_number
    LOOP
        why := coalesce(ticket_board.review_return_unavailable(p_stage, r.assignee, p_now),
                        nullif(btrim(coalesce(p_not_running->>r.assignee, '')), ''));
        CONTINUE WHEN why IS NULL;
        DELETE FROM ticket_board.review_returns WHERE ticket_id = r.id AND stage = p_stage;
        moved := NULL;
        IF r.assignee <> owners[1] THEN
            PERFORM set_config('ticket_board.review_balance_ticket', r.id, true);
            PERFORM set_config('ticket_board.workflow_actor', 'board', true);
            UPDATE ticket_board.tickets SET assignee = owners[1]
             WHERE id = r.id AND state = p_stage AND assignee = r.assignee
            RETURNING * INTO moved;
            PERFORM set_config('ticket_board.workflow_actor', '', true);
            PERFORM set_config('ticket_board.review_balance_ticket', '', true);
            CONTINUE WHEN moved.id IS NULL;  -- moved by someone else meanwhile
            PERFORM ticket_board.enqueue_transition_notification(
                moved.id, moved.title, p_stage, moved.state, moved.assignee, moved.updated_at, moved.ticket_number,
                moved.id || CASE WHEN moved.title <> '' THEN ' -- ' || moved.title ELSE '' END || ' is now assigned to you');
        END IF;
        PERFORM ticket_board.append_ticket_comment(r.id, 'ticket_board_service', format(
            'Review return in %s: %s kicked this back, but cannot take the rework (%s), so it lands with %s, the first '
            'owner, and may be balanced.', p_stage, r.assignee, why, owners[1]));
        fell := fell || jsonb_build_object('ticket', r.id, 'from', r.assignee, 'to', owners[1], 'why', why);
    END LOOP;
    RETURN fell;
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.review_return_fall_back(text, timestamptz, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.review_return_fall_back(text, timestamptz, jsonb) TO ticket_board_listener;

-- A reviewer's records of a ticket's conversation, set aside whole in fresh_review_archive, so that reviewer's next
-- hand-off of it is a first one: its pane is cleared at an idle boundary, as for any new ticket, and it reviews afresh.
CREATE OR REPLACE FUNCTION ticket_board.set_aside_review_conversation(p_ticket text, p_role text, p_by text, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE t ticket_board.tickets; set_aside jsonb;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    set_aside := jsonb_build_object(
        'contexts', (SELECT coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) FROM ticket_board.ticket_role_contexts x
                      WHERE x.ticket_id = p_ticket AND x.role = p_role),
        'clears', (SELECT coalesce(jsonb_agg(to_jsonb(c)), '[]'::jsonb) FROM ticket_board.ticket_role_session_clears c
                    WHERE c.ticket_id = p_ticket AND c.role = p_role),
        'restores', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) FROM ticket_board.ticket_context_restores r
                      WHERE r.ticket_id = p_ticket AND r.role = p_role),
        'continuations', (SELECT coalesce(jsonb_agg(to_jsonb(k)), '[]'::jsonb) FROM ticket_board.ticket_context_continuations k
                           WHERE k.ticket_id = p_ticket AND k.role = p_role));
    INSERT INTO ticket_board.fresh_review_archive(ticket_id, role, stage, candidate, requested_by, reason, set_aside)
    VALUES (p_ticket, p_role, t.state, t.commit_hash, p_by, btrim(p_reason), set_aside);
    DELETE FROM ticket_board.ticket_context_restores WHERE ticket_id = p_ticket AND role = p_role;
    DELETE FROM ticket_board.ticket_role_contexts WHERE ticket_id = p_ticket AND role = p_role;
    DELETE FROM ticket_board.ticket_role_session_clears WHERE ticket_id = p_ticket AND role = p_role;
    DELETE FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket AND role = p_role;
    PERFORM ticket_board.enqueue_transition_notification(
        t.id, t.title, t.state, t.state, p_role, t.updated_at, t.ticket_number,
        t.id || CASE WHEN t.title <> '' THEN ' -- ' || t.title ELSE '' END || ' is now assigned to you for a fresh '
        'review of the exact candidate ' || coalesce(nullif(t.commit_hash, ''), '(none recorded)')
        || '; the earlier findings are in its comments');
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.set_aside_review_conversation(text, text, text, text) FROM PUBLIC;

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
           -- SYRD-875: a rework with the reviewer that kicked it back stays with it, handed over or not yet.
           AND NOT ticket_board.review_return_held(t.id)
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
    IF p_outcome='failed' AND ticket_board.declared_stage_kind(t.state) = 'review' AND t.assignee = attempt.role
       AND ticket_board.review_return_held(t.id)
       AND NOT EXISTS (SELECT FROM ticket_board.fresh_review_archive a
                         JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = a.ticket_id
                        WHERE a.ticket_id = t.id AND a.role = attempt.role
                          AND a.archived_at >= coalesce(ns.entered_current_state_at, '-infinity'::timestamptz)) THEN
        -- SYRD-875: a rework back with the reviewer that kicked it back, whose saved conversation of it cannot be
        -- restored, is reviewed afresh by that same reviewer instead of parking: the exact candidate, with its earlier
        -- findings in the ticket's comments. The parked hand-off's notice is dropped; the fresh hand-off replaces it.
        -- Once a round: a fresh hand-off that fails again parks, and is the Director's, below -- never a loop.
        PERFORM ticket_board.append_ticket_comment(t.id, 'ticket_board_service', format(
            'Fresh review in %s: %s kicked this back and takes the rework, but its saved conversation of %s cannot be '
            'restored (%s). It is set aside (kept in the board''s record), and %s reviews %s afresh: the exact candidate '
            '%s, with its earlier findings in these comments.', t.state, attempt.role, t.id,
            coalesce(p_detail->>'reason', 'unknown'), attempt.role, t.id, coalesce(nullif(t.commit_hash, ''), '(none recorded)')));
        PERFORM ticket_board.set_aside_review_conversation(t.id, attempt.role, 'board',
            'the reviewer that kicked it back takes the rework, and cannot restore its saved conversation: '
            || coalesce(p_detail->>'reason', 'unknown'));
    ELSIF p_outcome='failed' AND ticket_board.declared_stage_kind(t.state) = 'review' AND t.assignee = attempt.role THEN
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
    t ticket_board.tickets;
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
        PERFORM ticket_board.append_ticket_comment(p_ticket, who, format(
            'Director fresh review in %s: %s''s saved conversation of %s could not be restored, so it is set aside (kept '
            'in the board''s record) and %s reviews %s afresh: the exact candidate %s, with the earlier findings in these '
            'comments. Reason: %s', t.state, t.assignee, p_ticket, t.assignee, p_ticket,
            coalesce(nullif(t.commit_hash, ''), '(none recorded)'), btrim(p_reason)));
        PERFORM ticket_board.set_aside_review_conversation(p_ticket, t.assignee, who, p_reason);
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
    queued_for text; reserved_by text; origin jsonb; returned_to text; return_why text;
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

    -- SYRD-746: a Director's restore of a mistaken reopen puts back exactly the closed disposition the reopen took
    -- away, recorded when it did, and nothing else. The window names one ticket and is open for one statement,
    -- opened only by restore_reopened_ticket; what passes through it must equal the recorded disposition, sign-off
    -- by sign-off, so it can neither raise a sign-off that was not there nor carry any other commit.
    IF nullif(current_setting('ticket_board.restore_reopen', true), '') = previous.id THEN
        IF NOT ticket_board.reopen_restore_admits(previous, proposed) THEN
            RAISE EXCEPTION 'a restore may put back only the closed disposition recorded when % was reopened', previous.id
                USING ERRCODE='42501';
        END IF;
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
    -- SYRD-599: a Router operation's window names one ticket and one statement,
    -- and admits exactly its own change (require_router_change). Everything
    -- after this still applies to it: the stage-owner check, serial focus, the
    -- declared transition and its bookkeeping.
    IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
        PERFORM ticket_board.require_router_change(previous, proposed, actor);
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director'
           AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id
           AND nullif(current_setting('ticket_board.review_balance_ticket', true), '') IS DISTINCT FROM previous.id THEN  -- SYRD-745
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
               AND proposed.assignee IN (SELECT ticket_board.pull_claimant_pool(cfg))  -- SYRD-795: only a claimant waits there
               AND NOT proposed.manually_controlled
               AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL THEN
                -- SYRD-599: Router changes the owner and nothing else. Diverting its
                -- pick into the ready stage would move the ticket it was told to
                -- leave where it is, so it is refused instead: Router chooses a
                -- free owner or hands the ticket back.
                IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
                    RAISE EXCEPTION '% already has % active; on this pull-scheduled board Router cannot queue work behind it: choose an owner with no active ticket, or return % to the Director',
                        proposed.assignee, ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id), previous.id
                        USING ERRCODE='42501'; END IF;
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
    -- SYRD-599: router_return is declared so it is visible, owner-scoped and
    -- bookkept like any move, but only its own operation may take it: it
    -- carries the revision check and the Director's handoff.
    IF tr->>'action'='router_return'
       AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id THEN
        RAISE EXCEPTION 'router_return is taken only through ticket_board.router_return' USING ERRCODE='42501'; END IF;
    -- SYRD-673: Audit's kick-back returns the work to the stage and owner that
    -- produced the revision under review, as the board recorded it -- never to
    -- the declared `to`, the current assignee or a guess. With no valid record
    -- it is refused, and nothing moves.
    IF ticket_board.audit_return_transition(tr) THEN
        origin:=ticket_board.audit_return_status(previous.id);
        IF NOT coalesce((origin->>'valid')::boolean,false) THEN
            RAISE EXCEPTION '% cannot be kicked back: %. Nothing was moved. Ask the Director (ticket-board-write request-dependency % --role director); the repair: %',
                previous.id, coalesce(origin->>'problem','the board has no record of who produced its revision'), previous.id,
                coalesce(origin->>'repair','the Director records where it returns with set-audit-return') USING ERRCODE='55000'; END IF;
    -- SYRD-719: Audit's documentation return goes to the Documentation stage and
    -- owner the ticket passed with the revision under review, keeping it as the
    -- candidate the correction builds on. Without that record it is refused, and
    -- nothing moves: the ordinary kick-back is the safe way back.
    ELSIF ticket_board.audit_documentation_return_transition(tr) THEN
        origin:=ticket_board.audit_return_status(previous.id)->'documentation';
        IF NOT coalesce((origin->>'valid')::boolean,false) THEN
            RAISE EXCEPTION '% cannot be returned to Documentation: %. Nothing was moved. A finding that is not documentation-only, or that Documentation cannot take, goes back to its substantive origin with audit-kick-back',
                previous.id, coalesce(origin->>'problem','the board has no record that it passed through Documentation')
                USING ERRCODE='55000'; END IF;
    END IF;
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
       -- SYRD-629: the User handing their review to the Director promotes nothing.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-599: Router handing work back to triage promotes nothing.
       AND tr->>'action'<>'router_return'
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
       -- SYRD-629: nor is the User handing it to the Director, in a stage they
       -- alone hold to decide it: the User's message about a candidate keeps the
       -- candidate, its commit and Audit's sign-off, for the Director to return
       -- to the User or send on. Only from User Review: a move into Analysis
       -- from any other review still needs that review's sign-off.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    -- SYRD-719: work returned to its substantive origin is changed work, so the
    -- Documentation stage this Audit stage returns documentation to owes its
    -- sign-off again: the next submission passes the documentation gate anew.
    IF ticket_board.audit_return_transition(tr) THEN
        FOR reset IN SELECT s->>'signoff' FROM jsonb_array_elements(cfg->'transitions') x, jsonb_array_elements(cfg->'stages') s
                      WHERE x->>'from'=previous.state AND ticket_board.audit_documentation_return_transition(x)
                        AND s->>'name'=x->>'to' AND s->>'signoff' IS NOT NULL LOOP
            doc:=ticket_board.set_workflow_flag(doc,reset,false);
        END LOOP;
    END IF;
    -- A return hands work back to be redone, so the candidate goes -- except to
    -- a review stage, whose owner produced it and reviews on top of it (SYRD-673).
    IF tr->>'primitive' IN ('return','reopen') AND ticket_board.declared_stage_kind(origin->>'stage') IS DISTINCT FROM 'review' THEN
        doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
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
    target:=coalesce(origin->>'stage',proposed.state);
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        -- The producer's stage is exact: no gate skips past it (SYRD-673).
        EXIT WHEN origin IS NOT NULL OR (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    -- SYRD-599: on a Router-enabled board the Director's ordinary route into
    -- Implementation is Router's, unless the Director names an Implementation
    -- owner: that is the direct route, and every rule below applies to it. A
    -- Router that cannot be told is refused, never replaced by an implementer:
    -- going direct is the Director's choice, not a fallback.
    IF dest->>'kind'='implementation' AND actor='director' AND tr->>'primitive'='move'
       AND source_stage->>'kind' NOT IN ('implementation','review')
       AND ticket_board.router_holds(NULL,'router') AND NOT proposed.assignee=ANY(owners) THEN
        IF NOT ticket_board.router_available() THEN
            RAISE EXCEPTION 'Router is not running, so % cannot be routed through it; route it to an Implementation owner directly (%), or start Router',
                previous.id, array_to_string(owners, ', ') USING ERRCODE='55000';
        END IF;
        proposed.assignee:='router';
    END IF;
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF origin IS NOT NULL THEN
        proposed.assignee:=origin->>'assignee';
    ELSIF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    ELSIF dest->>'kind'='review' AND NOT proposed.assignee=ANY(owners) THEN
        -- SYRD-875: work re-entering a shared review stage, naming no owner, returns to the reviewer whose kick-back
        -- last sent it out (review_returns), when that reviewer can take it. Otherwise it lands with the first owner,
        -- below, and the ticket says why (record_review_return). A first submission has no such reviewer, and lands as
        -- before.
        returned_to:=ticket_board.review_return_reviewer(previous.id,target);
        IF returned_to IS NOT NULL THEN
            return_why:=ticket_board.review_return_unavailable(target,returned_to,clock_timestamp());
            IF return_why IS NULL THEN
                proposed.assignee:=returned_to;
            ELSE
                PERFORM set_config('ticket_board.review_return_fallback',jsonb_build_object('ticket',previous.id,
                    'stage',target,'role',returned_to,'why',return_why)::text,true);
            END IF;
        END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners)
       AND NOT ticket_board.router_holds(target,proposed.assignee) THEN proposed.assignee:=owners[1]; END IF;
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
    -- move is still diverted, pinned to the busy author -- when the author is a
    -- claimant, the only one who may take it from there. An implementer the
    -- Director dispatches on a pull board claims nothing, so pinned there its
    -- ticket was never taken, not even when its slot freed (SYRD-795, MEFP-1213
    -- behind a blocked MEFP-1209): its work queues in place, as SYRD-568's does,
    -- and the freed slot hands it over.
    IF ticket_board.declared_scheduling() IS NOT NULL
       AND dest->>'kind'='implementation' AND NOT ticket_board.router_holds(dest->>'name',proposed.assignee)
       AND proposed.assignee IN (SELECT ticket_board.pull_claimant_pool(cfg))
       AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
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


COMMIT;
