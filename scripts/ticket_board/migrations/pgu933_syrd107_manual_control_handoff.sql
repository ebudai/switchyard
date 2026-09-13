-- SYRD-107: a held ticket still hands work over when it changes owner.
--
-- Manual control is a deliberate silence -- no nudges, no reminders, no
-- announcement of every edit while a ticket is steered by hand -- and it was
-- also silencing the one message that is not a reminder. On the live case the
-- work moved from in_progress/ops to audit/audit with the hold still set, no
-- transition row was ever enqueued, and Audit sat idle until a Director
-- hand-delivered a comment.
--
-- An actionable transition that moves work to a DIFFERENT owner now enqueues
-- the ordinary durable notification whether or not the ticket is held. Every
-- other silence manual control provides is untouched: a destination nobody owns
-- or that notifies nobody has no target role at all, a move that keeps the same
-- owner is not a handoff, an in-place edit is not a transition, and a blocked
-- ticket still says nothing.
--
-- The function body below is a copy of the one in schema.sql, and
-- ticket_board_schema_function_migration_test holds them equal.

BEGIN;

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

COMMIT;
