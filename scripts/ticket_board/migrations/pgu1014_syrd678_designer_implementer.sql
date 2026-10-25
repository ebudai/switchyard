-- SYRD-678: a designer may also be an Implementation owner, and Router may be
-- told which work it takes.
--
-- A role's kind is its identity and stays singular. A draft role (a designer)
-- may declare `additional_kinds: ["implementer"]`: it keeps owning Draft and
-- releasing drafts, and may also own the Implementation stage, be assigned
-- there, hold serial focus, submit with or without a commit, and receive
-- Audit's findings as the producer of its revision (SYRD-673). Every check that
-- asked "is this role an implementer" by its kind now asks role_has_kind:
-- Implementation ownership, the remembered implementer, Router's choice and the
-- pull claim. A role's `routing_scope` names a gate flag: Router may give it only
-- tickets carrying that flag, the Director anyone. A scoped role may not claim
-- pulled work. Documents that declare neither field behave exactly as before.
--
-- schema.sql carries the same functions.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.role_has_kind(r jsonb, p_kind text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(r->>'kind' = p_kind
        OR (jsonb_typeof(r->'additional_kinds') = 'array' AND r->'additional_kinds' ? p_kind), false);
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
           OR r->>'kind' NOT IN ('system','draft','implementer','reviewer','support','user','router')
           OR jsonb_typeof(r->'active') <> 'boolean' OR jsonb_typeof(r->'capabilities') <> 'array'
           OR EXISTS (SELECT FROM jsonb_array_elements_text(r->'capabilities') c WHERE c NOT IN
               ('create_ticket','file_bug','add_comment','edit_fields','await_role','clear_awaiting_role',
                'set_blockers','set_manually_controlled','crop_attachment','merge','dismiss_notification',
                'reassign','director_edit','request_publication','resolve_publication',
                'recover_stalled_ticket','router_assign')) THEN
            RAISE EXCEPTION 'invalid role policy: %', r->>'name';
        END IF;
        IF (r->>'runtime' IS NULL) <> (r->>'target' IS NULL)
           OR (r->>'runtime' IS NOT NULL AND (r->>'runtime' NOT IN ('claude','codex','agy','hermes','copilot')
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
        -- SYRD-678: a draft role (a designer) may also implement, and nothing
        -- else may be combined; a routing scope names a declared gate flag and
        -- narrows Router's choice of an Implementation owner.
        IF r ? 'additional_kinds' AND (jsonb_typeof(r->'additional_kinds') <> 'array'
            OR jsonb_array_length(r->'additional_kinds') = 0
            OR EXISTS (SELECT FROM jsonb_array_elements(r->'additional_kinds') k WHERE jsonb_typeof(k) <> 'string')
            OR (SELECT count(DISTINCT k) FROM jsonb_array_elements_text(r->'additional_kinds') k) <> jsonb_array_length(r->'additional_kinds')
            OR r->>'kind' <> 'draft' OR r->'additional_kinds' <> '["implementer"]'::jsonb) THEN
            RAISE EXCEPTION 'invalid additional_kinds: only a draft role may also be an implementer: %', r->>'name'; END IF;
        IF r ? 'routing_scope' AND (jsonb_typeof(r->'routing_scope') <> 'object'
            OR (SELECT array_agg(k) FROM jsonb_object_keys(r->'routing_scope') k) <> ARRAY['flag']
            OR jsonb_typeof(r->'routing_scope'->'flag') <> 'string'
            OR cfg->'flags'->(r->'routing_scope'->>'flag')->>'kind' IS DISTINCT FROM 'gate'
            OR NOT ticket_board.role_has_kind(r, 'implementer')) THEN
            RAISE EXCEPTION 'invalid routing_scope: it names a declared gate flag, on a role that implements: %', r->>'name'; END IF;
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
    -- SYRD-276: how long an implementer stays reserved by work it has touched.
    -- Absent is 'review', the behaviour every tenant has always had.
    IF cfg ? 'reservation' AND (jsonb_typeof(cfg->'reservation') IS DISTINCT FROM 'string'
        OR cfg->>'reservation' NOT IN ('review','lifecycle')) THEN
        RAISE EXCEPTION 'invalid reservation policy'; END IF;
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
            SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE (s->'owners') ? (x->>'name') AND NOT ticket_board.role_has_kind(x, 'implementer'))) THEN
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
        -- SYRD-567: or a review hands work back to the non-review stage that
        -- submits into it (a design review returning a spec to its designer).
        -- workflow_config._review_hands_back is the same rule.
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation'
           AND NOT (EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='review')
                    AND s->>'kind'<>'review'
                    AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                                WHERE m->>'from'=tr->>'to' AND m->>'to'=tr->>'from' AND m->>'primitive' IN ('move','approve')))
        THEN RAISE EXCEPTION 'return must target implementation'; END IF;
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
    PERFORM ticket_board.validate_router_declaration(cfg);  -- SYRD-599
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_notification_roles()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ticket_board.declared_workflow() IS NOT NULL THEN
  IF NEW.awaiting_role<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.awaiting_role) THEN RAISE EXCEPTION 'unknown awaited role'; END IF;
  IF NEW.last_implementer_assignee<>'' AND NOT EXISTS(SELECT FROM ticket_board.workflow_roles WHERE name=NEW.last_implementer_assignee AND ticket_board.role_has_kind(definition,'implementer')) THEN RAISE EXCEPTION 'unknown implementation owner'; END IF;
 END IF;
 RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.scheduling_claim_transitions(cfg jsonb)
RETURNS SETOF jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT x FROM jsonb_array_elements(cfg->'transitions') x
     WHERE x->>'from'=cfg->'scheduling'->>'ready_stage'
       AND x->>'to'=(SELECT s->>'name' FROM jsonb_array_elements(cfg->'stages') s WHERE s->>'kind'='implementation' LIMIT 1)
       AND x->>'primitive'='move' AND jsonb_array_length(x->'actors')>0
       AND NOT EXISTS (SELECT FROM jsonb_array_elements_text(x->'actors') a
                       WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
                                         WHERE r->>'name'=a AND ticket_board.role_has_kind(r, 'implementer')));
$$;

CREATE OR REPLACE FUNCTION ticket_board.pull_claimant_pool(cfg jsonb)
RETURNS SETOF text LANGUAGE sql IMMUTABLE AS $$
    SELECT r->>'name' FROM jsonb_array_elements(cfg->'roles') r
     WHERE nullif(cfg->'scheduling','null'::jsonb) IS NOT NULL
       AND ticket_board.role_has_kind(r, 'implementer') AND r->'active'='true'::jsonb AND r->'ephemeral'='true'::jsonb
       AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
                   WHERE s->>'kind'='implementation' AND s->'owners' ? (r->>'name'))
     ORDER BY r->>'name';
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_declared_scheduling(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE policy jsonb:=nullif(cfg->'scheduling','null'::jsonb); ready text; implementation text; claims int; seconds jsonb;
    cohorts jsonb; field text;
BEGIN
    IF policy IS NULL THEN RETURN; END IF;
    IF jsonb_typeof(policy)<>'object' OR NOT (policy ?& ARRAY['mode','ready_stage','release_after'])
       OR EXISTS (SELECT FROM jsonb_object_keys(policy) k WHERE k NOT IN ('mode','ready_stage','release_after','idle_alert_seconds','cohorts')) THEN
        RAISE EXCEPTION 'invalid scheduling policy'; END IF;
    IF policy->>'mode' IS DISTINCT FROM 'pull' THEN RAISE EXCEPTION 'invalid scheduling mode'; END IF;
    IF coalesce(cfg->>'reservation','review')<>'review' THEN
        RAISE EXCEPTION 'scheduling cannot be combined with reservation lifecycle'; END IF;
    IF jsonb_typeof(policy->'ready_stage') IS DISTINCT FROM 'string'
       OR NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=policy->>'ready_stage') THEN
        RAISE EXCEPTION 'scheduling ready stage must be a declared stage'; END IF;
    ready:=policy->>'ready_stage';
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ready
               AND ((x->>'terminal')::boolean OR x->>'kind' IN ('implementation','review'))) THEN
        RAISE EXCEPTION 'scheduling ready stage must be a non-terminal holding stage'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x WHERE x->>'to'=ready AND x->'actors'<>'["director"]'::jsonb) THEN
        RAISE EXCEPTION 'only the director may admit work to the ready stage'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') x JOIN jsonb_array_elements(cfg->'stages') d ON d->>'name'=x->>'to'
                   WHERE x->>'from'=ready AND x->'actors' ? 'director' AND NOT (d->>'terminal')::boolean
                     AND d->>'kind' NOT IN ('implementation','review')
                     AND (jsonb_array_length(coalesce(d->'owners','[]'::jsonb))>0 OR d->'notify'->>'kind'<>'none')) THEN
        RAISE EXCEPTION 'the director must be able to withdraw admitted work from the ready stage'; END IF;
    SELECT x->>'name' INTO implementation FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'kind'='implementation';
    SELECT count(*) INTO claims FROM ticket_board.scheduling_claim_transitions(cfg);
    IF claims<>1 THEN RAISE EXCEPTION 'scheduling needs exactly one implementer move from the ready stage into implementation'; END IF;
    -- SYRD-678: a claimant takes pulled work of any kind, which a routing scope says it must not.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
               WHERE r ? 'routing_scope' AND r->'ephemeral'='true'::jsonb AND ticket_board.role_has_kind(r, 'implementer')) THEN
        RAISE EXCEPTION 'a role with a routing_scope would claim pulled work of any kind: make it non-ephemeral or drop the scope'; END IF;
    IF EXISTS (SELECT FROM ticket_board.scheduling_claim_transitions(cfg) x
               WHERE coalesce((x->>'require_reason')::boolean,false) OR coalesce((x->>'owner_scoped')::boolean,false)) THEN
        RAISE EXCEPTION 'the claim transition cannot require a reason or be owner-scoped: a pulled claim has neither'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=policy->>'release_after' AND x->>'kind'='review') THEN
        RAISE EXCEPTION 'scheduling release_after must be a review stage'; END IF;
    seconds:=policy->'idle_alert_seconds';
    IF seconds IS NOT NULL AND (jsonb_typeof(seconds)<>'number' OR (seconds#>>'{}')::numeric<>trunc((seconds#>>'{}')::numeric)
       OR (seconds#>>'{}')::numeric NOT BETWEEN 60 AND 86400) THEN
        RAISE EXCEPTION 'scheduling idle_alert_seconds must be an integer from 60 to 86400'; END IF;
    -- SYRD-628: provider cohorts, one per runtime: a target of running
    -- workers, a ceiling on the tickets they hold at once, model and effort pins.
    cohorts:=policy->'cohorts';
    IF cohorts IS NOT NULL THEN
        IF jsonb_typeof(cohorts)<>'array' OR jsonb_array_length(cohorts)=0 THEN
            RAISE EXCEPTION 'scheduling cohorts must be a non-empty list'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE jsonb_typeof(c)<>'object' OR NOT c ? 'runtime'
                   OR EXISTS (SELECT FROM jsonb_object_keys(c) k WHERE k NOT IN ('runtime','target','ceiling','model','effort'))) THEN
            RAISE EXCEPTION 'invalid scheduling cohort'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c
                   WHERE jsonb_typeof(c->'runtime') IS DISTINCT FROM 'string'
                      OR c->>'runtime' NOT IN ('claude','codex','agy','hermes','copilot'))
           OR (SELECT count(DISTINCT c->>'runtime') FROM jsonb_array_elements(cohorts) c)<>jsonb_array_length(cohorts) THEN
            RAISE EXCEPTION 'a scheduling cohort names a known runtime, and each runtime once'; END IF;
        IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE NOT (c ? 'target' OR c ? 'ceiling')) THEN
            RAISE EXCEPTION 'a scheduling cohort needs a target or a ceiling'; END IF;
        FOREACH field IN ARRAY ARRAY['target','ceiling'] LOOP
            IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE c ? field
                       AND (jsonb_typeof(c->field)<>'number' OR (c->>field)::numeric<>trunc((c->>field)::numeric)
                            OR (c->>field)::numeric NOT BETWEEN 0 AND 64)) THEN
                RAISE EXCEPTION 'scheduling cohort % must be an integer from 0 to 64', field; END IF;
        END LOOP;
        FOREACH field IN ARRAY ARRAY['model','effort'] LOOP
            IF EXISTS (SELECT FROM jsonb_array_elements(cohorts) c WHERE c ? field
                       AND (jsonb_typeof(c->field)<>'string' OR c->>field<>btrim(c->>field)
                            OR length(c->>field) NOT BETWEEN 1 AND 120)) THEN
                RAISE EXCEPTION 'scheduling cohort % must be a name of 1 to 120 characters', field; END IF;
        END LOOP;
    END IF;
    -- Pinned rework waits where admitted work waits: the serial diversion
    -- destination is the ready stage, so a kickback to a busy author is
    -- claimable by that author alone, through the same claim transition.
    IF cfg->'queue'->>'stage' IS DISTINCT FROM ready THEN
        RAISE EXCEPTION 'scheduling needs the queue to be the ready stage'; END IF;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.router_assignable_roles()
RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT coalesce(array_agg(o ORDER BY o), ARRAY[]::text[])
    FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s,
         jsonb_array_elements_text(s->'owners') o
    WHERE s->>'kind' = 'implementation'
      AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
                  WHERE r->>'name' = o AND (r->>'active')::boolean AND ticket_board.role_has_kind(r, 'implementer'));
$$;

CREATE OR REPLACE FUNCTION ticket_board.router_scope_refusal(p_ticket jsonb, p_role text)
RETURNS text LANGUAGE sql STABLE AS $$
    SELECT format('router gives %s only work flagged %s, and %s is not; choose another owner, or return it to the '
                  'Director, who may flag it or assign %s directly', p_role, r->'routing_scope'->>'flag', p_ticket->>'id', p_role)
      FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
     WHERE r->>'name' = p_role AND r ? 'routing_scope'
       AND NOT ticket_board.workflow_flag(p_ticket, r->'routing_scope'->>'flag', ticket_board.declared_workflow());
$$;

CREATE OR REPLACE FUNCTION ticket_board.router_assign(id text, assignee text, revision text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text := ticket_board.current_app_actor();
    target text := btrim(lower(coalesce(router_assign.assignee, '')));
    ticket_row ticket_board.tickets%ROWTYPE;
    classified text := btrim(coalesce(router_assign.revision, ''));
    message text;
BEGIN
    SELECT * INTO ticket_row FROM ticket_board.tickets t WHERE t.id = router_assign.id FOR UPDATE;
    PERFORM ticket_board.require_router_ticket(ticket_row, actor, 'router_assign', classified);
    PERFORM ticket_board.require_actor(ARRAY[]::text[], 'router_assign');
    IF target = actor OR NOT target = ANY(ticket_board.router_assignable_roles()) THEN
        RAISE EXCEPTION 'router may assign only to an active owner of Implementation (%), not %',
            array_to_string(ticket_board.router_assignable_roles(), ', '), coalesce(nullif(target, ''), '<empty>')
            USING ERRCODE = '42501';
    END IF;
    message := ticket_board.router_scope_refusal(to_jsonb(ticket_row), target);  -- SYRD-678
    IF message IS NOT NULL THEN
        RAISE EXCEPTION '%', message USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('ticket_board.router_ticket', ticket_row.id, true);
    PERFORM set_config('ticket_board.router_action', 'assign', true);
    UPDATE ticket_board.tickets t SET assignee = target WHERE t.id = ticket_row.id;
    PERFORM set_config('ticket_board.router_ticket', '', true);
    PERFORM set_config('ticket_board.router_action', '', true);
    INSERT INTO ticket_board.router_decisions (ticket_id, router_role, decision, assignee, classified_revision)
    VALUES (ticket_row.id, actor, 'assign', target, classified);

    -- The same one notification `reassign` gives, and withheld for the same
    -- reasons: the existing queue decides when the implementer works it.
    SELECT * INTO ticket_row FROM ticket_board.tickets t WHERE t.id = router_assign.id;
    IF ticket_row.queued_for_assignee <> '' OR ticket_board.ticket_has_unresolved_blockers(ticket_row.id)
       OR ticket_board.ticket_serial_waiting(ticket_row.id) THEN
        RETURN;
    END IF;
    message := ticket_row.id || CASE WHEN ticket_row.title <> '' THEN ' -- ' || ticket_row.title ELSE '' END
        || ' is now assigned to you';
    PERFORM ticket_board.enqueue_transition_notification(ticket_row.id, ticket_row.title, ticket_row.state,
        ticket_row.state, ticket_row.assignee, ticket_row.updated_at, ticket_row.ticket_number, message);
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_routing(p_ticket_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    WITH t AS (SELECT * FROM ticket_board.tickets WHERE id = p_ticket_id),
    history AS (
        SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
            'router', d.router_role, 'decision', d.decision, 'assignee', nullif(d.assignee, ''),
            'reason', nullif(d.reason, ''), 'revision', d.classified_revision,
            'decided_at', ticket_board.utc_text(d.decided_at), 'outcome', d.outcome,
            'outcome_role', d.outcome_role, 'outcome_reason', d.outcome_reason,
            'outcome_at', CASE WHEN d.outcome_at IS NOT NULL THEN ticket_board.utc_text(d.outcome_at) END))
            ORDER BY d.decision_id) AS decisions
        FROM ticket_board.router_decisions d WHERE d.ticket_id = p_ticket_id)
    SELECT CASE WHEN ticket_board.router_holds(t.state, t.assignee) OR history.decisions IS NOT NULL THEN
        jsonb_strip_nulls(jsonb_build_object(
            'holder', CASE WHEN ticket_board.router_holds(t.state, t.assignee) THEN t.assignee END,
            'revision', CASE WHEN ticket_board.router_holds(t.state, t.assignee)
                             THEN ticket_board.router_ticket_revision(t.id) END,
            -- SYRD-678: the owners Router may give THIS ticket; a scoped owner
            -- whose flag it does not carry is listed under scopes instead.
            'assignable', CASE WHEN ticket_board.router_holds(t.state, t.assignee)
                               THEN to_jsonb(ARRAY(SELECT a FROM unnest(ticket_board.router_assignable_roles()) a
                                                   WHERE ticket_board.router_scope_refusal(to_jsonb(t), a) IS NULL)) END,
            'scopes', CASE WHEN ticket_board.router_holds(t.state, t.assignee) THEN (
                SELECT jsonb_object_agg(r->>'name', jsonb_build_object('flag', r->'routing_scope'->>'flag',
                           'eligible', ticket_board.router_scope_refusal(to_jsonb(t), r->>'name') IS NULL))
                  FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
                 WHERE r ? 'routing_scope' AND r->>'name' = ANY(ticket_board.router_assignable_roles())) END,
            'decisions', history.decisions))
    END
    FROM t, history;
$$;

REVOKE ALL ON FUNCTION ticket_board.router_scope_refusal(jsonb, text) FROM PUBLIC;

COMMIT;
