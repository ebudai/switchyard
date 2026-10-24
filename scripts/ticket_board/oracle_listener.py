"""The Oracle board's notification delivery: the durable queue, delivered with bounded polling (SYRD-642).

PostgreSQL wakes its listener with NOTIFY as a hint, and the queue is the
source of truth either way. Oracle has no NOTIFY, so this loop just polls,
bounded:
* it asks for the next due notice at once after a delivery;
* when nothing is due it waits `poll_seconds` (default 5);
* when the database is unreachable it waits `reconnect_seconds` and connects
  again.
A notice is never lost: it stays queued until it is acknowledged.

One pass (`deliver_due`):
1. **Claim** the oldest due notice: `FOR UPDATE SKIP LOCKED`, fetching exactly
   one row, and recording `claimed_at` and the attempt. A claim older than
   `CLAIM_TIMEOUT_SECONDS` may be claimed again, so a listener that died
   mid-delivery delays a notice but never loses it.
2. **Resolve** the target role's pane from its runtime registration, and only
   while the registered process is still the live one (`peer_identity`). No
   live pane, or a busy one, puts the notice back with a delay. Nothing is
   typed into a pane that is not the role's.
3. **Send** through the same sender the PostgreSQL listener uses (directorctl),
   then **acknowledge**: delete the notice and trace the delivery.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from typing import Any, Callable

from .oracle_app import CLAIM_TIMEOUT_SECONDS
from .oracle_config import OracleConfig, connect, from_environment

LOGGER = logging.getLogger("ticket_board.oracle_listener")
POLL_SECONDS = 5.0
RECONNECT_SECONDS = 5.0
#: How long a notice waits when its pane is missing or busy, before it is tried again.
RETRY_SECONDS = 15


def trace(cursor: Any, notice: dict[str, Any], event: str, detail: str = "") -> None:
    cursor.execute("INSERT INTO notification_trace (ts, ticket_id, notification_id, target_role, kind, event, detail) "
                   "VALUES (SYSTIMESTAMP, :t, :n, :r, :k, :e, :d)", t=notice["ticket_id"], n=notice["id"],
                   r=notice["target_role"], k=notice["kind"], e=event, d=detail[:4000] or None)


def claim(conn: Any) -> dict[str, Any] | None:
    """The oldest due notice, claimed for this pass, or None."""
    cursor = conn.cursor()
    cursor.prefetchrows = 1  # lock exactly the row fetched
    cursor.arraysize = 1
    cursor.execute(
        "SELECT id, ticket_id, kind, target_role, message, attempts FROM notification_queue "
        "WHERE next_attempt_at <= SYSTIMESTAMP "
        "AND (claimed_at IS NULL OR claimed_at < SYSTIMESTAMP - NUMTODSINTERVAL(:timeout, 'SECOND')) "
        "ORDER BY next_attempt_at, id FOR UPDATE SKIP LOCKED", timeout=CLAIM_TIMEOUT_SECONDS)
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        conn.rollback()
        return None
    notice = dict(zip(("id", "ticket_id", "kind", "target_role", "message", "attempts"), row))
    update = conn.cursor()
    update.execute("UPDATE notification_queue SET claimed_at = SYSTIMESTAMP, attempts = attempts + 1 WHERE id = :id",
                   id=notice["id"])
    trace(update, notice, "claim")
    conn.commit()
    return notice


def acknowledge(conn: Any, notice: dict[str, Any]) -> None:
    cursor = conn.cursor()
    cursor.execute("DELETE FROM notification_queue WHERE id = :id", id=notice["id"])
    trace(cursor, notice, "ack")
    conn.commit()


def requeue(conn: Any, notice: dict[str, Any], reason: str, delay_seconds: int = RETRY_SECONDS) -> None:
    cursor = conn.cursor()
    cursor.execute("UPDATE notification_queue SET claimed_at = NULL, last_error = :e, "
                   "next_attempt_at = SYSTIMESTAMP + NUMTODSINTERVAL(:d, 'SECOND') WHERE id = :id",
                   e=reason[:4000], d=delay_seconds, id=notice["id"])
    trace(cursor, notice, "requeue", reason)
    conn.commit()


def live_targets(conn: Any, is_live: Callable[[int, int], bool]) -> dict[str, str]:
    """Each registered role's pane target, while its registered process is the one running."""
    cursor = conn.cursor()
    cursor.execute("SELECT role, actual_target, process_pid, process_start_time FROM role_runtime_assignments")
    return {role: target for role, target, pid, start in cursor if is_live(int(pid), int(start))}


def _process_is_live(pid: int, start_time: int) -> bool:
    from .peer_identity import SessionIdentity, session_is_live

    return session_is_live(SessionIdentity(pid=pid, start_time=start_time))


def deliver_due(conn: Any, send: Callable[[str, str], None], *, busy: Callable[[str], bool] = lambda _t: False,
                is_live: Callable[[int, int], bool] = _process_is_live, limit: int = 100) -> int:
    """Deliver every due notice this pass can; returns how many were delivered."""
    delivered = 0
    for _ in range(limit):
        notice = claim(conn)
        if notice is None:
            break
        target = live_targets(conn, is_live).get(notice["target_role"])
        if not target:
            requeue(conn, notice, f"{notice['target_role']} has no live registered pane")
            continue
        if busy(target):
            requeue(conn, notice, f"{target} is busy", delay_seconds=5)
            continue
        try:
            send(target, notice["message"])
        except Exception as exc:  # noqa: BLE001 -- the notice stays queued and is tried again
            requeue(conn, notice, f"send to {target} failed: {exc}")
            continue
        acknowledge(conn, notice)
        delivered += 1
    return delivered


def run_forever(config: OracleConfig, send: Callable[[str, str], None], *, stop: threading.Event,
                busy: Callable[[str], bool] = lambda _t: False, poll_seconds: float = POLL_SECONDS,
                reconnect_seconds: float = RECONNECT_SECONDS, connector: Callable[[OracleConfig], Any] = connect) -> None:
    while not stop.is_set():
        try:
            conn = connector(config)
        except Exception as exc:  # noqa: BLE001 -- an outage: wait, and connect again
            LOGGER.warning("Oracle board unreachable: %s", str(exc).splitlines()[0])
            stop.wait(reconnect_seconds)
            continue
        try:
            while not stop.is_set():
                if deliver_due(conn, send, busy=busy) == 0:
                    stop.wait(poll_seconds)
        except Exception as exc:  # noqa: BLE001 -- a dropped connection: reconnect
            LOGGER.warning("Oracle board connection lost: %s", str(exc).splitlines()[0])
            stop.wait(reconnect_seconds)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def delivery(directorctl: str) -> tuple[Callable[[str, str], None], Callable[[str], bool]]:
    """The PostgreSQL listener's own sender and pane activity gate: nothing is typed into a pane mid-turn."""
    from .notification_dispatch import DirectorctlSender
    from .pane_activity_gate import PaneActivityGate

    return DirectorctlSender(directorctl), PaneActivityGate().is_working


def main(argv: list[str] | None = None) -> int:
    from .notification_dispatch import DEFAULT_DIRECTORCTL

    parser = argparse.ArgumentParser(description="Deliver an Oracle board's queued notices to tmux panes.")
    parser.add_argument("--directorctl", default=DEFAULT_DIRECTORCTL)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    args, _unknown = parser.parse_known_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = from_environment()
    LOGGER.info("delivering for the Oracle board at %s (polling every %.1fs)", config.endpoint, args.poll_seconds)
    send, busy = delivery(args.directorctl)
    run_forever(config, send, stop=threading.Event(), busy=busy, poll_seconds=args.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
