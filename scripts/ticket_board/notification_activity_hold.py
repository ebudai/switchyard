"""Activity gating, stale reminders, and bounded finished-turn holds."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from . import composer_hold
from .notification_dispatch import ComposerSnapshot, NotificationDispatch
from .notification_eligibility import NotificationEligibility
from .notification_ledger import NotificationLedger

# Reminders addressed to the same role they are about. Their whole claim is
# "you appear idle on this ticket", so observing that role work voids them.
# 'escalation' is deliberately absent: it goes to the director about someone
# else's stall, so the director's own pane says nothing about that stall.
SELF_REMINDER_KINDS = frozenset({"nudge", "idle_reminder"})

#: How long a handoff may wait behind a FINISHED turn's descendants before it
#: is delivered anyway and the wait is escalated in the record. Fifteen minutes:
#: long enough that an ordinary verification run started before the turn-end
#: hook finishes inside it, short enough that nobody loses most of an hour to a
#: poll loop nothing will ever satisfy. The live stall ran 38 minutes and was
#: ended by a human (SYRD-212).
DEFAULT_PRIOR_TURN_HOLD_MAX_SECONDS = 900.0

#: How many consecutive probes a prior turn's descendant must keep using CPU on
#: before it is believed to be progressing rather than waking. See
#: `ORPHANED_PRIOR_TURN_WAITER` (SYRD-212).
#: A descendant of a finished turn that is still spending CPU, but only in the
#: bursts a poll loop spends it in: awake on one probe, asleep on the next.
#:
#: SYRD-101 classified a prior turn's leftovers by lifecycle and made an
#: exception for progress, so that an hour-long sweep from before the turn-end
#: hook keeps its pane quiet. The exception was tested only for whether ANY
#: survivor's CPU had risen, and a shell polling a task-output file every ten
#: seconds raises it too. So the exception swallowed the rule: routing SYRD-211
#: to App on 2026-09-18, notification 2842 sat undelivered for 38 minutes
#: against a pane whose trusted hook had said idle at 21:02, because a SYRD-210
#: poll loop started at 20:49 woke often enough to look like work (SYRD-212).
#:
#: The difference is not what the process is called; it is whether it is making
#: progress or waiting. A build advances on every probe. A waiter advances on
#: some and not others, which is what this reason names.
ORPHANED_PRIOR_TURN_WAITER = "orphaned_prior_turn_waiter"
#: A prior turn's descendant that IS progressing: CPU rising on consecutive
#: probes, not in bursts. Held as work, exactly as SYRD-101 requires, but said
#: with its own name so that a trace can tell it apart from this turn's work
#: and from a waiter (SYRD-212).
PRIOR_TURN_CHILD_WORK = "prior_turn_child_work"
#: The holds a later handoff may not wait behind indefinitely: both come from a
#: turn the runtime has already declared finished. `pane_child_work` is absent
#: on purpose -- that is the CURRENT turn working, and interrupting it is what
#: the activity gate exists to prevent.
PRIOR_TURN_HOLD_REASONS = frozenset({PRIOR_TURN_CHILD_WORK, ORPHANED_PRIOR_TURN_WAITER})
#: Why a queued handoff was dropped instead of delivered: the owner reached the
#: ticket without it, so delivering would hand them the same assignment twice.
OWNER_ALREADY_ACTED = "owner_already_acted"
PRIOR_TURN_HOLD_DELIVER = "deliver"
PRIOR_TURN_HOLD_DISCARD = "discard"


@dataclass(frozen=True)
class ActivityTrace:
    busy: bool
    reason: str
    region_digest: str = ""


@dataclass(frozen=True)
class ComposerHoldTrace(ActivityTrace):
    """The gate's composer verdict, and whether the runtime's own trusted hook says the turn is over (SYRD-570).

    Built here, from the gate's trace and its hook state, so the gate's own
    traces stay exactly what they were.
    """

    trusted_idle: bool = False


class NotificationActivityHold:
    def __init__(
        self,
        *,
        activity_gate: Callable[[], Callable[[str], bool]],
        work_evidence_reasons: Callable[[], frozenset[str]],
        logger: logging.Logger,
        ledger: NotificationLedger,
        eligibility: NotificationEligibility,
        dispatch: NotificationDispatch,
        prior_turn_hold_max_seconds: float,
        monotonic: Callable[[], float],
    ) -> None:
        self.activity_gate = activity_gate
        self.work_evidence_reasons = work_evidence_reasons
        self.logger = logger
        self.ledger = ledger
        self.eligibility = eligibility
        self.dispatch = dispatch
        self.prior_turn_hold_max_seconds = max(0.0, prior_turn_hold_max_seconds)
        self.monotonic = monotonic
        #: When each still-queued handoff first waited behind a finished turn's
        #: descendants. Keyed by notification, because the bound belongs to the
        #: handoff that is waiting rather than to the pane it waits on.
        self._prior_turn_hold_started_at: dict[int, float] = {}

    @staticmethod
    def _decode_text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def _activity_trace(self, target: str, pane_busy: bool) -> ActivityTrace:
        gate_owner = getattr(self.activity_gate(), "__self__", None)
        last_trace = getattr(gate_owner, "last_trace", None)
        if callable(last_trace):
            trace = last_trace(target)
            if isinstance(trace, ActivityTrace):
                if trace.busy and trace.reason in composer_hold.HOLD_REASONS:
                    # SYRD-570: whether the runtime's own trusted hook says the
                    # turn is over while the composer holds the pane.
                    return ComposerHoldTrace(trace.busy, trace.reason, trace.region_digest,
                                             composer_hold.trusted_idle(gate_owner.state_store.read(target)))
                return trace
        return ActivityTrace(pane_busy, "busy" if pane_busy else "idle")

    def _activity_state_for_notification(self, kind: str, target: str, *, pre_send_recheck: bool = False) -> tuple[bool, ActivityTrace]:
        """Whether this destination is working, for any kind and any role.

        One gate for everything. The anti-clobber gate reports a working agent
        as free -- it only guards a human's half-typed line -- so any kind
        allowed to use it is a kind that can be typed into a running turn. That
        exemption is what put "please read /tmp/directorctl_payload..." into
        Main's composer mid-implementation, and it is gone.

        Nothing here reads a role name, a project, a port or a CLI name. The
        activity gate resolves the destination's runtime from the declared
        workflow and reads that runtime's own hook state, so a role added or
        re-hosted later is covered without being named.
        """
        if pre_send_recheck:
            gate_owner = getattr(self.activity_gate(), "__self__", None)
            pre_send_full_busy = getattr(gate_owner, "pre_send_busy", None)
            if callable(pre_send_full_busy):
                pane_busy = bool(pre_send_full_busy(target))
                return pane_busy, self._activity_trace(target, pane_busy)
        pane_busy = self.activity_gate()(target)
        return pane_busy, self._activity_trace(target, pane_busy)

    def _should_defer_for_activity(self, activity_trace: ActivityTrace) -> bool:
        return activity_trace.busy

    def _reminder_is_stale_for_activity(self, kind: str, activity_trace: ActivityTrace) -> bool:
        """Whether a queued self-directed reminder has been voided by real work.

        Requeueing such a reminder behind a busy pane only re-delivers a claim
        that is already false, which is how SYRD-32 was seen live: the Inspector
        was mid-review and still received "you appear idle". The idle generators
        raise a fresh reminder if the owner genuinely stalls again, so dropping
        loses nothing. Only work evidence counts -- a pane held busy for some
        other reason still requeues.
        """
        return kind in SELF_REMINDER_KINDS and activity_trace.reason in self.work_evidence_reasons()

    def _drop_stale_reminder(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        target: str,
        message: str,
        attempts: int,
        activity_trace: ActivityTrace,
        composer_before: ComposerSnapshot,
        phase: str,
    ) -> None:
        self.logger.info(
            "Dropping stale %s notification %s for %s: %s is working (%s)",
            kind,
            notification_id,
            ticket_id,
            target,
            activity_trace.reason,
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="drop",
            pane_busy=True,
            busy_reason="stale_reminder_work_observed",
            region_digest=activity_trace.region_digest,
            detail={
                **self.dispatch._delivery_diagnostic_detail(
                    target=target,
                    message=message,
                    attempts=attempts,
                    activity_trace=activity_trace,
                    before=composer_before,
                    decision="drop",
                    reason="stale_reminder_work_observed",
                ),
                "phase": phase,
            },
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="listener_discard",
            detail={"reason": "stale_reminder_work_observed", "phase": phase},
        )
        # Never ack here: this reminder was suppressed, not delivered, and
        # ack_notification would credit it as a completed reminder round.
        self.ledger.discard(conn, notification_id, "stale_reminder_work_observed")
        self.ledger.forget(notification_id)

    def _release_prior_turn_hold(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        activity_trace: ActivityTrace,
    ) -> str:
        """Decide whether a handoff has waited behind a finished turn too long.

        Returns "" to leave the ordinary gate alone, `PRIOR_TURN_HOLD_DISCARD`
        when the owner has already reached this ticket without us, and
        `PRIOR_TURN_HOLD_DELIVER` when the wait is over the bound and the
        handoff should go out with the wait recorded against it.

        Only holds from a turn that has ENDED are bounded. This turn's own work
        is not: interrupting it is precisely what the activity gate exists to
        prevent, and no clock here may override that.
        """
        if activity_trace is None or activity_trace.reason not in PRIOR_TURN_HOLD_REASONS:
            # Whatever is holding it now, it is not a finished turn's leftovers.
            # Forgetting the wait is deliberate: a pane that went back to work
            # for its CURRENT turn starts the bound again if it later falls back
            # to a prior-turn hold.
            self._prior_turn_hold_started_at.pop(notification_id, None)
            return ""
        now = self.monotonic()
        started = self._prior_turn_hold_started_at.setdefault(notification_id, now)
        held = now - started
        if held < self.prior_turn_hold_max_seconds:
            return ""
        notified_at = self._owner_already_notified_at(conn, ticket_id, target_role)
        if notified_at:
            self.eligibility._drop_superseded_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                detail={
                    "held_seconds": round(held, 1),
                    "held_reason": activity_trace.reason,
                    "owner_notified_at": notified_at,
                },
                phase="prior_turn_hold",
                reason=OWNER_ALREADY_ACTED,
            )
            self._prior_turn_hold_started_at.pop(notification_id, None)
            return PRIOR_TURN_HOLD_DISCARD
        self.logger.warning(
            "Delivering notification %s for %s past a finished turn's %s held %.0fs",
            notification_id, ticket_id, activity_trace.reason, held,
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="gate_escalate",
            pane_busy=True,
            busy_reason=ORPHANED_PRIOR_TURN_WAITER,
            detail={
                "held_seconds": round(held, 1),
                "held_reason": activity_trace.reason,
                "bound_seconds": self.prior_turn_hold_max_seconds,
            },
        )
        self._prior_turn_hold_started_at.pop(notification_id, None)
        return PRIOR_TURN_HOLD_DELIVER

    def _owner_already_notified_at(self, conn: Any, ticket_id: str, target_role: str) -> str:
        """When this ticket's owner was last sent this assignment, if ever.

        The board's own `active_work_notified_at`: the newest successful send of
        a transition for this ticket, to this role, in the state it is in now.
        Read here rather than assumed, because it is the difference between a
        handoff nobody has seen -- which must go out -- and a second copy of one
        they already have (SYRD-212).
        """
        result = conn.execute(
            """
SELECT max(trace.ts)::text AS last_sent_at
FROM ticket_board.notification_trace trace
JOIN ticket_board.tickets t ON t.id = trace.ticket_id
WHERE trace.ticket_id = %s
  AND trace.target_role = %s
  AND trace.kind = 'transition'
  AND trace.event = 'send'
  AND trace.ticket_state_at_event = t.state
""",
            (ticket_id, target_role),
        )
        row = result.fetchone()
        if row is None:
            return ""
        value = row["last_sent_at"] if isinstance(row, dict) else row[0]
        return self._decode_text(value).strip()
