-- SYRD-124: notifications that re-announce a stage the owner is already in.
--
-- Live on 2026-09-13, twice: "SYRD-122 ... entered Implementation" and
-- "SYRD-111 ... entered Implementation" arrived for tickets that were already
-- in_progress and already being worked by the role they were sent to. Nothing
-- had transitioned. What had happened, both times, was that a blocker on the
-- ticket reached done and the unblock trigger announced the ticket's current
-- stage as an arrival.
--
-- Two things in the same sentence made it say that.
--
-- The declared-workflow `transition_message` took `p_already_announced` and
-- ignored it, so every transition notification on a board with a declared
-- workflow said "entered", including the second and third time one role heard
-- about one stage. The legacy branch has always distinguished the two.
--
-- And the unblock path passed `false` for that argument outright, so even a
-- corrected message would have claimed a first arrival. It now asks the same
-- question the ordinary transition path asks -- has this role already been
-- sent a transition for this ticket in this state -- and says what actually
-- happened when the answer is yes.
--
-- Nothing is suppressed. A blocker clearing on a ticket whose owner has not
-- been told is still an arrival, because that owner has no other way to learn
-- the work is theirs, and that notification is unchanged.

BEGIN;

-- SYRD-124: the declared-workflow branch used to drop `p_already_announced` on
-- the floor and say "entered" every time. The legacy branch has always drawn
-- the distinction -- "New ticket for you" the first time, "is active again"
-- afterwards -- and it matters most on a board that runs a declared workflow,
-- because that is where a role is told a second time about a stage it is
-- already working in. "Entered" is a claim that the ticket moved; on a repeat
-- it did not, and the owner spends a whole read of the ticket finding that out.
CREATE OR REPLACE FUNCTION ticket_board.transition_message(p_ticket_id text,p_title text,p_old_state text,p_new_state text,p_already_announced boolean)
RETURNS text LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN ticket_board.legacy_transition_message(p_ticket_id,p_title,p_old_state,p_new_state,p_already_announced)
 WHEN p_already_announced THEN p_ticket_id || ' -- ' || p_title || ' is active again in ' || coalesce((SELECT display_label FROM ticket_board.workflow_stages WHERE name=p_new_state),p_new_state)
 ELSE p_ticket_id || ' -- ' || p_title || ' entered ' || coalesce((SELECT display_label FROM ticket_board.workflow_stages WHERE name=p_new_state),p_new_state) END;
$$;

-- The 3-argument form is dropped rather than overloaded, for the reason the
-- 4-argument transition_message was: a defaulted extra argument would make
-- every existing call ambiguous.
DROP FUNCTION IF EXISTS ticket_board.unblock_transition_message(text, text, text);

-- SYRD-124: nothing transitions when a blocker clears. The ticket is where it
-- was, with the owner it had; what changed is that it may now be worked. Told
-- for the first time, an owner needs to know it is theirs, so the ordinary
-- arrival wording is right. Told again -- and on the live board this reached a
-- role that was already working the ticket in that stage -- "entered" is a
-- claim the ticket moved, and it did not.
CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_message(
    p_ticket_id text,
    p_title text,
    p_new_state text,
    p_already_announced boolean
)
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is unblocked'
        WHEN p_new_state = 'backlog'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        ELSE ticket_board.transition_message(p_ticket_id, p_title, NULL, p_new_state, false)
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.notify_unblocked_dependents()
-- body reinstalled below with the corrected call; see schema.sql.
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

COMMIT;
