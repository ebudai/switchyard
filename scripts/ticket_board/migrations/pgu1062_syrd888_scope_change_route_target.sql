-- SYRD-888 (pgu1062): request_scope_change names the target when it routes a new plan ticket into Implementation.
-- It performed the Director's move by action name alone, so on a workflow where that action leaves triage for more
-- than one stage (sapi: route from analysis to in_progress and to user_review) the executor refused it as "unknown or
-- ambiguous workflow action; specify target", and the whole scope change rolled back.
--
-- Same signature; schema.sql carries the same definition.

BEGIN;

-- The Director's scope change: a plan ticket for the owner -- the open one, or a new one -- that the waiting ticket is
-- blocked on. The Director never edits the plan.
CREATE OR REPLACE FUNCTION ticket_board.request_scope_change(p_ticket text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE who text; ticket ticket_board.tickets; plan jsonb := ticket_board.plan_policy(); plan_id text; created boolean := false;
    reason text := btrim(coalesce(p_reason, '')); impl text; impl_to text; blockers text[];
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'set_blockers');
    who := ticket_board.current_app_actor();
    IF plan IS NULL THEN
        RAISE EXCEPTION 'this board runs no plan gate; the Director turns it on with: switchyard plan-gate <project> enable';
    END IF;
    IF reason = '' OR length(reason) > 4000 THEN
        RAISE EXCEPTION 'a scope change says what the plan is missing (4000 characters at most)';
    END IF;
    SELECT * INTO ticket FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF ticket_board.plan_terminal_stage(ticket.state) OR ticket_board.plan_ticket(ticket) THEN
        RAISE EXCEPTION '% is % a ticket that waits on the plan; a scope change is for open work the plan does not cover',
            p_ticket, CASE WHEN ticket_board.plan_ticket(ticket) THEN 'a plan ticket, not' ELSE 'closed, not' END;
    END IF;
    SELECT t.id INTO plan_id FROM ticket_board.tickets t
     WHERE ticket_board.plan_ticket(t) AND NOT ticket_board.plan_terminal_stage(t.state)
     ORDER BY t.ticket_number DESC LIMIT 1;
    IF plan_id IS NULL THEN
        plan_id := ticket_board.create_ticket(
            format('Plan: scope change for %s', p_ticket),
            format(E'Scope change requested by the Director for %s (%s).\n\n%s\n\nEdit the plan (%s) and, where a ruling belongs on record, the decision log (%s); nothing else. Audit reviews the whole plan, the User approves it.',
                   p_ticket, ticket.title, reason, plan->>'file', plan->>'decision_log'),
            'analysis', 'director', ARRAY[]::text[], '', true, true);
        UPDATE ticket_board.tickets SET workflow_flags = coalesce(workflow_flags, '{}'::jsonb) || '{"plan_change": true}'::jsonb
         WHERE id = plan_id;
        -- The Director's own declared move from where the new ticket waits into Implementation, to the plan owner.
        -- Performed with its target named: the same action can leave triage for more than one stage (SYRD-888: route
        -- to in_progress and to user_review), and the executor refuses a bare name that matches more than one move.
        SELECT x->>'action', x->>'to' INTO impl, impl_to
          FROM jsonb_array_elements(ticket_board.declared_workflow()->'transitions') x
          JOIN jsonb_array_elements(ticket_board.declared_workflow()->'stages') s ON s->>'name' = x->>'to'
         WHERE x->>'from' = (SELECT state FROM ticket_board.tickets WHERE id = plan_id) AND s->>'kind' = 'implementation'
           AND x->>'primitive' = 'move' AND x->'actors' ? 'director' ORDER BY x->>'action', x->>'to' LIMIT 1;
        IF impl IS NULL THEN
            RAISE EXCEPTION 'this workflow has no Director move from triage into Implementation for the plan ticket';
        END IF;
        PERFORM ticket_board.perform_workflow_action(plan_id, impl,
            jsonb_build_object('assignee', plan->>'owner', 'target', impl_to));
        created := true;
    ELSE
        PERFORM ticket_board.append_ticket_comment(plan_id, who,
            format(E'Scope change requested by the Director for %s (%s).\n\n%s', p_ticket, ticket.title, reason));
    END IF;
    SELECT coalesce(array_agg(blocker_ticket_id ORDER BY position), ARRAY[]::text[]) INTO blockers
      FROM ticket_board.ticket_blockers WHERE ticket_id = p_ticket;
    IF NOT plan_id = ANY(blockers) THEN
        PERFORM ticket_board.set_blockers(p_ticket, blockers || plan_id,
            format('Scope change: waits on plan ticket %s. %s', plan_id, left(reason, 300)));
    END IF;
    RETURN jsonb_build_object('ticket_id', p_ticket, 'plan_ticket', plan_id, 'created', created);
END;
$$;

COMMIT;
