-- SYRD-120: work the board was showing the Director and had not told them about.
--
-- Live on 2026-09-13, SYRD-111, SYRD-118 and SYRD-119 were all in analysis with
-- assignee `unassigned`. `/api/board` drew all three cards in the analysis
-- column, which the declared workflow gives the Director to own,
-- `ticket-board-read queue director` returned nothing, and no notification had
-- ever fired. The board was displaying somebody's work while denying, when
-- asked directly, that it was theirs.
--
-- Both halves came from reading ASSIGNMENT where the actionable fact is
-- OWNERSHIP OF THE STAGE. A stage whose notify policy is `assignee` resolves to
-- the assignee, `unassigned` owns nothing, and the ordinary transition
-- notification therefore has no target and stays silent. That silence is right
-- for a stage nobody owns and wrong for one somebody does.
--
-- The handoff added here fires precisely where the ordinary one is silent, so
-- the two can never both announce the same arrival, and it goes through the
-- same queue as everything else -- retries, busy deferral, restart recovery and
-- exactly-once delivery are that queue's, not a second mechanism's.

BEGIN;

-- The queue has to be allowed to carry the kind, the same replace-the-
-- constraint step that added 'awaiting_role' and 'publication' before it.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation', 'idle_reminder',
                    'awaiting_role', 'publication', 'triage'));

-- Who untriaged work in this stage belongs to, or NULL when there is no
-- answer to derive. Every way of having no answer is a case that must stay
-- silent, and none of them is a role name.
CREATE OR REPLACE FUNCTION ticket_board.unassigned_stage_owner(p_state text, p_assignee text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    cfg jsonb := ticket_board.declared_workflow();
    stage jsonb;
    owners text[];
BEGIN
    -- A board with no declared workflow has no stage ownership to read, and
    -- its legacy table already routes analysis to a fixed role.
    IF cfg IS NULL THEN
        RETURN NULL;
    END IF;
    -- Somebody is already being told. This handoff exists only in the gap the
    -- ordinary notification leaves, so the two can never both fire.
    IF ticket_board.transition_target_role(p_state, p_assignee) IS NOT NULL THEN
        RETURN NULL;
    END IF;
    SELECT x INTO stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = p_state;
    IF stage IS NULL OR coalesce((stage->>'terminal')::boolean, false) THEN
        RETURN NULL;
    END IF;
    -- A stage the document says notifies nobody is a deliberate silence, not a
    -- gap: user_review is owned and announces nothing on purpose.
    IF stage->'notify'->>'kind' = 'none' THEN
        RETURN NULL;
    END IF;
    -- Somewhere to put work down is not somewhere work is waiting for anyone.
    IF ticket_board.declared_parking_stage(p_state) THEN
        RETURN NULL;
    END IF;
    owners := ARRAY(SELECT jsonb_array_elements_text(stage->'owners'));
    -- Nobody owns it, or several do. Choosing between several owners would be
    -- this function inventing an assignment; the whole point is that nobody has
    -- made one yet.
    IF coalesce(array_length(owners, 1), 0) <> 1 THEN
        RETURN NULL;
    END IF;
    RETURN owners[1];
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.unassigned_triage_message(
    p_ticket_id text,
    p_title text,
    p_state text,
    p_owner text
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT format(
        '%s -- %s is in %s with no assignee, and %s owns that stage. '
        'Route it to an implementer, defer it, or take it yourself.',
        p_ticket_id, p_title, p_state, p_owner);
$$;

-- One durable handoff for one stay in the stage. Durable because it goes
-- through the same queue as every other notification: retries, busy deferral,
-- restart recovery and exactly-once delivery are that queue's, not a second
-- mechanism's, and the dedupe key means a trigger that fires twice for one
-- entry still leaves one notice.
CREATE OR REPLACE FUNCTION ticket_board.enqueue_unassigned_triage_handoff(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    recipient text;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    recipient := ticket_board.unassigned_stage_owner(t.state, t.assignee);
    IF recipient IS NULL THEN
        RETURN;
    END IF;
    -- Manual control is a deliberate silence the Director asked for, and a
    -- ticket waiting on work that is not finished is not actionable yet. Both
    -- are answered again when they end: clearing the hold and resolving the
    -- last blocker each bring the ticket back through this function.
    IF coalesce(t.manually_controlled, false)
       OR ticket_board.ticket_has_unresolved_blockers(t.id) THEN
        RETURN;
    END IF;
    PERFORM ticket_board.enqueue_notification(
        t.id,
        'triage',
        recipient,
        ticket_board.unassigned_triage_message(t.id, t.title, t.state, recipient),
        jsonb_build_object(
            'kind', 'triage',
            'id', t.id,
            'title', t.title,
            'state', t.state,
            'assignee', t.assignee,
            'target_role', recipient,
            'ticket_number', t.ticket_number,
            'updated_at', t.updated_at
        ),
        format('triage:%s:%s:%s', t.id, t.state, recipient));
END;
$$;

-- A pending notice that no longer describes the ticket is removed rather than
-- left to be discarded at delivery. The listener would drop it either way --
-- the payload carries the stage and assignee it was written for -- but a notice
-- sitting in the queue beside a ticket that has moved on is the shape SYRD-108
-- had to clean up after, and it reads to anyone looking at the queue as work
-- still waiting to be announced.
CREATE OR REPLACE FUNCTION ticket_board.supersede_unassigned_triage_handoff(p_ticket_id text)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    t ticket_board.tickets%ROWTYPE;
    stale record;
BEGIN
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    FOR stale IN
        SELECT q.id, q.ticket_id, q.target_role, q.kind, q.attempts, q.payload
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = p_ticket_id
          AND q.kind = 'triage'
          AND (q.payload->>'state' IS DISTINCT FROM t.state
               OR q.payload->>'assignee' IS DISTINCT FROM t.assignee
               OR coalesce(t.manually_controlled, false))
    LOOP
        PERFORM ticket_board.record_notification_trace(
            stale.ticket_id, stale.id, stale.target_role, stale.kind, 'discard',
            NULL, 'superseded_unassigned_triage', NULL,
            jsonb_build_object(
                'attempts', stale.attempts,
                'payload', stale.payload,
                'reason', 'superseded_unassigned_triage',
                'current_state', t.state,
                'current_assignee', t.assignee,
                'manually_controlled', coalesce(t.manually_controlled, false)));
        DELETE FROM ticket_board.ticket_notification_queue WHERE id = stale.id;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_unassigned_triage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- The same two suppressions every other notification honours, so a seeded
    -- fixture or a bulk import does not wake anyone.
    IF current_setting('ticket_board.suppress_create_notify', true) = 'on'
       OR current_setting('ticket_board.suppress_transition_notify', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- Routing, deferring, cancelling and taking manual control all arrive
        -- here as a change of the shape the notice was written for.
        PERFORM ticket_board.supersede_unassigned_triage_handoff(NEW.id);
    END IF;
    PERFORM ticket_board.enqueue_unassigned_triage_handoff(NEW.id);
    RETURN NULL;
END;
$$;

-- Entry, not residence: a ticket is announced when it arrives in a stage
-- nobody is holding it in, and an unrelated edit while it sits there is not a
-- second arrival. Two triggers rather than one because a WHEN condition on an
-- INSERT cannot speak about OLD, and the condition is the whole point of the
-- UPDATE half. Manual control is in it because clearing a deliberate hold is
-- the moment the work becomes actionable again.
DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage ON ticket_board.tickets;
DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage_insert ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_unassigned_triage_insert
AFTER INSERT ON ticket_board.tickets
FOR EACH ROW
EXECUTE FUNCTION ticket_board.notify_unassigned_triage();

DROP TRIGGER IF EXISTS tickets_zzzzzz_unassigned_triage_update ON ticket_board.tickets;
CREATE TRIGGER tickets_zzzzzz_unassigned_triage_update
AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW
WHEN (
    OLD.state IS DISTINCT FROM NEW.state
    OR OLD.assignee IS DISTINCT FROM NEW.assignee
    OR OLD.manually_controlled IS DISTINCT FROM NEW.manually_controlled
)
EXECUTE FUNCTION ticket_board.notify_unassigned_triage();

-- The tickets already sitting in this state when the upgrade lands. Without
-- this the fix only helps work that arrives after it, and the three tickets
-- that produced the report stay exactly as silent as they were -- the board
-- would have learned the rule and still not applied it to the case that taught
-- it. One notice each, through the same queue, subject to the same holds: the
-- enqueue function decides, so a deferred, held or blocked ticket is skipped
-- here for the same reasons it would be skipped tomorrow.
DO $backfill$
DECLARE
    waiting text;
BEGIN
    FOR waiting IN
        SELECT t.id FROM ticket_board.tickets t
        WHERE ticket_board.unassigned_stage_owner(t.state, t.assignee) IS NOT NULL
        ORDER BY t.ticket_number
    LOOP
        PERFORM ticket_board.enqueue_unassigned_triage_handoff(waiting);
    END LOOP;
END $backfill$;

COMMIT;
