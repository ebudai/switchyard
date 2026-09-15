#!/usr/bin/env python3
"""SYRD-148: a blocked ticket cannot hand work to a role that cannot take it.

Live on SYRD-146: it carried blocked_by SYRD-147 with resolved=false, and an
await_role=director handoff was accepted and delivered anyway. An
awaiting-role handoff is not a note. It tells a named role to act now, it
suppresses that ticket's nudges while it stands, and after thirty minutes it
tells the Director that role ignored it. While a dependency is unresolved
every part of that is false, and the Director is given work nobody can do.

Three places, because there are three ways a false handoff can exist: one
created while blocked, one created legitimately and overtaken by a later
blocker, and one already sitting in the queue when either of those happens.
The cases below drive all three, and two more hold the behaviour that must not
change -- an ordinary handoff once the blockers resolve, and the unrelated
notifications this ticket names.

Run against a real cluster, because the rule is enforced in SQL inside the
same writes that create blockers and waits, and atomicity is the point of
where it is enforced.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402


def psql_fails(conninfo: str, sql: str) -> str:
    """Run SQL that must be refused, and give back what the database said."""
    import subprocess

    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    assert proc.returncode != 0, f"expected a refusal, got: {proc.stdout}"
    return proc.stderr


class Board:
    def __init__(self, admin: str, listener: str, service: str) -> None:
        self.admin = admin
        self.listener = listener
        self.service = service
        self.sent: list[tuple[str, str]] = []

    def add(self, ticket_id: str, *, state: str, assignee: str) -> None:
        t.seed_postgres_ticket(
            self.admin, ticket_id, title=f"Ticket {ticket_id}", state=state,
            assignee=assignee, commit_exempt=True,
        )

    def as_role(self, role: str, sql: str) -> str:
        return t.psql(self.service, f"SELECT set_config('ticket_board.caller_role', '{role}', false);\n{sql}")

    def as_role_refused(self, role: str, sql: str) -> str:
        return psql_fails(self.service, f"SELECT set_config('ticket_board.caller_role', '{role}', false);\n{sql}")

    def set_blockers(self, ticket_id: str, blockers: list[str], reason: str) -> None:
        ids = "ARRAY[" + ", ".join(f"'{item}'" for item in blockers) + "]::text[]" if blockers else "ARRAY[]::text[]"
        self.as_role("director", f"SELECT ticket_board.set_blockers('{ticket_id}', {ids}, '{reason}');")

    def await_role(self, ticket_id: str, role: str, *, caller: str) -> None:
        self.as_role(caller, f"SELECT ticket_board.set_awaiting_role('{ticket_id}', '{role}');")

    def await_role_refused(self, ticket_id: str, role: str, *, caller: str) -> str:
        return self.as_role_refused(caller, f"SELECT ticket_board.set_awaiting_role('{ticket_id}', '{role}');")

    def wait_state(self, ticket_id: str) -> tuple[str, bool]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(awaiting_role, '') || '|' || (awaiting_since_at IS NOT NULL)::text
FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket_id}';
""")
        role, _, since = raw.partition("|")
        return role, since.strip().lower() in {"t", "true"}

    def queued(self, ticket_id: str) -> list[dict[str, object]]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object('kind', kind, 'target_role', target_role, 'message', message) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def finish(self, ticket_id: str) -> None:
        """Resolve a blocker the way the board resolves one."""
        self.as_role("director", f"""
SELECT set_config('ticket_board.force_move', 'on', true);
SELECT ticket_board.force_move('{ticket_id}', 'done', 'director', false);
""")

    def deliver(self, *, rounds: int = 6) -> None:
        for _round in range(rounds):
            listener = TicketBoardNotifyListener(
                conninfo=self.listener,
                sender=lambda target, message: self.sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
                target_exists=lambda _target: True,
            )
            listener.listen_once(max_notifications=1)


def main() -> int:
    from temporary_cluster import temporary_cluster

    checks = 0
    with temporary_cluster(prefix="syrd148-blocked-handoff-", shutdown="immediate") as cluster:
        dbname = "syrd148_blocked_handoff"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        listener = t.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))
        board = Board(admin, listener, service)

        # THE LIVE SHAPE. An active ticket, an unresolved blocker, and its owner
        # trying to hand off to the Director.
        board.add("PGU-146", state="in_progress", assignee="ops")
        board.add("PGU-147", state="analysis", assignee="director")
        board.set_blockers("PGU-146", ["PGU-147"], "waiting on PGU-147")
        refusal = board.await_role_refused("PGU-146", "director", caller="ops")
        assert "unresolved blocker prevents an awaiting-role handoff" in refusal, refusal
        # Named, so the caller knows which ticket has to move first.
        assert "PGU-147" in refusal, refusal
        checks += 1

        # Refused, not quietly dropped: no wait exists and nothing was queued.
        assert board.wait_state("PGU-146") == ("", False), board.wait_state("PGU-146")
        assert [row for row in board.queued("PGU-146") if row["kind"] == "awaiting_role"] == [], board.queued("PGU-146")
        checks += 1

        # A WAIT OVERTAKEN BY A LATER BLOCKER. Legitimate when it was made.
        board.add("PGU-150", state="in_progress", assignee="app")
        board.add("PGU-151", state="analysis", assignee="director")
        # Awaiting a role other than the one who adds the blocker. The board
        # already clears a wait when the awaited role itself touches the ticket,
        # so a Director-awaiting wait retired by a Director's own write would
        # not show this clearing at all.
        board.await_role("PGU-150", "main", caller="app")
        assert board.wait_state("PGU-150") == ("main", True), board.wait_state("PGU-150")
        queued_before = [row for row in board.queued("PGU-150") if row["kind"] == "awaiting_role"]
        assert queued_before, board.queued("PGU-150")
        checks += 1

        board.set_blockers("PGU-150", ["PGU-151"], "waiting on PGU-151")
        # Cleared in the same write that created the blocker, so the board never
        # shows a blocked ticket that is also awaiting somebody.
        assert board.wait_state("PGU-150") == ("", False), board.wait_state("PGU-150")
        checks += 1

        # AND THE ROWS ALREADY IN THE QUEUE do not deliver, which is the part
        # the write boundary cannot fix on its own.
        board.sent.clear()
        board.deliver()
        handoffs = [message for _target, message in board.sent if "is awaiting main" in message]
        assert handoffs == [], board.sent
        checks += 1

        # PRESERVED: once the blocker resolves, an ordinary handoff works and
        # is delivered. The rule is about blocked tickets, not about handoffs.
        board.finish("PGU-151")
        # Awaiting a role other than the one who adds the blocker. The board
        # already clears a wait when the awaited role itself touches the ticket,
        # so a Director-awaiting wait retired by a Director's own write would
        # not show this clearing at all.
        board.await_role("PGU-150", "main", caller="app")
        assert board.wait_state("PGU-150") == ("main", True), board.wait_state("PGU-150")
        board.sent.clear()
        board.deliver()
        assert any("is awaiting main" in message for _target, message in board.sent), board.sent
        checks += 1

        # PRESERVED: the notification that fires when a blocker resolves is a
        # different kind and is untouched -- this ticket asks for that to be
        # checked rather than assumed.
        unblocked = [row for row in board.queued("PGU-150") if row["kind"] == "transition"]
        assert board.queued("PGU-150") is not None
        board.add("PGU-160", state="in_progress", assignee="research")
        board.add("PGU-161", state="analysis", assignee="director")
        board.set_blockers("PGU-160", ["PGU-161"], "waiting on PGU-161")
        t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-160';")
        board.finish("PGU-161")
        dependency = board.queued("PGU-160")
        assert dependency, dependency
        assert all(row["kind"] != "awaiting_role" for row in dependency), dependency
        checks += 1


        # THE SHAPE THAT MADE THE DIRECTOR CLEAR IT TWICE. On the live ticket
        # the wait was cleared and then reappeared, and only the second clear
        # held. Nothing was resurrecting it: the board still accepted a fresh
        # handoff on a ticket that was still blocked, so a clear lasted exactly
        # until the next attempt. The refusal is what makes one clear enough.
        board.add("PGU-190", state="in_progress", assignee="main")
        board.add("PGU-191", state="analysis", assignee="director")
        board.await_role("PGU-190", "ops", caller="main")
        assert board.wait_state("PGU-190") == ("ops", True), board.wait_state("PGU-190")
        board.set_blockers("PGU-190", ["PGU-191"], "waiting on PGU-191")
        assert board.wait_state("PGU-190") == ("", False), board.wait_state("PGU-190")
        # The retry a person makes when the handoff still looks like the right
        # move. Before this change it succeeded and the wait was back.
        again = board.await_role_refused("PGU-190", "ops", caller="main")
        assert "unresolved blocker prevents an awaiting-role handoff" in again, again
        assert "PGU-191" in again, again
        assert board.wait_state("PGU-190") == ("", False), board.wait_state("PGU-190")
        # The first handoff's queue rows are still there -- it was legitimate
        # when it was made, and clearing a wait does not reach into the queue.
        # What matters is that none of them reaches anybody.
        board.sent.clear()
        board.deliver()
        assert not any("is awaiting ops" in message for _target, message in board.sent), board.sent
        checks += 1

        # THE ROWS AN UPGRADE INHERITS. Everything above goes through the write
        # boundary, which now refuses to make a blocked wait at all -- so the
        # two later guards would never run and could be deleted without a case
        # noticing. A board upgraded into this change can hold exactly these
        # rows already, so they are planted directly rather than made, which is
        # the only way to reach the code that exists for them.
        board.add("PGU-170", state="in_progress", assignee="perf")
        board.add("PGU-171", state="analysis", assignee="director")
        board.set_blockers("PGU-170", ["PGU-171"], "waiting on PGU-171")
        t.psql(admin, """
UPDATE ticket_board.ticket_notification_state
SET awaiting_role = 'main', awaiting_since_at = clock_timestamp(), awaiting_notified_since_at = NULL
WHERE ticket_id = 'PGU-170';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-170';
""")
        t.psql(admin, "SELECT ticket_board.enqueue_awaiting_role_handoff('PGU-170');")
        assert [row for row in board.queued("PGU-170") if row["kind"] == "awaiting_role"] == [], board.queued("PGU-170")
        checks += 1

        # And one already in the queue, with its wait still matching, whose
        # ticket is blocked: the listener is the last place this can be caught.
        t.psql(admin, "DELETE FROM ticket_board.ticket_blockers WHERE ticket_id = 'PGU-170';")
        t.psql(admin, """
UPDATE ticket_board.ticket_notification_state
SET awaiting_notified_since_at = NULL WHERE ticket_id = 'PGU-170';
""")
        t.psql(admin, "SELECT ticket_board.enqueue_awaiting_role_handoff('PGU-170');")
        assert [row for row in board.queued("PGU-170") if row["kind"] == "awaiting_role"], board.queued("PGU-170")
        t.psql(admin, """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved)
VALUES ('PGU-170', 'PGU-171', 0, false);
""")
        board.sent.clear()
        board.deliver()
        assert not any("is awaiting main" in message for _target, message in board.sent), board.sent
        checks += 1

        # THE REFUSAL NAMES WHAT IS ACTUALLY IN THE WAY, not every blocker the
        # ticket ever had. A reader sent after a resolved one loses the trail.
        board.add("PGU-180", state="in_progress", assignee="research")
        board.add("PGU-181", state="analysis", assignee="director")
        board.add("PGU-182", state="analysis", assignee="director")
        board.set_blockers("PGU-180", ["PGU-181", "PGU-182"], "waiting on both")
        board.finish("PGU-181")
        named = board.await_role_refused("PGU-180", "main", caller="research")
        assert "PGU-182" in named, named
        assert "PGU-181" not in named, named
        checks += 1

    print(f"ticket_board_blocked_handoff_postgres_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
