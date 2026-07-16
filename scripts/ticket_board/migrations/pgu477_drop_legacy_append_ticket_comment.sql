-- PGU-472 added append_ticket_comment(text, text, text, boolean DEFAULT false).
-- Incrementally migrated databases can still have the old 3-arg overload,
-- which makes 3-arg call sites ambiguous because the 4th argument defaults.
DROP FUNCTION IF EXISTS ticket_board.append_ticket_comment(text, text, text);
