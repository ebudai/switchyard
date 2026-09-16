#!/usr/bin/env python3
"""SYRD-159: one change, one wake -- and one wake per role, not per transaction.

A single user-visible change is often written as two statements: set the
blockers, then say why. Each one calls notify_ticket_owner_in_place_change, and
the dedupe key was `ticket_update:<ticket>:<pg_current_xact_id()>`, so the two
writes produced two keys, two rows, and two wakes for one thing to read.
notification_trace on SYRD-146 shows exactly that: two ticket_update enqueues
for `ops` 87 milliseconds apart, same ticket, same role, same stage, different
notification ids -- which can only mean two different dedupe keys, because the
column is UNIQUE with ON CONFLICT DO UPDATE.

The key now names what the wake is about rather than the transaction that raised
it, so the queue's existing collapse does the work: a pending wake absorbs later
changes and carries the newest description, and a delivered wake is deleted by
ack_notification, so the next change mints a fresh one. Nothing is swallowed;
what is removed is the second wake for something the role has not read yet.

Against a real cluster, and through the real HTTP API where the defect appeared,
because the claim is about what the database does with two transactions -- which
a fake connection cannot have an opinion about. The board carries this project's
own workflow document so stage owners and transitions are the live ones.

The awaiting_role reminder ladder is deliberately NOT touched: its rungs differ
by a trailing :N on purpose, escalate on a schedule, and target the director.
One case here holds that ladder in place.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import ticket_board_write_api_test as fixture  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def main() -> int:
    with temporary_cluster(prefix="syrd159-dedupe.", shutdown="immediate") as cluster:
        dbname = "ticket_update_dedupe_test"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
                     "-U", "postgres", dbname])
        fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, fixture.RBAC_PATH.read_text())

        app = fixture.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets",
            project="pgu", ticket_prefix="PGU",
            database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE),
        )
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}",
                                     payload, caller=actor)

        def sql(query: str) -> str:
            return fixture.psql(admin, query).strip()

        def wakes(ticket: str) -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object("
                "'id', id, 'role', target_role, 'key', dedupe_key, 'message', message,"
                "'summary', payload->>'change_summary', 'attempts', attempts,"
                "'claimed', claimed_at IS NOT NULL) ORDER BY id), '[]')::text "
                f"FROM ticket_board.ticket_notification_queue "
                f"WHERE ticket_id='{ticket}' AND kind='ticket_update'"))

        def clear(ticket: str) -> None:
            fixture.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}';")

        try:
            cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

            fixture.seed_postgres_ticket(admin, "PGU-1", title="Owned by ops",
                                         state="in_progress", assignee="ops", commit_exempt=True)
            fixture.seed_postgres_ticket(admin, "PGU-2", title="A blocker",
                                         state="analysis", assignee="director", commit_exempt=True)

            # 1. SYRD-146'S SHAPE. One change, written as two statements the way
            #    the board's own API writes it: set the blockers, then say why.
            clear("PGU-1")
            act("PGU-1", "set_blockers", "director", blocked_by=["PGU-2"], blocked_reason="waiting on PGU-2")
            after_first = wakes("PGU-1")
            act("PGU-1", "add_comment", "director", text="Blocked on PGU-2, see above.")
            after_second = wakes("PGU-1")
            check(len(after_first) == 1, f"the first write wakes ops once: {after_first}")
            check(len(after_second) == 1,
                  f"and the second write does not wake them again for the same unread ticket: {after_second}")
            check(after_second[0]["id"] == after_first[0]["id"],
                  f"it is the same row, refreshed rather than a second one: {after_second}")
            check("new comment" in after_second[0]["message"],
                  f"carrying the newest description of what changed: {after_second[0]['message']}")
            check(after_second[0]["summary"] == "new comment",
                  f"in the payload the listener actually delivers, not only the message text: {after_second[0]}")
            check(after_second[0]["claimed"] is False,
                  f"and re-armed, so a wake already claimed for delivery is not left stale: {after_second[0]}")
            check(after_second[0]["role"] == "ops", f"still addressed to the assignee: {after_second}")

            # 3. NOTHING IS SWALLOWED. Once the wake is delivered the row is
            #    gone, so the next change mints a new one -- a collapsed wake is
            #    an unread wake, never a missed change.
            delivered = sql("SELECT id FROM ticket_board.ticket_notification_queue "
                            "WHERE ticket_id='PGU-1' AND kind='ticket_update'")
            # Acknowledged as the listener, because that is the only role the
            # board lets reconcile a notification -- acking as anyone else would
            # be testing a path production does not have.
            fixture.psql(admin, "SET ROLE ticket_board_listener;\n"
                                f"SELECT ticket_board.ack_notification({delivered});\nRESET ROLE;")
            check(wakes("PGU-1") == [], "an acknowledged wake leaves the queue")
            act("PGU-1", "add_comment", "director", text="A later, separate change.")
            after_ack = wakes("PGU-1")
            check(len(after_ack) == 1 and after_ack[0]["id"] != int(delivered),
                  f"and the next change raises a fresh wake: {after_ack}")

            # 4. TWO ROLES ARE TWO WAKES. Collapsing across roles would hand one
            #    role's notification to another; the role is in the key so it
            #    cannot happen, including inside a single transaction.
            clear("PGU-1")
            fixture.psql(admin, """
DO $$
BEGIN
    PERFORM set_config('ticket_board.caller_role', 'director', true);
    PERFORM ticket_board.notify_ticket_owner_in_place_change('PGU-1', 'blockers');
    UPDATE ticket_board.tickets SET assignee = 'app' WHERE id = 'PGU-1';
    PERFORM ticket_board.notify_ticket_owner_in_place_change('PGU-1', 'assignee');
END $$;""")
            both = wakes("PGU-1")
            check(len(both) == 2, f"one transaction, two roles, two wakes: {both}")
            check({row["role"] for row in both} == {"ops", "app"},
                  f"each addressed to its own role: {both}")
            check(len({row["key"] for row in both}) == 2,
                  f"because the keys differ by role: {both}")

            # 5. THE AWAITING-ROLE LADDER IS NOT TOUCHED. Its rungs are supposed
            #    to be several rows with keys differing by a trailing :N, on a
            #    schedule, addressed to the director. This case fails if the
            #    collapse is ever widened to reach them.
            fixture.psql(admin, """
DO $$
BEGIN
    PERFORM set_config('ticket_board.caller_role', 'director', true);
    UPDATE ticket_board.tickets SET assignee = 'ops' WHERE id = 'PGU-1';
END $$;""")
            # The blocker from case 1 has to go first: a blocked ticket cannot
            # create an awaiting-role handoff at all (SYRD-148), which would
            # make this case fail for a reason that has nothing to do with it.
            act("PGU-1", "set_blockers", "director", blocked_by=[], blocked_reason="")
            clear("PGU-1")
            act("PGU-1", "await_role", "ops", role="director")
            ladder = json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('key', dedupe_key, 'role', target_role)"
                " ORDER BY dedupe_key), '[]')::text "
                "FROM ticket_board.ticket_notification_queue "
                "WHERE ticket_id='PGU-1' AND kind='awaiting_role'"))
            check(len(ladder) > 1, f"the reminder ladder still has its rungs: {ladder}")
            check(all(row["role"] == "director" for row in ladder),
                  f"still addressed to the director, not the assignee: {ladder}")
            check(len({row["key"] for row in ladder}) == len(ladder),
                  f"each rung keeps its own key: {ladder}")
            check(all(row["key"].rsplit(":", 1)[-1].isdigit() for row in ladder),
                  f"differing by the trailing rung number, as documented: {ladder}")

            # 6. TWO TICKETS ARE TWO WAKES, for the same role. Collapsing per
            #    role alone would hide a change on one ticket behind an unread
            #    change on another -- a worse bug than the one being fixed.
            #    Both fixtures sit in audit so they resolve to one target role
            #    without serial focus redirecting either of them.
            fixture.seed_postgres_ticket(admin, "PGU-3", title="One for audit",
                                         state="audit", assignee="audit", commit_exempt=True)
            fixture.seed_postgres_ticket(admin, "PGU-4", title="Another for audit",
                                         state="audit", assignee="audit", commit_exempt=True)
            clear("PGU-3"); clear("PGU-4")
            fixture.psql(admin, """
DO $$
BEGIN
    PERFORM set_config('ticket_board.caller_role', 'director', true);
    PERFORM ticket_board.notify_ticket_owner_in_place_change('PGU-3', 'new comment');
    PERFORM ticket_board.notify_ticket_owner_in_place_change('PGU-4', 'new comment');
END $$;""")
            three, four = wakes("PGU-3"), wakes("PGU-4")
            check(len(three) == 1 and len(four) == 1,
                  f"each ticket keeps its own wake: {three} {four}")
            check(three[0]["role"] == "audit" and four[0]["role"] == "audit",
                  f"both addressed to the same role, which is the point: {three} {four}")
            check(three[0]["key"] != four[0]["key"],
                  f"because the ticket is in the key too: {three[0]['key']} {four[0]['key']}")

            # 7. AND THE KEY ITSELF, written out. Everything above is about
            #    behaviour and would hold for any key with the same identity;
            #    this states the identity the fix chose, so a future reader sees
            #    it without reconstructing it from five cases.
            check(after_second[0]["key"] == "ticket_update:PGU-1:ops",
                  f"the key is the ticket and the role, not the transaction: {after_second[0]['key']}")

            print(f"ticket_board_ticket_update_dedupe_postgres_test: {CHECKS} checks ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
