-- SYRD-616: durable execution leases for long commands under a role's pane.
--
-- A role's command that never ends holds the role's current turn for good,
-- and nothing that watches later deliveries notices: the work looks like the
-- role's own. The listener now records each long-running session of its own
-- under a managed role's pane as a lease with a wall-clock deadline, tells the
-- Director once when one runs past it or outlives its ticket, and carries out
-- the Director's interrupt. The Director may extend a lease by a bounded
-- interval. Nothing here stops a process: the listener does, and only when the
-- Director asked.
--
-- One row per role and child (a process group and its leader's start time,
-- SYRD-583's key), so a reused pid is a new lease. A stale lease's alert uses
-- SYRD-583's background_child_alerts row and dedupe key, so a child is named
-- once whichever report finds it first.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.execution_leases (
    id bigserial PRIMARY KEY,
    role text NOT NULL,
    child text NOT NULL,
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    turn_started_at timestamptz,
    pid integer NOT NULL,
    pgid integer NOT NULL,
    start_ticks bigint NOT NULL,
    started_at timestamptz NOT NULL,
    command text NOT NULL DEFAULT '',
    deadline timestamptz NOT NULL,
    deadline_source text NOT NULL CHECK (deadline_source IN ('default', 'declared', 'hard', 'extended')),
    hard boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'running' CHECK (state IN ('running', 'expired', 'stale', 'exited', 'interrupted')),
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    extensions jsonb NOT NULL DEFAULT '[]'::jsonb,
    alert_kind text CHECK (alert_kind IN ('expired', 'stale')),
    alerted_at timestamptz,
    alerted_deadline timestamptz,
    alert_notification_id bigint,
    alert_cleared_at timestamptz,
    alert_clear_reason text,
    interrupt jsonb,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    end_reason text,
    UNIQUE (role, child)
);
CREATE INDEX IF NOT EXISTS execution_leases_open_idx ON ticket_board.execution_leases (role) WHERE ended_at IS NULL;

-- The listener records what it sees: a new lease, or the next look at one.
CREATE OR REPLACE FUNCTION ticket_board.observe_execution_lease(
    p_role text, p_child text, p_pid integer, p_pgid integer, p_start_ticks bigint, p_started_at timestamptz,
    p_command text, p_ticket_id text, p_turn_started_at timestamptz, p_deadline timestamptz, p_source text,
    p_hard boolean, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('observe_execution_lease');
    SELECT * INTO l FROM ticket_board.execution_leases WHERE role = p_role AND child = p_child FOR UPDATE;
    IF FOUND THEN
        RETURN ticket_board.observe_execution_lease_again(l, p_turn_started_at, p_deadline, p_source, p_hard, p_evidence);
    END IF;
    INSERT INTO ticket_board.execution_leases (role, child, ticket_id, turn_started_at, pid, pgid, start_ticks, started_at,
                                               command, deadline, deadline_source, hard, evidence)
    VALUES (p_role, p_child, (SELECT t.id FROM ticket_board.tickets t WHERE t.id = p_ticket_id), p_turn_started_at,
            p_pid, p_pgid, p_start_ticks, p_started_at, left(coalesce(p_command, ''), 240), p_deadline, p_source,
            coalesce(p_hard, false), jsonb_build_array(p_evidence))
    ON CONFLICT (role, child) DO NOTHING
    RETURNING * INTO l;
    IF FOUND THEN
        RETURN to_jsonb(l);
    END IF;
    -- Another listener recorded it in between.
    SELECT * INTO l FROM ticket_board.execution_leases WHERE role = p_role AND child = p_child FOR UPDATE;
    RETURN ticket_board.observe_execution_lease_again(l, p_turn_started_at, p_deadline, p_source, p_hard, p_evidence);
END;
$$;

-- The next look at a lease the listener already recorded: a little more evidence, a declaration's deadline.
CREATE OR REPLACE FUNCTION ticket_board.observe_execution_lease_again(
    l ticket_board.execution_leases, p_turn_started_at timestamptz, p_deadline timestamptz, p_source text,
    p_hard boolean, p_evidence jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF l.ended_at IS NOT NULL THEN
        RETURN to_jsonb(l);
    END IF;
    UPDATE ticket_board.execution_leases SET
        last_seen_at = clock_timestamp(),
        -- The last few looks only: evidence for a person, never proof of progress.
        evidence = (SELECT coalesce(jsonb_agg(e ORDER BY n), '[]'::jsonb) FROM (
                        SELECT e, n FROM jsonb_array_elements(l.evidence || jsonb_build_array(p_evidence))
                            WITH ORDINALITY AS x(e, n)
                        ORDER BY n DESC LIMIT 6) kept),
        -- A declaration replaces the default; the Director's extension outranks both.
        deadline = CASE WHEN p_source IN ('declared', 'hard') AND l.deadline_source <> 'extended' THEN p_deadline
                        ELSE l.deadline END,
        deadline_source = CASE WHEN p_source IN ('declared', 'hard') AND l.deadline_source <> 'extended' THEN p_source
                               ELSE l.deadline_source END,
        hard = CASE WHEN p_source IN ('declared', 'hard') AND l.deadline_source <> 'extended' THEN coalesce(p_hard, false)
                    ELSE l.hard END,
        -- Only a turn it never had: a change of turn is note_execution_lease_turn's, which clears the alert.
        turn_started_at = coalesce(l.turn_started_at, p_turn_started_at)
    WHERE id = l.id
    RETURNING * INTO l;
    RETURN to_jsonb(l);
END;
$$;

-- Close a lease's alert; with p_withdraw, also take back its notice if the Director has not been sent it yet --
-- only when what it says is no longer true (the process ended, its deadline moved, the Director acted).
CREATE OR REPLACE FUNCTION ticket_board.clear_execution_lease_alert(p_lease bigint, p_reason text, p_withdraw boolean)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
BEGIN
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease FOR UPDATE;
    IF NOT FOUND OR l.alerted_at IS NULL OR l.alert_cleared_at IS NOT NULL THEN
        RETURN false;
    END IF;
    UPDATE ticket_board.execution_leases SET alert_cleared_at = clock_timestamp(), alert_clear_reason = p_reason
    WHERE id = p_lease;
    IF p_withdraw THEN
        DELETE FROM ticket_board.ticket_notification_queue
        WHERE id = l.alert_notification_id AND claimed_at IS NULL AND dead_lettered_at IS NULL;
    END IF;
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.note_execution_lease_turn(p_lease bigint, p_turn_started_at timestamptz)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    changed integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_execution_lease_turn');
    UPDATE ticket_board.execution_leases SET turn_started_at = p_turn_started_at
    WHERE id = p_lease AND ended_at IS NULL AND turn_started_at IS DISTINCT FROM p_turn_started_at;
    GET DIAGNOSTICS changed = ROW_COUNT;
    -- A new turn closes a past-deadline alert's episode, but the command still runs past it, so a notice not yet
    -- delivered stays. A stale alert stays open: the command still outlived its ticket.
    IF changed > 0 AND (SELECT alert_kind FROM ticket_board.execution_leases WHERE id = p_lease) = 'expired' THEN
        PERFORM ticket_board.clear_execution_lease_alert(p_lease, 'the role began another turn', false);
    END IF;
    RETURN changed > 0;
END;
$$;

-- Tell the Director once: past its deadline (once per deadline), or outliving its ticket (once per child).
CREATE OR REPLACE FUNCTION ticket_board.note_execution_lease_alert(
    p_lease bigint, p_kind text, p_ticket_id text, p_what text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
    added integer;
    notice text;
    alert_id bigint;
    dedupe text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_execution_lease_alert');
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease FOR UPDATE;
    IF NOT FOUND OR l.ended_at IS NOT NULL OR btrim(coalesce(p_what, '')) = ''
            OR NOT EXISTS (SELECT 1 FROM ticket_board.tickets t WHERE t.id = p_ticket_id) THEN
        RETURN false;
    END IF;
    IF p_kind = 'expired' THEN
        IF l.state = 'stale' OR l.alerted_deadline = l.deadline THEN
            RETURN false;
        END IF;
        notice := format('%s''s command has run past its lease: %s', l.role, p_what);
        dedupe := 'execution_lease:' || l.id || ':' || floor(extract(epoch FROM l.deadline))::bigint;
        UPDATE ticket_board.execution_leases SET state = 'expired' WHERE id = l.id;
    ELSIF p_kind = 'stale' THEN
        IF l.state = 'stale' THEN
            RETURN false;
        END IF;
        UPDATE ticket_board.execution_leases SET state = 'stale' WHERE id = l.id;
        INSERT INTO ticket_board.background_child_alerts (target_role, child, ticket_id)
        VALUES (l.role, l.child, p_ticket_id)
        ON CONFLICT (target_role, child) DO NOTHING;
        GET DIAGNOSTICS added = ROW_COUNT;
        IF added = 0 THEN
            RETURN false;  -- SYRD-583 or SYRD-636 already named this child
        END IF;
        notice := format('%s''s command outlived the ticket it was started for: %s', l.role, p_what);
        dedupe := 'background_child:' || l.role || ':' || l.child;
    ELSE
        RETURN false;
    END IF;
    alert_id := ticket_board.enqueue_notification(
        p_ticket_id, 'ticket_update', 'director', notice,
        jsonb_build_object('kind', 'execution_lease', 'id', p_ticket_id, 'target_role', 'director', 'role', l.role,
                           'lease', l.id, 'alert', p_kind, 'child', l.child, 'message', notice),
        dedupe);
    UPDATE ticket_board.execution_leases SET alert_kind = p_kind, alerted_at = clock_timestamp(),
        alerted_deadline = l.deadline, alert_notification_id = alert_id, alert_cleared_at = NULL, alert_clear_reason = NULL
    WHERE id = l.id;
    IF p_kind = 'stale' THEN
        UPDATE ticket_board.background_child_alerts SET alert_notification_id = alert_id
        WHERE target_role = l.role AND child = l.child;
    END IF;
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.end_execution_lease(p_lease bigint, p_state text, p_reason text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    changed integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('end_execution_lease');
    IF p_state NOT IN ('exited', 'interrupted') THEN
        RETURN false;
    END IF;
    UPDATE ticket_board.execution_leases SET state = p_state, ended_at = clock_timestamp(), end_reason = p_reason
    WHERE id = p_lease AND ended_at IS NULL;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed > 0 THEN
        PERFORM ticket_board.clear_execution_lease_alert(p_lease, p_reason, true);
    END IF;
    RETURN changed > 0;
END;
$$;

-- The Director moves a live lease's deadline, by a bounded interval, with a reason.
CREATE OR REPLACE FUNCTION ticket_board.extend_execution_lease(p_lease bigint, p_seconds integer, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
    who text;
    reason text := btrim(coalesce(p_reason, ''));
    new_deadline timestamptz;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    who := ticket_board.current_app_actor();
    IF reason = '' THEN
        RAISE EXCEPTION 'extending a lease needs a reason';
    END IF;
    IF p_seconds IS NULL OR p_seconds < 60 OR p_seconds > 4 * 3600 THEN
        RAISE EXCEPTION 'a lease is extended by 1 minute to 4 hours at a time, not % seconds', p_seconds;
    END IF;
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no execution lease %', p_lease;
    END IF;
    IF l.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'execution lease % has ended (%)', p_lease, l.end_reason;
    END IF;
    new_deadline := greatest(l.deadline, clock_timestamp()) + make_interval(secs => p_seconds);
    UPDATE ticket_board.execution_leases SET deadline = new_deadline, deadline_source = 'extended',
        state = CASE WHEN l.state = 'expired' THEN 'running' ELSE l.state END,
        extensions = l.extensions || jsonb_build_array(jsonb_build_object(
            'by', who, 'reason', reason, 'at', clock_timestamp(), 'seconds', p_seconds, 'deadline', new_deadline))
    WHERE id = l.id;
    PERFORM ticket_board.clear_execution_lease_alert(l.id, 'extended by ' || who, true);
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease;
    RETURN to_jsonb(l);
END;
$$;

-- The Director asks for a lease's process group to be interrupted; the listener carries it out.
CREATE OR REPLACE FUNCTION ticket_board.request_execution_lease_interrupt(p_lease bigint, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
    who text;
    reason text := btrim(coalesce(p_reason, ''));
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_manually_controlled');
    who := ticket_board.current_app_actor();
    IF reason = '' THEN
        RAISE EXCEPTION 'interrupting a lease needs a reason';
    END IF;
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no execution lease %', p_lease;
    END IF;
    IF l.ended_at IS NOT NULL THEN
        RAISE EXCEPTION 'execution lease % has ended (%)', p_lease, l.end_reason;
    END IF;
    IF l.interrupt IS NOT NULL THEN
        RAISE EXCEPTION 'execution lease % is already being interrupted', p_lease;
    END IF;
    UPDATE ticket_board.execution_leases SET interrupt = jsonb_build_object(
        'requested_by', who, 'reason', reason, 'requested_at', clock_timestamp(), 'stage', 'requested')
    WHERE id = l.id;
    PERFORM ticket_board.clear_execution_lease_alert(l.id, 'interrupt requested by ' || who, true);
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease;
    RETURN to_jsonb(l);
END;
$$;

-- The listener's record of each interrupt step; at the last it tells the role, once.
CREATE OR REPLACE FUNCTION ticket_board.record_execution_lease_interrupt(
    p_lease bigint, p_stage text, p_result text, p_ticket_id text, p_role_notice text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    l ticket_board.execution_leases%ROWTYPE;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('record_execution_lease_interrupt');
    SELECT * INTO l FROM ticket_board.execution_leases WHERE id = p_lease FOR UPDATE;
    IF NOT FOUND OR l.interrupt IS NULL OR l.interrupt->>'stage' = 'done' THEN
        RETURN false;
    END IF;
    UPDATE ticket_board.execution_leases SET interrupt = l.interrupt || jsonb_build_object(
        'stage', p_stage, 'stage_at', clock_timestamp(), 'result', p_result)
    WHERE id = l.id;
    IF p_stage = 'done' THEN
        UPDATE ticket_board.execution_leases SET state = 'interrupted', ended_at = coalesce(ended_at, clock_timestamp()),
            end_reason = coalesce(end_reason, 'interrupted by ' || (l.interrupt->>'requested_by'))
        WHERE id = l.id;
        IF btrim(coalesce(p_role_notice, '')) <> '' AND EXISTS (SELECT 1 FROM ticket_board.tickets t WHERE t.id = p_ticket_id) THEN
            PERFORM ticket_board.enqueue_notification(
                p_ticket_id, 'ticket_update', l.role, p_role_notice,
                jsonb_build_object('kind', 'execution_lease_interrupted', 'id', p_ticket_id, 'target_role', l.role,
                                   'lease', l.id, 'message', p_role_notice),
                'execution_lease_interrupted:' || l.id);
        END IF;
    END IF;
    RETURN true;
END;
$$;

-- Ended leases are kept a week, for the record.
CREATE OR REPLACE FUNCTION ticket_board.prune_execution_leases()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    removed integer;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('prune_execution_leases');
    DELETE FROM ticket_board.execution_leases WHERE ended_at < clock_timestamp() - interval '7 days';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;

REVOKE ALL ON ticket_board.execution_leases FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.execution_leases_id_seq FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.observe_execution_lease(text, text, integer, integer, bigint, timestamptz, text, text,
    timestamptz, timestamptz, text, boolean, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.observe_execution_lease_again(ticket_board.execution_leases, timestamptz,
    timestamptz, text, boolean, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.clear_execution_lease_alert(bigint, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_execution_lease_turn(bigint, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.note_execution_lease_alert(bigint, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.end_execution_lease(bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.extend_execution_lease(bigint, integer, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.request_execution_lease_interrupt(bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_execution_lease_interrupt(bigint, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.prune_execution_leases() FROM PUBLIC;
GRANT SELECT ON ticket_board.execution_leases TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.observe_execution_lease(text, text, integer, integer, bigint, timestamptz, text,
    text, timestamptz, timestamptz, text, boolean, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_execution_lease_turn(bigint, timestamptz) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.note_execution_lease_alert(bigint, text, text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.end_execution_lease(bigint, text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.record_execution_lease_interrupt(bigint, text, text, text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.prune_execution_leases() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.extend_execution_lease(bigint, integer, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.request_execution_lease_interrupt(bigint, text) TO ticket_board_service;

COMMIT;
