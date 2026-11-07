BEGIN;

-- SYRD-736 (pgu1029): pull-ready rework pinned to a worker that cannot take it is reported to the Director, once.
--
-- Under a pull policy, rework that returns to an author who is busy waits in the ready stage pinned to that author
-- (queued_for_assignee), behind the ticket the author holds (queued_behind_ticket). Only the author may claim it, and
-- the Director's "it can be routed now" wake is off under pull (SYRD-539): the author's own claim was the plan. On
-- MEFP on 2026-11-06, MEFP-768 and MEFP-769 waited pinned to luna-9 and luna-10 for nine hours after both authors'
-- predecessors had left them. Both authors were stopped. Eight other claimants ran idle, and could not claim pinned
-- work. The listener names only running workers in its idle notice, and the only one that could take the ticket was
-- not running, so nothing was said.
--
-- The listener reports, each pass, which claimants are idle capacity (p_idle, as notify_pull_idle_capacity takes it)
-- and why each other claimant is not (p_unavailable: not running, or its provider held for quota). Rework is
-- stranded when:
--   * it is claimable by its author now (ticket_ready_for_pull), so blocked, held, parked or awaited work is not;
--   * its author holds no ticket: the wait it was pinned for is over;
--   * and its author is not idle capacity: the listener could not give it the ticket.
-- A stranding is recorded the first pass it is seen, with the author's reason and the idle claimants as seen then
-- and on every later pass. It ends when any of those stops holding (a claim, a withdrawal, a re-admission, the author
-- running or taking other work), and its record goes with it.
--
-- When a stranding has lasted the policy's idle_alert_seconds while another claimant sits idle, the Director gets
-- one notice for it, naming the author, why it cannot take the ticket, the ticket it was queued behind, and the idle
-- claimants. The wait first gives the project's admission time to start the author (SYRD-688). Nothing is moved:
-- whether the author starts again, keeping its saved conversation, or another worker takes the ticket, is the
-- Director's call.
CREATE TABLE IF NOT EXISTS ticket_board.pull_pinned_rework_strands (
    ticket_id text PRIMARY KEY REFERENCES ticket_board.tickets(id) ON DELETE CASCADE,
    author text NOT NULL,
    -- When the stranding was first seen.
    since timestamptz NOT NULL,
    -- As the listener last saw them: why the author cannot take it, and the claimants idle while it waits.
    reason text NOT NULL,
    idle jsonb NOT NULL DEFAULT '{}'::jsonb,
    notified_at timestamptz
);

CREATE OR REPLACE FUNCTION ticket_board.notify_pull_pinned_rework_strands(p_idle jsonb, p_unavailable jsonb,
                                                                          p_now timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
DECLARE policy jsonb := ticket_board.declared_scheduling(); strands jsonb; free jsonb; strand record; sent integer := 0;
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('notify_pull_pinned_rework_strands');
    SELECT coalesce(jsonb_agg(jsonb_build_object('id', t.id, 'author', btrim(t.queued_for_assignee))), '[]'::jsonb)
      INTO strands
      FROM ticket_board.tickets t
     WHERE t.state = policy->>'ready_stage'
       AND btrim(t.queued_for_assignee) <> ''
       AND ticket_board.ticket_ready_for_pull(t.id, btrim(t.queued_for_assignee))
       AND ticket_board.ticket_current_reserved_ticket(btrim(t.queued_for_assignee)) IS NULL
       AND NOT coalesce(p_idle, '{}'::jsonb) ? btrim(t.queued_for_assignee);
    DELETE FROM ticket_board.pull_pinned_rework_strands s
     WHERE NOT EXISTS (SELECT FROM jsonb_to_recordset(strands) AS c(id text, author text)
                        WHERE c.id = s.ticket_id AND c.author = s.author);
    SELECT coalesce(jsonb_object_agg(k, v), '{}'::jsonb) INTO free
      FROM jsonb_each_text(coalesce(p_idle, '{}'::jsonb)) AS e(k, v)
     -- Idle and able to take work: what pull_pickup says of a running claimant that found nothing to claim.
     WHERE v = 'idle, nothing claimable';
    INSERT INTO ticket_board.pull_pinned_rework_strands AS s (ticket_id, author, since, reason, idle)
    SELECT c.id, c.author, p_now,
           coalesce(nullif(p_unavailable->>c.author, ''), 'it did not claim it on the listener''s last pass'), free
      FROM jsonb_to_recordset(strands) AS c(id text, author text)
    ON CONFLICT (ticket_id) DO UPDATE SET reason = EXCLUDED.reason, idle = EXCLUDED.idle;
    IF free = '{}'::jsonb THEN
        RETURN 0;
    END IF;
    FOR strand IN
        SELECT s.*, t.state, t.assignee, btrim(t.queued_behind_ticket) AS behind
          FROM ticket_board.pull_pinned_rework_strands s JOIN ticket_board.tickets t ON t.id = s.ticket_id
         WHERE s.notified_at IS NULL
           AND s.since <= p_now - make_interval(secs => coalesce((policy->>'idle_alert_seconds')::int, 900))
         ORDER BY s.since, t.ticket_number
    LOOP
        UPDATE ticket_board.pull_pinned_rework_strands SET notified_at = p_now WHERE ticket_id = strand.ticket_id;
        PERFORM ticket_board.enqueue_notification(
            strand.ticket_id, 'ticket_update', 'director',
            format('%s is rework pinned to %s, and %s has not been able to take it since %s: %s holds no ticket now%s, '
                   'but %s. These implementers are idle and cannot claim it while it is pinned: %s. Start %s, or move '
                   '%s out of %s and route it to one of them.',
                   strand.ticket_id, strand.author, strand.author,
                   to_char(strand.since AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI "UTC"'), strand.author,
                   CASE WHEN strand.behind <> '' THEN ' (it was queued behind ' || strand.behind || ')' ELSE '' END,
                   strand.reason,
                   (SELECT string_agg(k || ' (' || v || ')', ', ' ORDER BY k) FROM jsonb_each_text(free) AS e(k, v)),
                   strand.author, strand.ticket_id, policy->>'ready_stage'),
            -- queued_for and reserved_by are the queue's own identity: the listener drops this notice when the
            -- ticket's pin has moved on before it is delivered (SYRD-108), as it does a queue announcement.
            jsonb_build_object('kind', 'pull_pinned_rework', 'id', strand.ticket_id, 'state', strand.state,
                               'assignee', strand.assignee, 'target_role', 'director',
                               'queued_for', strand.author, 'reserved_by', strand.behind,
                               'reason', strand.reason, 'idle', free, 'stranded_since', strand.since),
            'pull-pinned-rework:' || strand.ticket_id || ':' || strand.author || ':'
                || extract(epoch FROM strand.since)::bigint);
        sent := sent + 1;
    END LOOP;
    RETURN sent;
END;
$$;
REVOKE EXECUTE ON FUNCTION ticket_board.notify_pull_pinned_rework_strands(jsonb, jsonb, timestamptz) FROM PUBLIC;

COMMIT;
