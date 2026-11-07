BEGIN;

-- SYRD-672 (pgu1030): a provider's safety refusal holds every role on that provider account, until a person clears it.
--
-- Repeated cybersecurity or safety refusals can get a provider account suspended. So the first one is a circuit
-- breaker: nothing automatic reaches any role sharing that account again -- no delivery, retry, reminder, nudge,
-- demand start, claim or route -- until the Director or the operator clears the hold with a reason. Nothing here asks
-- the provider whether the refusal has cleared, and no timeout, restart, quota reset or success elsewhere ends it.
--
-- Scope: one board is one project, whose roles all run as its owner account, with the owner's provider sign-in
-- (SYRD-69, role_credentials). So the account is this board's owner on one runtime: a hold on 'claude' holds every
-- Claude role here. A role's runtime is its live registration's, else its workflow declaration's, so a stopped role
-- is held as well and nothing starts it.
--
-- Evidence (provider_refusal.py): only a positively identified refusal -- Claude Code's transcript `stop_reason:
-- "refusal"`, or Codex's own cybersecurity lines on the screen of the process and turn that took it -- with its
-- classification and identity, never its text. Each identity is taken once, ever -- whether it begins a hold or is
-- seen while one stands (provider_refusal_evidence) -- so an old record or an old screen cannot hold it again after a
-- clear. Anything less certain is held by hand (hold_provider_account).
--
-- Notice: once per hold, recorded here, so restarts never repeat it. The Director is told (a notice on the refused
-- role's ticket) unless the Director's own runtime is the one held, and the operator always gets one user_events row
-- (provider_safety_hold) for the phone bridge, standing until the hold is cleared. The refused role is never told.
CREATE TABLE IF NOT EXISTS ticket_board.provider_account_holds (
    id bigserial PRIMARY KEY,
    runtime text NOT NULL CHECK (runtime IN ('claude', 'codex', 'agy', 'hermes', 'copilot')),
    -- 'refusal': identified from the provider's own words; 'suspected': held by hand, for anything less certain.
    kind text NOT NULL CHECK (kind = ANY (ARRAY['refusal', 'suspected'])),
    category text NOT NULL DEFAULT '' CHECK (category ~ '^[a-z0-9_-]{0,32}$'),
    source text NOT NULL DEFAULT '' CHECK (source ~ '^[A-Za-z0-9_.-]{0,64}$'),
    evidence text NOT NULL DEFAULT '' CHECK (evidence ~ '^[A-Za-z0-9_.:-]{0,96}$'),
    role text NOT NULL DEFAULT '' CHECK (role ~ '^([a-z][a-z0-9_-]{0,63})?$'),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    began_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    began_by text NOT NULL,
    reason text NOT NULL DEFAULT '',
    notified_director boolean NOT NULL DEFAULT false,
    user_event_id bigint REFERENCES ticket_board.user_events(id) ON DELETE SET NULL,
    cleared_at timestamptz,
    cleared_by text,
    clear_reason text,
    CHECK ((cleared_at IS NULL) = (cleared_by IS NULL) AND (cleared_at IS NULL) = (clear_reason IS NULL)),
    CHECK (kind = 'suspected' OR evidence <> '')
);
CREATE UNIQUE INDEX IF NOT EXISTS provider_account_holds_current ON ticket_board.provider_account_holds (runtime)
    WHERE cleared_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS provider_account_holds_evidence ON ticket_board.provider_account_holds (evidence)
    WHERE evidence <> '';

-- Every refusal identity the board has taken, ever: the one that began a hold, and each seen while a hold already
-- stood (Audit on 2632c854: a second refusal seen during a hold must not begin another after the clear). A taken
-- identity is never evidence again.
CREATE TABLE IF NOT EXISTS ticket_board.provider_refusal_evidence (
    evidence text PRIMARY KEY CHECK (evidence ~ '^[A-Za-z0-9_.:-]{1,96}$'),
    hold_id bigint NOT NULL REFERENCES ticket_board.provider_account_holds(id),
    role text NOT NULL,
    category text NOT NULL DEFAULT '',
    source text NOT NULL DEFAULT '',
    seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The account a role's provider runs on here: its registration's runtime, else its declared one; NULL when neither.
CREATE OR REPLACE FUNCTION ticket_board.role_account_runtime(p_role text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((SELECT a.runtime FROM ticket_board.role_runtime_assignments a WHERE a.role = p_role),
                    (SELECT w.definition->>'runtime' FROM ticket_board.workflow_roles w WHERE w.name = p_role));
$$;

-- The current hold on the account this role's provider runs on; NULL when none.
CREATE OR REPLACE FUNCTION ticket_board.role_account_hold(p_role text)
RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT h.id FROM ticket_board.provider_account_holds h
     WHERE h.cleared_at IS NULL AND h.runtime = ticket_board.role_account_runtime(p_role);
$$;

DO $kinds$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'ticket_board.user_events'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%kind%' LOOP
        EXECUTE format('ALTER TABLE ticket_board.user_events DROP CONSTRAINT %I', c.conname);
    END LOOP;
END
$kinds$;
ALTER TABLE ticket_board.user_events
    ADD CONSTRAINT user_events_kind_known CHECK (kind = ANY (ARRAY['user_review', 'operator_blocker',
        'operation_failed', 'director_ad_hoc', 'director_capacity', 'provider_safety_hold'])),
    ADD CONSTRAINT user_events_notice_is_ad_hoc CHECK ((kind = 'director_ad_hoc') = (notice_id IS NOT NULL)),
    ADD CONSTRAINT user_events_ticket_unless_board_wide CHECK (
        kind = ANY (ARRAY['director_ad_hoc', 'director_capacity', 'provider_safety_hold']) OR ticket_id IS NOT NULL);

-- Begin a hold and say so, once. The caller has found no current hold on p_runtime.
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
                   'ticket-board-write clear-provider-account-hold %s --reason "..."',
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

-- The listener's report of a refusal it identified: {runtime, category, source, evidence, pid, start_time}.
-- Taken only from the role's registered process; an identity already seen, held or cleared, is stale.
CREATE OR REPLACE FUNCTION ticket_board.note_provider_refusal(p_role text, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    reg ticket_board.role_runtime_assignments;
    v_evidence text := coalesce(p_evidence->>'evidence', '');
    cur bigint;
    result jsonb;
BEGIN
    SELECT * INTO reg FROM ticket_board.role_runtime_assignments a WHERE a.role = p_role FOR SHARE;
    IF reg.role IS NULL OR reg.runtime IS DISTINCT FROM p_evidence->>'runtime'
       OR reg.process_pid IS DISTINCT FROM (p_evidence->>'pid')::bigint
       OR reg.process_start_time IS DISTINCT FROM (p_evidence->>'start_time')::bigint THEN
        RETURN jsonb_build_object('stale', true, 'why', 'not the role''s registered process');
    END IF;
    IF v_evidence = '' THEN
        RAISE EXCEPTION 'a refusal needs its identity';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.provider_account_holds'));
    IF EXISTS (SELECT FROM ticket_board.provider_refusal_evidence e WHERE e.evidence = v_evidence)
       OR EXISTS (SELECT FROM ticket_board.provider_account_holds h WHERE h.evidence = v_evidence) THEN
        RETURN jsonb_build_object('stale', true, 'why', 'this refusal was already taken');
    END IF;
    SELECT h.id INTO cur FROM ticket_board.provider_account_holds h WHERE h.runtime = reg.runtime AND h.cleared_at IS NULL;
    IF cur IS NOT NULL THEN
        result := jsonb_build_object('hold', cur, 'began', false, 'runtime', reg.runtime, 'joined', true);
    ELSE
        result := ticket_board.begin_provider_account_hold(reg.runtime, 'refusal', lower(coalesce(p_evidence->>'category', '')),
                                                           coalesce(p_evidence->>'source', ''), v_evidence, p_role, 'listener', '');
        cur := (result->>'hold')::bigint;
    END IF;
    -- Taken either way: a refusal seen during a hold is consumed by it, and never holds the account again.
    INSERT INTO ticket_board.provider_refusal_evidence (evidence, hold_id, role, category, source)
    VALUES (v_evidence, cur, p_role, left(lower(coalesce(p_evidence->>'category', '')), 32),
            left(coalesce(p_evidence->>'source', ''), 64));
    RETURN result;
END;
$$;

-- Who may hold or clear an account: the Director, a role holding set_manually_controlled, or the User -- the
-- workflow's user-kind role, the human operator -- for when the Director's own account is the one held. No capability
-- marks the User alone, so it is admitted by its kind, as the server admits it. Returns the caller, which is recorded.
CREATE OR REPLACE FUNCTION ticket_board.provider_account_hold_actor(p_action text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text := ticket_board.current_app_actor();
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN
        PERFORM ticket_board.require_actor(ARRAY['director', 'user'], p_action);
    ELSIF ticket_board.current_actor_role() = 'ticket_board_service' AND EXISTS (
            SELECT FROM ticket_board.workflow_roles r
             WHERE r.name = actor AND (r.definition->>'active')::boolean AND r.definition->>'kind' = 'user') THEN
        NULL;
    ELSE
        PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    END IF;
    RETURN actor;
END;
$$;

-- A Director or the operator holds an account by hand: a suspected refusal, or any doubt. Repeating it is a no-op.
CREATE OR REPLACE FUNCTION ticket_board.hold_provider_account(p_runtime text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; cur bigint;
BEGIN
    actor := ticket_board.provider_account_hold_actor('hold_provider_account');
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'holding a provider account needs a reason'; END IF;
    IF p_runtime NOT IN ('claude', 'codex', 'agy', 'hermes', 'copilot') THEN
        RAISE EXCEPTION 'unknown runtime %', p_runtime;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.provider_account_holds'));
    SELECT h.id INTO cur FROM ticket_board.provider_account_holds h WHERE h.runtime = p_runtime AND h.cleared_at IS NULL;
    IF cur IS NOT NULL THEN
        RETURN jsonb_build_object('hold', cur, 'began', false, 'runtime', p_runtime);
    END IF;
    RETURN ticket_board.begin_provider_account_hold(p_runtime, 'suspected', '', 'by_hand', '', '', actor,
                                                    left(btrim(p_reason), 500));
END;
$$;

-- The only way a hold ends: a Director or the operator, after confirming the account is safe, with the reason kept.
CREATE OR REPLACE FUNCTION ticket_board.clear_provider_account_hold(p_runtime text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; cur ticket_board.provider_account_holds;
BEGIN
    actor := ticket_board.provider_account_hold_actor('clear_provider_account_hold');
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'clearing a provider account hold needs a reason'; END IF;
    SELECT * INTO cur FROM ticket_board.provider_account_holds h
     WHERE h.runtime = p_runtime AND h.cleared_at IS NULL FOR UPDATE;
    IF cur.id IS NULL THEN RAISE EXCEPTION 'the % account is not held', p_runtime; END IF;
    UPDATE ticket_board.provider_account_holds
       SET cleared_at = clock_timestamp(), cleared_by = actor, clear_reason = left(btrim(p_reason), 500)
     WHERE id = cur.id RETURNING * INTO cur;
    -- What waited on the hold is due now, once each, as SYRD-644's clear does; the board asks each again before it goes.
    UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = clock_timestamp()
     WHERE q.last_error LIKE 'provider_account_hold%' AND q.claimed_at IS NULL;
    RETURN to_jsonb(cur);
END;
$$;

-- Read-only: current holds, each with the roles it holds now, and the last few cleared. Never any refused text.
CREATE OR REPLACE FUNCTION ticket_board.provider_account_hold_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'held', coalesce((SELECT jsonb_agg(jsonb_build_object(
                     'hold', h.id, 'runtime', h.runtime, 'kind', h.kind, 'category', h.category, 'source', h.source,
                     'role', h.role, 'ticket', h.ticket_id, 'began_at', h.began_at, 'began_by', h.began_by,
                     'reason', h.reason, 'notified_director', h.notified_director,
                     'refusals', (SELECT count(*) FROM ticket_board.provider_refusal_evidence e WHERE e.hold_id = h.id),
                     'roles', coalesce((SELECT jsonb_agg(w.name ORDER BY w.name) FROM ticket_board.workflow_roles w
                                         WHERE ticket_board.role_account_runtime(w.name) = h.runtime), '[]'::jsonb))
                     ORDER BY h.id)
                   FROM ticket_board.provider_account_holds h WHERE h.cleared_at IS NULL), '[]'::jsonb),
        'cleared', coalesce((SELECT jsonb_agg(c ORDER BY (c->>'hold')::bigint DESC) FROM (
                     SELECT jsonb_build_object('hold', h.id, 'runtime', h.runtime, 'kind', h.kind, 'began_at', h.began_at,
                                               'cleared_at', h.cleared_at, 'cleared_by', h.cleared_by,
                                               'clear_reason', h.clear_reason) AS c
                       FROM ticket_board.provider_account_holds h WHERE h.cleared_at IS NOT NULL
                      ORDER BY h.id DESC LIMIT 5) recent), '[]'::jsonb));
$$;

-- SYRD-644's two questions, asked by reminders, escalations, stall notices and deliveries, now also say held for a
-- role whose provider account is held. An account hold has no deadline: deliveries recheck every five minutes, and
-- each recheck asks the board, never the provider.
CREATE OR REPLACE FUNCTION ticket_board.role_provider_hold_is_current(p_role text, p_now timestamptz)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT ticket_board.role_account_hold(p_role) IS NOT NULL OR EXISTS (
        SELECT FROM ticket_board.provider_quota_holds h
        JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
        WHERE h.role = p_role
          AND ((h.state = 'held' AND h.deadline > p_now)
               OR (h.state = 'released' AND h.released_at > p_now - interval '10 minutes')));
$$;

CREATE OR REPLACE FUNCTION ticket_board.role_provider_hold_until(p_role text, p_now timestamptz)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.role_account_hold(p_role) IS NOT NULL THEN p_now + interval '5 minutes' ELSE (
        SELECT h.deadline FROM ticket_board.provider_quota_holds h
        JOIN ticket_board.role_runtime_assignments a ON a.role = h.role AND a.generation = h.generation
        WHERE h.role = p_role AND h.state = 'held' AND h.deadline > p_now) END;
$$;

-- SYRD-672: a worker on a held account claims nothing, and Router routes around it.
CREATE OR REPLACE FUNCTION ticket_board.cohort_claim_refusal(p_role text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cohort jsonb := ticket_board.pull_cohort(ticket_board.declared_workflow(), p_role);
    ceiling integer; held integer;
BEGIN
    -- SYRD-672: no role on a held provider account claims anything, whether or not it has a cohort.
    IF ticket_board.role_account_hold(p_role) IS NOT NULL THEN
        RETURN format('%s''s provider account is held after a safety refusal; nothing is claimed for it until a person '
                      'clears the hold', p_role);
    END IF;
    IF cohort IS NULL THEN RETURN NULL; END IF;
    IF EXISTS (SELECT FROM ticket_board.cohort_drains d WHERE d.role = p_role AND d.expires_at > clock_timestamp()) THEN
        RETURN format('%s is being stopped by convergence', p_role);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('syrd628-cohort:' || (cohort->>'runtime'), 0));
    ceiling := ticket_board.cohort_effective_ceiling(cohort->>'runtime', clock_timestamp());
    IF ceiling IS NULL THEN RETURN NULL; END IF;
    SELECT count(*) INTO held FROM ticket_board.cohort_holders(cohort->>'runtime') h WHERE h.role <> p_role;
    IF held < ceiling THEN RETURN NULL; END IF;
    RETURN format('the %s cohort holds %s ticket(s), its ceiling is %s', cohort->>'runtime', held, ceiling);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.router_scope_refusal(p_ticket jsonb, p_role text)
RETURNS text LANGUAGE sql STABLE AS $$
    -- SYRD-672: a role on a held provider account is refused first, so Router carries on with the eligible owners.
    SELECT refusal FROM (
    SELECT CASE WHEN ticket_board.role_account_hold(p_role) IS NOT NULL THEN
                format('%s''s provider account is held after a safety refusal; choose an owner on another provider, or '
                       'leave %s queued for the Director', p_role, p_ticket->>'id') END
    UNION ALL
    SELECT format('router gives %s only work flagged %s, and %s is not; choose another owner, or return it to the '
                  'Director, who may flag it or assign %s directly', p_role, r->'routing_scope'->>'flag', p_ticket->>'id', p_role)
      FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
     WHERE r->>'name' = p_role AND r ? 'routing_scope'
       AND NOT ticket_board.workflow_flag(p_ticket, r->'routing_scope'->>'flag', ticket_board.declared_workflow())
    ) found(refusal) WHERE refusal IS NOT NULL LIMIT 1;
$$;

-- The phone bridge's feed: a safety hold's event stands until the hold is cleared.
CREATE OR REPLACE FUNCTION ticket_board.user_events_after(p_after bigint, p_limit integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'latest', (SELECT coalesce(max(id), 0) FROM ticket_board.user_events),
        'events', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', e.id,
                       'key', format('%s:%s:%s:%s', e.kind, coalesce(e.ticket_id, '-'), e.id,
                                     to_char(e.at AT TIME ZONE 'UTC', 'YYYYMMDD"T"HH24MISS.US')),
                       'kind', e.kind, 'ticket', coalesce(e.ticket_id, ''), 'detail', e.detail, 'at', e.at,
                       'title', coalesce(left(t.title, 120), ''),
                       'notice', CASE WHEN n.id IS NULL THEN NULL ELSE jsonb_build_object(
                           'title', n.title, 'message', n.message, 'urgent', n.urgent, 'reason', n.reason, 'requester_role',
                           n.requester_role, 'requester_pid', n.requester_pid, 'requester_generation',
                           n.requester_generation, 'digest', n.digest) END,
                       'standing', CASE e.kind
                           WHEN 'user_review' THEN t.state = e.detail
                                                   AND ticket_board.user_event_needs_user(t.state, t.assignee)
                           WHEN 'operator_blocker' THEN EXISTS (
                               SELECT FROM ticket_board.user_event_operator_waits w
                                WHERE w.ticket_id = e.ticket_id AND w.operator = e.detail)
                           -- SYRD-728: until a Director turn ends normally again.
                           WHEN 'director_capacity' THEN EXISTS (
                               SELECT FROM ticket_board.director_capacity_episodes d
                                WHERE d.user_event_id = e.id AND d.cleared_at IS NULL)
                           -- SYRD-672: until the provider account hold it began is cleared.
                           WHEN 'provider_safety_hold' THEN EXISTS (
                               SELECT FROM ticket_board.provider_account_holds h
                                WHERE h.user_event_id = e.id AND h.cleared_at IS NULL)
                           ELSE true END) ORDER BY e.id)
              FROM (SELECT * FROM ticket_board.user_events WHERE id > greatest(coalesce(p_after, 0), 0)
                     ORDER BY id LIMIT least(greatest(coalesce(p_limit, 20), 1), 50)) e
              LEFT JOIN ticket_board.tickets t ON t.id = e.ticket_id
              LEFT JOIN ticket_board.director_notices n ON n.id = e.notice_id), '[]'::jsonb));
$$;

REVOKE ALL ON ticket_board.provider_account_holds FROM PUBLIC;
REVOKE ALL ON ticket_board.provider_refusal_evidence FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.provider_account_holds_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.role_account_runtime(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.role_account_hold(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.provider_account_hold_actor(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.begin_provider_account_hold(text, text, text, text, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_provider_refusal(text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.hold_provider_account(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.clear_provider_account_hold(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.provider_account_hold_status() FROM PUBLIC;
GRANT SELECT ON ticket_board.provider_account_holds TO ticket_board_service, ticket_board_listener;
GRANT SELECT ON ticket_board.provider_refusal_evidence TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_account_runtime(text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_account_hold(text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_provider_refusal(text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.hold_provider_account(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.clear_provider_account_hold(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.provider_account_hold_status() TO ticket_board_service, ticket_board_listener;

COMMIT;
