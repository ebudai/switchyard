-- SYRD-476: the board says which ticket holds each implementer's serial slot.
--
-- `switchyard worker-pool <p> status` reported MEFP's luna-1 and luna-2
-- "ready, running" with nothing held, while MEFP-25 and MEFP-31 sat at User
-- UAT holding them: the board diverted MEFP-12 and MEFP-13 behind exactly
-- those tickets. Status read each worker's queue -- the tickets assigned to it
-- -- and a ticket at UAT is assigned to the user. The reservation itself was
-- computed only inside the routing gate, and nothing could read it.
--
-- serial_reservations() returns, for every implementer, the ticket
-- ticket_current_reserved_ticket() names -- the gate's own answer -- so a
-- reader sees what the gate will do: under the default policy or
-- "lifecycle", and whether the tenant declared its UAT stage a review stage
-- (holds) or a system stage (releases). Read-only, SECURITY DEFINER with a
-- fixed search_path, granted to the board service for GET /api/reservations.
-- Idempotent: CREATE OR REPLACE only. schema.sql carries the same definition.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.serial_reservations()
RETURNS TABLE(implementer text, ticket_id text, state text, assignee text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    -- Which ticket holds each implementer's one serial slot, answered by the
    -- same function the routing gate asks, so a report can never disagree with
    -- what the gate will do -- under either reservation policy, and whatever
    -- kind the tenant declared its UAT stage (SYRD-476). A free implementer is
    -- a row with no ticket. Read-only.
    SELECT roles.name, t.id, t.state, t.assignee
    FROM (
        SELECT DISTINCT btrim(lower(role)) AS name
        FROM ticket_board.workflow_stages AS ws, unnest(ws.owner_roles) AS role
    ) AS roles
    LEFT JOIN ticket_board.tickets AS t
      ON t.id = ticket_board.ticket_current_reserved_ticket(roles.name)
    WHERE ticket_board.ticket_is_implementer_assignee(roles.name)
    ORDER BY roles.name;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.serial_reservations() TO ticket_board_service;

COMMIT;
