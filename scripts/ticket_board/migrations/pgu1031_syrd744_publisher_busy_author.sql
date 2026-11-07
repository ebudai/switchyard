BEGIN;

-- SYRD-744 (pgu1031): the Publisher returns a ticket to a busy author as itself.
--
-- On a pull-scheduled board an author is free once Audit approves (release_after), so when the Publisher decides a
-- candidate cannot be fast-forwarded, its author has usually claimed other work: the return to implementation is
-- diverted by serial focus into the ready stage, pinned to that author. announce_serial_focus_queue, which says so,
-- named its actor with current_app_actor() -- and on a declared board that refuses a session with no caller role.
-- publisher_finish, by design, acts through ticket_board.workflow_actor and sets no caller role (a pane or HTTP caller
-- cannot name itself `publisher`), so every such return was refused:
--
--     the board refused to record returned: invalid configured caller role: <NULL>
--
-- and escalated instead; a Director's publication_retry failed the same way (MEFP-947, twice; nothing pushed).
--
-- The trigger now names the actor as enforce_declared_ticket_update already does: the move's declared workflow actor,
-- else the session's caller role, else the Director. No authority moves: this only chooses whose name the queue notice
-- carries, and the move itself was already checked, with that same actor, before this trigger ran.
--
-- The definition below is schema.sql's, character for character (the newest migration defining a function owns it).
CREATE OR REPLACE FUNCTION ticket_board.announce_serial_focus_queue()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    queued jsonb;
    queued_for text;
    reserved_by text;
    actor text;
    notice text;
BEGIN
    queued := nullif(current_setting('ticket_board.serial_focus_queued', true), '')::jsonb;
    IF queued IS NULL OR queued->>'queued_for' IS NULL THEN
        RETURN NEW;
    END IF;
    -- Clear before writing. append_ticket_comment touches the ticket, which
    -- re-enters this trigger; the cleared marker makes that pass a no-op.
    PERFORM set_config('ticket_board.serial_focus_queued', '', true);
    queued_for := queued->>'queued_for';
    reserved_by := coalesce(nullif(queued->>'reserved_by', ''), 'active work');
    -- SYRD-744: the actor that moved the ticket, as enforce_declared_ticket_update names it -- a workflow
    -- move's declared actor (the Publisher's publisher_finish sets one, and no caller role) before the session's
    -- caller role -- so a return diverted by serial focus is announced as the Publisher's, not refused.
    actor := coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''),
                      nullif(ticket_board.current_app_actor(), ''), 'director');

    notice := NEW.id || ' is queued for ' || queued_for || ': serial focus is held by '
        || reserved_by || '. It is waiting in ' || (queued->>'stage') || '/' || (queued->>'assignee')
        || '. Route it again once ' || reserved_by || ' leaves that implementer, or route it to a free implementer now.';

    PERFORM ticket_board.append_ticket_comment(NEW.id, actor, notice);
    PERFORM ticket_board.enqueue_notification(
        NEW.id,
        'ticket_update',
        'director',
        notice,
        jsonb_build_object(
            'kind', 'ticket_update',
            'id', NEW.id,
            'state', NEW.state,
            'assignee', NEW.assignee,
            'target_role', 'director',
            'queued_for', queued_for,
            'reserved_by', reserved_by,
            'message', notice
        ),
        'serial-focus-queued:' || NEW.id || ':' || queued_for || ':' || reserved_by
    );
    RETURN NEW;
END;
$$;

COMMIT;
