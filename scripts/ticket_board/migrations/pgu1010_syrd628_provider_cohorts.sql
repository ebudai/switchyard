-- SYRD-628: provider cohorts in a pull pool -- a claim ceiling per runtime,
-- quota records, and the record of every cohort change.
--
-- MEFP keeps an eight-worker pull pool split between Claude and Codex by hand,
-- to preserve both providers' headroom. A cohort is the claimant pool's members
-- of one runtime (scheduling.cohorts). The board enforces its ceiling on every
-- claim -- claim-next, the listener's pickup and the canonical action -- under a
-- cohort-wide lock, so no claim takes the cohort past it and the other cohorts
-- keep pulling. Quota records (the Director's, or a provider signal's) lower a
-- ceiling for a while; 0 is exhausted. Convergence -- starting and stopping
-- idle workers -- is the operator CLI's (switchyard worker-pool converge); the
-- board records what it did and why in cohort_events.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.cohort_quota (
    runtime text NOT NULL,
    -- 'operator' for the Director's record; any other name is a provider signal.
    source text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_.-]{0,40}$'),
    ceiling integer NOT NULL CHECK (ceiling BETWEEN 0 AND 64),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    PRIMARY KEY (runtime, source)
);

CREATE TABLE IF NOT EXISTS ticket_board.cohort_events (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    runtime text NOT NULL,
    role text NOT NULL DEFAULT '',
    change text NOT NULL CHECK (change IN ('quota_set', 'quota_cleared', 'signal', 'start', 'stop', 'deferred',
                                       'start_failed', 'stop_failed')),
    source text NOT NULL CHECK (source IN ('operator', 'auto', 'listener')),
    actor text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS cohort_events_runtime_at ON ticket_board.cohort_events (runtime, at DESC);

-- A worker convergence is about to stop: no claim may land on it meanwhile.
-- Short-lived, so an abandoned drain cannot keep a worker from work for long.
CREATE TABLE IF NOT EXISTS ticket_board.cohort_drains (
    role text PRIMARY KEY,
    runtime text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    drained_by text NOT NULL,
    drained_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);

-- The cohort a claimant belongs to: the policy's entry for its declared runtime.
-- NULL for a role outside the claimant pool, or whose runtime no cohort names.
CREATE OR REPLACE FUNCTION ticket_board.pull_cohort(cfg jsonb, p_role text)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT c FROM jsonb_array_elements(coalesce(cfg->'scheduling'->'cohorts', '[]'::jsonb)) c
    JOIN jsonb_array_elements(cfg->'roles') r ON r->>'name' = p_role AND r->>'runtime' = c->>'runtime'
    WHERE p_role IN (SELECT ticket_board.pull_claimant_pool(cfg))
    LIMIT 1;
$$;

-- How many tickets a cohort may hold at once now: its declared ceiling, lowered
-- by every quota record still in force. NULL when nothing limits it.
CREATE OR REPLACE FUNCTION ticket_board.cohort_effective_ceiling(p_runtime text, p_now timestamptz)
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN declared IS NULL AND quota IS NULL THEN NULL ELSE least(declared, quota) END
    FROM (SELECT (SELECT (c->>'ceiling')::int FROM jsonb_array_elements(
                      coalesce(ticket_board.declared_scheduling()->'cohorts', '[]'::jsonb)) c
                  WHERE c->>'runtime' = p_runtime) AS declared,
                 (SELECT min(q.ceiling) FROM ticket_board.cohort_quota q
                  WHERE q.runtime = p_runtime AND (q.expires_at IS NULL OR q.expires_at > p_now)) AS quota) limits;
$$;

-- The cohort's members holding a ticket now, by the serial reservation every
-- claim already respects.
CREATE OR REPLACE FUNCTION ticket_board.cohort_holders(p_runtime text)
RETURNS TABLE (role text, ticket text) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT member, ticket_board.ticket_current_reserved_ticket(member)
    FROM ticket_board.pull_claimant_pool(ticket_board.declared_workflow()) member
    WHERE (ticket_board.pull_cohort(ticket_board.declared_workflow(), member)->>'runtime') = p_runtime
      AND ticket_board.ticket_current_reserved_ticket(member) IS NOT NULL
    ORDER BY member;
$$;

-- Why `p_role` may not claim now because of its cohort, or NULL. Takes the
-- cohort's lock first, so two members cannot both count the cohort under its
-- ceiling; the count is a fresh read after the lock (VOLATILE), and it leaves
-- the claimant out -- a claimant holds nothing, or it would be 'holding'.
CREATE OR REPLACE FUNCTION ticket_board.cohort_claim_refusal(p_role text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cohort jsonb := ticket_board.pull_cohort(ticket_board.declared_workflow(), p_role);
    ceiling integer; held integer;
BEGIN
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

CREATE OR REPLACE FUNCTION ticket_board.require_declared_cohort(p_runtime text)
RETURNS void LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(coalesce(ticket_board.declared_scheduling()->'cohorts', '[]'::jsonb)) c
                   WHERE c->>'runtime' = p_runtime) THEN
        RAISE EXCEPTION 'no scheduling cohort for runtime %', p_runtime;
    END IF;
END;
$$;

-- The Director's quota record: a cohort may hold at most `p_ceiling` tickets
-- (0: exhausted) until cleared, or until it expires.
CREATE OR REPLACE FUNCTION ticket_board.set_cohort_quota(p_runtime text, p_ceiling integer, p_reason text,
                                                        p_expires_seconds integer DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; v_row ticket_board.cohort_quota;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    actor := ticket_board.current_app_actor();
    PERFORM ticket_board.require_declared_cohort(p_runtime);
    IF p_ceiling IS NULL OR p_ceiling NOT BETWEEN 0 AND 64 THEN
        RAISE EXCEPTION 'a quota ceiling is a number of tickets from 0 to 64'; END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'a quota record needs a reason'; END IF;
    IF p_expires_seconds IS NOT NULL AND p_expires_seconds NOT BETWEEN 60 AND 7 * 86400 THEN
        RAISE EXCEPTION 'a quota record expires after 1 minute to 7 days, or not at all'; END IF;
    INSERT INTO ticket_board.cohort_quota AS q (runtime, source, ceiling, reason, recorded_by, expires_at)
    VALUES (p_runtime, 'operator', p_ceiling, btrim(p_reason), actor,
            clock_timestamp() + make_interval(secs => p_expires_seconds))
    ON CONFLICT (runtime, source) DO UPDATE SET ceiling = excluded.ceiling, reason = excluded.reason,
        recorded_by = excluded.recorded_by, recorded_at = clock_timestamp(), expires_at = excluded.expires_at
    RETURNING * INTO v_row;
    INSERT INTO ticket_board.cohort_events (runtime, change, source, actor, reason, detail)
    VALUES (p_runtime, 'quota_set', 'operator', actor, btrim(p_reason),
            jsonb_build_object('ceiling', p_ceiling, 'expires_at', v_row.expires_at));
    RETURN to_jsonb(v_row);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_cohort_quota(p_runtime text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; v_row ticket_board.cohort_quota;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    actor := ticket_board.current_app_actor();
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'clearing a quota record needs a reason'; END IF;
    DELETE FROM ticket_board.cohort_quota q WHERE q.runtime = p_runtime AND q.source = 'operator' RETURNING * INTO v_row;
    IF v_row.runtime IS NULL THEN RAISE EXCEPTION 'no operator quota record for runtime %', p_runtime; END IF;
    INSERT INTO ticket_board.cohort_events (runtime, change, source, actor, reason, detail)
    VALUES (p_runtime, 'quota_cleared', 'operator', actor, btrim(p_reason), jsonb_build_object('ceiling', v_row.ceiling));
    RETURN to_jsonb(v_row);
END;
$$;

-- The plug for a provider's own usage telemetry. A signal is a quota record
-- under its source's name that must be renewed before it expires; NULL clears
-- it. Only a change is an event, so a signal renewed every pass is one line.
CREATE OR REPLACE FUNCTION ticket_board.note_cohort_quota_signal(p_runtime text, p_source text, p_ceiling integer,
                                                                p_reason text, p_expires_seconds integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE before ticket_board.cohort_quota;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_cohort_quota_signal');
    PERFORM ticket_board.require_declared_cohort(p_runtime);
    IF p_source IS NULL OR p_source = 'operator' OR p_source !~ '^[a-z][a-z0-9_.-]{0,40}$' THEN
        RAISE EXCEPTION 'a quota signal names its source, and it is not the operator'; END IF;
    SELECT * INTO before FROM ticket_board.cohort_quota q WHERE q.runtime = p_runtime AND q.source = p_source
       AND (q.expires_at IS NULL OR q.expires_at > clock_timestamp()) FOR UPDATE;
    IF p_ceiling IS NULL THEN
        DELETE FROM ticket_board.cohort_quota q WHERE q.runtime = p_runtime AND q.source = p_source;
        IF before.runtime IS NULL THEN RETURN false; END IF;
    ELSE
        IF p_ceiling NOT BETWEEN 0 AND 64 OR p_expires_seconds IS NULL OR p_expires_seconds NOT BETWEEN 60 AND 86400 THEN
            RAISE EXCEPTION 'a quota signal is a ceiling from 0 to 64 that expires after 1 minute to 1 day'; END IF;
        INSERT INTO ticket_board.cohort_quota AS q (runtime, source, ceiling, reason, recorded_by, expires_at)
        VALUES (p_runtime, p_source, p_ceiling, btrim(coalesce(nullif(btrim(p_reason), ''), p_source)), 'listener',
                clock_timestamp() + make_interval(secs => p_expires_seconds))
        ON CONFLICT (runtime, source) DO UPDATE SET ceiling = excluded.ceiling, reason = excluded.reason,
            recorded_at = clock_timestamp(), expires_at = excluded.expires_at;
        IF before.ceiling IS NOT DISTINCT FROM p_ceiling THEN RETURN false; END IF;
    END IF;
    INSERT INTO ticket_board.cohort_events (runtime, change, source, actor, reason, detail)
    VALUES (p_runtime, 'signal', 'listener', p_source, coalesce(nullif(btrim(p_reason), ''), p_source),
            jsonb_build_object('ceiling', p_ceiling, 'before', before.ceiling));
    RETURN true;
END;
$$;

-- What convergence did, or declined to do, recorded before the next step is
-- taken: a start or stop is written down as intended, then as done or failed.
CREATE OR REPLACE FUNCTION ticket_board.record_cohort_change(p_runtime text, p_role text, p_kind text, p_source text,
                                                            p_reason text, p_detail jsonb DEFAULT '{}'::jsonb)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; event_id bigint;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    actor := ticket_board.current_app_actor();
    PERFORM ticket_board.require_declared_cohort(p_runtime);
    IF p_kind NOT IN ('start', 'stop', 'deferred', 'start_failed', 'stop_failed') OR p_source NOT IN ('operator', 'auto') THEN
        RAISE EXCEPTION 'a cohort change is a start, stop, deferral or failure, by the operator or automatic'; END IF;
    -- pull_cohort is NULL for a role outside the claimant pool, so this refuses a non-member too.
    IF (ticket_board.pull_cohort(ticket_board.declared_workflow(), p_role)->>'runtime') IS DISTINCT FROM p_runtime THEN
        RAISE EXCEPTION 'role % is not a member of the % cohort', p_role, p_runtime; END IF;
    INSERT INTO ticket_board.cohort_events (runtime, role, change, source, actor, reason, detail)
    VALUES (p_runtime, p_role, p_kind, p_source, actor, btrim(p_reason), coalesce(p_detail, '{}'::jsonb))
    RETURNING id INTO event_id;
    RETURN event_id;
END;
$$;

-- Mark a member as being stopped, under the same per-worker lock every claim
-- takes first: a claim already under way finishes before this, and is
-- reported as `held`; a claim after it is refused. The caller stops the worker
-- only if nothing is held or pinned to it, and ends the drain either way.
CREATE OR REPLACE FUNCTION ticket_board.begin_cohort_drain(p_role text, p_reason text, p_seconds integer DEFAULT 600)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE actor text; cohort jsonb := ticket_board.pull_cohort(ticket_board.declared_workflow(), p_role); v_row ticket_board.cohort_drains;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    actor := ticket_board.current_app_actor();
    IF cohort IS NULL THEN RAISE EXCEPTION 'role % is not a member of any scheduling cohort', p_role; END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN RAISE EXCEPTION 'a drain needs a reason'; END IF;
    IF p_seconds IS NULL OR p_seconds NOT BETWEEN 30 AND 3600 THEN
        RAISE EXCEPTION 'a drain lasts 30 seconds to 1 hour'; END IF;
    PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(p_role));
    INSERT INTO ticket_board.cohort_drains AS d (role, runtime, reason, drained_by, expires_at)
    VALUES (p_role, cohort->>'runtime', btrim(p_reason), actor, clock_timestamp() + make_interval(secs => p_seconds))
    ON CONFLICT (role) DO UPDATE SET reason = excluded.reason, drained_by = excluded.drained_by,
        drained_at = clock_timestamp(), expires_at = excluded.expires_at
    RETURNING * INTO v_row;
    RETURN jsonb_build_object('role', p_role, 'runtime', v_row.runtime, 'expires_at', v_row.expires_at,
        'held', ticket_board.ticket_current_reserved_ticket(p_role),
        'pinned', coalesce((SELECT jsonb_agg(t.id ORDER BY t.id) FROM ticket_board.tickets t
                            WHERE coalesce(btrim(t.queued_for_assignee), '') = p_role
                              AND t.state IS NOT DISTINCT FROM ticket_board.declared_scheduling()->>'ready_stage'), '[]'::jsonb));
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.end_cohort_drain(p_role text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    DELETE FROM ticket_board.cohort_drains d WHERE d.role = p_role;
    RETURN FOUND;
END;
$$;

-- The board's side of each cohort, read-only: its policy, what limits it now
-- and why, who holds tickets, and what was recently changed.
CREATE OR REPLACE FUNCTION ticket_board.cohort_status(p_now timestamptz)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'cohorts', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'runtime', c->>'runtime', 'model', c->'model', 'effort', c->'effort',
                'target', c->'target', 'ceiling', c->'ceiling',
                'effective_ceiling', ticket_board.cohort_effective_ceiling(c->>'runtime', p_now),
                'members', coalesce((SELECT jsonb_agg(m ORDER BY m) FROM ticket_board.pull_claimant_pool(ticket_board.declared_workflow()) m
                                     WHERE ticket_board.pull_cohort(ticket_board.declared_workflow(), m)->>'runtime' = c->>'runtime'), '[]'::jsonb),
                'holding', coalesce((SELECT jsonb_agg(jsonb_build_object('role', h.role, 'ticket', h.ticket))
                                     FROM ticket_board.cohort_holders(c->>'runtime') h), '[]'::jsonb),
                'quota', coalesce((SELECT jsonb_agg(jsonb_build_object('source', q.source, 'ceiling', q.ceiling,
                                       'reason', q.reason, 'recorded_by', q.recorded_by, 'recorded_at', q.recorded_at,
                                       'expires_at', q.expires_at) ORDER BY q.source)
                                   FROM ticket_board.cohort_quota q
                                   WHERE q.runtime = c->>'runtime' AND (q.expires_at IS NULL OR q.expires_at > p_now)), '[]'::jsonb),
                'draining', coalesce((SELECT jsonb_agg(d.role ORDER BY d.role) FROM ticket_board.cohort_drains d
                                      WHERE d.runtime = c->>'runtime' AND d.expires_at > p_now), '[]'::jsonb),
                'telemetry', CASE WHEN EXISTS (SELECT FROM ticket_board.cohort_quota q WHERE q.runtime = c->>'runtime'
                                                 AND q.source <> 'operator' AND q.expires_at > p_now)
                                  THEN 'signal' ELSE 'static' END)
                ORDER BY c->>'runtime')
            FROM jsonb_array_elements(coalesce(ticket_board.declared_scheduling()->'cohorts', '[]'::jsonb)) c), '[]'::jsonb),
        'events', coalesce((
            SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id DESC)
            FROM (SELECT * FROM ticket_board.cohort_events ORDER BY id DESC LIMIT 50) e), '[]'::jsonb));
$$;

REVOKE ALL ON ticket_board.cohort_quota FROM PUBLIC;
REVOKE ALL ON ticket_board.cohort_events FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.cohort_events_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.cohort_effective_ceiling(text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.cohort_holders(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.cohort_claim_refusal(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.require_declared_cohort(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.set_cohort_quota(text, integer, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.clear_cohort_quota(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_cohort_quota_signal(text, text, integer, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_cohort_change(text, text, text, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.cohort_status(timestamptz) FROM PUBLIC;
REVOKE ALL ON ticket_board.cohort_drains FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.begin_cohort_drain(text, text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.end_cohort_drain(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_scheduling(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE policy jsonb:=nullif(cfg->'scheduling','null'::jsonb); ready text; implementation text; claims int; seconds jsonb;
    cohorts jsonb; field text;
BEGIN
    IF policy IS NULL THEN RETURN; END IF;
    IF jsonb_typeof(policy)<>'object' OR NOT (policy ?& ARRAY['mode','ready_stage','release_after'])
       OR EXISTS (SELECT FROM jsonb_object_keys(policy) k WHERE k NOT IN ('mode','ready_stage','release_after','idle_alert_seconds','cohorts')) THEN
        RAISE EXCEPTION 'invalid scheduling policy'; END IF;
    IF policy->>'mode' IS DISTINCT FROM 'pull' THEN RAISE EXCEPTION 'invalid scheduling mode'; END IF;
    IF coalesce(cfg->>'reservation','review')<>'review' THEN
        RAISE EXCEPTION 'scheduling cannot be combined with reservation lifecycle'; END IF;
    IF jsonb_typeof(policy->'ready_stage') IS DISTINCT FROM 'string'
       OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=policy->>'ready_stage') THEN
        RAISE EXCEPTION 'scheduling ready stage must be a declared stage'; END IF;
    ready:=policy->>'ready_stage';
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ready
               AND ((x->>'terminal')::boolean OR x->>'kind' IN ('implementation','review'))) THEN
        RAISE EXCEPTION 'scheduling ready stage must be a non-terminal holding stage'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'to'=ready AND x->'actors'<>'["director"]'::jsonb) THEN
        RAISE EXCEPTION 'only the director may admit work to the ready stage'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x JOIN jsonb_array_elements(cfg->'stages') d ON d->>'name'=x->>'to'
                   WHERE x->>'from'=ready AND x->'actors' ? 'director' AND NOT (d->>'terminal')::boolean
                     AND d->>'kind' NOT IN ('implementation','review')
                     AND (jsonb_array_length(coalesce(d->'owners','[]'::jsonb))>0 OR d->'notify'->>'kind'<>'none')) THEN
        RAISE EXCEPTION 'the director must be able to withdraw admitted work from the ready stage'; END IF;
    SELECT x->>'name' INTO implementation FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind'='implementation';
    SELECT count(*) INTO claims FROM ticket_board.scheduling_claim_transitions(cfg);
    IF claims<>1 THEN RAISE EXCEPTION 'scheduling needs exactly one implementer move from the ready stage into implementation'; END IF;
    IF EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(cfg) x
               WHERE coalesce((x->>'require_reason')::boolean,false) OR coalesce((x->>'owner_scoped')::boolean,false)) THEN
        RAISE EXCEPTION 'the claim transition cannot require a reason or be owner-scoped: a pulled claim has neither'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=policy->>'release_after' AND x->>'kind'='review') THEN
        RAISE EXCEPTION 'scheduling release_after must be a review stage'; END IF;
    seconds:=policy->'idle_alert_seconds';
    IF seconds IS NOT NULL AND (jsonb_typeof(seconds)<>'number' OR (seconds#>>'{}')::numeric<>trunc((seconds#>>'{}')::numeric)
       OR (seconds#>>'{}')::numeric NOT BETWEEN 60 AND 86400) THEN
        RAISE EXCEPTION 'scheduling idle_alert_seconds must be an integer from 60 to 86400'; END IF;
    -- SYRD-628: provider cohorts, one per runtime: a target of running
    -- workers, a ceiling on the tickets they hold at once, model and effort pins.
    cohorts:=policy->'cohorts';
    IF cohorts IS NOT NULL THEN
        IF jsonb_typeof(cohorts)<>'array' OR jsonb_array_length(cohorts)=0 THEN
            RAISE EXCEPTION 'scheduling cohorts must be a non-empty list'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE jsonb_typeof(c)<>'object' OR NOT c ? 'runtime'
                   OR EXISTS (SELECT FROM jsonb_object_keys(c) k WHERE k NOT IN ('runtime','target','ceiling','model','effort'))) THEN
            RAISE EXCEPTION 'invalid scheduling cohort'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c
                   WHERE jsonb_typeof(c->'runtime') IS DISTINCT FROM 'string'
                      OR c->>'runtime' NOT IN ('claude','codex','agy','hermes','copilot'))
           OR (SELECT count(DISTINCT c->>'runtime') FROM jsonb_array_elements(cohorts) c)<>jsonb_array_length(cohorts) THEN
            RAISE EXCEPTION 'a scheduling cohort names a known runtime, and each runtime once'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE NOT (c ? 'target' OR c ? 'ceiling')) THEN
            RAISE EXCEPTION 'a scheduling cohort needs a target or a ceiling'; END IF;
        FOREACH field IN ARRAY ARRAY['target','ceiling'] LOOP
            IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE c ? field
                       AND (jsonb_typeof(c->field)<>'number' OR (c->>field)::numeric<>trunc((c->>field)::numeric)
                            OR (c->>field)::numeric NOT BETWEEN 0 AND 64)) THEN
                RAISE EXCEPTION 'scheduling cohort % must be an integer from 0 to 64', field; END IF;
        END LOOP;
        FOREACH field IN ARRAY ARRAY['model','effort'] LOOP
            IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE c ? field
                       AND (jsonb_typeof(c->field)<>'string' OR c->>field<>btrim(c->>field)
                            OR length(c->>field) NOT BETWEEN 1 AND 120)) THEN
                RAISE EXCEPTION 'scheduling cohort % must be a name of 1 to 120 characters', field; END IF;
        END LOOP;
    END IF;
    -- Pinned rework waits where admitted work waits: the serial diversion
    -- destination is the ready stage, so a kickback to a busy author is
    -- claimable by that author alone, through the same claim transition.
    IF cfg->'queue'->>'stage' IS DISTINCT FROM ready THEN
        RAISE EXCEPTION 'scheduling needs the queue to be the ready stage'; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.claim_ready_ticket(p_role text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); policy jsonb:=ticket_board.declared_scheduling(); caller text:=ticket_board.current_actor_role();
    tr jsonb; held text; candidate record; via text; refusal text; tried text[]:='{}';
BEGIN
    IF caller='ticket_board_service' THEN
        -- A worker claims only for itself: its role is the process-bound caller.
        IF p_role IS DISTINCT FROM ticket_board.current_app_actor() THEN
            RAISE EXCEPTION 'a worker can claim only for itself' USING ERRCODE='42501'; END IF;
        via:='self';
    ELSIF caller='ticket_board_listener' THEN
        via:='listener';
    ELSE
        RAISE EXCEPTION 'claim requires the board service or listener' USING ERRCODE='42501';
    END IF;
    IF policy IS NULL THEN RAISE EXCEPTION 'this workflow does not use pull scheduling'; END IF;
    SELECT x INTO tr FROM ticket_board.scheduling_claim_transitions(cfg) x WHERE ticket_board.transition_allows_actor(cfg, x, p_role);
    IF tr IS NULL OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=p_role AND (r.definition->>'active')::boolean) THEN
        RAISE EXCEPTION 'role % cannot claim ready work', p_role USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(p_role));
    held:=ticket_board.ticket_current_reserved_ticket(p_role);
    IF held IS NOT NULL THEN
        RETURN jsonb_build_object('claimed', false, 'ticket', held, 'reason', 'holding');
    END IF;
    -- SYRD-628: the claimant's provider cohort is at its ceiling. Not a
    -- failure, and not idle capacity: the other cohorts keep pulling.
    refusal:=ticket_board.cohort_claim_refusal(p_role);
    IF refusal IS NOT NULL THEN
        RETURN jsonb_build_object('claimed', false, 'ticket', NULL, 'reason', 'ceiling', 'detail', refusal);
    END IF;
    -- One candidate at a time (SYRD-628). A FOR loop over this query fetches
    -- rows in batches, so FOR UPDATE locked every ready ticket at once and a
    -- worker claiming at the same moment skipped them all: "nothing ready"
    -- with work waiting.
    LOOP
        SELECT t.id INTO candidate FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
        WHERE ticket_board.ticket_ready_for_pull(t.id, p_role) AND t.id <> ALL (tried)
        ORDER BY (coalesce(btrim(t.queued_for_assignee),'')=p_role) DESC,
                 coalesce((t.workflow_flags->>'priority')::int, 0) DESC,
                 ns.entered_current_state_at, t.ticket_number
        LIMIT 1
        FOR UPDATE OF t SKIP LOCKED;
        EXIT WHEN candidate.id IS NULL;
        tried := tried || candidate.id;
        BEGIN
            PERFORM set_config('ticket_board.workflow_action', tr->>'action', true);
            PERFORM set_config('ticket_board.workflow_actor', p_role, true);
            UPDATE ticket_board.tickets SET state=tr->>'to', assignee=p_role, queued_for_assignee='', queued_behind_ticket=''
             WHERE id=candidate.id;
            PERFORM set_config('ticket_board.workflow_action', '', true);
            PERFORM set_config('ticket_board.workflow_actor', '', true);
            IF (SELECT state FROM ticket_board.tickets WHERE id=candidate.id) IS DISTINCT FROM tr->>'to' THEN
                RAISE EXCEPTION 'claim diverted';
            END IF;
            INSERT INTO ticket_board.pull_claims(ticket_id, role, via) VALUES (candidate.id, p_role, via);
            RETURN jsonb_build_object('claimed', true, 'ticket', candidate.id, 'via', via);
        EXCEPTION WHEN OTHERS THEN
            -- A gate or rule refused this one; it stays where it was, and the next is tried.
            PERFORM set_config('ticket_board.workflow_action', '', true);
            PERFORM set_config('ticket_board.workflow_actor', '', true);
        END;
    END LOOP;
    RETURN jsonb_build_object('claimed', false, 'ticket', NULL, 'reason', 'nothing ready');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.lock_pull_assignment()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE role_name text; actor text:=nullif(current_setting('ticket_board.workflow_actor',true),''); claim boolean:=false; refusal text;
BEGIN
    IF ticket_board.declared_scheduling() IS NULL OR ticket_board.declared_stage_kind(NEW.state) IS DISTINCT FROM 'implementation' THEN
        RETURN NEW; END IF;
    -- A claim is the claimant's: the declared claim transition, taken through
    -- the canonical action with no assignee, would otherwise land on the
    -- implementation stage's default owner, whoever claimed it.
    IF TG_OP='UPDATE' AND actor IS NOT NULL AND OLD.state=ticket_board.declared_scheduling()->>'ready_stage'
       AND EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(ticket_board.declared_workflow()) x
                   WHERE x->>'action'=current_setting('ticket_board.workflow_action',true)
                     AND ticket_board.transition_allows_actor(ticket_board.declared_workflow(), x, actor)) THEN
        NEW.assignee := actor;
        claim := true;
    END IF;
    IF TG_OP='UPDATE' AND OLD.state IS NOT DISTINCT FROM NEW.state AND OLD.assignee IS NOT DISTINCT FROM NEW.assignee THEN
        RETURN NEW; END IF;
    -- The proposed assignee, and the author a return would be sent to.
    FOR role_name IN
        SELECT DISTINCT r FROM unnest(ARRAY[NEW.assignee,
            (SELECT ns.last_implementer_assignee FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id=NEW.id)]) r
        WHERE r IS NOT NULL AND r<>'' ORDER BY r
    LOOP
        PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(role_name));
    END LOOP;
    -- SYRD-628: the canonical claim action obeys the cohort ceiling claim-next
    -- does; the implementer's lock is taken first, in the same order as there.
    IF claim THEN
        refusal := ticket_board.cohort_claim_refusal(actor);
        IF refusal IS NOT NULL THEN
            RAISE EXCEPTION 'role % cannot claim now: %', actor, refusal USING ERRCODE='42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

GRANT SELECT ON ticket_board.cohort_quota, ticket_board.cohort_events TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.cohort_status(timestamptz) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.set_cohort_quota(text, integer, text, integer) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.clear_cohort_quota(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.record_cohort_change(text, text, text, text, text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.begin_cohort_drain(text, text, integer) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.end_cohort_drain(text) TO ticket_board_service;
GRANT SELECT ON ticket_board.cohort_drains TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_cohort_quota_signal(text, text, integer, text, integer) TO ticket_board_listener;

COMMIT;
