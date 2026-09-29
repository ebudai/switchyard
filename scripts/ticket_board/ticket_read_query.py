"""Read-only ticket row selection for the board's list and detail views.

The caller supplies its existing PostgreSQL connection. This query reads the
workflow and ticket rows in that transaction; it never creates one or writes.
"""

from __future__ import annotations

from typing import Any


def select_ticket_rows(conn: Any, ticket_id: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE t.id = %s" if ticket_id is not None else ""
    params = (ticket_id,) if ticket_id is not None else ()
    from .workflow_config import read_configuration
    workflow = read_configuration(conn)
    configured = workflow is not None
    owner_sql = "ticket_board.transition_target_role(scoped.state, scoped.assignee)" if configured else """CASE
            WHEN scoped.state = 'in_progress' THEN NULLIF(scoped.assignee, 'unassigned')
            WHEN scoped.state IN ('analysis', 'dat', 'director_review') THEN 'director'
            WHEN scoped.state = 'inspection' THEN 'inspector'
            WHEN scoped.state = 'audit' THEN 'audit'
            ELSE NULL END"""
    if configured:
        # The persisted document has already passed workflow_config.validate,
        # but still quote stage names as data before embedding the compact
        # read-only scope. This avoids granting the board service execution
        # rights on Director-only workflow mutation helpers.
        active_stages = [
            str(stage["name"])
            for stage in workflow["stages"]
            if not stage["terminal"] and stage["kind"] != "draft"
        ]
        active_stage_sql = ", ".join(
            "'" + stage.replace("'", "''") + "'" for stage in active_stages
        ) or "NULL"
        scope_sql = f"scoped.state IN ({active_stage_sql})"
    else:
        scope_sql = "scoped.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review')"
    return conn.execute(
        f"""
WITH notification_scope AS (
    SELECT
        scoped.id,
        scoped.state,
        scoped.ticket_number,
        scoped.manually_controlled,
        scoped.queued_for_assignee,
        scoped.queued_behind_ticket,
        notification_state.entered_current_state_at,
        COALESCE(notification_state.awaiting_role, '') AS awaiting_role,
        {owner_sql} AS owner_role
    FROM ticket_board.tickets scoped
    LEFT JOIN ticket_board.ticket_notification_state notification_state
        ON notification_state.ticket_id = scoped.id
    WHERE {scope_sql}
),
notification_candidates AS (
    SELECT
        notification_scope.id,
        notification_scope.state,
        notification_scope.ticket_number,
        notification_scope.owner_role,
        notification_scope.entered_current_state_at,
        -- Delivery can legitimately be deferred while the assigned pane is
        -- busy. Current-work visibility therefore comes from durable workflow
        -- ownership and serial-focus state, never from a successful send.
        -- A logical role, not merely a non-NULL value. An ownerless stage
        -- resolves to NULL, but a stage that names an owner as the empty
        -- string would partition every such ticket together and highlight one
        -- of them for a role nobody is: the invariant is one ticket per role,
        -- so the row has to name a role (SYRD-72).
        NULLIF(notification_scope.owner_role, '') IS NOT NULL
            AND NOT notification_scope.manually_controlled
            AND notification_scope.awaiting_role = ''
            AND notification_scope.queued_for_assignee = ''
            AND notification_scope.queued_behind_ticket = ''
            AND NOT EXISTS (
                SELECT FROM ticket_board.ticket_blockers blocker
                WHERE blocker.ticket_id = notification_scope.id
                  AND NOT blocker.resolved
            )
            AS is_actionable_current,
        sent.last_sent_at AS active_work_notified_at,
        queued.attempts AS active_work_delivery_attempts,
        queued.last_error AS active_work_delivery_last_error,
        queued.dead_lettered_at AS active_work_delivery_dead_lettered_at,
        queued.terminal_reason AS active_work_delivery_terminal_reason,
        queued.next_attempt_at AS active_work_delivery_next_attempt_at,
        queued.id IS NOT NULL AS active_work_delivery_queued,
        unconfirmed.last_unconfirmed_at AS active_work_delivery_unconfirmed_at,
        unconfirmed.unconfirmed_reason AS active_work_delivery_unconfirmed_reason
    FROM notification_scope
    LEFT JOIN LATERAL (
        SELECT max(trace.ts) AS last_sent_at
        FROM ticket_board.notification_trace trace
        WHERE trace.ticket_id = notification_scope.id
          AND trace.target_role = notification_scope.owner_role
          AND trace.kind = 'transition'
          AND trace.event = 'send'
          AND trace.ticket_state_at_event = notification_scope.state
          -- This VISIT to the stage, not any earlier one: a ticket sent to Ops,
          -- routed back to analysis and returned to Ops must not report the
          -- first visit's send while the second notice is pending or dead
          -- (SYRD-264 Final Sign-Off). A legacy row with no recorded entry is
          -- not bounded rather than hidden.
          AND (notification_scope.entered_current_state_at IS NULL
               OR trace.ts >= notification_scope.entered_current_state_at)
    ) sent ON true
    -- The notice that has NOT been delivered, if there is one: acknowledged
    -- notices are deleted, so a remaining row for this owner and this stage is
    -- either still pending or dead-lettered. Without this the board showed a
    -- ticket as the owner's current work -- which MEFP-1 was -- and nothing at
    -- all about its notice having been dead-lettered (SYRD-264).
    LEFT JOIN LATERAL (
        SELECT q.id, q.attempts, q.last_error, q.dead_lettered_at, q.terminal_reason,
               q.next_attempt_at
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = notification_scope.id
          AND q.target_role = notification_scope.owner_role
          AND q.kind = 'transition'
          AND COALESCE(q.payload->>'new_state', q.payload->>'state') = notification_scope.state
          AND (notification_scope.entered_current_state_at IS NULL
               OR q.created_at >= notification_scope.entered_current_state_at)
        ORDER BY q.id DESC
        LIMIT 1
    ) queued ON true
    -- Sent, and never seen to arrive: the recipient's own hooks recorded no
    -- turn afterwards, or there was no hook record to read. Not a send, so it
    -- cannot read as delivered -- which is how MEFP-1's Final Sign-Off notice
    -- was reported (SYRD-268). The latest one, with the listener's reason.
    LEFT JOIN LATERAL (
        SELECT trace.ts AS last_unconfirmed_at, trace.busy_reason AS unconfirmed_reason
        FROM ticket_board.notification_trace trace
        WHERE trace.ticket_id = notification_scope.id
          AND trace.target_role = notification_scope.owner_role
          AND trace.kind = 'transition'
          AND trace.event = 'send_unconfirmed'
          AND trace.ticket_state_at_event = notification_scope.state
          AND (notification_scope.entered_current_state_at IS NULL
               OR trace.ts >= notification_scope.entered_current_state_at)
        ORDER BY trace.ts DESC
        LIMIT 1
    ) unconfirmed ON true
),
active_work AS (
    SELECT
        notification_candidates.id,
        notification_candidates.owner_role,
        notification_candidates.active_work_notified_at,
        notification_candidates.active_work_delivery_attempts,
        notification_candidates.active_work_delivery_last_error,
        notification_candidates.active_work_delivery_dead_lettered_at,
        notification_candidates.active_work_delivery_terminal_reason,
        notification_candidates.active_work_delivery_next_attempt_at,
        notification_candidates.active_work_delivery_queued,
        notification_candidates.active_work_delivery_unconfirmed_at,
        notification_candidates.active_work_delivery_unconfirmed_reason,
        -- There is exactly one visible current ticket per logical owner. The
        -- send timestamp remains separate evidence and does not rank work.
        notification_candidates.is_actionable_current
            AND row_number() OVER (
                PARTITION BY notification_candidates.owner_role
                ORDER BY notification_candidates.is_actionable_current DESC,
                    notification_candidates.entered_current_state_at NULLS LAST,
                    notification_candidates.ticket_number
            ) = 1 AS active_work_highlight
    FROM notification_candidates
)
SELECT
    t.id,
    t.title,
    t.body,
    t.state,
    t.assignee,
    t.parent_id,
    t.origin_project,
    t.external_source_ref,
    t.blocked_reason,
    t.queued_for_assignee,
    t.queued_behind_ticket,
    t.implementation,
    t.audit_prompt,
    t.audit_signoff,
    t.needs_audit,
    t.needs_inspection,
    t.inspector_signoff,
    t.needs_user_signoff,
    t.user_signoff,
    t.regression,
    t.manually_controlled,
    t.commit_hash,
    t.commit_exempt,
    COALESCE(to_jsonb(t)->'workflow_flags', '{{}}'::jsonb) AS workflow_flags,
    t.created_text,
    t.updated_text,
    active_work.owner_role AS active_work_owner_role,
    active_work.active_work_notified_at,
    active_work.active_work_delivery_attempts,
    active_work.active_work_delivery_last_error,
    active_work.active_work_delivery_dead_lettered_at,
    active_work.active_work_delivery_terminal_reason,
    active_work.active_work_delivery_next_attempt_at,
    COALESCE(active_work.active_work_delivery_queued, false) AS active_work_delivery_queued,
    active_work.active_work_delivery_unconfirmed_at,
    active_work.active_work_delivery_unconfirmed_reason,
    COALESCE(active_work.active_work_highlight, false) AS active_work_highlight,
    COALESCE(notification_state.awaiting_role, '') AS awaiting_role,
    COALESCE(
        (SELECT array_agg(b.blocker_ticket_id ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id
           AND NOT b.resolved),
        ARRAY[]::text[]
    ) AS blocked_by,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('id', b.blocker_ticket_id, 'resolved', b.resolved) ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id),
        '[]'::jsonb
    ) AS blockers,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('who', c.who, 'text', c.text, 'ts', c.ts_text, 'urgent', c.urgent) ORDER BY c.position)
         FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = t.id),
        '[]'::jsonb
    ) AS comments,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('path', a.path, 'metadata', a.metadata) ORDER BY a.position)
         FROM ticket_board.ticket_attachments a
         WHERE a.ticket_id = t.id),
        '[]'::jsonb
    ) AS screenshots
FROM ticket_board.tickets t
LEFT JOIN active_work ON active_work.id = t.id
LEFT JOIN ticket_board.ticket_notification_state notification_state
    ON notification_state.ticket_id = t.id
{where}
ORDER BY t.ticket_number
;
""",
        params,
    ).fetchall()
