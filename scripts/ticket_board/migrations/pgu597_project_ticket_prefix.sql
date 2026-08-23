BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.ticket_id_pattern()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT '^[A-Z][A-Z0-9]*-[0-9]+$';
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_prefix()
RETURNS text
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    raw_prefix text := upper(regexp_replace(coalesce(nullif(current_setting('ticket_board.ticket_prefix', true), ''), 'PGU'), '[^A-Z0-9]', '', 'g'));
BEGIN
    IF raw_prefix = '' THEN
        raw_prefix := 'PGU';
    END IF;
    IF raw_prefix ~ '^[0-9]' THEN
        raw_prefix := 'T' || raw_prefix;
    END IF;
    RETURN raw_prefix;
END;
$$;

ALTER TABLE ticket_board.ticket_blockers
    DROP CONSTRAINT IF EXISTS ticket_blockers_blocker_ticket_id_check;
ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_parent_id_check;
ALTER TABLE ticket_board.tickets
    DROP CONSTRAINT IF EXISTS tickets_id_check;

ALTER TABLE ticket_board.tickets
    DROP COLUMN ticket_number;
ALTER TABLE ticket_board.tickets
    ADD COLUMN ticket_number integer
        GENERATED ALWAYS AS ((substring(id from '^[A-Z][A-Z0-9]*-([0-9]+)$'))::integer) STORED;

ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_id_check CHECK (id ~ ticket_board.ticket_id_pattern());
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_parent_id_check CHECK (parent_id = '' OR parent_id ~ ticket_board.ticket_id_pattern());
ALTER TABLE ticket_board.tickets
    ADD CONSTRAINT tickets_ticket_number_check CHECK (ticket_number > 0);
ALTER TABLE ticket_board.ticket_blockers
    ADD CONSTRAINT ticket_blockers_blocker_ticket_id_check CHECK (blocker_ticket_id ~ ticket_board.ticket_id_pattern());

CREATE UNIQUE INDEX IF NOT EXISTS tickets_ticket_number_key
    ON ticket_board.tickets (ticket_number);
CREATE INDEX IF NOT EXISTS tickets_state_assignee_idx
    ON ticket_board.tickets (state, assignee, ticket_number);

CREATE OR REPLACE FUNCTION ticket_board.next_ticket_id()
RETURNS text
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    next_number integer;
BEGIN
    LOCK TABLE ticket_board.tickets IN EXCLUSIVE MODE;
    SELECT COALESCE(max(ticket_number), 0) + 1
    INTO next_number
    FROM ticket_board.tickets;
    RETURN ticket_board.ticket_prefix() || '-' || next_number::text;
END;
$$;

DO $$
DECLARE
    function_row record;
    function_sql text;
    patched_sql text;
BEGIN
    FOR function_row IN
        SELECT p.oid
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'ticket_board'
          AND p.prokind = 'f'
    LOOP
        function_sql := pg_get_functiondef(function_row.oid);
        patched_sql := replace(function_sql, '!~ ''^PGU-[0-9]+$''', '!~ ticket_board.ticket_id_pattern()');
        patched_sql := replace(patched_sql, '~ ''^PGU-[0-9]+$''', '~ ticket_board.ticket_id_pattern()');
        patched_sql := replace(patched_sql, 'PGU-N', 'PREFIX-N');
        IF patched_sql IS DISTINCT FROM function_sql THEN
            EXECUTE patched_sql;
        END IF;
    END LOOP;
END;
$$;

COMMIT;
