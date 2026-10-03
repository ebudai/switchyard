"""Ephemeral role session clear before a queued notification is sent."""

from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from . import session_context
from .notification_ledger import NotificationLedger

#: What each supported CLI is told, in its own composer, to start the next
#: ticket on an empty conversation. Every runtime the workflow document accepts
#: has an entry, and a runtime with no entry is not cleared silently -- the
#: notification waits instead (SYRD-135). They all spell it the same way today;
#: the table is here because that is a fact about four products, not a property
#: of the mechanism, and the day one of them spells it differently this is the
#: one line that changes. What each of them does with a typed `/clear` was
#: probed in disposable panes and written down in
#: docs/pgu-816-clear-sessionstart-evidence.md: all four start a fresh
#: conversation, and only Claude and Codex also announce it through a hook --
#: which is why nothing here depends on the hook.
SESSION_CLEAR_COMMANDS = {
    "claude": "/clear",
    "codex": "/clear",
    "agy": "/clear",
    "hermes": "/clear",
}
#: The notification kinds that hand a ticket to a role as work to do now.
#: Everything else a pane receives is about work it already has -- a comment on
#: its own ticket, a reminder that it looks idle, the director's escalation
#: about somebody else -- and clearing on any of those would delete the context
#: of the very work being asked about.
SESSION_CLEAR_KINDS = frozenset({"transition", "awaiting_role"})
SESSION_CLEAR_FAILED_ERROR = "ephemeral_session_clear_failed"
SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR = "ephemeral_session_clear_unsupported_runtime"
#: How long to let a cleared CLI finish resetting before the ticket is typed
#: into it. Short, because the clear is a local composer action; non-zero,
#: because the message that follows is the whole point of the clear.
DEFAULT_SESSION_CLEAR_SETTLE_SECONDS = 0.5

class NotificationSessionClear:
    def __init__(
        self, *, logger: logging.Logger, ledger: NotificationLedger,
        role_runtimes: Callable[[], dict[str, str]],
        ephemeral_roles: Callable[[], set[str]],
        announced_queue_identity: Callable[[str], object],
        sender: Callable[[], Callable[[str, str], object]],
        failure_reason: Callable[[BaseException, str], str],
        sleeper: Callable[[float], None],
        settle_seconds: Callable[[], float],
        gate: Callable[[], Any] = lambda: None,
    ) -> None:
        self.logger = logger
        self.ledger = ledger
        self.role_runtimes = role_runtimes
        self.ephemeral_roles = ephemeral_roles
        self.announced_queue_identity = announced_queue_identity
        self.sender = sender
        self.failure_reason = failure_reason
        self.sleeper = sleeper
        self.settle_seconds = settle_seconds
        #: The pane activity gate, for the hook state a ticket's conversation is proven from (SYRD-540).
        self.gate = gate

    def _session_clear_is_due(self, conn: Any, ticket_id: str, target_role: str, kind: str, payload: str) -> bool:
        """Whether this delivery is the first handoff of this ticket to an ephemeral role.

        Four independent questions, and every one of them has to say yes.
        The kind, because a comment or a reminder is about work the pane
        already has. The role, because ephemerality is declared per role and
        absent means false. The queue announcement, because being told a ticket
        is reserved for you later is not the ticket becoming yours now -- that
        is the same distinction the serial-focus gate above draws, and clearing
        on the announcement would erase the work the role is still doing. And
        the board, because "first" has to survive listener restarts.

        A database that cannot answer raises rather than returning False: the
        contract is that the ticket does not arrive until the clear has, and a
        failed read is not evidence that the clear already happened.
        """
        if target_role not in self.ephemeral_roles():
            return False
        if self.announced_queue_identity(payload) is not None:
            return False
        if kind not in SESSION_CLEAR_KINDS:
            # Not a hand-off, so never a clear; but nothing about a pulled
            # ticket reaches a pane that does not hold its conversation (SYRD-540).
            return self._restore_decision(conn, ticket_id, target_role, handoff=False) != "deliver"
        result = conn.execute(
            "SELECT ticket_board.role_session_clear_pending(%s::text, %s::text)",
            (ticket_id, target_role),
        )
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
        if row is None:
            return False
        pending = row["role_session_clear_pending"] if isinstance(row, dict) else row[0]
        # A pulled ticket handed back to a pane that no longer holds its
        # conversation gets it back first, or is parked (SYRD-540).
        return bool(pending) or self._restore_decision(conn, ticket_id, target_role, handoff=True, probe=True) != "deliver"

    def _first_handoff(self, conn: Any, ticket_id: str, target_role: str) -> bool:
        row = conn.execute("SELECT ticket_board.role_session_clear_pending(%s::text, %s::text) AS p",
                           (ticket_id, target_role)).fetchone()
        return bool(row) and bool(row["p"] if isinstance(row, dict) else row[0])

    def _context_listener(self) -> Any:
        from types import SimpleNamespace
        return SimpleNamespace(role_runtimes=self.role_runtimes(), sender=self.sender(), logger=self.logger)

    def _restore_decision(self, conn: Any, ticket_id: str, target_role: str, *, handoff: bool, probe: bool = False) -> str:
        """session_context.before_delivery; `probe` only reads (status), so asking whether a clear is due acts on nothing."""
        import time
        if probe:
            status = session_context._status(conn, ticket_id, target_role)
            return "act" if session_context.needs_action(status, self.gate(), target_role) else "deliver"
        return session_context.before_delivery(self._context_listener(), conn, self.gate(), ticket_id=ticket_id,
                                               role=target_role, now=time.time(), handoff=handoff)

    def _record_session_clear(self, conn: Any, ticket_id: str, target_role: str) -> bool:
        result = conn.execute(
            "SELECT ticket_board.record_role_session_clear(%s::text, %s::text)",
            (ticket_id, target_role),
        )
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
        if row is None:
            return False
        recorded = row["record_role_session_clear"] if isinstance(row, dict) else row[0]
        return bool(recorded)
    def _clear_role_session(
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
    ) -> bool:
        """Send the role's CLI its clear command, and say whether the ticket may follow.

        Recording happens after the send returns, never before: a row written
        first would mark a clear that did not happen, and the ticket would then
        be delivered onto the previous one's context with the board believing
        the opposite. A send that fails leaves no row, so the requeued
        notification tries again.
        """
        runtime = self.role_runtimes().get(target_role, "")
        command = SESSION_CLEAR_COMMANDS.get(runtime, "")
        handoff = kind in SESSION_CLEAR_KINDS
        if not handoff or not self._first_handoff(conn, ticket_id, target_role):
            # SYRD-540: a pulled ticket whose conversation the pane does not hold.
            # It is never cleared: restored and proven first, or parked.
            decision = self._restore_decision(conn, ticket_id, target_role, handoff=handoff)
            if decision == "deliver":
                return True
            if decision == "park":
                self.ledger.trace(conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role,
                                  kind=kind, event="drop", busy_reason="ticket_context_parked", detail={"target": target})
                self.ledger.discard(conn, notification_id, "ticket_context_parked")
                return False
            # Bounded: a restore is confirmed or parked within its own deadlines.
            self.ledger.requeue(conn, notification_id, attempts, f"ticket_context_{decision}",
                                delay_seconds=session_context.RESUME_WAIT_SECONDS / 3)
            return False
        if not command:
            self.logger.error(
                "Holding notification %s for %s: no clear command is known for runtime %r of ephemeral role %s",
                notification_id, ticket_id, runtime, target_role,
            )
            self.ledger.trace(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="session_clear_failed",
                busy_reason=SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR,
                detail={"target": target, "runtime": runtime, "attempts": attempts},
            )
            self.ledger.requeue(
                conn, notification_id, attempts, SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR
            )
            return False
        try:
            self.sender()(target, command)
        except (subprocess.SubprocessError, OSError) as exc:
            failure_reason = self.failure_reason(exc, target)
            self.logger.warning(
                "Failed to clear the session of ephemeral role %s before %s: %s",
                target_role, ticket_id, exc,
            )
            self.ledger.trace(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="session_clear_failed",
                busy_reason=failure_reason,
                detail={
                    "target": target,
                    "runtime": runtime,
                    "command": command,
                    "attempts": attempts,
                },
            )
            if failure_reason == "tmux_target_missing":
                self.ledger.dead_letter(
                    conn,
                    notification_id,
                    failure_reason,
                    target=target,
                    message=message,
                    attempts=attempts,
                    payload="",
                )
            else:
                self.ledger.requeue(
                    conn, notification_id, attempts, f"{SESSION_CLEAR_FAILED_ERROR}: {failure_reason}"
                )
            return False
        recorded = self._record_session_clear(conn, ticket_id, target_role)
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="session_clear",
            detail={
                "target": target,
                "runtime": runtime,
                "command": command,
                "recorded": recorded,
                "attempts": attempts,
            },
        )
        if self.settle_seconds() > 0:
            self.sleeper(self.settle_seconds())
        return True
