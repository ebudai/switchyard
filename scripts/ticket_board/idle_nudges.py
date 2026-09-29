"""The ticket notify listener's reminder generators (SYRD-480).

Once per pass, before due notifications are delivered, the listener asks the
board to enqueue the reminders that pane evidence calls for: a nudge when a
role's turn ended idle, a nudge when a role has sat idle past the grace, the
Director's handoffs for an owner who stopped without resolving and for a pane
stopped on a permission prompt, and the serial-focus capacity wake-ups.

`IdleNudges` owns what those generators remember between passes -- which
turn-end boundary and which present-idle instant each role was already acted
on, and when work was last observed in each pane -- and the timings they pass
to the board. It owns no transaction: every generator is one statement on the
listener's autocommit connection, and the board function it calls decides,
idempotently, what to enqueue. It reads the listener's activity gate and role
targets when a pass runs, because the listener rebinds both.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


class IdleNudges:
    def __init__(
        self,
        *,
        activity_gate: Callable[[], Any],
        role_targets: Callable[[], Mapping[str, str]],
        logger: Any,
        idle_stall_grace_seconds: float,
        idle_stall_nudge_cadence_seconds: float,
        idle_stall_escalate_after: int,
        unresolved_turn_grace_seconds: int,
        permission_prompt_grace_seconds: int,
    ) -> None:
        self._activity_gate = activity_gate
        self._role_targets = role_targets
        self.logger = logger
        self.idle_stall_grace_seconds = idle_stall_grace_seconds
        self.idle_stall_nudge_cadence_seconds = idle_stall_nudge_cadence_seconds
        self.idle_stall_escalate_after = idle_stall_escalate_after
        self.unresolved_turn_grace_seconds = unresolved_turn_grace_seconds
        self.permission_prompt_grace_seconds = permission_prompt_grace_seconds
        self._seen_turn_end_idle_since_by_role: dict[str, str] = {}
        self._consumed_present_idle_since_by_role: dict[str, str] = {}
        self._work_observed_at_by_role: dict[str, str] = {}

    def _reset_busy_backoff_for_idle_roles(self, conn: Any, idle_since_by_role: dict[str, str]) -> int:
        if not idle_since_by_role:
            return 0
        try:
            result = conn.execute(
                "SELECT ticket_board.reset_notification_backoff_for_idle_roles(%s::jsonb)",
                (json.dumps(idle_since_by_role, sort_keys=True),),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to reset busy notification backoff for idle panes: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            reset_count = int(value)
        except (TypeError, ValueError):
            return 0
        if reset_count:
            self.logger.info("Reset busy notification backoff for %s idle-pane rows", reset_count)
        return reset_count

    def _idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        gate_owner = getattr(self._activity_gate(), "__self__", None)
        idle_since_by_role = getattr(gate_owner, "idle_since_by_role", None)
        if not callable(idle_since_by_role):
            return {}
        try:
            checked_roles = roles if roles is not None else sorted(self._role_targets())
            idle_since = dict(idle_since_by_role(checked_roles))
            self._record_work_observed_from_gate(checked_roles)
            return idle_since
        except Exception as exc:
            self.logger.warning("Failed to read pane idle hook state for stall nudges: %s", exc)
            return {}

    def _record_work_observed_from_gate(self, roles: list[str]) -> None:
        from . import notify_listener as listener

        gate_owner = getattr(self._activity_gate(), "__self__", None)
        last_trace = getattr(gate_owner, "last_trace", None)
        if not callable(last_trace):
            return
        observed_at = datetime.now(timezone.utc).isoformat()
        for role in roles:
            target = self._role_targets().get(role)
            if target is None:
                continue
            try:
                trace = last_trace(target)
            except Exception:
                continue
            if isinstance(trace, listener.ActivityTrace) and trace.busy and trace.reason in listener.WORK_EVIDENCE_REASONS:
                self._work_observed_at_by_role[role] = observed_at

    def _work_observed_at_for_roles(self, roles: list[str]) -> dict[str, str]:
        return {role: self._work_observed_at_by_role[role] for role in roles if role in self._work_observed_at_by_role}

    def _turn_end_idle_since_by_role(self) -> dict[str, str]:
        gate_owner = getattr(self._activity_gate(), "__self__", None)
        turn_end_idle_since_by_role = getattr(gate_owner, "turn_end_idle_since_by_role", None)
        if not callable(turn_end_idle_since_by_role):
            return {}
        try:
            return dict(turn_end_idle_since_by_role())
        except Exception as exc:
            self.logger.warning("Failed to read pane turn-end idle hook state for reminders: %s", exc)
            return {}

    def _fresh_turn_end_idle_since_by_role(self) -> dict[str, str]:
        turn_end_idle_since = self._turn_end_idle_since_by_role()
        fresh_turn_end_idle_since: dict[str, str] = {}
        for role, idle_since in turn_end_idle_since.items():
            if self._seen_turn_end_idle_since_by_role.get(role) != idle_since:
                fresh_turn_end_idle_since[role] = idle_since
            self._seen_turn_end_idle_since_by_role[role] = idle_since
        stale_roles = set(self._seen_turn_end_idle_since_by_role) - set(turn_end_idle_since)
        for role in stale_roles:
            self._seen_turn_end_idle_since_by_role.pop(role, None)
        return fresh_turn_end_idle_since

    def _present_fresh_idle_since_by_role(self) -> dict[str, str]:
        idle_since_by_role = self._idle_since_by_role(
            [role for role in sorted(self._role_targets()) if role != "director"]
        )
        stale_roles = set(self._consumed_present_idle_since_by_role) - set(idle_since_by_role)
        for role in stale_roles:
            self._consumed_present_idle_since_by_role.pop(role, None)
        fresh_idle_since: dict[str, str] = {}
        for role, idle_since in idle_since_by_role.items():
            if role == "director":
                continue
            if self._consumed_present_idle_since_by_role.get(role) == idle_since:
                continue
            try:
                datetime.fromisoformat(idle_since)
            except ValueError:
                self._consumed_present_idle_since_by_role.pop(role, None)
                continue
            fresh_idle_since[role] = idle_since
            self._consumed_present_idle_since_by_role[role] = idle_since
        return fresh_idle_since

    def _process_permission_prompt_waits(self, conn: Any) -> int:
        """SYRD-234: a pane stopped on a prompt is waiting, not working.

        Every other generator here fires off the idle path. A pane stopped on a
        permission prompt never goes idle -- the activity gate reads `blocked`
        as busy and requeues behind it -- so without this the role looks like it
        is working for as long as nobody looks at it.
        """
        gate_owner = getattr(self._activity_gate(), "__self__", None)
        if gate_owner is None or not hasattr(gate_owner, "permission_prompt_waits"):
            # No gate means no pane state to read, which is "cannot tell" --
            # and cannot tell is not the same as nobody is waiting.
            return 0
        waiting = gate_owner.permission_prompt_waits()
        if not waiting:
            return 0
        try:
            result = conn.execute(
                "SELECT ticket_board.notify_permission_prompt_waits("
                "%s::jsonb, clock_timestamp(), %s::interval)",
                (
                    json.dumps(waiting, sort_keys=True),
                    f"{self.permission_prompt_grace_seconds} seconds",
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue permission-prompt waits: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info(
                "Told the director about %s pane(s) stopped on a permission prompt: %s",
                enqueued,
                ", ".join(sorted(waiting)),
            )
        return enqueued

    def _process_unresolved_turn_end(self, conn: Any, idle_since: dict[str, str]) -> int:
        """SYRD-194: tell the Director when an owner stopped without resolving.

        The turn identity is the confirmed turn-end boundary itself. This
        listener already dedupes on exactly that value -- a role appears in
        `idle_since` once per completed turn and not again until the next one --
        so the identity a lease is scoped to needs no separate minting in the
        hook, and cannot disagree with the boundary that produced it.

        Leases are consumed first, naming THIS turn: a lease taken for this
        boundary survives and suppresses, and one taken for any earlier boundary
        is retired, because the next turn it promised has now arrived. That is
        what keeps "I am continuing" a statement about the next turn rather than
        an indefinite flag.

        Independent of the reminder generator beside it, and run before it, so a
        Director handoff never depends on the reminder path having anything to
        say (SYRD-193).
        """
        # No early return on an empty map: the escalation half of this runs on
        # ordinary passes, because "the owner never answered" is a statement
        # about elapsed time and a silent owner ends no turns (SYRD-203).
        for role, turn_id in sorted(idle_since.items()):
            try:
                conn.execute(
                    "SELECT ticket_board.consume_turn_continuation(%s, %s)",
                    (role, turn_id),
                )
            except Exception as exc:  # pragma: no cover - logged, never fatal
                self.logger.warning(
                    "Failed to consume continuation lease for %s: %s", role, exc
                )
        try:
            result = conn.execute(
                "SELECT ticket_board.notify_unresolved_turn_end("
                "%s::jsonb, clock_timestamp(), %s::interval)",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.unresolved_turn_grace_seconds} seconds",
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue unresolved turn-end handoffs: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info(
                "Enqueued %s unresolved turn-end Director handoffs", enqueued
            )
        return enqueued

    def process_idle_turn_end_nudges(self, conn: Any) -> int:
        idle_since = self._fresh_turn_end_idle_since_by_role()
        if idle_since:
            self._consumed_present_idle_since_by_role.update(idle_since)
        else:
            idle_since = self._present_fresh_idle_since_by_role()
        if not idle_since:
            # The REMINDER generator below has nothing to say without a turn
            # end, and returning here is right for it. The unresolved-turn half
            # is not like that: its escalation is a statement about elapsed
            # time, and the owner it is waiting on is by definition silent, so
            # requiring a turn end to ask about them makes it depend on the one
            # event that cannot be assumed.
            #
            # SYRD-203 made `_process_unresolved_turn_end` tolerate an empty map
            # and said so in its docstring, but left this return in front of it,
            # so an ordinary pass never reached the code that had just been
            # taught to handle one. Live on SYRD-206: the owner was prompted
            # correctly, the grace expired, and the Director heard nothing until
            # some OTHER role happened to end a turn (SYRD-207).
            self._process_unresolved_turn_end(conn, {})
            # Same reasoning as the line above, and the same trap: a pane
            # stopped on a prompt produces no turn ends at all, so returning
            # before this would make the case it exists for unreachable.
            self._process_permission_prompt_waits(conn)
            return 0
        # The same two inputs the stall generator beside this one has taken
        # since SYRD-58. A turn ending says the previous turn finished, not that
        # the role stopped working: between the turns of one review the pane is
        # idle by every measure this path had, which is how Audit was told it
        # had not advanced a ticket it had just been handed, and escalated to
        # the Director a turn later (SYRD-163).
        work_observed_at = self._work_observed_at_for_roles(sorted(idle_since))
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_turn_end_nudges(
    %s::jsonb,
    clock_timestamp(),
    %s::interval,
    %s::jsonb
)
""",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.idle_stall_grace_seconds:g} seconds",
                    json.dumps(work_observed_at, sort_keys=True),
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue idle turn-end reminders: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s idle turn-end ticket nudges", enqueued)
        # After the reminder path, never conditional on it, so the generator
        # above decides on exactly the inputs it always had. An owner that
        # stopped without resolving is reported whether or not a reminder was
        # due, and whether or not one was enqueued (SYRD-194).
        self._process_unresolved_turn_end(conn, idle_since)
        self._process_permission_prompt_waits(conn)
        return enqueued

    def process_serial_focus_queue_wakeups(self, conn: Any) -> int:
        """Tell the director when a capacity wait they were told about has ended.

        Takes no idle map and no pane state, unlike the reminder generators
        either side of it. A reservation ending is a fact about the board, not
        about whether anybody's pane happens to be free, and gating it on a
        fresh idle sample would make the announcement wait for a coincidence
        (SYRD-109).
        """
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_serial_focus_queue_wakeups(clock_timestamp())
"""
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue serial-focus queue wake-ups: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s serial-focus capacity hand-offs", enqueued)
        return enqueued

    def process_idle_stall_nudges(self, conn: Any) -> int:
        idle_since = self._idle_since_by_role()
        work_observed_at = self._work_observed_at_for_roles(sorted(self._role_targets()))
        if not idle_since and not work_observed_at:
            return 0
        if idle_since:
            self._reset_busy_backoff_for_idle_roles(conn, idle_since)
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_stall_nudges(
    %s::jsonb,
    clock_timestamp(),
    %s::interval,
    %s::interval,
    %s::integer,
    %s::jsonb
)
""",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.idle_stall_grace_seconds:g} seconds",
                    f"{self.idle_stall_nudge_cadence_seconds:g} seconds",
                    self.idle_stall_escalate_after,
                    json.dumps(work_observed_at, sort_keys=True),
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue idle-stall nudges: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s idle-stall ticket nudges", enqueued)
        return enqueued
