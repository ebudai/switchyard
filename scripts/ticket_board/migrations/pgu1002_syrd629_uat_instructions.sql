-- SYRD-629: UAT instructions and questions, and Send to Director. schema.sql carries the same functions;
-- rbac.sql the same privileges.
BEGIN;

-- The User's instructions for a ticket in User Review, as current data (SYRD-629).
--
-- A ticket reaches the User with three short parts the Director prepared for
-- it: what to do, what they should see, and what to choose then. The board
-- shows them at the top of the ticket; without them the User had to rebuild
-- the steps from comments or ask the Director to restate them.
--
--  * Only the Director writes them (the director_edit capability), and only
--    whole: three non-blank parts, each short. A reconfirmation restamps the
--    same text for the ticket as it is now.
--  * They are written for the ticket's commit at that moment ('' for no code)
--    and stop being current when the ticket leaves User Review without the
--    User's acceptance, or goes back to implementation. A ticket returned or
--    resubmitted therefore reaches the User again only with instructions
--    replaced or reconfirmed for what it now is.
--  * Entering a User Review stage by any path -- a declared transition, a
--    legacy one, a Director edit or a forced move -- is refused without
--    current instructions.
--  * The User's question while reviewing (ask_director) is an attributed
--    comment, leaves the ticket where it is, and tells the Director once.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_uat_instructions (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    do_this text NOT NULL CHECK (btrim(do_this) <> ''),
    you_should_see text NOT NULL CHECK (btrim(you_should_see) <> ''),
    then_choose text NOT NULL CHECK (btrim(then_choose) <> ''),
    prepared_by text NOT NULL,
    prepared_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    for_commit text NOT NULL DEFAULT '',
    superseded_at timestamptz,
    superseded_reason text
);

-- Every version the Director wrote or reconfirmed, newest last.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_uat_instruction_history (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    do_this text NOT NULL,
    you_should_see text NOT NULL,
    then_choose text NOT NULL,
    prepared_by text NOT NULL,
    prepared_at timestamptz NOT NULL,
    for_commit text NOT NULL,
    reconfirmed boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS ticket_uat_instruction_history_ticket_idx
    ON ticket_board.ticket_uat_instruction_history (ticket_id, id);

CREATE TABLE IF NOT EXISTS ticket_board.ticket_uat_questions (
    id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    asked_by text NOT NULL,
    asked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    question text NOT NULL CHECK (btrim(question) <> ''),
    comment_id bigint REFERENCES ticket_board.ticket_comments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ticket_uat_questions_ticket_idx ON ticket_board.ticket_uat_questions (ticket_id, id);

-- Whether a stage is one where the User reviews: a declared review stage the
-- User owns, or the legacy board's user_review.
CREATE OR REPLACE FUNCTION ticket_board.uat_stage(p_stage text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN p_stage = 'user_review'
                ELSE coalesce((SELECT x->'owners' ? 'user' AND x->>'kind' = 'review'
                                 FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                                WHERE x->>'name' = p_stage), false) END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.uat_implementation_stage(p_stage text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN ticket_board.declared_workflow() IS NULL THEN p_stage = 'in_progress'
                ELSE coalesce((SELECT x->>'kind' = 'implementation'
                                 FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
                                WHERE x->>'name' = p_stage), false) END;
$$;

-- Why a ticket's instructions are not current for p_commit, or NULL when they are.
CREATE OR REPLACE FUNCTION ticket_board.uat_instructions_gap(p_ticket text, p_commit text)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE saved ticket_board.ticket_uat_instructions;
BEGIN
    SELECT * INTO saved FROM ticket_board.ticket_uat_instructions WHERE ticket_id = p_ticket;
    IF NOT FOUND THEN
        RETURN 'none were prepared';
    ELSIF saved.superseded_at IS NOT NULL THEN
        RETURN format('the last ones (%s, %s) stopped being current: %s', saved.prepared_by,
                      ticket_board.utc_text(saved.prepared_at), saved.superseded_reason);
    ELSIF saved.for_commit <> coalesce(p_commit, '') THEN
        RETURN format('the last ones were written for %s, and the ticket now carries %s',
                      coalesce(nullif(saved.for_commit, ''), 'no commit'),
                      coalesce(nullif(p_commit, ''), 'no commit'));
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_uat_instructions()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE gap text;
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state AND ticket_board.uat_stage(NEW.state) THEN
        gap := ticket_board.uat_instructions_gap(NEW.id, NEW.commit_hash);
        IF gap IS NOT NULL THEN
            RAISE EXCEPTION 'UAT instructions required before % enters %: %. The Director prepares them with: ticket-board-write uat-instructions % --do-this ... --you-should-see ... (or --reconfirm)',
                NEW.id, NEW.state, gap, NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zzzzzz_uat_instructions_required ON ticket_board.tickets;
-- An update only: no ticket is created in a review (create_ticket starts one in
-- draft, analysis or backlog), so the way into User Review is a state change.
-- Named to run after every other BEFORE trigger, on the state they settled.
CREATE TRIGGER tickets_zzzzzz_uat_instructions_required BEFORE UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.require_uat_instructions();

CREATE OR REPLACE FUNCTION ticket_board.supersede_uat_instructions()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE why text;
BEGIN
    IF OLD.state IS NOT DISTINCT FROM NEW.state THEN
        RETURN NULL;
    END IF;
    IF ticket_board.uat_stage(OLD.state) AND NOT ticket_board.uat_stage(NEW.state) AND NOT NEW.user_signoff THEN
        why := format('the ticket left %s for %s without the User''s acceptance', OLD.state, NEW.state);
    ELSIF ticket_board.uat_implementation_stage(NEW.state) THEN
        why := format('the ticket went back to %s', NEW.state);
    END IF;
    IF why IS NOT NULL THEN
        UPDATE ticket_board.ticket_uat_instructions
           SET superseded_at = clock_timestamp(), superseded_reason = why
         WHERE ticket_id = NEW.id AND superseded_at IS NULL;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tickets_zz_uat_instructions_supersede ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_uat_instructions_supersede AFTER UPDATE ON ticket_board.tickets
    FOR EACH ROW EXECUTE FUNCTION ticket_board.supersede_uat_instructions();

-- The Director writes a ticket's instructions, or reconfirms the ones it has
-- for the ticket as it is now.
CREATE OR REPLACE FUNCTION ticket_board.set_uat_instructions(
    p_ticket text, p_do_this text, p_you_should_see text, p_then_choose text, p_reconfirm boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    who text;
    ticket ticket_board.tickets;
    saved ticket_board.ticket_uat_instructions;
    parts text[];
    labels text[] := ARRAY['Do this', 'You should see', 'Then choose'];
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF ticket.state IN ('done', 'cancelled') OR coalesce((SELECT (x->>'terminal')::boolean
            FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x WHERE x->>'name' = ticket.state), false) THEN
        RAISE EXCEPTION '% is %: there is no UAT to prepare', p_ticket, ticket.state;
    END IF;
    IF p_reconfirm THEN
        SELECT * INTO saved FROM ticket_board.ticket_uat_instructions WHERE ticket_id = p_ticket;
        IF NOT FOUND THEN
            RAISE EXCEPTION '% has no UAT instructions to reconfirm: write them with --do-this and --you-should-see', p_ticket;
        END IF;
        parts := ARRAY[saved.do_this, saved.you_should_see, saved.then_choose];
    ELSE
        parts := ARRAY[btrim(coalesce(p_do_this, '')), btrim(coalesce(p_you_should_see, '')),
                       btrim(coalesce(p_then_choose, ''))];
        FOR i IN 1..3 LOOP
            IF parts[i] = '' THEN
                RAISE EXCEPTION 'UAT instructions need all three parts: "%" is blank', labels[i];
            END IF;
            IF length(parts[i]) > 1500 THEN
                RAISE EXCEPTION '"%" is % characters; keep each part short (1500 at most) and link to details instead',
                    labels[i], length(parts[i]);
            END IF;
        END LOOP;
    END IF;
    INSERT INTO ticket_board.ticket_uat_instructions AS u
        (ticket_id, do_this, you_should_see, then_choose, prepared_by, prepared_at, for_commit)
    VALUES (p_ticket, parts[1], parts[2], parts[3], who, clock_timestamp(), coalesce(ticket.commit_hash, ''))
    ON CONFLICT (ticket_id) DO UPDATE
       SET do_this = EXCLUDED.do_this, you_should_see = EXCLUDED.you_should_see, then_choose = EXCLUDED.then_choose,
           prepared_by = EXCLUDED.prepared_by, prepared_at = EXCLUDED.prepared_at, for_commit = EXCLUDED.for_commit,
           superseded_at = NULL, superseded_reason = NULL
    RETURNING * INTO saved;
    INSERT INTO ticket_board.ticket_uat_instruction_history
        (ticket_id, do_this, you_should_see, then_choose, prepared_by, prepared_at, for_commit, reconfirmed)
    VALUES (p_ticket, saved.do_this, saved.you_should_see, saved.then_choose, saved.prepared_by, saved.prepared_at,
            saved.for_commit, coalesce(p_reconfirm, false));
    RETURN ticket_board.ticket_uat(p_ticket);
END;
$$;

-- The User asks the Director something while reviewing: an attributed
-- comment, the ticket stays where it is, and the Director is told once.
CREATE OR REPLACE FUNCTION ticket_board.ask_director(p_ticket text, p_question text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    who text;
    ticket ticket_board.tickets;
    question text := btrim(coalesce(p_question, ''));
    comment_id bigint;
    asked ticket_board.ticket_uat_questions;
BEGIN
    -- add_comment's own roles, as every require_actor for an action must name
    -- the same ones; the question is then the User's alone below.
    PERFORM ticket_board.require_actor(ARRAY['director', 'user', 'main', 'app', 'ops', 'audit', 'inspector', 'perf', 'research'], 'add_comment');
    who := ticket_board.current_app_actor();
    -- The User's alone: add_comment is every role's, and a question from anyone
    -- else would reach the Director as the User's.
    IF who IS DISTINCT FROM 'user' THEN
        RAISE EXCEPTION '% cannot call ask_director: it is the User''s question from User Review', who USING ERRCODE = '42501';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF NOT ticket_board.uat_stage(ticket.state) THEN
        RAISE EXCEPTION 'Ask Director is for a ticket in User Review; % is %', p_ticket, ticket.state;
    END IF;
    IF question = '' THEN
        RAISE EXCEPTION 'a question for the Director must say something';
    END IF;
    IF length(question) > 4000 THEN
        RAISE EXCEPTION 'the question is % characters; 4000 at most', length(question);
    END IF;
    PERFORM ticket_board.append_ticket_comment(p_ticket, who, 'Question for the Director: ' || question, false);
    SELECT c.id INTO comment_id FROM ticket_board.ticket_comments c
     WHERE c.ticket_id = p_ticket ORDER BY c.position DESC LIMIT 1;
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', 'on', true);
    PERFORM ticket_board.touch_ticket(p_ticket);
    PERFORM set_config('ticket_board.awaiting_role_comment_touch', '', true);
    INSERT INTO ticket_board.ticket_uat_questions (ticket_id, asked_by, question, comment_id)
    VALUES (p_ticket, who, question, comment_id)
    RETURNING * INTO asked;
    PERFORM ticket_board.enqueue_notification(
        p_ticket, 'ticket_update', 'director',
        format('%s has a question about %s, which stays in User Review: "%s". Answer with a comment on the ticket; '
               'it shows beside the question at the top of their UAT panel.', who, p_ticket, left(question, 600)),
        jsonb_build_object('kind', 'uat_question', 'id', p_ticket, 'target_role', 'director',
                           'question_id', asked.id, 'asked_by', who, 'question', question),
        'uat_question:' || asked.id);
    RETURN ticket_board.ticket_uat(p_ticket);
END;
$$;

-- A ticket's UAT data for its JSON: NULL when it has none.
CREATE OR REPLACE FUNCTION ticket_board.ticket_uat(p_ticket text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN u.ticket_id IS NULL AND q.questions IS NULL THEN NULL ELSE jsonb_build_object(
        'instructions', CASE WHEN u.ticket_id IS NULL THEN NULL ELSE jsonb_build_object(
            'do_this', u.do_this, 'you_should_see', u.you_should_see, 'then_choose', u.then_choose,
            'prepared_by', u.prepared_by, 'prepared_at', ticket_board.utc_text(u.prepared_at),
            'for_commit', u.for_commit, 'superseded_at', ticket_board.utc_text(u.superseded_at),
            'superseded_reason', u.superseded_reason,
            'current', ticket_board.uat_instructions_gap(t.id, t.commit_hash) IS NULL,
            'gap', ticket_board.uat_instructions_gap(t.id, t.commit_hash)) END,
        'questions', coalesce(q.questions, '[]'::jsonb)) END
      FROM ticket_board.tickets t
      LEFT JOIN ticket_board.ticket_uat_instructions u ON u.ticket_id = t.id
      LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object('id', x.id, 'asked_by', x.asked_by, 'asked_at', ticket_board.utc_text(x.asked_at),
                                              'question', x.question, 'comment_id', x.comment_id) ORDER BY x.id) AS questions
            FROM (SELECT * FROM ticket_board.ticket_uat_questions WHERE ticket_id = t.id ORDER BY id DESC LIMIT 10) x) q ON true
     WHERE t.id = p_ticket;
$$;

-- A stage the Director alone holds work in to decide it: not a review, not
-- terminal, owned by the Director and nobody else. A review may hand work there
-- without its sign-off, and its record rides along (SYRD-629). The same rule
-- as workflow_config.director_hold_stage_names.
CREATE OR REPLACE FUNCTION ticket_board.declared_director_hold(stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce((SELECT NOT (x->>'terminal')::boolean
   AND x->>'kind' <> 'review'
   AND coalesce(x->'owners','[]'::jsonb) = '["director"]'::jsonb
  FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x
  WHERE x->>'name'=stage), false);
$$;

-- The User handing their review to the Director: out of a User Review stage
-- into a Director hold. Only this move keeps the review's record without its
-- sign-off; a move into Analysis from anywhere else is no such hand-off.
CREATE OR REPLACE FUNCTION ticket_board.declared_user_handoff(from_stage text, to_stage text)
RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT ticket_board.declared_workflow() IS NOT NULL AND ticket_board.uat_stage(from_stage)
   AND ticket_board.declared_director_hold(to_stage);
$$;

-- Moves out of a review into a Director hold keep the review's record (identical to schema.sql).
CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update(previous ticket_board.tickets, proposed ticket_board.tickets)
RETURNS ticket_board.tickets LANGUAGE plpgsql AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); doc jsonb:=to_jsonb(proposed); tr jsonb; source_stage jsonb; dest jsonb;
    action_name text:=current_setting('ticket_board.workflow_action',true);
    -- Whose transition this is. Normally the caller's own; on a recovery the
    -- executor names the OWNER here, because the step being taken has to be one
    -- the owner could have taken and this trigger asks exactly that question
    -- (SYRD-133). Set transaction-locally by the executor immediately before the
    -- update, in the same way `workflow_action` already is, and cleared after.
    actor text:=coalesce(
        nullif(current_setting('ticket_board.workflow_actor',true),''),
        ticket_board.current_app_actor()); target text;
    reset text; flag record; owners text[]; fallback text; loops int:=0;
    queued_for text; reserved_by text;
BEGIN
    IF actor IS NULL THEN RAISE EXCEPTION 'configured ticket writes require a registered actor' USING ERRCODE='42501'; END IF;
    -- A narrated override moves a ticket the declared workflow will not: that
    -- is what it is for, and the transition table cannot describe it without
    -- describing its own bypass. Only ticket_board.force_move sets this, it is
    -- transaction-local, and reaching it already required the control role's
    -- capabilities. The legacy trigger has always had this escape; the
    -- declarative one did not, so the operation was refused here even after the
    -- capability layer let it through (SYRD-78).
    --
    -- It does not license a sign-off. An override that raised one would be
    -- manufacturing the review it exists to route around, so a sign-off flag
    -- that moves under it is refused rather than written.
    IF current_setting('ticket_board.force_move', true) = 'on' THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a forced move cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;

    -- Merge closes its own source, and only its own source. The window is one
    -- ticket, named by the operation itself, and one statement: it is opened
    -- immediately before the close and shut immediately after, so nothing else
    -- in the transaction can travel through it. The destination has to be a
    -- stage the workflow declares terminal, and no sign-off may move -- a merge
    -- that raised one would be manufacturing the review the target still owes
    -- (SYRD-79).
    IF nullif(current_setting('ticket_board.merge_source', true), '') = previous.id THEN
        IF NOT coalesce((
            SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
            WHERE x->>'name' = proposed.state
        ), false) THEN
            RAISE EXCEPTION 'merge may only close its source into a terminal stage, not %', proposed.state
                USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind'='signoff'
               AND ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg)
                   IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
                RAISE EXCEPTION 'a merge cannot change sign-off %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- A Director edit changes fields the transition table has nothing to say
    -- about, at whatever stage the ticket is in. The window names one ticket
    -- and is open for one statement. It carries no sign-off: ticket_board
    -- .director_edit, the only operation that opens this window, refuses to
    -- raise one before it writes anything (SYRD-83).
    IF nullif(current_setting('ticket_board.director_edit_target', true), '') = previous.id THEN
        RETURN proposed;
    END IF;
    -- SYRD-598: withdrawing one named sign-off. ticket_board.withdraw_signoff,
    -- the only operation that opens this window, names one ticket and one
    -- sign-off for one statement, and only that sign-off may fall: no stage,
    -- owner or other flag moves through it, and nothing is raised.
    IF split_part(nullif(current_setting('ticket_board.signoff_withdrawal', true), ''), ':', 1) = previous.id THEN
        IF previous.state IS DISTINCT FROM proposed.state OR previous.assignee IS DISTINCT FROM proposed.assignee THEN
            RAISE EXCEPTION 'withdrawing a sign-off moves nothing' USING ERRCODE='42501';
        END IF;
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND NOT (flag.key = split_part(current_setting('ticket_board.signoff_withdrawal', true), ':', 2)
                        AND NOT ticket_board.workflow_flag(doc,flag.key,cfg)) THEN
                RAISE EXCEPTION 'withdrawing a sign-off changes only that sign-off, not %', flag.key USING ERRCODE='42501';
            END IF;
        END LOOP;
        RETURN proposed;
    END IF;
    -- SYRD-599: a Router operation's window names one ticket and one statement,
    -- and admits exactly its own change (require_router_change). Everything
    -- after this still applies to it: the stage-owner check, serial focus, the
    -- declared transition and its bookkeeping.
    IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
        PERFORM ticket_board.require_router_change(previous, proposed, actor);
    END IF;
    IF previous.state=proposed.state THEN
        IF previous.assignee IS DISTINCT FROM proposed.assignee AND actor<>'director'
           AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id THEN
            RAISE EXCEPTION 'only director may reassign without transition' USING ERRCODE='42501'; END IF;
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg)
               AND (flag.value->>'kind'='signoff' OR actor<>'director') THEN
                RAISE EXCEPTION 'flag change requires authorized workflow action'; END IF;
        END LOOP;
        -- SYRD-77: an owner change that is not a transition never reached the
        -- serial-focus redirect below, so a same-stage reassignment could leave
        -- one implementer holding two implementation tickets -- exactly the
        -- reservation the queue exists to protect. Resolving it here rather
        -- than in the caller means no write path can hand an implementer
        -- reserved work by declining to ask.
        --
        -- SYRD-568: only a pull policy still diverts, into its ready stage
        -- pinned to the busy author. A Director-dispatched board leaves the
        -- ticket where it was put, assigned, waiting in that implementer's
        -- queue; only the active ticket holds the slot.
        IF previous.assignee IS DISTINCT FROM proposed.assignee THEN
            IF ticket_board.declared_scheduling() IS NOT NULL
               AND ticket_board.declared_stage_kind(proposed.state)='implementation'
               AND ticket_board.ticket_is_implementer_assignee(proposed.assignee)
               AND NOT proposed.manually_controlled
               AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL THEN
                -- SYRD-599: Router changes the owner and nothing else. Diverting its
                -- pick into the ready stage would move the ticket it was told to
                -- leave where it is, so it is refused instead: Router chooses a
                -- free owner or hands the ticket back.
                IF nullif(current_setting('ticket_board.router_ticket', true), '') = previous.id THEN
                    RAISE EXCEPTION '% already has % active; on this pull-scheduled board Router cannot queue work behind it: choose an owner with no active ticket, or return % to the Director',
                        proposed.assignee, ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id), previous.id
                        USING ERRCODE='42501'; END IF;
                IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN
                    RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
                queued_for:=proposed.assignee;
                reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
                proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
                PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
                proposed.queued_for_assignee:=queued_for;
                proposed.queued_behind_ticket:=coalesce(reserved_by,'');
                PERFORM set_config('ticket_board.serial_focus_queued',
                    jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                        'stage',proposed.state,'assignee',proposed.assignee)::text,true);
            ELSE
                -- A deliberate new owner ends the hold that named the old one,
                -- so the marker never outlives the reservation that set it.
                proposed.queued_for_assignee:='';
                proposed.queued_behind_ticket:='';
            END IF;
        END IF;
        RETURN proposed;
    END IF;
    FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF ticket_board.workflow_flag(to_jsonb(previous),flag.key,cfg) IS DISTINCT FROM ticket_board.workflow_flag(doc,flag.key,cfg) THEN
            RAISE EXCEPTION 'flags cannot be patched alongside a transition'; END IF;
    END LOOP;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x
      WHERE x->>'from'=previous.state AND x->>'to'=proposed.state AND x->>'action'=action_name;
    IF tr IS NULL OR NOT ticket_board.transition_allows_actor(cfg,tr,actor) OR ((tr->>'owner_scoped')::boolean AND previous.assignee<>actor) THEN
        RAISE EXCEPTION 'unauthorized configured transition: % / %',actor,action_name USING ERRCODE='42501'; END IF;
    -- SYRD-599: router_return is declared so it is visible, owner-scoped and
    -- bookkept like any move, but only its own operation may take it: it
    -- carries the revision check and the Director's handoff.
    IF tr->>'action'='router_return'
       AND nullif(current_setting('ticket_board.router_ticket', true), '') IS DISTINCT FROM previous.id THEN
        RAISE EXCEPTION 'router_return is taken only through ticket_board.router_return' USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=previous.state;
    IF (source_stage->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
    -- A blocker stops work going FORWARD. Putting work down is not going
    -- forward: a parking stage is not terminal, owns nobody and notifies
    -- nobody, so nothing is promoted by landing there and the blocker is
    -- exactly the reason to park. Refusing it forced the director to clear the
    -- blocker, defer, and restore it -- three writes, a window where the ticket
    -- looked unblocked, and a blocked ticket holding an implementer's serial
    -- slot in the meantime (SYRD-192).
    --
    -- Keyed on the DESTINATION's shape rather than on an action name, because
    -- the name is the tenant's: `declared_parking_stage` is the same predicate
    -- `workflow_config.parking_stage_names` uses, so a tenant that calls it
    -- something other than `backlog` gets this for free.
    IF tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       -- SYRD-629: the User handing their review to the Director promotes nothing.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.declared_review_return(cfg, previous.state, tr->>'to')
       -- SYRD-599: Router handing work back to triage promotes nothing.
       AND tr->>'action'<>'router_return'
       -- SYRD-568: placing blocked work in its implementer's queue promotes
       -- nothing either -- it holds no slot and is handed to nobody until its
       -- blockers resolve and it is the active ticket. Submitting it still is.
       -- A pull policy queues in its ready stage instead, as before.
       AND NOT (ticket_board.declared_stage_kind(tr->>'to') = 'implementation'
                AND ticket_board.declared_scheduling() IS NULL)
       AND ticket_board.ticket_has_unresolved_blockers(previous.id) THEN
        RAISE EXCEPTION 'unresolved blocker prevents forward promotion: %',
            ticket_board.unresolved_blocker_list(previous.id); END IF;
    -- SYRD-568: a ticket waiting in its implementer's queue is not that
    -- implementer's work yet, so it cannot be submitted for review. Putting it
    -- down, routing it back or cancelling it are not submissions.
    IF source_stage->>'kind'='implementation'
       AND (ticket_board.declared_stage_kind(tr->>'to')='review'
            OR coalesce((tr->>'require_commit')::boolean,false) OR coalesce((tr->>'allow_no_code')::boolean,false))
       AND ticket_board.ticket_serial_waiting(previous.id) THEN
        RAISE EXCEPTION '% is waiting in %''s queue behind %; it can be submitted once it is the active ticket',
            previous.id, previous.assignee, coalesce(ticket_board.ticket_current_reserved_ticket(previous.assignee),'its blockers')
            USING ERRCODE='42501'; END IF;
    IF (tr->>'require_commit')::boolean AND btrim(proposed.commit_hash)='' AND NOT proposed.commit_exempt THEN
        RAISE EXCEPTION 'commit required'; END IF;
    -- A declared return out of review hands the work back: it needs no
    -- verdict, and the sign-off stays exactly as it was (SYRD-536). Its own
    -- branch, so SYRD-263's parking exemption below is unchanged.
    IF tr->>'primitive' NOT IN ('approve','return','reopen')
       AND ticket_board.declared_review_return(cfg, previous.state, tr->>'to') THEN
        NULL;
    ELSIF tr->>'primitive'='approve' THEN doc:=ticket_board.set_workflow_flag(doc,source_stage->>'signoff',true);
    -- A sign-off gates leaving a review FORWARD. Parking is not a verdict: the
    -- stage owns nobody, notifies nobody and promotes nothing, and the review's
    -- record rides along untouched -- no sign-off is granted, none is cleared,
    -- the commit stays. Requiring the missing sign-off made an unaccepted User
    -- Review impossible to put down without deciding it (SYRD-263). Keyed on
    -- the destination's shape, like the blocker exemption above (SYRD-192).
    ELSIF source_stage->>'signoff' IS NOT NULL AND tr->>'primitive' NOT IN ('return','reopen')
       AND NOT ticket_board.declared_parking_stage(tr->>'to')
       -- SYRD-629: nor is the User handing it to the Director, in a stage they
       -- alone hold to decide it: the User's message about a candidate keeps the
       -- candidate, its commit and Audit's sign-off, for the Director to return
       -- to the User or send on. Only from User Review: a move into Analysis
       -- from any other review still needs that review's sign-off.
       AND NOT ticket_board.declared_user_handoff(previous.state, tr->>'to')
       AND NOT ticket_board.workflow_flag(doc,source_stage->>'signoff',cfg) THEN RAISE EXCEPTION 'stage signoff required'; END IF;
    FOR reset IN SELECT jsonb_array_elements_text(tr->'clear_signoffs') LOOP
        doc:=ticket_board.set_workflow_flag(doc,reset,false);
    END LOOP;
    IF tr->>'primitive' IN ('return','reopen') THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',false); END IF;
    IF (tr->>'allow_no_code')::boolean THEN doc:=doc||jsonb_build_object('commit_hash','','commit_exempt',true); END IF;
    -- A sign-off is a review of one commit (SYRD-271). MEFP-4 was approved for
    -- one commit, routed back by a plain `move` that clears nothing, and
    -- resubmitted with another -- and the audit stage, already signed, was
    -- skipped: the old verdict stood in for a review nobody had done. So when
    -- work leaves implementation carrying a different commit, every review
    -- sign-off is cleared here, before the gates are walked, and it enters
    -- review for exactly the commit it carries.
    -- The same commit resubmitted keeps its review: that exact work was seen.
    -- A no-code submission has no commit to show it is the same work, so it
    -- is always reviewed as new.
    IF source_stage->>'kind' = 'implementation'
       AND ((tr->>'allow_no_code')::boolean
            OR (btrim(coalesce(doc->>'commit_hash','')) <> ''
                AND (doc->>'commit_hash') IS DISTINCT FROM previous.commit_hash)) THEN
        FOR flag IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
            IF flag.value->>'kind' = 'signoff' THEN
                doc:=ticket_board.set_workflow_flag(doc,flag.key,false);
            END IF;
        END LOOP;
    END IF;
    target:=proposed.state;
    LOOP
        SELECT x INTO dest FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=target;
        IF dest IS NULL THEN RAISE EXCEPTION 'missing target stage'; END IF;
        EXIT WHEN (dest->>'gate' IS NULL OR ticket_board.workflow_flag(doc,dest->>'gate',cfg))
          AND NOT (dest->>'signoff' IS NOT NULL AND dest->>'skip_to' IS NOT NULL AND ticket_board.workflow_flag(doc,dest->>'signoff',cfg));
        target:=dest->>'skip_to'; loops:=loops+1;
        IF loops>jsonb_array_length(cfg->'stages') THEN RAISE EXCEPTION 'gate cycle'; END IF;
    END LOOP;
    owners:=ARRAY(SELECT jsonb_array_elements_text(dest->'owners'));
    -- SYRD-599: on a Router-enabled board the Director's ordinary route into
    -- Implementation is Router's, unless the Director names an Implementation
    -- owner: that is the direct route, and every rule below applies to it. A
    -- Router that cannot be told is refused, never replaced by an implementer:
    -- going direct is the Director's choice, not a fallback.
    IF dest->>'kind'='implementation' AND actor='director' AND tr->>'primitive'='move'
       AND source_stage->>'kind' NOT IN ('implementation','review')
       AND ticket_board.router_holds(NULL,'router') AND NOT proposed.assignee=ANY(owners) THEN
        IF NOT ticket_board.router_available() THEN
            RAISE EXCEPTION 'Router is not running, so % cannot be routed through it; route it to an Implementation owner directly (%), or start Router',
                previous.id, array_to_string(owners, ', ') USING ERRCODE='55000';
        END IF;
        proposed.assignee:='router';
    END IF;
    IF dest->>'signoff' IS NOT NULL THEN doc:=ticket_board.set_workflow_flag(doc,dest->>'signoff',false); END IF;
    IF tr->>'primitive'='return' THEN
        SELECT last_implementer_assignee INTO fallback FROM ticket_board.ticket_notification_state WHERE ticket_id=previous.id;
        IF fallback=ANY(owners) THEN proposed.assignee:=fallback; END IF;
    END IF;
    IF cardinality(owners)>0 AND NOT proposed.assignee=ANY(owners)
       AND NOT ticket_board.router_holds(target,proposed.assignee) THEN proposed.assignee:=owners[1]; END IF;
    IF previous.manually_controlled AND tr->>'primitive'='approve' THEN
        -- Record the decision but retain the deliberate stage/owner hold. The
        -- executor creates the durable handoff after activity triggers finish.
        PERFORM set_config('ticket_board.held_review_target',coalesce(nullif(ticket_board.transition_target_role(target,proposed.assignee),actor),'director'),true);
        -- And keep the transition the approval earned, instead of discarding it
        -- with the stage. A hold defers a decision; releasing it replays this
        -- exact step, as this actor, once (SYRD-180).
        INSERT INTO ticket_board.ticket_deferred_review(ticket_id,action_name,actor,to_stage)
        VALUES(previous.id,action_name,actor,tr->>'to')
        ON CONFLICT (ticket_id) DO UPDATE SET action_name=EXCLUDED.action_name,
            actor=EXCLUDED.actor,to_stage=EXCLUDED.to_stage,deferred_at=clock_timestamp();
        target:=previous.state; proposed.assignee:=previous.assignee;
    ELSE
        -- A move that actually happens leaves nothing owed, including the move
        -- that pays a deferral: nothing may be replayed twice.
        DELETE FROM ticket_board.ticket_deferred_review WHERE ticket_id=previous.id;
    END IF;
    doc:=doc||jsonb_build_object('state',target,'assignee',proposed.assignee);
    proposed:=jsonb_populate_record(proposed,doc);
    PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
    -- SYRD-568: on a Director-dispatched board a move into an implementation
    -- stage lands there, assigned, even when that implementer already has
    -- active work, and waits in their queue until it is the active ticket. A
    -- pull policy keeps its ready stage as the queue (SYRD-539): there the
    -- move is still diverted, pinned to the busy author.
    IF ticket_board.declared_scheduling() IS NOT NULL
       AND dest->>'kind'='implementation' AND NOT ticket_board.router_holds(dest->>'name',proposed.assignee)
       AND ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id) IS NOT NULL AND NOT proposed.manually_controlled THEN
        IF cfg->'queue' IS NULL OR cfg->'queue'='null'::jsonb THEN RAISE EXCEPTION 'implementer already owns reserved work; configure a holding destination'; END IF;
        queued_for:=proposed.assignee;
        reserved_by:=ticket_board.ticket_current_reserved_ticket(proposed.assignee,proposed.id);
        proposed.state:=cfg->'queue'->>'stage'; proposed.assignee:=cfg->'queue'->>'assignee';
        PERFORM ticket_board.require_stage_owner_assignee(proposed.state,proposed.assignee);
        -- Serial focus still holds the ticket, but the outcome is now legible.
        -- The queue destination is frequently the stage the ticket came from,
        -- so without these the caller sees an unchanged ticket and a success.
        proposed.queued_for_assignee:=queued_for;
        proposed.queued_behind_ticket:=coalesce(reserved_by,'');
        PERFORM set_config('ticket_board.serial_focus_queued',
            jsonb_build_object('queued_for',queued_for,'reserved_by',reserved_by,
                'stage',proposed.state,'assignee',proposed.assignee)::text,true);
    ELSE
        -- Any move that is not held clears the marker, so it never outlives the
        -- reservation that caused it.
        proposed.queued_for_assignee:='';
        proposed.queued_behind_ticket:='';
    END IF;
    -- Deferring is putting work down, so it has to stop looking like work
    -- somebody has. An owner left on a parked ticket keeps its implementer's
    -- serial reservation and keeps the board highlighting it as current, which
    -- is how a deferral would quietly go on nudging the person who deferred it.
    -- Everything the ticket is -- content, hierarchy, blockers, gates,
    -- sign-offs, comments -- is untouched; only who holds it changes (SYRD-92).
    IF ticket_board.declared_parking_stage(proposed.state) THEN
        -- queued_for is set only by the serial-focus redirect just above, whose
        -- holding destination is frequently this same stage. That ticket is
        -- waiting on a busy implementer rather than being put down, and its
        -- queue bookkeeping is what makes the outcome legible, so it is left
        -- exactly as it was.
        IF queued_for IS NULL THEN
            proposed.assignee:='unassigned';
            proposed.parked:=true;
            proposed.queued_for_assignee:='';
            proposed.queued_behind_ticket:='';
        END IF;
    ELSIF ticket_board.declared_parking_stage(previous.state) THEN
        -- Reviving it: the hold ends with the stage that carried it.
        proposed.parked:=false;
    END IF;
    RETURN proposed;
END;
$$;

-- A declared board shaped like the shipped workflow gets Send to Director:
-- the User's message moves the ticket to the Director's own decision stage,
-- user_feedback, instead of returning it to implementation. The User's
-- kick-back, and the relay that mirrored it, give way to it. A board of any
-- other shape -- no user_kick_back out of a User-owned user_review, a missing
-- destination, a control role not called director, or the change already made
-- -- is left as it is, for its Director to configure deliberately.
DO $feedback$
DECLARE cfg jsonb; rev bigint; controllers int; director text; at int; changed jsonb;
    stage jsonb := '{"name": "user_feedback", "label": "Director reviewing the User''s message", "owners": ["director"], "kind": "system", "gate": null, "skip_to": null, "signoff": null, "terminal": false, "notify": {"kind": "assignee", "role": null}}'::jsonb;
    additions jsonb := '[{"from": "user_review", "to": "user_feedback", "action": "user_send_to_director", "label": "Send to Director", "actors": ["user"], "primitive": "move", "owner_scoped": true, "require_commit": false, "require_reason": true, "clear_signoffs": [], "allow_no_code": false}, {"from": "user_feedback", "to": "user_review", "action": "director_return_to_uat", "label": "Return to the User for UAT", "actors": ["director"], "primitive": "move", "owner_scoped": false, "require_commit": false, "require_reason": false, "clear_signoffs": [], "allow_no_code": false}, {"from": "user_feedback", "to": "in_progress", "action": "director_send_to_implementer", "label": "Send to the implementer", "actors": ["director"], "primitive": "return", "owner_scoped": false, "require_commit": false, "require_reason": true, "clear_signoffs": ["audit_signoff", "user_signoff"], "allow_no_code": false}, {"from": "user_feedback", "to": "analysis", "action": "director_route_feedback", "label": "Route elsewhere (to Analysis)", "actors": ["director"], "primitive": "move", "owner_scoped": false, "require_commit": false, "require_reason": true, "clear_signoffs": [], "allow_no_code": false}, {"from": "user_feedback", "to": "backlog", "action": "defer", "label": "Defer", "actors": ["director"], "primitive": "move", "owner_scoped": false, "require_commit": false, "require_reason": false, "clear_signoffs": [], "allow_no_code": false}, {"from": "user_feedback", "to": "cancelled", "action": "cancel", "label": "Cancel", "actors": ["director"], "primitive": "move", "owner_scoped": false, "require_commit": false, "require_reason": false, "clear_signoffs": ["inspector_signoff", "audit_signoff", "user_signoff"], "allow_no_code": false}]'::jsonb;
BEGIN
    SELECT document INTO cfg FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = 'user_feedback') THEN RETURN; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
                    WHERE x->>'name' = 'user_review' AND x->>'kind' = 'review' AND x->'owners' = '["user"]'::jsonb)
       OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
                       WHERE x->>'action' = 'user_kick_back' AND x->>'from' = 'user_review')
       OR (SELECT count(*) FROM jsonb_array_elements(cfg->'stages') x
            WHERE x->>'name' IN ('in_progress', 'analysis', 'backlog', 'cancelled')) <> 4 THEN
        RETURN;
    END IF;
    SELECT count(*), min(r->>'name') INTO controllers, director
      FROM jsonb_array_elements(cfg->'roles') r
     WHERE (r->>'active')::boolean AND r->'capabilities' ?& ticket_board.control_capabilities();
    IF controllers <> 1 OR director <> 'director' THEN RETURN; END IF;
    -- Applied as the service, which every board whose document was configured
    -- holds these privileges as. A board seeded some other way and not yet given
    -- rbac.sql's grants is left as it is, rather than failing the whole upgrade.
    IF NOT has_schema_privilege('ticket_board_service', 'ticket_board', 'USAGE')
       OR NOT has_function_privilege('ticket_board_service', 'ticket_board.apply_declared_workflow(jsonb)', 'EXECUTE') THEN
        RAISE NOTICE 'pgu1002: ticket_board_service cannot apply a workflow here yet; the User''s Send to Director is not added';
        RETURN;
    END IF;

    SELECT min(x.ordinality)::int INTO at FROM jsonb_array_elements(cfg->'stages') WITH ORDINALITY x
     WHERE x.value->>'name' = 'user_review';
    changed := jsonb_set(cfg, ARRAY['stages'],
        (SELECT jsonb_agg(s.value ORDER BY s.o) FROM (
            SELECT y.value, y.ordinality::numeric AS o FROM jsonb_array_elements(cfg->'stages') WITH ORDINALITY y
            UNION ALL SELECT stage, at + 0.5) s));
    changed := jsonb_set(changed, ARRAY['transitions'],
        (SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM jsonb_array_elements(cfg->'transitions') x
          WHERE NOT (x->>'from' = 'user_review' AND x->>'action' IN ('user_kick_back', 'relay_user_kick_back')))
        || additions);
    changed := jsonb_set(changed, ARRAY['migrations'], coalesce(changed->'migrations', '{}'::jsonb), true);
    changed := jsonb_set(changed, ARRAY['migrations', 'user_feedback'], 'true'::jsonb, true);
    -- It adds a stage and removes none, whatever the last configuration asked to remove.
    changed := changed || '{"remove_stages": []}'::jsonb;

    -- Applied the way the Director applies a document, so the stage is
    -- projected and every rule checked; the revision is then the migration's.
    EXECUTE 'SET LOCAL ROLE ticket_board_service';
    PERFORM set_config('ticket_board.caller_role', director, true);
    PERFORM set_config('ticket_board.project', cfg->>'project', true);
    rev := ticket_board.apply_declared_workflow(changed);
    EXECUTE 'RESET ROLE';
    UPDATE ticket_board.workflow_revisions SET actor = 'pgu1002' WHERE revision = rev;
    PERFORM pg_notify('ticket_board_state_transition', jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $feedback$;

-- Privileges: the same lines rbac.sql carries (SYRD-629).
REVOKE ALL ON ticket_board.ticket_uat_instructions, ticket_board.ticket_uat_instruction_history,
    ticket_board.ticket_uat_questions FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.uat_stage(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.uat_implementation_stage(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.uat_instructions_gap(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_uat_instructions() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.supersede_uat_instructions() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.set_uat_instructions(text, text, text, text, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ask_director(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_uat(text) FROM PUBLIC;
GRANT SELECT ON ticket_board.ticket_uat_instructions, ticket_board.ticket_uat_instruction_history,
    ticket_board.ticket_uat_questions TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.set_uat_instructions(text, text, text, text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ask_director(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_uat(text) TO ticket_board_service, ticket_board_listener;

COMMIT;
