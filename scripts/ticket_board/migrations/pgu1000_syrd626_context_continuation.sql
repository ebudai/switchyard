-- SYRD-626: an ephemeral implementer keeps its conversation from one ticket to
-- the next in the same declared parent/child hierarchy.
--
-- Under a pull policy every newly handed ticket starts on a cleared
-- conversation (SYRD-135, SYRD-540). The Director packages a feature as bounded
-- child tickets, and clearing between two of them discards exactly the context
-- the package was cut to keep. So the first hand-off of a ticket to its pulled
-- author decides, once and durably, whether the pane's current conversation
-- continues. The decision is `continue` only when every condition can be
-- proven from the board:
--
--  * the Director has not asked for a fresh context for the ticket;
--  * the conversation the pane holds belongs to another ticket that is the
--    role's latest conversation -- nothing was cleared for, continued into or
--    restored to in between;
--  * that ticket has passed its configured release gate (a pull_releases row,
--    written only by the declared approval out of scheduling.release_after);
--  * the new ticket is assigned to this role in implementation;
--  * the two tickets' parent chains, read from the board's parent links, share
--    at least one ticket (parent and child, siblings, deeper descendants);
--  * no other work of this role is waiting for SYRD-540 to restore it;
--  * the role still runs the runtime that conversation was proven on, the pane
--    is still in that very session from its registered process, and in that
--    session's worktree.
--
-- Anything else is `fresh`, and the ordinary clear follows. A check that could
-- not be completed -- a parent link that loops or names a ticket not on this
-- board, a conversation never confirmed, evidence the pane did not report -- is
-- `fresh` too, and tells the Director once. Blockers, titles, branches, timing
-- and queue order are never consulted.
--
-- A continuation binds the new ticket to the same proven session, so a later
-- rework of either ticket is restored by SYRD-540 as before, and a clear for
-- unrelated work supersedes both. The decision row is the board's evidence: it
-- is returned in the ticket's JSON and in ticket_context_status.
BEGIN;

CREATE TABLE IF NOT EXISTS ticket_board.ticket_context_continuations (
    ticket_id text NOT NULL REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (btrim(role) <> ''),
    decision text NOT NULL CHECK (decision IN ('continue', 'fresh')),
    -- The ticket whose conversation the pane held, when there was one.
    from_ticket text,
    -- The topmost and the nearest ticket the two parent chains share.
    shared_root text,
    shared_ancestor text,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    -- True when the check could not be completed: the Director is told once.
    ambiguous boolean NOT NULL DEFAULT false,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- The assignment round it was decided for: the note goes with that round's delivery only.
    round_at timestamptz,
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When the ticket was bound to the continued conversation.
    applied_at timestamptz,
    PRIMARY KEY (ticket_id, role),
    CHECK (decision = 'fresh' OR (from_ticket IS NOT NULL AND shared_root IS NOT NULL AND shared_ancestor IS NOT NULL
                                  AND NOT ambiguous)),
    CHECK (applied_at IS NULL OR decision = 'continue')
);

-- The Director's "start this one fresh", at a package boundary, before delivery.
CREATE TABLE IF NOT EXISTS ticket_board.ticket_context_fresh_requests (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    requested_by text NOT NULL,
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- A ticket's parent chain, itself first, from the board's parent links. The
-- chain stops where a link loops or names a ticket this board does not have,
-- and says so: what lies beyond it cannot be proven.
CREATE OR REPLACE FUNCTION ticket_board.ticket_lineage(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE chain text[] := ARRAY[p_ticket]; cur text := p_ticket; nxt text;
BEGIN
    IF NOT EXISTS (SELECT FROM ticket_board.tickets WHERE id = p_ticket) THEN
        RETURN jsonb_build_object('chain', '[]'::jsonb, 'problem', format('%s is not on this board', p_ticket));
    END IF;
    LOOP
        SELECT nullif(btrim(parent_id), '') INTO nxt FROM ticket_board.tickets WHERE id = cur;
        EXIT WHEN nxt IS NULL;
        IF nxt = ANY(chain) THEN
            RETURN jsonb_build_object('chain', to_jsonb(chain),
                                      'problem', format('the parent links of %s loop back to %s', p_ticket, nxt));
        END IF;
        IF NOT EXISTS (SELECT FROM ticket_board.tickets WHERE id = nxt) THEN
            RETURN jsonb_build_object('chain', to_jsonb(chain),
                                      'problem', format('%s names parent %s, which is not on this board', cur, nxt));
        END IF;
        chain := chain || nxt;
        cur := nxt;
    END LOOP;
    RETURN jsonb_build_object('chain', to_jsonb(chain), 'problem', NULL);
END;
$$;

-- When p_role's pane entered p_ticket's conversation: its clear, or the continuation into it.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_entered_at(p_ticket text, p_role text)
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce(
        (SELECT c.cleared_at FROM ticket_board.ticket_role_session_clears c WHERE c.ticket_id = p_ticket AND c.role = p_role),
        (SELECT k.applied_at FROM ticket_board.ticket_context_continuations k
          WHERE k.ticket_id = p_ticket AND k.role = p_role AND k.decision = 'continue'));
$$;

-- Every time p_role's pane entered a ticket's conversation, with the checkout
-- it left the previous one at: a clear (the SessionStart its binding recorded),
-- a continuation (the checkout observed when it was decided), or a confirmed
-- restore. `bound` is whether the entered ticket's conversation has a binding.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_entries(p_role text)
RETURNS TABLE(ticket_id text, at timestamptz, kind text, checkout jsonb, bound boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT c.ticket_id, c.cleared_at, 'clear', x.evidence->'session_start'->'checkout', x.ticket_id IS NOT NULL
      FROM ticket_board.ticket_role_session_clears c
      LEFT JOIN ticket_board.ticket_role_contexts x ON x.ticket_id = c.ticket_id AND x.role = c.role
     WHERE c.role = p_role
    UNION ALL
    SELECT k.ticket_id, k.applied_at, 'continuation', k.evidence->'observed'->'checkout', true
      FROM ticket_board.ticket_context_continuations k
     WHERE k.role = p_role AND k.decision = 'continue' AND k.applied_at IS NOT NULL
    UNION ALL
    SELECT r.ticket_id, r.resolved_at, 'restore', NULL::jsonb, true
      FROM ticket_board.ticket_context_restores r
     WHERE r.role = p_role AND r.outcome = 'confirmed';
$$;

-- SYRD-135's "is the first hand-off still to come?": no clear recorded, and no
-- continuation applied -- a continued ticket's conversation is already the pane's.
CREATE OR REPLACE FUNCTION ticket_board.role_session_clear_pending(p_ticket_id text, p_role text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ticket_board, pg_temp
AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('role_session_clear_pending');
    RETURN NOT EXISTS (
        SELECT FROM ticket_board.ticket_role_session_clears
        WHERE ticket_id = p_ticket_id AND role = p_role
    ) AND NOT EXISTS (
        SELECT FROM ticket_board.ticket_context_continuations
        WHERE ticket_id = p_ticket_id AND role = p_role AND decision = 'continue' AND applied_at IS NOT NULL
    );
END;
$$;

-- SYRD-540's "has the pane moved on from this ticket's conversation?", with a
-- continued ticket entering it at its continuation. Only a clear for other
-- work moves the pane on: continuing into the next ticket of a package keeps
-- the conversation that holds this one.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_superseded(p_ticket text, p_role text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT coalesce((
        SELECT mine.at < latest.at
           AND NOT EXISTS (SELECT FROM ticket_board.ticket_context_restores r
                            WHERE r.ticket_id=p_ticket AND r.role=p_role AND r.outcome='confirmed' AND r.resolved_at > latest.at)
          FROM (SELECT ticket_board.ticket_context_entered_at(p_ticket, p_role) AS at) mine,
               LATERAL (SELECT max(o.cleared_at) AS at FROM ticket_board.ticket_role_session_clears o
                         WHERE o.role=p_role AND o.ticket_id<>p_ticket) latest
         WHERE mine.at IS NOT NULL AND latest.at IS NOT NULL), false);
$$;

-- What a delivery of p_ticket to p_role must do about its conversation (SYRD-540),
-- now with the ticket's continuation decision, and the checkout it was left at
-- read from whatever entered the next conversation: a clear or a continuation.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_status(p_ticket text, p_role text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ticket_context_status');
    IF ticket_board.declared_scheduling() IS NULL THEN RETURN jsonb_build_object('superseded', false); END IF;
    RETURN jsonb_build_object(
        'superseded', ticket_board.ticket_context_superseded(p_ticket, p_role),
        'context', (SELECT to_jsonb(x) FROM ticket_board.ticket_role_contexts x WHERE x.ticket_id=p_ticket AND x.role=p_role),
        'open', (SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r
                  WHERE r.ticket_id=p_ticket AND r.role=p_role AND r.outcome IS NULL),
        'last', (SELECT to_jsonb(r) FROM ticket_board.ticket_context_restores r
                  WHERE r.ticket_id=p_ticket AND r.role=p_role ORDER BY r.id DESC LIMIT 1),
        'parked', ticket_board.ticket_context_parked(p_ticket, p_role),
        -- The checkout as the author LEFT this ticket: recorded when the pane
        -- entered its next ticket's conversation (SYRD-540, SYRD-626).
        'left_checkout', (SELECT e.checkout FROM ticket_board.ticket_context_entries(p_role) e
                           WHERE e.ticket_id<>p_ticket AND e.kind IN ('clear', 'continuation') AND e.bound
                             AND e.at > ticket_board.ticket_context_entered_at(p_ticket, p_role)
                           ORDER BY e.at LIMIT 1),
        -- The ticket's own topic: what it last asked to publish.
        'topic', (SELECT jsonb_build_object('ref', pr.ref, 'commit', pr.commit_hash, 'state', pr.state)
                    FROM ticket_board.publication_requests pr
                   WHERE pr.ticket_id=p_ticket AND pr.state IN ('requested', 'published') ORDER BY pr.id DESC LIMIT 1),
        'continuation', (SELECT to_jsonb(k) FROM ticket_board.ticket_context_continuations k
                          WHERE k.ticket_id=p_ticket AND k.role=p_role));
END;
$$;

-- The one decision for p_ticket's first hand-off to p_role: found if it was
-- already made, otherwise made and recorded. p_observed is what the listener
-- read from the pane: {runtime, session_id, registered_process, checkout}.
-- NULL without a pull policy, which keeps every other board's clear as it was.
CREATE OR REPLACE FUNCTION ticket_board.decide_ticket_context_continuation(p_ticket text, p_role text, p_observed jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE
    made ticket_board.ticket_context_continuations;
    t ticket_board.tickets;
    previous record;
    ties int;
    mine jsonb; theirs jsonb; shared text[]; nearest text; topmost text;
    context ticket_board.ticket_role_contexts;
    waiting text;
    tree text;
    decision text := 'fresh'; reason text; ambiguous boolean := false;
    observed jsonb := coalesce(p_observed, '{}'::jsonb);
    fresh ticket_board.ticket_context_fresh_requests;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('decide_ticket_context_continuation');
    IF ticket_board.declared_scheduling() IS NULL THEN RETURN NULL; END IF;
    -- The ticket's row serializes its one decision with the Director's fresh
    -- request (request_fresh_ticket_context takes the same lock first): each
    -- reads the other's record only once the other has committed or given up.
    SELECT * INTO t FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO made FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket AND role = p_role;
    IF FOUND THEN RETURN to_jsonb(made) || jsonb_build_object('recorded', false); END IF;

    SELECT e.ticket_id, e.at, e.kind INTO previous FROM ticket_board.ticket_context_entries(p_role) e
     WHERE e.ticket_id <> p_ticket ORDER BY e.at DESC LIMIT 1;
    SELECT * INTO fresh FROM ticket_board.ticket_context_fresh_requests WHERE ticket_id = p_ticket;
    IF fresh.ticket_id IS NOT NULL THEN
        reason := format('the Director asked for a fresh context (%s): %s', fresh.requested_by, fresh.reason);
    ELSIF previous.ticket_id IS NULL THEN
        reason := format('%s''s pane has held no earlier ticket''s conversation', p_role);
    ELSE
        mine := ticket_board.ticket_lineage(p_ticket);
        theirs := ticket_board.ticket_lineage(previous.ticket_id);
        SELECT array_agg(m.id ORDER BY m.n) INTO shared
          FROM jsonb_array_elements_text(mine->'chain') WITH ORDINALITY m(id, n)
         WHERE (theirs->'chain') ? m.id;
        IF shared IS NULL THEN
            IF mine->>'problem' IS NOT NULL OR theirs->>'problem' IS NOT NULL THEN
                ambiguous := true;
                reason := format('whether %s and %s share a parent hierarchy cannot be proven: %s', p_ticket,
                                 previous.ticket_id, coalesce(mine->>'problem', theirs->>'problem'));
            ELSE
                reason := format('%s and %s share no parent hierarchy', p_ticket, previous.ticket_id);
            END IF;
        ELSE
            nearest := shared[1];
            topmost := shared[cardinality(shared)];
            SELECT count(*) INTO ties FROM ticket_board.ticket_context_entries(p_role) e
             WHERE e.ticket_id <> p_ticket AND e.at = previous.at;
            SELECT * INTO context FROM ticket_board.ticket_role_contexts WHERE ticket_id = previous.ticket_id AND role = p_role;
            SELECT x.ticket_id INTO waiting FROM ticket_board.ticket_role_contexts x JOIN ticket_board.tickets w ON w.id = x.ticket_id
             WHERE x.role = p_role AND x.ticket_id NOT IN (p_ticket, previous.ticket_id) AND w.assignee = p_role
               AND ticket_board.declared_stage_kind(w.state) = 'implementation'
            UNION
            SELECT r.ticket_id FROM ticket_board.ticket_context_restores r
             WHERE r.role = p_role AND r.outcome IS NULL AND r.ticket_id <> p_ticket
            LIMIT 1;
            tree := (context.evidence->'session_start'->'checkout')->>'cwd';
            IF ties > 1 THEN
                ambiguous := true;
                reason := format('%s''s latest conversation cannot be told apart: %s tickets entered it at the same moment',
                                 p_role, ties);
            ELSIF NOT EXISTS (SELECT FROM ticket_board.pull_releases r WHERE r.ticket_id = previous.ticket_id) THEN
                reason := format('%s has not passed its release gate (%s)', previous.ticket_id,
                                 ticket_board.declared_scheduling()->>'release_after');
            ELSIF t.assignee IS DISTINCT FROM p_role OR ticket_board.declared_stage_kind(t.state) IS DISTINCT FROM 'implementation' THEN
                reason := format('%s is not assigned to %s in implementation', p_ticket, p_role);
            ELSIF waiting IS NOT NULL THEN
                reason := format('%s is returned work of %s that its own saved conversation must be restored for first',
                                 waiting, p_role);
            ELSIF context.ticket_id IS NULL OR context.state <> 'bound' THEN
                ambiguous := true;
                reason := format('%s''s conversation was never confirmed, so it cannot be identified', previous.ticket_id);
            ELSIF nullif(observed->>'runtime', '') IS NULL OR nullif(observed->>'session_id', '') IS NULL
                  OR jsonb_typeof(observed->'registered_process') IS DISTINCT FROM 'boolean'
                  OR nullif(observed->'checkout'->>'cwd', '') IS NULL OR nullif(tree, '') IS NULL THEN
                ambiguous := true;
                reason := format('the pane did not report what proves it still holds %s''s conversation: %s', previous.ticket_id,
                                 concat_ws(', ',
                                     CASE WHEN nullif(observed->>'runtime', '') IS NULL THEN 'its runtime' END,
                                     CASE WHEN nullif(observed->>'session_id', '') IS NULL THEN 'its session' END,
                                     CASE WHEN jsonb_typeof(observed->'registered_process') IS DISTINCT FROM 'boolean'
                                          THEN 'which process announced it' END,
                                     CASE WHEN nullif(observed->'checkout'->>'cwd', '') IS NULL THEN 'its worktree' END,
                                     CASE WHEN nullif(tree, '') IS NULL THEN format('%s''s worktree', previous.ticket_id) END));
            ELSIF observed->>'runtime' IS DISTINCT FROM context.runtime THEN
                reason := format('%s now runs %s, and %s''s conversation was on %s', p_role, observed->>'runtime',
                                 previous.ticket_id, context.runtime);
            ELSIF observed->>'session_id' IS DISTINCT FROM context.session_id THEN
                reason := format('the pane is in session %s, not %s''s conversation %s', observed->>'session_id',
                                 previous.ticket_id, context.session_id);
            ELSIF NOT (observed->>'registered_process')::boolean THEN
                reason := 'the pane''s session was announced by another process than the registered provider';
            ELSIF observed->'checkout'->>'cwd' IS DISTINCT FROM tree THEN
                reason := format('the pane is in worktree %s, not %s''s worktree %s', observed->'checkout'->>'cwd',
                                 previous.ticket_id, tree);
            ELSE
                decision := 'continue';
                reason := format('%s continues %s''s conversation: both are in %s''s hierarchy', p_ticket,
                                 previous.ticket_id, topmost);
            END IF;
        END IF;
    END IF;

    INSERT INTO ticket_board.ticket_context_continuations(
        ticket_id, role, decision, from_ticket, shared_root, shared_ancestor, reason, ambiguous, evidence, round_at)
    VALUES (p_ticket, p_role, decision, previous.ticket_id,
            CASE WHEN decision = 'continue' THEN topmost END, CASE WHEN decision = 'continue' THEN nearest END,
            reason, ambiguous,
            jsonb_build_object('observed', observed, 'previous', CASE WHEN previous.ticket_id IS NULL THEN NULL ELSE
                                   jsonb_build_object('ticket', previous.ticket_id, 'entered', previous.kind, 'at', previous.at) END,
                               'lineage', jsonb_build_object(p_ticket, mine, coalesce(previous.ticket_id, ''), theirs),
                               'shared', to_jsonb(shared), 'context', to_jsonb(context),
                               'release', (SELECT to_jsonb(r) FROM ticket_board.pull_releases r WHERE r.ticket_id = previous.ticket_id),
                               'forced', fresh.ticket_id IS NOT NULL),
            (SELECT (to_jsonb(ns)->>'current_assignment_at')::timestamptz
               FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = p_ticket))
    ON CONFLICT (ticket_id, role) DO NOTHING
    RETURNING * INTO made;
    IF NOT FOUND THEN
        -- Decided concurrently: that decision is the one.
        SELECT * INTO made FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket AND role = p_role;
        RETURN to_jsonb(made) || jsonb_build_object('recorded', false);
    END IF;
    IF ambiguous THEN
        PERFORM ticket_board.notify_ticket_context_exception(p_ticket, p_role, 'continuation_unproven',
            format('%s starts %s in a fresh conversation: whether it may continue %s could not be proven (%s). Nothing was '
                   'carried over. If the two belong together, fix what is named and hand the next ticket of the package over '
                   'as usual.', p_role, p_ticket, coalesce(previous.ticket_id, 'its last ticket'), reason),
            'ticket-context-continuation:' || p_ticket || ':' || p_role);
    END IF;
    RETURN to_jsonb(made) || jsonb_build_object('recorded', true);
END;
$$;

-- Bind a continued ticket to the conversation it continues, once. The binding
-- copies the proven session and its SessionStart evidence, so SYRD-540 finds
-- the same session and worktree for this ticket as for the one before it.
-- "applied" (now, or earlier), or "fresh": the decision is not a continuation,
-- or its conversation lost its binding since -- then it becomes a fresh one, the
-- Director is told once, and the ordinary clear follows.
CREATE OR REPLACE FUNCTION ticket_board.apply_ticket_context_continuation(p_ticket text, p_role text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE k ticket_board.ticket_context_continuations; source ticket_board.ticket_role_contexts;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('apply_ticket_context_continuation');
    SELECT * INTO k FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket AND role = p_role FOR UPDATE;
    IF NOT FOUND OR k.decision <> 'continue' THEN RETURN 'fresh'; END IF;
    IF k.applied_at IS NOT NULL THEN RETURN 'applied'; END IF;
    SELECT * INTO source FROM ticket_board.ticket_role_contexts WHERE ticket_id = k.from_ticket AND role = p_role;
    IF source.ticket_id IS NULL OR source.state <> 'bound' THEN
        UPDATE ticket_board.ticket_context_continuations
           SET decision = 'fresh', ambiguous = true, shared_root = NULL, shared_ancestor = NULL,
               reason = format('%s''s conversation has no confirmed binding any more, so it cannot be continued', k.from_ticket)
         WHERE ticket_id = p_ticket AND role = p_role;
        PERFORM ticket_board.notify_ticket_context_exception(p_ticket, p_role, 'continuation_unproven',
            format('%s starts %s in a fresh conversation: %s''s conversation lost its confirmed binding before it could be '
                   'continued. Nothing was carried over.', p_role, p_ticket, k.from_ticket),
            'ticket-context-continuation:' || p_ticket || ':' || p_role);
        RETURN 'fresh';
    END IF;
    UPDATE ticket_board.ticket_context_continuations SET applied_at = clock_timestamp()
     WHERE ticket_id = p_ticket AND role = p_role;
    INSERT INTO ticket_board.ticket_role_contexts(ticket_id, role, runtime, session_id, state, evidence)
    VALUES (p_ticket, p_role, source.runtime, source.session_id, 'bound',
            source.evidence || jsonb_build_object('continued_from', k.from_ticket, 'shared_root', k.shared_root))
    ON CONFLICT (ticket_id, role) DO NOTHING;
    RETURN 'applied';
END;
$$;

-- The continuation the delivery of p_ticket to p_role in its current round
-- announces: applied, and decided for this very assignment round.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_continuation_note(p_ticket text, p_role text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ticket_context_continuation_note');
    RETURN (SELECT to_jsonb(k) FROM ticket_board.ticket_context_continuations k
              LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = k.ticket_id
             WHERE k.ticket_id = p_ticket AND k.role = p_role AND k.decision = 'continue' AND k.applied_at IS NOT NULL
               AND k.round_at IS NOT DISTINCT FROM (to_jsonb(ns)->>'current_assignment_at')::timestamptz);
END;
$$;

-- The Director's fresh context for a ticket, asked for before its first hand-off is decided.
CREATE OR REPLACE FUNCTION ticket_board.request_fresh_ticket_context(p_ticket text, p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE who text; decided ticket_board.ticket_context_continuations; saved ticket_board.ticket_context_fresh_requests;
BEGIN
    PERFORM ticket_board.require_actor(ARRAY['director'], 'director_edit');
    who := ticket_board.current_app_actor();
    -- The same lock decide_ticket_context_continuation takes first: a request and
    -- a decision never both see the other absent.
    PERFORM FROM ticket_board.tickets WHERE id = p_ticket FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ticket % does not exist', p_ticket;
    END IF;
    IF btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'force_fresh_context needs a reason';
    END IF;
    SELECT * INTO decided FROM ticket_board.ticket_context_continuations WHERE ticket_id = p_ticket ORDER BY decided_at LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION '%''s context was already decided for % at %: % (%)', p_ticket, decided.role, decided.decided_at,
            decided.decision, decided.reason;
    END IF;
    INSERT INTO ticket_board.ticket_context_fresh_requests(ticket_id, requested_by, reason)
    VALUES (p_ticket, who, btrim(p_reason))
    ON CONFLICT (ticket_id) DO UPDATE SET requested_by = EXCLUDED.requested_by, reason = EXCLUDED.reason,
                                          requested_at = clock_timestamp()
    RETURNING * INTO saved;
    RETURN to_jsonb(saved);
END;
$$;

-- A ticket's continuation evidence, for its JSON: NULL when there is none.
CREATE OR REPLACE FUNCTION ticket_board.ticket_context_continuation(p_ticket text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
    SELECT CASE WHEN d.decisions IS NULL AND f.requested IS NULL THEN NULL
                ELSE jsonb_build_object('decisions', coalesce(d.decisions, '[]'::jsonb), 'fresh_requested', f.requested) END
      FROM (SELECT jsonb_agg(jsonb_build_object('role', k.role, 'decision', k.decision, 'from_ticket', k.from_ticket,
                                                'shared_root', k.shared_root, 'shared_ancestor', k.shared_ancestor,
                                                'reason', k.reason, 'ambiguous', k.ambiguous, 'decided_at', k.decided_at,
                                                'applied_at', k.applied_at) ORDER BY k.decided_at) AS decisions
              FROM ticket_board.ticket_context_continuations k WHERE k.ticket_id = p_ticket) d,
           (SELECT CASE WHEN r.ticket_id IS NULL THEN NULL
                        ELSE jsonb_build_object('by', r.requested_by, 'reason', r.reason, 'at', r.requested_at) END AS requested
              FROM (SELECT 1) one LEFT JOIN ticket_board.ticket_context_fresh_requests r ON r.ticket_id = p_ticket) f;
$$;

-- What a launch of p_role must do about its conversation (SYRD-626). Launch is
-- not a ticket boundary: a role that holds an active ticket whose first hand-off
-- already happened resumes that ticket's conversation; a continuation decided
-- but not yet delivered resumes the conversation it continues; only a role
-- holding no handed-over work starts fresh. "refuse" is a ticket whose
-- conversation must be resumed but cannot be identified: the launcher stops and
-- says so rather than erase it. "resume_recorded" is a board without a pull
-- policy, which binds no sessions: the launcher's own record is the evidence.
CREATE OR REPLACE FUNCTION ticket_board.role_context_recovery(p_role text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE active text; handed boolean; binding ticket_board.ticket_role_contexts;
    k ticket_board.ticket_context_continuations; source ticket_board.ticket_role_contexts;
BEGIN
    SELECT q.ticket_id INTO active FROM ticket_board.serial_queue() q WHERE q.implementer = p_role AND q.active
     ORDER BY q.queue_position LIMIT 1;
    IF active IS NULL THEN
        RETURN jsonb_build_object('role', p_role, 'action', 'fresh', 'ticket', NULL,
                                  'reason', format('%s holds no active ticket', p_role));
    END IF;
    handed := EXISTS (SELECT FROM ticket_board.ticket_role_session_clears c WHERE c.ticket_id = active AND c.role = p_role)
           OR EXISTS (SELECT FROM ticket_board.ticket_context_continuations c
                       WHERE c.ticket_id = active AND c.role = p_role AND c.decision = 'continue' AND c.applied_at IS NOT NULL);
    SELECT * INTO k FROM ticket_board.ticket_context_continuations WHERE ticket_id = active AND role = p_role;
    IF NOT handed THEN
        IF k.decision = 'continue' THEN
            SELECT * INTO source FROM ticket_board.ticket_role_contexts WHERE ticket_id = k.from_ticket AND role = p_role;
            IF source.state = 'bound' THEN
                RETURN jsonb_build_object('role', p_role, 'action', 'resume', 'ticket', active, 'session_id', source.session_id,
                                          'runtime', source.runtime,
                                          'reason', format('%s continues %s''s conversation %s, decided before the restart',
                                                           active, k.from_ticket, source.session_id));
            END IF;
            RETURN jsonb_build_object('role', p_role, 'action', 'refuse', 'ticket', active,
                                      'reason', format('%s was decided to continue %s''s conversation, which has no confirmed session',
                                                       active, k.from_ticket));
        END IF;
        RETURN jsonb_build_object('role', p_role, 'action', 'fresh', 'ticket', active,
                                  'reason', format('%s has not been handed to %s yet, so no conversation of it exists', active, p_role));
    END IF;
    IF ticket_board.declared_scheduling() IS NULL THEN
        RETURN jsonb_build_object('role', p_role, 'action', 'resume_recorded', 'ticket', active,
                                  'reason', format('%s is %s''s active ticket', active, p_role));
    END IF;
    SELECT * INTO binding FROM ticket_board.ticket_role_contexts WHERE ticket_id = active AND role = p_role;
    IF binding.state = 'bound' THEN
        RETURN jsonb_build_object('role', p_role, 'action', 'resume', 'ticket', active, 'session_id', binding.session_id,
                                  'runtime', binding.runtime,
                                  'reason', format('%s is %s''s active ticket, in conversation %s', active, p_role, binding.session_id));
    END IF;
    RETURN jsonb_build_object('role', p_role, 'action', 'refuse', 'ticket', active,
                              'reason', format('%s is %s''s active ticket, and %s', active, p_role,
                                               CASE WHEN binding.ticket_id IS NULL THEN 'no conversation was recorded for it'
                                                    ELSE 'its conversation was never confirmed' END));
END;
$$;

-- Privileges: the same lines rbac.sql carries (SYRD-626).
REVOKE ALL ON ticket_board.ticket_context_continuations, ticket_board.ticket_context_fresh_requests FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_lineage(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_context_entered_at(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_context_entries(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.decide_ticket_context_continuation(text, text, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_context_continuation_note(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.request_fresh_ticket_context(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.ticket_context_continuation(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION ticket_board.role_context_recovery(text) FROM PUBLIC;
GRANT SELECT ON ticket_board.ticket_context_continuations, ticket_board.ticket_context_fresh_requests
    TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.decide_ticket_context_continuation(text, text, jsonb) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_context_continuation_note(text, text) TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.request_fresh_ticket_context(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.ticket_context_continuation(text) TO ticket_board_service, ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.role_context_recovery(text) TO ticket_board_service;

COMMIT;
