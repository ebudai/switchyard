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
-- Two ordered units, each its own transaction, because together they cross
-- the 1,250-line review ceiling (SYRD-541):
--   pgu977 (this file): the queue -- the slot, its record, settling, the
--     published order, and who is told what;
--   pgu978: routing in place -- the update and insert triggers, reassignment,
--     force_move and director_edit stop diverting on a dispatch board.
-- A board between the two still diverts as it always did, so no ticket waits
-- in place yet and every notice is the old one or an activation this unit
-- sends once. schema.sql carries every function's final copy where it stands.
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
        -- From the document, not workflow_stages: a ticket read must never wait
        -- on the stage table, which a workflow preview holds exclusively while
        -- it waits for tickets (SYRD-572).
        SELECT DISTINCT btrim(lower(owner)) AS name
        FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x,
             jsonb_array_elements_text(x->'owners') AS owner
        WHERE x->>'kind' = 'implementation'
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
