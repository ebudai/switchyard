-- PGU-270 live-data migration.
--
-- Run this before reapplying schema.sql, because schema.sql removes 'ready'
-- from tickets_state_check.

BEGIN;

UPDATE ticket_board.tickets
SET state = 'in_progress'
WHERE state = 'ready';

COMMIT;
