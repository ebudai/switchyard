-- SYRD-838 (pgu1049): the note the User types with Accept reaches the Director in the notice for the accepted ticket.
--
-- The User typed a note into the UAT panel and pressed Accept; the note was dropped, and nothing told the Director.
-- The board UI now sends the note as the acceptance's own text, which user_sign_off and a declared approval already
-- post as the User's comment in the same transaction. This adds that comment to the transition notice the
-- acceptance enqueues for the Director ("... entered Final Sign-Off: the User accepted it, with a note: ..."), as an
-- Inspector's recommendation already rides in its kick-back notice.
--
-- schema.sql carries the same function.

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
    note text;
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
        -- SYRD-838: a note the User typed with an acceptance is for the Director, who acts on it while closing
        -- the ticket, so it rides in the notice that hands the ticket over. It is the User's comment written by
        -- this same acceptance -- Accept carries it as the action's own text -- and only an approval out of the
        -- User's review counts, so a message sent back with the ticket, or a comment from before, is never one.
        -- A long note is cut in the notice; the comment keeps all of it.
        IF (ticket_board.declared_workflow() IS NULL AND OLD.state = 'user_review' AND NEW.user_signoff)
           OR EXISTS (
               SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') AS s,
                           jsonb_array_elements(ticket_board.declared_workflow()->'transitions') AS x
               WHERE s->>'name' = OLD.state AND s->>'kind' = 'review' AND s->'owners' ? 'user'
                 AND x->>'from' = OLD.state AND x->>'to' = NEW.state AND x->>'primitive' = 'approve'
                 AND x->>'action' = current_setting('ticket_board.workflow_action', true)) THEN
            SELECT btrim(c.text)
            INTO note
            FROM ticket_board.ticket_comments AS c
            WHERE c.ticket_id = NEW.id
              AND c.who = 'user'
              AND btrim(c.text) <> ''
              AND c.xmin = pg_current_xact_id()::xid
            ORDER BY c.position DESC
            LIMIT 1;
            IF note IS NOT NULL THEN
                message := message || ': the User accepted it, with a note: '
                    || CASE WHEN char_length(note) <= 4000 THEN note
                            ELSE left(note, 4000) || ' ... (the whole note is the User''s comment on the ticket)' END;
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

COMMIT;
