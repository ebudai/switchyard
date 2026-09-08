-- One Unix identity per project. Role routing and actual-process authority are
-- committed as one row so no consumer can observe a new process with an old
-- target (or the inverse). Kept in sync with the tail of schema.sql.
CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignments (
    role text PRIMARY KEY REFERENCES ticket_board.workflow_roles(name) ON DELETE CASCADE,
    runtime text NOT NULL CHECK (runtime IN ('claude','codex','agy','hermes')),
    actual_target text NOT NULL CHECK (actual_target ~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'),
    worktree text NOT NULL CHECK (worktree LIKE '/%'),
    session_dir text NOT NULL CHECK (session_dir LIKE '/%'),
    process_pid bigint NOT NULL CHECK (process_pid > 1),
    process_start_time bigint NOT NULL CHECK (process_start_time > 0),
    process_uid bigint NOT NULL CHECK (process_uid >= 0),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(actual_target),
    UNIQUE(process_pid, process_start_time, process_uid)
);
CREATE TABLE IF NOT EXISTS ticket_board.role_runtime_assignment_history (
    role text NOT NULL, generation bigint NOT NULL, runtime text NOT NULL,
    actual_target text NOT NULL, worktree text NOT NULL, session_dir text NOT NULL,
    process_pid bigint NOT NULL, process_start_time bigint NOT NULL,
    process_uid bigint NOT NULL, registered_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(role, generation)
);

DROP FUNCTION IF EXISTS ticket_board.register_role_runtime(text,text,text,text,text,bigint,bigint,bigint);
CREATE OR REPLACE FUNCTION ticket_board.register_role_runtime(
    p_role text, p_runtime text, p_actual_target text, p_worktree text,
    p_session_dir text, p_process_pid bigint, p_process_start_time bigint,
    p_process_uid bigint, p_expected_generation bigint
)
RETURNS SETOF ticket_board.role_runtime_assignments
LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE
    normalized_role text := lower(btrim(coalesce(p_role, '')));
    configured jsonb;
    published ticket_board.role_runtime_assignments%ROWTYPE;
BEGIN
    SELECT definition INTO configured FROM ticket_board.workflow_roles
     WHERE name=normalized_role FOR SHARE;
    IF configured IS NULL
       AND NOT EXISTS (SELECT FROM ticket_board.workflow_configuration)
       AND (
           EXISTS (SELECT FROM ticket_board.workflow_stages WHERE normalized_role=ANY(owner_roles))
           OR EXISTS (SELECT FROM ticket_board.workflow_transitions WHERE normalized_role=ANY(allowed_roles))
       ) THEN
        configured := jsonb_build_object(
            'name',normalized_role,'active',true,
            'runtime',p_runtime,'target',p_actual_target
        );
        INSERT INTO ticket_board.workflow_roles(name,definition)
        VALUES(normalized_role,configured) ON CONFLICT(name) DO NOTHING;
        SELECT definition INTO configured FROM ticket_board.workflow_roles
         WHERE name=normalized_role FOR SHARE;
    END IF;
    IF configured IS NULL OR NOT coalesce((configured->>'active')::boolean,false)
       OR configured->>'runtime' IS DISTINCT FROM p_runtime
       OR configured->>'target' IS DISTINCT FROM p_actual_target THEN
        RAISE EXCEPTION 'role/runtime/target is not active workflow configuration';
    END IF;
    IF p_actual_target !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'
       OR p_worktree NOT LIKE '/%' OR p_session_dir NOT LIKE '/%'
       OR p_process_pid <= 1 OR p_process_start_time <= 0 OR p_process_uid < 0 THEN
        RAISE EXCEPTION 'invalid role runtime registration';
    END IF;
    INSERT INTO ticket_board.role_runtime_assignments AS assignment(
        role,runtime,actual_target,worktree,session_dir,
        process_pid,process_start_time,process_uid
    ) VALUES (
        normalized_role,p_runtime,p_actual_target,p_worktree,p_session_dir,
        p_process_pid,p_process_start_time,p_process_uid
    ) ON CONFLICT (role) DO UPDATE SET
        runtime=EXCLUDED.runtime, actual_target=EXCLUDED.actual_target,
        worktree=EXCLUDED.worktree, session_dir=EXCLUDED.session_dir,
        process_pid=EXCLUDED.process_pid, process_start_time=EXCLUDED.process_start_time,
        process_uid=EXCLUDED.process_uid, generation=assignment.generation+1,
        registered_at=clock_timestamp()
    WHERE assignment.generation=p_expected_generation
    RETURNING assignment.* INTO published;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'runtime assignment generation changed';
    END IF;
    INSERT INTO ticket_board.role_runtime_assignment_history(
        role,generation,runtime,actual_target,worktree,session_dir,
        process_pid,process_start_time,process_uid,registered_at
    ) VALUES (
        published.role,published.generation,published.runtime,published.actual_target,
        published.worktree,published.session_dir,published.process_pid,
        published.process_start_time,published.process_uid,published.registered_at
    );
    RETURN NEXT published;
END;
$$;
REVOKE ALL ON ticket_board.role_runtime_assignments FROM PUBLIC;
REVOKE ALL ON ticket_board.role_runtime_assignment_history FROM PUBLIC;
DO $$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_service') THEN
        GRANT SELECT ON ticket_board.role_runtime_assignments TO ticket_board_service;
        GRANT SELECT ON ticket_board.role_runtime_assignment_history TO ticket_board_service;
        GRANT EXECUTE ON FUNCTION ticket_board.register_role_runtime(text,text,text,text,text,bigint,bigint,bigint,bigint)
        TO ticket_board_service;
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='ticket_board_listener') THEN
        GRANT SELECT ON ticket_board.role_runtime_assignments TO ticket_board_listener;
    END IF;
END $$;
