CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is still in '
        || p_state
        || ' and you haven''t advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell the director directly what is wrong. Do NOT do nothing.';
$$;

CREATE OR REPLACE FUNCTION ticket_board.idle_without_advancing_director_message(p_ticket_id text, p_state text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_ticket_id
        || ' is still in '
        || p_state
        || ' and you haven''t advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell Eric directly what is wrong. Do NOT do nothing.';
$$;
