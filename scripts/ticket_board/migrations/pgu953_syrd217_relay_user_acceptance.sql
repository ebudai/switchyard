-- SYRD-217: the Director can record a successful User UAT without an override.
--
-- The counterpart to SYRD-214, and the case its reasoning missed. SYRD-214
-- admitted relayed rejections only, on the argument that a relayed approval is
-- a forged sign-off. But the User's acceptance is exactly as unreachable as
-- their rejection, and refusing to record it protected nobody: on SYRD-211 the
-- User replied that everything was good, the Director narrated an override to
-- director_review and closed the ticket, and `user_signoff` stayed false on a
-- shipped ticket the User had in fact accepted. The sign-off the board declined
-- to write simply went missing.
--
-- So `relays_decision_of` now admits `approve` as well as `return`, under a
-- fence tighter than the one it replaces: a relay must mirror a move the
-- relayed role can itself make out of that stage -- same primitive, same
-- destination, same sign-off clearing -- it is relayable only for a role with
-- no pane of its own, and an approval must additionally name the commit it
-- accepts and may not be the move that ends the ticket.
--
-- This narrows SYRD-82's prohibition on the director holding sign-off
-- authority, by exactly the relayed case. That is a deliberate weakening and is
-- recorded as one: the director still holds no approval of its own, and the
-- alternative in practice was a narrated override that named no commit,
-- checked no gate and left no structured record.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.signoff_reset_key(tr jsonb)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce((SELECT array_agg(x ORDER BY x)
                       FROM jsonb_array_elements_text(tr->'clear_signoffs') x),
                    ARRAY[]::text[]);
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_workflow(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r jsonb; s jsonb; tr jsonb; f record; cursor_name text; visited text[];
    names text[]; role_names text[]; active_roles text[]; flag_names text[];
BEGIN
    IF cfg->>'schema' IS DISTINCT FROM 'switchyard.workflow.v1'
       OR jsonb_typeof(cfg->'roles') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'stages') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'transitions') IS DISTINCT FROM 'array'
       OR jsonb_typeof(cfg->'flags') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'invalid workflow document';
    END IF;
    SELECT array_agg(x->>'name') INTO names FROM jsonb_array_elements(cfg->'stages') x;
    SELECT array_agg(x->>'name'), coalesce(array_agg(x->>'name') FILTER (WHERE (x->>'active')::boolean),ARRAY[]::text[])
      INTO role_names, active_roles FROM jsonb_array_elements(cfg->'roles') x;
    SELECT array_agg(key) INTO flag_names FROM jsonb_each(cfg->'flags');
    IF cardinality(names) IS NULL OR cardinality(role_names) IS NULL
       OR cardinality(names) <> (SELECT count(DISTINCT n) FROM unnest(names) n)
       OR cardinality(role_names) <> (SELECT count(DISTINCT n) FROM unnest(role_names) n)
       OR NOT ARRAY['director','user','unassigned']::text[] <@ role_names
       OR NOT coalesce('director' = ANY(active_roles),false) THEN RAISE EXCEPTION 'missing or duplicate identities'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
        WHERE x->>'name'='director'
          AND x->'capabilities' ?& ticket_board.director_control_capabilities()) THEN
        RAISE EXCEPTION 'director must keep its control capabilities: %',
            (SELECT string_agg(c, ', ' ORDER BY c) FROM unnest(ticket_board.director_control_capabilities()) c
             WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x
                 WHERE x->>'name'='director' AND x->'capabilities' ? c));
    END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        IF jsonb_typeof(r->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(r->'active') IS DISTINCT FROM 'boolean' OR jsonb_typeof(r->'capabilities') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'role fields must have explicit types'; END IF;
        IF NOT r ?& ARRAY['name','label','kind','active','capabilities']
           OR (r->>'name') !~ '^[a-z][a-z0-9_-]{0,63}$' OR btrim(r->>'label') = ''
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign','director_edit','request_publication','resolve_publication',
                'recover_stalled_ticket')) THEN
            RAISE EXCEPTION 'invalid role policy: %', r->>'name';
        END IF;
        IF (r->>'runtime' IS NULL) <> (r->>'target' IS NULL)
           OR (r->>'runtime' IS NOT NULL AND (r->>'runtime' NOT IN ('claude','codex','agy','hermes')
               OR r->>'target' !~ '^[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$')) THEN
            RAISE EXCEPTION 'invalid runtime/target'; END IF;
        -- SYRD-135: absent is false. Only a real boolean may say otherwise, so
        -- a quoted "false" cannot read as a value and behave as its truthiness.
        IF r ? 'ephemeral' AND jsonb_typeof(r->'ephemeral') <> 'boolean' THEN
            RAISE EXCEPTION 'ephemeral must be a boolean: %', r->>'name'; END IF;
        -- SYRD-37: whether this role is handed one ticket at a time in the
        -- stages it owns. Same rule and same reason as `ephemeral`: absent is
        -- "unchanged", and only a real boolean may say otherwise.
        IF r ? 'serial' AND jsonb_typeof(r->'serial') <> 'boolean' THEN
            RAISE EXCEPTION 'serial must be a boolean: %', r->>'name'; END IF;
        -- SYRD-141: what a person reads on this role's pane. Optional; a
        -- non-empty single-line string when present, because a terminal
        -- renders it and a newline in a title is an instruction, not text.
        IF r ? 'presentation_label' AND (jsonb_typeof(r->'presentation_label') <> 'string'
            OR btrim(r->>'presentation_label') = '' OR r->>'presentation_label' LIKE E'%\n%') THEN
            RAISE EXCEPTION 'presentation_label must be non-empty single-line text: %', r->>'name'; END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'slot' FROM jsonb_array_elements(cfg->'roles') x
       WHERE x->>'slot' IS NOT NULL GROUP BY x->>'slot' HAVING count(*) > 1)
       OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'slot' IS NOT NULL
          AND ((x->>'slot')::int NOT BETWEEN 0 AND 5 OR NOT (x->>'active')::boolean OR x->>'target' IS NULL)) THEN
        RAISE EXCEPTION 'invalid or duplicate visible slot'; END IF;
    FOR f IN SELECT * FROM jsonb_each(cfg->'flags') LOOP
        IF EXISTS (SELECT FROM pg_attribute WHERE attrelid='ticket_board.tickets'::regclass AND attname=f.key AND NOT attisdropped) AND f.key NOT IN ('needs_inspection','needs_audit','needs_user_signoff','inspector_signoff','audit_signoff','user_signoff') THEN RAISE EXCEPTION 'flag collides with protected ticket field'; END IF;
        IF jsonb_typeof(f.value->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'flag kind must be explicit'; END IF;
        IF f.key IN ('needs_inspection','needs_audit','needs_user_signoff') AND f.value->>'kind'<>'gate' OR f.key IN ('inspector_signoff','audit_signoff','user_signoff') AND f.value->>'kind'<>'signoff' THEN RAISE EXCEPTION 'cannot change builtin flag kind'; END IF;
        IF f.key !~ '^[a-z][a-z0-9_]{0,63}$' OR f.value->>'kind' NOT IN ('gate','signoff')
           OR jsonb_typeof(f.value->'default') IS DISTINCT FROM 'boolean'
           OR (f.value->>'kind' = 'signoff' AND (f.value->>'default')::boolean) THEN
            RAISE EXCEPTION 'invalid flag policy'; END IF;
    END LOOP;
    IF cfg->'queue' IS NOT NULL AND cfg->'queue'<>'null'::jsonb AND NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cfg->'queue'->>'stage'
        AND x->>'kind' IN ('draft','system') AND x->>'gate' IS NULL AND NOT (x->>'terminal')::boolean
        AND (jsonb_array_length(x->'owners')=0 OR x->'owners' ? (cfg->'queue'->>'assignee'))
        AND (cfg->'queue'->>'assignee'='unassigned' OR cfg->'queue'->>'assignee'=ANY(active_roles))) THEN RAISE EXCEPTION 'invalid configured holding destination'; END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind'='implementation') <> 1 THEN
        RAISE EXCEPTION 'exactly one implementation stage required'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF jsonb_typeof(s->'name') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'label') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'kind') IS DISTINCT FROM 'string' OR jsonb_typeof(s->'terminal') IS DISTINCT FROM 'boolean' OR jsonb_typeof(s->'owners') IS DISTINCT FROM 'array' OR jsonb_typeof(s->'notify') IS DISTINCT FROM 'object' OR jsonb_typeof(s->'notify'->'kind') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'stage fields must have explicit types'; END IF;
        IF NOT s ?& ARRAY['name','label','owners','kind','gate','skip_to','signoff','terminal','notify']
           OR s->>'name' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(s->>'label') = ''
           OR s->>'kind' NOT IN ('draft','implementation','review','system')
           OR jsonb_typeof(s->'terminal') <> 'boolean' OR jsonb_typeof(s->'owners') <> 'array'
           OR NOT ARRAY(SELECT jsonb_array_elements_text(s->'owners')) <@ active_roles THEN
            RAISE EXCEPTION 'invalid stage/owner policy: %', s->>'name'; END IF;
        IF s->>'gate' IS NOT NULL AND (NOT coalesce(s->>'gate'=ANY(flag_names),false) OR cfg->'flags'->(s->>'gate')->>'kind' <> 'gate')
           OR s->>'signoff' IS NOT NULL AND (NOT coalesce(s->>'signoff'=ANY(flag_names),false) OR cfg->'flags'->(s->>'signoff')->>'kind' <> 'signoff')
           OR (s->>'gate' IS NULL) <> (s->>'skip_to' IS NULL) THEN
            RAISE EXCEPTION 'invalid gate/signoff'; END IF;
        IF s->>'kind'='implementation' AND (jsonb_array_length(s->'owners')=0 OR EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE (s->'owners') ? (x->>'name') AND x->>'kind'<>'implementer')) THEN
            RAISE EXCEPTION 'implementation owner must be implementer'; END IF;
        IF NOT (s->'notify') ?& ARRAY['kind','role'] OR s->'notify'->>'kind' NOT IN ('none','assignee','fixed_role','stage_owner_fallback')
           OR (s->'notify'->>'kind'='fixed_role' AND NOT coalesce(s->'notify'->>'role'=ANY(active_roles),false))
           OR (s->'notify'->>'kind'<>'fixed_role' AND s->'notify'->>'role' IS NOT NULL) THEN
            RAISE EXCEPTION 'notification routing or silence must be explicit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'target' IS NULL AND
          ((s->'notify'->>'kind'='fixed_role' AND x->>'name'=s->'notify'->>'role') OR
           (s->'notify'->>'kind' IN ('assignee','stage_owner_fallback') AND s->'owners' ? (x->>'name')))) THEN
            RAISE EXCEPTION 'notifying roles require a pane target'; END IF;
        visited := ARRAY[]::text[]; cursor_name := s->>'name';
        LOOP
            IF cursor_name=ANY(visited) OR NOT cursor_name=ANY(names) THEN RAISE EXCEPTION 'gate skip cycle or missing stage'; END IF;
            visited := visited || cursor_name;
            SELECT x->>'skip_to' INTO cursor_name FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=cursor_name;
            EXIT WHEN cursor_name IS NULL;
        END LOOP;
    END LOOP;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        IF EXISTS (SELECT FROM unnest(ARRAY['from','to','action','label','primitive']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'string') OR EXISTS (SELECT FROM unnest(ARRAY['owner_scoped','require_commit','require_reason','allow_no_code']) k WHERE jsonb_typeof(tr->k) IS DISTINCT FROM 'boolean') OR jsonb_typeof(tr->'clear_signoffs') IS DISTINCT FROM 'array' OR jsonb_typeof(tr->'actors') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'transition fields must have explicit types'; END IF;
        IF NOT tr ?& ARRAY['from','to','action','label','primitive','actors','owner_scoped','require_commit','require_reason','clear_signoffs']
           OR NOT tr->>'from'=ANY(names) OR NOT tr->>'to'=ANY(names) OR tr->>'from'=tr->>'to'
           OR tr->>'action' !~ '^[a-z][a-z0-9_]{0,63}$' OR btrim(tr->>'label')=''
           OR tr->>'primitive' NOT IN ('move','approve','return','reopen')
           OR jsonb_typeof(tr->'actors') <> 'array' OR jsonb_array_length(tr->'actors')=0
           OR NOT ARRAY(SELECT jsonb_array_elements_text(tr->'actors')) <@ active_roles
           OR jsonb_typeof(tr->'owner_scoped') <> 'boolean'
           OR jsonb_typeof(tr->'require_commit') <> 'boolean'
           OR jsonb_typeof(tr->'require_reason') <> 'boolean' THEN RAISE EXCEPTION 'invalid transition policy'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from';
        IF (s->>'terminal')::boolean AND tr->>'primitive'<>'reopen' THEN RAISE EXCEPTION 'terminal exit requires reopen'; END IF;
        IF tr->>'primitive'='approve' AND s->>'signoff' IS NULL THEN RAISE EXCEPTION 'approval requires signoff'; END IF;
        SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'to';
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation' THEN RAISE EXCEPTION 'return must target implementation'; END IF;
        IF (tr->>'allow_no_code')::boolean AND ((tr->>'require_commit')::boolean OR NOT (tr->>'require_reason')::boolean OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='implementation')) THEN RAISE EXCEPTION 'invalid no-code transition policy'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements_text(tr->'clear_signoffs') x
            WHERE cfg->'flags'->x->>'kind' IS DISTINCT FROM 'signoff') THEN RAISE EXCEPTION 'invalid signoff reset'; END IF;
        -- SYRD-214: a transition may record a decision its actor did not make,
        -- for a role that reached its verdict off the board. Absent means the
        -- ordinary case: the actor's own decision, which is every transition
        -- that predates relaying.
        --
        -- SYRD-217 widened this from "returns only". Refusing to relay the
        -- User's acceptance did not stop the acceptance being acted on; it
        -- pushed it into a narrated override, where the destination is
        -- whatever the controller types and no gate is checked at all. The
        -- fence that replaces it is tighter where it matters: a relay must
        -- mirror a move the relayed role can itself make out of this stage --
        -- same primitive, same destination, same sign-off clearing -- so it is
        -- that role's own step under another hand, never a route the role does
        -- not have. An approval additionally names what it accepts and may not
        -- be the move that ends the ticket.
        IF jsonb_typeof(tr->'relays_decision_of') IS NOT NULL
           AND jsonb_typeof(tr->'relays_decision_of') <> 'null' THEN
            IF jsonb_typeof(tr->'relays_decision_of') <> 'string'
               OR NOT coalesce(tr->>'relays_decision_of'=ANY(role_names),false)
               OR tr->>'primitive' NOT IN ('return','approve')
               OR NOT (tr->>'require_reason')::boolean
               OR tr->'actors' ? (tr->>'relays_decision_of')
               OR (tr->>'owner_scoped')::boolean
               OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of'))
               OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                   WHERE m->>'from'=tr->>'from' AND m->>'primitive'=tr->>'primitive'
                     AND m->'actors' ? (tr->>'relays_decision_of')
                     AND (m->>'to' IS DISTINCT FROM tr->>'to'
                          OR ticket_board.signoff_reset_key(m) IS DISTINCT FROM ticket_board.signoff_reset_key(tr)))
               -- An approval is relayed only for a role with no pane of its
               -- own -- a reviewer that can sign off for itself must -- it
               -- names what it accepts, and it may not end the ticket.
               -- Approvals only: applied to returns this would retroactively
               -- invalidate what pgu952 legitimately produced (SYRD-217).
               OR (tr->>'primitive'='approve'
                   AND ((SELECT pane->>'target' FROM jsonb_array_elements(cfg->'roles') pane
                          WHERE pane->>'name'=tr->>'relays_decision_of') IS NOT NULL
                        OR NOT (tr->>'require_commit')::boolean
                        OR coalesce((SELECT (x->>'terminal')::boolean FROM jsonb_array_elements(cfg->'stages') x
                                      WHERE x->>'name'=tr->>'to'),false))) THEN
                RAISE EXCEPTION 'invalid relayed decision policy: %', tr->>'action'; END IF;
        END IF;
    END LOOP;
    IF EXISTS (SELECT x->>'from',x->>'to',x->>'action' FROM jsonb_array_elements(cfg->'transitions') x
        GROUP BY 1,2,3 HAVING count(*)>1) THEN RAISE EXCEPTION 'duplicate transition'; END IF;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        IF NOT (s->>'terminal')::boolean THEN
            IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=s->>'name')
              OR (s->>'kind'<>'draft' AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'to'=s->>'name')) THEN
                RAISE EXCEPTION 'stage lacks entry or exit: %', s->>'name'; END IF;
            IF NOT EXISTS (
                WITH RECURSIVE reachable(n) AS (
                    SELECT s->>'name' UNION
                    SELECT x->>'to' FROM reachable q, jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=q.n
                ) SELECT FROM reachable q, jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=q.n AND (x->>'terminal')::boolean
            ) THEN RAISE EXCEPTION 'stage cannot reach a terminal: %', s->>'name'; END IF;
        END IF;
    END LOOP;
    -- Queue, defer, cancel and reopen are transitions, and their names belong to
    -- the tenant, so the floor is the shape they must leave behind rather than a
    -- list of actions to look for: a director that cannot leave a stage has lost
    -- control of every ticket sitting in it, whatever the document calls the
    -- move (SYRD-82).
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to move work out of every stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE NOT (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->'actors' ? 'director'));
    END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE (x->>'terminal')::boolean AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(cfg->'transitions') tx
            WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director')) THEN
        RAISE EXCEPTION 'director must be able to reopen every terminal stage: %',
            (SELECT string_agg(x->>'name', ', ' ORDER BY x->>'name')
             FROM jsonb_array_elements(cfg->'stages') x
             WHERE (x->>'terminal')::boolean AND NOT EXISTS (
                 SELECT FROM jsonb_array_elements(cfg->'transitions') tx
                 WHERE tx->>'from'=x->>'name' AND tx->>'primitive'='reopen' AND tx->'actors' ? 'director'));
    END IF;
    -- The half of the floor that is a prohibition. An `approve` transition is
    -- what writes its source stage's sign-off flag, so listing the director
    -- among its actors is how a document would hand the controller the power to
    -- approve the work it directs.
    --
    -- SYRD-217 narrows this by exactly one case, and that is a weakening of
    -- SYRD-82's floor, said plainly. A relayed approval writes somebody else's
    -- sign-off rather than the director's, and the relay fence above has
    -- already required it to mirror a move that role can make from that stage,
    -- to name the commit it accepts, to carry its reason, and to be relayable
    -- only for a role with no way to act for itself. The director still holds
    -- no approval of its own. What this cannot do is make the director
    -- truthful -- but nor could the narrated override it replaces, which
    -- named no commit and checked no gate.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
        WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
          AND x->>'relays_decision_of' IS NULL) THEN
        RAISE EXCEPTION 'director must not be granted sign-off authority: %',
            (SELECT string_agg(x->>'action', ', ' ORDER BY x->>'action')
             FROM jsonb_array_elements(cfg->'transitions') x
             WHERE x->>'primitive'='approve' AND x->'actors' ? 'director'
               AND x->>'relays_decision_of' IS NULL);
    END IF;

    -- Somewhere to put work down, and a way to pick it back up. `cancel` leaves
    -- every stage, so a floor that only asked whether the director could leave
    -- was satisfied by a document that could only end work, never park it
    -- (SYRD-92).
    IF NOT EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') x
        WHERE NOT (x->>'terminal')::boolean
          AND jsonb_array_length(coalesce(x->'owners','[]'::jsonb))=0
          AND x->'notify'->>'kind'='none'
    ) THEN
        RAISE EXCEPTION 'workflow must keep a stage where deferred work can wait';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') held
        WHERE NOT (held->>'terminal')::boolean
          AND NOT (jsonb_array_length(coalesce(held->'owners','[]'::jsonb))=0
                   AND held->'notify'->>'kind'='none')
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=held->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))=0
                AND dest->'notify'->>'kind'='none')
    ) THEN
        RAISE EXCEPTION 'director must be able to defer work out of every active stage';
    END IF;
    IF EXISTS (
        SELECT FROM jsonb_array_elements(cfg->'stages') parked
        WHERE NOT (parked->>'terminal')::boolean
          AND jsonb_array_length(coalesce(parked->'owners','[]'::jsonb))=0
          AND parked->'notify'->>'kind'='none'
          AND NOT EXISTS (
              SELECT FROM jsonb_array_elements(cfg->'transitions') t
              JOIN LATERAL jsonb_array_elements(cfg->'stages') dest ON dest->>'name'=t->>'to'
              WHERE t->>'from'=parked->>'name' AND t->'actors' ? 'director'
                AND NOT (dest->>'terminal')::boolean
                AND jsonb_array_length(coalesce(dest->'owners','[]'::jsonb))>0)
    ) THEN
        RAISE EXCEPTION 'deferred work must have an ordinary way back';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.perform_workflow_action_as(
    p_actor text, p_narrator text, id text, action text, payload jsonb DEFAULT '{}'::jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; tr jsonb; actor text:=p_actor; candidates int; handoff text; source_stage jsonb;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR actor IS NULL THEN RAISE EXCEPTION 'workflow action requires registered service actor' USING ERRCODE='42501'; END IF;
    SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=perform_workflow_action_as.id FOR UPDATE;
    SELECT count(*) INTO candidates FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF candidates<>1 THEN RAISE EXCEPTION 'unknown or ambiguous workflow action; specify target'; END IF;
    SELECT x INTO tr FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'from'=t.state AND x->>'action'=action
        AND (payload->>'target' IS NULL OR x->>'to'=payload->>'target');
    IF NOT tr->'actors' ? actor OR ((tr->>'owner_scoped')::boolean AND t.assignee<>actor) THEN
        RAISE EXCEPTION 'actor cannot perform workflow action' USING ERRCODE='42501'; END IF;
    SELECT x INTO source_stage FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state;
    IF t.manually_controlled AND tr->>'primitive'='approve' AND ticket_board.workflow_flag(to_jsonb(t),source_stage->>'signoff',cfg) THEN
        -- Repeated decisions neither mint a new wait generation nor reopen a resolved one.
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
        RETURN;
    END IF;
    IF (tr->>'require_reason')::boolean AND btrim(coalesce(payload->>'text',payload->>'reason',''))='' THEN RAISE EXCEPTION 'reason required'; END IF;
    IF payload ? 'commit_hash' AND (payload->>'commit_hash') !~ '^[0-9a-fA-F]{7,40}$' THEN RAISE EXCEPTION 'invalid commit hash'; END IF;
    -- SYRD-217: an approval accepted on somebody else's word is held to more
    -- than that role's own sign-off would be. The stage's gate must actually
    -- be open, the acceptance must name the exact candidate already recorded
    -- -- naming a different one approves something nobody tested, and
    -- silently replacing it is worse -- and every other review this ticket is
    -- subject to must already have been given.
    --
    -- Stricter than the role's own sign-off, deliberately. The User signing
    -- off an unaudited ticket is their own mistake to make, in front of the
    -- work; the same thing relayed is a mistake nobody in the conversation is
    -- placed to catch.
    IF tr->>'relays_decision_of' IS NOT NULL AND tr->>'primitive'='approve' THEN
        IF source_stage->>'gate' IS NOT NULL
           AND NOT ticket_board.workflow_flag(to_jsonb(t),source_stage->>'gate',cfg) THEN
            RAISE EXCEPTION 'relayed approval requires this stage to be gated on: %', source_stage->>'gate'; END IF;
        IF t.commit_exempt AND btrim(coalesce(t.commit_hash,''))='' THEN
            -- Nothing was built, so there is nothing to name; naming one
            -- anyway would be accepting an artefact this ticket never had.
            IF payload ? 'commit_hash' THEN
                RAISE EXCEPTION 'relayed approval cannot name a commit on a commit-exempt ticket'; END IF;
        ELSIF btrim(coalesce(t.commit_hash,''))='' OR payload->>'commit_hash' IS DISTINCT FROM t.commit_hash THEN
            RAISE EXCEPTION 'relayed approval must name the recorded candidate commit'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x
                    WHERE x->>'name'<>t.state
                      AND x->>'signoff' IS NOT NULL AND x->>'gate' IS NOT NULL
                      AND ticket_board.workflow_flag(to_jsonb(t),x->>'gate',cfg)
                      AND NOT ticket_board.workflow_flag(to_jsonb(t),x->>'signoff',cfg)) THEN
            RAISE EXCEPTION 'relayed approval requires the earlier reviews this ticket is subject to'; END IF;
    END IF;
    IF btrim(coalesce(payload->>'text',payload->>'reason',''))<>'' THEN
        -- SYRD-214: a relayed decision says whose it was, in the record itself.
        --
        -- What the preamble adds is the one thing a reader cannot otherwise
        -- tell: that the decision came from somebody who does not operate the
        -- board, and that the role exercising the action is not claiming to
        -- have made it or to have done the review behind it. It names the
        -- actor rather than the narrator because the actor is whose authority
        -- the transition's actor list was checked against. (The two differ
        -- only on the SYRD-133 recovery path, which cannot reach a relay: its
        -- transitions are allow_no_code and so leave implementation, while a
        -- relay is a return and so enters it.) Composed here rather than left
        -- to whoever types the reason: provenance that depends on wording is
        -- provenance that goes missing the first time somebody is in a hurry.
        PERFORM ticket_board.append_ticket_comment(
            t.id,
            coalesce(p_narrator,actor),
            CASE
            WHEN tr->>'relays_decision_of' IS NULL THEN
                coalesce(payload->>'text',payload->>'reason')
            -- One E-string per branch: only the first of a run of adjacent
            -- literals takes the E prefix, so a continuation carrying \n or an
            -- escaped quote would be neither escaped nor balanced.
            WHEN tr->>'primitive'='approve' THEN
                -- A relayed approval IS the sign-off, so the record says so
                -- rather than disclaiming it. What it still will not say is
                -- that the relayer reviewed anything (SYRD-217).
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as %2$s sign-off, decided by %2$s and entered by %1$s, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            ELSE
                format(
                    E'%1$s relayed this decision from %2$s, who reported it outside the board. It is recorded as the decision of %2$s, it is not %2$s sign-off, and %1$s did not perform the review behind it.\n\n%3$s',
                    actor,
                    tr->>'relays_decision_of',
                    coalesce(payload->>'text',payload->>'reason'))
            END);
    END IF;
    PERFORM set_config('ticket_board.held_review_target','',true);
    PERFORM set_config('ticket_board.workflow_action',action,true);
    PERFORM set_config('ticket_board.workflow_actor',actor,true);
    UPDATE ticket_board.tickets SET state=tr->>'to',
        assignee=coalesce(nullif(payload->>'assignee',''),assignee),
        commit_hash=coalesce(payload->>'commit_hash',commit_hash)
        WHERE tickets.id=t.id;
    PERFORM set_config('ticket_board.workflow_action','',true);
    PERFORM set_config('ticket_board.workflow_actor','',true);
    handoff:=nullif(current_setting('ticket_board.held_review_target',true),'');
    PERFORM set_config('ticket_board.held_review_target','',true);
    IF handoff IS NOT NULL THEN
        UPDATE ticket_board.ticket_notification_state SET awaiting_role=handoff, awaiting_since_at=clock_timestamp(),
            last_activity_at=clock_timestamp(), nudge_count=0 WHERE ticket_id=t.id;
        PERFORM ticket_board.enqueue_awaiting_role_handoff(t.id);
    END IF;
END;
$$;

DO $relay$
DECLARE cfg jsonb; rev bigint; model jsonb; relay jsonb; director text;
        controllers int; changed boolean := false;
BEGIN
    SELECT document, revision INTO cfg, rev FROM ticket_board.workflow_configuration WHERE singleton;
    IF cfg IS NULL THEN RETURN; END IF;

    -- The acceptance relay, where the shape this repairs exists. The
    -- User's own sign-off is the model it copies -- destination and sign-off
    -- clearing both -- so the relay and the move it stands in for cannot
    -- disagree about where they land.
    SELECT x INTO model FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from' = 'user_review'
       AND x->>'primitive' = 'approve'
       AND x->'actors' ? 'user'
     ORDER BY x->>'action'
     LIMIT 1;
    -- The control role, named by what it can do rather than by what it is
    -- called (SYRD-49). Where more than one active role qualifies the migration
    -- declines to guess: handing the authority to enter a User's acceptance to
    -- the wrong role is worse than leaving a tenant to grant it deliberately.
    SELECT count(*), min(r->>'name') INTO controllers, director
      FROM jsonb_array_elements(cfg->'roles') r
     WHERE (r->>'active')::boolean
       AND r->'capabilities' ?& ticket_board.control_capabilities();

    IF model IS NOT NULL
       AND controllers = 1
       -- Already granted: nothing to do, and no revision nobody asked for.
       AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
                        WHERE x->>'relays_decision_of' = 'user'
                          AND x->>'from' = 'user_review'
                          AND x->>'primitive' = 'approve')
       -- A tenant that already spells this action something else keeps its own.
       AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x
                        WHERE x->>'action' = 'relay_user_sign_off')
       -- A User that can act for itself needs nobody to relay for it, and on
       -- such an installation direct sign-off is the right answer.
       AND (SELECT pane->>'target' FROM jsonb_array_elements(cfg->'roles') pane
             WHERE pane->>'name' = 'user') IS NULL
       -- A relayed approval hands the work on; it never finishes it.
       AND NOT coalesce((SELECT (x->>'terminal')::boolean
                           FROM jsonb_array_elements(cfg->'stages') x
                          WHERE x->>'name' = model->>'to'), true)
    THEN
        relay := jsonb_build_object(
            'from', 'user_review',
            'to', model->>'to',
            'action', 'relay_user_sign_off',
            'label', 'Record the User''s acceptance (relayed)',
            'actors', jsonb_build_array(director),
            'primitive', 'approve',
            'owner_scoped', false,
            'require_commit', true,
            'require_reason', true,
            'clear_signoffs', coalesce(model->'clear_signoffs', '[]'::jsonb),
            'allow_no_code', false,
            'relays_decision_of', 'user');
        cfg := jsonb_set(cfg, ARRAY['transitions'], (cfg->'transitions') || jsonb_build_array(relay));
        -- jsonb_set creates the leaf, never the object above it.
        cfg := jsonb_set(cfg, ARRAY['migrations'], coalesce(cfg->'migrations', '{}'::jsonb), true);
        cfg := jsonb_set(cfg, ARRAY['migrations', 'relay_user_acceptance'], 'true'::jsonb, true);
        changed := true;
    END IF;

    IF NOT changed THEN RETURN; END IF;
    PERFORM ticket_board.validate_declared_workflow(cfg);
    rev := rev + 1;
    INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('pgu953', cfg);
    UPDATE ticket_board.workflow_configuration SET revision = rev, document = cfg WHERE singleton;
    PERFORM pg_notify('ticket_board_state_transition',
        jsonb_build_object('kind', 'workflow', 'revision', rev)::text);
END $relay$;

COMMIT;
