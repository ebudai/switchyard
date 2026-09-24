-- SYRD-262: root's bounded rebind of declared pane roles.
--
-- A legacy tenant's first declared workflow copied an example's runtime,
-- target and slot for every role, so MEFP's Codex Director and Ops matched no
-- registration: the board hid both and the Director had no authority to fix
-- the declaration. This installs the one operation that can: root, through
-- the database owner's connection deploys already use, changes only runtime,
-- target and slot of roles that already exist, against the exact live
-- revision and document it reviewed, and records who did it. It is granted to
-- nobody -- rbac.sql revokes every function from every role and does not grant
-- this one back -- so no board process or pane can call it.
--
-- The definition below is the one in schema.sql, character for character, and
-- tests/workflow_pane_rebind_test.py asserts they stay that way: a migration
-- copy that drifts is the copy an upgraded board actually runs.
--
-- Idempotent: CREATE OR REPLACE, and a revoke that is a no-op when already
-- revoked.

CREATE OR REPLACE FUNCTION ticket_board.rebind_declared_pane_roles(
    expected_revision bigint, expected_document jsonb, bindings jsonb, attribution text
) RETURNS bigint LANGUAGE plpgsql SET search_path=ticket_board,pg_temp AS $$
DECLARE
    current_revision bigint;
    current_document jsonb;
    rebound jsonb;
    binding record;
    runtime_value text;
    target_value text;
    slot_value jsonb;
    rev bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    SELECT revision, document INTO current_revision, current_document
      FROM ticket_board.workflow_configuration WHERE singleton;
    IF current_document IS NULL THEN
        RAISE EXCEPTION 'the board runs no declared workflow to rebind'; END IF;
    IF current_revision IS DISTINCT FROM expected_revision
       OR current_document IS DISTINCT FROM expected_document THEN
        RAISE EXCEPTION 'workflow changed since it was reviewed; reread before rebinding'; END IF;
    IF btrim(coalesce(attribution, '')) = '' THEN
        RAISE EXCEPTION 'a rebind must say what it repairs'; END IF;
    IF jsonb_typeof(bindings) IS DISTINCT FROM 'object' OR bindings = '{}'::jsonb THEN
        RAISE EXCEPTION 'a rebind names at least one role'; END IF;
    rebound := current_document;
    FOR binding IN SELECT key, value FROM jsonb_each(bindings) LOOP
        IF jsonb_typeof(binding.value) IS DISTINCT FROM 'object'
           OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(binding.value) k)
              IS DISTINCT FROM ARRAY['runtime', 'slot', 'target'] THEN
            RAISE EXCEPTION 'a rebind sets exactly runtime, target and slot: %', binding.key; END IF;
        IF NOT EXISTS (SELECT FROM jsonb_array_elements(current_document->'roles') r
                       WHERE r->>'name' = binding.key) THEN
            RAISE EXCEPTION 'no declared role %', binding.key; END IF;
        runtime_value := binding.value->>'runtime';
        target_value := binding.value->>'target';
        slot_value := binding.value->'slot';
        IF (runtime_value IS NULL) <> (target_value IS NULL) THEN
            RAISE EXCEPTION 'runtime and target are set together: %', binding.key; END IF;
        IF runtime_value IS NOT NULL AND runtime_value NOT IN ('claude', 'codex', 'agy', 'hermes') THEN
            RAISE EXCEPTION 'unknown runtime % for %', runtime_value, binding.key; END IF;
        IF target_value IS NOT NULL AND (
            target_value !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'
            OR left(split_part(target_value, ':', 1), length(current_document->>'project') + 1)
               <> (current_document->>'project') || '-') THEN
            RAISE EXCEPTION 'target % is not a pane of this project', target_value; END IF;
        IF jsonb_typeof(slot_value) <> 'null' AND (
            jsonb_typeof(slot_value) <> 'number' OR slot_value::text !~ '^[0-5]$' OR target_value IS NULL) THEN
            RAISE EXCEPTION 'invalid visible slot for %', binding.key; END IF;
        rebound := jsonb_set(rebound, '{roles}', (
            SELECT jsonb_agg(CASE WHEN r->>'name' = binding.key
                                  THEN r || jsonb_build_object('runtime', binding.value->'runtime',
                                                               'target', binding.value->'target',
                                                               'slot', slot_value)
                                  ELSE r END ORDER BY ordinal)
              FROM jsonb_array_elements(rebound->'roles') WITH ORDINALITY AS e(r, ordinal)));
    END LOOP;
    IF EXISTS (SELECT r->>'target' FROM jsonb_array_elements(rebound->'roles') r
               WHERE (r->>'active')::boolean AND r->>'target' IS NOT NULL
               GROUP BY r->>'target' HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'active pane targets must be unique'; END IF;
    IF EXISTS (SELECT r->'slot' FROM jsonb_array_elements(rebound->'roles') r
               WHERE jsonb_typeof(r->'slot') = 'number'
               GROUP BY r->'slot' HAVING count(*) > 1) THEN
        RAISE EXCEPTION 'visible slots must be unique'; END IF;
    IF rebound = current_document THEN
        RETURN current_revision;
    END IF;
    INSERT INTO ticket_board.workflow_revisions(actor, document)
        VALUES('rebind: ' || btrim(attribution), rebound) RETURNING revision INTO rev;
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = rebound WHERE singleton;
    UPDATE ticket_board.workflow_roles w SET definition = r
      FROM jsonb_array_elements(rebound->'roles') r
     WHERE w.name = r->>'name' AND bindings ? w.name;
    RETURN rev;
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.rebind_declared_pane_roles(bigint, jsonb, jsonb, text) FROM PUBLIC;
