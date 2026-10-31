-- SYRD-714: the board's durable record of the events that need the User, for the host's phone bridge.
--
-- The host's user-notification bridge (scripts/user_notify.py) sends a few high-value events to the operator's
-- phone. It must never read, acknowledge or change role-pane delivery, so it does not read the notification queue.
-- These triggers record each event here instead, in the same transaction as the change that caused it:
--   * user_review       -- a ticket enters a human's review: a review stage assigned to a role of kind 'user'
--                          (SYRD-582's definition), or, with no declared workflow, `user_review`;
--   * operator_blocker  -- a ticket gains an unresolved `operator:<name>` blocker. set_blockers deletes and
--                          re-inserts every standing blocker, so a wait is tracked per (ticket, operator) and
--                          ends only when, at commit, no unresolved blocker names that operator;
--   * operation_failed  -- a privileged job recorded on a ticket finishes failed, timed out or rolled back;
--   * director_ad_hoc   -- the Director's own ad hoc notification (`record_director_notice`), recorded with who
--                          sent it -- the control role's registered live process -- in `director_notices`.
-- Nothing else is recorded: no assignment, nudge, comment or other transition. A row holds no ticket text; the
-- reader adds the current title, cut short. Rows older than 30 days are pruned as new ones arrive.
--
-- `user_events_after` is the one read: at most 50 events after an id, each with a key that stays the same however
-- often it is read, and whether it still stands. schema.sql ends with the same objects.
BEGIN;

-- A Director's ad hoc notification, as the board accepted it: who sent it (the control role's registered process,
-- as the kernel named it to the board), what it says, the ticket it links, and the digest of the preview the
-- Director confirmed. Never changed; its delivery is the host's record.
CREATE TABLE IF NOT EXISTS ticket_board.director_notices (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    requester_role text NOT NULL,
    requester_pid bigint NOT NULL,
    requester_start_time bigint NOT NULL,
    requester_uid bigint NOT NULL,
    requester_generation bigint NOT NULL,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 80 AND title !~ '[[:cntrl:]]'),
    message text NOT NULL CHECK (char_length(message) BETWEEN 1 AND 300 AND message !~ '[[:cntrl:]]'),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    urgent boolean NOT NULL DEFAULT false,
    reason text NOT NULL DEFAULT '' CHECK (char_length(reason) <= 200 AND reason !~ '[[:cntrl:]]'),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    CHECK (NOT urgent OR btrim(reason) <> '')
);

CREATE TABLE IF NOT EXISTS ticket_board.user_events (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind text NOT NULL CHECK (kind IN ('user_review', 'operator_blocker', 'operation_failed', 'director_ad_hoc')),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    -- The operator's name, the failed action's name, the review stage, or normal/urgent: never free text.
    detail text NOT NULL DEFAULT '' CHECK (detail ~ '^[a-z0-9_-]{0,64}$'),
    notice_id bigint REFERENCES ticket_board.director_notices(id) ON DELETE CASCADE,
    CHECK ((kind = 'director_ad_hoc') = (notice_id IS NOT NULL)),
    CHECK (kind = 'director_ad_hoc' OR ticket_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS ticket_board.user_event_operator_waits (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    operator text NOT NULL,
    since timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ticket_id, operator)
);

-- Recording an event never fails the change that caused it: board work goes on without the phone.
CREATE OR REPLACE FUNCTION ticket_board.record_user_event(p_kind text, p_ticket text, p_detail text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    DELETE FROM ticket_board.user_events WHERE at < clock_timestamp() - interval '30 days';
    INSERT INTO ticket_board.user_events (kind, ticket_id, detail)
    VALUES (p_kind, p_ticket, left(regexp_replace(lower(coalesce(p_detail, '')), '[^a-z0-9_-]', '', 'g'), 64));
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'user event % for % not recorded: %', p_kind, p_ticket, SQLERRM;
END;
$$;

-- Whether a ticket in this stage, with this assignee, is waiting for a human's review.
CREATE OR REPLACE FUNCTION ticket_board.user_event_needs_user(p_state text, p_assignee text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN coalesce(p_state = 'user_review', false)
                ELSE coalesce(ticket_board.declared_stage_kind(p_state) = 'review', false) AND EXISTS (
                    SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
                     WHERE r->>'name' = p_assignee AND r->>'kind' = 'user') END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.user_event_review()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF ticket_board.user_event_needs_user(NEW.state, NEW.assignee)
       AND (TG_OP = 'INSERT' OR NOT ticket_board.user_event_needs_user(OLD.state, OLD.assignee)) THEN
        PERFORM ticket_board.record_user_event('user_review', NEW.id, NEW.state);
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_user_event_review ON ticket_board.tickets;
CREATE TRIGGER tickets_z_user_event_review AFTER INSERT OR UPDATE OF state, assignee ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.user_event_review();

CREATE OR REPLACE FUNCTION ticket_board.user_event_operator_blocker()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE v_operator text;
BEGIN
    IF NEW.resolved OR NEW.blocker_ticket_id NOT LIKE 'operator:%' THEN
        RETURN NULL;
    END IF;
    v_operator := substr(NEW.blocker_ticket_id, length('operator:') + 1);
    INSERT INTO ticket_board.user_event_operator_waits (ticket_id, operator) VALUES (NEW.ticket_id, v_operator)
        ON CONFLICT (ticket_id, operator) DO NOTHING;
    IF FOUND THEN
        PERFORM ticket_board.record_user_event('operator_blocker', NEW.ticket_id, v_operator);
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_blockers_user_event ON ticket_board.ticket_blockers;
CREATE TRIGGER ticket_blockers_user_event AFTER INSERT OR UPDATE OF resolved, blocker_ticket_id
    ON ticket_board.ticket_blockers FOR EACH ROW EXECUTE FUNCTION ticket_board.user_event_operator_blocker();

-- At commit: a wait whose operator no unresolved blocker names any more has ended. Deferred, so set_blockers'
-- delete-and-reinsert of a standing blocker is one unbroken wait.
CREATE OR REPLACE FUNCTION ticket_board.user_event_operator_released()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF OLD.blocker_ticket_id LIKE 'operator:%' AND NOT EXISTS (
        SELECT FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = OLD.ticket_id AND b.blocker_ticket_id = OLD.blocker_ticket_id AND NOT b.resolved) THEN
        DELETE FROM ticket_board.user_event_operator_waits
         WHERE ticket_id = OLD.ticket_id AND operator = substr(OLD.blocker_ticket_id, length('operator:') + 1);
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_blockers_user_event_released ON ticket_board.ticket_blockers;
CREATE CONSTRAINT TRIGGER ticket_blockers_user_event_released AFTER DELETE OR UPDATE
    ON ticket_board.ticket_blockers DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ticket_board.user_event_operator_released();

CREATE OR REPLACE FUNCTION ticket_board.user_event_operation_failed()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF NEW.state IN ('failed', 'timed_out', 'rolled_back') AND (TG_OP = 'INSERT' OR OLD.state = 'running') THEN
        PERFORM ticket_board.record_user_event('operation_failed', NEW.ticket_id, NEW.action);
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS privileged_jobs_user_event ON ticket_board.privileged_jobs;
CREATE TRIGGER privileged_jobs_user_event AFTER INSERT OR UPDATE OF state ON ticket_board.privileged_jobs
    FOR EACH ROW EXECUTE FUNCTION ticket_board.user_event_operation_failed();

-- The Director's ad hoc notification. The board server has already checked the text and named the calling process
-- from the kernel; this checks again that the caller is the control role and is that role's registered live process
-- -- a retired, replaced or detached process is refused -- and holds the rate: at most 5 an hour, and at most 1
-- urgent, which needs a reason. The same preview confirmed again within ten minutes is the notice already recorded.
CREATE OR REPLACE FUNCTION ticket_board.record_director_notice(
    p_pid bigint, p_start_time bigint, p_uid bigint, p_title text, p_message text, p_ticket text, p_urgent boolean,
    p_reason text, p_digest text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    v_caller text := ticket_board.current_app_actor();
    v_generation bigint;
    v_id bigint;
    v_event bigint;
BEGIN
    IF NOT coalesce(ticket_board.role_controls_project(v_caller), false) THEN
        RAISE EXCEPTION 'only the control role sends an ad hoc phone notification, not %', v_caller
            USING ERRCODE = '42501';
    END IF;
    SELECT generation INTO v_generation FROM ticket_board.role_runtime_assignments
     WHERE role = v_caller AND process_pid = p_pid AND process_start_time = p_start_time AND process_uid = p_uid;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'process % is not %''s registered live pane: a retired, replaced or detached process sends no '
            'phone notification', p_pid, v_caller USING ERRCODE = '42501';
    END IF;
    IF p_ticket IS NOT NULL AND NOT EXISTS (SELECT FROM ticket_board.tickets WHERE id = p_ticket) THEN
        RAISE EXCEPTION 'ticket % is not on this board', p_ticket;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.director_notices'));
    SELECT id INTO v_id FROM ticket_board.director_notices
     WHERE digest = p_digest AND requester_role = v_caller AND at > clock_timestamp() - interval '10 minutes'
     ORDER BY id DESC LIMIT 1;
    IF FOUND THEN
        RETURN jsonb_build_object('notice', v_id, 'duplicate', true);
    END IF;
    IF (SELECT count(*) FROM ticket_board.director_notices WHERE at > clock_timestamp() - interval '1 hour') >= 5 THEN
        RAISE EXCEPTION 'at most 5 ad hoc phone notifications an hour: wait, or put it on the board';
    END IF;
    IF p_urgent AND EXISTS (SELECT FROM ticket_board.director_notices
                             WHERE urgent AND at > clock_timestamp() - interval '1 hour') THEN
        RAISE EXCEPTION 'at most 1 urgent ad hoc phone notification an hour';
    END IF;
    INSERT INTO ticket_board.director_notices (requester_role, requester_pid, requester_start_time, requester_uid,
                                               requester_generation, title, message, ticket_id, urgent, reason, digest)
    VALUES (v_caller, p_pid, p_start_time, p_uid, v_generation, p_title, p_message, p_ticket, coalesce(p_urgent, false),
            coalesce(p_reason, ''), p_digest)
    RETURNING id INTO v_id;
    DELETE FROM ticket_board.user_events WHERE at < clock_timestamp() - interval '30 days';
    INSERT INTO ticket_board.user_events (kind, ticket_id, detail, notice_id)
    VALUES ('director_ad_hoc', p_ticket, CASE WHEN p_urgent THEN 'urgent' ELSE 'normal' END, v_id)
    RETURNING id INTO v_event;
    RETURN jsonb_build_object('notice', v_id, 'event', v_event, 'duplicate', false);
END;
$$;

-- The bridge's one read. `standing`: the review still waits for the User, the operator wait has not ended.
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
                           ELSE true END) ORDER BY e.id)
              FROM (SELECT * FROM ticket_board.user_events WHERE id > greatest(coalesce(p_after, 0), 0)
                     ORDER BY id LIMIT least(greatest(coalesce(p_limit, 20), 1), 50)) e
              LEFT JOIN ticket_board.tickets t ON t.id = e.ticket_id
              LEFT JOIN ticket_board.director_notices n ON n.id = e.notice_id), '[]'::jsonb));
$$;

REVOKE ALL ON ticket_board.user_events, ticket_board.user_event_operator_waits, ticket_board.director_notices FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.user_events_id_seq, ticket_board.director_notices_id_seq FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.record_director_notice(bigint, bigint, bigint, text, text, text, boolean, text,
                                                                text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.record_user_event(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_event_needs_user(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_event_review() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_event_operator_blocker() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_event_operator_released() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_event_operation_failed() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.user_events_after(bigint, integer) FROM PUBLIC;

COMMIT;
