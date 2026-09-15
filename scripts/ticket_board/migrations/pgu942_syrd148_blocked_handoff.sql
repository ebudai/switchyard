-- SYRD-148: a blocked ticket handed work to a role that could not receive it.
--
-- Live on SYRD-146: blocked_by SYRD-147 with resolved=false, and an
-- await_role=director handoff was accepted anyway and delivered. An
-- awaiting-role handoff is an actionable instruction -- a named role is told
-- to act now, the ticket's nudges are suppressed while it stands, and after
-- thirty minutes the Director is told that role ignored it. While a dependency
-- is unresolved every part of that is false.
--
-- The same rule now holds in three places, because there are three ways a
-- false handoff can exist. `set_awaiting_role` refuses to create one and names
-- the blockers in the way. `apply_blockers` retires a wait a later blocker has
-- overtaken, in the same write that creates the blocker. And
-- `enqueue_awaiting_role_handoff` will not schedule the burst for a blocked
-- ticket, which is what catches a row a board carries in across this upgrade.
--
-- Both definitions of the two overridden functions are reinstalled here. The
-- declared-workflow copies override the legacy ones, so a guard added only to
-- the first is a guard a board with a declared workflow never runs -- which a
-- regression case caught before this shipped.
--
-- Deliberately narrow: serial-focus queue announcements, the unblock
-- notification that fires when a blocker resolves, and every other
-- notification kind are untouched, and an ordinary handoff once the blockers
-- resolve is unchanged.

BEGIN;

-- SYRD-148: which unresolved blockers stand between a ticket and a handoff.
-- Returned as text so the refusal names them: "this is blocked" sends the
-- reader back to the board, "blocked by SYRD-147" sends them to the ticket
-- that has to move first.
CREATE OR REPLACE FUNCTION ticket_board.unresolved_blocker_list(p_ticket_id text)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT string_agg(b.blocker_ticket_id, ', ' ORDER BY b.position)
    FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = p_ticket_id AND NOT b.resolved;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF t.state NOT IN ('in_progress', 'inspection', 'audit')
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    -- SYRD-148: a handoff says a named role can act now. While a blocker is
    -- unresolved that is false, and the whole schedule below -- four
    -- notifications ending in an escalation to the Director -- would be false
    -- with it. The write boundary refuses to create such a wait; this is the
    -- delivery side of the same rule, and it is what catches a wait that was
    -- legitimate when it was made and was overtaken by a blocker.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.enqueue_awaiting_role_handoff(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF normalized_role NOT IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- SYRD-148: an awaiting-role handoff is an actionable instruction to
    -- somebody else. A ticket whose own dependency has not resolved cannot
    -- give one: the named role would be told to act on work that cannot
    -- proceed, and after thirty minutes the Director would be told they had
    -- ignored it. Refused rather than accepted quietly, and the blockers are
    -- named so the caller knows what has to move first.
    IF ticket_board.ticket_has_unresolved_blockers(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents an awaiting-role handoff: %',
            ticket_board.unresolved_blocker_list(set_awaiting_role.id);
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND t.state IN ('in_progress', 'inspection', 'audit')
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND t.state IN ('in_progress', 'inspection', 'audit');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.apply_blockers(
    p_ticket_id text,
    p_blocker_ids text[],
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    normalized_reason text := coalesce(p_reason, '');
    blocker_count integer;
    invalid_id text;
    missing_id text;
    cycle_id text;
BEGIN
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT coalesce(nullif(upper(btrim(raw_id)), ''), '<empty>')
    INTO invalid_id
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    WHERE raw_id IS NULL
       OR btrim(raw_id) = ''
       OR upper(btrim(raw_id)) !~ ticket_board.ticket_id_pattern()
       OR upper(btrim(raw_id)) = p_ticket_id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = p_ticket_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    WITH normalized AS (
        SELECT upper(btrim(raw_id)) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY upper(btrim(raw_id))
    )
    SELECT normalized.blocker_id
    INTO missing_id
    FROM normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    WHERE blocker.id IS NULL
    LIMIT 1;
    IF missing_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocker ticket not found: %', missing_id;
    END IF;

    WITH RECURSIVE normalized AS (
        SELECT upper(btrim(raw_id)) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY upper(btrim(raw_id))
    ),
    blocker_chain(ticket_id) AS (
        SELECT blocker_id
        FROM normalized
        UNION
        SELECT tb.blocker_ticket_id
        FROM blocker_chain chain
        JOIN ticket_board.ticket_blockers tb ON tb.ticket_id = chain.ticket_id
    )
    SELECT ticket_id
    INTO cycle_id
    FROM blocker_chain
    WHERE ticket_id = p_ticket_id
    LIMIT 1;
    IF cycle_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocked_by cycle detected for %', p_ticket_id;
    END IF;

    DELETE FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id;

    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
    SELECT p_ticket_id, normalized.blocker_id, min(normalized.ord)::integer - 1, bool_or(blocker.state IN ('done', 'cancelled'))
    FROM (
        SELECT upper(btrim(raw_id)) AS blocker_id, ord
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    GROUP BY normalized.blocker_id
    ORDER BY min(normalized.ord);

    UPDATE ticket_board.tickets
    SET blocked_reason = normalized_reason
    WHERE tickets.id = p_ticket_id;

    -- SYRD-148: a wait made before the blocker is not made true by it. Cleared
    -- here rather than beside the callers, so it happens in the same write that
    -- creates the blocker and no reader can see the pair disagree; and cleared
    -- rather than left for the delivery side to skip, because the wait is also
    -- what suppresses nudges and what the board shows the Director.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        UPDATE ticket_board.ticket_notification_state
        SET awaiting_role = '',
            awaiting_since_at = NULL,
            awaiting_notified_since_at = NULL
        WHERE ticket_id = p_ticket_id
          AND (awaiting_role <> '' OR awaiting_since_at IS NOT NULL);
    END IF;

    PERFORM ticket_board.refresh_ticket_source_json(p_ticket_id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enqueue_awaiting_role_handoff(p_ticket_id text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    ns ticket_board.ticket_notification_state%ROWTYPE;
    step integer;
    offsets interval[] := ARRAY[interval '0', interval '5 minutes', interval '15 minutes', interval '30 minutes', interval '4 hours'];
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id FOR UPDATE;
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF NOT ticket_board.workflow_wait_stage(t.state)
       OR ns.awaiting_role = '' OR ns.awaiting_since_at IS NULL
       OR ns.awaiting_notified_since_at = ns.awaiting_since_at THEN
        RETURN;
    END IF;
    -- SYRD-148: the same rule as the legacy definition above. This is the one
    -- a board with a declared workflow actually runs, and it overrides that
    -- one, so a guard added only there is a guard this board never reaches.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;
    FOR step IN 1..4 LOOP
        recipient := CASE WHEN step = 4 THEN 'director' ELSE ns.awaiting_role END;
        PERFORM ticket_board.enqueue_notification(
            t.id, 'awaiting_role', recipient,
            format('%s -- %s: %s is awaiting %s. %s Read the ticket and act or explicitly clear/retarget the wait.',
                t.id, t.title, t.assignee, ns.awaiting_role,
                CASE WHEN step = 4 THEN 'Unresolved handoff escalated to director after 30 minutes.'
                     WHEN step = 1 THEN 'New handoff.' ELSE 'Handoff remains unresolved.' END),
            jsonb_build_object('kind', 'awaiting_role', 'id', t.id, 'state', t.state,
                'assignee', t.assignee, 'awaiting_role', ns.awaiting_role,
                'awaiting_since_at', ns.awaiting_since_at, 'step', step,
                'expires_at', ns.awaiting_since_at + offsets[step + 1]),
            format('awaiting_role:%s:%s:%s', t.id, ns.awaiting_since_at, step),
            ns.awaiting_since_at + offsets[step]);
    END LOOP;
    UPDATE ticket_board.ticket_notification_state
    SET awaiting_notified_since_at = ns.awaiting_since_at WHERE ticket_id = p_ticket_id;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_awaiting_role(
    id text,
    awaiting_role text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    normalized_role text := lower(btrim(coalesce(awaiting_role, '')));
BEGIN
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    IF NOT (CASE WHEN ticket_board.declared_workflow() IS NULL THEN normalized_role IN ('director', 'main', 'app', 'perf', 'ops', 'audit', 'inspector', 'research') ELSE EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=normalized_role AND (r.definition->>'active')::boolean AND r.definition->>'target' IS NOT NULL) END) THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    IF normalized_role = ticket_board.current_app_actor() THEN
        RAISE EXCEPTION 'awaiting_role cannot be the caller role: %', normalized_role;
    END IF;
    -- SYRD-148: an awaiting-role handoff is an actionable instruction to
    -- somebody else. A ticket whose own dependency has not resolved cannot
    -- give one: the named role would be told to act on work that cannot
    -- proceed, and after thirty minutes the Director would be told they had
    -- ignored it. Refused rather than accepted quietly, and the blockers are
    -- named so the caller knows what has to move first.
    IF ticket_board.ticket_has_unresolved_blockers(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents an awaiting-role handoff: %',
            ticket_board.unresolved_blocker_list(set_awaiting_role.id);
    END IF;
    -- Lock in ticket -> notification-state order, as ticket activity triggers do.
    PERFORM 1 FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id AND ticket_board.workflow_wait_stage(t.state)
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    IF EXISTS (SELECT 1 FROM ticket_board.ticket_notification_state ns
               WHERE ns.ticket_id = id AND ns.awaiting_role = normalized_role
                 AND ns.awaiting_since_at IS NOT NULL) THEN
        PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
        RETURN;
    END IF;
    UPDATE ticket_board.ticket_notification_state ns
    SET awaiting_role = normalized_role,
        awaiting_since_at = clock_timestamp(),
        last_activity_at = clock_timestamp(),
        nudge_count = 0
    FROM ticket_board.tickets t
    WHERE t.id = set_awaiting_role.id
      AND ns.ticket_id = t.id
      AND ticket_board.workflow_wait_stage(t.state);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'active ticket not found for awaiting_role: %', id;
    END IF;
    PERFORM ticket_board.touch_ticket(id);
    PERFORM ticket_board.enqueue_awaiting_role_handoff(id);
END;
$$;

COMMIT;
