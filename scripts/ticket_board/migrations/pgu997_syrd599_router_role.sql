-- SYRD-599: an optional Router role chooses the Implementation owner for work the
-- Director admits, or hands it back to triage with a reason; nothing else.
--   * router_assign / router_return: purpose-built, each in a one-ticket,
--     one-statement window that enforce_declared_ticket_update checks
--     (require_router_change); a digest revision (router_ticket_revision)
--     refuses a stale or duplicate decision.
--   * router_holds: Router is a legal Implementation assignee without owning
--     the stage, so it is never an implementer and reserves no slot.
--   * On a board with an active Router the Director's ordinary route into
--     Implementation is Router's unless an owner is named; with Router not
--     running (router_available) it is refused, never given to an implementer.
--   * router_decisions records each decision, and misroutes and corrections.
-- See docs/syrd-599-router-role.md. A document without a router is unaffected.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.router_holds(p_stage text, p_assignee text)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb := ticket_board.declared_workflow();
BEGIN
    IF cfg IS NULL THEN RETURN false; END IF;
    RETURN EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r
                   WHERE r->>'name' = btrim(lower(coalesce(p_assignee, ''))) AND r->>'kind' = 'router'
                     AND (r->>'active')::boolean)
       AND (p_stage IS NULL OR EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
                                       WHERE s->>'name' = p_stage AND s->>'kind' = 'implementation'));
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.ticket_valid_assignee(p_assignee text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH normalized AS (
        SELECT btrim(lower(coalesce(p_assignee, ''))) AS role
    )
    SELECT role = 'unassigned'
        OR role = 'agent'
        OR EXISTS (
            SELECT 1
            FROM ticket_board.workflow_stages ws
            WHERE role = ANY(ws.owner_roles)
        )
        OR ticket_board.router_holds(NULL, role)
    FROM normalized;
$$;

CREATE OR REPLACE FUNCTION ticket_board.require_stage_owner_assignee(p_state text, p_assignee text)
RETURNS void
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    owners text[];
    normalized_assignee text := btrim(lower(coalesce(p_assignee, '')));
BEGIN
    SELECT ws.owner_roles
    INTO owners
    FROM ticket_board.workflow_stages ws
    WHERE ws.name = p_state;

    IF NOT FOUND OR coalesce(cardinality(owners), 0) = 0 THEN
        RETURN;
    END IF;

    IF normalized_assignee = 'unassigned' OR normalized_assignee = ANY(owners)
       OR ticket_board.router_holds(p_state, normalized_assignee) THEN
        RETURN;
    END IF;

    RAISE EXCEPTION 'assignee % is not an owner of stage % (owners: %)',
        coalesce(nullif(btrim(coalesce(p_assignee, '')), ''), '<empty>'),
        p_state,
        array_to_string(owners, ', ');
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.apply_stage_default_assignee_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.state IS DISTINCT FROM NEW.state
       AND current_setting('ticket_board.force_move', true) IS DISTINCT FROM 'on'
       AND NOT coalesce(OLD.manually_controlled, false)
       AND NOT coalesce(NEW.manually_controlled, false)
       AND ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id) IS NOT NULL
       -- SYRD-599: work the Director routed to Router stays Router's.
       AND NOT ticket_board.router_holds(NEW.state, NEW.assignee)
       AND (
           NEW.state <> 'analysis'
           OR NEW.assignee IS NULL
           OR btrim(NEW.assignee) = ''
           OR NEW.assignee = 'unassigned'
       ) THEN
        NEW.assignee := ticket_board.stage_entry_assignee(NEW.state, NEW.assignee, NEW.id);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.transition_target_role(p_state text,p_assignee text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); s jsonb; owners text[];
BEGIN
    IF cfg IS NULL THEN RETURN ticket_board.legacy_transition_target_role(p_state,p_assignee); END IF;
    SELECT x INTO s FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=p_state;
    owners:=ARRAY(SELECT jsonb_array_elements_text(s->'owners'));
    RETURN CASE s->'notify'->>'kind'
      WHEN 'none' THEN NULL WHEN 'fixed_role' THEN s->'notify'->>'role'
      WHEN 'assignee' THEN CASE WHEN p_assignee=ANY(owners) OR ticket_board.router_holds(p_state,p_assignee) THEN p_assignee END
      WHEN 'stage_owner_fallback' THEN CASE WHEN p_assignee=ANY(owners) OR ticket_board.router_holds(p_state,p_assignee) THEN p_assignee ELSE owners[1] END
      ELSE NULL END;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.remember_declared_implementer()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 -- SYRD-599: Router is never anybody's implementer, so it is never remembered as one.
 IF ticket_board.declared_stage_kind(NEW.state)='implementation' AND NOT ticket_board.router_holds(NEW.state,NEW.assignee) THEN
   UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee=NEW.assignee WHERE ticket_id=NEW.id;
 END IF;
 RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION ticket_board.validate_router_declaration(cfg jsonb)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE router jsonb; impl jsonb; triage jsonb;
BEGIN
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r WHERE r->>'kind'='router' AND r->>'name'<>'router') THEN
        RAISE EXCEPTION 'invalid router role: only the role named router may be of kind router'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') r WHERE r->'capabilities' ? 'router_assign' AND r->>'kind'<>'router') THEN
        RAISE EXCEPTION 'invalid router role: router_assign belongs to the router role alone'; END IF;
    SELECT r INTO router FROM jsonb_array_elements(cfg->'roles') r WHERE r->>'kind'='router';
    IF router IS NULL THEN
        IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t WHERE t->>'action'='router_return') THEN
            RAISE EXCEPTION 'invalid router role: router_return needs the router role'; END IF;
        RETURN;
    END IF;
    SELECT s INTO impl FROM jsonb_array_elements(cfg->'stages') s WHERE s->>'kind'='implementation';
    IF (SELECT coalesce(array_agg(c ORDER BY c), ARRAY[]::text[]) FROM jsonb_array_elements_text(router->'capabilities') c)
       IS DISTINCT FROM ARRAY['router_assign'] THEN
        RAISE EXCEPTION 'invalid router role: its capabilities are exactly router_assign'; END IF;
    IF router ? 'serial' AND (router->>'serial')::boolean THEN
        RAISE EXCEPTION 'invalid router role: router never holds a serial slot'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') s
               WHERE s->'owners' ? 'router' OR (s->'notify'->>'kind'='fixed_role' AND s->'notify'->>'role'='router')) THEN
        RAISE EXCEPTION 'invalid router role: router owns no stage'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
               WHERE (t->'actors' ? 'router' OR t->>'relays_decision_of'='router') AND t->>'action'<>'router_return') THEN
        RAISE EXCEPTION 'invalid router role: router_return is its only move'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t WHERE t->>'action'='router_return' AND NOT (
            t->'actors' = '["router"]'::jsonb AND t->>'from' = impl->>'name' AND t->>'to' = 'analysis'
            AND t->>'primitive' = 'move' AND (t->>'owner_scoped')::boolean AND (t->>'require_reason')::boolean
            AND NOT (t->>'require_commit')::boolean AND NOT coalesce((t->>'allow_no_code')::boolean, false)
            AND t->'clear_signoffs' = '[]'::jsonb
            AND coalesce(jsonb_typeof(t->'relays_decision_of'), 'null') = 'null')) THEN
        RAISE EXCEPTION 'invalid router role: router_return moves Implementation to analysis, router alone, owner-scoped, with a reason, and changes nothing else'; END IF;
    IF NOT (router->>'active')::boolean THEN
        RETURN;
    END IF;
    IF router->>'target' IS NULL THEN
        RAISE EXCEPTION 'invalid router role: an active router needs a runtime and pane target to be told about work'; END IF;
    IF impl->'notify'->>'kind' NOT IN ('assignee','stage_owner_fallback') THEN
        RAISE EXCEPTION 'invalid router role: Implementation must notify its assignee for router to be told'; END IF;
    SELECT s INTO triage FROM jsonb_array_elements(cfg->'stages') s WHERE s->>'name'='analysis';
    IF triage IS NULL OR (triage->>'terminal')::boolean OR triage->'owners' IS DISTINCT FROM '["director"]'::jsonb
       OR triage->'notify'->>'kind' NOT IN ('assignee','stage_owner_fallback') THEN
        RAISE EXCEPTION 'invalid router role: router returns work to analysis, which must be the director''s alone and announced'; END IF;
    IF (SELECT count(*) FROM jsonb_array_elements(cfg->'transitions') t WHERE t->>'action'='router_return') <> 1 THEN
        RAISE EXCEPTION 'invalid router role: an active router needs its router_return'; END IF;
    IF NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'transitions') t
        WHERE t->>'action'='implementer_kick_back' AND t->>'from'=impl->>'name' AND t->>'to'='analysis'
          AND t->>'primitive'='move' AND (t->>'owner_scoped')::boolean AND (t->>'require_reason')::boolean
          AND (SELECT array_agg(a ORDER BY a) FROM jsonb_array_elements_text(t->'actors') a)
              = (SELECT array_agg(o ORDER BY o) FROM jsonb_array_elements_text(impl->'owners') o)) THEN
        RAISE EXCEPTION 'invalid router role: every Implementation owner needs implementer_kick_back to analysis, owner-scoped with a reason, to hand back a misrouted ticket'; END IF;
END;
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

CREATE OR REPLACE FUNCTION ticket_board.apply_declared_workflow(cfg jsonb)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE r jsonb; s jsonb; tr jsonb; rev bigint; pos int:=0; old_cfg jsonb; item record;
BEGIN
    IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN
        RAISE EXCEPTION 'only director may configure workflow' USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'));
    IF nullif(current_setting('ticket_board.project',true),'') IS NULL OR cfg->>'project' IS DISTINCT FROM current_setting('ticket_board.project',true) THEN
        RAISE EXCEPTION 'workflow must match selected project identity'; END IF;
    IF EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') runtime_role WHERE runtime_role->>'target' IS NOT NULL AND runtime_role->>'target' IS DISTINCT FROM (cfg->>'project')||'-'||(runtime_role->>'name')||':0.0') THEN
        RAISE EXCEPTION 'foreign project/role runtime target'; END IF;
    PERFORM ticket_board.validate_declared_workflow(cfg);
    LOCK TABLE ticket_board.tickets, ticket_board.workflow_stages, ticket_board.workflow_transitions IN SHARE ROW EXCLUSIVE MODE;
    old_cfg := ticket_board.declared_workflow();
    IF old_cfg IS NOT NULL AND old_cfg->>'project' IS DISTINCT FROM cfg->>'project' THEN RAISE EXCEPTION 'cannot change workflow tenant identity'; END IF;
    IF old_cfg=cfg THEN SELECT revision INTO rev FROM ticket_board.workflow_configuration; RETURN rev; END IF;
    -- Stage removal is deliberate, empty, and cannot erase outstanding review.
    IF jsonb_typeof(cfg->'remove_stages') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'explicit remove_stages list required'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws WHERE NOT EXISTS
        (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name)
        AND NOT cfg->'remove_stages' ? ws.name) THEN
        RAISE EXCEPTION 'configuration omits an existing stage; preserve it or explicitly request removal'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_stages ws CROSS JOIN ticket_board.tickets t
        JOIN ticket_board.workflow_stages current_stage ON current_stage.name=t.state
        WHERE ws.exit_signoff_field IS NOT NULL AND NOT current_stage.is_terminal
        AND (ws.entry_gate_field IS NULL OR ticket_board.workflow_flag(to_jsonb(t),ws.entry_gate_field,coalesce(old_cfg,cfg)))
        AND NOT ticket_board.workflow_flag(to_jsonb(t),ws.exit_signoff_field,coalesce(old_cfg,cfg))
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name
          AND x->>'signoff'=ws.exit_signoff_field AND x->>'gate' IS NOT DISTINCT FROM ws.entry_gate_field)) THEN
        RAISE EXCEPTION 'configuration would remove an outstanding review obligation'; END IF;
    -- Whole-graph application must carry existing stages and attribution identities.
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=t.state)) THEN
        RAISE EXCEPTION 'configuration omits an occupied stage; preserve live/history state'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t WHERE NOT EXISTS
       (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=t.assignee)) THEN
        RAISE EXCEPTION 'configuration omits an assigned role; retain it inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.workflow_roles r WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') x WHERE x->>'name'=r.name)) THEN
        RAISE EXCEPTION 'retain historical roles as inactive'; END IF;
    IF EXISTS (SELECT FROM ticket_board.ticket_notification_state ns
        WHERE ns.awaiting_role<>'' AND ns.awaiting_since_at>clock_timestamp()-interval '4 hours'
        AND NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'roles') awaited WHERE awaited->>'name'=ns.awaiting_role
            AND (awaited->>'active')::boolean AND awaited->>'target' IS NOT NULL)) THEN
        RAISE EXCEPTION 'role has an unresolved handoff; clear or retarget it before retirement'; END IF;
    FOR r IN SELECT value FROM jsonb_array_elements(cfg->'roles') LOOP
        INSERT INTO ticket_board.workflow_roles(name,definition) VALUES(r->>'name',r)
        ON CONFLICT(name) DO UPDATE SET definition=EXCLUDED.definition;
    END LOOP;
    -- Referenced identities are not removed, including historical comment actors.
    ALTER TABLE ticket_board.workflow_stages DROP CONSTRAINT IF EXISTS workflow_stages_name_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_state_check;
    ALTER TABLE ticket_board.tickets DROP CONSTRAINT IF EXISTS tickets_assignee_check;
    ALTER TABLE ticket_board.ticket_notification_queue DROP CONSTRAINT IF EXISTS ticket_notification_queue_target_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_awaiting_role_check;
    ALTER TABLE ticket_board.ticket_notification_state DROP CONSTRAINT IF EXISTS ticket_notification_state_last_implementer_assignee_check;
    -- Empty notification-role sentinels stay supported; array and sentinel refs are validated below.
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='ticket_board.tickets'::regclass AND conname='tickets_workflow_state_fk') THEN
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_state_fk FOREIGN KEY(state) REFERENCES ticket_board.workflow_stages(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.tickets ADD CONSTRAINT tickets_workflow_role_fk FOREIGN KEY(assignee) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE ticket_board.ticket_notification_queue ADD CONSTRAINT notification_workflow_role_fk FOREIGN KEY(target_role) REFERENCES ticket_board.workflow_roles(name) DEFERRABLE INITIALLY DEFERRED;
    END IF;
    -- Only the reviewed whole-graph operation can replace stage/transition data.
    DELETE FROM ticket_board.workflow_transitions;
    UPDATE ticket_board.workflow_stages SET gate_skip_to=NULL;
    DELETE FROM ticket_board.workflow_stages ws WHERE NOT EXISTS (SELECT FROM jsonb_array_elements(cfg->'stages') x WHERE x->>'name'=ws.name);
    -- Move ranks out of the desired range before writing the final contiguous order.
    UPDATE ticket_board.workflow_stages SET rank=rank+(SELECT coalesce(max(rank),0)+jsonb_array_length(cfg->'stages')+1 FROM ticket_board.workflow_stages);
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        INSERT INTO ticket_board.workflow_stages(name,display_label,rank,owner_roles,entry_gate_field,gate_skip_to,exit_signoff_field,is_terminal)
        VALUES(s->>'name',s->>'label',pos,ARRAY(SELECT jsonb_array_elements_text(s->'owners')),s->>'gate',NULL,s->>'signoff',(s->>'terminal')::boolean)
        ON CONFLICT(name) DO UPDATE SET display_label=EXCLUDED.display_label,rank=EXCLUDED.rank,owner_roles=EXCLUDED.owner_roles,
            entry_gate_field=EXCLUDED.entry_gate_field,gate_skip_to=NULL,exit_signoff_field=EXCLUDED.exit_signoff_field,is_terminal=EXCLUDED.is_terminal;
        pos:=pos+1;
    END LOOP;
    FOR s IN SELECT value FROM jsonb_array_elements(cfg->'stages') LOOP
        UPDATE ticket_board.workflow_stages SET gate_skip_to=s->>'skip_to' WHERE name=s->>'name';
    END LOOP;
    DELETE FROM ticket_board.workflow_transitions;
    FOR tr IN SELECT value FROM jsonb_array_elements(cfg->'transitions') LOOP
        INSERT INTO ticket_board.workflow_transitions(from_stage,to_stage,action_name,allowed_roles,owner_scoped,director_override)
        VALUES(tr->>'from',tr->>'to',tr->>'action',ARRAY(SELECT jsonb_array_elements_text(tr->'actors')),(tr->>'owner_scoped')::boolean,false);
    END LOOP;
    INSERT INTO ticket_board.workflow_revisions(actor,document) VALUES(ticket_board.current_app_actor(),cfg) RETURNING revision INTO rev;
    INSERT INTO ticket_board.workflow_configuration(singleton,revision,document) VALUES(true,rev,cfg)
      ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,document=EXCLUDED.document;
    FOR item IN SELECT * FROM jsonb_each_text(coalesce(cfg->'reassign','{}'::jsonb)) LOOP
        IF NOT EXISTS (SELECT FROM ticket_board.workflow_stages WHERE name=item.key AND item.value=ANY(owner_roles)) THEN
            RAISE EXCEPTION 'invalid deliberate reassignment'; END IF;
        UPDATE ticket_board.tickets SET assignee=item.value WHERE state=item.key AND assignee<>item.value;
    END LOOP;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_stages ws ON ws.name=t.state
       WHERE cardinality(ws.owner_roles)>0 AND t.assignee<>'unassigned' AND NOT t.assignee=ANY(ws.owner_roles)
         AND NOT ticket_board.router_holds(t.state,t.assignee)) THEN
        RAISE EXCEPTION 'configuration would orphan live ticket ownership; supply deliberate reassignment'; END IF;
    IF EXISTS (SELECT FROM ticket_board.tickets t JOIN ticket_board.workflow_roles r ON r.name=t.assignee
        JOIN ticket_board.workflow_stages ws ON ws.name=t.state WHERE NOT ws.is_terminal AND NOT (r.definition->>'active')::boolean AND t.assignee<>'unassigned') THEN
        RAISE EXCEPTION 'inactive role still owns active tickets'; END IF;
    PERFORM pg_notify('ticket_board_state_transition',jsonb_build_object('kind','workflow','revision',rev)::text);
    RETURN rev;
END;
$$;

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

-- SYRD-599: the Router role. A low-cost role whose only job is to choose the
-- implementation owner for work the Director has already admitted to
-- Implementation, or to hand it back to the Director's triage with a reason.
-- Its authority is two purpose-built operations, never the Director's generic
-- ones: `router_assign` (an assignee-only change to an Implementation owner)
-- and `router_return` (the declared transition back to `analysis`). Each opens
-- a one-ticket window that the declared-update trigger checks, so a crafted
-- statement or a generic executor cannot take either move. Router is a legal
-- Implementation assignee without owning the stage, so it is never an
-- implementer: it holds no serial slot and any number of tickets can wait on
-- it (router_holds, below).
CREATE TABLE IF NOT EXISTS ticket_board.router_decisions (
    decision_id bigserial PRIMARY KEY,
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    router_role text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('assign', 'return')),
    assignee text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    classified_revision text NOT NULL CHECK (classified_revision ~ '^[0-9a-f]{32}$'),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- What became of an assignment: the implementer handed it back as not
    -- theirs (`misroute`), or the Director moved it to somebody else
    -- (`director_correction`). Null while it stands. The canary prices both.
    outcome text CHECK (outcome IS NULL OR outcome IN ('misroute', 'director_correction')),
    outcome_role text,
    outcome_reason text,
    outcome_at timestamptz,
    CHECK ((decision = 'assign') = (assignee <> '')),
    CHECK ((decision = 'return') = (reason <> '')),
    CHECK ((outcome IS NULL) = (outcome_at IS NULL)),
    CHECK (outcome IS NULL OR decision = 'assign')
);
CREATE INDEX IF NOT EXISTS router_decisions_ticket ON ticket_board.router_decisions (ticket_id, decision_id);

-- The ticket as Router classified it: everything its one read shows -- every
-- field but the bookkeeping a write stamps, the blockers, the comments (where
-- an ownership decision is often written) and the attachments -- and the
-- assignment round. Anything added or changed after Router read it, a
-- Director's comment included, is a different revision, and the stale decision
-- is refused rather than applied to work Router never saw.
CREATE OR REPLACE FUNCTION ticket_board.router_ticket_revision(p_ticket_id text)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT md5(jsonb_build_object(
        'ticket', to_jsonb(t) - ARRAY['updated_at', 'updated_text', 'row_updated_at', 'source_json', 'imported_at',
                                      'ticket_number'],
        'blockers', coalesce((SELECT jsonb_agg(jsonb_build_array(b.blocker_ticket_id, b.resolved) ORDER BY b.position)
            FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id), '[]'::jsonb),
        'comments', coalesce((SELECT jsonb_agg(jsonb_build_array(c.id, c.who, c.text, c.urgent) ORDER BY c.id)
            FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id), '[]'::jsonb),
        'attachments', coalesce((SELECT jsonb_agg(jsonb_build_array(a.path, a.is_primary, a.metadata) ORDER BY a.position)
            FROM ticket_board.ticket_attachments a WHERE a.ticket_id = t.id), '[]'::jsonb),
        'round', (SELECT ns.current_assignment_at FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = t.id)
    )::text)
    FROM ticket_board.tickets t WHERE t.id = p_ticket_id;
$$;

-- Whether Router can be told about work: an active router whose pane has
-- registered the runtime and target the document declares. Without one, its
-- notice would requeue for ever and the ticket would sit with nobody working it,
-- so the automatic route refuses instead (see enforce_declared_ticket_update).
CREATE OR REPLACE FUNCTION ticket_board.router_available()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT EXISTS (SELECT FROM ticket_board.role_runtime_assignments a
                   JOIN ticket_board.workflow_roles r ON r.name = a.role
                   WHERE r.definition->>'kind' = 'router' AND (r.definition->>'active')::boolean
                     AND r.definition->>'runtime' = a.runtime AND r.definition->>'target' = a.actual_target);
$$;

-- The declared Implementation stage and its active owners: the only roles
-- Router may choose, and never Router itself (it owns no stage).
CREATE OR REPLACE FUNCTION ticket_board.router_assignable_roles()
RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT coalesce(array_agg(o ORDER BY o), ARRAY[]::text[])
    FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') s,
         jsonb_array_elements_text(s->'owners') o
    WHERE s->>'kind' = 'implementation'
      AND EXISTS (SELECT FROM jsonb_array_elements(ticket_board.declared_workflow()->'roles') r
                  WHERE r->>'name' = o AND (r->>'active')::boolean AND r->>'kind' = 'implementer');
$$;

-- Who may call a Router operation, and on what. Both checks are the database's
-- own: the HTTP layer asks the same questions, and neither trusts the other.
CREATE OR REPLACE FUNCTION ticket_board.require_router_ticket(
    p_ticket ticket_board.tickets, p_actor text, p_operation text, p_revision text)
RETURNS void LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF ticket_board.current_actor_role() <> 'ticket_board_service' THEN
        RAISE EXCEPTION 'role % cannot call %', ticket_board.current_actor_role(), p_operation USING ERRCODE = '42501';
    END IF;
    IF ticket_board.declared_workflow() IS NULL OR NOT ticket_board.router_holds(NULL, p_actor) THEN
        RAISE EXCEPTION 'role % cannot call %: only the active router role may', coalesce(p_actor, '<none>'), p_operation
            USING ERRCODE = '42501';
    END IF;
    IF p_ticket.id IS NULL THEN
        RAISE EXCEPTION 'ticket not found';
    END IF;
    IF NOT ticket_board.router_holds(p_ticket.state, p_ticket.assignee) OR p_ticket.assignee IS DISTINCT FROM p_actor THEN
        RAISE EXCEPTION '% is not % work: it is % / % (router acts only on Implementation tickets assigned to it)',
            p_ticket.id, p_actor, p_ticket.state, p_ticket.assignee USING ERRCODE = '42501';
    END IF;
    IF p_ticket.manually_controlled THEN
        RAISE EXCEPTION '% is held by the Director; router does not act on it', p_ticket.id USING ERRCODE = '42501';
    END IF;
    IF btrim(coalesce(p_revision, '')) = '' THEN
        RAISE EXCEPTION '% requires the ticket revision router classified', p_operation;
    END IF;
    IF p_revision IS DISTINCT FROM ticket_board.router_ticket_revision(p_ticket.id) THEN
        RAISE EXCEPTION 'stale revision for %: the ticket changed after router read it; read it again', p_ticket.id
            USING ERRCODE = '40001';
    END IF;
END;
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

CREATE OR REPLACE FUNCTION ticket_board.router_return(id text, reason text, revision text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    actor text := ticket_board.current_app_actor();
    ticket_row ticket_board.tickets%ROWTYPE;
    classified text := btrim(coalesce(router_return.revision, ''));
    normalized_reason text := btrim(coalesce(router_return.reason, ''));
    tr jsonb;
BEGIN
    SELECT * INTO ticket_row FROM ticket_board.tickets t WHERE t.id = router_return.id FOR UPDATE;
    PERFORM ticket_board.require_router_ticket(ticket_row, actor, 'router_return', classified);
    IF normalized_reason = '' THEN
        RAISE EXCEPTION 'router_return requires a non-empty reason';
    END IF;
    IF length(normalized_reason) > 2000 THEN
        RAISE EXCEPTION 'router_return reason is limited to 2000 characters';
    END IF;
    SELECT x INTO tr FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
    WHERE x->>'action' = 'router_return' AND x->>'from' = ticket_row.state AND x->'actors' ? actor;
    IF tr IS NULL THEN
        RAISE EXCEPTION 'this workflow declares no router_return for %', actor USING ERRCODE = '42501';
    END IF;
    PERFORM ticket_board.append_ticket_comment(ticket_row.id, actor,
        'Router returned this to the Director''s triage. Reason: ' || normalized_reason);
    PERFORM set_config('ticket_board.router_ticket', ticket_row.id, true);
    PERFORM set_config('ticket_board.router_action', 'return', true);
    PERFORM set_config('ticket_board.workflow_action', 'router_return', true);
    UPDATE ticket_board.tickets t SET state = tr->>'to', assignee = 'director' WHERE t.id = ticket_row.id;
    PERFORM set_config('ticket_board.workflow_action', '', true);
    PERFORM set_config('ticket_board.router_ticket', '', true);
    PERFORM set_config('ticket_board.router_action', '', true);
    INSERT INTO ticket_board.router_decisions (ticket_id, router_role, decision, reason, classified_revision)
    VALUES (ticket_row.id, actor, 'return', normalized_reason, classified);
END;
$$;

-- The trigger's half of the window: whatever statement runs inside it may make
-- exactly the one change its operation is for, by router, on router's ticket.
CREATE OR REPLACE FUNCTION ticket_board.require_router_change(
    previous ticket_board.tickets, proposed ticket_board.tickets, actor text)
RETURNS void LANGUAGE plpgsql STABLE AS $$
DECLARE
    operation text := current_setting('ticket_board.router_action', true);
    -- What the window may move, and what the row records about any write.
    -- `ticket_number` is generated, so a BEFORE trigger sees it unset.
    bookkeeping text[] := ARRAY['state', 'assignee', 'updated_at', 'updated_text', 'row_updated_at', 'source_json',
        'queued_for_assignee', 'queued_behind_ticket', 'parked', 'ticket_number'];
    changed text;
BEGIN
    IF NOT ticket_board.router_holds(previous.state, actor) OR previous.assignee IS DISTINCT FROM actor THEN
        RAISE EXCEPTION 'the router window is router''s, on its own Implementation ticket' USING ERRCODE = '42501';
    END IF;
    SELECT string_agg(o.key, ', ' ORDER BY o.key) INTO changed
    FROM jsonb_each(to_jsonb(previous) - bookkeeping) o
    WHERE o.value IS DISTINCT FROM (to_jsonb(proposed) - bookkeeping) -> o.key;
    IF changed IS NOT NULL THEN
        RAISE EXCEPTION 'router may change only who owns %, not %', previous.id, changed USING ERRCODE = '42501';
    END IF;
    IF operation = 'assign' THEN
        IF proposed.state IS DISTINCT FROM previous.state OR proposed.assignee = actor
           OR NOT proposed.assignee = ANY(ticket_board.router_assignable_roles()) THEN
            RAISE EXCEPTION 'router_assign changes only the assignee, to an Implementation owner' USING ERRCODE = '42501';
        END IF;
    ELSIF operation = 'return' THEN
        IF proposed.state = previous.state OR proposed.assignee IS DISTINCT FROM 'director'
           OR current_setting('ticket_board.workflow_action', true) IS DISTINCT FROM 'router_return' THEN
            RAISE EXCEPTION 'router_return goes to the Director''s triage and nowhere else' USING ERRCODE = '42501';
        END IF;
    ELSE
        RAISE EXCEPTION 'unknown router operation' USING ERRCODE = '42501';
    END IF;
END;
$$;

-- What Router needs from its one read of a ticket: the revision to classify
-- against and the owners it may choose. Null unless router holds the ticket
-- and it has no history; the history is the Director's record of each decision
-- and what became of it.
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
            'assignable', CASE WHEN ticket_board.router_holds(t.state, t.assignee)
                               THEN to_jsonb(ticket_board.router_assignable_roles()) END,
            'decisions', history.decisions))
    END
    FROM t, history;
$$;

-- What became of an assignment, recorded as it happens. An implementer handing
-- Router's pick back with implementer_kick_back is a misroute; the Director
-- moving it to somebody else is a correction. Both are priced by the canary.
CREATE OR REPLACE FUNCTION ticket_board.note_router_outcome()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    open_id bigint;
    actor text := coalesce(nullif(current_setting('ticket_board.workflow_actor', true), ''),
                           nullif(current_setting('ticket_board.caller_role', true), ''));
    kind text;
    said text;
BEGIN
    IF ticket_board.declared_workflow() IS NULL
       OR ticket_board.declared_stage_kind(OLD.state) IS DISTINCT FROM 'implementation'
       OR OLD.assignee IS NOT DISTINCT FROM NEW.assignee AND OLD.state IS NOT DISTINCT FROM NEW.state THEN
        RETURN NULL;
    END IF;
    SELECT d.decision_id INTO open_id FROM ticket_board.router_decisions d
    WHERE d.ticket_id = OLD.id AND d.decision = 'assign' AND d.assignee = OLD.assignee AND d.outcome IS NULL
      AND d.decision_id = (SELECT max(x.decision_id) FROM ticket_board.router_decisions x WHERE x.ticket_id = OLD.id);
    IF open_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF OLD.state IS DISTINCT FROM NEW.state
       AND current_setting('ticket_board.workflow_action', true) = 'implementer_kick_back' THEN
        kind := 'misroute';
        SELECT c.text INTO said FROM ticket_board.ticket_comments c
        WHERE c.ticket_id = OLD.id AND c.who = OLD.assignee ORDER BY c.id DESC LIMIT 1;
    ELSIF OLD.state IS NOT DISTINCT FROM NEW.state AND actor = 'director' THEN
        kind := 'director_correction';
    ELSE
        RETURN NULL;
    END IF;
    UPDATE ticket_board.router_decisions
    SET outcome = kind, outcome_role = coalesce(actor, OLD.assignee), outcome_reason = said, outcome_at = clock_timestamp()
    WHERE decision_id = open_id;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS tickets_zz_router_outcome ON ticket_board.tickets;
CREATE TRIGGER tickets_zz_router_outcome AFTER UPDATE ON ticket_board.tickets
FOR EACH ROW EXECUTE FUNCTION ticket_board.note_router_outcome();

REVOKE ALL ON ticket_board.router_decisions FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.router_assign(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.router_return(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_router_change(ticket_board.tickets, ticket_board.tickets, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.require_router_ticket(ticket_board.tickets, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.note_router_outcome() FROM PUBLIC;
-- schema.sql stops here: the roles below do not exist yet when it runs, and rbac.sql grants the same.
GRANT EXECUTE ON FUNCTION ticket_board.router_assign(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.router_return(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_routing(text) TO ticket_board_service;
GRANT SELECT ON ticket_board.router_decisions TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.router_holds(text, text) TO ticket_board_service, ticket_board_listener;

COMMIT;
