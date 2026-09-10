-- SYRD-99: the listener now asks, at claim and again immediately before send,
-- whether a queued reminder has been superseded by an awaiting-role handoff
-- established after that reminder was generated.
--
-- It asks by calling ticket_awaiting_role_is_active() directly, so that the
-- delivery side and the enqueue side agree on what "an active wait" means
-- rather than each carrying its own copy of the four-hour window. rbac.sql
-- revokes EXECUTE on every function in the schema and grants back only what a
-- role needs, so without this the listener throws "permission denied for
-- function" on every delivery and nothing is ever sent -- the same way PGU-549
-- introduced a call to ticket_has_unresolved_blockers() without its grant.
--
-- SELECT on ticket_notification_queue and ticket_notification_state is already
-- covered by rbac.sql's GRANT SELECT ON ALL TABLES, which runs after the
-- REVOKEs above it.
GRANT EXECUTE ON FUNCTION ticket_board.ticket_awaiting_role_is_active(text, timestamptz, timestamptz, interval)
    TO ticket_board_listener;
