BEGIN;

-- SYRD-602: a confirmed upstream report creates one ticket, however often it is
-- submitted. The board server checks the confirmation against the report and
-- its own identity (report_confirmation.checked_submission) before calling
-- this; here each confirmation is recorded with the ticket it created, so a
-- replay -- or two identical submissions at once -- returns that ticket and
-- creates nothing.
CREATE TABLE IF NOT EXISTS ticket_board.report_confirmations (
    digest text PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{24}$'),
    ticket_id text REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION ticket_board.file_confirmed_report(
    p_title text, p_body text, p_origin_project text, p_external_source_ref text, p_requested_stage text,
    p_confirmation text)
RETURNS TABLE (ticket_id text, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    filed text;
BEGIN
    INSERT INTO ticket_board.report_confirmations (digest) VALUES (p_confirmation) ON CONFLICT DO NOTHING;
    IF NOT FOUND THEN
        SELECT c.ticket_id INTO filed FROM ticket_board.report_confirmations c WHERE c.digest = p_confirmation;
        IF filed IS NULL THEN
            RAISE EXCEPTION 'report confirmation % was recorded without a ticket', p_confirmation;
        END IF;
        RETURN QUERY SELECT filed, false;
        RETURN;
    END IF;
    -- The overload the board always called: four arguments unless a state is requested (SYRD-548).
    IF coalesce(p_requested_stage, '') = '' THEN
        filed := ticket_board.file_report(p_title, p_body, p_origin_project, p_external_source_ref);
    ELSE
        filed := ticket_board.file_report(p_title, p_body, p_origin_project, p_external_source_ref, p_requested_stage);
    END IF;
    UPDATE ticket_board.report_confirmations c SET ticket_id = filed WHERE c.digest = p_confirmation;
    RETURN QUERY SELECT filed, true;
END;
$$;
REVOKE ALL ON ticket_board.report_confirmations FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.file_confirmed_report(text, text, text, text, text, text) FROM PUBLIC;
-- schema.sql stops here: the service role does not exist yet when it runs, and rbac.sql grants the same.
GRANT EXECUTE ON FUNCTION ticket_board.file_confirmed_report(text, text, text, text, text, text) TO ticket_board_service;

COMMIT;
