-- SYRD-451: on MEFP the Director recorded a person's decision as an
-- `operator:<name>` blocker, as the installed skills say, and the board
-- refused it. That board ran a release from before SYRD-273, which gives
-- exactly that refusal; every release since accepts the form. What the
-- release still let through was the other half of the contract: a blocker
-- naming another board's work or a person "never resolves by itself" and is
-- ended with release-external-blocker, whose reason records the result. A
-- set_blockers that replaced or cleared the list dropped it anyway, with no
-- reason and no record.
--
-- apply_blockers is the one writer that replaces a ticket's blockers. It now
-- refuses to drop a standing external blocker and names the release. The
-- signature is unchanged, so no grant moves. schema.sql carries the same.
BEGIN;

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
    standing_ref text;
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

    -- SYRD-451: another board's work or a person is a wait nothing on this
    -- board resolves, so it ends only by its release, whose reason records
    -- what happened (release_external_blocker). Replacing or clearing the
    -- list must still name every one that stands; dropping one here ended the
    -- wait with no reason and no record of the person's answer.
    SELECT b.blocker_ticket_id
    INTO standing_ref
    FROM ticket_board.ticket_blockers b
    WHERE b.ticket_id = p_ticket_id
      AND ticket_board.is_external_blocker(b.blocker_ticket_id)
      AND b.blocker_ticket_id <> ALL (ARRAY(
          SELECT ticket_board.normalize_blocker_ref(raw_id)
          FROM unnest(coalesce(p_blocker_ids, ARRAY[]::text[])) AS raw_id
      ))
    ORDER BY b.position
    LIMIT 1;
    IF standing_ref IS NOT NULL THEN
        RAISE EXCEPTION 'external blocker % stands until it is released: release-external-blocker % --ref % --reason "<what happened>"',
            standing_ref, p_ticket_id, standing_ref;
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

COMMIT;
