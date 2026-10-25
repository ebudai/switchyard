-- SYRD-662: a wait on the User is the Director's decision, where the workflow
-- lets one stand.
--
-- SYRD-222 sat in Implementation, owned by Ops, with awaiting_role=user: Ops
-- had recorded `request-dependency --role user`, which SYRD-549 admitted from
-- any role holding await_role. The ticket stayed in Implementation, Ops' slot
-- stayed taken, and the User never saw it in User Review. Now:
--   * set_awaiting_role refuses the User for every caller and says what to do
--     instead: finish the stage through its declared transition, with the UAT
--     steps on the ticket, so it reaches User Review through every gate;
--   * await_user is the one way to wait on the User: the Director's, with a
--     reason and the action or decision asked of the User, in a stage the
--     workflow's user_waits lists; a trigger refuses any other writer;
--   * entering User Review needs every sign-off the ticket owes, and the
--     publication of its commit where the Publisher is declared, whatever the
--     path, the Director's override included;
--   * the unresolved-turn prompt offers await-user, and only to the Director
--     where it would be admitted;
--   * a wait on the User already standing is not moved: the ticket's JSON says
--     it is not valid and why, the Director's queue shows it, and the Director
--     is told once here.
--
-- schema.sql carries the same objects.

BEGIN;

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
    -- The list is the legacy board's. Under a declared workflow require_actor
    -- admits by the caller's own `await_role` capability and ignores it, so a
    -- custom-named role holding the capability may record a wait (SYRD-549).
    actor := ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    -- A pane role, or the declared User (SYRD-549).
    IF ticket_board.wait_target_kind(normalized_role) IS NULL THEN
        RAISE EXCEPTION 'invalid awaiting_role: %', awaiting_role;
    END IF;
    -- SYRD-662: a wait on the User is the Director's await-user, never a
    -- dependency anyone records. From Implementation it hid work the User never
    -- saw in User Review (SYRD-222). Refused before anything is written.
    IF ticket_board.wait_target_kind(normalized_role) = 'external'
       AND NOT ticket_board.user_wait_pending(set_awaiting_role.id) THEN
        RAISE EXCEPTION 'a dependency cannot name the User: %',
            ticket_board.user_wait_guidance(set_awaiting_role.id, ticket_board.current_app_actor());
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

CREATE OR REPLACE FUNCTION ticket_board.unresolved_turn_choices(p_state text, p_assignee text, p_owner text)
RETURNS text[]
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    cfg jsonb := ticket_board.declared_workflow();
    caps jsonb;
    choices text[] := ARRAY[]::text[];
BEGIN
    IF cfg IS NULL THEN
        choices := ARRAY['transition'];
        IF ticket_board.workflow_wait_stage(p_state) AND p_owner <> 'user' THEN
            choices := choices || 'request-dependency'::text;
        END IF;
        IF p_owner = 'director' THEN
            choices := choices || 'blocker'::text;
        END IF;
        RETURN choices;
    END IF;
    SELECT r.definition->'capabilities' INTO caps
    FROM ticket_board.workflow_roles r
    WHERE r.name = p_owner AND (r.definition->>'active')::boolean;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'transitions') tr
        WHERE tr->>'from' = p_state
          AND ticket_board.transition_allows_actor(cfg, tr, p_owner)
          AND (NOT coalesce((tr->>'owner_scoped')::boolean, false) OR p_owner = p_assignee)
    ) THEN
        choices := choices || 'transition'::text;
    END IF;
    IF ticket_board.workflow_wait_stage(p_state) AND coalesce(caps ?& ARRAY['add_comment', 'await_role'], false) THEN
        choices := choices || 'request-dependency'::text;
    END IF;
    -- SYRD-662: a wait on the User is the Director's own decision, where the
    -- workflow lets one stand, and is offered to nobody else.
    IF coalesce(caps ?& ARRAY['add_comment', 'await_role'], false)
       AND ticket_board.user_wait_refusal(p_state, p_owner) IS NULL THEN
        choices := choices || 'await-user'::text;
    END IF;
    IF coalesce(caps ? 'set_blockers', false) THEN
        choices := choices || 'blocker'::text;
    END IF;
    RETURN choices;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.unresolved_turn_repair_options(p_choices text[])
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    options text[];
BEGIN
    SELECT coalesce(array_agg(CASE c
               WHEN 'transition' THEN 'transition it if the work is done'
               WHEN 'request-dependency' THEN '`request-dependency` if it is waiting on another role'
               -- SYRD-662: the User is reached through User Review, or by the Director's await-user.
               WHEN 'await-user' THEN '`await-user` if only the User can act or decide next'
               WHEN 'blocker' THEN 'record a blocker if something else must land first'
           END ORDER BY n) FILTER (WHERE c IN ('transition', 'request-dependency', 'await-user', 'blocker')), ARRAY[]::text[])
    INTO options
    FROM unnest(p_choices) WITH ORDINALITY AS u(c, n);
    RETURN CASE cardinality(options)
               WHEN 0 THEN 'No move on it from this stage is yours to make: say in a comment what it needs.'
               WHEN 1 THEN 'Do this: ' || options[1] || '.'
               ELSE 'Do one of: ' || array_to_string(options[1:cardinality(options) - 1], '; ')
                    || '; or ' || options[cardinality(options)] || '.'
           END;
END;
$$;

-- SYRD-662: a wait on the User is the Director's decision, where the workflow
-- lets one stand (identical to migrations/pgu1012_syrd662_user_waits.sql).

-- The Director's request, for the wait it was made with: current while the
-- ticket still waits on that role since that moment.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_user_wait_requests (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL,
    since_at timestamptz,
    requested_by text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    request text NOT NULL CHECK (btrim(request) <> ''),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- The transaction await_user is recording the wait in, while it does: the
    -- one mark set_awaiting_role and the guard admit a wait on the User by.
    -- Nothing but await_user writes this table, so no caller can forge it.
    pending_xact bigint
);

-- Whether await_user is recording a wait on the User for p_ticket in this transaction.
CREATE OR REPLACE FUNCTION ticket_board.user_wait_pending(p_ticket text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.ticket_user_wait_requests
                    WHERE ticket_id = p_ticket AND pending_xact = txid_current());
$$;

-- Whether the workflow lets the Director wait on the User in p_stage: listed in
-- its user_waits. What workflow_config refuses to list is ignored here too:
-- Implementation is finished through its transitions, User Review is where the
-- User already is, and a terminal or unowned stage holds no work to wait.
CREATE OR REPLACE FUNCTION ticket_board.workflow_user_wait_stage(p_stage text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); s jsonb;
BEGIN
    IF cfg IS NULL OR jsonb_typeof(cfg->'user_waits') IS DISTINCT FROM 'array' OR NOT cfg->'user_waits' ? p_stage THEN
        RETURN false;
    END IF;
    SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = p_stage;
    RETURN s IS NOT NULL AND NOT coalesce((s->>'terminal')::boolean, true) AND s->>'kind' <> 'implementation'
       AND NOT ticket_board.uat_stage(p_stage) AND jsonb_array_length(coalesce(s->'owners', '[]'::jsonb)) > 0;
END;
$$;

-- Why p_actor may not wait on the User in p_stage, or NULL when it may.
CREATE OR REPLACE FUNCTION ticket_board.user_wait_refusal(p_state text, p_actor text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); label text;
BEGIN
    IF cfg IS NULL THEN
        RETURN 'this board declares no workflow, and only a declared workflow lets the Director wait on the User';
    END IF;
    -- The control role, by the capabilities that define it (SYRD-78). A board UI
    -- confirmation acts as the User, so it is not the Director here either.
    IF NOT EXISTS (SELECT FROM ticket_board.workflow_roles r
                   WHERE r.name = p_actor AND (r.definition->>'active')::boolean
                     AND r.definition->'capabilities' ?& ticket_board.control_capabilities()) THEN
        RETURN format('only the Director waits on the User, and %s is not the Director', coalesce(p_actor, 'nobody'));
    END IF;
    IF NOT ticket_board.workflow_user_wait_stage(p_state) THEN
        SELECT coalesce(x->>'label', x->>'name') INTO label FROM jsonb_array_elements(cfg->'stages') x
         WHERE x->>'name' = p_state;
        RETURN format('%s is not a stage this workflow lets the Director wait on the User in (its user_waits)',
                      coalesce(label, p_state));
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE ticket_board.wait_target_kind(r.name) = 'external') THEN
        RETURN 'this workflow declares no active User to wait on';
    END IF;
    RETURN NULL;
END;
$$;

-- What a role that tried to wait on the User should do instead.
CREATE OR REPLACE FUNCTION ticket_board.user_wait_guidance(p_ticket text, p_actor text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); t ticket_board.tickets; label text; uat text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    IF ticket_board.user_wait_refusal(t.state, p_actor) IS NULL THEN
        RETURN format('a wait on the User is the Director''s decision, with its reason and what the User is asked to do: '
                      'ticket-board-write await-user %s --reason ... --request ...', p_ticket);
    END IF;
    SELECT coalesce(x->>'label', x->>'name') INTO label FROM jsonb_array_elements(cfg->'stages') x
     WHERE x->>'name' = t.state;
    SELECT coalesce(x->>'label', x->>'name') INTO uat FROM jsonb_array_elements(cfg->'stages') WITH ORDINALITY AS s(x, n)
     WHERE ticket_board.uat_stage(x->>'name') ORDER BY n LIMIT 1;
    IF uat IS NULL THEN
        RETURN format('%s is in %s, and this workflow has no User Review stage, so nothing on it waits on the User. '
                      'Finish %s through its declared transition. Work only a person outside the board can do is '
                      'the Director''s operator:<name> blocker', p_ticket, coalesce(label, t.state), coalesce(label, t.state));
    END IF;
    RETURN format('%s is in %s, and work does not wait on the User from there. Finish %s through its declared '
                  'transition, with the User''s UAT steps on the ticket: it reaches the User in %s once every gate it '
                  'owes has passed. Work only a person outside the board can do is the Director''s operator:<name> blocker',
                  p_ticket, coalesce(label, t.state), coalesce(label, t.state), uat);
END;
$$;

-- The Director's wait on the User: the reason and the action or decision asked
-- of the User, recorded as a comment and with the wait, in one transaction.
CREATE OR REPLACE FUNCTION ticket_board.await_user(p_ticket text, p_reason text, p_request text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    who text;
    t ticket_board.tickets;
    refusal text;
    target text;
    reason text := btrim(coalesce(p_reason, ''));
    request text := btrim(coalesce(p_request, ''));
BEGIN
    -- The list is set_awaiting_role's legacy one; who may wait on the User is
    -- user_wait_refusal's question, the Director alone.
    PERFORM ticket_board.require_actor(
        ARRAY['director', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'],
        'set_awaiting_role'
    );
    who := ticket_board.current_app_actor();
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    refusal := ticket_board.user_wait_refusal(t.state, who);
    IF refusal IS NOT NULL THEN
        RAISE EXCEPTION 'await-user refused for %: %', t.id, refusal USING ERRCODE = '42501';
    END IF;
    IF reason = '' OR request = '' THEN
        RAISE EXCEPTION 'await-user needs a reason and the action or decision the User is asked for';
    END IF;
    IF length(reason) > 4000 OR length(request) > 4000 THEN
        RAISE EXCEPTION 'await-user takes 4000 characters at most for its reason and its request';
    END IF;
    SELECT r.name INTO target FROM ticket_board.workflow_roles r
     WHERE ticket_board.wait_target_kind(r.name) = 'external' ORDER BY r.name <> 'user', r.name LIMIT 1;
    PERFORM ticket_board.add_comment(t.id, format(E'Waiting on the User. Asked of the User: %s\n\nWhy: %s', request, reason));
    INSERT INTO ticket_board.ticket_user_wait_requests (ticket_id, role, since_at, requested_by, reason, request, pending_xact)
    VALUES (t.id, target, NULL, who, reason, request, txid_current())
    ON CONFLICT (ticket_id) DO UPDATE SET role = EXCLUDED.role, since_at = NULL, requested_by = EXCLUDED.requested_by,
        reason = EXCLUDED.reason, request = EXCLUDED.request, requested_at = clock_timestamp(),
        pending_xact = EXCLUDED.pending_xact;
    PERFORM ticket_board.set_awaiting_role(t.id, target);
    UPDATE ticket_board.ticket_user_wait_requests r SET since_at = ns.awaiting_since_at, pending_xact = NULL
      FROM ticket_board.ticket_notification_state ns WHERE r.ticket_id = t.id AND ns.ticket_id = t.id;
    RETURN ticket_board.user_wait_status(t.id);
END;
$$;

-- Every writer, present and future: a wait naming the User is written only by
-- await_user, for the ticket it is recording. A wait already standing is left
-- as it is, so an older record is reported (user_wait_status), never rewritten.
CREATE OR REPLACE FUNCTION ticket_board.guard_user_wait()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF coalesce(NEW.awaiting_role, '') <> ''
       AND (TG_OP = 'INSERT' OR NEW.awaiting_role IS DISTINCT FROM OLD.awaiting_role
            OR NEW.awaiting_since_at IS DISTINCT FROM OLD.awaiting_since_at)
       AND ticket_board.wait_target_kind(NEW.awaiting_role) = 'external'
       AND NOT ticket_board.user_wait_pending(NEW.ticket_id) THEN
        RAISE EXCEPTION 'only the Director''s await-user records a wait on the User; % was not changed', NEW.ticket_id
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS ticket_notification_state_user_wait_guard ON ticket_board.ticket_notification_state;
CREATE TRIGGER ticket_notification_state_user_wait_guard
    BEFORE INSERT OR UPDATE OF awaiting_role, awaiting_since_at ON ticket_board.ticket_notification_state
    FOR EACH ROW EXECUTE FUNCTION ticket_board.guard_user_wait();

-- A ticket's wait on the User for its JSON, NULL when it waits on nobody of the
-- kind. Valid only as the Director's current request in a stage that still
-- admits one; otherwise it says what is wrong and the ordinary repair, and the
-- board moves nothing itself.
CREATE OR REPLACE FUNCTION ticket_board.user_wait_status(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    ns ticket_board.ticket_notification_state;
    t ticket_board.tickets;
    r ticket_board.ticket_user_wait_requests;
    label text;
    problem text;
BEGIN
    SELECT * INTO ns FROM ticket_board.ticket_notification_state WHERE ticket_id = p_ticket;
    IF coalesce(ns.awaiting_role, '') = '' OR ticket_board.wait_target_kind(ns.awaiting_role) IS DISTINCT FROM 'external' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    SELECT * INTO r FROM ticket_board.ticket_user_wait_requests
     WHERE ticket_id = p_ticket AND role = ns.awaiting_role AND since_at IS NOT DISTINCT FROM ns.awaiting_since_at;
    SELECT coalesce(x->>'label', x->>'name') INTO label FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
     WHERE x->>'name' = t.state;
    problem := CASE WHEN r.ticket_id IS NULL
                    THEN 'it was recorded without the Director''s await-user, the only way work waits on the User'
                    WHEN NOT ticket_board.workflow_user_wait_stage(t.state)
                    THEN format('%s does not let the Director wait on the User', coalesce(label, t.state)) END;
    RETURN jsonb_build_object(
        'role', ns.awaiting_role, 'since', ticket_board.utc_text(ns.awaiting_since_at), 'valid', problem IS NULL,
        'requested_by', r.requested_by, 'request', r.request, 'reason', r.reason, 'problem', problem,
        'repair', CASE WHEN problem IS NOT NULL THEN format(
            'the Director clears it (ticket-board-write clear-awaiting-role %s), or %s finishes %s through its declared '
            'transition and every gate after it', p_ticket, t.assignee, coalesce(label, t.state)) END);
END;
$$;

-- The review sign-offs a ticket owes before it enters p_stage, a User Review,
-- and does not have. Owed are the reviews on the way there from
-- Implementation: the stages work can reach from Implementation without
-- passing p_stage (by an approval, a move or a gate skip), that also lead into
-- p_stage. The way may pass any stage that holds live work -- a Director's
-- hold before User Review, the Publisher's -- but not a parking stage (the
-- shape deferral parks work in), so a parked branch, the reviews before
-- Implementation and the ones only after User Review owe nothing. A terminal
-- stage leads nowhere: its only exit is a reopen. Each is owed when it has a
-- sign-off and its gate is on (or it has no gate). The Publisher's stage on
-- the way is owed as the publication of the ticket's commit (SYRD-625).
CREATE OR REPLACE FUNCTION ticket_board.uat_missing_gates(cfg jsonb, doc jsonb, p_stage text)
RETURNS text[] LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE onward text[]; reached text[] := ARRAY[p_stage]; grown boolean := true; missing text[] := ARRAY[]::text[];
    s jsonb; publisher boolean := ticket_board.publisher_declared(cfg);
BEGIN
    SELECT coalesce(array_agg(x->>'name'), ARRAY[]::text[]) INTO onward
      FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind' = 'implementation';
    WHILE grown LOOP
        grown := false;
        FOR s IN SELECT x FROM jsonb_array_elements(cfg->'stages') x
                 WHERE x->>'name' <> p_stage AND NOT (x->>'name' = ANY(onward))
                   AND NOT ticket_board.declared_parking_stage(x->>'name')
                   AND (EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') f
                                WHERE f->>'name' = ANY(onward) AND f->>'skip_to' = x->>'name')
                        OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
                                   WHERE t->>'from' = ANY(onward) AND t->>'to' = x->>'name'
                                     AND t->>'primitive' IN ('approve', 'move')))
        LOOP
            onward := onward || (s->>'name');
            grown := true;
        END LOOP;
    END LOOP;
    grown := true;
    WHILE grown LOOP
        grown := false;
        FOR s IN SELECT x FROM jsonb_array_elements(cfg->'stages') x
                 WHERE x->>'name' = ANY(onward) AND x->>'kind' <> 'implementation'
                   AND NOT (x->>'name' = ANY(reached))
                   AND (x->>'skip_to' = ANY(reached)
                        OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
                                   WHERE t->>'from' = x->>'name' AND t->>'to' = ANY(reached)
                                     AND t->>'primitive' IN ('approve', 'move')))
        LOOP
            reached := reached || (s->>'name');
            grown := true;
            IF s->>'signoff' IS NOT NULL
               AND (s->>'gate' IS NULL OR ticket_board.workflow_flag(doc, s->>'gate', cfg))
               AND NOT ticket_board.workflow_flag(doc, s->>'signoff', cfg) THEN
                missing := missing || (s->>'signoff');
            END IF;
        END LOOP;
    END LOOP;
    IF publisher AND 'publication' = ANY(reached) AND NOT EXISTS (
        SELECT FROM ticket_board.publication_attempts a
         WHERE a.ticket_id = doc->>'id' AND a.state = 'published' AND a.candidate = coalesce(doc->>'commit_hash', '')) THEN
        missing := missing || 'publication'::text;
    END IF;
    RETURN (SELECT coalesce(array_agg(m ORDER BY m), ARRAY[]::text[]) FROM unnest(missing) m);
END;
$$;

-- Every way into User Review, the Director's override included: only once
-- every gate the ticket owes has passed for the commit it carries. A sign-off
-- follows its commit (SYRD-271), so Audit's is for this one.
CREATE OR REPLACE FUNCTION ticket_board.require_uat_gates()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); missing text[];
BEGIN
    IF cfg IS NOT NULL AND OLD.state IS DISTINCT FROM NEW.state AND ticket_board.uat_stage(NEW.state) THEN
        missing := ticket_board.uat_missing_gates(cfg, to_jsonb(NEW), NEW.state);
        IF cardinality(missing) > 0 THEN
            RAISE EXCEPTION '% cannot enter %: it does not have %, and it reaches the User only after every gate it owes',
                NEW.id, NEW.state, array_to_string(missing, ', ') USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tickets_zzzzzz_uat_gates_passed ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_uat_gates_passed BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.require_uat_gates();

REVOKE ALL ON ticket_board.ticket_user_wait_requests FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.user_wait_pending(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.workflow_user_wait_stage(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.user_wait_refusal(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.user_wait_guidance(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.await_user(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.guard_user_wait() FROM PUBLIC;
-- user_wait_status is closed and granted in rbac.sql, like ticket_uat: the
-- ticket read only probes that it exists.
REVOKE ALL ON FUNCTION ticket_board.uat_missing_gates(jsonb, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.require_uat_gates() FROM PUBLIC;

-- The waits on the User already standing that the board no longer admits: the
-- Director is told once each, and nothing is moved or cleared.
DO $$
DECLARE rec record; status jsonb;
BEGIN
    FOR rec IN SELECT t.id, t.title, t.state, t.assignee
                 FROM ticket_board.ticket_notification_state ns
                 JOIN ticket_board.tickets t ON t.id = ns.ticket_id
                WHERE coalesce(ns.awaiting_role, '') <> ''
                  AND ticket_board.wait_target_kind(ns.awaiting_role) = 'external'
                ORDER BY t.id LOOP
        status := ticket_board.user_wait_status(rec.id);
        IF NOT coalesce((status->>'valid')::boolean, true) THEN
            PERFORM ticket_board.enqueue_notification(
                rec.id, 'ticket_update', 'director',
                format('%s -- %s waits on the User from %s (%s), which the board no longer admits: %s. Nothing was '
                       'moved or cleared. Repair: %s.', rec.id, rec.title, rec.state, rec.assignee,
                       status->>'problem', status->>'repair'),
                jsonb_build_object('kind', 'invalid_user_wait', 'id', rec.id, 'state', rec.state,
                                   'assignee', rec.assignee, 'target_role', 'director',
                                   'awaiting_role', status->>'role', 'since', status->>'since'),
                format('invalid_user_wait:%s:%s', rec.id, status->>'since'));
        END IF;
    END LOOP;
END $$;

COMMIT;
