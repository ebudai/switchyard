DO $$
DECLARE
    stale_function record;
BEGIN
    FOR stale_function IN
        SELECT
            n.nspname,
            p.proname,
            pg_get_function_identity_arguments(p.oid) AS identity_arguments
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'ticket_board'
          AND p.prokind = 'f'
          AND lower(pg_get_functiondef(p.oid)) ~ '(eric_signoff|eric_review|eric_sign_off|eric_reopen|needs_eric)'
    LOOP
        EXECUTE format(
            'DROP FUNCTION IF EXISTS %I.%I(%s)',
            stale_function.nspname,
            stale_function.proname,
            stale_function.identity_arguments
        );
    END LOOP;
END;
$$;
