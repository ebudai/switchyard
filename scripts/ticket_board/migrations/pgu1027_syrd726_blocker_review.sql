BEGIN;

-- SYRD-726 (pgu1027): the owner asks the Director to review a standing external blocker.
--
-- SYRD-725 sends the User's answer to an operator wait to the Director. The other edge stayed silent: on SYRD-693
-- the Director kept operator:eric standing after Eric's approval, until the approved canary was done; Main did it,
-- recorded the evidence, and could not tell the Director the blocker was ready to release -- request-dependency is
-- refused while an external blocker stands, and a role's plain comment notifies nobody.
--
-- request_blocker_review: the ticket's current owner, from its registered live pane, names one standing external
-- blocker on a ticket it owns in implementation and says why it is ready. A blocked ticket gives up its owner's
-- serial slot, so owning it in that stage is what makes it the owner's current work. The Director gets one notice with the reference and the
-- reason; the owner none. It is recorded on the ticket as the owner's comment and in ticket_blocker_reviews. The
-- blocker, the stage, the assignee, sign-offs and every authority stay as they were: only the Director releases,
-- replaces or keeps a blocker, and the release tells the owner once, as before.
--
-- Bounded: the same reason again, while the blocker stands unchanged, is the request already made. A different one
-- waits until the Director has acted -- a Director comment on the ticket since the request -- or the blocker has
-- changed: released, or recorded again by set_blockers, either of which ends the request.

CREATE TABLE IF NOT EXISTS ticket_board.ticket_blocker_reviews (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    ref text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> '' AND char_length(reason) <= 2000),
    requested_by text NOT NULL,
    runtime_generation bigint NOT NULL,
    comment_id bigint NOT NULL REFERENCES ticket_board.ticket_comments(id) ON DELETE CASCADE,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When the blocker it names was released or recorded again: the request is over.
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS ticket_blocker_reviews_open_idx
    ON ticket_board.ticket_blocker_reviews (ticket_id, ref) WHERE ended_at IS NULL;

-- A blocker row removed -- released, or deleted by set_blockers to record the list again -- ends the requests about
-- it. set_blockers' re-insert is a new recording, so a request after it is a new request.
CREATE OR REPLACE FUNCTION ticket_board.end_blocker_reviews()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    UPDATE ticket_board.ticket_blocker_reviews SET ended_at = clock_timestamp()
     WHERE ticket_id = OLD.ticket_id AND ref = OLD.blocker_ticket_id AND ended_at IS NULL;
    RETURN NULL;
END;
$$;
-- Each new function is closed to PUBLIC in the transaction that creates it (SYRD-725): the runner's revocation
-- comes after every migration and rbac.sql's after that. rbac.sql grants the service and the listener their one each.
REVOKE EXECUTE ON FUNCTION ticket_board.end_blocker_reviews() FROM PUBLIC;
DROP TRIGGER IF EXISTS ticket_blockers_end_reviews ON ticket_board.ticket_blockers;
CREATE TRIGGER ticket_blockers_end_reviews AFTER DELETE ON ticket_board.ticket_blockers
    FOR EACH ROW EXECUTE FUNCTION ticket_board.end_blocker_reviews();

CREATE OR REPLACE FUNCTION ticket_board.request_blocker_review(
    p_ticket text, p_ref text, p_reason text, p_pid bigint, p_start_time bigint, p_uid bigint)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    v_caller text;
    v_ref text := ticket_board.normalize_blocker_ref(p_ref);
    v_reason text := btrim(coalesce(p_reason, ''));
    v_ticket ticket_board.tickets%ROWTYPE;
    v_generation bigint;
    v_last ticket_board.ticket_blocker_reviews%ROWTYPE;
    v_comment bigint;
    v_review bigint;
    v_message text;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['main', 'app', 'ops', 'perf', 'research'], 'await_role');
    v_caller := ticket_board.current_app_actor();
    -- The row lock orders this against a release, set_blockers, a transition or a reassignment, which take it too.
    SELECT * INTO v_ticket FROM ticket_board.tickets t WHERE t.id = upper(btrim(coalesce(p_ticket, ''))) FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    IF v_caller IS NULL OR v_ticket.assignee IS DISTINCT FROM v_caller OR NOT EXISTS (
        SELECT FROM ticket_board.workflow_stages ws
         WHERE ws.name = v_ticket.state
           AND ticket_board.declared_stage_kind(ws.name) = 'implementation'
           AND v_caller = ANY (ws.owner_roles)) THEN
        RAISE EXCEPTION '% may ask for a blocker review only of a ticket it owns in implementation; % is %/%',
            coalesce(v_caller, '<none>'), v_ticket.id, v_ticket.state, v_ticket.assignee USING ERRCODE = '42501';
    END IF;
    -- The registration the board server resolves a pane by, as set_blocked_reason checks it (SYRD-723).
    SELECT a.generation INTO v_generation
      FROM ticket_board.role_runtime_assignments a JOIN ticket_board.workflow_roles r ON r.name = a.role
     WHERE a.role = v_caller AND a.process_pid = p_pid AND a.process_start_time = p_start_time
       AND a.process_uid = p_uid AND r.definition->>'runtime' = a.runtime
       AND r.definition->>'target' = a.actual_target;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'process % is not %''s registered live runtime: a retired, replaced or detached process '
            'asks for no review', p_pid, v_caller USING ERRCODE = '42501';
    END IF;
    IF NOT ticket_board.is_external_blocker(v_ref) OR NOT EXISTS (
        SELECT FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = v_ticket.id AND b.blocker_ticket_id = v_ref AND NOT b.resolved) THEN
        RAISE EXCEPTION '% has no standing external blocker %: name one of %', v_ticket.id, coalesce(v_ref, p_ref),
            coalesce(ticket_board.unresolved_blocker_list(v_ticket.id), 'none');
    END IF;
    IF v_reason = '' OR char_length(v_reason) > 2000 THEN
        RAISE EXCEPTION 'a blocker review needs the reason it is ready, at most 2000 characters, with the evidence';
    END IF;
    SELECT * INTO v_last FROM ticket_board.ticket_blocker_reviews r
     WHERE r.ticket_id = v_ticket.id AND r.ref = v_ref AND r.ended_at IS NULL ORDER BY r.id DESC LIMIT 1;
    IF FOUND AND v_last.reason = v_reason THEN
        RETURN jsonb_build_object('ticket', v_ticket.id, 'ref', v_ref, 'review', v_last.id, 'duplicate', true);
    END IF;
    IF FOUND AND NOT EXISTS (SELECT FROM ticket_board.ticket_comments c
                              WHERE c.ticket_id = v_ticket.id AND c.id > v_last.comment_id AND c.who = 'director') THEN
        RAISE EXCEPTION 'a review of % was asked for at %, and the Director has not acted on it: a Director comment, '
            'a release or the blocker recorded again answers it. Put anything new in a comment meanwhile.', v_ref,
            ticket_board.utc_text(v_last.requested_at);
    END IF;
    PERFORM ticket_board.append_ticket_comment(v_ticket.id, v_caller,
        format('Review of %s requested: %s', v_ref, v_reason), false);
    SELECT c.id INTO v_comment FROM ticket_board.ticket_comments c
     WHERE c.ticket_id = v_ticket.id ORDER BY c.position DESC LIMIT 1;
    INSERT INTO ticket_board.ticket_blocker_reviews (ticket_id, ref, reason, requested_by, runtime_generation,
                                                     comment_id)
    VALUES (v_ticket.id, v_ref, v_reason, v_caller, v_generation, v_comment) RETURNING id INTO v_review;
    -- The ticket's stored document carries the new comment. No wait can stand beside an external blocker, so the
    -- touch has none to clear.
    PERFORM ticket_board.touch_ticket(v_ticket.id);
    v_message := format('%s -- %s (in %s): %s asks you to review %s, which still blocks it: "%s" Release it with the '
                        'result (that tells %s once), keep it and say why in a comment, or replace it. Nothing was '
                        'released or moved.', v_ticket.id, v_ticket.title, v_ticket.state, v_caller, v_ref,
                        left(v_reason, 600), v_caller);
    PERFORM ticket_board.enqueue_notification(
        v_ticket.id, 'ticket_update', 'director', v_message,
        jsonb_build_object('kind', 'blocker_review', 'id', v_ticket.id, 'target_role', 'director', 'review', v_review,
                           'ref', v_ref, 'requested_by', v_caller, 'reason', v_reason, 'message', v_message),
        'blocker_review:' || v_review);
    RETURN jsonb_build_object('ticket', v_ticket.id, 'ref', v_ref, 'review', v_review, 'duplicate', false);
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.request_blocker_review(text, text, text, bigint, bigint, bigint) FROM PUBLIC;

-- The listener's question before it delivers one: has the request not ended? Its blocker's removal ends it (the
-- trigger above), whether released -- which tells the owner -- or recorded again by the Director; and no ticket
-- finishes while a blocker stands.
CREATE OR REPLACE FUNCTION ticket_board.blocker_review_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.ticket_blocker_reviews r
                    WHERE r.id = (p_payload->>'review')::bigint AND r.ended_at IS NULL);
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.blocker_review_is_current(jsonb) FROM PUBLIC;

COMMIT;
