-- SYRD-706: pg_cron runs a job by connecting to `cron.host`, `localhost` by default, as the job's user. pgu's two
-- jobs (due nudges every five minutes, notification-trace pruning hourly) ran as `postgres` over TCP 127.0.0.1, and
-- only because the cluster trusted every loopback connection -- the same rule that let any account on the host be
-- the cluster superuser. Once that rule is gone they would stop, silently.
--
-- Each job connects through the server's own socket directory instead, where peer authentication admits the
-- server's own `postgres` account and no other: the jobs scheduled are moved. Only schema.sql schedules them, and it
-- ends with these same lines, so a fresh install or a re-applied schema moves them again. A cluster without pg_cron
-- is left alone.
BEGIN;

-- The first directory the server listens on, as libpq takes a host: a path, so the connection is the socket.
CREATE OR REPLACE FUNCTION ticket_board.local_socket_directory()
RETURNS text
LANGUAGE sql
STABLE
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT nullif(btrim(split_part(current_setting('unix_socket_directories'), ',', 1)), '')
$$;

CREATE OR REPLACE FUNCTION ticket_board.cron_over_the_socket(job text)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF to_regclass('cron.job') IS NULL OR ticket_board.local_socket_directory() IS NULL THEN
        RETURN;
    END IF;
    BEGIN
        EXECUTE 'UPDATE cron.job SET nodename = $1 WHERE jobname = $2 AND nodename IS DISTINCT FROM $1'
            USING ticket_board.local_socket_directory(), job;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_cron job % not moved to the local socket: %', job, SQLERRM;
    END;
END;
$$;

SELECT ticket_board.cron_over_the_socket('ticket_board_due_nudges');
SELECT ticket_board.cron_over_the_socket('ticket_board_notification_trace_prune');

COMMIT;
