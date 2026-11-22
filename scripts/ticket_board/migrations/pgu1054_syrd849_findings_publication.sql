-- SYRD-849 (pgu1054): a research finding with a commit reaches main through the Publisher.
--
-- SYRD-842's close_on_finding moved a finding from triage to done, so a findings commit lived only on its researcher's
-- roles/ ref and the research directory on main stayed empty. On a board that runs the Publisher the research stage
-- now composes (research_stage.py): the Director's publish_finding, triage into publication; the Publisher's
-- finding_published, on to done, and finding_publication_return, back to the researcher; and the Director's
-- finding_publication_kick_back from publication_failed, back to the researcher. What the declaration cannot say:
--
-- - publish_finding is for a research ticket whose submitted finding has a commit, and publishes exactly that commit;
--   close_on_finding is refused for such a finding where publish_finding is declared (a no-commit finding closes as
--   before); the ordinary publication_kick_back is refused for a finding, and the finding's kick-back for anything
--   else (require_research_brief).
-- - publish_finding marks the round's finding as accepted for publication (note_research_round), which
--   publication_is_finding reads; entering Research again starts a round with no finding, so no mark.
-- - For such a ticket the Publisher's outcome takes the finding's moves (publisher_finish), and no Audit code sign-off
--   is asked of it: its review was research_review, which holds no sign-off flag (publication_missing_signoffs).
-- - The Publisher's window covers the finding's two Publisher moves (enforce_publisher_window).
--
-- schema.sql carries the same definitions at its tail.

BEGIN;

-- Whether the ticket is a finding the Director accepted for publication (publish_finding) this round.
CREATE OR REPLACE FUNCTION ticket_board.publication_is_finding(p_ticket text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT ticket_board.research_declared() AND EXISTS (
        SELECT FROM ticket_board.ticket_research_briefs b JOIN ticket_board.tickets t ON t.id = b.ticket_id
         WHERE b.ticket_id = p_ticket AND b.finding ? 'publication'
           AND ticket_board.workflow_flag(to_jsonb(t), 'needs_research', ticket_board.declared_workflow()));
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.publication_is_finding(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION ticket_board.require_research_brief()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb; action text; flagged boolean; brief ticket_board.ticket_research_briefs; committed boolean;
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state OR NOT ticket_board.research_declared() THEN
        RETURN NEW;
    END IF;
    cfg := ticket_board.declared_workflow();
    flagged := ticket_board.workflow_flag(to_jsonb(NEW), 'needs_research', cfg);
    action := nullif(current_setting('ticket_board.workflow_action', true), '');
    IF action IN ('return_finding', 'close_on_finding', 'publish_finding', 'finding_publication_kick_back')
       AND NOT flagged THEN
        RAISE EXCEPTION '% is for research tickets, and % is not one', action, NEW.id;
    END IF;
    -- SYRD-849: a finding with a commit reaches main through the Publisher, where the board runs it; the finding it
    -- publishes is the one submitted; and a finding in publication goes back to its researcher, never to Implementation.
    SELECT * INTO brief FROM ticket_board.ticket_research_briefs WHERE ticket_id = NEW.id;
    committed := brief.finding->>'action' = 'submit_finding' AND btrim(coalesce(NEW.commit_hash, '')) <> '';
    IF action = 'publish_finding' AND NOT committed THEN
        RAISE EXCEPTION '% has no findings commit to publish: close it on its finding with ticket-board-write close-on-finding % --reason ...',
            NEW.id, NEW.id;
    END IF;
    IF action = 'publish_finding' AND lower(NEW.commit_hash) <> lower(brief.finding->>'commit_hash') THEN
        RAISE EXCEPTION '%''s commit % is not the findings commit its researcher submitted (%); nothing was published',
            NEW.id, NEW.commit_hash, brief.finding->>'commit_hash';
    END IF;
    IF action = 'close_on_finding' AND committed AND EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'action' = 'publish_finding') THEN
        RAISE EXCEPTION '%''s finding has a commit, and this board publishes it through the Publisher before it is done: ticket-board-write publish-finding % --reason ...',
            NEW.id, NEW.id;
    END IF;
    IF action = 'publication_kick_back' AND ticket_board.publication_is_finding(NEW.id) THEN
        RAISE EXCEPTION '% is a finding: it goes back to its researcher with ticket-board-write finding-publication-kick-back % --reason ...',
            NEW.id, NEW.id;
    END IF;
    IF action = 'finding_publication_kick_back' AND NOT ticket_board.publication_is_finding(NEW.id) THEN
        RAISE EXCEPTION '% was not accepted for publication as a finding; finding-publication-kick-back is not its move', NEW.id;
    END IF;
    -- A no-commit finding replaces any earlier round's findings commit: the candidate never names a finding the
    -- researcher replaced.
    IF OLD.state = 'research' AND action = 'submit_finding_without_commit' THEN
        NEW.commit_hash := '';
    END IF;
    IF NEW.state = 'research' THEN
        IF NOT flagged THEN
            RAISE EXCEPTION '% is not a research ticket, so it cannot enter Research. The Director makes it one with its brief: ticket-board-write research-brief % --question ... --timebox ... --usable-answer ...',
                NEW.id, NEW.id;
        END IF;
        IF NOT EXISTS (SELECT FROM ticket_board.ticket_research_briefs b WHERE b.ticket_id = NEW.id) THEN
            RAISE EXCEPTION 'a research brief is required before % enters Research: the question, a timebox and what a usable answer looks like. The Director writes it with: ticket-board-write research-brief % --question ... --timebox ... --usable-answer ...',
                NEW.id, NEW.id;
        END IF;
    ELSIF OLD.state = ticket_board.research_triage() AND flagged
          AND ticket_board.declared_stage_kind(NEW.state) = 'implementation' THEN
        RAISE EXCEPTION '% is a research ticket: start it with ticket-board-write start-research %, or turn its needs_research gate off first to implement it here',
            NEW.id, NEW.id;
    END IF;
    RETURN NEW;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.note_research_round()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE action text := nullif(current_setting('ticket_board.workflow_action', true), '');
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state OR NOT ticket_board.research_declared() THEN
        RETURN NULL;
    END IF;
    IF NEW.state = 'research' THEN
        UPDATE ticket_board.ticket_research_briefs
           SET round_started_at = clock_timestamp(), timebox_noticed_at = NULL, timebox_notification_id = NULL,
               finding = NULL
         WHERE ticket_id = NEW.id;
    ELSIF NEW.state = 'publication' AND action = 'publish_finding' THEN
        -- SYRD-849: the Director accepted this finding for publication; the Publisher's moves for it are the finding's.
        UPDATE ticket_board.ticket_research_briefs
           SET finding = finding || jsonb_build_object('publication', jsonb_build_object(
                   'by', coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''), 'director'),
                   'at', clock_timestamp()))
         WHERE ticket_id = NEW.id;
    ELSIF OLD.state = 'research' AND action IN ('submit_finding', 'submit_finding_without_commit') THEN
        UPDATE ticket_board.ticket_research_briefs
           SET finding = jsonb_build_object('action', action,
                   'commit_hash', CASE WHEN action = 'submit_finding' THEN NEW.commit_hash ELSE '' END,
                   'actor', coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''), OLD.assignee),
                   'at', clock_timestamp())
         WHERE ticket_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;


CREATE OR REPLACE FUNCTION ticket_board.publication_missing_signoffs(cfg jsonb, doc jsonb)
RETURNS text[] LANGUAGE plpgsql STABLE AS $$
DECLARE reached text[] := ARRAY['publication']; grown boolean := true; missing text[] := ARRAY[]::text[]; s jsonb;
BEGIN
    -- SYRD-849: a finding the Director accepted for publication was reviewed in research_review, which holds no
    -- sign-off flag, and decided by that acceptance; Audit's code gates are not its.
    IF doc->>'state' = 'publication' AND ticket_board.publication_is_finding(doc->>'id') THEN
        RETURN ARRAY[]::text[];
    END IF;
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
    -- SYRD-849: a finding the Director accepted for publication takes the finding's moves: on to done when
    -- published, back to its researcher when returned.
    IF ticket_board.publication_is_finding(t.id) THEN
        action_name := CASE p_outcome WHEN 'published' THEN 'finding_published'
                                      WHEN 'returned' THEN 'finding_publication_return' ELSE action_name END;
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


CREATE OR REPLACE FUNCTION ticket_board.enforce_publisher_window()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (current_setting('ticket_board.workflow_action', true)
            IN ('publication_published', 'publication_return', 'publication_escalate', 'finding_published',
                'finding_publication_return')
        OR current_setting('ticket_board.workflow_actor', true) = 'publisher'
        OR current_setting('ticket_board.caller_role', true) = 'publisher')
       AND nullif(current_setting('ticket_board.publisher_ticket', true), '') IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'only the Publisher takes publication moves, through ticket_board.publisher_finish'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$;


COMMIT;
