-- SYRD-797 (pgu1045): an owner's change notice says something true when it is read.
--
-- On SAPI-69 (2026-11-15) ops was told five times that "the wait on director was cleared" and once of a "new comment"
-- that it could not find:
-- * the Director clears an awaited wait and then comments its decision, two writes; the clear's wake went at once,
--   and ops read the ticket before the comment existed. The clear's wake is now held for 30 seconds: the comment's
--   own wake replaces it (one wake per ticket and role, SYRD-159) and goes at once; with no comment it goes after;
-- * a wake queued while ops was busy was delivered after ops had raised its next wait, saying a wait was cleared
--   that stood again. The listener drops a "wait cleared" notice while a wait stands on the ticket
--   (notification_eligibility);
-- * an approval and ops' own comment crossed by two seconds, and "new comment" read as none, ops' own being newest.
--   A comment's notice now names it: "new comment at <the time the ticket shows for it>".
--
-- Each function is its last copy as schema.sql has it, with only that changed. Grants are unchanged.
--
-- schema.sql carries the same functions.

BEGIN;

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
    comment_at text;
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
        -- SYRD-797: the notice names the comment, by the time the ticket shows for it, so its reader finds it even
        -- when it is not the newest: on SAPI-69 a Director approval and ops' own comment crossed by two seconds, and
        -- ops, seeing its own comment last, read the wake as no new comment.
        SELECT c.ts_text INTO comment_at FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = add_comment.id ORDER BY c.position DESC LIMIT 1;
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment' || coalesce(' at ' || comment_at, ''));
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role(id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    cleared text;
    signed_off boolean;
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'clear_awaiting_role'
    );
    SELECT t.manually_controlled AND (ticket_board.pending_held_review(t.id) IS NOT NULL
                                      OR CASE t.state WHEN 'inspection' THEN t.inspector_signoff
                                                      WHEN 'audit' THEN t.audit_signoff
                                                      WHEN 'user_review' THEN t.user_signoff
                                                      ELSE false END)
    INTO signed_off
    FROM ticket_board.tickets t WHERE t.id = clear_awaiting_role.id FOR UPDATE;
    SELECT ns.awaiting_role INTO cleared
    FROM ticket_board.ticket_notification_state ns
    WHERE ns.ticket_id = clear_awaiting_role.id;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL,
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    WHERE ticket_id = clear_awaiting_role.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    -- SYRD-697: the ticket is its owner's work again, and its owner may be sitting idle at its prompt waiting for
    -- exactly this. Told as for any change to the ticket's blockers; an owner clearing its own wait already knows,
    -- and clearing a ticket with no wait changes nothing. Nor is a held review's completion wait (SYRD-22, SYRD-180)
    -- its owner's: the owner has approved, the approval is deferred until the hold is released, and the wait is on
    -- whoever takes the ticket next.
    IF coalesce(cleared, '') <> '' AND NOT coalesce(signed_off, false) THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, format('the wait on %s was cleared', cleared));
        -- SYRD-797: held for a moment. The awaited role usually clears the wait and then says why in a comment; told
        -- at once, the owner read the ticket before the comment existed (SAPI-69, five times). The comment's own wake
        -- replaces this one and goes at once; with no comment, this one goes after the moment.
        UPDATE ticket_board.ticket_notification_queue q
           SET next_attempt_at = clock_timestamp() + interval '30 seconds'
         WHERE q.ticket_id = clear_awaiting_role.id AND q.kind = 'ticket_update' AND q.claimed_at IS NULL
           AND q.payload->>'change_summary' = format('the wait on %s was cleared', cleared);
    END IF;
END;
$$;

COMMIT;
