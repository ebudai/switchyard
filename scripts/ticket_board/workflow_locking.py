"""Workflow applies and previews that serialize, bound every wait, and never wedge readers (SYRD-572).

`ticket_board.apply_declared_workflow` replaces the stage and transition graph
and alters constraints on `tickets`, so even a preview -- run whole, then rolled
back -- takes `AccessExclusiveLock` on `workflow_stages` and `tickets`. On MEFP
(2026-10-07) that went wrong two ways, with no timeout anywhere to end either:

* **Concurrent previews deadlocked.** Each read the serial reservations first,
  holding a share lock on `tickets`, and only then reached the function's
  advisory lock. One preview held the advisory lock and wanted `tickets`
  exclusively; another held a share lock on `tickets` and wanted the advisory
  lock. PostgreSQL detected that one and aborted a victim.
* **A preview queued for `tickets` behind a share lock held for ever.** Every
  later reader queued behind the preview's exclusive request, and the board's
  ticket reads stopped for fifteen minutes.

So an apply here takes the workflow advisory lock as the first statement of its
transaction -- before anything holds a share lock -- with a generous bound,
since waiting for it blocks no reader. Its table locks then get a short bound,
because a reader *can* queue behind those. A wait that runs out, a detected
deadlock or a statement that overruns rolls the transaction back, which leaves
nothing changed, and is retried a few times. A real apply is safe to retry: it
carries the revision it was computed from. Past the last attempt the caller is
told plainly that nothing changed and why.

Every setting is transaction-local (`set_config(..., true)`), so a connection
goes back exactly as it came, committed, rolled back or closed by its `with`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

#: The key `apply_declared_workflow` itself takes, so its own call is re-entrant.
WORKFLOW_LOCK_KEY = "ticket_board.workflow_configuration"
#: Waiting behind another preview or apply: blocks no reader, so it can be generous.
SERIALIZE_WAIT_SECONDS = 30.0
#: The function's table locks: readers queue behind a pending exclusive request,
#: so this is how long a preview may hold them up.
TABLE_LOCK_WAIT_SECONDS = 5.0
STATEMENT_SECONDS = 60.0
ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 0.25


class WorkflowBusy(RuntimeError):
    """The workflow could not take its locks within its bounds; nothing was changed."""


def _retryable() -> tuple[type[BaseException], ...]:
    from psycopg import errors

    # 55P03 lock_not_available (lock_timeout), 40P01 deadlock_detected,
    # 57014 query_canceled (statement_timeout).
    return (errors.LockNotAvailable, errors.DeadlockDetected, errors.QueryCanceled)


def _set_local(conn: Any, name: str, seconds: float) -> None:
    conn.execute("SELECT set_config(%s, %s, true)", (name, f"{int(seconds * 1000)}ms"))


def apply_workflow_bounded(
    connect: Callable[[], Any],
    cfg: dict[str, Any],
    *,
    expected_revision: int,
    dry_run: bool,
    caller_role: str,
    set_caller_role: Callable[[Any, str], None],
    reservations: Callable[[Any], dict[str, Any]],
    reservation_changes: Callable[[Any, dict[str, Any]], dict[str, Any]],
    serialize_wait_seconds: float = SERIALIZE_WAIT_SECONDS,
    table_lock_wait_seconds: float = TABLE_LOCK_WAIT_SECONDS,
    statement_seconds: float = STATEMENT_SECONDS,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Apply (or preview) a validated workflow document within bounded lock waits."""
    retryable = _retryable()
    causes: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with connect() as conn:
                _set_local(conn, "statement_timeout", statement_seconds)
                _set_local(conn, "lock_timeout", serialize_wait_seconds)
                # First, before anything below takes a share lock on tickets.
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (WORKFLOW_LOCK_KEY,))
                _set_local(conn, "lock_timeout", table_lock_wait_seconds)
                set_caller_role(conn, caller_role)
                held_before = reservations(conn)  # SYRD-539: what this document does to in-flight work
                row = conn.execute(
                    "SELECT ticket_board.apply_declared_workflow(%s::jsonb,%s) AS revision",
                    (json.dumps(cfg), expected_revision),
                ).fetchone()
                result = {"revision": row["revision"], "document": cfg, "dry_run": dry_run,
                          "reservation_changes": reservation_changes(conn, held_before)}
                if dry_run:
                    conn.rollback()
                return result
        except retryable as exc:
            # The `with` has rolled the transaction back and closed the connection.
            causes.append(f"attempt {attempt}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            if attempt < attempts:
                sleep(RETRY_PAUSE_SECONDS * attempt)
    raise WorkflowBusy(
        f"the workflow {'preview' if dry_run else 'change'} could not take the locks it needs within its "
        f"bounds ({serialize_wait_seconds:g}s behind another workflow change, {table_lock_wait_seconds:g}s "
        f"on the ticket tables, {attempts} attempts): {'; '.join(causes)}. Ticket reads or another workflow "
        "change held them; nothing was changed. Retry once they finish."
    )
