"""The ticket notify listener's record of each queued notification (SYRD-484).

Everything the listener writes to the board about one notification goes
through `NotificationLedger`: the trace rows that say what happened to it, and
its disposition -- acked as delivered, discarded, requeued with backoff, or
dead-lettered. Each is one statement on the listener's autocommit connection,
and the board function it calls owns the idempotency; a trace that fails is
logged, never fatal, so tracing can never wedge delivery.

The ledger also owns the one piece of delivery state those writes share:
which notifications already have their deferral traced. A notification that
waits behind a busy pane is requeued on every pass; its deferral is traced
once, and forgotten when the notification reaches a terminal disposition.
"""

from __future__ import annotations

import json
from typing import Any, Callable


class NotificationLedger:
    def __init__(self, *, logger: Any, requeue_base_seconds: float, requeue_max_seconds: float) -> None:
        self.logger = logger
        self.requeue_base_seconds = requeue_base_seconds
        self.requeue_max_seconds = requeue_max_seconds
        #: Notifications whose current deferral has already been traced.
        self._traced_gate_defer_notifications: set[int] = set()

    def backoff_seconds(self, attempts: int) -> float:
        exponent = max(attempts - 1, 0)
        return min(self.requeue_max_seconds, self.requeue_base_seconds * (2**exponent))

    def safe_json_payload(self, payload: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

    def trace(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        event: str,
        pane_busy: bool | None = None,
        busy_reason: str | None = None,
        region_digest: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        pane_state = None if pane_busy is None else ("busy" if pane_busy else "idle")
        try:
            conn.execute(
                """
SELECT ticket_board.record_notification_trace(
    %s::text,
    %s::bigint,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::jsonb
)
""",
                (
                    ticket_id,
                    notification_id,
                    target_role,
                    kind,
                    event,
                    pane_state,
                    busy_reason,
                    region_digest,
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )
        except Exception as exc:  # Trace failures must not wedge delivery.
            self.logger.warning("Failed to record notification trace for %s/%s: %s", notification_id, event, exc)

    def ack(self, conn: Any, notification_id: int) -> None:
        conn.execute("SELECT ticket_board.ack_notification(%s::bigint)", (notification_id,))

    def discard(self, conn: Any, notification_id: int, reason: str) -> None:
        """Remove a queued notification that was never delivered.

        Not an ack: ack_notification records delivery accounting, and for an
        idle_reminder that means incrementing idle_reminder_count, which turns
        the next idle wave into an escalation to the director (SYRD-32).
        """
        conn.execute(
            "SELECT ticket_board.discard_notification(%s::bigint, %s::text)",
            (notification_id, reason),
        )

    def requeue(
        self,
        conn: Any,
        notification_id: int,
        attempts: int,
        error: str,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        delay_seconds = self.backoff_seconds(attempts) if delay_seconds is None else delay_seconds
        conn.execute(
            "SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
            (notification_id, f"{delay_seconds:g} seconds", error[:500]),
        )

    def dead_letter(
        self,
        conn: Any,
        notification_id: int,
        reason: str,
        *,
        target: str,
        message: str,
        attempts: int,
        payload: str,
        error_output: str = "",
    ) -> None:
        conn.execute(
            "SELECT ticket_board.dead_letter_notification(%s::bigint, %s::text, %s::jsonb)",
            (
                notification_id,
                reason[:500],
                json.dumps(
                    {
                        "target": target,
                        "message": message,
                        "attempts": attempts,
                        "payload": self.safe_json_payload(payload),
                        **({"error_output": error_output[-1000:]} if error_output else {}),
                    },
                    sort_keys=True,
                ),
            ),
        )
        self.forget(notification_id)

    def trace_deferral_once(self, conn: Any, *, notification_id: int, detail: Callable[[], dict[str, Any]], **fields: Any) -> None:
        """Trace a deferral the first time this notification is deferred, not on every requeue."""
        if notification_id not in self._traced_gate_defer_notifications:
            self.trace(conn, notification_id=notification_id, detail=detail(), **fields)
            self._traced_gate_defer_notifications.add(notification_id)

    def forget(self, notification_id: int) -> None:
        """The notification reached a terminal disposition; a later deferral of the same id is news again."""
        self._traced_gate_defer_notifications.discard(notification_id)
