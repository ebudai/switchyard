-- SYRD-100: install the function publication routing already depends on.
--
-- `pgu930_syrd93_publication_requests.sql` creates `publication_control_role()`,
-- which calls `ticket_board.control_capabilities()` to work out which role holds
-- publication authority. That function was added to schema.sql on 2026-09-09 in
-- the commit that let the control role's narrated overrides reach their own
-- safety checks, and no migration was added with it.
--
-- A board installed fresh from schema.sql therefore has it and a board built by
-- applying migrations does not, which is every board that has been upgraded
-- rather than rebuilt. On this project's live board the first publication
-- request a role ever made was refused with
--
--   function ticket_board.control_capabilities() does not exist
--
-- from inside `request_publication`, so the whole implementer side of
-- publication was unusable there.
--
-- The body is schema.sql's, unchanged, so a migrated board and a fresh one
-- agree. `CREATE OR REPLACE` makes applying this more than once, and applying
-- it to a board that already has the function, do the same thing.
CREATE OR REPLACE FUNCTION ticket_board.control_capabilities()
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT ARRAY['set_manually_controlled', 'merge']::text[];
$$;
