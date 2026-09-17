-- SYRD-199: restore the notification kinds pgu948 dropped.
--
-- ticket_notification_queue_kind_check is DROP/ADDed by several migrations in
-- sequence, so only the LAST definition survives. pgu936 (SYRD-120) had extended
-- the list with 'publication' and 'triage'. pgu948 (SYRD-194) rebuilt it from the
-- list as it stood BEFORE pgu936 and dropped both, while the code that enqueues
-- them stayed exactly where it was:
--
--     request_publication          -> 'publication'
--     publication resolution       -> 'publication'
--     unassigned triage notice     -> 'triage'
--
-- The visible effect was not a missing notification. enqueue_notification runs
-- inside the caller's transaction, so the CHECK violation aborted the whole
-- statement: filing a bug creates a ticket in 'analysis' with no assignee, which
-- fires the triage notice, which rejected, which rolled the new ticket back.
-- file-bug failed outright.
--
-- This restores the UNION of every kind the schema enqueues, not any previous
-- list -- rebuilding from a snapshot is the mistake that caused this.
ALTER TABLE ticket_board.ticket_notification_queue
    DROP CONSTRAINT IF EXISTS ticket_notification_queue_kind_check;
ALTER TABLE ticket_board.ticket_notification_queue
    ADD CONSTRAINT ticket_notification_queue_kind_check
    CHECK (kind IN ('transition', 'ticket_update', 'nudge', 'escalation',
                    'idle_reminder', 'awaiting_role', 'publication', 'triage',
                    'unresolved_turn', 'unresolved_turn_repair'));
