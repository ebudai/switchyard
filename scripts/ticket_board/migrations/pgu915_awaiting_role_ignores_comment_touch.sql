-- PGU-915: add_comment touches ticket_board.tickets(updated_at), which fires
-- tickets_clear_awaiting_role_activity. Mark that bookkeeping touch so a
-- comment by the awaited role does not clear awaiting_role, while edit_fields
-- and state/assignee changes still do.

CREATE OR REPLACE FUNCTION ticket_board.touch_ticket_notification_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.ts, clock_timestamp());
BEGIN
    -- Deliberately does NOT clear awaiting_role. Until PGU-909 this cleared it
    -- whenever the awaited role commented (`WHEN awaiting_role = NEW.who`), on
    -- the theory that the awaited role's comment IS the awaited deliverable --
    -- PGU-453 built it for perf posting a measurement.
    --
    -- That theory is right about half the comments and wrong about the other
    -- half, and the wrong half fails silently. A director replying "seen, still
    -- blocked" cleared the escalation while the blocking condition was entirely
    -- unchanged: the ticket vanished from the director's own queue and its nudges
    -- resumed, naming the implementer as stuck. An acknowledgement is not an
    -- action, and nothing here can tell the two apart.
    --
    -- So the awaited role now clears the flag explicitly, with
    -- clear_awaiting_role. The cost is a stale flag when someone forgets, and
    -- that is the better failure: it is visible, because the ticket stays in that
    -- role's queue, and it stops suppressing nudges by itself once
    -- ticket_awaiting_role_is_active's window expires. A dropped escalation is
    -- visible to nobody and expires never.
    --
    -- Note for anyone tracing this: add_comment touches ticket_board.tickets to
    -- refresh updated_at after inserting the comment. The tickets trigger uses
    -- ticket_board.awaiting_role_comment_touch to distinguish that bookkeeping
    -- touch from a real non-transition ticket edit by the awaited role.
    UPDATE ticket_board.ticket_notification_state
    SET last_activity_at = activity_at,
        nudge_count = 0
    WHERE ticket_id = NEW.ticket_id;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role_from_ticket_activity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    actor text := nullif(current_setting('ticket_board.caller_role', true), '');
BEGIN
    IF current_setting('ticket_board.awaiting_role_comment_touch', true) = 'on' THEN
        RETURN NULL;
    END IF;

    UPDATE ticket_board.ticket_notification_state
    SET awaiting_role = '',
        awaiting_since_at = NULL
    WHERE ticket_id = NEW.id
      AND awaiting_role <> ''
      AND (
          OLD.state IS DISTINCT FROM NEW.state
          OR OLD.assignee IS DISTINCT FROM NEW.assignee
          OR actor = awaiting_role
      );
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.upsert_ticket_notification_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    activity_at timestamptz := coalesce(NEW.updated_at, NEW.row_updated_at, clock_timestamp());
    reset_idle_reminder boolean := false;
    activation_reset boolean := false;
    comment_touch boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO ticket_board.ticket_notification_state (
            ticket_id,
            current_state,
            current_assignee,
            previous_state,
            entered_current_state_at,
            last_activity_at,
            last_transition_notified_at,
            last_implementer_assignee
        ) VALUES (
            NEW.id,
            NEW.state,
            NEW.assignee,
            NULL,
            activity_at,
            activity_at,
            activity_at,
            CASE
                WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
                ELSE ''
            END
        )
        ON CONFLICT (ticket_id) DO UPDATE
        SET current_state = EXCLUDED.current_state,
            current_assignee = EXCLUDED.current_assignee,
            previous_state = EXCLUDED.previous_state,
            entered_current_state_at = EXCLUDED.entered_current_state_at,
            last_activity_at = EXCLUDED.last_activity_at,
            last_transition_notified_at = EXCLUDED.last_transition_notified_at,
            last_nudged_at = NULL,
            nudge_count = 0,
            idle_reminder_count = 0,
            last_implementer_assignee = EXCLUDED.last_implementer_assignee;
        RETURN NULL;
    END IF;

    activation_reset := (
        (OLD.state = 'backlog' OR OLD.parked)
        AND NOT NEW.parked
        AND NEW.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
    );
    reset_idle_reminder := OLD.state IS DISTINCT FROM NEW.state OR OLD.assignee IS DISTINCT FROM NEW.assignee OR activation_reset;
    comment_touch := current_setting('ticket_board.awaiting_role_comment_touch', true) = 'on';

    INSERT INTO ticket_board.ticket_notification_state (
        ticket_id,
        current_state,
        current_assignee,
        previous_state,
        entered_current_state_at,
        last_activity_at,
        last_transition_notified_at,
        last_nudged_at,
        nudge_count,
        last_implementer_assignee
    ) VALUES (
        NEW.id,
        NEW.state,
        NEW.assignee,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE NULL
        END,
        CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN activity_at
            ELSE activity_at
        END,
        activity_at,
        NULL,
        NULL,
        0,
        CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ''
        END
    )
    ON CONFLICT (ticket_id) DO UPDATE
    SET current_state = EXCLUDED.current_state,
        current_assignee = EXCLUDED.current_assignee,
        previous_state = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state THEN OLD.state
            ELSE ticket_board.ticket_notification_state.previous_state
        END,
        entered_current_state_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN EXCLUDED.entered_current_state_at
            ELSE ticket_board.ticket_notification_state.entered_current_state_at
        END,
        last_activity_at = EXCLUDED.last_activity_at,
        last_transition_notified_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_transition_notified_at
        END,
        last_nudged_at = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN NULL
            ELSE ticket_board.ticket_notification_state.last_nudged_at
        END,
        nudge_count = CASE
            WHEN OLD.state IS DISTINCT FROM NEW.state OR activation_reset THEN 0
            ELSE ticket_board.ticket_notification_state.nudge_count
        END,
        idle_reminder_count = CASE
            WHEN reset_idle_reminder THEN 0
            ELSE ticket_board.ticket_notification_state.idle_reminder_count
        END,
        awaiting_role = CASE
            WHEN reset_idle_reminder THEN ''
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN ''
            ELSE ticket_board.ticket_notification_state.awaiting_role
        END,
        awaiting_since_at = CASE
            WHEN reset_idle_reminder THEN NULL
            WHEN NOT comment_touch
                 AND nullif(current_setting('ticket_board.caller_role', true), '') = ticket_board.ticket_notification_state.awaiting_role THEN NULL
            ELSE ticket_board.ticket_notification_state.awaiting_since_at
        END,
        last_implementer_assignee = CASE
            WHEN NEW.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(NEW.assignee) THEN NEW.assignee
            WHEN OLD.state = 'in_progress' AND ticket_board.ticket_is_implementer_assignee(OLD.assignee) THEN OLD.assignee
            ELSE ticket_board.ticket_notification_state.last_implementer_assignee
        END;

    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text,
    urgent boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text, urgent);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', 'on', true);
    PERFORM ticket_board.touch_ticket(id);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', '', true);
    IF urgent OR comment_actor IN ('director', 'user') THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment');
    END IF;
END;
$$;
