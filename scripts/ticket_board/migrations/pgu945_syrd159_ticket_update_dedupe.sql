-- SYRD-159: one ticket update woke the assignee twice.
--
-- notify_ticket_owner_in_place_change keyed its dedupe string on
-- pg_current_xact_id(), so a single user-visible change written as two
-- statements -- set the blockers, then say why -- minted two keys, two queue
-- rows and two wakes for one thing to read. notification_trace on SYRD-146
-- shows the pair 87ms apart, same ticket, same role, same stage.
--
-- The key now names what the wake is about: the ticket and the role it is
-- addressed to. The queue's existing collapse then does the work -- ON CONFLICT
-- refreshes a pending row with the newest description and re-arms it, and
-- ack_notification deletes a delivered row, so the next change after delivery
-- still mints its own wake. The role is in the key so two roles notified in one
-- transaction cannot collide, which ON CONFLICT would otherwise resolve by
-- retargeting the first role's wake to the second.
--
-- The awaiting_role reminder ladder is untouched: its rungs differ by a
-- trailing :N deliberately and are addressed to the director.
BEGIN;

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

COMMIT;
