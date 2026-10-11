-- SYRD-582: an implementer's slot is released when its ticket reaches a
-- human's review, and a queued ticket is never delivered before it is active.
--
-- Otto, 2026-10-10/11 (release f92a3d4d): the designer's OTTO-128, and later
-- OTTO-134, sat in user_review with the User, and under the default 'review'
-- policy every review stage held its author's slot. So:
--   * OTTO-133, routed to the designer, waited silently behind a ticket only
--     the User could move, and the pane sat idle with no signal;
--   * OTTO-134, routed the same way, was delivered anyway -- by the owner
--     notice the Director's routing comment raised, which never asked whether
--     the ticket was waiting -- and its finished work was then refused.
--
-- Now:
--   * entering a review stage assigned to a role of kind 'user' records a
--     release (user_review_releases); re-entering implementation clears it;
--   * under the default 'review' policy on a dispatch board, a released ticket
--     holds nothing, so the next queued ticket becomes active and is handed
--     over once, by settle_serial_focus, in the same transaction;
--   * rework from the User queues behind the author's new active ticket, as
--     any later route does (SYRD-568), and a ticket past the User's review
--     does not take the slot back in Director review;
--   * the lifecycle policy (SYRD-276) and pull boards (SYRD-539) are
--     unchanged, and every other review stage still holds;
--   * the owner notice for a comment, edit or reassignment is not sent for a
--     ticket waiting in its implementer's queue.
--
-- Tickets already with the User are recorded now, and each implementer's slot
-- is settled with its handoff: a ticket that has been waiting behind one of
-- them is handed over once. Nothing is moved. schema.sql carries every final
-- copy.
BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.user_review_releases (
    -- SYRD-582: a ticket whose author's slot was released when it reached a
    -- human's review -- a review stage assigned to a role of kind 'user'.
    -- Written whatever the policy (the policy decides what it means), removed
    -- when the ticket re-enters implementation, which holds its author again.
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    released_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    stage text NOT NULL
);

CREATE OR REPLACE FUNCTION ticket_board.record_user_review_release()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-582: remember that a ticket reached a human's review, so its author's
-- slot stays released through the stages after it (Director review, or a
-- Director's re-audit) until rework brings it back into implementation.
-- Named to run before settle_serial_focus, which reads it.
DECLARE v_kind text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN RETURN NULL; END IF;
    IF TG_OP = 'UPDATE' AND OLD.state IS NOT DISTINCT FROM NEW.state
       AND OLD.assignee IS NOT DISTINCT FROM NEW.assignee THEN
        RETURN NULL;
    END IF;
    v_kind := ticket_board.declared_stage_kind(NEW.state);
    IF v_kind = 'implementation' THEN
        DELETE FROM ticket_board.user_review_releases WHERE ticket_id = NEW.id;
    ELSIF v_kind = 'review' AND EXISTS (
        SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
        WHERE r->>'name' = NEW.assignee AND r->>'kind' = 'user') THEN
        INSERT INTO ticket_board.user_review_releases (ticket_id, stage) VALUES (NEW.id, NEW.state)
        ON CONFLICT (ticket_id) DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_user_review_release ON ticket_board.tickets;
CREATE TRIGGER tickets_z_user_review_release
AFTER INSERT OR UPDATE ON ticket_board.tickets
FOR EACH ROW EXECUTE FUNCTION ticket_board.record_user_review_release();

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
 -- SYRD-582: under the default 'review' policy on a Director-dispatched board,
 -- a ticket that has reached a human's review (user_review_releases) holds
 -- nothing: its author is free for the next ticket while the User reviews,
 -- and stays free through the stages after it. Rework re-entering
 -- implementation clears that record and queues behind the author's new
 -- active ticket. Every other review stage still holds, the lifecycle policy
 -- still holds through User review, and a pull board keeps its own release.
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
    AND NOT (s.kind='review' AND ticket_board.declared_scheduling() IS NULL
             AND coalesce((SELECT d.cfg->>'reservation' FROM doc d),'review')='review'
             AND EXISTS (SELECT FROM ticket_board.user_review_releases u WHERE u.ticket_id=t.id))
    AND NOT (s.kind='implementation' AND ticket_board.ticket_has_unresolved_blockers(t.id)))
   OR (coalesce((SELECT d.cfg->>'reservation' FROM doc d),'review')='lifecycle'
    AND s.kind='system' AND NOT s.terminal AND NOT t.parked
    AND ns.last_implementer_assignee=p_implementer)
 )
 ORDER BY s.kind='implementation', f.ticket_id IS NULL, t.ticket_number LIMIT 1) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_ticket_owner_in_place_change(
    p_ticket_id text,
    p_change_summary text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    ticket_row ticket_board.tickets%ROWTYPE;
    actor text;
    target_role text;
    change_summary text := nullif(btrim(coalesce(p_change_summary, '')), '');
    message text;
BEGIN
    actor := ticket_board.current_app_actor();
    SELECT *
    INTO ticket_row
    FROM ticket_board.tickets
    WHERE id = p_ticket_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    target_role := ticket_board.transition_target_role(ticket_row.state, ticket_row.assignee);
    IF target_role IS NULL OR actor IS NULL OR actor = target_role THEN
        RETURN;
    END IF;
    -- SYRD-582: a ticket queued behind its implementer's active work is not
    -- their work yet. A comment, edit or reassignment on it woke them with it
    -- anyway (Otto's OTTO-134: the Director's routing comment arrived as work
    -- the board then refused to accept). It is handed over once, when it
    -- becomes active, and the ticket it hands over carries every change. A
    -- ticket held back by its own blocker still tells its owner what changed,
    -- as before: it cannot be submitted, and the change may be what frees it.
    IF ticket_board.ticket_serial_waiting(p_ticket_id)
       AND NOT ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        RETURN;
    END IF;

    message := ticket_board.ticket_update_message(
        p_ticket_id,
        ticket_row.title,
        ticket_row.state,
        actor,
        change_summary
    );

    PERFORM ticket_board.enqueue_notification(
        p_ticket_id,
        'ticket_update',
        target_role,
        message,
        jsonb_build_object(
            'kind', 'ticket_update',
            'id', p_ticket_id,
            'title', ticket_row.title,
            'state', ticket_row.state,
            'assignee', ticket_row.assignee,
            'updated_at', ticket_row.updated_at,
            'ticket_number', ticket_row.ticket_number,
            'target_role', target_role,
            'message', message,
            'actor', actor,
            'change_summary', change_summary
        ),
        -- SYRD-159: the wake is identified by what it is about -- this ticket,
        -- this role -- not by the transaction that happened to raise it. Keyed
        -- on pg_current_xact_id(), one user-visible change delivered as two
        -- writes (set the blockers, then say why) minted two rows with two keys
        -- and woke the assignee twice for one thing to read. The queue already
        -- knows how to collapse: ON CONFLICT refreshes the pending row's message
        -- and re-arms it, and a delivered row is deleted by ack_notification, so
        -- a stable key means "at most one unread wake per ticket per role" and
        -- the next change after delivery still mints its own.
        --
        -- The role belongs in the key for a second reason: without it, two
        -- notifications raised in one transaction for two different roles
        -- collide, and ON CONFLICT ... SET target_role = EXCLUDED.target_role
        -- would silently retarget the first role's wake to the second.
        'ticket_update:' || p_ticket_id || ':' || target_role
    );
END;
$$;

GRANT SELECT ON ticket_board.user_review_releases TO ticket_board_service, ticket_board_listener;

-- Tickets already with the User have released their author. A ticket past
-- the User's review in a later stage is not recorded: no flag says reliably
-- that it got there (Otto's Director route back to implementation keeps
-- user_signoff), so it holds as it did until it is finished.
INSERT INTO ticket_board.user_review_releases (ticket_id, stage)
SELECT t.id, t.state
FROM ticket_board.tickets t
WHERE ticket_board.declared_workflow() IS NOT NULL
  AND ticket_board.declared_stage_kind(t.state) = 'review'
  AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
              WHERE r->>'name' = t.assignee AND r->>'kind' = 'user')
ON CONFLICT (ticket_id) DO NOTHING;

-- And the slot each of those held is handed on, once: the ticket now active
-- was waiting in its implementer's queue, and nobody had told them.
SELECT ticket_board.settle_serial_focus(i.name, true)
FROM (SELECT DISTINCT btrim(lower(role)) AS name
      FROM ticket_board.workflow_stages ws, unnest(ws.owner_roles) AS role ORDER BY 1) i;

COMMIT;
