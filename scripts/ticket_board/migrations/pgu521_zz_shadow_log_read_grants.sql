DO $$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'director',
        'user',
        'ops',
        'app',
        'audit',
        'inspector',
        'perf',
        'research',
        'main',
        'ticket_board_service',
        'ticket_board_listener'
    ] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format(
                'GRANT SELECT ON ticket_board.workflow_transition_shadow_log TO %I',
                role_name
            );
        END IF;
    END LOOP;
END;
$$;
