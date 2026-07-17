ALTER TABLE ticket_board.ticket_attachments
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION ticket_board.append_ticket_attachment(
    p_id text,
    p_path text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_position integer;
    normalized_path text := btrim(coalesce(p_path, ''));
BEGIN
    IF normalized_path = '' THEN
        RAISE EXCEPTION 'attachment path must be non-empty';
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE id = p_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_id;
    END IF;

    SELECT COALESCE(max(position), -1) + 1
    INTO next_position
    FROM ticket_board.ticket_attachments
    WHERE ticket_id = p_id;

    INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, is_primary, source_field, metadata)
    VALUES (p_id, next_position, normalized_path, next_position = 0, 'screenshots', coalesce(p_metadata, '{}'::jsonb))
    ON CONFLICT (ticket_id, path) DO UPDATE
    SET metadata = EXCLUDED.metadata;

    UPDATE ticket_board.tickets
    SET screenshot = COALESCE(
        screenshot,
        (
            SELECT path
            FROM ticket_board.ticket_attachments
            WHERE ticket_id = p_id
            ORDER BY position
            LIMIT 1
        )
    )
    WHERE id = p_id;

    PERFORM ticket_board.touch_ticket(p_id);
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.append_ticket_attachment(text, text, jsonb) TO ticket_board_service;
