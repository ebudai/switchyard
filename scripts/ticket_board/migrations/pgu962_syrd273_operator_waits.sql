-- SYRD-273: work can wait on a person.
--
-- MEFP-2 (in_progress/ops) needed Eric to run an already-reviewed export
-- script after work. `await-role user` is refused (a person has no pane to
-- hand to), an external blocker needs another board's ticket (SYRD-270), and
-- inventing one would be false provenance. So Ops fell back on a Director
-- dependency; the Director could do nothing with it and cleared it; Ops,
-- then looking stalled, asked again -- the same "new handoff" over and over.
--
-- A blocker is exactly the wait that was needed: it keeps the stage and the
-- owner, silences every reminder, handoff and escalation, refuses forward
-- promotion, and clears the Director handoff it replaces. What was missing is
-- a blocker that names a person. `operator:<name>` is that:
--
--   * set by the Director, like any blocker (set_blockers);
--   * never resolved by anything that happens -- only released, by the
--     Director, with release_external_blocker, whose required reason is where
--     the person's result is recorded, and which tells the owner once;
--   * `operator` is reserved: it is never read as a project, and a name is
--     never shaped like a ticket id (`operator:PGU-3` is refused).
--
-- Redefines external_blocker_pattern and normalize_blocker_ref, identical to
-- schema.sql's, and rebuilds the blocker constraint on them. A board upgraded
-- through pgu960 already reads the function, but one provisioned fresh from
-- schema.sql carries the inline literal, which the function cannot widen.
-- Idempotent: CREATE OR REPLACE, and the constraint is dropped before it is
-- added.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.external_blocker_pattern()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- Work on another board, `project:PREFIX-N` (SYRD-270), or a person,
    -- `operator:name` (SYRD-273). `operator` is reserved: it is never a
    -- project, and a name is never shaped like a ticket id.
    SELECT '^((?!operator:)[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+|operator:[a-z][a-z0-9_]*)$';
$$;

CREATE OR REPLACE FUNCTION ticket_board.normalize_blocker_ref(p_raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- A local id is upper case. A qualified reference keeps its project in
    -- lower case, as project names are written, and its ticket id in upper;
    -- a person (`operator:name`) is lower case throughout.
    SELECT CASE
        WHEN lower(split_part(btrim(coalesce(p_raw, '')), ':', 1)) = 'operator'
             AND position(':' IN btrim(coalesce(p_raw, ''))) > 0 THEN
            lower(btrim(coalesce(p_raw, '')))
        WHEN position(':' IN btrim(coalesce(p_raw, ''))) > 0 THEN
            lower(btrim(split_part(btrim(p_raw), ':', 1))) || ':'
            || upper(btrim(substr(btrim(p_raw), position(':' IN btrim(p_raw)) + 1)))
        ELSE upper(btrim(coalesce(p_raw, '')))
    END;
$$;

ALTER TABLE ticket_board.ticket_blockers
    DROP CONSTRAINT IF EXISTS ticket_blockers_blocker_ticket_id_check;
ALTER TABLE ticket_board.ticket_blockers
    ADD CONSTRAINT ticket_blockers_blocker_ticket_id_check CHECK (
        blocker_ticket_id ~ ticket_board.ticket_id_pattern()
        OR blocker_ticket_id ~ ticket_board.external_blocker_pattern()
    );

COMMIT;
