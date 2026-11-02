BEGIN;

-- SYRD-725 (pgu1026): the User's answer to an operator wait reaches the Director, who alone can release it.
--
-- A ticket blocked by operator:<name> waits on a person outside the board, and only the Director (whoever holds
-- set_blockers) releases it. When the User answered on SYRD-691, add_comment told the ticket's owner, Ops, who
-- cannot release it; the Director, who can, heard nothing until the User noticed the stall, since a wait on the
-- Director is refused while such a blocker stands.
--
-- Now a User comment on a ticket that is not finished and has an unresolved operator blocker tells the Director,
-- once, and not the owner: the Director is who can act on it while the blocker stands, and its release tells the
-- owner, once, with what it recorded. The comment is stored exactly as before. Nothing reads its text and nothing
-- is released by it: it is evidence for the Director to inspect. The board records no tie between the User and an
-- operator's name, so the notice says the User answered while the named blocker stands, and claims no more.
-- Every other comment keeps its routing, an urgent one included.

-- Each User comment the Director was told of, once: a notice is never raised twice for one comment, whatever is
-- retried or replayed, and the record outlives the queue row its delivery removes.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_operator_responses (
    comment_id bigint PRIMARY KEY REFERENCES ticket_board.ticket_comments(id) ON DELETE CASCADE,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    operators text[] NOT NULL,
    urgent boolean NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The operator blockers standing on a ticket, in their order.
CREATE OR REPLACE FUNCTION ticket_board.standing_operator_blockers(p_ticket text)
RETURNS text[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(array_agg(b.blocker_ticket_id ORDER BY b.position), ARRAY[]::text[])
      FROM ticket_board.ticket_blockers b
     WHERE b.ticket_id = p_ticket AND NOT b.resolved AND b.blocker_ticket_id LIKE 'operator:%';
$$;
-- Each new function is closed to PUBLIC in the transaction that creates it: the runner's own revocation comes only
-- after every migration, and rbac.sql's after that, so no pane role may call one in between. rbac.sql grants the
-- listener its one.
REVOKE EXECUTE ON FUNCTION ticket_board.standing_operator_blockers(text) FROM PUBLIC;

-- The Director's one notice of the User's comment, the newest on the ticket.
CREATE OR REPLACE FUNCTION ticket_board.notify_operator_response(p_ticket text, p_operators text[], p_urgent boolean)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ticket ticket_board.tickets%ROWTYPE;
    v_comment bigint;
    message text;
BEGIN
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket;
    SELECT c.id INTO v_comment FROM ticket_board.ticket_comments c
     WHERE c.ticket_id = p_ticket ORDER BY c.position DESC LIMIT 1;
    INSERT INTO ticket_board.ticket_operator_responses (comment_id, ticket_id, operators, urgent)
    VALUES (v_comment, p_ticket, p_operators, coalesce(p_urgent, false)) ON CONFLICT (comment_id) DO NOTHING;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    message := format('%s -- %s (in %s, owned by %s) is blocked by %s, and the User answered on it%s. Only the '
                      'Director can inspect that comment and release or keep the blocker: a comment is evidence to '
                      'read, not proof the prerequisite is met, and nothing was released.%s', p_ticket, ticket.title,
                      ticket.state, ticket.assignee, array_to_string(p_operators, ', '),
                      CASE WHEN p_urgent THEN ', marked urgent' ELSE '' END,
                      CASE WHEN ticket.assignee <> 'director' THEN format(' %s is not told while the blocker stands; '
                          'releasing it tells %s.', ticket.assignee, ticket.assignee) ELSE '' END);
    PERFORM ticket_board.enqueue_notification(
        p_ticket, 'ticket_update', 'director', message,
        jsonb_build_object('kind', 'operator_response', 'id', p_ticket, 'target_role', 'director',
                           'comment_id', v_comment, 'operators', to_jsonb(p_operators), 'owner', ticket.assignee,
                           'urgent', p_urgent, 'message', message),
        'operator_response:' || v_comment);
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.notify_operator_response(text, text[], boolean) FROM PUBLIC;

-- The listener's question before it delivers one: is a blocker it names still standing on an unfinished ticket?
-- Once the Director has released them all, the release has told the owner and this notice is moot.
CREATE OR REPLACE FUNCTION ticket_board.operator_response_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT NOT ticket_board.ticket_turn_terminal(p_payload->>'id')
           AND ticket_board.standing_operator_blockers(p_payload->>'id')
               && ARRAY(SELECT jsonb_array_elements_text(coalesce(p_payload->'operators', '[]'::jsonb)));
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.operator_response_is_current(jsonb) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text,
    urgent boolean
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
    operators text[];
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    -- The ticket's row first, as release_external_blocker and set_blockers take it: a comment and a release
    -- at once are ordered, where both appending at the same position deadlocked.
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = add_comment.id FOR NO KEY UPDATE;
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text, urgent);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', 'on', true);
    PERFORM ticket_board.touch_ticket(id);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', '', true);
    -- Read holding the ticket's row: a release committed first is seen here, and one made after waits for
    -- this comment, whose Director notice it then makes stale.
    operators := CASE WHEN comment_actor = 'user' AND NOT ticket_board.ticket_turn_terminal(id)
                      THEN ticket_board.standing_operator_blockers(id) END;
    IF cardinality(operators) > 0 THEN
        PERFORM ticket_board.notify_operator_response(id, operators, urgent);
    ELSIF urgent OR comment_actor IN ('director', 'user') THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_external_blocker(
    p_ticket text,
    p_ref text,
    p_reason text,
    p_evidence text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    v_ticket text := upper(btrim(coalesce(p_ticket, '')));
    ref text := ticket_board.normalize_blocker_ref(p_ref);
    reason text := btrim(coalesce(p_reason, ''));
    evidence text := btrim(coalesce(p_evidence, ''));
    released_text text;
BEGIN
    -- The authority that sets a blocker is the one that releases it: the
    -- Director's, or on a declared board whoever holds set_blockers.
    PERFORM ticket_board.enter_ui_delegation('release_external_blocker', v_ticket, format('ticket %s', v_ticket));  -- SYRD-598
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    PERFORM ticket_board.leave_ui_delegation();
    IF reason = '' THEN
        RAISE EXCEPTION 'releasing an external blocker requires a reason';
    END IF;
    IF NOT ticket_board.is_external_blocker(ref) THEN
        RAISE EXCEPTION 'not an external blocker: %; a blocker on this board resolves when its ticket is done', p_ref;
    END IF;
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = v_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', v_ticket;
    END IF;

    DELETE FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = v_ticket
      AND b.blocker_ticket_id = ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % has no external blocker %', v_ticket, ref;
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b
                   WHERE b.ticket_id = v_ticket) THEN
        UPDATE ticket_board.tickets t SET blocked_reason = '' WHERE t.id = v_ticket;
    END IF;

    -- The release is on the record, with why and, where the caller verified
    -- one, what this board itself now recognizes.
    released_text := format('External blocker %s released: %s', ref, reason);
    IF evidence <> '' THEN
        released_text := released_text || E'\n' || evidence;
    END IF;
    PERFORM ticket_board.append_ticket_comment(v_ticket, ticket_board.current_app_actor(), released_text, false);
    PERFORM ticket_board.touch_ticket(v_ticket);
    PERFORM ticket_board.refresh_ticket_source_json(v_ticket);
    -- The owner is told, as for any change to the ticket's blockers. Nothing is
    -- moved: whatever the wait was holding back still goes through its gates.
    -- SYRD-725: with what the release recorded (the evidence is in the comment), so the owner's one
    -- wake-up says what changed.
    PERFORM ticket_board.notify_ticket_owner_in_place_change(
        v_ticket, format('external blocker released (%s): %s', ref, left(reason, 400)));
END;
$$;

COMMIT;
