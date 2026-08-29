BEGIN;

DO $$
DECLARE
    fn text;
BEGIN
    fn := pg_get_functiondef('ticket_board.enforce_ticket_workflow_update()'::regprocedure);
    IF position('AND EXISTS (
           SELECT 1
           FROM ticket_board.workflow_stages ws
           WHERE ws.name = ''audit''
       ) THEN' in fn) = 0 THEN
        fn := replace(
            fn,
            $old$
    IF OLD.state <> 'director_review'
       AND NEW.state = 'director_review'
       AND NOT NEW.audit_signoff
       AND NOT ticket_board.workflow_transition_allowed_config(OLD.state, NEW.state) THEN
$old$,
            $new$
    IF OLD.state <> 'director_review'
       AND NEW.state = 'director_review'
       AND NOT NEW.audit_signoff
       AND EXISTS (
           SELECT 1
           FROM ticket_board.workflow_stages ws
           WHERE ws.name = 'audit'
       ) THEN
$new$
        );
        IF position('AND EXISTS (
           SELECT 1
           FROM ticket_board.workflow_stages ws
           WHERE ws.name = ''audit''
       ) THEN' in fn) = 0 THEN
            RAISE EXCEPTION 'could not patch enforce_ticket_workflow_update for PGU-736';
        END IF;
        EXECUTE fn;
    END IF;
END;
$$;

COMMIT;
