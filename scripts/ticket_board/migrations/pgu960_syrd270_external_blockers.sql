-- SYRD-270: a ticket can wait on work that lives on another board.
--
-- MEFP-4 could not submit its commit until Switchyard operator ticket SYRD-269
-- changed the MEFP board's commit repository. The MEFP board had no way to say
-- so: blocked_by accepted only its own tickets ("blocker ticket not found"),
-- and await-role accepts only a role with a pane, never a human operator. Ops
-- fell back on a Director dependency, the Director could do nothing with it
-- and cleared it, Ops opened it again, and the Director was handed the same
-- non-decision over and over.
--
-- An unresolved blocker already says everything that wait needs to say:
-- forward promotion is refused (no gate can be skipped), nudges, stall and
-- turn-end reminders, awaiting-role handoffs and escalations are suppressed,
-- and the ticket keeps its stage and its owner. What was missing is a blocker
-- that names something this board does not hold. So blocked_by now also
-- accepts a qualified reference, `<project>:<PREFIX>-<n>` (e.g.
-- `syrd:SYRD-269`):
--
--   * It never resolves by itself. No ticket on this board carries that id, so
--     nothing moving anywhere -- the foreign ticket included -- touches it.
--     "SYRD-269 moved" is not "MEFP's board can see the commit".
--   * It is released only explicitly, by the role that may set blockers, with
--     a reason: `release_external_blocker`. The release is a comment on the
--     ticket and a notice to its owner; it submits nothing, so the owner still
--     takes the work through every gate.
--   * The board's own prefix is refused in the qualified form: a local ticket
--     is blocked on by its bare id, and resolves when it is done.
--
-- This redefines apply_blockers and set_blockers whole, with bodies identical
-- to schema.sql's; the drift guard compares them. Idempotent: CREATE OR
-- REPLACE, and the constraint is dropped before it is added.
BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.external_blocker_pattern()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT '^[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+$';
$$;

CREATE OR REPLACE FUNCTION ticket_board.is_external_blocker(p_ref text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(p_ref, '') ~ ticket_board.external_blocker_pattern();
$$;

CREATE OR REPLACE FUNCTION ticket_board.normalize_blocker_ref(p_raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    -- A local id is upper case. A qualified reference keeps its project in
    -- lower case, as project names are written, and its ticket id in upper.
    SELECT CASE
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

CREATE OR REPLACE FUNCTION ticket_board.apply_blockers(
    p_ticket_id text,
    p_blocker_ids text[],
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    normalized_reason text := coalesce(p_reason, '');
    blocker_count integer;
    invalid_id text;
    own_board_ref text;
    missing_id text;
    cycle_id text;
BEGIN
    SELECT count(*)
    INTO blocker_count
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id;

    IF blocker_count > 0 AND btrim(normalized_reason) = '' THEN
        RAISE EXCEPTION 'blocked_reason must be non-empty when blockers are set';
    END IF;

    SELECT coalesce(nullif(ticket_board.normalize_blocker_ref(raw_id), ''), '<empty>')
    INTO invalid_id
    FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    WHERE raw_id IS NULL
       OR btrim(raw_id) = ''
       OR NOT (
           ticket_board.normalize_blocker_ref(raw_id) ~ ticket_board.ticket_id_pattern()
           OR ticket_board.is_external_blocker(ticket_board.normalize_blocker_ref(raw_id))
       )
       OR ticket_board.normalize_blocker_ref(raw_id) = p_ticket_id
    LIMIT 1;
    IF invalid_id IS NOT NULL THEN
        RAISE EXCEPTION 'invalid blocker ticket id: %', invalid_id;
    END IF;

    -- SYRD-270: a qualified reference is for work this board does not hold.
    -- One naming this board's own ticket would never resolve when that ticket
    -- is done, which is exactly what a local blocker is for.
    SELECT ref
    INTO own_board_ref
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS ref
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
    ) AS refs
    WHERE ticket_board.is_external_blocker(ref)
      AND split_part(ref, ':', 2) ~ ('^' || ticket_board.ticket_prefix() || '-[0-9]+$')
    LIMIT 1;
    IF own_board_ref IS NOT NULL THEN
        RAISE EXCEPTION 'external blocker % names a ticket on this board; block on % instead',
            own_board_ref, split_part(own_board_ref, ':', 2);
    END IF;

    PERFORM 1 FROM ticket_board.tickets WHERE tickets.id = p_ticket_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket_id;
    END IF;

    WITH normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    )
    SELECT normalized.blocker_id
    INTO missing_id
    FROM normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    WHERE blocker.id IS NULL
      AND NOT ticket_board.is_external_blocker(normalized.blocker_id)
    LIMIT 1;
    IF missing_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocker ticket not found: %', missing_id;
    END IF;

    WITH RECURSIVE normalized AS (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    ),
    blocker_chain(ticket_id) AS (
        SELECT blocker_id
        FROM normalized
        UNION
        SELECT tb.blocker_ticket_id
        FROM blocker_chain chain
        JOIN ticket_board.ticket_blockers tb ON tb.ticket_id = chain.ticket_id
    )
    SELECT ticket_id
    INTO cycle_id
    FROM blocker_chain
    WHERE ticket_id = p_ticket_id
    LIMIT 1;
    IF cycle_id IS NOT NULL THEN
        RAISE EXCEPTION 'blocked_by cycle detected for %', p_ticket_id;
    END IF;

    DELETE FROM ticket_board.ticket_blockers
    WHERE ticket_id = p_ticket_id;

    -- An external blocker has no ticket here, so it is unresolved by
    -- construction; only release_external_blocker removes it.
    INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
    SELECT p_ticket_id, normalized.blocker_id, min(normalized.ord)::integer - 1,
           coalesce(bool_or(blocker.state IN ('done', 'cancelled')), false)
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id, ord
        FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
    ) AS normalized
    LEFT JOIN ticket_board.tickets blocker ON blocker.id = normalized.blocker_id
    GROUP BY normalized.blocker_id
    ORDER BY min(normalized.ord);

    UPDATE ticket_board.tickets
    SET blocked_reason = normalized_reason
    WHERE tickets.id = p_ticket_id;

    -- SYRD-148: a wait made before the blocker is not made true by it. Cleared
    -- here rather than beside the callers, so it happens in the same write that
    -- creates the blocker and no reader can see the pair disagree; and cleared
    -- rather than left for the delivery side to skip, because the wait is also
    -- what suppresses nudges and what the board shows the Director.
    IF ticket_board.ticket_has_unresolved_blockers(p_ticket_id) THEN
        UPDATE ticket_board.ticket_notification_state
        SET awaiting_role = '',
            awaiting_since_at = NULL,
            awaiting_notified_since_at = NULL
        WHERE ticket_id = p_ticket_id
          AND (awaiting_role <> '' OR awaiting_since_at IS NOT NULL);
    END IF;

    PERFORM ticket_board.refresh_ticket_source_json(p_ticket_id);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.set_blockers(
    id text,
    ids text[],
    reason text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    current_blockers text[];
    current_reason text;
    next_blockers text[];
    next_reason text := coalesce(reason, '');
BEGIN
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    SELECT tickets.blocked_reason
    INTO current_reason
    FROM ticket_board.tickets
    WHERE tickets.id = set_blockers.id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', id;
    END IF;

    SELECT COALESCE(array_agg(blocker_ticket_id ORDER BY position), ARRAY[]::text[])
    INTO current_blockers
    FROM ticket_board.ticket_blockers
    WHERE ticket_id = set_blockers.id;

    SELECT COALESCE(array_agg(blocker_id ORDER BY first_ord), ARRAY[]::text[])
    INTO next_blockers
    FROM (
        SELECT ticket_board.normalize_blocker_ref(raw_id) AS blocker_id, min(ord) AS first_ord
        FROM unnest(coalesce(ids, ARRAY[]::text[])) WITH ORDINALITY AS input(raw_id, ord)
        GROUP BY ticket_board.normalize_blocker_ref(raw_id)
    ) AS normalized;

    PERFORM ticket_board.apply_blockers(id, ids, reason);
    PERFORM ticket_board.touch_ticket(id);
    IF current_blockers IS DISTINCT FROM next_blockers OR coalesce(current_reason, '') IS DISTINCT FROM next_reason THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(id, 'blockers');
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.release_external_blocker(
    p_ticket text,
    p_ref text,
    p_reason text,
    p_evidence text
)
RETURNS void
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    actor text;
    v_ticket text := upper(btrim(coalesce(p_ticket, '')));
    ref text := ticket_board.normalize_blocker_ref(p_ref);
    reason text := btrim(coalesce(p_reason, ''));
    evidence text := btrim(coalesce(p_evidence, ''));
    released_text text;
BEGIN
    -- The authority that sets a blocker is the one that releases it: the
    -- Director's, or on a declared board whoever holds set_blockers.
    actor := ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    IF reason = '' THEN
        RAISE EXCEPTION 'releasing an external blocker requires a reason';
    END IF;
    IF NOT ticket_board.is_external_blocker(ref) THEN
        RAISE EXCEPTION 'not an external blocker: %; a blocker on this board resolves when its ticket is done', p_ref;
    END IF;
    PERFORM 1 FROM ticket_board.tickets t WHERE t.id = v_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', v_ticket;
    END IF;

    DELETE FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = v_ticket
      AND b.blocker_ticket_id = ref;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % has no external blocker %', v_ticket, ref;
    END IF;
    IF NOT EXISTS (SELECT FROM ticket_board.ticket_blockers b
                   WHERE b.ticket_id = v_ticket) THEN
        UPDATE ticket_board.tickets t SET blocked_reason = '' WHERE t.id = v_ticket;
    END IF;

    -- The release is on the record, with why and, where the caller verified
    -- one, what this board itself now recognizes.
    released_text := format('External blocker %s released: %s', ref, reason);
    IF evidence <> '' THEN
        released_text := released_text || E'\n' || evidence;
    END IF;
    PERFORM ticket_board.append_ticket_comment(v_ticket, ticket_board.current_app_actor(), released_text, false);
    PERFORM ticket_board.touch_ticket(v_ticket);
    PERFORM ticket_board.refresh_ticket_source_json(v_ticket);
    -- The owner is told, as for any change to the ticket's blockers. Nothing is
    -- moved: whatever the wait was holding back still goes through its gates.
    PERFORM ticket_board.notify_ticket_owner_in_place_change(v_ticket, 'external blocker released');
END;
$$;

GRANT EXECUTE ON FUNCTION ticket_board.release_external_blocker(text, text, text, text) TO ticket_board_service;

DO $grants$
DECLARE writer text;
BEGIN
    FOR writer IN
        SELECT rolname FROM pg_roles
        WHERE NOT rolsuper
          AND has_function_privilege(
                  rolname, 'ticket_board.force_move(text, text, text, boolean)', 'EXECUTE')
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION ticket_board.release_external_blocker(text, text, text, text) TO %I', writer);
    END LOOP;
END $grants$;

COMMIT;
