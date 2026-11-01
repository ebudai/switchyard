BEGIN;

-- SYRD-721 (pgu1023): a report filed as one of a group -- the same report, filed on each of
-- the reporting project's report ancestors it concerns -- records its report
-- group with its confirmation and says it on the ticket, so the related reports
-- are recognised without either board controlling the other. The group is in
-- the confirmation the board checked (report_confirmation.report_digest), so a
-- replay of the same report returns the same ticket, as before.
ALTER TABLE ticket_board.report_confirmations ADD COLUMN IF NOT EXISTS report_group text NOT NULL DEFAULT ''
    CHECK (report_group = '' OR report_group ~ '^rg-[0-9a-f]{20}$');

CREATE OR REPLACE FUNCTION ticket_board.file_confirmed_report(
    p_title text, p_body text, p_origin_project text, p_external_source_ref text, p_requested_stage text,
    p_confirmation text, p_report_group text)
RETURNS TABLE (ticket_id text, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    filed text;
    made boolean;
BEGIN
    IF coalesce(p_report_group, '') !~ '^rg-[0-9a-f]{20}$' THEN
        RAISE EXCEPTION 'report group % is not rg- followed by 20 hex digits', p_report_group;
    END IF;
    SELECT f.ticket_id, f.created INTO filed, made
      FROM ticket_board.file_confirmed_report(p_title, p_body, p_origin_project, p_external_source_ref,
                                              p_requested_stage, p_confirmation) f;
    IF made THEN
        UPDATE ticket_board.report_confirmations c SET report_group = p_report_group WHERE c.digest = p_confirmation;
        PERFORM ticket_board.append_ticket_comment(filed, 'ticket_board_service', format(
            'Report group %s: %s filed this report as one of a group. The same report may be on other boards, '
            'each with its own ticket; this board controls only this one.', p_report_group, p_origin_project));
        -- As file_report does (SYRD-256): an actor that is no role, for the refresh alone.
        PERFORM set_config('ticket_board.workflow_actor', 'report:tenant', true);
        PERFORM ticket_board.refresh_ticket_source_json(filed);
        PERFORM set_config('ticket_board.workflow_actor', '', true);
    END IF;
    RETURN QUERY SELECT filed, made;
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.file_confirmed_report(text, text, text, text, text, text, text) FROM PUBLIC;
-- schema.sql stops here: the service role does not exist yet when it runs, and rbac.sql grants the same.
GRANT EXECUTE ON FUNCTION ticket_board.file_confirmed_report(text, text, text, text, text, text, text) TO ticket_board_service;

COMMIT;
