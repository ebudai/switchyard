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
from datetime import datetime, timedelta
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
        # Every event is dated AFTER its ticket entered the stage: a record
        # is only evidence for the visit it belongs to (SYRD-264 Final Sign-Off).
        t.psql(admin, """
CREATE TEMP TABLE entry AS
SELECT ticket_id, entered_current_state_at AS at FROM ticket_board.ticket_notification_state;
-- PGU-1: MEFP-1's shape. The one notice was claimed and dead-lettered.
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ('PGU-1', 'PGU-2', 'PGU-3', 'PGU-4');
INSERT INTO ticket_board.ticket_notification_queue
    (ticket_id, kind, target_role, message, payload, dedupe_key, attempts, dead_lettered_at,
     terminal_reason, last_error, created_at)
SELECT 'PGU-1', 'transition', 'ops', 'PGU-1 is yours',
       jsonb_build_object('kind', 'transition', 'id', 'PGU-1', 'new_state', 'in_progress', 'target_role', 'ops'),
       'syrd264:PGU-1', 1, at + interval '28 seconds', 'tmux_target_missing', 'tmux_target_missing',
       at + interval '1 second'
FROM entry WHERE ticket_id = 'PGU-1';
-- PGU-2: still queued after a routing failure, waiting for its next try.
INSERT INTO ticket_board.ticket_notification_queue
    (ticket_id, kind, target_role, message, payload, dedupe_key, attempts, last_error, next_attempt_at, created_at)
SELECT 'PGU-2', 'transition', 'main', 'PGU-2 is yours',
       jsonb_build_object('kind', 'transition', 'id', 'PGU-2', 'new_state', 'in_progress', 'target_role', 'main'),
       'syrd264:PGU-2', 3, 'runtime_assignment_unresolved', at + interval '5 minutes', at + interval '1 second'
FROM entry WHERE ticket_id = 'PGU-2';
-- PGU-3: delivered.
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
SELECT at + interval '2 seconds', 'PGU-3', 'app', 'transition', 'send', 'in_progress', 'app', 'idle', 'idle'
FROM entry WHERE ticket_id = 'PGU-3';
""")
        entered = {
            line.split("|")[0]: line.split("|")[1]
            for line in t.psql(admin, "SELECT ticket_id, to_char(entered_current_state_at AT TIME ZONE 'UTC', "
                                      "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"') "
                                      "FROM ticket_board.ticket_notification_state;").splitlines()
        }

        def after(ticket_id: str, seconds: float) -> str:
            base = datetime.fromisoformat(entered[ticket_id])
            return (base + timedelta(seconds=seconds)).isoformat()
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
        check(same_instant(delivery["at"], after("PGU-1", 28)), f"and when: {delivery}")

        pending = by_id["PGU-2"]["active_work_delivery"]
        check(pending["state"] == "pending", f"a queued notice is pending: {pending}")
        check(pending["attempts"] == 3 and pending["reason"] == "runtime_assignment_unresolved",
              f"with its attempts and last error: {pending}")
        check(same_instant(pending["next_attempt_at"], after("PGU-2", 300)), f"and its next try: {pending}")

        delivered = by_id["PGU-3"]["active_work_delivery"]
        check(delivered["state"] == "delivered", f"a traced send is delivered: {delivered}")
        check(same_instant(delivered["at"], after("PGU-3", 2)), f"at the send: {delivered}")

        check(leftover.strip() == "0", f"PGU-4 has no notice queued: {leftover}")
        none = by_id["PGU-4"]["active_work_delivery"]
        check(none["state"] == "none", f"an owner with no notice recorded says so: {none}")

        # A dead letter left from an EARLIER visit to this stage is not this
        # visit's failure: with nothing queued now, the answer is still "none".
        t.psql(admin, """
INSERT INTO ticket_board.ticket_notification_queue
    (ticket_id, kind, target_role, message, payload, dedupe_key, attempts, dead_lettered_at,
     terminal_reason, created_at)
SELECT 'PGU-4', 'transition', 'research', 'old visit',
       jsonb_build_object('kind', 'transition', 'id', 'PGU-4', 'new_state', 'in_progress', 'target_role', 'research'),
       'syrd264:PGU-4:earlier-visit', 1, entered_current_state_at - interval '1 hour', 'tmux_target_missing',
       entered_current_state_at - interval '2 hours'
FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-4';
""")
        earlier = app.get_ticket("PGU-4")["active_work_delivery"]
        check(earlier["state"] == "none", f"an earlier visit's dead letter is not reported now: {earlier}")

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

        # The Director's case: sent to an implementer, back to analysis, back
        # again. The FIRST visit's send must not stand in for the second visit's
        # notice. `perf`, because every other implementer already holds its one
        # serial slot above and a second ticket would only be queued behind it.
        t.seed_postgres_ticket(admin, "PGU-6", title="two visits", state="analysis", assignee="director")
        app.route_ticket("PGU-6", "in_progress", "perf", caller_role="director")
        landed = app.get_ticket("PGU-6")
        check((landed["state"], landed["assignee"]) == ("in_progress", "perf"), f"the route landed: {landed['state']}/{landed['assignee']}")

        def acknowledge_first_visit_send() -> None:
            # What the listener does on a delivered send: trace it, remove the row.
            t.psql(admin, """
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
VALUES (clock_timestamp(), 'PGU-6', 'perf', 'transition', 'send', 'in_progress', 'perf', 'idle', 'idle');
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-6';
""")

        acknowledge_first_visit_send()
        first = app.get_ticket("PGU-6")["active_work_delivery"]
        check(first["state"] == "delivered", f"the first visit was delivered: {first}")

        app.route_ticket("PGU-6", "analysis", "director", caller_role="director")
        app.route_ticket("PGU-6", "in_progress", "perf", caller_role="director")
        queued = t.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue "
                               "WHERE ticket_id='PGU-6' AND target_role='perf';")
        check(queued.strip() == "1", f"the second visit enqueued its own notice: {queued}")

        def delivery_now() -> tuple[dict, dict]:
            listed = {ticket["id"]: ticket for ticket in app.list_tickets()[0]}["PGU-6"]["active_work_delivery"]
            return listed, app.get_ticket("PGU-6")["active_work_delivery"]

        listed, single = delivery_now()
        check(listed["state"] == "pending" and single["state"] == "pending",
              f"the second visit is pending, not the first visit's delivery: {listed} / {single}")
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET dead_lettered_at = clock_timestamp(), "
                      "terminal_reason = 'tmux_target_missing' WHERE ticket_id = 'PGU-6';")
        listed, single = delivery_now()
        check(listed["state"] == "failed" and single["state"] == "failed",
              f"and once dead-lettered it says failed, through both reads: {listed} / {single}")
        check(listed["reason"] == "tmux_target_missing", f"{listed}")

        # SYRD-268: sent, and the recipient's hooks recorded no turn. That is
        # not delivery, and it is reported as exactly what it is.
        t.seed_postgres_ticket(admin, "PGU-7", title="final sign-off", state="director_review",
                               assignee="director")
        t.psql(admin, """
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-7';
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
VALUES (clock_timestamp(), 'PGU-7', 'director', 'transition', 'send_unconfirmed', 'director_review',
        'director', 'idle', 'no_submission_witnessed');
""")
        unconfirmed = app.get_ticket("PGU-7")
        check(unconfirmed["active_work_notified_at"] == "",
              f"an unconfirmed send is not a notification time: {unconfirmed['active_work_notified_at']}")
        state = unconfirmed["active_work_delivery"]
        check(state["state"] == "unconfirmed" and state["reason"] == "no_submission_witnessed",
              f"it is reported unconfirmed, not delivered: {state}")
        listed = {ticket["id"]: ticket for ticket in app.list_tickets()[0]}["PGU-7"]["active_work_delivery"]
        check(listed == state, f"through both reads: {listed}")
        # "Cannot tell" is unconfirmed too, never delivered: a later attempt
        # that found no hook state to read is reported with its own reason.
        t.psql(admin, """
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
VALUES (clock_timestamp(), 'PGU-7', 'director', 'transition', 'send_unconfirmed', 'director_review',
        'director', 'idle', 'no_hook_state');
""")
        for read in (app.get_ticket("PGU-7"),
                     {ticket["id"]: ticket for ticket in app.list_tickets()[0]}["PGU-7"]):
            unknown = read["active_work_delivery"]
            check(unknown["state"] == "unconfirmed" and unknown["reason"] == "no_hook_state",
                  f"no hook state is unconfirmed, with the latest attempt's reason: {unknown}")
            check(read["active_work_notified_at"] == "", "and is not a notification time either")
        # A later witnessed send is delivery.
        t.psql(admin, """
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event,
     pane_busy_determination, busy_reason)
VALUES (clock_timestamp(), 'PGU-7', 'director', 'transition', 'send', 'director_review', 'director',
        'idle', 'idle');
""")
        check(app.get_ticket("PGU-7")["active_work_delivery"]["state"] == "delivered",
              "and a witnessed send afterwards is delivered")

        # The single-ticket read carries it too.
        single = app.get_ticket("PGU-1")
        check(single["active_work_delivery"]["state"] == "failed", f"get_ticket agrees: {single['active_work_delivery']}")

    print(f"active_work_delivery_state_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
