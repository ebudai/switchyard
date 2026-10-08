-- SYRD-568: an implementer's assigned Implementation work is a visible queue.
--
-- A route of a second ticket to a busy implementer used to be diverted into
-- the holding destination (analysis/director on syrd) by the declared update
-- trigger -- and force_move and director_edit diverted it too -- so the
-- Director re-routed it later, by hand, and parked whole cohorts in manually
-- controlled Analysis to keep their reminders quiet.
--
-- Now the ticket lands where it was routed, assigned. Only one ticket per
-- implementer holds the serial slot: ticket_current_reserved_ticket skips a
-- ticket with an unresolved blocker, puts review and lifecycle holds first as
-- before, keeps the slot with the ticket already active (serial_focus), and
-- otherwise takes the lowest ticket number.
-- settle_serial_focus records the slot after
-- every change that can move it and hands over a newly active ticket once.
-- A waiting ticket (ticket_serial_waiting) gets no handoff, no highlight, no
-- reminder, and cannot be submitted. serial_queue() publishes the order.
--
-- Existing queued_for_assignee markers are left where they are: they still
-- silence reminders while their reservation holds and still wake the Director
-- when it ends, and the Director's ordinary route now succeeds without an
-- override. Legacy boards (no declared workflow) are unchanged.
--
-- schema.sql ends with this same body.
BEGIN;

-- SYRD-568: assigned implementation queues

CREATE TABLE IF NOT EXISTS ticket_board.serial_focus (
    -- SYRD-568: the ticket that holds each implementer's one slot, recorded
    -- when it took the slot. One row per implementer, so two tickets can never
    -- both be active for one.
    implementer text PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

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
 --
 -- SYRD-568: an implementer may have several tickets assigned in its
 -- implementation stage, and only one of them holds the slot. A ticket with an
 -- unresolved blocker holds nothing. Work past implementation that holds its
 -- author (review, or lifecycle after a reopen) comes first, as it always
 -- did; among implementation work the ticket already active
 -- (ticket_board.serial_focus) keeps the slot while it still qualifies, so
 -- work routed later, or unblocked later, never takes it from work under way;
 -- otherwise the lowest ticket number, which is the queue's stated order.
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_current_reserved_ticket(p_implementer,p_excluding_ticket_id)
 --
 -- The document is read once and its stages joined, and only tickets that
 -- name the implementer are considered: the waiting check asks this for every
 -- queued ticket, and reading the document again for every ticket on the
 -- board made that seconds (SYRD-568). The conditions are the same.
 ELSE (
 WITH doc AS MATERIALIZED (SELECT ticket_board.declared_workflow() AS cfg),
 stages AS MATERIALIZED (
   SELECT x->>'name' AS name, x->>'kind' AS kind, coalesce((x->>'terminal')::boolean, false) AS terminal
   FROM doc, jsonb_array_elements(doc.cfg->'stages') x)
 SELECT t.id FROM ticket_board.tickets t
 JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id=t.id
 JOIN stages s ON s.name=t.state
 LEFT JOIN ticket_board.serial_focus f ON f.implementer=p_implementer AND f.ticket_id=t.id
 WHERE (p_excluding_ticket_id IS NULL OR t.id<>p_excluding_ticket_id) AND NOT t.manually_controlled
 AND (t.assignee=p_implementer OR ns.last_implementer_assignee=p_implementer)
 AND (
   (s.kind IN ('implementation','review')
    AND (CASE WHEN s.kind='implementation' THEN t.assignee ELSE ns.last_implementer_assignee END)=p_implementer
    AND NOT (s.kind='review' AND ticket_board.declared_scheduling() IS NOT NULL
             AND EXISTS (SELECT FROM ticket_board.pull_releases r WHERE r.ticket_id=t.id))
    AND NOT (s.kind='implementation' AND ticket_board.ticket_has_unresolved_blockers(t.id)))
   OR (coalesce((SELECT d.cfg->>'reservation' FROM doc d),'review')='lifecycle'
    AND s.kind='system' AND NOT s.terminal AND NOT t.parked
    AND ns.last_implementer_assignee=p_implementer)
 )
 ORDER BY s.kind='implementation', f.ticket_id IS NULL, t.ticket_number LIMIT 1) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_serial_waiting(p_ticket_id text)
RETURNS boolean LANGUAGE sql STABLE AS $$
    -- SYRD-568: assigned to an implementer in an implementation stage, and not
    -- the ticket holding that implementer's slot -- queued behind it, or
    -- blocked. Visible, assigned work that nobody is asked to do yet: it gets
    -- no handoff, no highlight and no reminder, and it cannot be submitted.
    -- Manual control is the Director's own hold and is not a queue.
    SELECT EXISTS (
        SELECT FROM ticket_board.tickets t
        WHERE t.id = p_ticket_id
          AND ticket_board.declared_workflow() IS NOT NULL
          AND ticket_board.declared_stage_kind(t.state) = 'implementation'
          AND ticket_board.ticket_is_implementer_assignee(t.assignee)
          AND NOT t.manually_controlled
          AND ticket_board.ticket_current_reserved_ticket(t.assignee) IS DISTINCT FROM t.id
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.settle_serial_focus(p_implementer text, p_notify boolean DEFAULT true,
                                                            p_moved text DEFAULT NULL)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-568: record which ticket holds an implementer's slot, and hand it over
-- when that changes. Called after every change that can move the slot; the
-- slot itself is ticket_current_reserved_ticket's answer, so this records it
-- and never decides it. An activation is the row written here, once, in the
-- transaction that caused it: a listener restart, or this being called again,
-- finds the row and sends nothing more.
DECLARE
    v_implementer text := btrim(lower(coalesce(p_implementer, '')));
    v_held text;
    v_active text;
    v_ticket ticket_board.tickets%ROWTYPE;
    v_message text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL OR v_implementer = ''
       OR NOT ticket_board.ticket_is_implementer_assignee(v_implementer) THEN
        RETURN NULL;
    END IF;
    PERFORM pg_advisory_xact_lock(ticket_board.implementer_assignment_lock_key(v_implementer));
    v_held := ticket_board.ticket_current_reserved_ticket(v_implementer);
    SELECT f.ticket_id INTO v_active FROM ticket_board.serial_focus f WHERE f.implementer = v_implementer FOR UPDATE;
    -- Nothing can hold the slot (the active ticket finished, or is blocked
    -- with nothing behind it): the record stays. A ticket blocked and then
    -- unblocked with nothing in between is the same work, so it has nothing
    -- new to be handed; its unblock notice says what changed.
    IF v_held IS NULL THEN
        RETURN NULL;
    END IF;
    IF v_active IS NOT DISTINCT FROM v_held THEN
        RETURN v_held;
    END IF;
    INSERT INTO ticket_board.serial_focus (implementer, ticket_id) VALUES (v_implementer, v_held)
    ON CONFLICT (implementer) DO UPDATE SET ticket_id = EXCLUDED.ticket_id, activated_at = clock_timestamp();
    SELECT * INTO v_ticket FROM ticket_board.tickets t WHERE t.id = v_held;
    -- Only a change of hands caused by something else is announced. The
    -- ticket whose own row moved (p_moved) is told by whatever moved it -- a
    -- route's transition notice, a reassignment's, an unblock's, a released
    -- blocker's -- and that runs after this trigger, so it cannot be seen
    -- here. The first record for an implementer (a board that predates the
    -- queue, or declared its workflow later) only writes down what already
    -- holds. Work in review needs no handoff to its author, and a handoff
    -- already queued for this stage is not repeated.
    IF p_notify AND v_active IS NOT NULL AND v_held IS DISTINCT FROM p_moved
       AND ticket_board.declared_stage_kind(v_ticket.state) = 'implementation'
       AND v_ticket.assignee = v_implementer
       AND NOT EXISTS (
           SELECT FROM ticket_board.ticket_notification_queue q
           WHERE q.ticket_id = v_held AND q.kind = 'transition' AND q.target_role = v_implementer
             AND coalesce(q.payload->>'new_state', q.payload->>'state') = v_ticket.state
             AND q.dead_lettered_at IS NULL) THEN
        v_message := ticket_board.transition_message(
            v_ticket.id, v_ticket.title, NULL, v_ticket.state,
            ticket_board.ticket_state_already_announced(v_ticket.id, v_implementer, v_ticket.state))
            || ': it was waiting in your queue and is now your active ticket';
        PERFORM ticket_board.enqueue_notification(
            v_ticket.id, 'transition', v_implementer, v_message,
            jsonb_build_object(
                'kind', 'transition', 'id', v_ticket.id, 'title', v_ticket.title,
                'old_state', v_ticket.state, 'new_state', v_ticket.state, 'assignee', v_ticket.assignee,
                'updated_at', v_ticket.updated_at, 'ticket_number', v_ticket.ticket_number,
                'target_role', v_implementer, 'message', v_message, 'serial_activation', true),
            'serial_activation:' || v_ticket.id || ':' || v_implementer || ':' || pg_current_xact_id()::text);
    END IF;
    RETURN v_held;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.settle_serial_focus_for_ticket()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-568: a ticket's stage, owner, manual control or parking moved, so the
-- slot of every implementer it can hold may have moved with it.
DECLARE v_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN RETURN NULL; END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.state IS NOT DISTINCT FROM NEW.state AND OLD.assignee IS NOT DISTINCT FROM NEW.assignee
       AND OLD.manually_controlled IS NOT DISTINCT FROM NEW.manually_controlled
       AND OLD.parked IS NOT DISTINCT FROM NEW.parked THEN
        RETURN NULL;
    END IF;
    FOR v_role IN
        SELECT DISTINCT r FROM unnest(ARRAY[
            CASE WHEN TG_OP = 'UPDATE' THEN OLD.assignee END, NEW.assignee,
            (SELECT ns.last_implementer_assignee FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = NEW.id)]) r
        WHERE coalesce(r, '') <> '' ORDER BY r
    LOOP
        PERFORM ticket_board.settle_serial_focus(v_role, true, NEW.id);
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.settle_serial_focus_for_blocker()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-568: a blocker was added, removed or resolved, so the ticket it holds
-- back may have started, or stopped, being able to hold its implementer.
DECLARE v_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN RETURN NULL; END IF;
    FOR v_role IN
        SELECT DISTINCT r
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id,
             unnest(ARRAY[t.assignee, ns.last_implementer_assignee]) r
        WHERE t.id = CASE WHEN TG_OP = 'DELETE' THEN OLD.ticket_id ELSE NEW.ticket_id END
          AND coalesce(r, '') <> ''
        ORDER BY r
    LOOP
        PERFORM ticket_board.settle_serial_focus(v_role, true, CASE WHEN TG_OP = 'DELETE' THEN OLD.ticket_id ELSE NEW.ticket_id END);
    END LOOP;
    RETURN NULL;
END;
$$;

-- Named to run after every other AFTER trigger on the table, so the slot is
-- settled against the move's final state and after the handoff it queued.
DROP TRIGGER IF EXISTS tickets_zzzzzzz_settle_serial_focus ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzzz_settle_serial_focus
AFTER INSERT OR UPDATE ON ticket_board.tickets
FOR EACH ROW EXECUTE FUNCTION ticket_board.settle_serial_focus_for_ticket();
DROP TRIGGER IF EXISTS ticket_blockers_zz_settle_serial_focus ON ticket_board.ticket_blockers;
CREATE TRIGGER ticket_blockers_zz_settle_serial_focus
AFTER INSERT OR UPDATE OR DELETE ON ticket_board.ticket_blockers
FOR EACH ROW EXECUTE FUNCTION ticket_board.settle_serial_focus_for_blocker();

CREATE OR REPLACE FUNCTION ticket_board.settle_serial_focus_for_workflow()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-568: a workflow declared, or changed, over tickets already in its
-- stages. Record what holds each slot now, so work already under way keeps it
-- against tickets routed later, and announce nothing: the document changed,
-- not who has the work. apply_declared_workflow projects the stages before it
-- writes this row, so they are current here.
DECLARE v_role text;
BEGIN
    FOR v_role IN
        SELECT DISTINCT btrim(lower(role)) FROM ticket_board.workflow_stages ws, unnest(ws.owner_roles) AS role ORDER BY 1
    LOOP
        PERFORM ticket_board.settle_serial_focus(v_role, false);
    END LOOP;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS workflow_configuration_zz_settle_serial_focus ON ticket_board.workflow_configuration;
CREATE TRIGGER workflow_configuration_zz_settle_serial_focus
AFTER INSERT OR UPDATE ON ticket_board.workflow_configuration
FOR EACH STATEMENT EXECUTE FUNCTION ticket_board.settle_serial_focus_for_workflow();

CREATE OR REPLACE FUNCTION ticket_board.serial_queue()
RETURNS TABLE(implementer text, ticket_id text, state text, queue_position integer, active boolean,
              waiting_on text[], active_ticket text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    -- SYRD-568: each implementer's assigned implementation work, in the order
    -- it will be taken: the active ticket first; then the tickets that can
    -- start, lowest number first, which is the order settle_serial_focus
    -- activates them in; then the blocked ones, with what they wait on. Work
    -- already past implementation that still holds its author is listed as
    -- that author's active ticket. Read-only, and empty on a board without a
    -- declared workflow.
    -- Each implementer's slot is asked for once (MATERIALIZED: inlined, it was
    -- asked again for every ticket the join below looks at, which took the
    -- board snapshot from milliseconds to seconds), and so are the stage kinds.
    WITH implementation_stages AS MATERIALIZED (
        SELECT x->>'name' AS name FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
        WHERE x->>'kind' = 'implementation'
    ), implementers AS MATERIALIZED (
        SELECT DISTINCT btrim(lower(role)) AS name
        FROM ticket_board.workflow_stages ws, unnest(ws.owner_roles) AS role
        WHERE ticket_board.declared_workflow() IS NOT NULL
          AND ticket_board.ticket_is_implementer_assignee(btrim(lower(role)))
    ), held AS MATERIALIZED (
        SELECT i.name, ticket_board.ticket_current_reserved_ticket(i.name) AS ticket FROM implementers i
    ), queued AS (
        SELECT h.name, h.ticket, t.id, t.state, t.ticket_number, t.id IS NOT DISTINCT FROM h.ticket AS active,
               coalesce((SELECT array_agg(b.blocker_ticket_id ORDER BY b.position)
                         FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id AND NOT b.resolved),
                        ARRAY[]::text[]) AS waiting_on
        FROM held h
        JOIN ticket_board.tickets t
          ON t.id = h.ticket
          OR (t.assignee = h.name AND NOT t.manually_controlled
              AND t.state IN (SELECT s.name FROM implementation_stages s))
    )
    SELECT q.name, q.id, q.state,
           (row_number() OVER (PARTITION BY q.name
                               ORDER BY q.active DESC, cardinality(q.waiting_on) > 0, q.ticket_number))::integer,
           q.active, q.waiting_on, coalesce(q.ticket, '')
    FROM queued q
    ORDER BY q.name, 4;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_state_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_state text;
    current_assignee text;
    message text;
    recommendation text;
BEGIN
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF current_setting('ticket_board.suppress_transition_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;

    IF TG_OP = 'UPDATE' THEN
        IF OLD.state IS NOT DISTINCT FROM NEW.state THEN
            RETURN NULL;
        END IF;
        -- Manual control is a deliberate silence: no nudges, no reminders, no
        -- announcement of every edit while the Director steers a ticket by
        -- hand. It was also silencing the one message that is not a reminder.
        -- An actionable transition that moves work to a DIFFERENT owner is a
        -- handoff, and the new owner is the one person who cannot be expected
        -- to find out any other way -- on the live case, Audit sat idle while
        -- the work waited for it, and a Director had to hand-deliver a comment
        -- (SYRD-107).
        --
        -- Narrow on purpose, and every clause is a case that must stay silent:
        -- a destination nobody owns or that notifies nobody has no target at
        -- all, and a move that keeps the same owner is not a handoff.
        IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
            IF ticket_board.transition_target_role(NEW.state, NEW.assignee) IS NULL
               OR ticket_board.transition_target_role(NEW.state, NEW.assignee)
                  IS NOT DISTINCT FROM ticket_board.transition_target_role(OLD.state, OLD.assignee) THEN
                RETURN NULL;
            END IF;
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(NEW.id) THEN
            RETURN NULL;
        END IF;
        -- SYRD-568: a ticket waiting in its implementer's queue is handed over
        -- when it becomes that implementer's active ticket, by
        -- settle_serial_focus, and not before.
        IF ticket_board.ticket_serial_waiting(NEW.id) THEN
            RETURN NULL;
        END IF;
        message := ticket_board.transition_message(
            NEW.id,
            NEW.title,
            OLD.state,
            NEW.state,
            ticket_board.ticket_state_already_announced(
                NEW.id,
                ticket_board.transition_target_role(NEW.state, NEW.assignee),
                NEW.state
            )
        );
        IF OLD.state = 'inspection' AND NEW.state = 'in_progress' THEN
            SELECT c.text
            INTO recommendation
            FROM ticket_board.ticket_comments AS c
            WHERE c.ticket_id = NEW.id
              AND btrim(c.text) <> ''
              AND c.xmin = pg_current_xact_id()::xid
            ORDER BY c.position DESC
            LIMIT 1;
            IF recommendation IS NOT NULL THEN
                message := message || ': ' || recommendation;
            END IF;
        END IF;

        PERFORM ticket_board.enqueue_transition_notification(
            NEW.id,
            NEW.title,
            OLD.state,
            NEW.state,
            NEW.assignee,
            NEW.updated_at,
            NEW.ticket_number,
            message
        );
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF coalesce(NEW.manually_controlled, false) THEN
            RETURN NULL;
        END IF;

        SELECT state, assignee
        INTO current_state, current_assignee
        FROM ticket_board.tickets
        WHERE id = NEW.id;
        IF current_state IS DISTINCT FROM NEW.state OR current_assignee IS DISTINCT FROM NEW.assignee THEN
            RETURN NULL;
        END IF;
        IF ticket_board.ticket_has_unresolved_blockers(NEW.id) THEN
            RETURN NULL;
        END IF;
        IF ticket_board.ticket_serial_waiting(NEW.id) THEN  -- SYRD-568, as above
            RETURN NULL;
        END IF;

        IF ticket_board.transition_target_role(NEW.state, NEW.assignee) IS NOT NULL THEN
            PERFORM ticket_board.enqueue_transition_notification(
                NEW.id,
                NEW.title,
                NULL,
                NEW.state,
                NEW.assignee,
                NEW.updated_at,
                NEW.ticket_number,
                ticket_board.transition_message(NEW.id, NEW.title, NULL, NEW.state, false)
            );
        END IF;
        RETURN NULL;
    END IF;

    RAISE EXCEPTION 'unsupported trigger operation for notify_ticket_state_transition: %', TG_OP;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_unblocked_dependents()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    dependent record;
BEGIN
    IF TG_OP <> 'UPDATE'
       OR OLD.state IS NOT DISTINCT FROM NEW.state
       OR OLD.state IN ('done', 'cancelled')
       OR NEW.state NOT IN ('done', 'cancelled') THEN
        RETURN NULL;
    END IF;

    FOR dependent IN
        SELECT t.id, t.title, t.state, t.assignee, t.updated_at, t.ticket_number
        FROM ticket_board.ticket_blockers b
        JOIN ticket_board.tickets t ON t.id = b.ticket_id
        WHERE b.blocker_ticket_id = NEW.id
          AND NOT t.manually_controlled
          AND NOT ticket_board.ticket_has_unresolved_blockers(t.id)
          AND ticket_board.unblock_transition_target_role(t.state, t.assignee) IS NOT NULL
          -- SYRD-568: work still waiting in its implementer's queue is not
          -- handed over by unblocking -- telling every unblocked ticket's owner
          -- would wake all of them -- and work that unblocking just made
          -- active was handed over once already, in this same transaction.
          AND NOT ticket_board.ticket_serial_waiting(t.id)
          AND NOT EXISTS (
              SELECT FROM ticket_board.ticket_notification_queue q
              WHERE q.ticket_id = t.id AND q.kind = 'transition' AND q.payload ? 'serial_activation'
                AND q.xmin = pg_current_xact_id()::xid)
        ORDER BY t.ticket_number
    LOOP
        PERFORM ticket_board.enqueue_unblock_notification(
            dependent.id,
            dependent.title,
            NULL,
            dependent.state,
            dependent.assignee,
            dependent.updated_at,
            dependent.ticket_number,
            ticket_board.unblock_transition_message(
                dependent.id,
                dependent.title,
                dependent.state,
                ticket_board.ticket_state_already_announced(
                    dependent.id,
                    ticket_board.unblock_transition_target_role(dependent.state, dependent.assignee),
                    dependent.state
                )
            )
        );
    END LOOP;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true);
    -- Whose transition this is. Normally the caller's own; on a recovery the
    -- executor names the OWNER here, because the step being taken has to be one
    -- the owner could have taken and this trigger asks exactly that question
    -- (SYRD-133). Set transaction-locally by the executor immediately before the
    -- update, in the same way `workflow_action` already is, and cleared after.
    actor text:=coalesce(
        nullif(current_setting('ticket_board.workflow_actor',true),''),
        ticket_board.current_app_actor()); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
    queued_for text; reserved_by text;
BEGIN
    IF actor IS NULL THEN RAISE EXCEPTION 'configured ticket writes require a registered actor' USING ERRCODE='42501'; END IF;
    -- A narrated override moves a ticket the declared workflow will not: that
    -- is what it is for, and the transition table cannot describe it without
    -- describing its own bypass. Only ticket_board.force_move sets this, it is
    -- transaction-local, and reaching it already required the control role's
    -- capabilities. The legacy trigger has always had this escape; the
    -- declarative one did not, so the operation was refused here even after the
    -- capability layer let it through (SYRD-78).
    --
    -- It does not license a sign-off. An override that raised one would be
    -- manufacturing the review it exists to route around, so a sign-off flag
    -- that moves under it is refused rather than written.
    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a forced move cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;

    -- Merge closes its own source, and only its own source. The window is one
    -- ticket, named by the operation itself, and one statement: it is opened
    -- immediately before the close and shut immediately after, so nothing else
    -- in the transaction can travel through it. The destination has to be a
    -- stage the workflow declares terminal, and no sign-off may move -- a merge
    -- that raised one would be manufacturing the review the target still owes
    -- (SYRD-79).
    IF nullif(current_setting('ticket_board.merge_source', true), '') = previous.id THEN
        IF NOT coalesce((
            SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
            WHERE x->>'name' = proposed.state
        ), false) THEN
            RAISE EXCEPTION 'merge may only close its source into a terminal stage, not %', proposed.state
                USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a merge cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- A Director edit changes fields the transition table has nothing to say
    -- about, at whatever stage the ticket is in. The window names one ticket
    -- and is open for one statement. It carries no sign-off: ticket_board
    -- .director_edit, the only operation that opens this window, refuses to
    -- raise one before it writes anything (SYRD-83).
    IF nullif(current_setting('ticket_board.director_edit_target', true), '') = previous.id THEN
        RETURN proposed;
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director' THEN
            RAISE EXCEPTION 'only director may reassign without transition' USING ERRCODE='42501'; END IF;
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND (flag.value->>'kind'='signoff' OR actor<>'director') THEN
                RAISE EXCEPTION 'flag change requires authorized workflow action'; END IF;
        END LOOP;
        -- SYRD-77: an owner change that is not a transition never reached the
        -- serial-focus redirect below, so a same-stage reassignment could leave
        -- one implementer holding two implementation tickets -- exactly the
        -- reservation the queue exists to protect. Resolving it here rather
        -- than in the caller means no write path can hand an implementer
        -- reserved work by declining to ask.
        --
        -- SYRD-568: only a pull policy still diverts, into its ready stage
        -- pinned to the busy author. A Director-dispatched board leaves the
        -- ticket where it was put, assigned, waiting in that implementer's
        -- queue; only the active ticket holds the slot.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_scheduling() IS NOT NULL
               AND ticket_board.declared_stage_kind(proposed.state)='implementation'
               AND ticket_board.ticket_is_implementer_assignee(proposed.assignee)
               AND NOT proposed.manually_controlled
               AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL THEN
                IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN
                    RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
                queued_for:=proposed.assignee;
                reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
                proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
                PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
                proposed.queued_for_assignee:=queued_for;
                proposed.queued_behind_ticket:=coalesce(reserved_by,'');
                PERFORM set_config('ticket_board.serial_focus_queued',
                    jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                        'stage',proposed.state,'assignee',proposed.assignee)::text,true);
            ELSE
                -- A deliberate new owner ends the hold that named the old one,
                -- so the marker never outlives the reservation that set it.
                proposed.queued_for_assignee:='';
                proposed.queued_behind_ticket:='';
            END IF;
        END IF;
        RETURN proposed;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
            RAISE EXCEPTION 'flags cannot be patched alongside a transition'; END IF;
    END LOOP;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x
      WHERE x->>'from'=previous.state AND x->>'to'=proposed.state AND x->>'action'=action_name;
    IF tr IS NULL OR NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
        RAISE EXCEPTION 'unauthorized configured transition: % / %',actor,action_name USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=previous.state;
    IF (source_stage->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
    -- A blocker stops work going FORWARD. Putting work down is not going
    -- forward: a parking stage is not terminal, owns nobody and notifies
    -- nobody, so nothing is promoted by landing there and the blocker is
    -- exactly the reason to park. Refusing it forced the director to clear the
    -- blocker, defer, and restore it -- three writes, a window where the ticket
    -- looked unblocked, and a blocked ticket holding an implementer's serial
    -- slot in the meantime (SYRD-192).
    --
    -- Keyed on the DESTINATION's shape rather than on an action name, because
    -- the name is the tenant's: `declared_parking_stage` is the same predicate
    -- `workflow_config.parking_stage_names` uses, so a tenant that calls it
    -- something other than `backlog` gets this for free.
    IF tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-568: placing blocked work in its implementer's queue promotes
       -- nothing either -- it holds no slot and is handed to nobody until its
       -- blockers resolve and it is the active ticket. Submitting it still is.
       -- A pull policy queues in its ready stage instead, as before.
       AND NOT (ticket_board.declared_stage_kind(tr->>'to') = 'implementation'
                AND ticket_board.declared_scheduling() IS NULL)
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    -- SYRD-568: a ticket waiting in its implementer's queue is not that
    -- implementer's work yet, so it cannot be submitted for review. Putting it
    -- down, routing it back or cancelling it are not submissions.
    IF source_stage->>'kind'='implementation'
       AND (ticket_board.declared_stage_kind(tr->>'to')='review'
            OR coalesce((tr->>'require_commit')::boolean,false) OR coalesce((tr->>'allow_no_code')::boolean,false))
       AND ticket_board.ticket_serial_waiting(previous.id) THEN
        RAISE EXCEPTION '% is waiting in %''s queue behind %; it can be submitted once it is the active ticket',
            previous.id, previous.assignee, coalesce(ticket_board.ticket_current_reserved_ticket(previous.assignee),'its blockers')
            USING ERRCODE='42501'; END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    -- A declared return out of review hands the work back: it needs no
    -- verdict, and the sign-off stays exactly as it was (SYRD-536). Its own
    -- branch, so SYRD-263's parking exemption below is unchanged.
    IF tr->>'primitive' NOT IN ('approve','return','reopen')
       AND ticket_board.declared_review_return(cfg, previous.state, tr->>'to') THEN
        NULL;
    ELSIF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
    -- A sign-off gates leaving a review FORWARD. Parking is not a verdict: the
    -- stage owns nobody, notifies nobody and promotes nothing, and the review's
    -- record rides along untouched -- no sign-off is granted, none is cleared,
    -- the commit stays. Requiring the missing sign-off made an unaccepted User
    -- Review impossible to put down without deciding it (SYRD-263). Keyed on
    -- the destination's shape, like the blocker exemption above (SYRD-192).
    ELSIF source_stage->>'signoff' IS NOT NULL AND tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    IF tr->>'primitive' IN ('return','reopen') THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
    IF (tr->>'allow_no_code')::boolean THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',true); END IF;
    -- A sign-off is a review of one commit (SYRD-271). MEFP-4 was approved for
    -- one commit, routed back by a plain `move` that clears nothing, and
    -- resubmitted with another -- and the audit stage, already signed, was
    -- skipped: the old verdict stood in for a review nobody had done. So when
    -- work leaves implementation carrying a different commit, every review
    -- sign-off is cleared here, before the gates are walked, and it enters
    -- review for exactly the commit it carries.
    -- The same commit resubmitted keeps its review: that exact work was seen.
    -- A no-code submission has no commit to show it is the same work, so it
    -- is always reviewed as new.
    IF source_stage->>'kind' = 'implementation'
       AND ((tr->>'allow_no_code')::boolean
            OR (btrim(coalesce(doc->>'commit_hash','')) <> ''
                AND (doc->>'commit_hash') IS DISTINCT FROM previous.commit_hash)) THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind' = 'signoff' THEN
                doc:=ticket_board.set_workflow_flag(doc,flag.key,false);
            END IF;
        END LOOP;
    END IF;
    target:=proposed.state;
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        EXIT WHEN (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners) THEN proposed.assignee:=owners[1]; END IF;
    IF previous.manually_controlled AND tr->>'primitive'='approve' THEN
        -- Record the decision but retain the deliberate stage/owner hold. The
        -- executor creates the durable handoff after activity triggers finish.
        PERFORM set_config('ticket_board.held_review_target',coalesce(nullif(ticket_board.transition_target_role(target,proposed.assignee),actor),'director'),true);
        -- And keep the transition the approval earned, instead of discarding it
        -- with the stage. A hold defers a decision; releasing it replays this
        -- exact step, as this actor, once (SYRD-180).
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(previous.id,action_name,actor,tr->>'to')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
        target:=previous.state; proposed.assignee:=previous.assignee;
    ELSE
        -- A move that actually happens leaves nothing owed, including the move
        -- that pays a deferral: nothing may be replayed twice.
        DELETE FROM ticket_board.ticket_deferred_review WHERE ticket_id=previous.id;
    END IF;
    doc:=doc||jsonb_build_object('state',target,'assignee',proposed.assignee);
    proposed:=jsonb_populate_record(proposed,doc);
    PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
    -- SYRD-568: on a Director-dispatched board a move into an implementation
    -- stage lands there, assigned, even when that implementer already has
    -- active work, and waits in their queue until it is the active ticket. A
    -- pull policy keeps its ready stage as the queue (SYRD-539): there the
    -- move is still diverted, pinned to the busy author.
    IF ticket_board.declared_scheduling() IS NOT NULL
       AND dest->>'kind'='implementation' AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
        IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
        queued_for:=proposed.assignee;
        reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
        proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        -- Serial focus still holds the ticket, but the outcome is now legible.
        -- The queue destination is frequently the stage the ticket came from,
        -- so without these the caller sees an unchanged ticket and a success.
        proposed.queued_for_assignee:=queued_for;
        proposed.queued_behind_ticket:=coalesce(reserved_by,'');
        PERFORM set_config('ticket_board.serial_focus_queued',
            jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                'stage',proposed.state,'assignee',proposed.assignee)::text,true);
    ELSE
        -- Any move that is not held clears the marker, so it never outlives the
        -- reservation that caused it.
        proposed.queued_for_assignee:='';
        proposed.queued_behind_ticket:='';
    END IF;
    -- Deferring is putting work down, so it has to stop looking like work
    -- somebody has. An owner left on a parked ticket keeps its implementer's
    -- serial reservation and keeps the board highlighting it as current, which
    -- is how a deferral would quietly go on nudging the person who deferred it.
    -- Everything the ticket is -- content, hierarchy, blockers, gates,
    -- sign-offs, comments -- is untouched; only who holds it changes (SYRD-92).
    IF ticket_board.declared_parking_stage(proposed.state) THEN
        -- queued_for is set only by the serial-focus redirect just above, whose
        -- holding destination is frequently this same stage. That ticket is
        -- waiting on a busy implementer rather than being put down, and its
        -- queue bookkeeping is what makes the outcome legible, so it is left
        -- exactly as it was.
        IF queued_for IS NULL THEN
            proposed.assignee:='unassigned';
            proposed.parked:=true;
            proposed.queued_for_assignee:='';
            proposed.queued_behind_ticket:='';
        END IF;
    ELSIF ticket_board.declared_parking_stage(previous.state) THEN
        -- Reviving it: the hold ends with the stage that carried it.
        proposed.parked:=false;
    END IF;
    RETURN proposed;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.enforce_ticket_workflow_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        IF ticket_board.declared_stage_kind(NEW.state)='draft' THEN NEW.assignee:=coalesce(ticket_board.stage_default_assignee(NEW.state),NEW.assignee); END IF;
        -- SYRD-568: only a pull policy still diverts a new ticket for a busy
        -- implementer; otherwise it waits in that implementer's queue.
        IF ticket_board.declared_scheduling() IS NOT NULL
           AND ticket_board.declared_stage_kind(NEW.state)='implementation' AND ticket_board.ticket_current_reserved_ticket(NEW.assignee,NEW.id) IS NOT NULL AND NOT NEW.manually_controlled THEN
            IF ticket_board.declared_workflow()->'queue' IS NULL OR ticket_board.declared_workflow()->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
            NEW.state:=ticket_board.declared_workflow()->'queue'->>'stage'; NEW.assignee:=ticket_board.declared_workflow()->'queue'->>'assignee';
        END IF;
        PERFORM ticket_board.require_stage_owner_assignee(NEW.state,NEW.assignee);
        RETURN NEW;
    END IF;
    IF NEW.state = 'draft' THEN
        NEW.assignee := coalesce(ticket_board.stage_default_assignee('draft'), 'unassigned');
        NEW.parked := false;
    END IF;
    PERFORM ticket_board.require_stage_owner_assignee(NEW.state, NEW.assignee);
    IF NEW.state = 'in_progress'
       AND ticket_board.ticket_is_implementer_assignee(NEW.assignee)
       AND ticket_board.ticket_current_reserved_ticket(NEW.assignee, NEW.id) IS NOT NULL THEN
        NEW.state := 'backlog';
        NEW.parked := false;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.reassign(
    id text,
    assignee text,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text := ticket_board.current_app_actor();
    current_state text;
    current_assignee text;
    normalized_assignee text := btrim(lower(coalesce(reassign.assignee, '')));
    normalized_reason text := btrim(coalesce(reassign.reason, ''));
    ticket_row ticket_board.tickets%ROWTYPE;
    message text;
BEGIN
    -- Director-only at the database boundary, not merely at the API. A missing
    -- caller role is a refusal here rather than a fallback to the service
    -- identity: ownership changes are attributed or they do not happen.
    IF ticket_board.current_actor_role() <> 'ticket_board_service' OR actor IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'role % cannot call reassign',
            coalesce(nullif(actor, ''), ticket_board.current_actor_role())
            USING ERRCODE = '42501';
    END IF;
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'reassign requires a reason';
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(normalized_assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', reassign.assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = reassign.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    IF current_state = 'draft' THEN
        RAISE EXCEPTION 'draft tickets must be released with release_draft';
    END IF;
    IF current_assignee = normalized_assignee THEN
        RAISE EXCEPTION 'ticket % is already assigned to %', id, normalized_assignee;
    END IF;
    -- Refuse an owner the stage cannot have instead of moving the ticket to
    -- suit the owner.
    PERFORM ticket_board.require_stage_owner_assignee(current_state, normalized_assignee);

    -- Recorded before the write, so the reason survives even when the serial
    -- reservation then redirects the ticket and appends its own notice.
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director reassignment in ' || current_state || ': '
        || current_assignee || ' -> ' || normalized_assignee
        || '. Reason: ' || normalized_reason
    );

    UPDATE ticket_board.tickets
    SET assignee = normalized_assignee
    WHERE tickets.id = reassign.id;

    SELECT * INTO ticket_row FROM ticket_board.tickets WHERE tickets.id = reassign.id;
    -- Serial focus held it. The queue announcement has already told the
    -- director, and the target implementer has no actionable work yet, so a
    -- second notification here would be wrong rather than merely noisy.
    IF ticket_row.state IS DISTINCT FROM current_state OR ticket_row.queued_for_assignee <> '' THEN
        RETURN;
    END IF;
    -- Held or blocked work is not actionable either. It stays reassigned; the
    -- existing unblock and release paths announce it when it becomes real work.
    -- SYRD-568: a ticket now waiting in its new owner's queue is not theirs to act on yet.
    IF coalesce(ticket_row.manually_controlled, false) OR ticket_board.ticket_has_unresolved_blockers(id)
       OR ticket_board.ticket_serial_waiting(id) THEN
        RETURN;
    END IF;
    message := id
        || CASE WHEN ticket_row.title <> '' THEN ' -- ' || ticket_row.title ELSE '' END
        || ' is now assigned to you';
    -- One notification, through the same durable queue every other handoff
    -- uses: it dedupes per transaction, suppresses a director self-handoff, and
    -- stays silent for stages that declare no notification.
    PERFORM ticket_board.enqueue_transition_notification(
        id,
        ticket_row.title,
        current_state,
        ticket_row.state,
        ticket_row.assignee,
        ticket_row.updated_at,
        ticket_row.ticket_number,
        message
    );
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.force_move(
    id text,
    new_state text,
    assignee text,
    suppress_notification boolean DEFAULT false
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_state text;
    current_assignee text;
    target_state text;
    serial_focus_queued boolean := false;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'force_move');
    IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = new_state) THEN
        RAISE EXCEPTION 'invalid state: %', new_state;
    END IF;
    IF NOT ticket_board.ticket_valid_assignee(assignee) THEN
        RAISE EXCEPTION 'invalid assignee: %', assignee;
    END IF;

    SELECT tickets.state, tickets.assignee
    INTO current_state, current_assignee
    FROM ticket_board.tickets
    WHERE tickets.id = force_move.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    target_state := new_state;
    -- SYRD-568: a declared workflow queues assigned implementation work in
    -- place, so only a legacy board still diverts it.
    IF new_state = 'in_progress'
       AND ticket_board.declared_workflow() IS NULL
       AND ticket_board.ticket_is_implementer_assignee(force_move.assignee)
       AND ticket_board.ticket_current_reserved_ticket(force_move.assignee, force_move.id) IS NOT NULL THEN
        target_state := 'backlog';
        serial_focus_queued := true;
    END IF;

    PERFORM set_config('ticket_board.force_move', 'on', true);
    IF suppress_notification THEN
        PERFORM set_config('ticket_board.suppress_transition_notify', 'on', true);
    END IF;
    PERFORM ticket_board.append_ticket_comment(
        id,
        actor,
        'Director override: '
        || current_state
        || '/'
        || current_assignee
        || ' -> '
        || new_state
        || '/'
        || assignee
        || CASE
            WHEN serial_focus_queued THEN ' (queued: implementer already has active work)'
            ELSE ''
        END
        || CASE WHEN suppress_notification THEN ' (notification suppressed)' ELSE '' END
    );

    UPDATE ticket_board.tickets
    SET state = target_state,
        assignee = force_move.assignee,
        parked = false
    WHERE tickets.id = force_move.id;
    PERFORM ticket_board.touch_ticket(id);
    IF NOT suppress_notification
       AND current_state IS NOT DISTINCT FROM target_state
       AND current_assignee IS DISTINCT FROM assignee THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'assignee');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.director_edit(
    id text,
    patch jsonb,
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller text;
    invalid_field text;
    current_ticket ticket_board.tickets%ROWTYPE;
    field text;
    old_doc jsonb;
    new_doc jsonb;
    target_state text;
    target_assignee text;
    normalized_parent text;
    seen text[];
    loops int := 0;
BEGIN
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call director_edit; ticket_board_service is the only database writer', actor
            USING ERRCODE = '42501';
    END IF;
    caller := ticket_board.current_app_actor();
    IF NOT ticket_board.role_controls_project(caller) THEN
        RAISE EXCEPTION 'role % cannot call director_edit', coalesce(caller, '<none>')
            USING ERRCODE = '42501';
    END IF;
    IF patch IS NULL OR jsonb_typeof(patch) <> 'object' THEN
        RAISE EXCEPTION 'director_edit patch must be an object';
    END IF;
    IF btrim(coalesce(reason, '')) = '' THEN
        RAISE EXCEPTION 'director_edit requires a reason';
    END IF;

    SELECT key INTO invalid_field
    FROM jsonb_object_keys(patch) AS key
    WHERE key NOT IN (
        'title','body','parent_id','implementation','audit_prompt','origin_project',
        'external_source_ref','blocked_reason','assignee','state','needs_audit',
        'needs_inspection','needs_user_signoff','commit_exempt','regression',
        'manually_controlled','queued_for_assignee','queued_behind_ticket',
        'audit_signoff','inspector_signoff','user_signoff'
    )
    ORDER BY key LIMIT 1;
    IF invalid_field IS NOT NULL THEN
        -- Identity and creation provenance are not editable by anybody.
        RAISE EXCEPTION 'director_edit cannot update: %', invalid_field;
    END IF;

    FOREACH field IN ARRAY ticket_board.signoff_fields() LOOP
        IF patch ? field AND coalesce((patch->>field)::boolean, false) THEN
            RAISE EXCEPTION
                'director_edit cannot create sign-off %; only its own sign-off action may', field
                USING ERRCODE = '42501';
        END IF;
    END LOOP;

    SELECT * INTO current_ticket FROM ticket_board.tickets WHERE tickets.id = director_edit.id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;
    old_doc := to_jsonb(current_ticket);

    IF patch ? 'assignee' THEN
        target_assignee := patch->>'assignee';
        IF NOT ticket_board.ticket_valid_assignee(target_assignee) THEN
            RAISE EXCEPTION 'invalid assignee: %', target_assignee;
        END IF;
    END IF;
    IF patch ? 'state' THEN
        target_state := patch->>'state';
        IF NOT EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name = target_state) THEN
            RAISE EXCEPTION 'invalid state: %', target_state;
        END IF;
    END IF;
    IF patch ? 'parent_id' THEN
        normalized_parent := nullif(upper(btrim(coalesce(patch->>'parent_id', ''))), '');
        IF normalized_parent = director_edit.id THEN
            RAISE EXCEPTION 'a ticket cannot be its own parent';
        END IF;
        IF normalized_parent IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM ticket_board.tickets WHERE tickets.id = normalized_parent) THEN
                RAISE EXCEPTION 'parent ticket not found: %', normalized_parent;
            END IF;
            seen := ARRAY[director_edit.id];
            WHILE normalized_parent IS NOT NULL LOOP
                loops := loops + 1;
                IF loops > 64 THEN RAISE EXCEPTION 'parent chain is too deep'; END IF;
                IF normalized_parent = ANY (seen) THEN
                    RAISE EXCEPTION 'parent change would create a cycle';
                END IF;
                seen := seen || normalized_parent;
                SELECT nullif(btrim(parent_id), '') INTO normalized_parent
                FROM ticket_board.tickets WHERE tickets.id = normalized_parent;
            END LOOP;
            normalized_parent := upper(btrim(patch->>'parent_id'));
        END IF;
    END IF;

    -- Serial focus is an invariant of moving into an implementer's slot, not a
    -- property of the operation that moved it there.
    -- SYRD-568: only a legacy board still diverts; a declared one queues in place.
    IF target_state = 'in_progress'
       AND ticket_board.declared_workflow() IS NULL
       AND ticket_board.ticket_is_implementer_assignee(coalesce(target_assignee, current_ticket.assignee))
       AND ticket_board.ticket_current_reserved_ticket(
               coalesce(target_assignee, current_ticket.assignee), director_edit.id) IS NOT NULL THEN
        target_state := 'backlog';
    END IF;

    -- Its own window, named for this ticket and open for this statement only.
    -- Deliberately not the narrated-move one: a Director edit should not
    -- inherit everything a forced move may bypass, and the sign-off boundary
    -- is enforced independently of either.
    PERFORM set_config('ticket_board.director_edit_target', director_edit.id, true);
    UPDATE ticket_board.tickets SET
        title = CASE WHEN patch ? 'title' THEN patch->>'title' ELSE title END,
        body = CASE WHEN patch ? 'body' THEN patch->>'body' ELSE body END,
        parent_id = CASE WHEN patch ? 'parent_id' THEN coalesce(normalized_parent, '') ELSE parent_id END,
        implementation = CASE WHEN patch ? 'implementation' THEN patch->>'implementation' ELSE implementation END,
        audit_prompt = CASE WHEN patch ? 'audit_prompt' THEN patch->>'audit_prompt' ELSE audit_prompt END,
        origin_project = CASE WHEN patch ? 'origin_project' THEN patch->>'origin_project' ELSE origin_project END,
        external_source_ref = CASE WHEN patch ? 'external_source_ref' THEN patch->>'external_source_ref' ELSE external_source_ref END,
        blocked_reason = CASE WHEN patch ? 'blocked_reason' THEN patch->>'blocked_reason' ELSE blocked_reason END,
        queued_for_assignee = CASE WHEN patch ? 'queued_for_assignee' THEN patch->>'queued_for_assignee' ELSE queued_for_assignee END,
        queued_behind_ticket = CASE WHEN patch ? 'queued_behind_ticket' THEN patch->>'queued_behind_ticket' ELSE queued_behind_ticket END,
        assignee = coalesce(target_assignee, assignee),
        state = coalesce(target_state, state),
        needs_audit = CASE WHEN patch ? 'needs_audit' THEN (patch->>'needs_audit')::boolean ELSE needs_audit END,
        needs_inspection = CASE WHEN patch ? 'needs_inspection' THEN (patch->>'needs_inspection')::boolean ELSE needs_inspection END,
        needs_user_signoff = CASE WHEN patch ? 'needs_user_signoff' THEN (patch->>'needs_user_signoff')::boolean ELSE needs_user_signoff END,
        commit_exempt = CASE WHEN patch ? 'commit_exempt' THEN (patch->>'commit_exempt')::boolean ELSE commit_exempt END,
        regression = CASE WHEN patch ? 'regression' THEN (patch->>'regression')::boolean ELSE regression END,
        manually_controlled = CASE WHEN patch ? 'manually_controlled' THEN (patch->>'manually_controlled')::boolean ELSE manually_controlled END,
        audit_signoff = CASE WHEN patch ? 'audit_signoff' THEN false ELSE audit_signoff END,
        inspector_signoff = CASE WHEN patch ? 'inspector_signoff' THEN false ELSE inspector_signoff END,
        user_signoff = CASE WHEN patch ? 'user_signoff' THEN false ELSE user_signoff END
    WHERE tickets.id = director_edit.id;
    PERFORM set_config('ticket_board.director_edit_target', '', true);

    SELECT to_jsonb(tickets) INTO new_doc FROM ticket_board.tickets WHERE tickets.id = director_edit.id;
    FOR field IN SELECT jsonb_object_keys(patch) LOOP
        IF old_doc->field IS DISTINCT FROM new_doc->field THEN
            INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
            VALUES (director_edit.id, caller, btrim(reason), field, old_doc->field, new_doc->field);
        END IF;
    END LOOP;
    PERFORM ticket_board.append_ticket_comment(
        director_edit.id, caller,
        'Director edit: ' || btrim(reason)
    );
    PERFORM ticket_board.touch_ticket(director_edit.id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.finish_current_stage_blocker(
    p_ticket_id text,
    p_target_role text,
    p_state text,
    p_now timestamptz DEFAULT clock_timestamp(),
    p_claim_timeout interval DEFAULT interval '2 minutes'
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    blocker_ticket_id text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('finish_current_stage_blocker');

    -- SYRD-568: in an implementation stage the board already decided whose
    -- turn it is -- the implementer's active ticket, the only one ever handed
    -- over (settle_serial_focus). Ordering by arrival here instead held an
    -- active ticket's notice behind tickets queued or blocked before it.
    IF ticket_board.declared_workflow() IS NOT NULL AND ticket_board.declared_stage_kind(p_state) = 'implementation' THEN
        -- Manual control is the Director's own hold, never admitted behind
        -- anything, exactly as the arrival order below never held it.
        IF EXISTS (SELECT FROM ticket_board.tickets t WHERE t.id = p_ticket_id AND t.manually_controlled) THEN
            RETURN '';
        END IF;
        blocker_ticket_id := ticket_board.ticket_current_reserved_ticket(p_target_role);
        RETURN CASE WHEN blocker_ticket_id IS NULL OR blocker_ticket_id = p_ticket_id THEN '' ELSE blocker_ticket_id END;
    END IF;

    WITH current_ticket AS (
        SELECT
            t.ticket_number,
            ns.entered_current_state_at
        FROM ticket_board.tickets t
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.id = p_ticket_id
          AND t.state = p_state
          AND t.assignee = p_target_role
          AND NOT t.manually_controlled
    ),
    blocker_candidates AS (
        SELECT
            t.id,
            t.ticket_number,
            ns.entered_current_state_at
        FROM current_ticket c
        JOIN ticket_board.tickets t ON t.state = p_state
        LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
        WHERE t.assignee = p_target_role
          AND t.id <> p_ticket_id
          AND NOT t.manually_controlled
          AND (
              (
                  ns.entered_current_state_at IS NOT NULL
                  AND c.entered_current_state_at IS NOT NULL
                  AND ns.entered_current_state_at < c.entered_current_state_at
              )
              OR (
                  (
                      ns.entered_current_state_at IS NULL
                      OR c.entered_current_state_at IS NULL
                      OR ns.entered_current_state_at = c.entered_current_state_at
                  )
                  AND t.ticket_number < c.ticket_number
              )
          )
    )
    SELECT b.id
    INTO blocker_ticket_id
    FROM blocker_candidates b
    WHERE NOT (
        EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = b.id
              AND q.kind = 'transition'
              AND q.next_attempt_at <= p_now
              AND (q.claimed_at IS NULL OR q.claimed_at <= p_now - p_claim_timeout)
              AND q.dead_lettered_at IS NULL
            ORDER BY q.next_attempt_at, q.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
    )
    ORDER BY
        CASE
            WHEN b.entered_current_state_at IS NOT NULL
                THEN b.entered_current_state_at
            ELSE NULL
        END NULLS LAST,
        b.ticket_number
    LIMIT 1;

    RETURN coalesce(blocker_ticket_id, '');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_serial_focus_reservation_is_current(
    p_ticket_id text,
    p_queued_for_assignee text,
    p_queued_behind_ticket text
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
AS $$
    -- Whether the reservation a queued ticket names is the one still holding
    -- that implementer. The sibling above infers the hold live from a backlog
    -- ticket's own assignee; a declarative queue destination is frequently
    -- analysis/director, so there is no implementer on the row to infer from
    -- and the durable fields are the only record of what it is waiting for.
    --
    -- Every way of not being current answers false -- fields cleared, the named
    -- ticket finished, cancelled, never existed, or superseded by other work on
    -- the same implementer. That is the safe direction: a stale identity
    -- restores ordinary reminders rather than silencing a ticket forever
    -- (SYRD-109).
    -- Every operand is made non-null before it is compared. An unguarded
    -- `reserved_ticket = queued_behind` is NULL, not false, when the
    -- implementer holds nothing -- and `AND NOT NULL` is NULL, so the callers'
    -- WHERE clauses would drop exactly the tickets whose wait had just ended.
    -- The predicate that exists to stop a ticket being silenced forever would
    -- have been the thing silencing it.
    -- SYRD-568: a ticket waiting in place in its implementer's queue is held
    -- by that implementer's active ticket in exactly the same sense, whatever
    -- its (now empty) markers say; every reminder that asks this is quiet
    -- for it until it is active. PL/pgSQL, not SQL, because schema.sql defines
    -- this function long before ticket_serial_waiting exists.
    BEGIN RETURN ticket_board.ticket_serial_waiting(p_ticket_id) OR (
       coalesce(btrim(p_queued_for_assignee), '') <> ''
       AND coalesce(btrim(p_queued_behind_ticket), '') <> ''
       AND coalesce(
               ticket_board.ticket_current_reserved_ticket(
                   btrim(p_queued_for_assignee),
                   p_ticket_id
               ),
               ''
           ) = btrim(p_queued_behind_ticket)
       -- A ticket already owned by the implementer it names is not waiting for
       -- them, whatever the fields say. `ticket_current_reserved_ticket`
       -- excludes the ticket it is asked about, so without this a marker left
       -- on a ticket that has since reached its implementer's own lane would
       -- match some other reservation and silence work somebody is doing.
       AND NOT EXISTS (
           SELECT 1
           FROM ticket_board.tickets held
           WHERE held.id = p_ticket_id
             AND btrim(lower(held.assignee)) = btrim(lower(p_queued_for_assignee))
       )); END;
$$;

GRANT SELECT ON ticket_board.serial_focus TO ticket_board_service, ticket_board_listener;
REVOKE EXECUTE ON FUNCTION ticket_board.settle_serial_focus(text, boolean, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.serial_queue() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.serial_queue() TO ticket_board_service, ticket_board_listener;

-- Record today's slots without a handoff: nothing changed hands.
SELECT ticket_board.settle_serial_focus(i.name, false)
FROM (SELECT DISTINCT btrim(lower(role)) AS name
      FROM ticket_board.workflow_stages ws, unnest(ws.owner_roles) AS role) i;

COMMIT;
