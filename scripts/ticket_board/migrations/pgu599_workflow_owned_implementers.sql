CREATE OR REPLACE FUNCTION ticket_board.ticket_is_implementer_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM ticket_board.workflow_stages ws
        WHERE ws.name = 'in_progress'
          AND btrim(lower(coalesce(p_assignee, ''))) = ANY(ws.owner_roles)
    );
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_valid_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH normalized AS (
        SELECT btrim(lower(coalesce(p_assignee, ''))) AS role
    )
    SELECT role = 'unassigned'
        OR role = 'agent'
        OR EXISTS (
            SELECT 1
            FROM ticket_board.workflow_stages ws
            WHERE role = ANY(ws.owner_roles)
        )
    FROM normalized;
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

CREATE OR REPLACE FUNCTION ticket_board.current_app_actor()
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    actor text;
BEGIN
    actor := nullif(current_setting('ticket_board.caller_role', true), '');
    IF actor IS NULL THEN
        RETURN ticket_board.current_actor_role();
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM (
            SELECT unnest(wt.allowed_roles) AS role
            FROM ticket_board.workflow_transitions wt
            UNION
            SELECT unnest(ws.owner_roles) AS role
            FROM ticket_board.workflow_stages ws
        ) AS valid_roles
        WHERE valid_roles.role = actor
    ) THEN
        RAISE EXCEPTION 'invalid ticket_board.caller_role: %', actor;
    END IF;
    RETURN actor;
END;
$$;
