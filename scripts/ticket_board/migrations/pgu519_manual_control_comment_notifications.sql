CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text,
    urgent boolean
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    comment_actor text;
BEGIN
    actor := ticket_board.require_actor(ARRAY['director', 'eric', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    comment_actor := ticket_board.current_app_actor();
    PERFORM ticket_board.append_ticket_comment(id, comment_actor, text, urgent);
    PERFORM ticket_board.touch_ticket(id);
    IF urgent OR comment_actor IN ('director', 'eric') THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'new comment');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.add_comment(
    id text,
    text text
)
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT ticket_board.add_comment(id, text, false);
$$;
