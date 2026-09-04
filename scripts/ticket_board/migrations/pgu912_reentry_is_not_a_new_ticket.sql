-- PGU-912: a ticket re-entering in_progress must not be announced as new.
--
-- The message must still ARRIVE -- re-entry notifications are how a held or
-- re-queued ticket reaches its implementer at all. This changes wording only;
-- nothing here suppresses a notification.
--
-- Order matters: the 4-argument transition_message is dropped before the
-- 5-argument form is created, and unblock_transition_message is recreated
-- afterwards because its SQL body names the function by signature.

CREATE OR REPLACE FUNCTION ticket_board.ticket_state_already_announced(
    p_ticket_id text,
    p_target_role text,
    p_state text
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.notification_trace nt
        WHERE nt.ticket_id = p_ticket_id
          AND nt.target_role = p_target_role
          AND nt.ticket_state_at_event = p_state
          AND nt.kind = 'transition'
          AND nt.event = 'send'
    );
$$;

DROP FUNCTION IF EXISTS ticket_board.transition_message(text, text, text, text);

CREATE OR REPLACE FUNCTION ticket_board.transition_message(
    p_ticket_id text,
    p_title text,
    p_old_state text,
    p_new_state text,
    p_already_announced boolean
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN ticket_board.state_rank(p_old_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) IS NOT NULL
             AND ticket_board.state_rank(p_new_state) < ticket_board.state_rank(p_old_state)
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' kicked back to you'
        WHEN p_new_state = 'analysis'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'in_progress' AND p_already_announced
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' is active again'
        WHEN p_new_state = 'in_progress'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        WHEN p_new_state = 'inspection'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for inspection'
        WHEN p_new_state = 'audit'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for audit'
        WHEN p_new_state = 'dat'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for Director Acceptance Testing'
        WHEN p_new_state = 'user_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for User UAT'
        WHEN p_new_state = 'director_review'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' ready for your review'
        WHEN p_new_state = 'done'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' marked done'
        WHEN p_new_state = 'cancelled'
            THEN p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END || ' cancelled'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.unblock_transition_message(
    p_ticket_id text,
    p_title text,
    p_new_state text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN p_new_state = 'backlog'
            THEN 'New ticket for you: ' || p_ticket_id || CASE WHEN p_title <> '' THEN ' -- ' || p_title ELSE '' END
        ELSE ticket_board.transition_message(p_ticket_id, p_title, NULL, p_new_state, false)
    END;
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
        IF coalesce(OLD.manually_controlled, false) OR coalesce(NEW.manually_controlled, false) THEN
            RETURN NULL;
        END IF;
        IF OLD.state IS NOT DISTINCT FROM NEW.state THEN
            RETURN NULL;
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
