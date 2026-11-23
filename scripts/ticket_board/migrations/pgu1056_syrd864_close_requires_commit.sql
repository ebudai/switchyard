-- SYRD-864: a declared document whose move into done does not require the commit is refused when it is applied.
--
-- The declared executor refuses a close without a commit only when the move says require_commit. A document that
-- dropped it was accepted, and a code ticket (not commit_exempt) then closed into done with no commit recorded.
-- validate_declared_workflow now refuses any move into done without require_commit, except a no-code completion
-- (allow_no_code) and, on a board that declares the research stage, its close_on_finding. The rest of the function
-- is pgu1022's, unchanged; workflow_config.validate refuses the same documents (close_commit.py).

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
        -- SYRD-719: or Audit returns a documentation-only finding, out of a stage
        -- its kick-back leaves, to the Documentation stage
        -- (documentation_gate.audit_documentation_return is the same rule).
        IF tr->>'primitive'='return' AND s->>'kind'<>'implementation'
           AND NOT (EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=tr->>'from' AND x->>'kind'='review')
                    AND s->>'kind'<>'review'
                    AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                                WHERE m->>'from'=tr->>'to' AND m->>'to'=tr->>'from' AND m->>'primitive' IN ('move','approve')))
           AND NOT (tr->>'action'='audit_documentation_kick_back' AND tr->>'to'='documentation'
                    AND s->>'kind'='review' AND s->>'gate'='needs_documentation'
                    AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') m
                                WHERE m->>'from'=tr->>'from' AND m->>'action'='audit_kick_back' AND m->>'primitive'='return'))
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
    -- SYRD-864: every move into done records the commit it closes on. A no-code completion (allow_no_code) and,
    -- on a board that declares the research stage, its close_on_finding close without one by design.
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
               WHERE t->>'to' = 'done' AND NOT (t->>'require_commit')::boolean
                 AND NOT coalesce((t->>'allow_no_code')::boolean, false)
                 AND NOT (t->>'action' = 'close_on_finding'
                          AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = 'research'))) THEN
        RAISE EXCEPTION 'a move into done must require the commit it closes on (require_commit): %',
            (SELECT string_agg(format('%s (%s -> done)', t->>'action', t->>'from'), ', ')
               FROM jsonb_array_elements(cfg->'transitions') t
              WHERE t->>'to' = 'done' AND NOT (t->>'require_commit')::boolean
                AND NOT coalesce((t->>'allow_no_code')::boolean, false)
                AND NOT (t->>'action' = 'close_on_finding'
                         AND EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name' = 'research')));
    END IF;
    PERFORM ticket_board.validate_router_declaration(cfg);  -- SYRD-599
END;
$$;
