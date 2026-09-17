#!/usr/bin/env python3
"""SYRD-194: an owner that stops without resolving is reported at the boundary.

SYRD-133 gave the Director an escalation and SYRD-163 gave it patience. Neither
covers the case SYRD-193 reproduced: App printed a Director question, returned
to the prompt, and touched nothing -- and `notify_idle_turn_end_nudges` can only
reach the Director through `idle_reminder_count >= 1`, so the owner has to be
nudged and ignore it first. Nothing was delivered until a human read the pane.

The guard here is a sibling, not a loosening: every suppression SYRD-163 added
still governs reminders and escalations, untouched. What holds this one back is
not elapsed time but a LEASE the owner takes deliberately, scoped to one
completed turn and consumed when the next begins -- so "I am continuing" is a
statement about the next turn and cannot become a way to stall.

Driven against a real cluster with the real migration applied.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from temporary_cluster import temporary_cluster

MIGRATION = (
    ROOT / "scripts" / "ticket_board" / "migrations" / "pgu948_syrd194_unresolved_turn_guard.sql"
).read_text()

LISTENER = "ticket_board_listener"


def guard(admin: str, *, role: str = "app", turn: str = "turn-1", now: str = "clock_timestamp()") -> int:
    out = t.psql(
        admin,
        "SET ROLE " + LISTENER + ";\n"
        f"SELECT ticket_board.notify_unresolved_turn_end('{{\"{role}\": \"{turn}\"}}'::jsonb, {now});",
    )
    return int(out.strip().splitlines()[-1])


def queued(admin: str, ticket: str) -> list[str]:
    out = t.psql(
        admin,
        "SELECT kind || '|' || target_role FROM ticket_board.ticket_notification_queue "
        f"WHERE ticket_id = '{ticket}' ORDER BY id;",
    )
    return [line for line in out.strip().splitlines() if line.strip()]


def drain(admin: str) -> None:
    t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="unresolved-turn-", shutdown="immediate") as cluster:
        db = "unresolved_turn_guard_test"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.psql(admin, MIGRATION)
        t.psql(
            admin,
            f"GRANT EXECUTE ON FUNCTION ticket_board.notify_unresolved_turn_end(jsonb, timestamptz) TO {LISTENER};"
            f"GRANT EXECUTE ON FUNCTION ticket_board.consume_turn_continuation(text, text) TO {LISTENER};"
            f"GRANT SELECT ON ticket_board.turn_continuation_lease TO {LISTENER};",
        )
        t.seed_postgres_ticket(admin, "PGU-1", title="App work", state="in_progress", assignee="app")
        drain(admin)

        # 1. The regression: an unresolved turn end reaches the Director at once,
        #    with no prior idle reminder and no waiting.
        assert guard(admin) == 1
        # Two audiences, one event: the Director is told, and the owner gets its
        # single repair prompt. The Director's copy does not depend on the owner
        # ever reading theirs, which is the property SYRD-193 was missing.
        assert queued(admin, "PGU-1") == [
            "unresolved_turn|director",
            "unresolved_turn_repair|app",
        ], queued(admin, "PGU-1")
        checks += 2

        # 2. The same turn, reported again, stays silent -- and stays silent even
        #    after the queue row is gone, which is what "acknowledgement does not
        #    cause repeats" means.
        assert guard(admin) == 0
        drain(admin)
        assert guard(admin) == 0
        assert queued(admin, "PGU-1") == []
        checks += 3

        # 3. A FRESH turn identity re-arms the guard.
        assert guard(admin, turn="turn-2") == 1
        assert queued(admin, "PGU-1") == [
            "unresolved_turn|director",
            "unresolved_turn_repair|app",
        ]
        drain(admin)
        checks += 2

        # 4. A lease taken for a turn buys silence for exactly that turn.
        t.psql(
            admin,
            "SET ROLE " + t.SERVICE_ROLE + ";\n"
            "SET ticket_board.caller_role = 'app';\n"
            "SELECT ticket_board.grant_turn_continuation('PGU-1', 'turn-3', 'Still building the guard.');",
        )
        assert guard(admin, turn="turn-3") == 0
        assert queued(admin, "PGU-1") == []
        checks += 2

        # ... and not for any other turn. The next boundary is a new promise.
        assert guard(admin, turn="turn-4") == 1
        drain(admin)
        checks += 1

        # 5. A lease that is never followed by a turn EXPIRES, and an expired
        #    lease buys nothing. This is what stops continuation stalling.
        t.psql(
            admin,
            "UPDATE ticket_board.turn_continuation_lease "
            "SET expires_at = clock_timestamp() - interval '1 minute' WHERE turn_id = 'turn-3';",
        )
        assert guard(admin, turn="turn-3") == 1, "an expired lease must not suppress"
        drain(admin)
        checks += 1

        # 6. A new turn consumes the previous lease, so it cannot be reused.
        t.psql(
            admin,
            "SET ROLE " + t.SERVICE_ROLE + ";\n"
            "SET ticket_board.caller_role = 'app';\n"
            "SELECT ticket_board.grant_turn_continuation('PGU-1', 'turn-5', 'One more turn.');",
        )
        # A new turn supersedes EVERY outstanding promise for that owner, not
        # just the newest: each one was a promise about a next turn, and the
        # next turn has now arrived. (This returned 2 the first time it ran --
        # turn-3's expired lease was still unconsumed -- and 2 is right.)
        consumed = int(
            t.psql(
                admin,
                "SET ROLE " + LISTENER + ";\n"
                "SELECT ticket_board.consume_turn_continuation('app', 'turn-6');",
            ).strip().splitlines()[-1]
        )
        assert consumed >= 1, consumed
        assert t.psql(
            admin,
            "SELECT consumed_at IS NOT NULL FROM ticket_board.turn_continuation_lease "
            "WHERE turn_id = 'turn-5';",
        ).strip().splitlines()[-1] == "t"
        assert guard(admin, turn="turn-5") == 1, "a consumed lease must not suppress"
        drain(admin)
        checks += 2

        # 7. Every legitimate resolution stays silent. awaiting_role and
        #    manual control are mutations on the live ticket; terminal and
        #    unassigned get tickets of their own, because the board enforces
        #    legal transitions even on a direct UPDATE and will not let a
        #    fixture jump in_progress -> done.
        for label, setup, teardown in (
            (
                "awaiting_role",
                "UPDATE ticket_board.ticket_notification_state SET awaiting_role='director', "
                "awaiting_since_at=clock_timestamp() WHERE ticket_id='PGU-1';",
                "UPDATE ticket_board.ticket_notification_state SET awaiting_role='', "
                "awaiting_since_at=NULL WHERE ticket_id='PGU-1';",
            ),
            (
                "manually_controlled",
                "UPDATE ticket_board.tickets SET manually_controlled=true WHERE id='PGU-1';",
                "UPDATE ticket_board.tickets SET manually_controlled=false WHERE id='PGU-1';",
            ),
        ):
            t.psql(admin, setup)
            assert guard(admin, turn=f"quiet-{label}") == 0, label
            assert queued(admin, "PGU-1") == [], label
            t.psql(admin, teardown)
            checks += 2

        # Terminal and unassigned work, owned by nobody this guard can report to.
        t.seed_postgres_ticket(admin, "PGU-3", title="Finished", state="done", assignee="unassigned")
        t.seed_postgres_ticket(admin, "PGU-4", title="Unowned", state="in_progress", assignee="unassigned")
        drain(admin)
        assert guard(admin, turn="quiet-terminal") == 1, "only PGU-1 should report"
        assert queued(admin, "PGU-3") == [], "a terminal ticket is not an unresolved turn"
        assert queued(admin, "PGU-4") == [], "unassigned work has no owner to report"
        drain(admin)
        checks += 3

        # A blocker is a durable reason to be stopped, so it is silent too.
        t.seed_postgres_ticket(admin, "PGU-2", title="Blocker", state="in_progress", assignee="main")
        t.psql(
            admin,
            "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position) "
            "VALUES ('PGU-1', 'PGU-2', 0) ON CONFLICT DO NOTHING;",
        )
        assert guard(admin, turn="quiet-blocked") == 0
        t.psql(admin, "DELETE FROM ticket_board.ticket_blockers WHERE ticket_id='PGU-1';")
        checks += 1

        # 7b. The Director is owed one notification about a ticket, not one per
        #     generator. If something is already queued for them about it -- an
        #     escalation from the reminder path, most often -- this stays quiet.
        #     Found by ticket_board_idle_turn_end_nudges_test: without this, an
        #     unresolved_turn row for the same ticket sat beside the escalation
        #     and the reminder suite's "one escalation per stall" count broke.
        drain(admin)
        # Inserted directly as the owning connection: the listener role may read
        # the queue and run the gated generators, but not enqueue by hand.
        t.psql(
            admin,
            "INSERT INTO ticket_board.ticket_notification_queue "
            "(ticket_id, kind, target_role, message, payload, dedupe_key) "
            "VALUES ('PGU-1', 'escalation', 'director', 'already told', "
            "'{}'::jsonb, 'already-told:PGU-1');",
        )
        assert guard(admin, turn="turn-already-told") == 0
        assert [q for q in queued(admin, "PGU-1") if q.startswith("unresolved_turn")] == []
        drain(admin)
        checks += 2

        # ... and once that is delivered and gone, a later unresolved turn is
        # reported again: the suppression is about the same breath, not forever.
        assert guard(admin, turn="turn-after-told") == 1
        drain(admin)
        checks += 1

        # 8. A role whose turn did not end is not judged at all.
        drain(admin)
        assert guard(admin, role="audit", turn="turn-9") == 0
        assert queued(admin, "PGU-1") == []
        checks += 2

    print(f"unresolved_turn_guard_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
