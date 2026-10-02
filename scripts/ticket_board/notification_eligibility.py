"""Currentness, supersession and serial queue decisions for queued notices (SYRD-487 scratch)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from . import reminder_snooze

TERMINAL_STATES = {"done", "cancelled"}
NUDGE_ELIGIBLE_STATES = {"in_progress", "inspection", "audit", "dat", "director_review", "analysis", "backlog"}
#: Kinds that say "this role has not moved". A handoff established after one of
#: them was generated says the opposite, so delivering it afterwards reports a
#: stall the board itself no longer believes in. `awaiting_role` is deliberately
#: absent: it IS the handoff's own bounded schedule, and dropping it here would
#: silence the very notifications the wait exists to send (SYRD-99).
SUPERSEDABLE_REMINDER_KINDS = frozenset({"idle_reminder", "nudge", "escalation"})
#: Kinds addressed to the control role by construction rather than by stage
#: ownership. On a declared board the ordinary resolver answers "whoever owns
#: this stage", which for an unresolved-turn handoff is the very role that
#: stopped -- so without this the Director's copy resolves to the owner, is
#: judged stale, and is dropped before delivery (SYRD-194).
DIRECTOR_BOUND_KINDS = frozenset({"escalation", "unresolved_turn"})
# The owner's "still yours" prompt and the Director's grace escalation after it.
# Both say a turn ended unresolved, so both are only true while it still is.
UNRESOLVED_TURN_KINDS = frozenset({"unresolved_turn_repair", "unresolved_turn"})
#: Distinct from `stale_notification`, which means the ticket moved, and from
#: `pane busy`, which means delivery was only postponed. This one means the
#: reminder was answered before it could be delivered.
SUPERSEDED_BY_AWAITING_ROLE = "superseded_by_awaiting_role"
#: The one stage a board with no declared workflow serialises, and the roles a
#: static workflow never serialises in it. Both were written inline before a
#: document could say otherwise; they are named here so it is visible that they
#: apply ONLY to a tenant with nothing to read, and so a declarative tenant's
#: answer comes from its own document rather than from these names (SYRD-37).
LEGACY_SERIAL_STAGE = "in_progress"
LEGACY_NON_SERIAL_ROLES = frozenset({"director", "audit", "inspector"})

#: A serial-focus queue announcement names one implementer and the reservation
#: holding them. Every reroute while the board stays in the same holding stage
#: writes another announcement with the same ticket, state and assignee, so the
#: ordinary currentness check cannot tell the obsolete ones apart: only the
#: queue identity distinguishes them (SYRD-108).
SUPERSEDED_QUEUE_NOTICE = "superseded_serial_focus_queue"
#: `announce_serial_focus_queue` renders a reservation it cannot name as this
#: text, while `queued_behind_ticket` keeps the empty string. Both spellings are
#: the same reservation, so neither may read as a change of identity.
UNNAMED_RESERVATION = "active work"


class NotificationEligibility:
    def __init__(self, *, logger: Any, ledger: Any, workflow: Callable[[], Any], decode_text: Callable[[Any], str]) -> None:
        self.logger = logger
        self.ledger = ledger
        self._workflow = workflow
        self.decode_text = decode_text

    @property
    def workflow(self) -> Any:
        return self._workflow()

    def _current_ticket_state(self, conn: Any, ticket_id: str) -> tuple[str, str, bool, bool, bool] | None:
        result = conn.execute(
            """
SELECT state,
       assignee,
       manually_controlled,
       coalesce((to_jsonb(t)->>'parked')::boolean, false) AS parked,
       ticket_board.ticket_has_unresolved_blockers(id) AS has_unresolved_blockers
FROM ticket_board.tickets t
WHERE id = %s
""",
            (ticket_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return (
                self.decode_text(row["state"]),
                self.decode_text(row["assignee"]),
                bool(row["manually_controlled"]),
                bool(row["parked"]),
                bool(row["has_unresolved_blockers"]),
            )
        return self.decode_text(row[0]), self.decode_text(row[1]), bool(row[2]), bool(row[3]), bool(row[4])

    def _current_target_role(self, kind: str, state: str, assignee: str) -> str | None:
        cfg = getattr(self, "workflow", None)
        if cfg and kind == "triage":
            # Untriaged work is addressed by stage ownership, which is the one
            # question the ordinary resolver answers with "nobody" (SYRD-120).
            from .workflow_config import unassigned_stage_owner
            return unassigned_stage_owner(cfg, state, assignee)
        if cfg and kind not in DIRECTOR_BOUND_KINDS:
            from .workflow_config import notification_role
            return notification_role(cfg, state, assignee)
        if kind == "transition":
            if state == "analysis":
                return "director"
            if state == "in_progress":
                return assignee if assignee != "unassigned" else None
            if state == "inspection":
                return "inspector"
            if state == "audit":
                return "audit"
            if state == "dat":
                return "director"
            if state == "user_review":
                return None
            if state == "director_review":
                return "director"
            return None
        if kind in DIRECTOR_BOUND_KINDS:
            return "director"
        if state == "in_progress":
            return assignee if assignee != "unassigned" else None
        if state == "inspection":
            return "inspector"
        if state in {"audit", "dat", "director_review", "analysis", "backlog"}:
            return "director" if state in {"analysis", "backlog", "dat", "director_review"} else "audit"
        return None

    def _superseding_awaiting_role(
        self, conn: Any, notification_id: int, ticket_id: str, kind: str
    ) -> dict[str, Any] | None:
        """The handoff that makes an already-queued reminder wrong to deliver.

        A reminder, a nudge and an escalation all assert the same thing: the role
        that owns this ticket has not moved it. An awaiting-role handoff
        established after that wave was generated asserts the opposite -- the
        owner did move, and the next step belongs to somebody else.

        `notify_idle_turn_end_nudges()` and `notify_idle_stall_nudges()` already
        refuse to generate reminders while a wait is active. That is the enqueue
        half, and it is not enough on its own. The wait can
        also begin *after* the wave is generated, while the target's pane is
        busy, and the queued row then survives every deferral and is delivered
        later against a board that no longer agrees with it. That is what
        happened: an Ops escalation queued at 07:40:18 was deferred repeatedly on
        a busy Director pane and delivered at 07:50:56, more than eight minutes
        after Ops set `awaiting_role=director` at 07:42:37. Ops was not stuck, and
        the Director and the User were interrupted for it repeatedly (SYRD-99).

        Decided from the ticket's current notification state -- the awaiting role,
        the identity of the wait and its age -- rather than from the payload's
        copy of the state and assignee, which is precisely what did not change.
        Returns the trace detail when the reminder is superseded, or None.
        """
        if kind not in SUPERSEDABLE_REMINDER_KINDS:
            return None
        # `created_at` is when this wave was generated, and it is the only column
        # that means that: `updated_at` moves on every claim and every requeue,
        # so comparing against it would make each deferral look like a fresh
        # reminder and nothing would ever be superseded. The dedupe upsert leaves
        # `created_at` alone for the same reason.
        result = conn.execute(
            """
SELECT ns.awaiting_role,
       ns.awaiting_since_at,
       q.created_at AS queued_at,
       ticket_board.ticket_awaiting_role_is_active(
           ns.awaiting_role, ns.awaiting_since_at, clock_timestamp()
       ) AS wait_is_active,
       (ns.awaiting_since_at > q.created_at) AS established_after_queueing
FROM ticket_board.ticket_notification_queue q
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = q.ticket_id
WHERE q.id = %s AND q.ticket_id = %s
""",
            (notification_id, ticket_id),
        )
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
        if row is None:
            return None
        if isinstance(row, dict):
            awaiting_role, awaiting_since_at, queued_at, wait_is_active, established_after = (
                row["awaiting_role"], row["awaiting_since_at"], row["queued_at"],
                row["wait_is_active"], row["established_after_queueing"],
            )
        else:
            awaiting_role, awaiting_since_at, queued_at, wait_is_active, established_after = row[:5]
        # A wait that was cleared, or one whose window has expired, supersedes
        # nothing: the owner is answerable again and the reminder is the truth.
        if not wait_is_active or not established_after:
            return None
        return {
            "awaiting_role": self.decode_text(awaiting_role),
            "awaiting_since_at": str(awaiting_since_at),
            "queued_at": str(queued_at),
            "superseded_kind": kind,
        }

    def _drop_superseded_notification(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        detail: dict[str, Any],
        phase: str,
        reason: str = SUPERSEDED_BY_AWAITING_ROLE,
    ) -> None:
        """Remove one superseded notification, saying exactly why it went.

        The detail is what distinguishes the two supersessions this listener
        knows about -- a handoff that answered a reminder, and a reroute that
        replaced a queue announcement -- so it is logged whole rather than
        summarised into a sentence that only fits one of them.
        """
        self.logger.info(
            "Dropping %s notification %s for %s as %s: %s",
            kind, notification_id, ticket_id, reason,
            ", ".join(f"{key}={value}" for key, value in sorted(detail.items())),
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="drop",
            busy_reason=reason,
            detail={**detail, "phase": phase},
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="listener_discard",
            detail={"reason": reason, "phase": phase},
        )
        # Discarded, never acked. This reminder was answered before it could be
        # delivered, and `ack_notification` records delivery accounting:
        # for an idle_reminder it increments idle_reminder_count, which the next
        # idle wave reads as "already reminded" and turns into an escalation to
        # the Director (SYRD-32). Acking here would answer one false escalation
        # by scheduling the next one.
        self.ledger.discard(conn, notification_id, reason)
        self.ledger.forget(notification_id)

    @staticmethod
    def _queue_identity_key(queued_for: str, reserved_by: str) -> tuple[str, str]:
        """One spelling of a queue identity, whichever side it was read from.

        The announcement renders a reservation it cannot name as `active work`
        while the ticket column keeps the empty string, and neither of those is
        a change of identity. Comparison is on this key; what gets traced is
        what was actually written, because `PGU-2` is the ticket id and `pgu-2`
        is not.
        """
        reservation = str(reserved_by or "").strip()
        return (
            str(queued_for or "").strip().lower(),
            "" if reservation.lower() == UNNAMED_RESERVATION else reservation.lower(),
        )

    def _announced_queue_identity(self, payload: str) -> tuple[str, str] | None:
        """The queue identity an announcement was written for, or None.

        Only a serial-focus announcement carries `queued_for`, so every other
        notification returns None here and keeps exactly the checks it had.
        """
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        queued_for = str(parsed.get("queued_for") or "").strip()
        if not queued_for:
            return None
        return queued_for, str(parsed.get("reserved_by") or "").strip()

    def _superseded_queue_notice(self, conn: Any, ticket_id: str, payload: str) -> dict[str, Any] | None:
        """Whether a queue announcement still describes where the ticket waits.

        `queued_for_assignee` and `queued_behind_ticket` are rewritten by every
        route the board accepts and cleared by any route that is not held, so
        they are the live answer to the question the announcement was written
        to answer. A reroute between enqueue and send leaves the announcement
        naming an implementer the Director is no longer waiting on, and the
        instruction in it -- route it again once that reservation clears -- is
        then wrong in a way the reader cannot see (SYRD-108).
        """
        announced = self._announced_queue_identity(payload)
        if announced is None:
            return None
        result = conn.execute(
            """
SELECT queued_for_assignee, queued_behind_ticket
FROM ticket_board.tickets
WHERE id = %s
""",
            (ticket_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            queued_for, queued_behind = row["queued_for_assignee"], row["queued_behind_ticket"]
        else:
            queued_for, queued_behind = row[0], row[1]
        current = (
            self.decode_text(queued_for).strip(),
            self.decode_text(queued_behind).strip(),
        )
        if self._queue_identity_key(*current) == self._queue_identity_key(*announced):
            return None
        return {
            "reason": SUPERSEDED_QUEUE_NOTICE,
            "announced_queued_for": announced[0],
            "announced_reserved_by": announced[1] or UNNAMED_RESERVATION,
            "current_queued_for": current[0],
            "current_reserved_by": current[1] or UNNAMED_RESERVATION,
        }

    def _notification_is_current(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> bool:
        current = self._current_ticket_state(conn, ticket_id)
        if current is None:
            return False
        current_state, current_assignee, manually_controlled, parked, has_unresolved_blockers = current

        terminal_states = {stage["name"] for stage in self.workflow["stages"] if stage["terminal"]} if getattr(self, "workflow", None) else TERMINAL_STATES
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return current_state not in terminal_states
        if not isinstance(parsed, dict):
            return current_state not in terminal_states

        payload_ticket_id = str(parsed.get("id", ticket_id)).strip().upper()
        if payload_ticket_id and payload_ticket_id != ticket_id:
            return False

        expected_state = str(parsed.get("state") or parsed.get("new_state") or "").strip()
        if expected_state and current_state != expected_state:
            return False

        expected_assignee = str(parsed.get("assignee") or "").strip().lower()
        if expected_assignee and current_assignee != expected_assignee:
            return False

        kind = str(parsed.get("kind") or "").strip().lower()
        # A snooze batch's deadline notice is about the batch, addressed to the
        # role that set it; the ticket it hangs on proves nothing (SYRD-537).
        if kind == reminder_snooze.REMINDER_SNOOZE_DUE:
            return True
        terminal_states = {stage["name"] for stage in self.workflow["stages"] if stage["terminal"]} if getattr(self,"workflow",None) else TERMINAL_STATES
        if current_state in terminal_states:
            return (
                kind == "transition"
                and expected_state in terminal_states
                and current_state == expected_state
            )
        # SYRD-517: a turn-end prompt or grace escalation queued before the
        # ticket was held (or blocked, or put in a wait) was still delivered
        # after it -- MEFP-114's Director was told, turn after turn, that a
        # deliberately held ticket was unresolved. The generator already asks
        # ticket_turn_is_resolved before it enqueues; delivery now asks the
        # same question, so the two cannot disagree. Dropping the stale row
        # loses nothing: once the hold is lifted, the next unresolved turn
        # is prompted afresh.
        if kind in UNRESOLVED_TURN_KINDS:
            resolved = conn.execute(
                "SELECT ticket_board.ticket_turn_is_resolved(%s, clock_timestamp()) AS resolved",
                (ticket_id,),
            ).fetchone()
            if bool(resolved["resolved"] if isinstance(resolved, dict) else resolved[0]):
                return False
        # SYRD-514: an escalation queued in an earlier round (before a
        # submission and return, or a reassignment away and back) matches the
        # current state and assignee again, so the checks above keep it. The
        # generator only escalates prompts made since the current assignment
        # began; delivery applies the same bound to the prompt time the
        # escalation carries.
        if kind == "unresolved_turn":
            try:
                prompted_at = datetime.fromisoformat(str(parsed.get("prompted_at")))
            except ValueError:
                prompted_at = None
            if prompted_at is not None and prompted_at.tzinfo is not None:
                # to_jsonb keeps a board without the SYRD-514 column readable: no column, no bound.
                row = conn.execute(
                    """
SELECT (to_jsonb(ns)->>'current_assignment_at')::timestamptz > %s AS predates
FROM ticket_board.ticket_notification_state ns
WHERE ns.ticket_id = %s
""",
                    (prompted_at, ticket_id),
                ).fetchone()
                if row is not None and bool(row["predates"] if isinstance(row, dict) else row[0]):
                    return False
        # SYRD-527: the owner's repair prompt from an earlier round matches the
        # current state and assignee again after a submission and return, or a
        # reassignment away and back, exactly as the escalation above did. Its
        # payload carries no prompt time, and none is invented: its own queue
        # row is stamped created_at by the board when the prompt is generated
        # -- once per turn identity, and a refresh never moves it -- so rows
        # queued by any release have one. The same bound applies: a prompt made
        # before the current assignment began is not about this round.
        if kind == "unresolved_turn_repair":
            # to_jsonb keeps a board without the SYRD-514 column readable: no column, no bound.
            row = conn.execute(
                """
SELECT bool_or((to_jsonb(ns)->>'current_assignment_at')::timestamptz > q.created_at) AS predates
FROM ticket_board.ticket_notification_queue q
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = q.ticket_id
WHERE q.ticket_id = %s AND q.kind = 'unresolved_turn_repair' AND q.payload = %s::jsonb
""",
                (ticket_id, payload),
            ).fetchone()
            if row is not None and bool(row["predates"] if isinstance(row, dict) else row[0]):
                return False
        if kind == "awaiting_role":
            # Wait identity, not delivery ACK or comments, controls resolution.
            # Expired windows prevent a restart from delivering a reminder burst.
            if (self.workflow and not any(stage["name"] == current_state and not stage["terminal"] and stage["kind"] != "draft" for stage in self.workflow["stages"])) or (not self.workflow and current_state not in {"in_progress", "inspection", "audit", "user_review"}):
                return False
            expected_target = "director" if parsed.get("step") == 4 else parsed.get("awaiting_role")
            if target_role != expected_target:
                return False
            # A handoff tells a named role to act now, and a ticket whose own
            # dependency has not resolved cannot say that. The board refuses to
            # create such a wait and clears one a later blocker overtakes, so
            # this is the third place the same rule is checked rather than the
            # first: the queue row may have been written before any of that,
            # and `has_unresolved_blockers` is already read here (SYRD-148).
            if has_unresolved_blockers:
                return False
            row = conn.execute(
                """
SELECT EXISTS (
    SELECT 1 FROM ticket_board.ticket_notification_state
    WHERE ticket_id = %s AND awaiting_role = %s
      AND awaiting_since_at = %s::timestamptz
      AND clock_timestamp() < %s::timestamptz
)
""",
                (ticket_id, parsed.get("awaiting_role"), parsed.get("awaiting_since_at"), parsed.get("expires_at")),
            ).fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        if kind == "escalation":
            return (
                target_role == "director"
                and current_state in NUDGE_ELIGIBLE_STATES
                and current_assignee != "unassigned"
                and not manually_controlled
                and not parked
                and not has_unresolved_blockers
            )
        required_final_review_handoff = (
            kind == "transition"
            and expected_state == "director_review"
            and current_state == "director_review"
            and expected_assignee == "director"
            and current_assignee == "director"
            and target_role == "director"
        )
        # Scheduling flags hold owner work and optional reminders. They do
        # not undo a completed handoff to the Director's final review.
        if kind in {"idle_reminder", "nudge", "triage"} and (
            manually_controlled or parked or has_unresolved_blockers
        ) and not required_final_review_handoff:
            return False
        # ... and manual control does not undo any other handoff either. A
        # transition notification exists only because somebody OTHER than the
        # recipient moved this ticket into a stage that role owns: both of
        # those were decided when the row was enqueued, and a held ticket
        # enqueues one precisely when the work changes hands (SYRD-107).
        # Dropping it here undid that fix for every held ticket. Live on
        # SYRD-146: submitted commit-exempt to inspection, Inspector assigned,
        # the listener running with its original PID, and nothing ever sent --
        # the Director had put the rollout under manual control while the
        # handoff was still queued, and every retry threw it away as stale
        # (SYRD-178). Parked and blocked still stop it: neither says the work
        # can be done now.
        if kind == "transition" and (
            parked or has_unresolved_blockers
        ) and not required_final_review_handoff:
            return False

        current_target_role = self._current_target_role(kind, current_state, current_assignee)
        return current_target_role == target_role if getattr(self, "workflow", None) else current_target_role is None or current_target_role == target_role

    def _serial_gate_stage(self, target_role: str, payload: str) -> str:
        """The stage this notification would put `target_role` to work in, if it is serial there.

        Empty when nothing should be held: not a transition, not addressed to
        the role it names, or a stage this role is allowed to hold several
        tickets in at once.

        A declared workflow answers from the document -- the role owns the
        stage, and the role is serial in it. Without a document there is nothing
        to read, so the legacy rule stands unchanged: the implementation stage
        only, and not for the review and control roles a static workflow names
        (SYRD-37).
        """
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        if str(parsed.get("kind") or "").strip().lower() != "transition":
            return ""
        state = str(parsed.get("new_state") or "").strip()
        if not state:
            return ""
        assignee = str(parsed.get("assignee") or target_role).strip().lower()
        if assignee != target_role:
            return ""
        workflow = getattr(self, "workflow", None)
        if not workflow:
            if state != LEGACY_SERIAL_STAGE or target_role in LEGACY_NON_SERIAL_ROLES:
                return ""
            return state
        from .workflow_config import role_is_serial_in

        return state if role_is_serial_in(workflow, target_role, state) else ""

    def _finish_current_blocker(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> str:
        stage = self._serial_gate_stage(target_role, payload)
        if not stage:
            return ""
        result = conn.execute(
            "SELECT ticket_board.finish_current_stage_blocker(%s::text, %s::text, %s::text)",
            (ticket_id, target_role, stage),
        )
        row = result.fetchone()
        if row is None:
            return ""
        if isinstance(row, dict):
            return self.decode_text(row.get("id", ""))
        return self.decode_text(row[0])
