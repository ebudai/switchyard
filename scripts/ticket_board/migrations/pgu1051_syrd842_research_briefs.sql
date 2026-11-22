-- SYRD-842 (pgu1051): a research ticket's brief, the refusals that make it one, its timebox notice and its finding.
--
-- The research stage (scripts/ticket_board/research_stage.py) is a declared workflow composer: its stages and
-- transitions need nothing new from the executor. What the declaration cannot say is kept here, only on a board whose
-- declared workflow has the `research` stage and the `needs_research` gate; every other board is unchanged.
--
-- - ticket_research_briefs: the Director's brief for a research ticket -- the question, a timebox and what a usable
--   answer looks like, all three required -- written with set_research_brief, which also turns the ticket's
--   needs_research gate on. Each write is a Director comment.
-- - A ticket enters Research only with needs_research on and a brief, by any path; a ticket that carries the flag is
--   not routed from triage into Implementation; and return_finding and close_on_finding are for research tickets
--   only. Each refusal names the command that fixes it (require_research_brief), which also clears an earlier
--   round's findings commit when a no-commit finding leaves Research.
-- - Entering Research starts a round: its timebox clock, a fresh notice and no finding yet. A submission out of
--   Research records the finding -- which form, and the commit -- for the notices that hand it on
--   (note_research_round).
-- - note_research_timeboxes, run by the listener each pass: a research ticket whose timebox passed in Research
--   with no submission tells the Director once per round. The board stops nothing. The notice is current while the
--   ticket is still in that round (research_timebox_notice_is_current).
-- - ticket_research_handoff, the listener's line for a transition notice: the brief for the researcher, and the
--   finding for Audit's review and for the Director when the finding comes back.
--
-- schema.sql carries the same objects; rbac.sql grants the setter to the service and the two reads to the listener.

BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_research_briefs (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    question text NOT NULL CHECK (btrim(question) <> '' AND length(question) <= 2000),
    timebox_seconds integer NOT NULL CHECK (timebox_seconds BETWEEN 900 AND 2592000),
    usable_answer text NOT NULL CHECK (btrim(usable_answer) <> '' AND length(usable_answer) <= 2000),
    written_by text NOT NULL,
    written_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- The current round: when the ticket last entered Research, and whether the Director was told its timebox passed.
    round_started_at timestamptz,
    timebox_noticed_at timestamptz,
    timebox_notification_id bigint,
    -- The last submission out of Research this round: {"action", "commit_hash", "actor", "at"}.
    finding jsonb
);

-- Whether this board runs the research stage: its declared workflow has the stage and the gate.
CREATE OR REPLACE FUNCTION ticket_board.research_declared()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(ticket_board.declared_workflow()->'flags'->'needs_research'->>'kind' = 'gate'
                    AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                                 WHERE x->>'name' = 'research'), false);
$$;

-- The stage start_research leaves: where research tickets are triaged and findings come back.
CREATE OR REPLACE FUNCTION ticket_board.research_triage()
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT x->>'from' FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
     WHERE x->>'action' = 'start_research' AND x->>'to' = 'research' LIMIT 1;
$$;

-- "2h", "90m", "1d 6h": a timebox as the brief and the notices say it.
CREATE OR REPLACE FUNCTION ticket_board.research_timebox_text(p_seconds integer)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(nullif(concat_ws(' ',
        CASE WHEN p_seconds >= 86400 THEN (p_seconds / 86400) || 'd' END,
        CASE WHEN p_seconds % 86400 >= 3600 THEN (p_seconds % 86400 / 3600) || 'h' END,
        CASE WHEN p_seconds % 3600 >= 60 THEN (p_seconds % 3600 / 60) || 'm' END), ''), p_seconds || 's');
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_research_brief()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE cfg jsonb; action text; flagged boolean;
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state OR NOT ticket_board.research_declared() THEN
        RETURN NEW;
    END IF;
    cfg := ticket_board.declared_workflow();
    flagged := ticket_board.workflow_flag(to_jsonb(NEW), 'needs_research', cfg);
    action := nullif(current_setting('ticket_board.workflow_action', true), '');
    IF action IN ('return_finding', 'close_on_finding') AND NOT flagged THEN
        RAISE EXCEPTION '% is for research tickets, and % is not one', action, NEW.id;
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

DROP TRIGGER IF EXISTS tickets_zzzzzy_research_brief_required ON ticket_board.tickets;
-- An update only, like UAT instructions: no ticket is created in Research.
CREATE TRIGGER tickets_zzzzzy_research_brief_required BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.require_research_brief();

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

DROP TRIGGER IF EXISTS tickets_zz_research_round ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_research_round AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.note_research_round();

-- The Director writes (or rewrites) a ticket's research brief, which makes it a research ticket.
CREATE OR REPLACE FUNCTION ticket_board.set_research_brief(
    p_ticket text, p_question text, p_timebox_seconds integer, p_usable_answer text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    who text;
    ticket ticket_board.tickets;
    question text := btrim(coalesce(p_question, ''));
    answer text := btrim(coalesce(p_usable_answer, ''));
    saved ticket_board.ticket_research_briefs;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    IF NOT ticket_board.research_declared() THEN
        RAISE EXCEPTION 'this board has no research stage; the Director turns it on with: switchyard research-stage <project> enable';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                  WHERE x->>'name' = ticket.state), false) THEN
        RAISE EXCEPTION '% is %: there is no research to brief', p_ticket, ticket.state;
    END IF;
    IF question = '' THEN
        RAISE EXCEPTION 'a research ticket needs the question it answers: --question is blank';
    END IF;
    IF answer = '' THEN
        RAISE EXCEPTION 'a research brief says what a usable answer looks like: --usable-answer is blank';
    END IF;
    IF length(question) > 2000 OR length(answer) > 2000 THEN
        RAISE EXCEPTION 'keep the question and the usable answer short (2000 characters each at most) and put detail in the body';
    END IF;
    IF p_timebox_seconds IS NULL OR p_timebox_seconds NOT BETWEEN 900 AND 2592000 THEN
        RAISE EXCEPTION 'a research timebox is from 15 minutes to 30 days, not %',
            coalesce(ticket_board.research_timebox_text(p_timebox_seconds), 'none');
    END IF;
    INSERT INTO ticket_board.ticket_research_briefs AS b
        (ticket_id, question, timebox_seconds, usable_answer, written_by, written_at)
    VALUES (p_ticket, question, p_timebox_seconds, answer, who, clock_timestamp())
    ON CONFLICT (ticket_id) DO UPDATE
       SET question = EXCLUDED.question, timebox_seconds = EXCLUDED.timebox_seconds,
           usable_answer = EXCLUDED.usable_answer, written_by = EXCLUDED.written_by, written_at = EXCLUDED.written_at
    RETURNING * INTO saved;
    IF NOT ticket_board.workflow_flag(to_jsonb(ticket), 'needs_research', ticket_board.declared_workflow()) THEN
        UPDATE ticket_board.tickets
           SET workflow_flags = coalesce(workflow_flags, '{}'::jsonb) || '{"needs_research": true}'::jsonb
         WHERE id = p_ticket;
    END IF;
    PERFORM ticket_board.append_ticket_comment(p_ticket, who,
        format(E'Research brief.\nQuestion: %s\nTimebox: %s\nA usable answer: %s',
               saved.question, ticket_board.research_timebox_text(saved.timebox_seconds), saved.usable_answer));
    RETURN jsonb_build_object('ticket_id', p_ticket, 'question', saved.question,
                              'timebox_seconds', saved.timebox_seconds,
                              'timebox', ticket_board.research_timebox_text(saved.timebox_seconds),
                              'usable_answer', saved.usable_answer, 'written_by', saved.written_by,
                              'written_at', ticket_board.utc_text(saved.written_at));
END;
$$;

-- One pass: each research ticket whose timebox passed in Research with no submission tells the Director, once a round.
CREATE OR REPLACE FUNCTION ticket_board.note_research_timeboxes(p_now timestamptz DEFAULT clock_timestamp())
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE item record; queued bigint; told integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('note_research_timeboxes');
    IF NOT ticket_board.research_declared() THEN
        RETURN 0;
    END IF;
    FOR item IN
        SELECT t.id, t.title, t.assignee, b.timebox_seconds, b.round_started_at
          FROM ticket_board.ticket_research_briefs b JOIN ticket_board.tickets t ON t.id = b.ticket_id
         WHERE t.state = 'research' AND b.round_started_at IS NOT NULL AND b.timebox_noticed_at IS NULL
           AND b.round_started_at + make_interval(secs => b.timebox_seconds) <= p_now
         ORDER BY t.id
           FOR UPDATE OF b
    LOOP
        queued := ticket_board.enqueue_notification(item.id, 'ticket_update', 'director',
            format('%s -- %s: its research timebox (%s) passed at %s with no finding from %s. The board stops nothing: wait, return it, or put it down.',
                   item.id, item.title, ticket_board.research_timebox_text(item.timebox_seconds),
                   to_char((item.round_started_at + make_interval(secs => item.timebox_seconds)) AT TIME ZONE 'UTC',
                           'YYYY-MM-DD HH24:MI"Z"'), item.assignee),
            jsonb_build_object('kind', 'research_timebox', 'id', item.id, 'target_role', 'director',
                               'round_started_at', ticket_board.utc_text(item.round_started_at)),
            'research_timebox:' || item.id || ':' || floor(extract(epoch FROM item.round_started_at))::bigint);
        UPDATE ticket_board.ticket_research_briefs
           SET timebox_noticed_at = p_now, timebox_notification_id = queued
         WHERE ticket_id = item.id;
        told := told + 1;
    END LOOP;
    RETURN told;
END;
$$;

-- The timebox notice is current while its ticket is still in Research, in the round it was sent for.
CREATE OR REPLACE FUNCTION ticket_board.research_timebox_notice_is_current(p_payload jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.ticket_research_briefs b JOIN ticket_board.tickets t ON t.id = b.ticket_id
                    WHERE b.ticket_id = p_payload->>'id' AND t.state = 'research'
                      AND ticket_board.utc_text(b.round_started_at) = p_payload->>'round_started_at');
$$;

-- The line a transition notice carries for a research ticket, or NULL: the brief for the researcher, and the finding
-- for Audit's review and for the Director when it comes back.
CREATE OR REPLACE FUNCTION ticket_board.ticket_research_handoff(p_ticket text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE t ticket_board.tickets; b ticket_board.ticket_research_briefs; dir text; finding text;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ticket_research_handoff');
    IF NOT ticket_board.research_declared() THEN
        RETURN NULL;
    END IF;
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket;
    SELECT * INTO b FROM ticket_board.ticket_research_briefs WHERE ticket_id = p_ticket;
    IF t.id IS NULL OR b.ticket_id IS NULL THEN
        RETURN NULL;
    END IF;
    dir := coalesce(ticket_board.declared_workflow()->'research'->>'directory', 'docs/research');
    IF t.state = 'research' THEN
        RETURN format('Research question: %s Timebox: %s. A usable answer: %s A findings commit changes only %s/%s.md.',
                      b.question, ticket_board.research_timebox_text(b.timebox_seconds), b.usable_answer, dir, t.id);
    END IF;
    IF b.finding IS NULL OR NOT (t.state = 'research_review' OR t.state = ticket_board.research_triage()) THEN
        RETURN NULL;
    END IF;
    finding := CASE WHEN b.finding->>'action' = 'submit_finding'
        THEN format('Research finding: commit %s, %s/%s.md.', b.finding->>'commit_hash', dir, t.id)
        ELSE format('Research finding: no commit; the finding is the reason on %s''s submission.', b.finding->>'actor') END;
    IF t.state = 'research_review' THEN
        RETURN finding || ' Review its evidence: is every claim backed by what was run or cited?';
    END IF;
    RETURN finding || ' Close it on the finding, file the tickets it justifies, or return it.';
END;
$$;

REVOKE ALL ON ticket_board.ticket_research_briefs FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_research_brief() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_research_round() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.set_research_brief(text, text, integer, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_research_timeboxes(timestamptz) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_research_handoff(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.research_timebox_notice_is_current(jsonb) FROM PUBLIC;

COMMIT;
