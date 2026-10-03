-- SYRD-539: an opt-in pull policy. Implementers claim Director-admitted work
-- themselves (or the listener claims it for an eligible idle implementer),
-- and an author is released by its ticket's declared approval out of
-- `scheduling.release_after` (Audit pass), not before.
--
-- Nothing changes for a workflow without `scheduling`: every function below
-- returns to its previous behaviour when declared_scheduling() is NULL.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.declared_scheduling()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT nullif(ticket_board.declared_workflow()->'scheduling', 'null'::jsonb);
$$;

-- THE claim, wherever the policy needs it: the one move from the ready stage
-- into implementation whose actors are all implementers -- the same rule
-- workflow_config uses. Any other move between those stages (a Director's own
-- start, say) is an ordinary transition with its own semantics (SYRD-539).
CREATE OR REPLACE FUNCTION ticket_board.scheduling_claim_transitions(cfg jsonb)
RETURNS SETOF jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT x FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from'=cfg->'scheduling'->>'ready_stage'
       AND x->>'to'=(SELECT s->>'name' FROM jsonb_array_elements(cfg->'stages') s WHERE s->>'kind'='implementation' LIMIT 1)
       AND x->>'primitive'='move' AND jsonb_array_length(x->'actors')>0
       AND NOT EXISTS (SELECT FROM jsonb_array_elements_text(x->'actors') a
                       WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
                                         WHERE r->>'name'=a AND r->>'kind'='implementer'));
$$;

-- The same rules as workflow_config._validate_scheduling, on the stored
-- document itself, so no write path can store an incoherent policy.
CREATE OR REPLACE FUNCTION ticket_board.validate_declared_scheduling(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE policy jsonb:=nullif(cfg->'scheduling','null'::jsonb); ready text; implementation text; claims int; seconds jsonb;
BEGIN
    IF policy IS NULL THEN RETURN; END IF;
    IF jsonb_typeof(policy)<>'object' OR NOT (policy ?& ARRAY['mode','ready_stage','release_after'])
       OR EXISTS (SELECT FROM jsonb_object_keys(policy) k WHERE k NOT IN ('mode','ready_stage','release_after','idle_alert_seconds')) THEN
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
    -- Pinned rework waits where admitted work waits: the serial diversion
    -- destination is the ready stage, so a kickback to a busy author is
    -- claimable by that author alone, through the same claim transition.
    IF cfg->'queue'->>'stage' IS DISTINCT FROM ready THEN
        RAISE EXCEPTION 'scheduling needs the queue to be the ready stage'; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.check_declared_scheduling()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM ticket_board.validate_declared_scheduling(NEW.document);
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS workflow_configuration_scheduling ON ticket_board.workflow_configuration;
CREATE TRIGGER workflow_configuration_scheduling BEFORE INSERT OR UPDATE ON ticket_board.workflow_configuration
    FOR EACH ROW EXECUTE FUNCTION ticket_board.check_declared_scheduling();

-- The ready stage has a parking stage's shape (ownerless, silent) but its work
-- is admitted, not deferred: entering it must not park the ticket.
CREATE OR REPLACE FUNCTION ticket_board.declared_parking_stage(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce((SELECT NOT (x->>'terminal')::boolean
   AND jsonb_array_length(coalesce(x->'owners','[]'::jsonb))=0
   AND x->'notify'->>'kind'='none'
   AND stage IS DISTINCT FROM ticket_board.declared_scheduling()->>'ready_stage'
  FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
  WHERE x->>'name'=stage), false);
$$;

-- An author's release, as a fact: written only by the declared approval out of
-- `release_after`, removed when the ticket re-enters implementation.
CREATE TABLE IF NOT EXISTS ticket_board.pull_releases (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    released_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text NOT NULL
);

CREATE OR REPLACE FUNCTION ticket_board.record_pull_release()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb:=ticket_board.declared_scheduling(); action_name text:=nullif(current_setting('ticket_board.workflow_action',true),'');
BEGIN
    IF policy IS NULL OR OLD.state IS NOT DISTINCT FROM NEW.state THEN RETURN NULL; END IF;
    IF ticket_board.declared_stage_kind(NEW.state)='implementation' THEN
        DELETE FROM ticket_board.pull_releases WHERE ticket_id=NEW.id;
    ELSIF OLD.state=policy->>'release_after'
          AND current_setting('ticket_board.force_move',true) IS DISTINCT FROM 'on'
          AND action_name IS NOT NULL
          AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
                      -- Not matched on `to`: declared gates can carry the
                      -- approved ticket past skipped stages in the same update.
                      WHERE x->>'action'=action_name AND x->>'from'=OLD.state
                        AND x->>'primitive'='approve') THEN
        INSERT INTO ticket_board.pull_releases(ticket_id, action) VALUES (NEW.id, action_name)
            ON CONFLICT (ticket_id) DO UPDATE SET released_at=clock_timestamp(), action=EXCLUDED.action;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_pull_release ON ticket_board.tickets;
CREATE TRIGGER tickets_z_pull_release AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.record_pull_release();

CREATE OR REPLACE FUNCTION ticket_board.ticket_current_reserved_ticket(p_implementer text,p_excluding_ticket_id text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE AS $$
 -- Which ticket holds an implementer's one serial slot. Under the default
 -- 'review' policy: its own implementation ticket, or one of its tickets in a
 -- review stage. Under a tenant's 'lifecycle' policy (SYRD-276) a ticket also
 -- keeps holding its implementer wherever else it goes before it is finished --
 -- analysis after a User reopen, most importantly -- so a kickback can never
 -- leave that implementer free to take a second ticket and then hand the first
 -- one back to them. Terminal stages, parked tickets, draft stages and manual
 -- control never hold.
 --
 -- Under a pull policy (SYRD-539) a review stage holds only until the ticket's
 -- declared approval out of `release_after` is recorded; rework re-entering
 -- implementation clears that record, so it holds its author again.
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_current_reserved_ticket(p_implementer,p_excluding_ticket_id)
 ELSE (SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
 WHERE (p_excluding_ticket_id IS NULL OR t.id<>p_excluding_ticket_id) AND NOT t.manually_controlled
 AND (
   (ticket_board.declared_stage_kind(t.state) IN ('implementation','review')
    AND (CASE WHEN ticket_board.declared_stage_kind(t.state)='implementation' THEN t.assignee ELSE ns.last_implementer_assignee END)=p_implementer
    AND NOT (ticket_board.declared_stage_kind(t.state)='review' AND ticket_board.declared_scheduling() IS NOT NULL
             AND EXISTS (SELECT FROM ticket_board.pull_releases r WHERE r.ticket_id=t.id)))
   OR (coalesce(ticket_board.declared_workflow()->>'reservation','review')='lifecycle'
    AND ticket_board.declared_stage_kind(t.state)='system'
    AND NOT coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                      WHERE x->>'name'=t.state), false)
    AND NOT t.parked
    AND ns.last_implementer_assignee=p_implementer)
 )
 ORDER BY t.ticket_number LIMIT 1) END;
$$;

-- One assignment at a time per implementer. Every write that can put work into
-- the implementation stage -- a claim, a route, a kickback, a reassignment --
-- takes the same lock BEFORE the serial-reservation check runs, so two of them
-- cannot both see an implementer free (SYRD-539). Named to fire before
-- tickets_enforce_workflow_update.
CREATE OR REPLACE FUNCTION ticket_board.implementer_assignment_lock_key(p_role text)
RETURNS bigint LANGUAGE sql IMMUTABLE AS $$ SELECT hashtextextended('syrd539-implementer:' || p_role, 0); $$;

CREATE OR REPLACE FUNCTION ticket_board.lock_pull_assignment()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE role_name text; actor text:=nullif(current_setting('ticket_board.workflow_actor',true),'');
BEGIN
    IF ticket_board.declared_scheduling() IS NULL OR ticket_board.declared_stage_kind(NEW.state) IS DISTINCT FROM 'implementation' THEN
        RETURN NEW; END IF;
    -- A claim is the claimant's: the declared claim transition, taken through
    -- the canonical action with no assignee, would otherwise land on the
    -- implementation stage's default owner, whoever claimed it.
    IF TG_OP='UPDATE' AND actor IS NOT NULL AND OLD.state=ticket_board.declared_scheduling()->>'ready_stage'
       AND EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(ticket_board.declared_workflow()) x
                   WHERE x->>'action'=current_setting('ticket_board.workflow_action',true) AND x->'actors' ? actor) THEN
        NEW.assignee := actor;
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
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tickets_a_pull_assignment_lock ON ticket_board.tickets;
CREATE TRIGGER tickets_a_pull_assignment_lock BEFORE INSERT OR UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.lock_pull_assignment();

-- Admitted work belongs to nobody until it is claimed: whoever moved it in
-- (the Director admitting, or a diversion) does not own it, so no idle or
-- unresolved reminder targets anyone. Pinned rework keeps its author in
-- queued_for_assignee. Named to run after tickets_enforce_workflow_update.
CREATE OR REPLACE FUNCTION ticket_board.unassign_ready_work()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IS NOT DISTINCT FROM ticket_board.declared_scheduling()->>'ready_stage'
       AND NEW.assignee IS DISTINCT FROM 'unassigned' THEN
        NEW.assignee := 'unassigned';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tickets_zzzzz_pull_ready_unassigned ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzz_pull_ready_unassigned BEFORE INSERT OR UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.unassign_ready_work();

-- The one Director notice this policy adds (SYRD-539): claimable work has
-- waited past idle_alert_seconds while implementers that are not holding work
-- sit idle. Once per (ticket, admission), whoever the idle workers are; each
-- named with the reason the listener saw, so unknown readiness reads as
-- unknown. A claim, a withdrawal or a re-admission ends that episode.
CREATE OR REPLACE FUNCTION ticket_board.notify_pull_idle_capacity(p_idle jsonb, p_now timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb:=ticket_board.declared_scheduling(); waiting record; free jsonb;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_pull_idle_capacity');
    IF policy IS NULL THEN RETURN 0; END IF;
    SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb) INTO free
      FROM jsonb_each_text(coalesce(p_idle,'{}'::jsonb)) AS e(k, v)
     WHERE ticket_board.ticket_current_reserved_ticket(k) IS NULL;
    IF free='{}'::jsonb THEN RETURN 0; END IF;
    SELECT t.id, t.state, t.assignee, ns.entered_current_state_at AS since INTO waiting
      FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
     WHERE t.state=policy->>'ready_stage'
       AND ns.entered_current_state_at <= p_now - make_interval(secs => coalesce((policy->>'idle_alert_seconds')::int, 900))
       AND EXISTS (SELECT FROM jsonb_object_keys(free) k WHERE ticket_board.ticket_ready_for_pull(t.id, k))
     ORDER BY ns.entered_current_state_at, t.ticket_number LIMIT 1;
    IF waiting.id IS NULL THEN RETURN 0; END IF;
    -- Once per episode, delivered or not: the queue's own dedupe forgets a
    -- row once it is acknowledged; the trace of its enqueue does not.
    IF EXISTS (SELECT FROM ticket_board.notification_trace tr
               WHERE tr.ticket_id=waiting.id AND tr.event='enqueue'
                 AND tr.detail->>'dedupe_key'='pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint) THEN
        RETURN 0; END IF;
    PERFORM ticket_board.enqueue_notification(
        waiting.id, 'ticket_update', 'director',
        format('%s has been ready to claim since %s, and these implementers are idle without claiming it: %s.',
               waiting.id, to_char(waiting.since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'),
               (SELECT string_agg(k || ' (' || v || ')', ', ' ORDER BY k) FROM jsonb_each_text(free) AS e(k, v))),
        jsonb_build_object('kind', 'pull_idle_capacity', 'id', waiting.id,
                           'state', waiting.state, 'assignee', waiting.assignee, 'target_role', 'director',
                           'idle', free, 'ready_since', waiting.since),
        'pull-idle:' || waiting.id || ':' || extract(epoch FROM waiting.since)::bigint);
    RETURN 1;
END;
$$;

-- Who claimed what, and how: the worker itself, or the listener for it.
CREATE TABLE IF NOT EXISTS ticket_board.pull_claims (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL,
    via text NOT NULL CHECK (via IN ('self','listener')),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- What the pull queue is, read from the same predicates the claim uses:
-- admitted work with who may take it (anyone, or the one author it waits
-- for), and work currently held because it was pulled.
CREATE OR REPLACE FUNCTION ticket_board.pull_queue_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_scheduling() IS NULL THEN jsonb_build_object('enabled', false)
    ELSE jsonb_build_object(
        'enabled', true,
        'ready_work', coalesce((SELECT jsonb_agg(jsonb_build_object(
                'id', t.id, 'title', t.title,
                'priority', coalesce((t.workflow_flags->>'priority')::int, 0),
                'ready_since', ns.entered_current_state_at,
                'waiting_for', nullif(btrim(t.queued_for_assignee), ''),
                'behind', nullif(btrim(t.queued_behind_ticket), ''),
                'claimable', NOT t.parked AND NOT t.manually_controlled
                             AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
                             AND coalesce(btrim(ns.awaiting_role), '') = '')
              ORDER BY (nullif(btrim(t.queued_for_assignee), '') IS NULL), coalesce((t.workflow_flags->>'priority')::int, 0) DESC,
                       ns.entered_current_state_at, t.ticket_number)
            FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
            WHERE t.state = ticket_board.declared_scheduling()->>'ready_stage'), '[]'::jsonb),
        'claimed', coalesce((SELECT jsonb_agg(jsonb_build_object('id', t.id, 'role', c.role, 'via', c.via,
                                                              'claimed_at', c.claimed_at) ORDER BY c.claimed_at)
            FROM ticket_board.tickets t
            JOIN LATERAL (SELECT * FROM ticket_board.pull_claims pc WHERE pc.ticket_id = t.id ORDER BY pc.id DESC LIMIT 1) c ON true
            WHERE ticket_board.declared_stage_kind(t.state) = 'implementation' AND t.assignee = c.role), '[]'::jsonb))
    END;
$$;


-- Ready for a claim by p_role: admitted (in the ready stage), not parked,
-- held, blocked or awaiting anyone, and either unpinned or pinned to p_role.
CREATE OR REPLACE FUNCTION ticket_board.ticket_ready_for_pull(p_ticket text, p_role text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
        WHERE t.id=p_ticket
          AND t.state=ticket_board.declared_scheduling()->>'ready_stage'
          AND NOT t.parked AND NOT t.manually_controlled
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
          AND coalesce(btrim(ns.awaiting_role),'')=''
          AND (coalesce(btrim(t.queued_for_assignee),'')='' OR btrim(t.queued_for_assignee)=p_role));
$$;

-- Claim the next ready ticket for p_role, atomically. Idempotent: a role that
-- already holds work gets that ticket back and nothing changes.
CREATE OR REPLACE FUNCTION ticket_board.claim_ready_ticket(p_role text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); policy jsonb:=ticket_board.declared_scheduling(); caller text:=ticket_board.current_actor_role();
    tr jsonb; held text; candidate record; via text;
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
    SELECT x INTO tr FROM ticket_board.scheduling_claim_transitions(cfg) x WHERE x->'actors' ? p_role;
    IF tr IS NULL OR NOT EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE r.name=p_role AND (r.definition->>'active')::boolean) THEN
        RAISE EXCEPTION 'role % cannot claim ready work', p_role USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(p_role));
    held:=ticket_board.ticket_current_reserved_ticket(p_role);
    IF held IS NOT NULL THEN
        RETURN jsonb_build_object('claimed', false, 'ticket', held, 'reason', 'holding');
    END IF;
    FOR candidate IN
        SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
        WHERE ticket_board.ticket_ready_for_pull(t.id, p_role)
        ORDER BY (coalesce(btrim(t.queued_for_assignee),'')=p_role) DESC,
                 coalesce((t.workflow_flags->>'priority')::int, 0) DESC,
                 ns.entered_current_state_at, t.ticket_number
        FOR UPDATE OF t SKIP LOCKED
    LOOP
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

CREATE OR REPLACE FUNCTION ticket_board.notify_serial_focus_queue_wakeups(
    p_now timestamptz DEFAULT clock_timestamp()
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    candidate record;
    delivered_count integer := 0;
    now_reserved text;
    notice text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_serial_focus_queue_wakeups');

    FOR candidate IN
        SELECT
            t.id,
            t.title,
            t.state,
            t.assignee,
            t.ticket_number,
            btrim(t.queued_for_assignee) AS queued_for,
            btrim(t.queued_behind_ticket) AS queued_behind,
            btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket) AS wake_key
        FROM ticket_board.tickets t
        JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE btrim(t.queued_for_assignee) <> ''
          AND btrim(t.queued_behind_ticket) <> ''
          AND NOT t.manually_controlled
          -- Under a pull policy a freed author's pinned work is claimed for
          -- them, not routed by the Director, so this notice would only be
          -- routine dispatch traffic (SYRD-539).
          AND ticket_board.declared_scheduling() IS NULL
          -- The wait has genuinely ended. Everything else about this ticket --
          -- blockers, holds, its own stage -- is the director's to read; the
          -- one thing they cannot see from the board is that the reservation
          -- they were told to wait for is over.
          AND NOT ticket_board.ticket_serial_focus_reservation_is_current(
              t.id,
              t.queued_for_assignee,
              t.queued_behind_ticket
          )
          AND ns.serial_focus_wake_key IS DISTINCT FROM
              (btrim(t.queued_for_assignee) || ':' || btrim(t.queued_behind_ticket))
          -- Blocked work cannot be routed either, and announcing capacity for
          -- it would be this ticket's own complaint in a new costume. The
          -- claim is left unmade, so the hand-off is announced when the
          -- blocker clears rather than lost -- which is what every other
          -- generator here does with a blocked ticket.
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
        ORDER BY t.ticket_number
    LOOP
        now_reserved := ticket_board.ticket_current_reserved_ticket(
            candidate.queued_for,
            candidate.id
        );
        notice := ticket_board.serial_focus_available_message(
            candidate.id,
            candidate.title,
            candidate.queued_for,
            candidate.queued_behind,
            now_reserved
        );
        -- Claimed before it is enqueued. enqueue_notification dedupes a row
        -- that is still waiting, but a delivered row is gone, and this function
        -- runs on every listener pass: without the claim the same wake would be
        -- minted again on the next pass after the first was acknowledged.
        UPDATE ticket_board.ticket_notification_state
        SET serial_focus_wake_key = candidate.wake_key
        WHERE ticket_id = candidate.id;

        PERFORM ticket_board.enqueue_notification(
            candidate.id,
            'ticket_update',
            'director',
            notice,
            -- `queued_for` and `reserved_by` are the queue announcement's own
            -- vocabulary, and deliberately so. The listener reads those two
            -- keys off any notification that carries them and discards it when
            -- the ticket's queue columns have moved on (SYRD-108). A wake is a
            -- statement about one reservation identity, so it should be
            -- discarded on a reroute for exactly that reason -- and naming the
            -- fields anything else would not avoid the question, it would just
            -- exempt this notification from an answer it needs. The claim that
            -- makes a wake once-per-identity is cleared by the same reroute, so
            -- the new identity gets its own wake when its own wait ends.
            jsonb_build_object(
                'kind', 'ticket_update',
                'id', candidate.id,
                'state', candidate.state,
                'assignee', candidate.assignee,
                'target_role', 'director',
                'queued_for', candidate.queued_for,
                'reserved_by', candidate.queued_behind,
                'now_reserved', coalesce(now_reserved, ''),
                'message', notice
            ),
            'serial-focus-available:' || candidate.id || ':' || candidate.wake_key
        );
        delivered_count := delivered_count + 1;
    END LOOP;

    RETURN delivered_count;
END;
$$;

GRANT SELECT ON ticket_board.pull_releases, ticket_board.pull_claims TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.claim_ready_ticket(text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_ready_for_pull(text, text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.declared_scheduling() TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.pull_queue_status() TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.notify_pull_idle_capacity(jsonb, timestamptz) TO ticket_board_listener;

COMMIT;
