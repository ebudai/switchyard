#!/usr/bin/env python3
"""SYRD-225: a DAT kickback is a fresh handoff, and the board records it as one.

Live on SYRD-221. A Director DAT kickback returned the ticket to ops, and the
notice it queued carried `updated_at` from ops's own EARLIER submission. So did
the notification state's `entered_current_state_at` and `last_activity_at`. The
cause: `perform_workflow_action_as` moved state and assignee without touching
the ticket's activity time, and every trigger on that update reads
`NEW.updated_at` as the moment the change happened.

The board then reported the handoff as delivered on the strength of a send from
ops's PREVIOUS stint in Implementation: `active_work_notified_at` is the latest
transition send matching ticket, role and state, and a kickback returns a ticket
to the same role in the same state it left.

Both are covered here on a real cluster, and the first on both bodies a board
can be running: a fresh board installs schema.sql's copy of the function, and an
upgraded one installs the newest migration's.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import schema_function_drift as drift  # noqa: E402
import ticket_board_write_api_test as fixture  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
FIX = MIGRATIONS / "pgu954_syrd225_transition_activity_time.sql"

CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def body_from(migration: Path, name: str) -> str:
    text = migration.read_text()
    start = text.index(f"CREATE OR REPLACE FUNCTION ticket_board.{name}(")
    return text[start:text.index("$$;", start) + 3]


def previous_owner(name: str) -> Path:
    """The migration an upgraded board ran this function from BEFORE the fix."""
    needle = f"CREATE OR REPLACE FUNCTION ticket_board.{name}("
    owners = sorted(p for p in MIGRATIONS.glob("*.sql") if needle in p.read_text() and p != FIX)
    return owners[-1]


def main() -> int:
    # Fresh and upgraded boards run the same function, or one of them is
    # running something nobody tested.
    drift.assert_no_drift("perform_workflow_action_as")
    check(drift.owning_migration("perform_workflow_action_as") == FIX,
          f"the fix is not what an upgraded board ends up running: "
          f"{drift.owning_migration('perform_workflow_action_as').name}")

    with temporary_cluster(prefix="syrd225-kickback.", shutdown="immediate") as cluster:
        db = "syrd225_kickback"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, db)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
                     "-U", "postgres", db])
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, fixture.RBAC_PATH.read_text())
        app = fixture.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
            database_url=fixture.conninfo(cluster.socket_dir, cluster.port, db, fixture.SERVICE_ROLE),
        )
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        # Three tickets an implementer submitted and review carried to DAT, the
        # submission being what last set updated_at -- twelve minutes before
        # the Director sends each one back. One per implementer: the board
        # focuses an implementer on one ticket at a time, so a second returned
        # to the same role queues behind the first instead of arriving, and
        # would test the queue rather than the handoff. Seeded before the
        # workflow exists, because afterwards only a transition may move one.
        owners = {"PGU-221": "ops", "PGU-222": "app", "PGU-223": "main"}
        tickets = tuple(owners)
        for ticket in tickets:
            fixture.seed_postgres_ticket(admin, ticket, title=f"Fresh launch {ticket}", state="dat",
                                         assignee="director", audit_signoff=True, commit_hash="abc1234")
        sql("""
UPDATE ticket_board.tickets
   SET updated_at = clock_timestamp() - interval '12 minutes',
       updated_text = ticket_board.utc_text(clock_timestamp() - interval '12 minutes');
DELETE FROM ticket_board.ticket_notification_queue;""")
        for ticket, owner in owners.items():
            sql(f"UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee = '{owner}' "
                f"WHERE ticket_id = '{ticket}';")
        submitted = {t: sql(f"SELECT extract(epoch FROM updated_at) FROM ticket_board.tickets WHERE id='{t}';")
                     for t in tickets}

        cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        def kick_back(ticket: str) -> float:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/director_dat_kick_back",
                              {"text": "DAT rejects: fix the three findings."}, caller="director")
            return float(sql("SELECT extract(epoch FROM clock_timestamp());"))

        def recorded(ticket: str) -> dict[str, float]:
            row = json.loads(sql(f"""
SELECT jsonb_build_object(
  'state', t.state, 'assignee', t.assignee,
  'updated_at', extract(epoch FROM t.updated_at),
  'entered_current_state_at', extract(epoch FROM s.entered_current_state_at),
  'last_activity_at', extract(epoch FROM s.last_activity_at),
  'payload_updated_at', (SELECT extract(epoch FROM (q.payload->>'updated_at')::timestamptz)
                           FROM ticket_board.ticket_notification_queue q
                          WHERE q.ticket_id = t.id AND q.kind = 'transition'
                            AND q.target_role = t.assignee ORDER BY q.id DESC LIMIT 1))::text
FROM ticket_board.tickets t JOIN ticket_board.ticket_notification_state s ON s.ticket_id = t.id
WHERE t.id = '{ticket}';"""))
            return row

        def assert_fresh(ticket: str, kicked: float, board: str) -> None:
            row = recorded(ticket)
            check(row["state"] == "in_progress" and row["assignee"] == owners[ticket],
                  f"{board}: the kickback did not return {ticket} to {owners[ticket]}: {row}")
            before = float(submitted[ticket])
            for field in ("updated_at", "entered_current_state_at", "last_activity_at", "payload_updated_at"):
                value = row[field]
                check(value is not None, f"{board}: {field} is missing for {ticket}: {row}")
                # Within a few seconds of the kickback, and nowhere near the
                # submission twelve minutes earlier.
                check(abs(value - kicked) < 5.0,
                      f"{board}: {field} for {ticket} is not the kickback's time: "
                      f"{value - kicked:+.1f}s from it, {value - before:+.1f}s from the submission")

        # 1. A FRESH BOARD, running schema.sql's copy.
        assert_fresh("PGU-223", kick_back("PGU-223"), "fresh board")

        # 2. THE CONTROL. A board upgraded only as far as the migration before
        #    the fix. If this does not reproduce the stale reading, nothing
        #    below says anything about the migration.
        old = previous_owner("perform_workflow_action_as")
        sql(body_from(old, "perform_workflow_action_as"))
        kicked = kick_back("PGU-221")
        stale = recorded("PGU-221")
        check(abs(stale["payload_updated_at"] - float(submitted["PGU-221"])) < 1.0,
              f"the body from {old.name} did not reproduce the stale kickback, so this "
              f"case cannot show the migration fixing it: {stale}")
        check(kicked - stale["entered_current_state_at"] > 600,
              f"the control's entry time is not the submission's: {stale}")

        # 3. THE UPGRADE. Apply the migration exactly as an upgrade would.
        sql(FIX.read_text())
        assert_fresh("PGU-222", kick_back("PGU-222"), "upgraded board")

        # 4. THE GENERATION. A send from ops's earlier stint in Implementation
        #    is not this handoff's delivery.
        entered = sql("SELECT entered_current_state_at FROM ticket_board.ticket_notification_state "
                      "WHERE ticket_id = 'PGU-222';")
        sql(f"""
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, notification_id, target_role, kind, event, ticket_state_at_event)
VALUES ('{entered}'::timestamptz - interval '40 minutes', 'PGU-222', 1, 'app', 'transition',
        'send', 'in_progress');""")
        earlier = app.get_ticket("PGU-222").get("active_work_notified_at")
        check(not earlier,
              f"a send from the previous stint was reported as this handoff's delivery: {earlier}")

        sql(f"""
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, notification_id, target_role, kind, event, ticket_state_at_event)
VALUES ('{entered}'::timestamptz + interval '3 seconds', 'PGU-222', 2, 'app', 'transition',
        'send', 'in_progress');""")
        current = app.get_ticket("PGU-222").get("active_work_notified_at")
        check(current, "a send in this stint is not reported as delivered")

        # And an unconfirmed send is not a send.
        sql(f"""
INSERT INTO ticket_board.notification_trace
    (ts, ticket_id, notification_id, target_role, kind, event, ticket_state_at_event)
VALUES ('{entered}'::timestamptz + interval '1 second', 'PGU-223', 3, 'main', 'transition',
        'send_unconfirmed', 'in_progress');""")
        unconfirmed = app.get_ticket("PGU-223").get("active_work_notified_at")
        check(not unconfirmed,
              f"a send nobody could show arrived is reported as delivered: {unconfirmed}")

        server.shutdown()

    print(f"ticket_board_kickback_handoff_postgres_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
