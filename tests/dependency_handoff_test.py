#!/usr/bin/env python3
"""SYRD-133: a dependency left in a comment, and an escalation that repeats.

The live sequence on SYRD-131, corrected by what the board's own trace says
rather than by what the ticket first looked like. Ops took the Director's
retarget, staged the packet, wrote that the remaining step was a privileged
Director one, and stopped. `awaiting_role` stayed empty, so no durable handoff
existed and the work sat in `in_progress/ops` with nobody holding it.

The fail-safe underneath DID work: an idle reminder reached Ops at 14:56 and the
escalation reached the Director at 15:43. I had assumed it never fired; the
notification trace says otherwise, and the Director confirmed it. What it does
not do is stop. The escalation's dedupe key lives only as long as its queue row,
so once the Director acknowledged it the next wave found the same idle owner and
the same unmoved ticket and enqueued it again -- at 15:44:15, under a minute
later. A fail-safe that repeats every wave is one an operator learns to ignore.

So two things are proved here. `request_dependency` records the reason and the
wait in one transaction, so the durable half cannot be the part somebody
forgets, and the assignee is untouched because the work is still theirs. And the
escalation fires once per stall: it is not repeated after it is actioned, it is
re-armed when the stall's identity genuinely changes, and it stays suppressed
for work that is deliberately held, durably queued, blocked, or already handed
off.

Real cluster, real generator, real notification queue.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

LISTENER_GRANTS = """
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_turn_end_nudges(jsonb, timestamptz)
    TO ticket_board_listener;
"""


def run_turn_end(admin: str, *, idle_roles: tuple[str, ...] = ("ops",), idle_for: str = "5 seconds") -> int:
    """One wave of the fail-safe, with the named roles' panes idle."""
    roles = ",".join(
        f"'{role}', (now_at - interval '{idle_for}')::text" for role in idle_roles
    )
    raw = t.psql(
        admin,
        f"""
SET ROLE ticket_board_listener;
WITH params AS (SELECT clock_timestamp() AS now_at)
SELECT ticket_board.notify_idle_turn_end_nudges(jsonb_build_object({roles}), now_at) FROM params;
RESET ROLE;
""",
    )
    for line in reversed(raw.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    raise AssertionError(raw)


def queued(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(jsonb_build_object("
        "'kind', kind, 'target_role', target_role, 'message', message) ORDER BY id), '[]'::jsonb)::text "
        f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';",
    )
    return json.loads(raw)


def clear_queue(admin: str) -> None:
    t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")


def comment_as(admin: str, ticket: str, who: str, text: str, *, ago: str = "10 seconds") -> None:
    """A plain comment, exactly as a role writes one, at a chosen age."""
    t.psql(
        admin,
        f"""
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, ts, text, urgent, source_json)
VALUES (
    '{ticket}',
    coalesce((SELECT max(position) + 1 FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket}'), 0),
    '{who}',
    ticket_board.utc_text(clock_timestamp() - interval '{ago}'),
    clock_timestamp() - interval '{ago}',
    '{text}',
    false,
    jsonb_build_object('who', '{who}', 'ts', ticket_board.utc_text(clock_timestamp() - interval '{ago}'),
                       'text', '{text}', 'urgent', false)
);
""",
    )


def age_ticket(admin: str, ticket: str, *, entered: str = "2 hours", activity: str = "1 hour") -> None:
    t.psql(
        admin,
        f"""
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '{entered}',
    last_activity_at = clock_timestamp() - interval '{activity}'
WHERE ticket_id = '{ticket}';
""",
    )


def awaiting(admin: str, ticket: str) -> str:
    return t.psql(
        admin,
        f"SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket}';",
    )


def assignee_of(admin: str, ticket: str) -> str:
    return t.psql(admin, f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';")


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd133-dependency-", shutdown="immediate") as cluster:
        dbname = "syrd133_dependency"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))
        t.psql(admin, LISTENER_GRANTS)

        # ---- 1. the action the sequence needed -------------------------------
        t.seed_postgres_ticket(
            admin, "PGU-131", title="Deploy and live-accept", state="in_progress", assignee="ops"
        )
        assert awaiting(admin, "PGU-131") == ""
        t.psql(
            admin,
            "SET ROLE ticket_board_service;\n"
            "SELECT set_config('ticket_board.caller_role', 'ops', false);\n"
            "SELECT ticket_board.request_dependency('PGU-131', 'director',\n"
            "    'The remaining step is a privileged Director one; I cannot run it.');",
        )
        # Both halves, from one call: the sentence AND the durable wait.
        assert awaiting(admin, "PGU-131") == "director", awaiting(admin, "PGU-131")
        recorded = t.psql(
            admin,
            "SELECT count(*)::text FROM ticket_board.ticket_comments "
            "WHERE ticket_id = 'PGU-131' AND who = 'ops' AND text LIKE '%privileged Director%';",
        )
        assert recorded == "1", recorded
        # And the work is still Ops's, so it comes back to them when the wait
        # clears rather than needing to be routed home.
        assert assignee_of(admin, "PGU-131") == "in_progress/ops", assignee_of(admin, "PGU-131")
        # The Director is told, durably, by the same call.
        handoff = queued(admin, "PGU-131")
        assert any(row["target_role"] == "director" for row in handoff), handoff
        checks += 1

        # Neither half without the other: a refused call leaves no wait behind.
        t.seed_postgres_ticket(
            admin, "PGU-140", title="No reason given", state="in_progress", assignee="ops"
        )
        for arguments, expected in (
            ("'PGU-140', 'director', '   '", "requires a reason"),
            ("'PGU-140', '', 'something is needed'", "requires"),
        ):
            failed = ""
            try:
                t.psql(
                    admin,
                    "SET ROLE ticket_board_service;\n"
            "SELECT set_config('ticket_board.caller_role', 'ops', false);\n"
                    f"SELECT ticket_board.request_dependency({arguments});",
                )
            except AssertionError as exc:
                failed = str(exc)
            assert expected in failed, (arguments, failed)
            assert awaiting(admin, "PGU-140") == "", awaiting(admin, "PGU-140")
        checks += 1

        # ---- 2. the fail-safe, and its bound ---------------------------------
        # The SYRD-131 shape with the action NOT used: a comment, then idle.
        t.seed_postgres_ticket(
            admin, "PGU-150", title="Dependency left in a comment", state="in_progress", assignee="main"
        )
        age_ticket(admin, "PGU-150")
        comment_as(admin, "PGU-150", "main", "A privileged Director step is required.", ago="90 minutes")
        clear_queue(admin)

        # Ops is reminded first -- they own it and may simply be back.
        assert run_turn_end(admin, idle_roles=("main",)) == 1, queued(admin, "PGU-150")
        first = queued(admin, "PGU-150")
        assert [row["target_role"] for row in first] == ["main"], first
        assert first[0]["kind"] == "idle_reminder", first
        # Delivered and acknowledged, as the live one was at 14:56.
        t.psql(
            admin,
            "UPDATE ticket_board.ticket_notification_state SET idle_reminder_count = 1 "
            "WHERE ticket_id = 'PGU-150';",
        )
        clear_queue(admin)

        # Then the Director, because the owner is still idle and the ticket has
        # still not moved. This is the escalation that reached the Director at
        # 15:43 and the one the ticket is about.
        assert run_turn_end(admin, idle_roles=("main",)) == 1, queued(admin, "PGU-150")
        escalated = queued(admin, "PGU-150")
        assert [row["target_role"] for row in escalated] == ["director"], escalated
        assert escalated[0]["kind"] == "escalation", escalated
        checks += 1

        # THE DEFECT: acknowledge it, and the next wave used to enqueue another.
        clear_queue(admin)
        assert run_turn_end(admin, idle_roles=("main",)) == 0, queued(admin, "PGU-150")
        assert queued(admin, "PGU-150") == [], queued(admin, "PGU-150")
        # Repeatedly: still nothing, however many waves run.
        for _wave in range(4):
            assert run_turn_end(admin, idle_roles=("main",)) == 0, queued(admin, "PGU-150")
        assert queued(admin, "PGU-150") == [], queued(admin, "PGU-150")
        checks += 1

        # RE-ARMED when the stall is a different stall. The owner came back and
        # went idle again: that is a new stall and deserves a new escalation.
        assert run_turn_end(admin, idle_roles=("main",), idle_for="10 seconds") == 1, queued(admin, "PGU-150")
        again = queued(admin, "PGU-150")
        assert [row["target_role"] for row in again] == ["director"], again
        clear_queue(admin)
        checks += 1

        # ---- 3. and it stays quiet for work that is genuinely held -----------
        # Using the action itself is the point: a ticket with a durable handoff
        # is not an unattended one.
        t.psql(
            admin,
            "SET ROLE ticket_board_service;\n"
            "SELECT set_config('ticket_board.caller_role', 'main', false);\n"
            "SELECT ticket_board.request_dependency('PGU-150', 'director', 'Waiting on the Director.');",
        )
        clear_queue(admin)
        assert run_turn_end(admin, idle_roles=("main",), idle_for="20 seconds") == 0, queued(admin, "PGU-150")
        checks += 1

        # Deliberately held, durably queued, and blocked each stay silent too.
        t.seed_postgres_ticket(admin, "PGU-160", title="Held", state="in_progress", assignee="main")
        t.seed_postgres_ticket(admin, "PGU-161", title="Queued", state="in_progress", assignee="main")
        t.seed_postgres_ticket(admin, "PGU-162", title="Blocked", state="in_progress", assignee="main")
        t.seed_postgres_ticket(admin, "PGU-163", title="Blocker", state="in_progress", assignee="app")
        for ticket in ("PGU-160", "PGU-161", "PGU-162"):
            age_ticket(admin, ticket)
        t.psql(admin, "UPDATE ticket_board.tickets SET manually_controlled = true WHERE id = 'PGU-160';")
        t.psql(
            admin,
            "UPDATE ticket_board.tickets SET queued_for_assignee = 'main', "
            "queued_behind_ticket = 'PGU-163' WHERE id = 'PGU-161';",
        )
        t.psql(
            admin,
            "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved) "
            "VALUES ('PGU-162', 'PGU-163', 0, false);",
        )
        clear_queue(admin)
        run_turn_end(admin, idle_roles=("main",), idle_for="90 minutes")
        for ticket, why in (
            ("PGU-160", "deliberately held"),
            ("PGU-161", "durably queued"),
            ("PGU-162", "blocked"),
        ):
            assert queued(admin, ticket) == [], (why, queued(admin, ticket))
        checks += 1

    print(f"dependency_handoff_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
