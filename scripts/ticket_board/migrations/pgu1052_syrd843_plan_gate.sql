-- SYRD-843 (pgu1052): the plan gate's records -- the approved plan's index, each ticket's deliverable, scope changes,
-- the milestone-complete notice, plan-file findings -- and the refusals that make the plan the board's.
--
-- The plan gate (scripts/ticket_board/plan_gate.py) is a declared workflow composer that adds no stage: a plan ticket
-- (the `plan_change` gate) goes through the ordinary path. Everything below applies only on a board whose declared
-- workflow has the `plan` policy; every other board is unchanged.
--
-- - plan_indexes: the plan's index, keyed by the commit it was read at. The board service reads it from git
--   (plan_candidate.py) and records it here (record_plan_index); the database cannot read git. Content-addressed, so
--   recording an index early is never wrong.
-- - plan_approvals: a plan ticket closed on a commit makes that commit's index the approved plan. A plan ticket is
--   closed only with the User's acceptance and an index the board has read at its closing commit.
-- - ticket_deliverables: the deliverable of the approved plan a ticket serves, or `unplanned` with a reason
--   (set_ticket_deliverable, the Director's). A ticket enters Implementation from triage only with one or the other,
--   a deliverable that is in the approved plan, and of a milestone that is detailed; plan tickets need neither.
-- - request_scope_change: the Director's way to change scope without editing the plan. It opens a plan ticket for the
--   plan owner (or extends the open one) and makes the waiting ticket blocked on it.
-- - The milestone-complete notice: when every deliverable of a milestone has tickets and all are Done or cancelled,
--   the plan owner and the Director are told once that a re-plan is due.
-- - note_plan_finding: a non-plan ticket's candidate that changes the plan file, or any candidate whose plan file is
--   over the size limit, is recorded once per commit, as a comment for Audit and a notice for the Director.
-- - The gate is not turned off while a plan ticket is open.
-- - plan_status(): the plan as the board sees it, for the board UI and the CLI.
--
-- schema.sql carries the same objects; rbac.sql grants the service its writes and reads.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.plan_indexes (
    commit_hash text PRIMARY KEY CHECK (commit_hash ~ '^[0-9a-f]{40}$'),
    plan_file text NOT NULL,
    index jsonb NOT NULL CHECK (jsonb_typeof(index -> 'milestones') = 'array'),
    plan_lines integer NOT NULL CHECK (plan_lines >= 0),
    read_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS ticket_board.plan_approvals (
    id bigserial PRIMARY KEY,
    commit_hash text NOT NULL REFERENCES ticket_board.plan_indexes(commit_hash),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE SET NULL,
    approved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_deliverables (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    deliverable text,
    unplanned_reason text,
    set_by text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((deliverable IS NULL) <> (unplanned_reason IS NULL)),
    CHECK (unplanned_reason IS NULL OR (btrim(unplanned_reason) <> '' AND length(unplanned_reason) <= 2000))
);

CREATE TABLE IF NOT EXISTS ticket_board.plan_milestone_notices (
    milestone text PRIMARY KEY,
    plan_commit text NOT NULL,
    anchor_ticket text,
    noticed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS ticket_board.plan_findings (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    commit_hash text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('plan_file_touched', 'plan_size')),
    detail text NOT NULL,
    noticed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ticket_id, commit_hash, kind)
);

-- Whether this board runs the plan gate, and its policy.
CREATE OR REPLACE FUNCTION ticket_board.plan_policy()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN jsonb_typeof(ticket_board.declared_workflow() -> 'plan') = 'object'
                THEN ticket_board.declared_workflow() -> 'plan' END;
$$;

-- The terminal stage a ticket is finished in: the one a commit-carrying move closes into (mark_done's), never the
-- one work is cancelled into.
CREATE OR REPLACE FUNCTION ticket_board.plan_finished_stage()
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT x->>'to' FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
      JOIN jsonb_array_elements(ticket_board.declared_workflow()->'stages') s ON s->>'name' = x->>'to'
     WHERE (s->>'terminal')::boolean AND (x->>'require_commit')::boolean
     ORDER BY x->>'to' LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.plan_terminal_stage(p_stage text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((SELECT (s->>'terminal')::boolean FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s
                      WHERE s->>'name' = p_stage), p_stage IN ('done', 'cancelled'));
$$;

-- The approved plan: the index of the commit the last plan ticket was closed on, or NULL before one was.
CREATE OR REPLACE FUNCTION ticket_board.approved_plan()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object('commit', a.commit_hash, 'ticket', a.ticket_id, 'approved_at', ticket_board.utc_text(a.approved_at),
                              'plan_file', i.plan_file, 'plan_lines', i.plan_lines, 'index', i.index)
      FROM ticket_board.plan_approvals a JOIN ticket_board.plan_indexes i USING (commit_hash)
     ORDER BY a.id DESC LIMIT 1;
$$;

-- The approved plan's milestone a deliverable belongs to -- {"id", "title", "detailed", "deliverables"} -- or NULL.
CREATE OR REPLACE FUNCTION ticket_board.plan_deliverable_milestone(p_deliverable text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT m FROM jsonb_array_elements(ticket_board.approved_plan()->'index'->'milestones') m
     WHERE EXISTS (SELECT FROM jsonb_array_elements(m->'deliverables') d WHERE d->>'id' = p_deliverable) LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION ticket_board.plan_ticket(p_ticket ticket_board.tickets)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT ticket_board.workflow_flag(to_jsonb(p_ticket), 'plan_change', ticket_board.declared_workflow());
$$;

-- The service records an index it read from git at a commit. Content-addressed: a second recording changes nothing.
CREATE OR REPLACE FUNCTION ticket_board.record_plan_index(p_commit text, p_plan_file text, p_index jsonb, p_lines integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'only the board service records a plan index' USING ERRCODE = '42501';
    END IF;
    INSERT INTO ticket_board.plan_indexes (commit_hash, plan_file, index, plan_lines)
    VALUES (lower(p_commit), p_plan_file, p_index, p_lines) ON CONFLICT (commit_hash) DO NOTHING;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_plan_scope()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE d ticket_board.ticket_deliverables; m jsonb; plan jsonb;
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state OR ticket_board.plan_policy() IS NULL THEN
        RETURN NEW;
    END IF;
    IF ticket_board.declared_stage_kind(NEW.state) = 'implementation'
       AND coalesce(ticket_board.declared_stage_kind(OLD.state), '') NOT IN ('implementation', 'review')
       AND NOT ticket_board.plan_ticket(NEW) THEN
        SELECT * INTO d FROM ticket_board.ticket_deliverables WHERE ticket_id = NEW.id;
        IF NOT FOUND THEN
            RAISE EXCEPTION '% names no deliverable. On this plan-gated board a ticket enters Implementation serving a deliverable of the approved plan (ticket-board-write set-deliverable % --deliverable <id>), or as unplanned with its reason (--unplanned-reason-file PATH); a change of scope is a scope change (request-scope-change)',
                NEW.id, NEW.id;
        END IF;
        IF d.deliverable IS NOT NULL THEN
            m := ticket_board.plan_deliverable_milestone(d.deliverable);
            IF m IS NULL THEN
                RAISE EXCEPTION '% serves deliverable %, which the approved plan no longer has; set its deliverable again, or raise a scope change',
                    NEW.id, d.deliverable;
            END IF;
            IF NOT (m->>'detailed')::boolean THEN
                RAISE EXCEPTION 're-plan due: milestone % (%) is not detailed yet, so % (deliverable %) cannot be routed into Implementation; the plan owner details it through a plan ticket',
                    m->>'id', m->>'title', NEW.id, d.deliverable;
            END IF;
        END IF;
    END IF;
    IF NEW.state = ticket_board.plan_finished_stage() AND ticket_board.plan_ticket(NEW) THEN
        IF NOT NEW.user_signoff THEN
            RAISE EXCEPTION '% is a plan ticket, and the User has not accepted it: the plan is the User''s to approve, in User Review, before it is closed',
                NEW.id;
        END IF;
        IF NOT EXISTS (SELECT FROM ticket_board.plan_indexes WHERE commit_hash = lower(NEW.commit_hash)) THEN
            RAISE EXCEPTION 'the board has not read the plan index at %, the commit % is closed on; nothing was moved',
                coalesce(nullif(NEW.commit_hash, ''), 'no commit'), NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzzzx_plan_scope_required ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzx_plan_scope_required BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.require_plan_scope();

CREATE OR REPLACE FUNCTION ticket_board.note_plan_progress()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE d ticket_board.ticket_deliverables; m jsonb; plan jsonb; approved jsonb; next jsonb; message text;
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state OR ticket_board.plan_policy() IS NULL THEN
        RETURN NULL;
    END IF;
    IF NEW.state = ticket_board.plan_finished_stage() AND ticket_board.plan_ticket(NEW) THEN
        INSERT INTO ticket_board.plan_approvals (commit_hash, ticket_id) VALUES (lower(NEW.commit_hash), NEW.id);
    END IF;
    IF NOT ticket_board.plan_terminal_stage(NEW.state) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO d FROM ticket_board.ticket_deliverables WHERE ticket_id = NEW.id AND deliverable IS NOT NULL;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    m := ticket_board.plan_deliverable_milestone(d.deliverable);
    IF m IS NULL OR EXISTS (SELECT FROM ticket_board.plan_milestone_notices WHERE milestone = m->>'id') THEN
        RETURN NULL;
    END IF;
    -- Complete: every deliverable of the milestone has tickets, and every one of them is finished or cancelled.
    IF EXISTS (SELECT FROM jsonb_array_elements(m->'deliverables') x
                WHERE NOT EXISTS (SELECT FROM ticket_board.ticket_deliverables td WHERE td.deliverable = x->>'id'))
       OR EXISTS (SELECT FROM ticket_board.ticket_deliverables td JOIN ticket_board.tickets t ON t.id = td.ticket_id
                   WHERE td.deliverable IN (SELECT x->>'id' FROM jsonb_array_elements(m->'deliverables') x)
                     AND NOT ticket_board.plan_terminal_stage(t.state)) THEN
        RETURN NULL;
    END IF;
    plan := ticket_board.plan_policy();
    approved := ticket_board.approved_plan();
    SELECT n INTO next FROM jsonb_array_elements(approved->'index'->'milestones') WITH ORDINALITY AS a(n, pos)
     WHERE pos > (SELECT pos FROM jsonb_array_elements(approved->'index'->'milestones') WITH ORDINALITY AS b(n, pos)
                   WHERE n->>'id' = m->>'id') ORDER BY pos LIMIT 1;
    INSERT INTO ticket_board.plan_milestone_notices (milestone, plan_commit, anchor_ticket)
    VALUES (m->>'id', approved->>'commit', NEW.id);
    message := format('Milestone %s (%s) is complete: every ticket of its deliverables is Done or cancelled. A re-plan is due: %s',
                      m->>'id', m->>'title',
                      CASE WHEN next IS NULL THEN 'the plan has no next milestone; the plan owner plans what follows through a plan ticket.'
                           WHEN (next->>'detailed')::boolean THEN format('%s (%s) is detailed; review the plan at this boundary through a plan ticket.', next->>'id', next->>'title')
                           ELSE format('%s (%s) is not detailed yet, and its tickets are refused Implementation until the plan owner details it through a plan ticket.', next->>'id', next->>'title') END);
    PERFORM ticket_board.enqueue_notification(NEW.id, 'ticket_update', role, message,
                jsonb_build_object('kind', 'plan_milestone_complete', 'id', NEW.id, 'milestone', m->>'id', 'target_role', role),
                'plan_milestone_complete:' || (m->>'id') || ':' || role)
       FROM unnest(ARRAY['director', plan->>'owner']) AS role;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zz_plan_progress ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_plan_progress AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.note_plan_progress();

-- The Director names the deliverable a ticket serves, or that it is unplanned and why.
CREATE OR REPLACE FUNCTION ticket_board.set_ticket_deliverable(p_ticket text, p_deliverable text, p_unplanned_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE who text; ticket ticket_board.tickets; deliverable text := nullif(btrim(coalesce(p_deliverable, '')), '');
    reason text := nullif(btrim(coalesce(p_unplanned_reason, '')), ''); approved jsonb; m jsonb; saved ticket_board.ticket_deliverables;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    IF ticket_board.plan_policy() IS NULL THEN
        RAISE EXCEPTION 'this board runs no plan gate; the Director turns it on with: switchyard plan-gate <project> enable';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF ticket_board.plan_terminal_stage(ticket.state) THEN
        RAISE EXCEPTION '% is %: its deliverable no longer changes', p_ticket, ticket.state;
    END IF;
    IF (deliverable IS NULL) = (reason IS NULL) THEN
        RAISE EXCEPTION 'a ticket serves one deliverable of the approved plan, or is unplanned with a reason: give one of the two';
    END IF;
    IF reason IS NOT NULL AND length(reason) > 2000 THEN
        RAISE EXCEPTION 'keep the unplanned reason short (2000 characters at most)';
    END IF;
    IF deliverable IS NOT NULL THEN
        approved := ticket_board.approved_plan();
        IF approved IS NULL THEN
            RAISE EXCEPTION 'no plan has been approved yet, so % has no deliverable to name; approve the plan through a plan ticket, or mark it unplanned with a reason',
                p_ticket;
        END IF;
        m := ticket_board.plan_deliverable_milestone(deliverable);
        IF m IS NULL THEN
            RAISE EXCEPTION '% is not a deliverable of the approved plan (%, commit %); its deliverables are: %. A change of scope is a scope change: ticket-board-write request-scope-change %',
                deliverable, approved->>'plan_file', left(approved->>'commit', 12),
                (SELECT string_agg(x->>'id', ', ') FROM jsonb_array_elements(approved->'index'->'milestones') mm,
                        jsonb_array_elements(mm->'deliverables') x), p_ticket;
        END IF;
    END IF;
    INSERT INTO ticket_board.ticket_deliverables AS td (ticket_id, deliverable, unplanned_reason, set_by)
    VALUES (p_ticket, deliverable, reason, who)
    ON CONFLICT (ticket_id) DO UPDATE SET deliverable = EXCLUDED.deliverable, unplanned_reason = EXCLUDED.unplanned_reason,
        set_by = EXCLUDED.set_by, set_at = clock_timestamp()
    RETURNING * INTO saved;
    PERFORM ticket_board.append_ticket_comment(p_ticket, who,
        CASE WHEN deliverable IS NOT NULL THEN format('Deliverable: %s, of milestone %s (%s).', deliverable, m->>'id', m->>'title')
             ELSE format(E'Unplanned: %s', reason) END);
    RETURN jsonb_build_object('ticket_id', p_ticket, 'deliverable', saved.deliverable, 'unplanned_reason', saved.unplanned_reason,
                              'milestone', m->>'id');
END;
$$;

-- The Director's scope change: a plan ticket for the owner -- the open one, or a new one -- that the waiting ticket is
-- blocked on. The Director never edits the plan.
CREATE OR REPLACE FUNCTION ticket_board.request_scope_change(p_ticket text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE who text; ticket ticket_board.tickets; plan jsonb := ticket_board.plan_policy(); plan_id text; created boolean := false;
    reason text := btrim(coalesce(p_reason, '')); impl text; blockers text[];
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    who := ticket_board.current_app_actor();
    IF plan IS NULL THEN
        RAISE EXCEPTION 'this board runs no plan gate; the Director turns it on with: switchyard plan-gate <project> enable';
    END IF;
    IF reason = '' OR length(reason) > 4000 THEN
        RAISE EXCEPTION 'a scope change says what the plan is missing (4000 characters at most)';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF ticket_board.plan_terminal_stage(ticket.state) OR ticket_board.plan_ticket(ticket) THEN
        RAISE EXCEPTION '% is % a ticket that waits on the plan; a scope change is for open work the plan does not cover',
            p_ticket, CASE WHEN ticket_board.plan_ticket(ticket) THEN 'a plan ticket, not' ELSE 'closed, not' END;
    END IF;
    SELECT t.id INTO plan_id FROM ticket_board.tickets t
     WHERE ticket_board.plan_ticket(t) AND NOT ticket_board.plan_terminal_stage(t.state)
     ORDER BY t.ticket_number DESC LIMIT 1;
    IF plan_id IS NULL THEN
        plan_id := ticket_board.create_ticket(
            format('Plan: scope change for %s', p_ticket),
            format(E'Scope change requested by the Director for %s (%s).\n\n%s\n\nEdit the plan (%s) and, where a ruling belongs on record, the decision log (%s); nothing else. Audit reviews the whole plan, the User approves it.',
                   p_ticket, ticket.title, reason, plan->>'file', plan->>'decision_log'),
            'analysis', 'director', ARRAY[]::text[], '', true, true);
        UPDATE ticket_board.tickets SET workflow_flags = coalesce(workflow_flags, '{}'::jsonb) || '{"plan_change": true}'::jsonb
         WHERE id = plan_id;
        -- The Director's own declared move from where the new ticket waits into Implementation, to the plan owner.
        SELECT x->>'action' INTO impl FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
          JOIN jsonb_array_elements(ticket_board.declared_workflow()->'stages') s ON s->>'name' = x->>'to'
         WHERE x->>'from' = (SELECT state FROM ticket_board.tickets WHERE id = plan_id) AND s->>'kind' = 'implementation'
           AND x->>'primitive' = 'move' AND x->'actors' ? 'director' ORDER BY x->>'action' LIMIT 1;
        IF impl IS NULL THEN
            RAISE EXCEPTION 'this workflow has no Director move from triage into Implementation for the plan ticket';
        END IF;
        PERFORM ticket_board.perform_workflow_action(plan_id, impl, jsonb_build_object('assignee', plan->>'owner'));
        created := true;
    ELSE
        PERFORM ticket_board.append_ticket_comment(plan_id, who,
            format(E'Scope change requested by the Director for %s (%s).\n\n%s', p_ticket, ticket.title, reason));
    END IF;
    SELECT coalesce(array_agg(blocker_ticket_id ORDER BY position), ARRAY[]::text[]) INTO blockers
      FROM ticket_board.ticket_blockers WHERE ticket_id = p_ticket;
    IF NOT plan_id = ANY(blockers) THEN
        PERFORM ticket_board.set_blockers(p_ticket, blockers || plan_id,
            format('Scope change: waits on plan ticket %s. %s', plan_id, left(reason, 300)));
    END IF;
    RETURN jsonb_build_object('ticket_id', p_ticket, 'plan_ticket', plan_id, 'created', created);
END;
$$;

-- A finding about a candidate, once per ticket, commit and kind: a comment for Audit, a notice for the Director.
CREATE OR REPLACE FUNCTION ticket_board.note_plan_finding(p_ticket text, p_commit text, p_kind text, p_detail text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE added integer;
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'only the board service records a plan finding' USING ERRCODE = '42501';
    END IF;
    INSERT INTO ticket_board.plan_findings (ticket_id, commit_hash, kind, detail)
    VALUES (p_ticket, lower(p_commit), p_kind, p_detail) ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS added = ROW_COUNT;
    IF added = 0 THEN
        RETURN false;
    END IF;
    PERFORM ticket_board.append_ticket_comment(p_ticket, 'ticket_board_service', format('Plan finding: %s', p_detail));
    PERFORM ticket_board.enqueue_notification(p_ticket, 'ticket_update', 'director',
        format('%s -- %s: %s', p_ticket, (SELECT title FROM ticket_board.tickets WHERE id = p_ticket), p_detail),
        jsonb_build_object('kind', 'plan_finding', 'id', p_ticket, 'finding', p_kind, 'target_role', 'director'),
        'plan_finding:' || p_ticket || ':' || lower(p_commit) || ':' || p_kind);
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.keep_plan_gate_while_planning()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE open text;
BEGIN
    IF jsonb_typeof(OLD.document -> 'plan') = 'object' AND jsonb_typeof(NEW.document -> 'plan') IS DISTINCT FROM 'object' THEN
        SELECT string_agg(t.id, ', ' ORDER BY t.id) INTO open FROM ticket_board.tickets t
         WHERE ticket_board.workflow_flag(to_jsonb(t), 'plan_change', OLD.document)
           AND NOT coalesce((SELECT (s->>'terminal')::boolean FROM jsonb_array_elements(OLD.document->'stages') s
                              WHERE s->>'name' = t.state), false);
        IF open IS NOT NULL THEN
            RAISE EXCEPTION 'the plan gate cannot be turned off while a plan ticket is open: %; close or cancel it first', open;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS workflow_configuration_keep_plan_gate ON ticket_board.workflow_configuration;
CREATE TRIGGER workflow_configuration_keep_plan_gate BEFORE UPDATE ON ticket_board.workflow_configuration
    FOR EACH ROW EXECUTE FUNCTION ticket_board.keep_plan_gate_while_planning();

-- The plan as the board sees it: the approved commit, each milestone's deliverables with their tickets counted by
-- state, the unplanned tickets counted apart, and the milestones whose re-plan is due.
CREATE OR REPLACE FUNCTION ticket_board.plan_status()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT jsonb_build_object(
        'policy', ticket_board.plan_policy(),
        'approved', (SELECT a - 'index' FROM ticket_board.approved_plan() a),
        'milestones', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'id', m->>'id', 'title', m->>'title', 'detailed', (m->>'detailed')::boolean,
                'replan_due', EXISTS (SELECT FROM ticket_board.plan_milestone_notices n WHERE n.milestone = m->>'id'),
                'deliverables', coalesce((
                    SELECT jsonb_agg(jsonb_build_object('id', x->>'id', 'title', x->>'title', 'tickets', coalesce((
                        SELECT jsonb_object_agg(state, n) FROM (
                            SELECT t.state, count(*) AS n FROM ticket_board.ticket_deliverables td
                              JOIN ticket_board.tickets t ON t.id = td.ticket_id
                             WHERE td.deliverable = x->>'id' GROUP BY t.state) c), '{}'::jsonb)) ORDER BY xp)
                      FROM jsonb_array_elements(m->'deliverables') WITH ORDINALITY AS dx(x, xp)), '[]'::jsonb)) ORDER BY mp)
              FROM jsonb_array_elements(ticket_board.approved_plan()->'index'->'milestones') WITH ORDINALITY AS mx(m, mp)),
            '[]'::jsonb),
        'unplanned', coalesce((SELECT jsonb_object_agg(state, n) FROM (
            SELECT t.state, count(*) AS n FROM ticket_board.ticket_deliverables td JOIN ticket_board.tickets t ON t.id = td.ticket_id
             WHERE td.unplanned_reason IS NOT NULL GROUP BY t.state) u), '{}'::jsonb));
$$;

REVOKE ALL ON ticket_board.plan_indexes, ticket_board.plan_approvals, ticket_board.ticket_deliverables,
    ticket_board.plan_milestone_notices, ticket_board.plan_findings FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_plan_scope() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_plan_progress() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.keep_plan_gate_while_planning() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.record_plan_index(text, text, jsonb, integer) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.set_ticket_deliverable(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.request_scope_change(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_plan_finding(text, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.plan_status() FROM PUBLIC;

COMMIT;
