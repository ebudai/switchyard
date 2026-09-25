#!/usr/bin/env python3
"""SYRD-264: the board says whether the owner was actually told.

MEFP-1 was highlighted as Ops's current work, and the User read that as "Ops
has been notified". The highlight has never meant that: it is workflow
ownership, and the ticket's only notice was dead-lettered. The API carried
`active_work_notified_at` (empty) and nothing about the dead letter, and the
card showed nothing at all.

`active_work_delivery` now answers from the durable records, on a real
database through the real `list_tickets`:

* delivered -- a send is traced for this owner at this stage;
* failed    -- the notice was dead-lettered, with its reason;
* pending   -- the notice is queued and not delivered, with its last error;
* none      -- the ticket has an owner and no notice is recorded.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def same_instant(value: str, utc: str) -> bool:
    """The board formats in local time; compare the instant, not the text."""
    return bool(value) and datetime.fromisoformat(value) == datetime.fromisoformat(utc)


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def main() -> int:
    with temporary_cluster(prefix="syrd264-delivery-", shutdown="immediate") as cluster:
        db = "syrd264_delivery"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        # One implementation ticket per implementer, so each is its owner's
        # single current ticket and is highlighted.
        for ticket_id, role in (("PGU-1", "ops"), ("PGU-2", "main"), ("PGU-3", "app"), ("PGU-4", "research")):
            t.seed_postgres_ticket(admin, ticket_id, title=f"{role} work", state="in_progress", assignee=role)
        t.psql(admin, """
-- PGU-1: MEFP-1's shape. The one notice was claimed and dead-lettered.
INSERT INTO ticket_board.ticket_notification_queue
    (ticket_id, kind, target_role, message, payload, dedupe_key, attempts, dead_lettered_at,
     terminal_reason, last_error)
VALUES ('PGU-1', 'transition', 'ops', 'PGU-1 is yours',
        jsonb_build_object('kind', 'transition', 'id', 'PGU-1', 'new_state', 'in_progress', 'target_role', 'ops'),
        'syrd264:PGU-1', 1, '2026-09-24T23:25:13+00:00', 'tmux_target_missing', 'tmux_target_missing');
-- PGU-2: still queued after a routing failure, waiting for its next try.
INSERT INTO ticket_board.ticket_notification_queue
    (ticket_id, kind, target_role, message, payload, dedupe_key, attempts, last_error, next_attempt_at)
VALUES ('PGU-2', 'transition', 'main', 'PGU-2 is yours',
        jsonb_build_object('kind', 'transition', 'id', 'PGU-2', 'new_state', 'in_progress', 'target_role', 'main'),
        'syrd264:PGU-2', 3, 'runtime_assignment_unresolved', '2026-09-24T23:30:00+00:00');
-- PGU-3: delivered.
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
VALUES ('2026-09-24T23:26:00+00:00', 'PGU-3', 'app', 'transition', 'send', 'in_progress', 'app', 'idle', 'idle');
-- PGU-4: an owner, and no notice recorded at all.
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-4';
""")
        (cluster.root / "frames").mkdir(exist_ok=True)
        (cluster.root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        tickets, errors = app.list_tickets()
        check(errors == [], f"{errors}")
        by_id = {ticket["id"]: ticket for ticket in tickets}
        # Read what the board actually holds for PGU-4 rather than assume it.
        leftover = t.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-4';")

        failed = by_id["PGU-1"]
        check(failed["active_work_highlight"] is True, f"MEFP-1's shape is highlighted: {failed}")
        check(failed["active_work_notified_at"] == "", "and was never sent")
        delivery = failed["active_work_delivery"]
        check(delivery["state"] == "failed", f"and now says it failed: {delivery}")
        check(delivery["reason"] == "tmux_target_missing", f"with why: {delivery}")
        check(same_instant(delivery["at"], "2026-09-24T23:25:13+00:00"), f"and when: {delivery}")

        pending = by_id["PGU-2"]["active_work_delivery"]
        check(pending["state"] == "pending", f"a queued notice is pending: {pending}")
        check(pending["attempts"] == 3 and pending["reason"] == "runtime_assignment_unresolved",
              f"with its attempts and last error: {pending}")
        check(same_instant(pending["next_attempt_at"], "2026-09-24T23:30:00+00:00"), f"and its next try: {pending}")

        delivered = by_id["PGU-3"]["active_work_delivery"]
        check(delivered["state"] == "delivered", f"a traced send is delivered: {delivered}")
        check(same_instant(delivered["at"], "2026-09-24T23:26:00+00:00"), f"at the send: {delivered}")

        check(leftover.strip() == "0", f"PGU-4 has no notice queued: {leftover}")
        none = by_id["PGU-4"]["active_work_delivery"]
        check(none["state"] == "none", f"an owner with no notice recorded says so: {none}")

        # A notice for an EARLIER stage is not this stage's delivery.
        t.psql(admin, """
UPDATE ticket_board.ticket_notification_queue
SET payload = jsonb_set(payload, '{new_state}', '"analysis"')
WHERE ticket_id = 'PGU-2';
""")
        tickets, _errors = app.list_tickets()
        stale = {ticket["id"]: ticket for ticket in tickets}["PGU-2"]["active_work_delivery"]
        check(stale["state"] != "pending", f"a notice for another stage is not reported for this one: {stale}")

        # A ticket with no current owner has nothing to report.
        t.seed_postgres_ticket(admin, "PGU-5", title="unowned", state="backlog", assignee="unassigned")
        tickets, _errors = app.list_tickets()
        ownerless = {ticket["id"]: ticket for ticket in tickets}["PGU-5"]["active_work_delivery"]
        check(ownerless == {"state": ""}, f"an ownerless ticket reports nothing: {ownerless}")

        # The single-ticket read carries it too.
        single = app.get_ticket("PGU-1")
        check(single["active_work_delivery"]["state"] == "failed", f"get_ticket agrees: {single['active_work_delivery']}")

    print(f"active_work_delivery_state_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
