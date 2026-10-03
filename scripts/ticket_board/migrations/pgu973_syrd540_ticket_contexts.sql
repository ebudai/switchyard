-- SYRD-540: a pulled ticket's own conversation, proven, saved and restored.
--
-- Under a pull policy an author can start a new ticket after its last one
-- passes Audit, and that earlier ticket can come back (MEFP: Final Sign-Off).
-- The author must then work it in THAT ticket's conversation. A ticket is bound
-- to the session a SessionStart reported after its first clear -- proof, not the
-- pane's newest record -- and a later rework is handed over only once the
-- pane is proven back in it (`/resume <id>`, Claude). A restore that cannot be
-- proven, or cannot be attempted, never starts the ticket afresh: its saved
-- binding is kept, its hand-off is parked, and the Director is told once.
BEGIN;

-- Claimants are ephemeral roles (SYRD-540), checked on the stored document by
-- the same trigger that runs validate_declared_scheduling.
CREATE OR REPLACE FUNCTION ticket_board.validate_declared_scheduling_contexts(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF nullif(cfg->'scheduling','null'::jsonb) IS NULL THEN RETURN; END IF;
    IF EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(cfg) x, jsonb_array_elements_text(x->'actors') a
               WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
                                 WHERE r->>'name'=a AND coalesce((r->>'ephemeral')::boolean, false))) THEN
        RAISE EXCEPTION 'every role that can claim must be ephemeral, so each claimed ticket starts its own conversation';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.check_declared_scheduling()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ticket_board.validate_declared_scheduling(NEW.document);
    PERFORM ticket_board.validate_declared_scheduling_contexts(NEW.document);
    RETURN NEW;
END;
$$;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_role_contexts (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (btrim(role) <> ''),
    runtime text NOT NULL,
    session_id text,
    state text NOT NULL CHECK (state IN ('bound', 'unconfirmed')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ticket_id, role),
    CHECK ((state = 'bound') = (session_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_context_restores (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL,
    session_id text,
    reason text NOT NULL CHECK (reason IN ('rework', 'restart')),
    sent_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    outcome text CHECK (outcome IN ('confirmed', 'failed')),
    resolved_at timestamptz,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Checkout readiness, recorded apart from the conversation proof: a
    -- resumed conversation is not ready work until its worktree is proved at
    -- the ticket's expected commit.
    checkout_state text CHECK (checkout_state IN ('preparing', 'ready', 'failed')),
    checkout_at timestamptz,
    checkout_detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
-- One open attempt per ticket and role: a retry, a duplicate notice or a
-- restarted listener finds it instead of sending a second resume.
CREATE UNIQUE INDEX IF NOT EXISTS ticket_context_restores_open_idx
    ON ticket_board.ticket_context_restores (ticket_id, role) WHERE outcome IS NULL;

-- The Director exception both failures share: once per (ticket, role, event).
CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_context_exception(
    p_ticket text, p_role text, p_event text, p_message text, p_key text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT FROM ticket_board.notification_trace tr WHERE tr.ticket_id=p_ticket AND tr.event='enqueue'
               AND tr.detail->>'dedupe_key'=p_key) THEN RETURN; END IF;
    PERFORM ticket_board.enqueue_notification(p_ticket, 'ticket_update', 'director', p_message,
        jsonb_build_object('kind', 'ticket_context', 'event', p_event, 'id', p_ticket, 'role', p_role,
                           'target_role', 'director', 'message', p_message), p_key);
END;
$$;

-- Clears of pulled tickets that have no context recorded yet.
CREATE OR REPLACE FUNCTION ticket_board.pending_ticket_contexts()
RETURNS TABLE(ticket_id text, role text, cleared_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('pending_ticket_contexts');
    IF ticket_board.declared_scheduling() IS NULL THEN RETURN; END IF;
    RETURN QUERY SELECT c.ticket_id, c.role, c.cleared_at FROM ticket_board.ticket_role_session_clears c
     WHERE NOT EXISTS (SELECT FROM ticket_board.ticket_role_contexts x WHERE x.ticket_id=c.ticket_id AND x.role=c.role)
     ORDER BY c.cleared_at;
END;
$$;

-- Record a ticket's context: the proven session, or (p_session NULL) that none
-- was proven -- which the Director is told, once.
CREATE OR REPLACE FUNCTION ticket_board.record_ticket_context(
    p_ticket text, p_role text, p_runtime text, p_session text, p_evidence jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE inserted int;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('record_ticket_context');
    INSERT INTO ticket_board.ticket_role_contexts(ticket_id, role, runtime, session_id, state, evidence)
    VALUES (p_ticket, p_role, coalesce(p_runtime, ''), nullif(btrim(coalesce(p_session, '')), ''),
            CASE WHEN nullif(btrim(coalesce(p_session, '')), '') IS NULL THEN 'unconfirmed' ELSE 'bound' END,
            coalesce(p_evidence, '{}'::jsonb))
    ON CONFLICT (ticket_id, role) DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    IF inserted = 1 AND nullif(btrim(coalesce(p_session, '')), '') IS NULL THEN
        PERFORM ticket_board.notify_ticket_context_exception(p_ticket, p_role, 'unconfirmed',
            format('%s''s conversation for %s was not confirmed after its clear (%s); if it is reworked later, it cannot be restored.',
                   p_role, p_ticket, coalesce(p_evidence->>'reason', 'no SessionStart')),
            'ticket-context-unconfirmed:' || p_ticket || ':' || p_role);
    END IF;
    RETURN inserted = 1;
END;
$$;

-- Whether p_role's pane has moved on from p_ticket's conversation: cleared for
-- another ticket since, and not restored to this one (confirmed) after that.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_superseded(p_ticket text, p_role text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((
        SELECT mine.cleared_at < latest.at
           AND NOT EXISTS (SELECT FROM ticket_board.ticket_context_restores r
                            WHERE r.ticket_id=p_ticket AND r.role=p_role AND r.outcome='confirmed' AND r.resolved_at > latest.at)
          FROM ticket_board.ticket_role_session_clears mine,
               LATERAL (SELECT max(o.cleared_at) AS at FROM ticket_board.ticket_role_session_clears o
                         WHERE o.role=p_role AND o.ticket_id<>p_ticket) latest
         WHERE mine.ticket_id=p_ticket AND mine.role=p_role AND latest.at IS NOT NULL), false);
$$;

-- A failed restore parks the ticket's hand-off to that role until the ticket
-- next enters a stage: the Director's explicit decision (defer and re-admit to
-- retry; start it on another implementer), through moves it already has.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_parked(p_ticket text, p_role text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((
        SELECT (r.outcome = 'failed' OR r.checkout_state = 'failed')
           AND coalesce(r.checkout_at, r.resolved_at) >= coalesce(ns.entered_current_state_at, '-infinity'::timestamptz)
          FROM ticket_board.ticket_context_restores r
          LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = r.ticket_id
         WHERE r.ticket_id=p_ticket AND r.role=p_role ORDER BY r.id DESC LIMIT 1), false);
$$;

-- What a delivery of p_ticket to p_role must do about its conversation:
-- `superseded` when p_role has since been cleared for another ticket (so the
-- pane no longer holds this one's conversation), with the recorded context and
-- any open restore attempt.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_status(p_ticket text, p_role text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ticket_context_status');
    IF ticket_board.declared_scheduling() IS NULL THEN RETURN jsonb_build_object('superseded', false); END IF;
    RETURN jsonb_build_object(
        'superseded', ticket_board.ticket_context_superseded(p_ticket, p_role),
        'context', (SELECT to_jsonb(x) FROM ticket_board.ticket_role_contexts x WHERE x.ticket_id=p_ticket AND x.role=p_role),
        'open', (SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r
                  WHERE r.ticket_id=p_ticket AND r.role=p_role AND r.outcome IS NULL),
        'last', (SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r
                  WHERE r.ticket_id=p_ticket AND r.role=p_role ORDER BY r.id DESC LIMIT 1),
        'parked', ticket_board.ticket_context_parked(p_ticket, p_role),
        -- The checkout as the author LEFT this ticket: the one the SessionStart
        -- of its next ticket's clear recorded, saved in that ticket's binding.
        'left_checkout', (SELECT x.evidence->'session_start'->'checkout'
                            FROM ticket_board.ticket_role_session_clears o
                            JOIN ticket_board.ticket_role_contexts x ON x.ticket_id=o.ticket_id AND x.role=o.role
                           WHERE o.role=p_role AND o.ticket_id<>p_ticket
                             AND o.cleared_at > (SELECT c.cleared_at FROM ticket_board.ticket_role_session_clears c
                                                  WHERE c.ticket_id=p_ticket AND c.role=p_role)
                           ORDER BY o.cleared_at LIMIT 1),
        -- The ticket's own topic: what it last asked to publish.
        'topic', (SELECT jsonb_build_object('ref', pr.ref, 'commit', pr.commit_hash, 'state', pr.state)
                    FROM ticket_board.publication_requests pr
                   WHERE pr.ticket_id=p_ticket AND pr.state IN ('requested', 'published') ORDER BY pr.id DESC LIMIT 1));
END;
$$;

-- Start (or find) the one open restore attempt.
CREATE OR REPLACE FUNCTION ticket_board.open_ticket_context_restore(
    p_ticket text, p_role text, p_session text, p_reason text, p_detail jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempt ticket_board.ticket_context_restores;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('open_ticket_context_restore');
    SELECT * INTO attempt FROM ticket_board.ticket_context_restores WHERE ticket_id=p_ticket AND role=p_role AND outcome IS NULL;
    IF FOUND THEN RETURN jsonb_build_object('opened', false, 'attempt', to_jsonb(attempt)); END IF;
    INSERT INTO ticket_board.ticket_context_restores(ticket_id, role, session_id, reason, detail)
    VALUES (p_ticket, p_role, p_session, p_reason, coalesce(p_detail, '{}'::jsonb)) RETURNING * INTO attempt;
    RETURN jsonb_build_object('opened', true, 'attempt', to_jsonb(attempt));
END;
$$;

-- The one confirmation prompt an attempt may send: true only for the caller that records it.
CREATE OR REPLACE FUNCTION ticket_board.note_ticket_context_probe(p_attempt bigint)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE updated int;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_ticket_context_probe');
    UPDATE ticket_board.ticket_context_restores
       SET detail = detail || jsonb_build_object('probe_sent_at', extract(epoch FROM clock_timestamp()))
     WHERE id=p_attempt AND outcome IS NULL AND NOT detail ? 'probe_sent_at';
    GET DIAGNOSTICS updated = ROW_COUNT;
    RETURN updated = 1;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.mark_ticket_context_delivered(p_attempt bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('mark_ticket_context_delivered');
    UPDATE ticket_board.ticket_context_restores SET delivered_at=coalesce(delivered_at, clock_timestamp()) WHERE id=p_attempt;
END;
$$;

-- Move a confirmed attempt's checkout readiness forward: preparing (once), then
-- ready or failed. A failure parks the hand-off and tells the Director, once.
CREATE OR REPLACE FUNCTION ticket_board.set_ticket_context_checkout(p_attempt bigint, p_state text, p_detail jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempt ticket_board.ticket_context_restores;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('set_ticket_context_checkout');
    UPDATE ticket_board.ticket_context_restores
       SET checkout_state=p_state, checkout_at=clock_timestamp(), checkout_detail=checkout_detail || coalesce(p_detail, '{}'::jsonb)
     WHERE id=p_attempt AND outcome='confirmed'
       AND CASE p_state WHEN 'preparing' THEN checkout_state IS NULL
                        ELSE coalesce(checkout_state, 'preparing') = 'preparing' END
    RETURNING * INTO attempt;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_state='failed' THEN
        PERFORM ticket_board.notify_ticket_context_exception(attempt.ticket_id, attempt.role, 'checkout_failed',
            format('%s''s conversation for %s was restored, but its worktree is not at %s''s checkout (%s). Its hand-off is parked: '
                   '%s is sent nothing about %s, and nothing in its worktree was changed by the board. To retry once the '
                   'worktree is ready, defer %s and admit it again; started on another implementer it begins in that '
                   'implementer''s own new conversation.',
                   attempt.role, attempt.ticket_id, attempt.ticket_id, coalesce(p_detail->>'reason', 'unknown'),
                   attempt.role, attempt.ticket_id, attempt.ticket_id),
            'ticket-context-checkout:' || attempt.id);
    END IF;
    RETURN true;
END;
$$;

-- Close an attempt. A failure parks the hand-off and tells the Director, once per attempt.
CREATE OR REPLACE FUNCTION ticket_board.resolve_ticket_context_restore(p_attempt bigint, p_outcome text, p_detail jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempt ticket_board.ticket_context_restores;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('resolve_ticket_context_restore');
    UPDATE ticket_board.ticket_context_restores SET outcome=p_outcome, resolved_at=clock_timestamp(),
           detail=detail || coalesce(p_detail, '{}'::jsonb)
     WHERE id=p_attempt AND outcome IS NULL RETURNING * INTO attempt;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_outcome='failed' THEN
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

CREATE OR REPLACE FUNCTION ticket_board.open_ticket_context_restores()
RETURNS SETOF jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('open_ticket_context_restores');
    RETURN QUERY SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r WHERE r.outcome IS NULL ORDER BY r.id;
END;
$$;

-- Bound contexts of tickets their author is working now: what a restarted
-- provider must be put back into.
CREATE OR REPLACE FUNCTION ticket_board.held_ticket_contexts()
RETURNS SETOF jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('held_ticket_contexts');
    IF ticket_board.declared_scheduling() IS NULL THEN RETURN; END IF;
    RETURN QUERY SELECT to_jsonb(x) FROM ticket_board.ticket_role_contexts x JOIN ticket_board.tickets t ON t.id=x.ticket_id
     WHERE x.state='bound' AND t.assignee=x.role AND ticket_board.declared_stage_kind(t.state)='implementation'
       AND NOT ticket_board.ticket_context_superseded(x.ticket_id, x.role)
       AND NOT ticket_board.ticket_context_parked(x.ticket_id, x.role);
END;
$$;

GRANT SELECT ON ticket_board.ticket_role_contexts, ticket_board.ticket_context_restores TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.pending_ticket_contexts() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.record_ticket_context(text, text, text, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_context_status(text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.open_ticket_context_restore(text, text, text, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.mark_ticket_context_delivered(bigint) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.resolve_ticket_context_restore(bigint, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.open_ticket_context_restores() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.held_ticket_contexts() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_ticket_context_probe(bigint) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.set_ticket_context_checkout(bigint, text, jsonb) TO ticket_board_listener;

COMMIT;
