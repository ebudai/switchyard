-- SYRD-738 (pgu1039): rebind a Director held by a provider safety hold to another runtime.
--
-- When a safety refusal holds the Director's own provider account (SYRD-672), nothing may ask that Director for a
-- handoff, and nothing may act as the Director. The operator's `pkexec switchyard rebind-director` replaces it: it
-- moves the Director's declared runtime with SYRD-262's rebind_declared_pane_roles, starts a fresh session on the
-- new runtime, and hands that session the SYRD-737 recovery packet. These are its board records.
--
-- Every function here is root's, run through the database owner's connection that deploys, migrations and the
-- pane rebind already use. None is granted to any role: rbac.sql revokes them from PUBLIC and grants nothing back,
-- so neither the board service, the listener nor any pane can reach them. The one exception is the notice's
-- currency check, which the listener reads.
--
-- What they never do: clear the hold (only clear_provider_account_hold does, by the Director or the User), write a
-- ticket, or act as any role. The operator is the pkexec-verified person, recorded by account name.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.director_rebinds (
    id bigserial PRIMARY KEY,
    hold_id bigint NOT NULL REFERENCES ticket_board.provider_account_holds(id),
    -- 'applying' while the command runs; then how it ended. 'unavailable': no eligible alternate runtime existed.
    state text NOT NULL CHECK (state IN ('applying', 'applied', 'rolled_back', 'needs_operator', 'unavailable')),
    operator text NOT NULL CHECK (operator ~ '^[a-z_][a-z0-9_.-]{0,31}$'),
    reason text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 2000),
    from_runtime text NOT NULL CHECK (from_runtime IN ('claude', 'codex', 'agy', 'hermes', 'copilot')),
    to_runtime text CHECK (to_runtime IN ('claude', 'codex', 'agy', 'hermes', 'copilot')),
    to_model text NOT NULL DEFAULT '' CHECK (to_model ~ '^[A-Za-z0-9._:/@+-]{0,128}$'),
    -- The Director as it was: declared runtime, target and slot; its registration (pid, start, uid, generation); and
    -- the session record kept as held evidence. Identifiers and paths only.
    old_identity jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(old_identity) = 'object'),
    -- The recovery packet's provenance: schema, version, project, hold, generated_at, db_xmax, both digests.
    packet jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(packet) = 'object'),
    -- Each change as it was made, and each undo: [{at, step, detail}].
    steps jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(steps) = 'array'),
    -- What it ended with: the new registration, or the rollback's result and what remains.
    result jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(result) = 'object'),
    packet_file text CHECK (packet_file ~ '^/[A-Za-z0-9._/-]+$' AND length(packet_file) <= 512),
    began_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    notice_ticket text,
    user_event_id bigint REFERENCES ticket_board.user_events(id),
    CHECK ((state IN ('applying')) = (finished_at IS NULL)),
    CHECK (state = 'unavailable' OR to_runtime IS NOT NULL),
    CHECK (to_runtime IS DISTINCT FROM from_runtime)
);
-- One rebind runs at a time.
CREATE UNIQUE INDEX IF NOT EXISTS director_rebinds_applying ON ticket_board.director_rebinds ((true))
    WHERE state = 'applying';
CREATE INDEX IF NOT EXISTS director_rebinds_hold ON ticket_board.director_rebinds (hold_id, id);

-- The operator's notice for a rebind outcome: board-wide, its detail the outcome.
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
        'operation_failed', 'director_ad_hoc', 'director_capacity', 'provider_safety_hold', 'director_rebind'])),
    ADD CONSTRAINT user_events_notice_is_ad_hoc CHECK ((kind = 'director_ad_hoc') = (notice_id IS NOT NULL)),
    ADD CONSTRAINT user_events_ticket_unless_board_wide CHECK (
        kind = ANY (ARRAY['director_ad_hoc', 'director_capacity', 'provider_safety_hold', 'director_rebind'])
        OR ticket_id IS NOT NULL);

-- The hold a rebind may answer: current, and on the Director's own account. Raises otherwise.
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_hold(p_hold bigint)
RETURNS ticket_board.provider_account_holds LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = ticket_board, pg_temp AS $$
DECLARE
    h ticket_board.provider_account_holds;
BEGIN
    SELECT * INTO h FROM ticket_board.provider_account_holds WHERE id = p_hold;
    IF h.id IS NULL OR h.cleared_at IS NOT NULL THEN
        RAISE EXCEPTION 'hold % is not a current provider account hold', p_hold;
    END IF;
    IF ticket_board.role_account_runtime('director') IS DISTINCT FROM h.runtime THEN
        RAISE EXCEPTION 'hold % is on the % account, which is not the Director''s (%)', p_hold, h.runtime,
            coalesce(ticket_board.role_account_runtime('director'), 'none');
    END IF;
    RETURN h;
END;
$$;

-- Set the drain pause on the operator's behalf when none stands: routing and delivery to every role pane wait,
-- the Director's included while it is held. Returns the pause id, and whether this began it.
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_pause(p_operator text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    cur ticket_board.work_pauses;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.work_pauses'));
    SELECT * INTO cur FROM ticket_board.work_pauses WHERE ended_at IS NULL FOR UPDATE;
    IF cur.id IS NOT NULL THEN
        RETURN jsonb_build_object('pause', cur.id, 'began', false, 'mode', cur.mode);
    END IF;
    INSERT INTO ticket_board.work_pauses (mode, began_by, reason)
    VALUES ('drain', 'operator:' || p_operator, left(btrim(p_reason), 500)) RETURNING * INTO cur;
    PERFORM pg_notify('ticket_board_state_transition',
                      jsonb_build_object('kind', 'work_pause', 'pause', cur.id, 'mode', cur.mode)::text);
    RETURN jsonb_build_object('pause', cur.id, 'began', true, 'mode', cur.mode);
END;
$$;

-- No eligible alternate runtime: keep work paused and tell the operator, once per hold. Returns what stands.
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_unavailable(
    p_hold bigint, p_operator text, p_reason text, p_detail jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    h ticket_board.provider_account_holds := ticket_board.director_rebind_hold(p_hold);
    existing ticket_board.director_rebinds;
    v_id bigint;
    v_event bigint;
    v_pause jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.director_rebinds'));
    v_pause := ticket_board.director_rebind_pause(p_operator, 'no eligible alternate Director runtime for hold '
                                                              || p_hold || ': ' || p_reason);
    SELECT * INTO existing FROM ticket_board.director_rebinds
     WHERE hold_id = p_hold AND state = 'unavailable' ORDER BY id DESC LIMIT 1;
    IF existing.id IS NOT NULL THEN
        RETURN jsonb_build_object('rebind', existing.id, 'told', false, 'pause', v_pause);
    END IF;
    INSERT INTO ticket_board.director_rebinds (hold_id, state, operator, reason, from_runtime, result, finished_at)
    VALUES (p_hold, 'unavailable', p_operator, left(btrim(p_reason), 2000), h.runtime,
            jsonb_build_object('alternates', coalesce(p_detail, '{}'::jsonb), 'pause', v_pause), clock_timestamp())
    RETURNING id INTO v_id;
    INSERT INTO ticket_board.user_events (kind, ticket_id, detail) VALUES ('director_rebind', NULL, 'unavailable')
    RETURNING id INTO v_event;
    UPDATE ticket_board.director_rebinds SET user_event_id = v_event WHERE id = v_id;
    RETURN jsonb_build_object('rebind', v_id, 'told', true, 'user_event', v_event, 'pause', v_pause);
END;
$$;

-- Begin a rebind of the Director, held by p_hold, to p_to_runtime. Raises unless the hold is current and on the
-- Director's account, the target is another runtime and not itself held, and no other rebind runs.
CREATE OR REPLACE FUNCTION ticket_board.begin_director_rebind(
    p_hold bigint, p_operator text, p_reason text, p_to_runtime text, p_to_model text, p_old jsonb, p_packet jsonb)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    h ticket_board.provider_account_holds := ticket_board.director_rebind_hold(p_hold);
    v_id bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.director_rebinds'));
    IF p_to_runtime = h.runtime THEN
        RAISE EXCEPTION 'the % account is the one held; a rebind moves the Director off it', h.runtime;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.provider_account_holds WHERE runtime = p_to_runtime AND cleared_at IS NULL) THEN
        RAISE EXCEPTION 'the % account is held too', p_to_runtime;
    END IF;
    IF (p_packet->>'hold') IS DISTINCT FROM p_hold::text THEN
        RAISE EXCEPTION 'the recovery packet is for hold %, not hold %', coalesce(p_packet->>'hold', 'none'), p_hold;
    END IF;
    IF EXISTS (SELECT FROM ticket_board.director_rebinds WHERE state = 'applying') THEN
        RAISE EXCEPTION 'another Director rebind is running';
    END IF;
    INSERT INTO ticket_board.director_rebinds (hold_id, state, operator, reason, from_runtime, to_runtime, to_model,
                                               old_identity, packet)
    VALUES (p_hold, 'applying', p_operator, left(btrim(p_reason), 2000), h.runtime, p_to_runtime,
            coalesce(p_to_model, ''), coalesce(p_old, '{}'::jsonb), coalesce(p_packet, '{}'::jsonb))
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

-- Record one step of a running rebind, as it is made or undone.
CREATE OR REPLACE FUNCTION ticket_board.note_director_rebind_step(p_id bigint, p_step text, p_detail jsonb)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF p_step !~ '^[a-z_]{1,40}$' THEN RAISE EXCEPTION 'a rebind step is a name, not %', p_step; END IF;
    UPDATE ticket_board.director_rebinds
       SET steps = steps || jsonb_build_array(jsonb_build_object('at', clock_timestamp(), 'step', p_step,
                                                                 'detail', coalesce(p_detail, '{}'::jsonb)))
     WHERE id = p_id AND state = 'applying';
    IF NOT FOUND THEN RAISE EXCEPTION 'rebind % is not running', p_id; END IF;
END;
$$;

-- The one fixed notice the replacement Director is sent: identifiers, runtime names and a path, nothing else.
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_notice_text(r ticket_board.director_rebinds)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path = ticket_board, pg_temp AS $$
    SELECT format('Board: this is a fresh Director session on %s (Director rebind %s). The Director''s %s account is '
                  'under provider safety hold %s, so the previous session was stopped and is kept, unread, as held '
                  'evidence; nothing was asked of it. Your context is the board''s recovery packet, validated before '
                  'it was written to %s. Check it against the board now: ticket-board-read recovery-packet --check %s '
                  '(exit 0: the board still reads as it says; 1: read ticket-board-read recovery-packet --hold %s '
                  'instead). The hold stays until the operator clears it; send nothing to the %s account.',
                  r.to_runtime, r.id, r.from_runtime, r.hold_id, r.packet_file, r.packet_file, r.hold_id,
                  r.from_runtime);
$$;

-- Finish a running rebind. Applied: the Director must now be on the new runtime; it is sent the fixed notice and the
-- operator one event. Rolled back or needing an operator: work stays paused, and the operator gets one event.
CREATE OR REPLACE FUNCTION ticket_board.finish_director_rebind(
    p_id bigint, p_outcome text, p_result jsonb, p_packet_file text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    r ticket_board.director_rebinds;
    h ticket_board.provider_account_holds;
    v_ticket text;
    v_event bigint;
    v_pause jsonb;
    v_notice bigint;
    v_released integer := 0;
BEGIN
    IF p_outcome NOT IN ('applied', 'rolled_back', 'needs_operator') THEN
        RAISE EXCEPTION 'a rebind ends applied, rolled_back or needs_operator, not %', p_outcome;
    END IF;
    SELECT * INTO r FROM ticket_board.director_rebinds WHERE id = p_id FOR UPDATE;
    IF r.id IS NULL OR r.state <> 'applying' THEN RAISE EXCEPTION 'rebind % is not running', p_id; END IF;
    SELECT * INTO h FROM ticket_board.provider_account_holds WHERE id = r.hold_id;
    IF p_outcome = 'applied' THEN
        IF ticket_board.role_account_runtime('director') IS DISTINCT FROM r.to_runtime THEN
            RAISE EXCEPTION 'the Director is on %, not %; the rebind is not applied',
                coalesce(ticket_board.role_account_runtime('director'), 'no runtime'), r.to_runtime;
        END IF;
        IF p_packet_file IS NULL THEN RAISE EXCEPTION 'an applied rebind names the packet file it wrote'; END IF;
    END IF;
    UPDATE ticket_board.director_rebinds
       SET state = p_outcome, result = coalesce(p_result, '{}'::jsonb), packet_file = p_packet_file,
           finished_at = clock_timestamp()
     WHERE id = p_id RETURNING * INTO r;
    IF p_outcome = 'applied' THEN
        -- The notice hangs on a ticket, as every notice does; the listener judges it by the rebind alone. The hold's
        -- anchor, else the Director's, else the newest open ticket.
        v_ticket := coalesce(h.ticket_id, ticket_board.provider_hold_anchor('director'),
                             (SELECT t.id FROM ticket_board.tickets t WHERE NOT ticket_board.ticket_turn_terminal(t.id)
                               ORDER BY t.updated_at DESC NULLS LAST, t.id LIMIT 1));
        -- What waited on the held Director is due now, and is asked again before it goes.
        UPDATE ticket_board.ticket_notification_queue q SET next_attempt_at = clock_timestamp()
         WHERE q.target_role = 'director' AND q.last_error LIKE 'provider_account_hold%' AND q.claimed_at IS NULL
           AND q.dead_lettered_at IS NULL;
        GET DIAGNOSTICS v_released = ROW_COUNT;
        IF v_ticket IS NOT NULL THEN
            v_notice := ticket_board.enqueue_notification(
                v_ticket, 'escalation', 'director', ticket_board.director_rebind_notice_text(r),
                jsonb_build_object('kind', 'director_rebind', 'reason', 'director_rebind', 'id', v_ticket,
                                   'rebind', r.id, 'hold', r.hold_id, 'runtime', r.to_runtime,
                                   'target_role', 'director'),
                format('director-rebind:%s', r.id));
        END IF;
    ELSE
        v_pause := ticket_board.director_rebind_pause(r.operator, format('Director rebind %s %s; hold %s stands',
                                                                         r.id, p_outcome, r.hold_id));
    END IF;
    INSERT INTO ticket_board.user_events (kind, ticket_id, detail) VALUES ('director_rebind', NULL, p_outcome)
    RETURNING id INTO v_event;
    UPDATE ticket_board.director_rebinds SET user_event_id = v_event, notice_ticket = v_ticket WHERE id = p_id;
    RETURN jsonb_build_object('rebind', r.id, 'state', p_outcome, 'notice_ticket', v_ticket, 'notice', v_notice,
                              'released', v_released, 'user_event', v_event, 'pause', v_pause);
END;
$$;

-- The listener's question before it delivers the replacement Director's notice: is that rebind still the one the
-- Director runs on -- applied, the newest applied rebind, and the Director's account its runtime?
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_notice_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM ticket_board.director_rebinds r
         WHERE r.id = CASE WHEN coalesce(p_payload->>'rebind', '') ~ '^[0-9]{1,18}$'
                           THEN (p_payload->>'rebind')::bigint END
           AND r.state = 'applied'
           AND r.id = (SELECT max(id) FROM ticket_board.director_rebinds WHERE state = 'applied')
           AND ticket_board.role_account_runtime('director') = r.to_runtime);
$$;

-- What the rebind records say, for a read: the newest ten, without reasons' text beyond their length.
CREATE OR REPLACE FUNCTION ticket_board.director_rebind_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'id', r.id, 'hold', r.hold_id, 'state', r.state, 'operator', r.operator, 'from', r.from_runtime,
               'to', r.to_runtime, 'model', r.to_model, 'began_at', r.began_at, 'finished_at', r.finished_at,
               'packet', r.packet, 'steps', jsonb_array_length(r.steps), 'result', r.result) ORDER BY r.id DESC),
           '[]'::jsonb)
      FROM (SELECT * FROM ticket_board.director_rebinds ORDER BY id DESC LIMIT 10) r;
$$;

-- The phone bridge's feed: a rebind that could not replace the Director stands until a later rebind is applied or
-- its hold is cleared; an applied one is news, and stands no longer.
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
                           -- SYRD-738: a rebind that left no replacement, until one is applied or its hold is cleared.
                           WHEN 'director_rebind' THEN EXISTS (
                               SELECT FROM ticket_board.director_rebinds r
                                 JOIN ticket_board.provider_account_holds h ON h.id = r.hold_id
                                WHERE r.user_event_id = e.id AND r.state <> 'applied' AND h.cleared_at IS NULL
                                  AND NOT EXISTS (SELECT FROM ticket_board.director_rebinds later
                                                   WHERE later.hold_id = r.hold_id AND later.id > r.id
                                                     AND later.state = 'applied'))
                           ELSE true END) ORDER BY e.id)
              FROM (SELECT * FROM ticket_board.user_events WHERE id > greatest(coalesce(p_after, 0), 0)
                     ORDER BY id LIMIT least(greatest(coalesce(p_limit, 20), 1), 50)) e
              LEFT JOIN ticket_board.tickets t ON t.id = e.ticket_id
              LEFT JOIN ticket_board.director_notices n ON n.id = e.notice_id), '[]'::jsonb));
$$;

REVOKE ALL ON ticket_board.director_rebinds FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.director_rebinds_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_hold(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_pause(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_unavailable(bigint, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.begin_director_rebind(bigint, text, text, text, text, jsonb, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_director_rebind_step(bigint, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_notice_text(ticket_board.director_rebinds) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.finish_director_rebind(bigint, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_notice_is_current(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.director_rebind_status() FROM PUBLIC;
GRANT SELECT ON ticket_board.director_rebinds TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.director_rebind_notice_is_current(jsonb) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.director_rebind_status() TO ticket_board_service, ticket_board_listener;

COMMIT;
