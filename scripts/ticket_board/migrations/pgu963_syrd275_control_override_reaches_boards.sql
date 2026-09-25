-- SYRD-275: the Director's narrated override never reached a live board.
--
-- MEFP's Director, recovering MEFP-5 with `override-move`, was refused:
-- "role director cannot call force_move" -- although the role holds every
-- control capability and SYRD-78 (7848339) had made exactly this work.
--
-- It did, in schema.sql. SYRD-78 changed ticket_board.require_actor there, and
-- added control_override_actions(), but shipped no migration. Every board --
-- fresh or upgraded -- is built as schema.sql, then EVERY pgu* migration in
-- order (ticket-board-migrate records only 'schema.sql'), so the newest
-- MIGRATION copy of a function is what production runs: pgu921's
-- require_actor, which admits an action only as a declared capability, and
-- no document may declare force_move (that would be declaring its own
-- bypass). The suites load schema.sql alone, so they tested a function no
-- board had.
--
-- This installs schema.sql's require_actor and control_override_actions: a
-- narrated override is admitted for the role holding every control
-- capability (merge, set_blockers, set_manually_controlled -- the control
-- role, whatever it is called) and refused for every other role before any of
-- the operation's own checks run. Nothing else changes: force_move still may
-- not move a sign-off (enforce_declared_ticket_update, already live), and the
-- ordinary transitions and their gates are untouched.
--
-- Idempotent: CREATE OR REPLACE only.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.control_override_actions()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['force_move', 'override_move']::text[];
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_actor(
    p_allowed_roles text[],
    p_action text
)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    caller_role text;
BEGIN
    IF ticket_board.declared_workflow() IS NOT NULL THEN
        actor := ticket_board.current_actor_role();
        caller_role := ticket_board.current_app_actor();
        IF actor <> 'ticket_board_service' OR NOT EXISTS (
            SELECT FROM ticket_board.workflow_roles r
            WHERE r.name = caller_role
              AND (r.definition->>'active')::boolean
              AND (
                  r.definition->'capabilities' ? CASE p_action
                      WHEN 'set_awaiting_role' THEN 'await_role' ELSE p_action END
                  -- A narrated override is not an ordinary stage capability.
                  -- These two exist to move a ticket the declared workflow
                  -- will not, so a workflow that declared them as capabilities
                  -- would be declaring its own bypass -- and every project
                  -- would need a document migration before its Director could
                  -- use an operation the API has always advertised. The
                  -- authority is the control role, derived from the
                  -- capabilities that define one rather than from the name
                  -- 'director', so a project whose control role is called
                  -- something else is answered the same way (SYRD-49,
                  -- SYRD-78). Every other role is refused here, before any of
                  -- the operation's own safety checks run.
                  OR (
                      p_action = ANY (ticket_board.control_override_actions())
                      AND r.definition->'capabilities' ?& ticket_board.control_capabilities()
                  )
              )
        ) THEN
            RAISE EXCEPTION 'role % cannot call %', caller_role,p_action USING ERRCODE='42501';
        END IF;
        RETURN actor;
    END IF;
    actor := ticket_board.current_actor_role();
    IF actor <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %; ticket_board_service is the only database writer', actor, p_action
            USING ERRCODE = '42501';
    END IF;
    caller_role := nullif(current_setting('ticket_board.caller_role', true), '');
    IF caller_role IS NOT NULL
       AND cardinality(coalesce(p_allowed_roles, ARRAY[]::text[])) > 0
       AND caller_role <> ALL(p_allowed_roles)
       AND NOT (
           p_allowed_roles && ARRAY['main', 'app', 'ops', 'perf', 'research']::text[]
           AND ticket_board.ticket_is_implementer_assignee(caller_role)
       ) THEN
        RAISE EXCEPTION 'role % cannot call %', caller_role, p_action
            USING ERRCODE = '42501';
    END IF;
    RETURN actor;
END;
$$;

COMMIT;
