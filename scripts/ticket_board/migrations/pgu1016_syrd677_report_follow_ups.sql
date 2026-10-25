-- SYRD-677: a reporting tenant can follow up on its own upstream report.
--
-- A tenant that filed a report could add nothing to it afterwards: not a
-- correction, not that a later finding supersedes it, not a request to withdraw
-- it. Now:
--   * report_provenance records, when a report is filed, which project filed it
--     and its source reference, and is never changed afterwards: a Director's
--     edit of the ticket's origin fields moves no authority. Existing reports
--     are recorded from their origin fields here, once, without rewriting them;
--   * append_report_follow_up adds one comment, attributed reporter:<project>,
--     to a report that project filed, and tells the Director once. A
--     confirmation is used once: the same one again adds nothing. It changes no
--     state, assignee, flag, field or link; a withdrawal or a supersession is a
--     request for the Director, recorded as such.
-- The board's server checks the tenant's credential, which names its project,
-- and the confirmed digest; these refuse anything else again in the database.
--
-- schema.sql carries the same objects.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.report_provenance (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    origin_project text NOT NULL CHECK (origin_project <> ''),
    external_source_ref text NOT NULL DEFAULT '',
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    recorded_from text NOT NULL CHECK (recorded_from IN ('filing', 'backfill'))
);

-- A ticket created with an origin is a report: its provenance is what it was filed with.
CREATE OR REPLACE FUNCTION ticket_board.record_report_provenance()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    IF btrim(coalesce(NEW.origin_project, '')) <> '' THEN
        INSERT INTO ticket_board.report_provenance (ticket_id, origin_project, external_source_ref, recorded_from)
        VALUES (NEW.id, btrim(NEW.origin_project), btrim(coalesce(NEW.external_source_ref, '')), 'filing')
        ON CONFLICT (ticket_id) DO NOTHING;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_report_provenance ON ticket_board.tickets;
CREATE TRIGGER tickets_report_provenance
    AFTER INSERT ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.record_report_provenance();

CREATE OR REPLACE FUNCTION ticket_board.refuse_report_provenance_change()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'a report''s provenance is fixed when it is filed (SYRD-677)' USING ERRCODE = '42501';
END;
$$;

DROP TRIGGER IF EXISTS report_provenance_fixed ON ticket_board.report_provenance;
CREATE TRIGGER report_provenance_fixed
    BEFORE UPDATE ON ticket_board.report_provenance
    FOR EACH ROW EXECUTE FUNCTION ticket_board.refuse_report_provenance_change();

-- Reports filed before this: their origin fields as they stand.
INSERT INTO ticket_board.report_provenance (ticket_id, origin_project, external_source_ref, recorded_from)
SELECT t.id, btrim(t.origin_project), btrim(coalesce(t.external_source_ref, '')), 'backfill'
FROM ticket_board.tickets t
WHERE btrim(coalesce(t.origin_project, '')) <> ''
    ON CONFLICT (ticket_id) DO NOTHING;

-- Each confirmed follow-up, once: the comment it added and the notice it sent.
CREATE TABLE IF NOT EXISTS ticket_board.report_follow_ups (
    digest text PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{24}$'),
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    origin_project text NOT NULL,
    external_source_ref text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('comment', 'correction', 'withdrawal_request', 'supersession')),
    comment_id bigint,
    notification_id bigint,
    confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- What a follow-up's preview and its confirmation are checked against: whether the ticket exists, the
-- provenance it was filed with (NULLs when it was not filed as a report), and the source reference the ticket
-- shows now, which a Director may have edited since.
CREATE OR REPLACE FUNCTION ticket_board.report_follow_up_target(p_ticket text)
RETURNS TABLE (found boolean, origin_project text, external_source_ref text, shown_source_ref text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT true, p.origin_project, p.external_source_ref, btrim(coalesce(t.external_source_ref, ''))
    FROM ticket_board.tickets t
    LEFT JOIN ticket_board.report_provenance p ON p.ticket_id = t.id
    WHERE t.id = upper(btrim(p_ticket))
    UNION ALL
    SELECT false, NULL, NULL, NULL
    WHERE NOT EXISTS (SELECT FROM ticket_board.tickets t WHERE t.id = upper(btrim(p_ticket)));
$$;

CREATE OR REPLACE FUNCTION ticket_board.append_report_follow_up(
    p_ticket text,
    p_project text,
    p_external_source_ref text,
    p_kind text,
    p_text text,
    p_confirmation text
)
RETURNS TABLE (comment_id bigint, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text := ticket_board.current_actor_role();
    ticket_value text := upper(btrim(coalesce(p_ticket, '')));
    project text := btrim(coalesce(p_project, ''));
    source_ref text := btrim(coalesce(p_external_source_ref, ''));
    prov ticket_board.report_provenance%ROWTYPE;
    t ticket_board.tickets%ROWTYPE;
    said text;
    added bigint;
    notice bigint;
BEGIN
    -- Report-only authority, as file_report's (SYRD-256): the service writes it, for the credential it checked.
    IF actor IS DISTINCT FROM 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call append_report_follow_up; ticket_board_service is the only database writer',
            actor USING ERRCODE = '42501';
    END IF;
    IF p_kind IS NULL OR p_kind NOT IN ('comment', 'correction', 'withdrawal_request', 'supersession') THEN
        RAISE EXCEPTION 'a report follow-up is a comment, correction, withdrawal_request or supersession';
    END IF;
    IF btrim(coalesce(p_text, '')) = '' OR length(p_text) > 8000 THEN
        RAISE EXCEPTION 'a report follow-up''s text is between 1 and 8000 characters';
    END IF;
    IF coalesce(p_confirmation, '') !~ '^[0-9a-f]{24}$' THEN
        RAISE EXCEPTION 'a report follow-up needs its confirmation digest';
    END IF;

    SELECT * INTO t FROM ticket_board.tickets WHERE id = ticket_value FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'there is no ticket % on this board', ticket_value USING ERRCODE = 'P0002';
    END IF;
    SELECT * INTO prov FROM ticket_board.report_provenance p WHERE p.ticket_id = ticket_value;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% was not filed as a report; a follow-up is only for a report its own project filed',
            ticket_value USING ERRCODE = '42501';
    END IF;
    IF prov.origin_project IS DISTINCT FROM project THEN
        RAISE EXCEPTION '% was filed from another project; this follow-up is %''s', ticket_value, project
            USING ERRCODE = '42501';
    END IF;
    IF prov.external_source_ref IS DISTINCT FROM source_ref THEN
        RAISE EXCEPTION '%''s source reference is not the one this follow-up was confirmed for; preview it again',
            ticket_value USING ERRCODE = '42501';
    END IF;

    INSERT INTO ticket_board.report_follow_ups (digest, ticket_id, origin_project, external_source_ref, kind)
    VALUES (p_confirmation, ticket_value, project, source_ref, p_kind)
    ON CONFLICT (digest) DO NOTHING;
    IF NOT FOUND THEN
        -- A confirmation is used once: the same one again, or at the same moment, is the follow-up already added.
        RETURN QUERY SELECT f.comment_id, false FROM ticket_board.report_follow_ups f WHERE f.digest = p_confirmation;
        RETURN;
    END IF;

    said := CASE p_kind
        WHEN 'comment' THEN p_text
        WHEN 'correction' THEN 'Correction from the reporter: ' || p_text
        WHEN 'withdrawal_request' THEN 'The reporter asks to withdraw this report: ' || p_text
        ELSE 'The reporter says a later finding supersedes this report: ' || p_text
    END;
    PERFORM ticket_board.append_ticket_comment(ticket_value, 'reporter:' || project, said);
    SELECT max(c.id) INTO added FROM ticket_board.ticket_comments c
    WHERE c.ticket_id = ticket_value AND c.who = 'reporter:' || project;
    notice := ticket_board.enqueue_notification(
        ticket_value, 'ticket_update', 'director',
        format('%s -- %s: %s added a %s from %s''s report. Read it on the ticket; nothing else changed.',
               ticket_value, t.title, 'reporter:' || project, replace(p_kind, '_', ' '), project),
        jsonb_build_object('kind', 'report_follow_up', 'id', ticket_value, 'target_role', 'director',
                           'origin_project', project, 'follow_up', p_kind, 'comment_id', added),
        'report-follow-up:' || p_confirmation);
    UPDATE ticket_board.report_follow_ups SET comment_id = added, notification_id = notice
    WHERE digest = p_confirmation;
    RETURN QUERY SELECT added, true;
END;
$$;

REVOKE ALL ON ticket_board.report_provenance, ticket_board.report_follow_ups FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.record_report_provenance() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.refuse_report_provenance_change() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.report_follow_up_target(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.append_report_follow_up(text, text, text, text, text, text) FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.report_provenance, ticket_board.report_follow_ups TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.report_follow_up_target(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.append_report_follow_up(text, text, text, text, text, text) TO ticket_board_service;

COMMIT;
