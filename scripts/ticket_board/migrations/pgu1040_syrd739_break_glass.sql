-- SYRD-739 (pgu1040): a one-operation break-glass grant for a provider safety hold.
--
-- When a safety refusal holds an account (SYRD-672), the operator may start a temporary role on another provider to
-- make the one board mutation that isolates the refused task, then hand control back. Its authority is this grant,
-- never a role: a random token whose sha256 alone is kept here, for a fixed set of operations, until expires_at by the
-- database clock, and for ONE operation. The board's break-glass route takes the token alone, claims the grant here
-- (committed before anything is done, so no crash or second request reaches a second operation), performs the one
-- operation through the board's ordinary path, and finishes the grant here.
--
-- Opening, ending and handing back are root's, through the database owner's connection, and granted to no role. The
-- claim and the finish are the board service's, and nothing else's.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.break_glass_grants (
    id bigserial PRIMARY KEY,
    hold_id bigint NOT NULL REFERENCES ticket_board.provider_account_holds(id),
    operator text NOT NULL CHECK (operator ~ '^[a-z_][a-z0-9_.-]{0,31}$'),
    reason text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 2000),
    runtime text NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy', 'hermes', 'copilot')),
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    -- What the one operation may be: a subset of the fixed vocabulary. `abort` needs no grant.
    operations text[] NOT NULL CHECK (operations <> '{}' AND operations <@ ARRAY['record', 'isolate', 'reassign',
                                                                                  'unblock']::text[]),
    packet jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(packet) = 'object'),
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'operating', 'completed', 'refused', 'expired', 'aborted', 'ended')),
    -- The one request: {at, operation, args}; and what came of it: the ticket after, or the board's refusal.
    attempted jsonb,
    completed jsonb,
    ended_at timestamptz,
    ended_by text,
    -- The hand-back: whether the Director's account was eligible, and the notice sent, or the operator's route.
    handback jsonb,
    CHECK (expires_at > opened_at AND expires_at <= opened_at + interval '1 hour'),
    CHECK ((state IN ('open', 'operating')) = (ended_at IS NULL))
);
-- One grant stands at a time.
CREATE UNIQUE INDEX IF NOT EXISTS break_glass_grants_live ON ticket_board.break_glass_grants ((true))
    WHERE state IN ('open', 'operating');

-- Open a grant for a current hold, on a runtime that is not itself held. Returns its id.
CREATE OR REPLACE FUNCTION ticket_board.open_break_glass(
    p_hold bigint, p_operator text, p_reason text, p_runtime text, p_operations text[], p_ttl_seconds integer,
    p_token_sha256 text, p_packet jsonb)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    h ticket_board.provider_account_holds;
    v_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.break_glass_grants'));
    SELECT * INTO h FROM ticket_board.provider_account_holds WHERE id = p_hold;
    IF h.id IS NULL OR h.cleared_at IS NOT NULL THEN
        RAISE EXCEPTION 'hold % is not a current provider account hold', p_hold;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.provider_account_holds WHERE runtime = p_runtime AND cleared_at IS NULL) THEN
        RAISE EXCEPTION 'the % account is under a safety hold; a break-glass role runs on another', p_runtime;
    END IF;
    IF (p_packet->>'hold') IS DISTINCT FROM p_hold::text THEN
        RAISE EXCEPTION 'the recovery packet is for hold %, not hold %', coalesce(p_packet->>'hold', 'none'), p_hold;
    END IF;
    IF p_ttl_seconds IS NULL OR p_ttl_seconds < 60 OR p_ttl_seconds > 3600 THEN
        RAISE EXCEPTION 'a break-glass grant lasts 60 to 3600 seconds, not %', p_ttl_seconds;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.break_glass_grants WHERE state IN ('open', 'operating')) THEN
        RAISE EXCEPTION 'another break-glass grant stands';
    END IF;
    INSERT INTO ticket_board.break_glass_grants (hold_id, operator, reason, runtime, token_sha256, operations,
                                                 packet, expires_at)
    VALUES (p_hold, p_operator, left(btrim(p_reason), 2000), p_runtime, p_token_sha256,
            ARRAY(SELECT DISTINCT o FROM unnest(p_operations) o ORDER BY o), coalesce(p_packet, '{}'::jsonb),
            clock_timestamp() + make_interval(secs => p_ttl_seconds))
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

-- The board service's claim of a grant for its one request. Answers {grant, operation, args, hold, operator, runtime}
-- with the grant now `operating`, committed by the caller before anything is done; or {refused} with why, the grant
-- (if any) ended: an unknown or used token, an expired grant, or an operation it does not allow. `abort` ends it.
CREATE OR REPLACE FUNCTION ticket_board.claim_break_glass(p_token_sha256 text, p_operation text, p_args jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    g ticket_board.break_glass_grants;
    v_now timestamptz := clock_timestamp();
    v_attempt jsonb := jsonb_build_object('at', v_now, 'operation', p_operation, 'args', coalesce(p_args, '{}'::jsonb));
BEGIN
    SELECT * INTO g FROM ticket_board.break_glass_grants WHERE token_sha256 = p_token_sha256 FOR UPDATE;
    IF g.id IS NULL THEN
        RETURN jsonb_build_object('refused', 'no break-glass grant has this token');
    END IF;
    IF g.state <> 'open' THEN
        RETURN jsonb_build_object('refused', format('break-glass grant %s is %s: it allows one operation', g.id,
                                                     g.state), 'grant', g.id);
    END IF;
    IF v_now >= g.expires_at THEN
        UPDATE ticket_board.break_glass_grants SET state = 'expired', ended_at = v_now, ended_by = 'expiry',
                                                   attempted = v_attempt
         WHERE id = g.id;
        RETURN jsonb_build_object('refused', format('break-glass grant %s expired at %s', g.id, g.expires_at),
                                  'grant', g.id);
    END IF;
    IF p_operation = 'abort' THEN
        UPDATE ticket_board.break_glass_grants SET state = 'aborted', ended_at = v_now, ended_by = 'role',
                                                   attempted = v_attempt
         WHERE id = g.id;
        RETURN jsonb_build_object('aborted', true, 'grant', g.id);
    END IF;
    IF NOT (p_operation = ANY (g.operations)) THEN
        UPDATE ticket_board.break_glass_grants
           SET state = 'refused', ended_at = v_now, ended_by = 'board', attempted = v_attempt,
               completed = jsonb_build_object('refused', format('%s is not an operation this grant allows (%s)',
                                                               p_operation, array_to_string(g.operations, ', ')))
         WHERE id = g.id;
        RETURN jsonb_build_object('refused', format('break-glass grant %s does not allow %s; it allows %s', g.id,
                                                     p_operation, array_to_string(g.operations, ', ')), 'grant', g.id);
    END IF;
    UPDATE ticket_board.break_glass_grants SET state = 'operating', attempted = v_attempt WHERE id = g.id;
    RETURN jsonb_build_object('grant', g.id, 'operation', p_operation, 'args', coalesce(p_args, '{}'::jsonb),
                              'hold', g.hold_id, 'operator', g.operator, 'runtime', g.runtime);
END;
$$;

-- The board service's finish of the one operation it claimed: completed with the result, or refused with the board's
-- reason.
CREATE OR REPLACE FUNCTION ticket_board.finish_break_glass(p_grant bigint, p_completed boolean, p_result jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    g ticket_board.break_glass_grants;
BEGIN
    UPDATE ticket_board.break_glass_grants
       SET state = CASE WHEN p_completed THEN 'completed' ELSE 'refused' END, completed = coalesce(p_result, '{}'::jsonb),
           ended_at = clock_timestamp(), ended_by = 'board'
     WHERE id = p_grant AND state = 'operating' RETURNING * INTO g;
    IF g.id IS NULL THEN RAISE EXCEPTION 'break-glass grant % is not operating', p_grant; END IF;
    RETURN jsonb_build_object('grant', g.id, 'state', g.state);
END;
$$;

-- Root's end of a grant when its role stops: one still open is expired (its time ran out) or aborted (the operator
-- stopped it); one left operating, by a board that never finished it, is ended with its outcome unknown. A grant
-- already terminal is left as it is. Returns the grant as it ends.
CREATE OR REPLACE FUNCTION ticket_board.end_break_glass(p_grant bigint, p_outcome text, p_by text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    g ticket_board.break_glass_grants;
BEGIN
    IF p_outcome NOT IN ('expired', 'aborted') THEN
        RAISE EXCEPTION 'a grant is ended expired or aborted, not %', p_outcome;
    END IF;
    SELECT * INTO g FROM ticket_board.break_glass_grants WHERE id = p_grant FOR UPDATE;
    IF g.id IS NULL THEN RAISE EXCEPTION 'no break-glass grant %', p_grant; END IF;
    IF g.state IN ('open', 'operating') THEN
        UPDATE ticket_board.break_glass_grants
           SET state = CASE WHEN g.state = 'operating' THEN 'ended' ELSE p_outcome END, ended_at = clock_timestamp(),
               ended_by = p_by
         WHERE id = g.id RETURNING * INTO g;
    END IF;
    RETURN jsonb_build_object('grant', g.id, 'state', g.state, 'attempted', g.attempted, 'completed', g.completed);
END;
$$;

-- The fixed hand-back notice: identifiers, the operation and its ticket, nothing else.
CREATE OR REPLACE FUNCTION ticket_board.break_glass_notice_text(g ticket_board.break_glass_grants)
RETURNS text LANGUAGE sql STABLE SET search_path = ticket_board, pg_temp AS $$
    SELECT format('Board: break-glass grant %s, opened by the operator for safety hold %s while your account was held, '
                  'ended %s on %s. Its one request: %s%s. The hold is cleared and coordination is yours again; read '
                  'ticket-board-read holds and the tickets it names before acting.',
                  g.id, g.hold_id, g.state, g.runtime, coalesce(g.attempted->>'operation', 'none'),
                  CASE WHEN g.attempted->'args'->>'ticket' IS NOT NULL
                       THEN ' on ' || (g.attempted->'args'->>'ticket') ELSE '' END);
$$;

-- Root's hand-back once the grant has ended. Back to the Director only when its account is not held now: one fixed
-- notice. Otherwise nothing is sent, and the record says the operator was routed to a full Director replacement.
CREATE OR REPLACE FUNCTION ticket_board.hand_back_break_glass(p_grant bigint)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    g ticket_board.break_glass_grants;
    v_held boolean;
    v_ticket text;
    v_notice bigint;
    v_handback jsonb;
BEGIN
    SELECT * INTO g FROM ticket_board.break_glass_grants WHERE id = p_grant FOR UPDATE;
    IF g.id IS NULL OR g.state IN ('open', 'operating') THEN
        RAISE EXCEPTION 'break-glass grant % has not ended', p_grant;
    END IF;
    IF g.handback IS NOT NULL THEN RETURN g.handback; END IF;
    v_held := ticket_board.role_account_hold('director') IS NOT NULL;
    IF v_held THEN
        v_handback := jsonb_build_object('handed_back', false, 'route', 'rebind-director',
                                         'director_hold', ticket_board.role_account_hold('director'));
    ELSE
        v_ticket := coalesce(g.attempted->'args'->>'ticket', ticket_board.provider_hold_anchor('director'),
                             (SELECT t.id FROM ticket_board.tickets t WHERE NOT ticket_board.ticket_turn_terminal(t.id)
                               ORDER BY t.updated_at DESC NULLS LAST, t.id LIMIT 1));
        IF v_ticket IS NOT NULL AND EXISTS (SELECT FROM ticket_board.tickets WHERE id = v_ticket) THEN
            v_notice := ticket_board.enqueue_notification(
                v_ticket, 'escalation', 'director', ticket_board.break_glass_notice_text(g),
                jsonb_build_object('kind', 'break_glass_handback', 'reason', 'break_glass_handback', 'id', v_ticket,
                                   'grant', g.id, 'target_role', 'director'),
                format('break-glass-handback:%s', g.id));
        END IF;
        v_handback := jsonb_build_object('handed_back', true, 'ticket', v_ticket, 'notice', v_notice);
    END IF;
    UPDATE ticket_board.break_glass_grants SET handback = v_handback || jsonb_build_object('at', clock_timestamp())
     WHERE id = g.id RETURNING handback INTO v_handback;
    RETURN v_handback;
END;
$$;

-- The listener's question before it delivers a hand-back: still current while the Director's account is not held.
CREATE OR REPLACE FUNCTION ticket_board.break_glass_notice_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.break_glass_grants g
                    WHERE g.id = CASE WHEN coalesce(p_payload->>'grant', '') ~ '^[0-9]{1,18}$'
                                      THEN (p_payload->>'grant')::bigint END
                      AND (g.handback->>'handed_back')::boolean)
       AND ticket_board.role_account_hold('director') IS NULL;
$$;

-- What a read may show: every grant's record, never its token hash.
CREATE OR REPLACE FUNCTION ticket_board.break_glass_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'grant', g.id, 'hold', g.hold_id, 'operator', g.operator, 'runtime', g.runtime,
               'operations', to_jsonb(g.operations), 'state', g.state, 'opened_at', g.opened_at,
               'expires_at', g.expires_at, 'ended_at', g.ended_at, 'ended_by', g.ended_by, 'attempted', g.attempted,
               'completed', g.completed, 'handback', g.handback, 'packet', g.packet) ORDER BY g.id DESC), '[]'::jsonb)
      FROM (SELECT * FROM ticket_board.break_glass_grants ORDER BY id DESC LIMIT 10) g;
$$;

REVOKE ALL ON ticket_board.break_glass_grants FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.break_glass_grants_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.open_break_glass(bigint, text, text, text, text[], integer, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.claim_break_glass(text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.finish_break_glass(bigint, boolean, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.end_break_glass(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.break_glass_notice_text(ticket_board.break_glass_grants) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.hand_back_break_glass(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.break_glass_notice_is_current(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.break_glass_status() FROM PUBLIC;
-- Every column but the token's hash, which only the claim compares.
GRANT SELECT (id, hold_id, operator, reason, runtime, operations, packet, opened_at, expires_at, state, attempted,
              completed, ended_at, ended_by, handback) ON ticket_board.break_glass_grants
    TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.claim_break_glass(text, text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.finish_break_glass(bigint, boolean, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.break_glass_notice_is_current(jsonb) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.break_glass_status() TO ticket_board_service, ticket_board_listener;

COMMIT;
