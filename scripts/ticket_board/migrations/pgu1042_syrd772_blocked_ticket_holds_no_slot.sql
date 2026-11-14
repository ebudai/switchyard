-- SYRD-772 (pgu1042): a blocked ticket holds no implementer's slot in any stage, and does not take it back unblocked.
--
-- SYRD-568/726: a ticket with an unresolved blocker holds nothing -- but only in an implementation stage. Past it (the
-- Director's acceptance stage, say) a ticket waiting on a person outside the board (`operator:`), another board
-- (`project:`) or another ticket kept its implementer's one slot, so the next ticket in that implementer's queue was
-- never handed over: Main sat idle for as long as the operator took (SYRD-768 in DAT behind operator:eric). Now:
--
-- * the rule is the same in every stage that can hold an implementer (implementation, review, and a lifecycle board's
--   unfinished system stages): a ticket with an unresolved blocker, of any kind, holds nothing. Releasing the slot is
--   settle_serial_focus's existing hand-over: the next ticket becomes active and its owner is told once;
-- * a ticket blocked past implementation is recorded as released (blocked_slot_releases), as SYRD-582 records a
--   ticket with the User, and holds nothing in the stages after it even once unblocked, so it never takes the slot
--   back from work already under way. Re-entering implementation clears the record, and the ticket queues there
--   under the usual rules. Work past implementation that was never blocked holds exactly as before.
--
-- Applied, tickets already blocked past implementation are recorded, and every implementer's slot is settled once
-- against the new rule, announcing a slot that changes hands, so an implementer left idle behind a blocked ticket is
-- given its next one.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.blocked_slot_releases (
    -- SYRD-772: a ticket that stood behind an unresolved blocker past implementation, so its author's slot was
    -- released. It holds nothing in the stages after it, blocked or not, until rework brings it back into
    -- implementation, which removes the row and holds its author again under the queue's usual rules.
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    released_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    stage text NOT NULL
);

CREATE OR REPLACE FUNCTION ticket_board.record_blocked_slot_release(p_ticket_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-772: remember that a ticket past implementation is blocked, and so has given up its author's slot, for the
-- rest of that pass: forget it when the pass ends -- back in implementation, finished or cancelled (a terminal stage),
-- or deferred (a draft stage) -- so a later pass, a lifecycle reopen included, holds as it always did (Audit of
-- dcb9dc0b). Read by ticket_current_reserved_ticket.
DECLARE v_state text; v_kind text; v_terminal boolean;
BEGIN
    IF ticket_board.declared_workflow() IS NULL THEN RETURN; END IF;
    SELECT t.state INTO v_state FROM ticket_board.tickets t WHERE t.id = p_ticket_id;
    IF v_state IS NULL THEN RETURN; END IF;
    v_kind := ticket_board.declared_stage_kind(v_state);
    SELECT coalesce((x->>'terminal')::boolean, false) INTO v_terminal
    FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x WHERE x->>'name' = v_state;
    IF v_kind IN ('implementation', 'draft') OR coalesce(v_terminal, false) THEN
        DELETE FROM ticket_board.blocked_slot_releases WHERE ticket_id = p_ticket_id;
    ELSIF v_kind IN ('review', 'system') AND ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        INSERT INTO ticket_board.blocked_slot_releases (ticket_id, stage) VALUES (p_ticket_id, v_state)
        ON CONFLICT (ticket_id) DO NOTHING;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.record_blocked_slot_release_for_ticket()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-772: a ticket changed stage. Named to run before settle_serial_focus, which reads the record.
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.state IS NOT DISTINCT FROM NEW.state THEN RETURN NULL; END IF;
    PERFORM ticket_board.record_blocked_slot_release(NEW.id);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_z_blocked_slot_release ON ticket_board.tickets;
CREATE TRIGGER tickets_z_blocked_slot_release
AFTER INSERT OR UPDATE ON ticket_board.tickets
FOR EACH ROW EXECUTE FUNCTION ticket_board.record_blocked_slot_release_for_ticket();

CREATE OR REPLACE FUNCTION ticket_board.record_blocked_slot_release_for_blocker()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
-- SYRD-772: a blocker was recorded or changed. Named to run before ticket_blockers_zz_settle_serial_focus.
BEGIN
    IF TG_OP <> 'DELETE' THEN
        PERFORM ticket_board.record_blocked_slot_release(NEW.ticket_id);
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS ticket_blockers_z_blocked_slot_release ON ticket_board.ticket_blockers;
CREATE TRIGGER ticket_blockers_z_blocked_slot_release
AFTER INSERT OR UPDATE ON ticket_board.ticket_blockers
FOR EACH ROW EXECUTE FUNCTION ticket_board.record_blocked_slot_release_for_blocker();

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
 -- implementation stage, and only one of them holds the slot.
 --
 -- A ticket with an unresolved blocker holds nothing. SYRD-772: past
 -- implementation too -- a ticket behind another ticket, another board or a
 -- person is recorded as released when its blocker is (blocked_slot_releases),
 -- and stays released once unblocked, until rework re-enters implementation, so
 -- it never takes the slot back from the work handed over. Work past
 -- implementation that holds its author comes first, as it always did; among
 -- implementation work the ticket already active (ticket_board.serial_focus)
 -- keeps the slot while it still qualifies, so work routed later, or unblocked
 -- later, never takes it from work under way; otherwise the lowest ticket
 -- number, which is the queue's stated order.
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
 AND NOT (s.kind<>'implementation' AND EXISTS (SELECT FROM ticket_board.blocked_slot_releases b WHERE b.ticket_id=t.id))
 ORDER BY s.kind='implementation', f.ticket_id IS NULL, t.ticket_number LIMIT 1) END;
$$;

-- Tickets already blocked past implementation have given up their author's slot.
INSERT INTO ticket_board.blocked_slot_releases (ticket_id, stage)
SELECT t.id, t.state FROM ticket_board.tickets t
WHERE ticket_board.declared_workflow() IS NOT NULL
  AND ticket_board.declared_stage_kind(t.state) IN ('review', 'system')
  AND NOT coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                    WHERE x->>'name' = t.state), false)
  AND ticket_board.ticket_has_unresolved_blockers(t.id) ON CONFLICT (ticket_id) DO NOTHING;  -- one line: provisioning reads schema.sql's seed INSERTs up to a line that starts ON CONFLICT

-- Settle every implementer's slot once against the rule above, announcing a slot that changes hands.
DO $$
DECLARE v_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        FOR v_role IN
            SELECT DISTINCT btrim(lower(role)) FROM ticket_board.workflow_stages ws, unnest(ws.owner_roles) AS role
            ORDER BY 1
        LOOP
            PERFORM ticket_board.settle_serial_focus(v_role, true);
        END LOOP;
    END IF;
END;
$$;

REVOKE ALL ON FUNCTION ticket_board.record_blocked_slot_release(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_blocked_slot_release_for_ticket() FROM PUBLIC;
REVOKE ALL ON FUNCTION ticket_board.record_blocked_slot_release_for_blocker() FROM PUBLIC;
GRANT SELECT ON ticket_board.blocked_slot_releases TO ticket_board_service, ticket_board_listener;

COMMIT;
