-- SYRD-541: review of oversized growth before integration. See the block
-- comment below and docs/syrd-541-size-review.md. schema.sql carries the same
-- text. Deploys run rbac.sql after migrations (SYRD-530), and it carries the
-- grants too.
BEGIN;

-- SYRD-541: review of oversized growth before integration.
--
-- The board is the one integration step Switchyard holds: main is pushed to the
-- forge by hand, but Audit approves an exact candidate here and the Director
-- records the integration commit here. Once a Director enables review, those
-- two transitions need a recorded scan of the exact commit, and no unresolved
-- size finding for it. A finding is growth the candidate itself made: taking a
-- file over 1,250 lines, or past its allowance (its size at the merge-base, or
-- a Director's reviewed ceiling, whichever is larger). It resolves when the
-- growth is gone, or when the Director approves an exception bounded to the
-- measured size. Measurement is the board's own (scripts/file_size_policy.py,
-- run on its commit cache); no candidate-authored file can record a scan or an
-- exception.

CREATE TABLE IF NOT EXISTS ticket_board.size_review_policy (
    id boolean PRIMARY KEY DEFAULT true CHECK (id),
    enabled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    enabled_by text NOT NULL,
    baseline_commit text NOT NULL CHECK (baseline_commit ~ '^[0-9a-f]{40}$'),
    -- Every in-scope file over the review threshold at the baseline: retained
    -- debt, recorded, not approved.
    inventory jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_board.size_exceptions (
    id bigserial PRIMARY KEY,
    path text NOT NULL CHECK (btrim(path) <> ''),
    ceiling_lines integer NOT NULL CHECK (ceiling_lines > 0),
    reviewed_commit text NOT NULL CHECK (reviewed_commit ~ '^[0-9a-f]{40}$'),
    reviewed_base text NOT NULL CHECK (reviewed_base ~ '^[0-9a-f]{40}$'),
    rationale text NOT NULL CHECK (btrim(rationale) <> ''),
    approved_by text NOT NULL,
    -- NULL: standing, for the file wherever it changes; otherwise this ticket only.
    scope_ticket text REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS size_exceptions_one_active
    ON ticket_board.size_exceptions (path, coalesce(scope_ticket, ''))
    WHERE state = 'active';

-- The latest scan of each ticket's candidate: the packet's size evidence.
CREATE TABLE IF NOT EXISTS ticket_board.size_scans (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    candidate text NOT NULL CHECK (candidate ~ '^[0-9a-f]{40}$'),
    base text CHECK (base IS NULL OR base ~ '^[0-9a-f]{40}$'),
    report jsonb NOT NULL DEFAULT '[]'::jsonb,
    error text,
    scanned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One finding per ticket and file, kept across candidates: updated in place,
-- resolved when the growth is gone, reopened when it comes back.
CREATE TABLE IF NOT EXISTS ticket_board.size_findings (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    path text NOT NULL,
    previous_path text,
    reason text NOT NULL CHECK (reason IN ('crossing', 'growth', 'scan_failed')),
    state text NOT NULL CHECK (state IN ('open', 'resolved', 'excepted')),
    candidate text NOT NULL,
    base text,
    before_lines integer,
    after_lines integer,
    ceiling_lines integer,
    detail text NOT NULL DEFAULT '',
    exception_id bigint REFERENCES ticket_board.size_exceptions(id),
    revision integer NOT NULL DEFAULT 1,
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (ticket_id, path)
);

CREATE OR REPLACE FUNCTION ticket_board.size_review_enabled()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT EXISTS (SELECT 1 FROM ticket_board.size_review_policy);
$$;

-- The reviewed ceiling for each path, as this ticket sees it: its own
-- exception first, then a standing one. Paths with none are absent.
CREATE OR REPLACE FUNCTION ticket_board.size_ceilings(p_ticket text, p_paths text[])
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT coalesce(jsonb_object_agg(path, ceiling_lines), '{}'::jsonb)
    FROM (
        SELECT DISTINCT ON (e.path) e.path, e.ceiling_lines
        FROM ticket_board.size_exceptions e
        WHERE e.state = 'active'
          AND e.path = ANY (coalesce(p_paths, ARRAY[]::text[]))
          AND (e.scope_ticket IS NULL OR e.scope_ticket = p_ticket)
        ORDER BY e.path, (e.scope_ticket IS NULL), e.id DESC
    ) chosen;
$$;

-- Record one scan of a ticket's candidate, measured by the board itself. The
-- findings it names are opened or updated in place; this ticket's other open
-- findings are resolved, because this candidate no longer grows those files.
-- A failed scan is one 'scan_failed' finding, which only a later successful
-- scan clears. Called by the board while it handles a transition, in its own
-- transaction, so a refused transition still leaves the finding behind.
CREATE OR REPLACE FUNCTION ticket_board.record_size_scan(
    p_ticket text,
    p_candidate text,
    p_base text,
    p_report jsonb,
    p_findings jsonb,
    p_error text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    f jsonb;
    named text[] := ARRAY[]::text[];
    opened text[] := ARRAY[]::text[];
    existing ticket_board.size_findings;
    now_ts timestamptz := clock_timestamp();
    error_text text := nullif(btrim(coalesce(p_error, '')), '');
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'only the board records size scans' USING ERRCODE = '42501';
    END IF;
    PERFORM 1 FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket not found: %', p_ticket;
    END IF;
    INSERT INTO ticket_board.size_scans (ticket_id, candidate, base, report, error, scanned_at)
    VALUES (p_ticket, p_candidate, p_base, coalesce(p_report, '[]'::jsonb), error_text, now_ts)
    ON CONFLICT (ticket_id) DO UPDATE
        SET candidate = EXCLUDED.candidate, base = EXCLUDED.base, report = EXCLUDED.report,
            error = EXCLUDED.error, scanned_at = EXCLUDED.scanned_at;

    IF error_text IS NOT NULL THEN
        p_findings := jsonb_build_array(jsonb_build_object(
            'path', '*', 'reason', 'scan_failed', 'detail', error_text));
    END IF;
    FOR f IN SELECT * FROM jsonb_array_elements(coalesce(p_findings, '[]'::jsonb)) LOOP
        named := named || (f ->> 'path');
        SELECT * INTO existing FROM ticket_board.size_findings
        WHERE ticket_id = p_ticket AND path = f ->> 'path';
        IF NOT FOUND THEN
            INSERT INTO ticket_board.size_findings (ticket_id, path, previous_path, reason, state, candidate, base,
                                                    before_lines, after_lines, ceiling_lines, detail)
            VALUES (p_ticket, f ->> 'path', f ->> 'previous_path', f ->> 'reason', 'open', p_candidate, p_base,
                    (f ->> 'before')::integer, (f ->> 'after')::integer, (f ->> 'ceiling')::integer,
                    coalesce(f ->> 'detail', ''));
            opened := opened || (f ->> 'path');
        ELSIF existing.state <> 'open' OR existing.candidate <> p_candidate
              OR existing.after_lines IS DISTINCT FROM (f ->> 'after')::integer THEN
            IF existing.state <> 'open' THEN
                opened := opened || (f ->> 'path');
            END IF;
            UPDATE ticket_board.size_findings
               SET previous_path = f ->> 'previous_path', reason = f ->> 'reason', state = 'open',
                   candidate = p_candidate, base = p_base, before_lines = (f ->> 'before')::integer,
                   after_lines = (f ->> 'after')::integer, ceiling_lines = (f ->> 'ceiling')::integer,
                   detail = coalesce(f ->> 'detail', ''), exception_id = NULL,
                   revision = revision + 1, updated_at = now_ts
             WHERE id = existing.id;
        END IF;
    END LOOP;
    UPDATE ticket_board.size_findings
       SET state = 'resolved', revision = revision + 1, updated_at = now_ts,
           candidate = p_candidate, base = p_base
     WHERE ticket_id = p_ticket AND state = 'open' AND path <> ALL (named);

    IF cardinality(opened) > 0 THEN
        PERFORM ticket_board.notify_ticket_owner_in_place_change(
            p_ticket, 'size review needed: ' || array_to_string(opened, ', '));
    END IF;
    RETURN jsonb_build_object('opened', to_jsonb(opened), 'open', (
        SELECT coalesce(jsonb_agg(path ORDER BY path), '[]'::jsonb) FROM ticket_board.size_findings
        WHERE ticket_id = p_ticket AND state = 'open'));
END;
$$;

-- A Director's bounded exception for one open finding: the ceiling is the
-- measured size of the reviewed candidate, never a number chosen freely. The
-- finding must be about the ticket's current candidate (a stale one is refused),
-- and a failed scan cannot be excepted. Standing scope makes it the file's
-- allowance on every later ticket; otherwise it is this ticket's only. Without
-- p_apply it writes nothing.
CREATE OR REPLACE FUNCTION ticket_board.approve_size_exception(
    p_ticket text,
    p_path text,
    p_rationale text,
    p_standing boolean,
    p_apply boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    who text;
    finding ticket_board.size_findings;
    current_candidate text;
    rationale text := btrim(coalesce(p_rationale, ''));
    new_exception bigint;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'merge');
    who := ticket_board.current_app_actor();
    IF rationale = '' THEN
        RAISE EXCEPTION 'a size exception needs a rationale';
    END IF;
    SELECT * INTO finding FROM ticket_board.size_findings
    WHERE ticket_id = upper(btrim(p_ticket)) AND path = p_path
    FOR UPDATE;
    IF NOT FOUND OR finding.state <> 'open' THEN
        RAISE EXCEPTION 'no open size finding for % on %', p_path, p_ticket;
    END IF;
    IF finding.reason = 'scan_failed' THEN
        RAISE EXCEPTION 'a failed size scan cannot be excepted; scan the candidate again';
    END IF;
    SELECT lower(commit_hash) INTO current_candidate FROM ticket_board.tickets WHERE id = finding.ticket_id;
    IF current_candidate IS DISTINCT FROM finding.candidate THEN
        RAISE EXCEPTION 'the finding is about %, not the ticket''s candidate %; it must be measured again',
            left(finding.candidate, 12), left(coalesce(current_candidate, ''), 12);
    END IF;
    IF NOT p_apply THEN
        RETURN jsonb_build_object('applied', false, 'ticket', finding.ticket_id, 'path', finding.path,
                                  'ceiling', finding.after_lines, 'before', finding.before_lines,
                                  'reviewed_commit', finding.candidate, 'reviewed_base', finding.base,
                                  'scope', CASE WHEN p_standing THEN 'standing' ELSE finding.ticket_id END,
                                  'by', who, 'rationale', rationale);
    END IF;
    UPDATE ticket_board.size_exceptions
       SET state = 'superseded'
     WHERE state = 'active' AND path = finding.path
       AND coalesce(scope_ticket, '') = CASE WHEN p_standing THEN '' ELSE finding.ticket_id END;
    INSERT INTO ticket_board.size_exceptions (path, ceiling_lines, reviewed_commit, reviewed_base, rationale,
                                              approved_by, scope_ticket)
    VALUES (finding.path, finding.after_lines, finding.candidate, finding.base, rationale, who,
            CASE WHEN p_standing THEN NULL ELSE finding.ticket_id END)
    RETURNING id INTO new_exception;
    UPDATE ticket_board.size_findings
       SET state = 'excepted', exception_id = new_exception,
           ceiling_lines = finding.after_lines, revision = revision + 1, updated_at = clock_timestamp()
     WHERE id = finding.id;
    PERFORM ticket_board.append_ticket_comment(
        finding.ticket_id, who,
        format('Size exception %s: %s may be up to %s lines (%s scope; reviewed %s on base %s). %s',
               new_exception, finding.path, finding.after_lines,
               CASE WHEN p_standing THEN 'standing' ELSE 'this ticket' END,
               left(finding.candidate, 12), left(coalesce(finding.base, ''), 12), rationale),
        false);
    RETURN jsonb_build_object('applied', true, 'exception', new_exception, 'ticket', finding.ticket_id,
                              'path', finding.path, 'ceiling', finding.after_lines,
                              'reviewed_commit', finding.candidate, 'reviewed_base', finding.base,
                              'scope', CASE WHEN p_standing THEN 'standing' ELSE finding.ticket_id END,
                              'by', who, 'rationale', rationale);
END;
$$;

-- Enable review, recording the baseline: the inventory the board measured at
-- that commit, and any carried-forward reviewed exceptions. Director only;
-- once. Without p_apply it writes nothing.
CREATE OR REPLACE FUNCTION ticket_board.enable_size_review(
    p_baseline_commit text,
    p_inventory jsonb,
    p_carried jsonb,
    p_apply boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    who text;
    carried jsonb;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'merge');
    who := ticket_board.current_app_actor();
    IF ticket_board.size_review_enabled() THEN
        RAISE EXCEPTION 'size review is already enabled';
    END IF;
    IF NOT p_apply THEN
        RETURN jsonb_build_object('applied', false, 'baseline', p_baseline_commit, 'by', who,
                                  'inventory', p_inventory, 'carried', coalesce(p_carried, '[]'::jsonb));
    END IF;
    INSERT INTO ticket_board.size_review_policy (enabled_by, baseline_commit, inventory)
    VALUES (who, p_baseline_commit, coalesce(p_inventory, '[]'::jsonb));
    FOR carried IN SELECT * FROM jsonb_array_elements(coalesce(p_carried, '[]'::jsonb)) LOOP
        INSERT INTO ticket_board.size_exceptions (path, ceiling_lines, reviewed_commit, reviewed_base, rationale,
                                                  approved_by, scope_ticket)
        VALUES (carried ->> 'path', (carried ->> 'ceiling')::integer, carried ->> 'reviewed_commit',
                carried ->> 'reviewed_base', carried ->> 'rationale', carried ->> 'approved_by', NULL);
    END LOOP;
    RETURN jsonb_build_object('applied', true, 'baseline', p_baseline_commit, 'by', who,
                              'inventory', p_inventory, 'carried', coalesce(p_carried, '[]'::jsonb));
END;
$$;

-- The ticket's size evidence for its JSON and the review packet.
CREATE OR REPLACE FUNCTION ticket_board.ticket_size_review(p_ticket text)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
    SELECT CASE WHEN s.ticket_id IS NULL AND NOT EXISTS (
                     SELECT 1 FROM ticket_board.size_findings f WHERE f.ticket_id = p_ticket) THEN NULL
           ELSE jsonb_build_object(
               'enabled', ticket_board.size_review_enabled(),
               'candidate', s.candidate, 'base', s.base, 'error', s.error,
               'scanned_at', s.scanned_at, 'files', coalesce(s.report, '[]'::jsonb),
               'findings', coalesce((
                   SELECT jsonb_agg(jsonb_build_object(
                       'path', f.path, 'previous_path', f.previous_path, 'reason', f.reason, 'state', f.state,
                       'candidate', f.candidate, 'before', f.before_lines, 'after', f.after_lines,
                       'ceiling', f.ceiling_lines, 'detail', f.detail, 'exception', f.exception_id,
                       'revision', f.revision) ORDER BY f.path)
                   FROM ticket_board.size_findings f WHERE f.ticket_id = p_ticket), '[]'::jsonb))
           END
    FROM (SELECT p_ticket AS id) t
    LEFT JOIN ticket_board.size_scans s ON s.ticket_id = t.id;
$$;

-- THE gate. A BEFORE UPDATE trigger of its own, beside the workflow guard: it
-- touches nothing that guard decides. Once review is enabled, a declared
-- `approve`, or a `require_commit` move into a terminal stage (mark_done; never
-- cancel), needs a recorded scan of the exact commit and no unresolved finding
-- for it. A narrated force_move and a merge's own close are not transitions it
-- judges; every other gate still applies to them as before.
CREATE OR REPLACE FUNCTION ticket_board.enforce_size_review()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
DECLARE
    cfg jsonb;
    action_name text := nullif(current_setting('ticket_board.workflow_action', true), '');
    gated boolean;
    commit_sha text := lower(NEW.commit_hash);
    scan ticket_board.size_scans;
    unresolved text;
BEGIN
    IF NOT ticket_board.size_review_enabled() OR OLD.state = NEW.state OR action_name IS NULL
       OR current_setting('ticket_board.force_move', true) = 'on'
       OR nullif(current_setting('ticket_board.merge_source', true), '') = OLD.id THEN
        RETURN NEW;
    END IF;
    cfg := ticket_board.declared_workflow();
    IF cfg IS NULL THEN
        RETURN NEW;
    END IF;
    -- Matched on the action and the stage it leaves, never on the declared
    -- destination: an approval can land past it (a gate's skip), and a check
    -- that wanted the two equal would wave exactly that approval through.
    SELECT coalesce(bool_or(x ->> 'primitive' = 'approve'), false)
        OR (coalesce(bool_or(coalesce((x ->> 'require_commit')::boolean, false)), false)
            AND coalesce((SELECT (s ->> 'terminal')::boolean FROM jsonb_array_elements(cfg -> 'stages') s
                          WHERE s ->> 'name' = NEW.state), false))
      INTO gated
      FROM jsonb_array_elements(cfg -> 'transitions') x
     WHERE x ->> 'action' = action_name AND x ->> 'from' = OLD.state;
    IF NOT gated THEN
        RETURN NEW;
    END IF;
    IF coalesce(commit_sha, '') = '' THEN
        -- No code, legitimately: a commit-exempt ticket carries nothing to
        -- measure, and its other gates decide it as they always have (SYRD-267).
        -- Exempt only while there is no commit -- one that carries a commit is
        -- measured like any other.
        IF NEW.commit_exempt THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'size review: % needs a candidate commit to measure', action_name USING ERRCODE = '42501';
    END IF;
    -- Authoritative and current: the board's own completed scan of this exact
    -- commit, made just now. The board measures immediately before every gated
    -- action, so an older record -- a stale or concurrent earlier scan, or a
    -- path that skipped the measurement -- cannot stand in for it.
    SELECT * INTO scan FROM ticket_board.size_scans WHERE ticket_id = NEW.id;
    IF NOT FOUND OR scan.candidate <> commit_sha THEN
        RAISE EXCEPTION 'size review: commit % has not been measured; the board measures it when the action is taken',
            left(commit_sha, 12) USING ERRCODE = '42501';
    END IF;
    IF scan.scanned_at < clock_timestamp() - interval '10 minutes' THEN
        RAISE EXCEPTION 'size review: the measurement of % is from %, not this action; take the action through the board, which measures it again',
            left(commit_sha, 12), scan.scanned_at USING ERRCODE = '42501';
    END IF;
    SELECT string_agg(format('%s (%s%s)', f.path, f.reason,
                             CASE WHEN f.after_lines IS NULL THEN '' ELSE format(': %s -> %s lines', f.before_lines, f.after_lines) END),
                      '; ' ORDER BY f.path)
      INTO unresolved
      FROM ticket_board.size_findings f
     WHERE f.ticket_id = NEW.id AND f.state = 'open';
    IF unresolved IS NOT NULL THEN
        RAISE EXCEPTION 'size review: unresolved size findings for %: %. Split or reduce, or the Director approves a bounded exception (approve-size-exception)',
            left(commit_sha, 12), unresolved USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tickets_size_review ON ticket_board.tickets;
CREATE TRIGGER tickets_size_review
    BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.enforce_size_review();

REVOKE EXECUTE ON FUNCTION ticket_board.size_review_enabled() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.size_ceilings(text, text[]) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.record_size_scan(text, text, text, jsonb, jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.approve_size_exception(text, text, text, boolean, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.enable_size_review(text, jsonb, jsonb, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_size_review(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.enforce_size_review() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ticket_board.size_review_enabled() TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.size_ceilings(text, text[]) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.record_size_scan(text, text, text, jsonb, jsonb, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.approve_size_exception(text, text, text, boolean, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.enable_size_review(text, jsonb, jsonb, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_size_review(text) TO ticket_board_service;

COMMIT;
