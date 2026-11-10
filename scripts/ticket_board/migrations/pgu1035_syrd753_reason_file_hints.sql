-- SYRD-753 (pgu1035): generated board hints read free-form reasons from a file.
--
-- On MEFP (2026-11-10) a Codex role wrote `ticket-board-write ... --reason "..."` with a command quoted in Markdown
-- backticks inside the double quotes; Codex runs a tool call as `bash -c`, so the shell ran it. Every instruction
-- Switchyard generates now shows free text through a quoted heredoc or a file, which the shell
-- leaves literal. These four functions emitted the double-quoted form: the external-blocker refusal in
-- apply_blockers, the clear-early hint of note_provider_quota and provider_hold_status, and the clear hint of
-- begin_provider_account_hold. Each is its owning migration's copy (pgu990, pgu1011, pgu1011, pgu1030) with only that
-- placeholder changed to --reason-file PATH. No new function; grants are unchanged.
--
-- schema.sql carries the same functions.

BEGIN;

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
    own_board_ref text;
    missing_id text;
    cycle_id text;
    standing_ref text;
BEGIN
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT coalesce(nullif(ticket_board.normalize_blocker_ref(raw_id), ''), '<empty>')
    INTO invalid_id
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    WHERE raw_id IS NULL
       OR btrim(raw_id) = ''
       OR NOT (
           ticket_board.normalize_blocker_ref(raw_id) ~ ticket_board.ticket_id_pattern()
           OR ticket_board.is_external_blocker(ticket_board.normalize_blocker_ref(raw_id))
       )
       OR ticket_board.normalize_blocker_ref(raw_id) = p_ticket_id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    -- SYRD-270: a qualified reference is for work this board does not hold.
    -- One naming this board's own ticket would never resolve when that ticket
    -- is done, which is exactly what a local blocker is for.
    SELECT ref
    INTO own_board_ref
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS ref
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    ) AS refs
    WHERE ticket_board.is_external_blocker(ref)
      AND split_part(ref, ':', 2) ~ ('^' || ticket_board.ticket_prefix() || '-[0-9]+$')
    LIMIT 1;
    IF own_board_ref IS NOT NULL THEN
        RAISE EXCEPTION 'external blocker % names a ticket on this board; block on % instead',
            own_board_ref, split_part(own_board_ref, ':', 2);
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = p_ticket_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    WITH normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    )
    SELECT normalized.blocker_id
    INTO missing_id
    FROM normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    WHERE blocker.id IS NULL
      AND NOT ticket_board.is_external_blocker(normalized.blocker_id)
    LIMIT 1;
    IF missing_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocker ticket not found: %', missing_id;
    END IF;

    WITH RECURSIVE normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
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

    -- SYRD-451: another board's work or a person is a wait nothing on this
    -- board resolves, so it ends only by its release, whose reason records
    -- what happened (release_external_blocker). Replacing or clearing the
    -- list must still name every one that stands; dropping one here ended the
    -- wait with no reason and no record of the person's answer.
    SELECT b.blocker_ticket_id
    INTO standing_ref
    FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = p_ticket_id
      AND ticket_board.is_external_blocker(b.blocker_ticket_id)
      AND b.blocker_ticket_id <> ALL (ARRAY(
          SELECT ticket_board.normalize_blocker_ref(raw_id)
          FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
      ))
    ORDER BY b.position
    LIMIT 1;
    IF standing_ref IS NOT NULL THEN
        RAISE EXCEPTION 'external blocker % stands until it is released: release-external-blocker % --ref % --reason-file PATH',
            standing_ref, p_ticket_id, standing_ref;
    END IF;

    DELETE FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id;

    -- An external blocker has no ticket here, so it is unresolved by
    -- construction; only release_external_blocker removes it.
    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
    SELECT p_ticket_id, normalized.blocker_id, min(normalized.ord)::integer - 1,
           coalesce(bool_or(blocker.state IN ('done', 'cancelled')), false)
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id, ord
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
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

CREATE OR REPLACE FUNCTION ticket_board.note_provider_quota(p_role text, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    reg ticket_board.role_runtime_assignments;
    cur ticket_board.provider_quota_holds;
    last_end timestamptz;
    v_observed timestamptz := (p_evidence->>'observed_at')::timestamptz;
    v_turn text := coalesce(p_evidence->>'turn', '');
    v_reset timestamptz := nullif(p_evidence->>'reset_at', '')::timestamptz;
    grace interval := make_interval(secs => least(greatest(coalesce((p_evidence->>'grace_seconds')::numeric, 120), 0), 3600));
    recheck interval := make_interval(secs => least(greatest(coalesce((p_evidence->>'recheck_seconds')::numeric, 1800), 1), 86400));
    v_deadline timestamptz;
    v_evidence jsonb;
    sibling ticket_board.provider_quota_holds;
    anchor text;
    new_id bigint;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_provider_quota');
    IF v_observed IS NULL THEN RAISE EXCEPTION 'quota evidence needs the time it was observed'; END IF;
    SELECT * INTO reg FROM ticket_board.role_runtime_assignments a WHERE a.role = p_role;
    IF reg.role IS NULL OR reg.runtime IS DISTINCT FROM p_evidence->>'runtime'
       OR reg.generation IS DISTINCT FROM (p_evidence->>'generation')::bigint
       OR reg.process_pid IS DISTINCT FROM (p_evidence->>'pid')::integer
       OR reg.process_start_time IS DISTINCT FROM (p_evidence->>'start_time')::bigint THEN
        RETURN jsonb_build_object('ignored', 'not from the registered provider process');
    END IF;
    -- A reset already past is no reset: the deadline is a recheck from now.
    v_deadline := CASE WHEN v_reset IS NOT NULL AND v_reset + grace > clock_timestamp() THEN v_reset + grace
                     ELSE greatest(v_observed, clock_timestamp()) + recheck END;
    v_evidence := jsonb_build_object('source', p_evidence->>'source', 'observed_at', v_observed, 'turn', v_turn,
                                   'limit', coalesce(p_evidence->>'limit', ''),
                                   'message', left(coalesce(p_evidence->>'message', ''), 240),
                                   'reset_given', v_reset IS NOT NULL)
                  || CASE WHEN p_evidence ? 'count' THEN jsonb_build_object('count', (p_evidence->>'count')::int)
                          ELSE '{}'::jsonb END;
    SELECT * INTO cur FROM ticket_board.provider_quota_holds h
    WHERE h.role = p_role AND h.state IN ('held', 'released') FOR UPDATE;
    IF cur.id IS NOT NULL AND (cur.generation <> reg.generation OR cur.pid IS DISTINCT FROM reg.process_pid
                               OR cur.process_start IS DISTINCT FROM reg.process_start_time) THEN
        PERFORM ticket_board.end_provider_hold(cur.id, 'replaced');
        cur := NULL;
    END IF;
    -- Past its deadline it is released, whether or not a pass has said so yet:
    -- the attempt that follows may already have gone, and its answer is new.
    IF cur.id IS NOT NULL AND cur.state = 'held' AND cur.deadline <= clock_timestamp() THEN
        PERFORM ticket_board.release_provider_hold(cur.id, cur.deadline);
        cur.state := 'released';
        cur.released_at := cur.deadline;
    END IF;
    IF cur.id IS NOT NULL THEN
        -- Each turn is named by its evidence time, so newer is a new turn. A
        -- screen's evidence must also have grown: one more quota line than the
        -- hold last saw, since a turn id can move before the screen is redrawn.
        IF v_observed <= cur.evidence_at
           OR (p_evidence ? 'count' AND cur.evidence ? 'count'
               AND (p_evidence->>'count')::int <= (cur.evidence->>'count')::int)
           OR (cur.state = 'released' AND v_observed <= cur.released_at) THEN
            RETURN jsonb_build_object('hold', cur.id, 'stale', true);
        END IF;
        UPDATE ticket_board.provider_quota_holds h
        SET state = 'held', evidence = v_evidence, evidence_at = v_observed, evidence_turn = v_turn,
            reset_at = v_reset, deadline = v_deadline,
            provider_session = coalesce(nullif(p_evidence->>'session', ''), h.provider_session),
            renewals = h.renewals + CASE WHEN cur.state = 'released' THEN 1 ELSE 0 END,
            history = h.history || jsonb_build_array(jsonb_build_object(
                'at', clock_timestamp(), 'event', CASE WHEN cur.state = 'released' THEN 'renewed' ELSE 'repeated' END,
                'observed_at', v_observed, 'turn', v_turn, 'deadline', v_deadline))
        WHERE h.id = cur.id;
        RETURN jsonb_build_object('hold', cur.id, CASE WHEN cur.state = 'released' THEN 'renewed' ELSE 'repeated' END, true);
    END IF;
    -- Nothing older than the role's last ended hold: evidence from before an
    -- invalidation or a clear is what that ending already answered.
    SELECT max(h.ended_at) INTO last_end FROM ticket_board.provider_quota_holds h WHERE h.role = p_role;
    IF last_end IS NOT NULL AND v_observed <= last_end THEN
        RETURN jsonb_build_object('stale', true, 'reason', 'older than the role''s last ended hold');
    END IF;
    -- Siblings on one account: a hold on the same runtime and reset window is
    -- already the Director's news.
    SELECT * INTO sibling FROM ticket_board.provider_quota_holds h
    WHERE h.state = 'held' AND h.runtime = reg.runtime AND h.role <> p_role AND h.notified
      AND (h.reset_at = v_reset OR (h.reset_at IS NULL AND v_reset IS NULL AND abs(extract(epoch FROM h.deadline - v_deadline)) < 600))
    ORDER BY h.id LIMIT 1;
    INSERT INTO ticket_board.provider_quota_holds (role, runtime, generation, pid, process_start, provider_session, target,
                                                   state, evidence, evidence_at, evidence_turn, reset_at, deadline,
                                                   joined_hold, history)
    VALUES (p_role, reg.runtime, reg.generation, reg.process_pid, reg.process_start_time,
            coalesce(p_evidence->>'session', ''), coalesce(p_evidence->>'target', reg.actual_target, ''), 'held',
            v_evidence, v_observed, v_turn, v_reset, v_deadline, sibling.id,
            jsonb_build_array(jsonb_build_object('at', clock_timestamp(), 'event', 'began', 'observed_at', v_observed,
                                                 'turn', v_turn, 'deadline', v_deadline)))
    RETURNING id INTO new_id;
    anchor := ticket_board.provider_hold_anchor(p_role);
    IF sibling.id IS NULL AND anchor IS NOT NULL THEN
        PERFORM ticket_board.enqueue_notification(
            anchor, 'escalation', 'director',
            format('%s''s %s account is out of quota%s: "%s". Its deliveries and nudges, and alerts about it, wait '
                   'until %s%s; then it gets one fresh try. Its tickets, reservations and context are untouched. '
                   'Clear early with: ticket-board-write clear-provider-hold %s --reason-file PATH',
                   p_role, reg.runtime, CASE WHEN p_evidence->>'limit' <> '' THEN ' (' || (p_evidence->>'limit') || ')' ELSE '' END,
                   left(coalesce(p_evidence->>'message', ''), 240), to_char(v_deadline AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'),
                   CASE WHEN v_reset IS NULL THEN ' (the provider gave no reset time; this is a recheck)' ELSE ' (its reset, plus a grace)' END,
                   p_role),
            -- Its own kind, as the Director's other alerts: an open stall episode
            -- lets only its own escalation through (SYRD-595), and this is news
            -- of a different kind about the same role.
            jsonb_build_object('kind', 'provider_quota_hold', 'reason', 'provider_quota_hold', 'id', anchor, 'role', p_role,
                               'owner_role', p_role, 'runtime', reg.runtime, 'hold', new_id, 'deadline', v_deadline,
                               'reset_at', v_reset, 'target_role', 'director'),
            format('provider-quota-hold:%s', new_id));
        UPDATE ticket_board.provider_quota_holds SET notified = true WHERE id = new_id;
    END IF;
    RETURN jsonb_build_object('hold', new_id, 'began', true, 'joined', sibling.id, 'notified', sibling.id IS NULL AND anchor IS NOT NULL);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.provider_hold_status(p_now timestamptz)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object('holds', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'id', h.id, 'role', h.role, 'runtime', h.runtime, 'state', h.state,
            'current', h.state = 'held' AND h.deadline > p_now, 'evidence_at', h.evidence_at,
            'source', h.evidence->>'source', 'limit', h.evidence->>'limit', 'message', h.evidence->>'message',
            'reset_at', h.reset_at, 'deadline', h.deadline, 'recheck', NOT coalesce((h.evidence->>'reset_given')::boolean, false),
            'renewals', h.renewals, 'released_at', h.released_at, 'ended_at', h.ended_at, 'end_reason', h.end_reason,
            'joined_hold', h.joined_hold,
            'clear', CASE WHEN h.state IN ('held', 'released')
                          THEN format('ticket-board-write clear-provider-hold %s --reason-file PATH', h.role) END)
            ORDER BY h.state = 'ended', h.id DESC)
        FROM (SELECT * FROM ticket_board.provider_quota_holds
              WHERE state IN ('held', 'released') OR ended_at > p_now - interval '1 day'
              ORDER BY id DESC LIMIT 50) h), '[]'::jsonb));
$$;

CREATE OR REPLACE FUNCTION ticket_board.begin_provider_account_hold(
    p_runtime text, p_kind text, p_category text, p_source text, p_evidence text, p_role text, p_by text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    new_id bigint;
    v_ticket text;
    v_event bigint;
    v_director_held boolean := ticket_board.role_account_runtime('director') IS NOT DISTINCT FROM p_runtime;
    v_roles text;
BEGIN
    v_ticket := CASE WHEN p_role <> '' THEN ticket_board.provider_hold_anchor(p_role) END;
    INSERT INTO ticket_board.provider_account_holds (runtime, kind, category, source, evidence, role, ticket_id,
                                                     began_by, reason)
    VALUES (p_runtime, p_kind, coalesce(p_category, ''), coalesce(p_source, ''), coalesce(p_evidence, ''),
            coalesce(p_role, ''), v_ticket, p_by, coalesce(p_reason, ''))
    RETURNING id INTO new_id;
    INSERT INTO ticket_board.user_events (kind, ticket_id, detail) VALUES ('provider_safety_hold', NULL, p_runtime)
    RETURNING id INTO v_event;
    UPDATE ticket_board.provider_account_holds SET user_event_id = v_event WHERE id = new_id;
    IF NOT v_director_held AND v_ticket IS NOT NULL THEN
        SELECT string_agg(w.name, ', ' ORDER BY w.name) INTO v_roles FROM ticket_board.workflow_roles w
         WHERE ticket_board.role_account_runtime(w.name) = p_runtime;
        PERFORM ticket_board.enqueue_notification(
            v_ticket, 'escalation', 'director',
            format('%s account held (hold %s): %s. Every role on it waits -- %s -- with no delivery, retry, reminder, '
                   'start, claim or route, and no request to the provider. Tickets, reservations and context are kept. '
                   'Choose: inspect it by hand; move the work to another provider (switchyard set-role-runtime); or, '
                   'once the operator confirms the account is safe, clear it: '
                   'ticket-board-write clear-provider-account-hold %s --reason-file PATH',
                   p_runtime, new_id,
                   CASE p_kind WHEN 'refusal' THEN format('%s''s provider refused a request on safety grounds%s',
                                                          p_role, CASE WHEN p_category <> '' THEN ' (' || p_category || ')' ELSE '' END)
                               ELSE 'held by hand as a suspected safety refusal' END,
                   coalesce(v_roles, 'none'), p_runtime),
            jsonb_build_object('kind', 'provider_safety_hold', 'reason', 'provider_safety_hold', 'id', v_ticket,
                               'runtime', p_runtime, 'hold', new_id, 'role', p_role, 'target_role', 'director'),
            format('provider-safety-hold:%s', new_id));
        UPDATE ticket_board.provider_account_holds SET notified_director = true WHERE id = new_id;
    END IF;
    RETURN jsonb_build_object('hold', new_id, 'began', true, 'runtime', p_runtime, 'director_held', v_director_held,
                              'notified_director', NOT v_director_held AND v_ticket IS NOT NULL, 'user_event', v_event);
END;
$$;

COMMIT;
