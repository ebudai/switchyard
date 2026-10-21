-- SYRD-625: the Publisher, a deterministic system actor that publishes an
-- approved candidate to the integration branch. schema.sql carries the same
-- table and functions; rbac.sql the same privileges.
BEGIN;

-- One row per publication attempt: what the Publisher set out to publish, how
-- far it got, and how it ended. A ticket has at most one open attempt, and
-- closing it -- published, returned, escalated or superseded -- is the same
-- statement as the transition that hands the ticket on, so the three handoffs
-- exclude each other and none happens twice (publisher_finish).
--
-- Keyed by the ticket's assignment round (ticket_notification_state
-- .current_assignment_at): a ticket that leaves publication and comes back is
-- a new round, and an open attempt from an older round is superseded rather
-- than resumed. Within a round a re-run resumes the open attempt, so a crash
-- between journal steps repeats work from facts instead of starting over.
CREATE TABLE IF NOT EXISTS ticket_board.publication_attempts (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    round_at timestamptz NOT NULL,
    -- The ticket's recorded commit when the attempt began: the approved candidate.
    candidate text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open', 'published', 'returned', 'escalated', 'superseded')),
    step text NOT NULL DEFAULT 'started'
        CHECK (step IN ('started', 'prepared', 'pushing', 'pushed', 'verified')),
    -- The public tip the candidate was checked against, and the push leased on.
    expected_parent text NOT NULL DEFAULT '' CHECK (expected_parent = '' OR expected_parent ~ '^[0-9a-f]{40}$'),
    facts jsonb NOT NULL DEFAULT '{}'::jsonb,
    repairs jsonb NOT NULL DEFAULT '[]'::jsonb,
    detail text NOT NULL DEFAULT '',
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS publication_attempts_one_open
    ON ticket_board.publication_attempts (ticket_id) WHERE state = 'open';

-- Whether this board's workflow hands publication to the Publisher, declared
-- as composed: a publication stage the publisher alone owns, and a publisher
-- that is an active system role with no runtime, pane target or capability.
-- workflow_config.validate_publisher holds the full shape; this is what the
-- functions below refuse to act without.
CREATE OR REPLACE FUNCTION ticket_board.publisher_declared(cfg jsonb)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT cfg IS NOT NULL
       AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
                   WHERE s->>'name' = 'publication' AND s->'owners' = '["publisher"]'::jsonb)
       AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
                   WHERE r->>'name' = 'publisher' AND r->>'kind' = 'system' AND (r->>'active')::boolean
                     AND coalesce(r->'capabilities', '[]'::jsonb) = '[]'::jsonb
                     AND coalesce(r->>'runtime', '') = '' AND coalesce(r->>'target', '') = '');
$$;

-- Every review sign-off the ticket owes before publication and does not have:
-- the reviews that lead into the publication stage -- by an approval or move,
-- or by a gate skip -- and the ones that lead into those, back to
-- Implementation, each owed when it has a sign-off and its gate is on (or it
-- has no gate). The same reviews the gate walk could have stopped at.
CREATE OR REPLACE FUNCTION ticket_board.publication_missing_signoffs(cfg jsonb, doc jsonb)
RETURNS text[] LANGUAGE plpgsql STABLE AS $$
DECLARE reached text[] := ARRAY['publication']; grown boolean := true; missing text[] := ARRAY[]::text[]; s jsonb;
BEGIN
    WHILE grown LOOP
        grown := false;
        FOR s IN SELECT x FROM jsonb_array_elements(cfg->'stages') x
                 WHERE x->>'kind' = 'review' AND NOT (x->>'name' = ANY(reached))
                   AND (x->>'skip_to' = ANY(reached)
                        OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
                                   WHERE t->>'from' = x->>'name' AND t->>'to' = ANY(reached)
                                     AND t->>'primitive' IN ('approve', 'move')))
        LOOP
            reached := reached || (s->>'name');
            grown := true;
            IF s->>'signoff' IS NOT NULL
               AND (s->>'gate' IS NULL OR ticket_board.workflow_flag(doc, s->>'gate', cfg))
               AND NOT ticket_board.workflow_flag(doc, s->>'signoff', cfg) THEN
                missing := missing || (s->>'signoff');
            END IF;
        END LOOP;
    END LOOP;
    RETURN (SELECT coalesce(array_agg(m ORDER BY m), ARRAY[]::text[]) FROM unnest(missing) m);
END;
$$;

-- What size review says about this ticket's exact candidate: '' when it has
-- nothing to say against publishing it, otherwise why not. Off, or no commit
-- (a no-code ticket), is nothing to say.
CREATE OR REPLACE FUNCTION ticket_board.publication_size_problem(p_ticket text, p_candidate text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE scan ticket_board.size_scans; unresolved text;
BEGIN
    IF NOT ticket_board.size_review_enabled() OR btrim(coalesce(p_candidate, '')) = '' THEN
        RETURN '';
    END IF;
    SELECT * INTO scan FROM ticket_board.size_scans WHERE ticket_id = p_ticket;
    IF NOT FOUND OR scan.candidate <> lower(p_candidate) THEN
        RETURN format('size review has no measurement of %s', left(p_candidate, 12));
    END IF;
    IF scan.error IS NOT NULL THEN
        RETURN format('size review could not measure %s: %s', left(p_candidate, 12), scan.error);
    END IF;
    SELECT string_agg(format('%s (%s)', f.path, f.reason), '; ' ORDER BY f.path) INTO unresolved
      FROM ticket_board.size_findings f WHERE f.ticket_id = p_ticket AND f.state = 'open';
    RETURN CASE WHEN unresolved IS NULL THEN ''
                ELSE format('unresolved size findings for %s: %s', left(p_candidate, 12), unresolved) END;
END;
$$;

-- Whether `p_ticket` is the Publisher's to work on now: in publication,
-- assigned to the Publisher, not held by the Director, not parked, and not
-- waiting on a blocker -- a blocked ticket waits where it is, and the
-- Publisher neither publishes it nor hands it anywhere.
CREATE OR REPLACE FUNCTION ticket_board.publication_ready(t ticket_board.tickets)
RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT t.state = 'publication' AND t.assignee = 'publisher' AND NOT t.manually_controlled AND NOT t.parked
       AND NOT ticket_board.ticket_has_unresolved_blockers(t.id);
$$;

-- An open attempt that no longer describes its ticket -- it left publication,
-- its round or recorded commit changed -- is bookkeeping to close, never work
-- to resume. Returns how many were closed.
CREATE OR REPLACE FUNCTION ticket_board.supersede_publication_attempts(p_ticket text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE closed integer;
BEGIN
    UPDATE ticket_board.publication_attempts a
       SET state = 'superseded', finished_at = clock_timestamp(), updated_at = clock_timestamp(),
           detail = 'superseded: the ticket is no longer this attempt''s (it left publication, or its round or commit changed)'
      FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
     WHERE a.state = 'open' AND a.ticket_id = t.id AND (p_ticket IS NULL OR a.ticket_id = p_ticket)
       AND (t.state <> 'publication' OR t.assignee <> 'publisher' OR ns.current_assignment_at <> a.round_at
            OR coalesce(t.commit_hash, '') <> a.candidate);
    GET DIAGNOSTICS closed = ROW_COUNT;
    RETURN closed;
END;
$$;

-- The tickets the Publisher may work on, oldest in the stage first, after
-- closing attempts that no longer match their ticket. Empty on a board
-- without the Publisher.
CREATE OR REPLACE FUNCTION ticket_board.publisher_pending()
RETURNS SETOF text LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('publisher_pending');
    IF NOT ticket_board.publisher_declared(ticket_board.declared_workflow()) THEN
        RETURN;
    END IF;
    PERFORM ticket_board.supersede_publication_attempts(NULL);
    RETURN QUERY
        SELECT t.id FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
         WHERE ticket_board.publication_ready(t)
         ORDER BY ns.entered_current_state_at, t.ticket_number;
END;
$$;

-- Begin, or resume, the attempt for one ticket, and say everything the
-- Publisher needs to know from the board: the candidate, the round, the open
-- attempt's step and journal, which sign-offs are missing, and what size
-- review says. A ticket that is not ready answers {"ready": false} and gets
-- no attempt.
CREATE OR REPLACE FUNCTION ticket_board.publisher_begin(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); t ticket_board.tickets; round timestamptz;
    attempt ticket_board.publication_attempts; resumed boolean := false;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('publisher_begin');
    IF NOT ticket_board.publisher_declared(cfg) THEN
        RAISE EXCEPTION 'this board''s workflow does not declare the Publisher' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND OR NOT ticket_board.publication_ready(t) THEN
        RETURN jsonb_build_object('ready', false);
    END IF;
    SELECT current_assignment_at INTO round FROM ticket_board.ticket_notification_state WHERE ticket_id = t.id;
    PERFORM ticket_board.supersede_publication_attempts(t.id);
    SELECT * INTO attempt FROM ticket_board.publication_attempts WHERE ticket_id = t.id AND state = 'open';
    IF FOUND THEN
        resumed := true;
    ELSE
        INSERT INTO ticket_board.publication_attempts (ticket_id, round_at, candidate)
        VALUES (t.id, round, coalesce(t.commit_hash, '')) RETURNING * INTO attempt;
    END IF;
    RETURN jsonb_build_object(
        'ready', true, 'attempt', attempt.id, 'resumed', resumed, 'ticket', t.id, 'candidate', attempt.candidate,
        'commit_exempt', t.commit_exempt, 'step', attempt.step, 'expected_parent', attempt.expected_parent,
        'facts', attempt.facts, 'repairs', attempt.repairs,
        'missing_signoffs', to_jsonb(ticket_board.publication_missing_signoffs(cfg, to_jsonb(t))),
        'size_problem', ticket_board.publication_size_problem(
            t.id, coalesce(nullif(attempt.facts->>'candidate_full', ''), t.commit_hash)),
        'size_review', ticket_board.size_review_enabled(),
        'continuation', (SELECT x->>'to' FROM jsonb_array_elements(cfg->'transitions') x
                         WHERE x->>'action' = 'publication_published' AND x->>'from' = 'publication'));
END;
$$;

-- The open attempt for `p_attempt`, locked with its ticket, when it still
-- describes the ticket; NULL (and the attempt closed as superseded) when not.
CREATE OR REPLACE FUNCTION ticket_board.publication_attempt_current(p_attempt bigint)
RETURNS ticket_board.publication_attempts LANGUAGE plpgsql AS $$
DECLARE attempt ticket_board.publication_attempts; t ticket_board.tickets;
BEGIN
    SELECT * INTO attempt FROM ticket_board.publication_attempts WHERE id = p_attempt;
    IF NOT FOUND OR attempt.state <> 'open' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = attempt.ticket_id FOR UPDATE;
    SELECT * INTO attempt FROM ticket_board.publication_attempts WHERE id = p_attempt FOR UPDATE;
    PERFORM ticket_board.supersede_publication_attempts(t.id);
    SELECT * INTO attempt FROM ticket_board.publication_attempts WHERE id = p_attempt;
    IF attempt.state <> 'open' THEN
        RETURN NULL;
    END IF;
    RETURN attempt;
END;
$$;

-- Journal one step forward (or stay on it), with what was learnt and repaired.
-- Refused, as 'superseded', when the attempt no longer describes its ticket;
-- the Publisher then stops without pushing.
CREATE OR REPLACE FUNCTION ticket_board.publisher_step(
    p_attempt bigint, p_step text, p_expected_parent text, p_facts jsonb, p_repairs jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE attempt ticket_board.publication_attempts;
    steps text[] := ARRAY['started', 'prepared', 'pushing', 'pushed', 'verified'];
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('publisher_step');
    attempt := ticket_board.publication_attempt_current(p_attempt);
    IF attempt.id IS NULL THEN
        RAISE EXCEPTION 'superseded: publication attempt % no longer describes its ticket', p_attempt
            USING ERRCODE = '40001';
    END IF;
    IF array_position(steps, p_step) IS NULL OR array_position(steps, p_step) < array_position(steps, attempt.step) THEN
        RAISE EXCEPTION 'publication journal: % cannot follow %', p_step, attempt.step USING ERRCODE = '22023';
    END IF;
    UPDATE ticket_board.publication_attempts
       SET step = p_step, expected_parent = coalesce(nullif(p_expected_parent, ''), expected_parent),
           facts = facts || coalesce(p_facts, '{}'::jsonb), repairs = coalesce(p_repairs, repairs),
           updated_at = clock_timestamp()
     WHERE id = p_attempt;
END;
$$;

-- End the attempt, and with it hand the ticket on, once: 'published' takes
-- publication_published (and the ordinary gate walk on from there),
-- 'returned' takes publication_return to the recorded implementer with every
-- sign-off cleared, and 'escalated' takes publication_escalate to the
-- Director. `p_text` is the Publisher's comment -- the evidence, or the exact
-- diagnostic -- written in the same transaction.
--
-- An attempt that no longer describes its ticket is closed as superseded and
-- moves nothing. When the Publisher had already published, its evidence is
-- still recorded on the ticket, so a publication is never silent.
CREATE OR REPLACE FUNCTION ticket_board.publisher_finish(
    p_attempt bigint, p_outcome text, p_text text, p_facts jsonb, p_repairs jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow(); attempt ticket_board.publication_attempts;
    t ticket_board.tickets; action_name text; tr jsonb; problem text; missing text[]; verified text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('publisher_finish');
    action_name := CASE p_outcome WHEN 'published' THEN 'publication_published' WHEN 'returned' THEN 'publication_return'
                                  WHEN 'escalated' THEN 'publication_escalate' END;
    IF action_name IS NULL OR btrim(coalesce(p_text, '')) = '' THEN
        RAISE EXCEPTION 'publisher_finish needs an outcome (published, returned or escalated) and its text'
            USING ERRCODE = '22023';
    END IF;
    IF NOT ticket_board.publisher_declared(cfg) THEN
        RAISE EXCEPTION 'this board''s workflow does not declare the Publisher' USING ERRCODE = '42501';
    END IF;
    attempt := ticket_board.publication_attempt_current(p_attempt);
    IF attempt.id IS NULL THEN
        SELECT * INTO attempt FROM ticket_board.publication_attempts WHERE id = p_attempt;
        IF FOUND AND p_outcome = 'published' AND attempt.state = 'superseded' THEN
            PERFORM ticket_board.append_ticket_comment(attempt.ticket_id, 'publisher',
                'The ticket left publication before this could be recorded, so it was not moved. ' || p_text);
        END IF;
        RETURN jsonb_build_object('recorded', false, 'superseded', true);
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = attempt.ticket_id;
    IF NOT ticket_board.publication_ready(t) THEN
        RAISE EXCEPTION '% is not ready for the Publisher (held, parked or blocked); nothing was recorded', t.id
            USING ERRCODE = '55000';
    END IF;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = 'publication' AND x->>'action' = action_name AND x->'actors' = '["publisher"]'::jsonb;
    IF tr IS NULL THEN
        RAISE EXCEPTION 'this board''s workflow does not declare %', action_name USING ERRCODE = '42501';
    END IF;
    IF p_outcome = 'published' THEN
        -- Recorded only for what was verified: the journal reached `verified`
        -- for this commit, or there is no commit to publish at all.
        IF NOT (attempt.step = 'verified' OR (t.commit_exempt AND btrim(coalesce(t.commit_hash, '')) = '')) THEN
            RAISE EXCEPTION 'publication of % is not verified (journal step %); nothing was recorded', t.id, attempt.step
                USING ERRCODE = '55000';
        END IF;
        verified := lower(coalesce(nullif(coalesce(p_facts, '{}'::jsonb)->>'candidate_full', ''),
                                   nullif(attempt.facts->>'candidate_full', ''), ''));
        IF btrim(coalesce(t.commit_hash, '')) <> '' AND NOT (verified ~ '^[0-9a-f]{40}$'
                                                            AND left(verified, length(t.commit_hash)) = lower(t.commit_hash)) THEN
            RAISE EXCEPTION 'the verified commit % is not %''s recorded candidate %; nothing was recorded',
                coalesce(nullif(verified, ''), 'none'), t.id, t.commit_hash USING ERRCODE = '42501';
        END IF;
        missing := ticket_board.publication_missing_signoffs(cfg, to_jsonb(t));
        IF cardinality(missing) > 0 THEN
            RAISE EXCEPTION '% is missing sign-off %; the Publisher does not waive a gate', t.id,
                array_to_string(missing, ', ') USING ERRCODE = '42501';
        END IF;
        problem := ticket_board.publication_size_problem(t.id, nullif(verified, ''));
        IF problem <> '' THEN
            RAISE EXCEPTION '%', problem USING ERRCODE = '42501';
        END IF;
    END IF;
    PERFORM ticket_board.append_ticket_comment(t.id, 'publisher', p_text);
    PERFORM set_config('ticket_board.publisher_ticket', t.id, true);
    PERFORM set_config('ticket_board.workflow_action', action_name, true);
    PERFORM set_config('ticket_board.workflow_actor', 'publisher', true);
    UPDATE ticket_board.tickets SET state = tr->>'to' WHERE id = t.id;
    PERFORM set_config('ticket_board.workflow_action', '', true);
    PERFORM set_config('ticket_board.workflow_actor', '', true);
    PERFORM set_config('ticket_board.publisher_ticket', '', true);
    SELECT * INTO t FROM ticket_board.tickets WHERE id = t.id;
    IF t.state = 'publication' THEN
        RAISE EXCEPTION '% did not leave publication', t.id USING ERRCODE = '55000';
    END IF;
    UPDATE ticket_board.publication_attempts
       SET state = CASE p_outcome WHEN 'published' THEN 'published' WHEN 'returned' THEN 'returned' ELSE 'escalated' END,
           facts = facts || coalesce(p_facts, '{}'::jsonb), repairs = coalesce(p_repairs, repairs),
           detail = left(p_text, 4000), finished_at = clock_timestamp(), updated_at = clock_timestamp()
     WHERE id = attempt.id;
    RETURN jsonb_build_object('recorded', true, 'state', t.state, 'assignee', t.assignee);
END;
$$;

-- The Publisher's moves are taken by the Publisher, through publisher_finish,
-- and by nothing else: not a pane or HTTP caller naming itself `publisher`, not
-- a relay, a recovery or a delegated board-UI action. Its window names one
-- ticket and is open for one statement. A Director's narrated override
-- (force_move) is not a Publisher move and is not affected.
CREATE OR REPLACE FUNCTION ticket_board.enforce_publisher_window()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (current_setting('ticket_board.workflow_action', true)
            IN ('publication_published', 'publication_return', 'publication_escalate')
        OR current_setting('ticket_board.workflow_actor', true) = 'publisher'
        OR current_setting('ticket_board.caller_role', true) = 'publisher')
       AND nullif(current_setting('ticket_board.publisher_ticket', true), '') IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'only the Publisher takes publication moves, through ticket_board.publisher_finish'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS tickets_publisher_window ON ticket_board.tickets;
CREATE TRIGGER tickets_publisher_window BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.enforce_publisher_window();

REVOKE ALL ON ticket_board.publication_attempts FROM PUBLIC;
REVOKE ALL ON SEQUENCE ticket_board.publication_attempts_id_seq FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publisher_pending() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publisher_begin(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publisher_step(bigint, text, text, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publisher_finish(bigint, text, text, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.supersede_publication_attempts(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publication_attempt_current(bigint) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publisher_declared(jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publication_missing_signoffs(jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publication_size_problem(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.publication_ready(ticket_board.tickets) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.enforce_publisher_window() FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT SELECT ON ticket_board.publication_attempts TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.publisher_pending() TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.publisher_begin(text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.publisher_step(bigint, text, text, jsonb, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.publisher_finish(bigint, text, text, jsonb, jsonb) TO ticket_board_listener;

COMMIT;
