#!/usr/bin/env python3
"""SYRD-109: a ticket held by a serial-focus reservation must not be nudged.

The reproduction is notification 1324. SYRD-107 sat in analysis/director,
queued for main behind SYRD-93, with SYRD-93 still in_progress/main -- so the
one move the reminder asked for, routing it into main's lane, is the move the
board itself refuses. The director was told "advance it or hand it off" anyway.

Run against a real cluster. The decision is made in SQL, inside the selectors
that generate reminders, and the wake it pairs with is a durable claim written
in the same transaction as the notification. A fake connection would assert the
shape of a query rather than what the database does with it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id, "title": title, "body": "", "state": state,
        "assignee": assignee, "comments": [],
        "created": "2026-09-13T00:00:00+00:00", "updated": "2026-09-13T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def create_pane_roles(conninfo: str) -> None:
    psql(conninfo, "\n".join(
        f'CREATE ROLE {name} LOGIN;'
        for name in ('director', '"user"', 'ops', 'app', 'audit', 'inspector', 'perf', 'research', 'main')
    ))


class Board:
    """One disposable board, and the few things these cases do to it."""

    def __init__(self, admin: str, listener: str) -> None:
        self.admin = admin
        self.listener = listener
        self.sent: list[tuple[str, str]] = []

    def add_ticket(self, ticket_id: str, title: str, state: str, assignee: str) -> None:
        psql(self.admin, f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', 'backlog', '{assignee}', '',
    '2026-09-13T00:00:00+00:00', '2026-09-13T00:00:00+00:00',
    '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
UPDATE ticket_board.tickets SET state = '{state}', assignee = '{assignee}' WHERE id = '{ticket_id}';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '2 hours',
    last_activity_at = clock_timestamp() - interval '2 hours',
    last_nudged_at = NULL
WHERE ticket_id = '{ticket_id}';
""")

    def set_queue(self, ticket_id: str, queued_for: str, queued_behind: str) -> None:
        """Through an ordinary UPDATE, so the wake-key trigger sees it move."""
        psql(self.admin, f"""
UPDATE ticket_board.tickets
SET queued_for_assignee = '{queued_for}', queued_behind_ticket = '{queued_behind}'
WHERE id = '{ticket_id}';
""")

    def move(self, ticket_id: str, state: str, assignee: str) -> None:
        psql(self.admin, f"""
UPDATE ticket_board.tickets SET state = '{state}', assignee = '{assignee}' WHERE id = '{ticket_id}';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '2 hours',
    last_activity_at = clock_timestamp() - interval '2 hours',
    last_nudged_at = NULL
WHERE ticket_id = '{ticket_id}';
""")

    def block(self, ticket_id: str, blocker_id: str) -> None:
        psql(self.admin, f"""
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('{ticket_id}', '{blocker_id}', 0)
ON CONFLICT DO NOTHING;
""")

    def unblock(self, ticket_id: str) -> None:
        psql(self.admin, f"DELETE FROM ticket_board.ticket_blockers WHERE ticket_id = '{ticket_id}';")

    def clear_queue_rows(self) -> None:
        psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")

    def generate_turn_end(self, role: str = "director") -> None:
        psql(self.listener, f"""
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '30 minutes')::text),
    clock_timestamp()
);
""")

    def generate_stall(self, role: str = "director") -> None:
        psql(self.listener, f"""
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '30 minutes')::text),
    clock_timestamp()
);
""")

    def generate_due(self) -> None:
        psql(self.admin, "SELECT ticket_board.notify_due_nudges(clock_timestamp());")

    def generate_wakeups(self) -> int:
        return int(psql(self.listener,
                        "SELECT ticket_board.notify_serial_focus_queue_wakeups(clock_timestamp());"))

    def queued(self) -> list[dict[str, object]]:
        raw = psql(self.listener, """
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', id, 'ticket_id', ticket_id, 'kind', kind, 'target_role', target_role, 'message', message
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue;
""")
        return json.loads(raw)

    def trace(self, ticket_id: str) -> list[dict[str, object]]:
        raw = psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'event', event, 'kind', kind, 'busy_reason', coalesce(busy_reason, '')
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.notification_trace WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def wake_key(self, ticket_id: str) -> str:
        return psql(self.listener, f"""
SELECT serial_focus_wake_key FROM ticket_board.ticket_notification_state
WHERE ticket_id = '{ticket_id}';
""")

    def reservation_is_current(self, ticket_id: str) -> bool:
        # Read as the owner. The predicate is deliberately not granted to the
        # listener: every caller in production is either SECURITY DEFINER or
        # the owner itself, and widening that for a test would be the test
        # changing the thing it is checking.
        answer = psql(self.admin, f"""
SELECT ticket_board.ticket_serial_focus_reservation_is_current(
    id, queued_for_assignee, queued_behind_ticket)
FROM ticket_board.tickets WHERE id = '{ticket_id}';
""")
        # Strict. A NULL reads as "not current" to a Python truth test, but in
        # the selectors it becomes `AND NOT NULL`, which drops the row rather
        # than keeping it -- so the predicate meant to stop a ticket being
        # silenced forever would be the thing silencing it.
        assert answer in ("t", "f"), f"{ticket_id}: predicate answered {answer!r}, not a boolean"
        return answer == "t"

    def deliver(self, gate, *, rounds: int = 1) -> int:
        """Work the queue with the pane looking however the gate says.

        A fresh listener per round, each allowed one delivery: the budget counts
        over a listener's whole life, so one long-lived listener with a large
        budget waits on the channel instead of returning.
        """
        worked = 0
        for _round in range(rounds):
            listener = TicketBoardNotifyListener(
                conninfo=self.listener,
                sender=lambda target, message: self.sent.append((target, message)),
                activity_gate=gate,
                poll_seconds=0,
                target_exists=lambda _target: True,
            )
            worked += listener.listen_once(max_notifications=1)
        return worked


def for_ticket(rows: list[dict[str, object]], ticket_id: str) -> list[dict[str, object]]:
    return [row for row in rows if row["ticket_id"] == ticket_id]


def main() -> int:
    with temporary_cluster(prefix="ticket-board-queued-reminder.") as cluster:
        dbname = "syrd_queued_reminder_test"
        admin = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=postgres"
        listener = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_listener"
        subprocess.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname],
            check=True, capture_output=True, text=True,
        )
        psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
        create_pane_roles(admin)
        psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
        board = Board(admin, listener)

        # THE LIVE CASE. SYRD-107's shape: the queued ticket is in the director's
        # own analysis lane, reserved for main, behind work main still holds.
        # It carries the LOWER ticket number, so without the fix it is the one
        # the selector picks -- which is exactly what happened.
        board.add_ticket("SYRD-901", "Queued behind a reservation", "analysis", "director")
        board.add_ticket("SYRD-902", "Genuinely unattended analysis", "analysis", "director")
        board.add_ticket("SYRD-950", "Main is working on this", "in_progress", "main")
        board.set_queue("SYRD-901", "main", "SYRD-950")

        check(board.reservation_is_current("SYRD-901"),
              "the reservation SYRD-901 names should read as current")

        board.clear_queue_rows()
        board.generate_turn_end()
        rows = board.queued()
        check(not for_ticket(rows, "SYRD-901"),
              f"a queued ticket must not be reminded: {rows}")
        check(len(for_ticket(rows, "SYRD-902")) == 1,
              f"the unattended analysis ticket must still be reminded: {rows}")
        check("Advance it or hand it off" in str(for_ticket(rows, "SYRD-902")[0]["message"]),
              "the surviving reminder should be the ordinary one")

        # The other two generic generators reach the same ticket by their own
        # routes. A reservation that silences one prod and not the others is
        # not a fix, it is a quieter version of the same complaint.
        board.clear_queue_rows()
        board.generate_stall()
        rows = board.queued()
        check(not for_ticket(rows, "SYRD-901"), f"stall nudges must skip it too: {rows}")
        board.clear_queue_rows()
        board.generate_due()
        rows = board.queued()
        check(not for_ticket(rows, "SYRD-901"), f"due nudges must skip it too: {rows}")

        # THE SAME GUARD WHERE A DIFFERENT QUEUE DESTINATION WOULD PUT IT.
        # This project's holding stage is analysis/director, so every case above
        # reaches the selectors through their director branch. The destination is
        # declarative -- `queue.stage` and `queue.assignee` -- and a tenant whose
        # holding stage is owned by a review role instead lands in the owner
        # branch beside it, where the reminder is just as impossible to act on.
        # Through the stages it would really travel: backlog is not allowed to
        # jump straight to a review stage, and forcing it would be testing a
        # row shape the board cannot produce.
        board.add_ticket("SYRD-905", "Queued in a role-owned holding stage", "in_progress", "ops")
        board.move("SYRD-905", "audit", "audit")
        board.set_queue("SYRD-905", "main", "SYRD-950")
        check(board.reservation_is_current("SYRD-905"),
              "the owner-branch ticket's reservation should read as current")
        board.clear_queue_rows()
        board.generate_stall("audit")
        check(not for_ticket(board.queued(), "SYRD-905"),
              f"stall nudges must skip it in the owner branch: {board.queued()}")
        board.clear_queue_rows()
        board.generate_due()
        check(not for_ticket(board.queued(), "SYRD-905"),
              f"due nudges must skip it in the owner branch: {board.queued()}")

        # And the guard is that narrow: clear the marker and the same ticket,
        # in the same stage, is nudged again on the very next pass.
        board.set_queue("SYRD-905", "", "")
        board.clear_queue_rows()
        board.generate_due()
        check(len(for_ticket(board.queued(), "SYRD-905")) == 1,
              f"an unqueued audit ticket is still nudged: {board.queued()}")
        psql(admin, "DELETE FROM ticket_board.tickets WHERE id = 'SYRD-905';")

        # A MARKER CANNOT SILENCE THE IMPLEMENTER IT NAMES. If the ticket has
        # since reached that implementer's own lane, it is their work, not their
        # queue -- and the reservation lookup excludes the ticket it is asked
        # about, so a stale marker would otherwise match somebody else's hold.
        board.add_ticket("SYRD-906", "Main is actually working on this", "in_progress", "main")
        board.set_queue("SYRD-906", "main", "SYRD-950")
        check(not board.reservation_is_current("SYRD-906"),
              "a ticket owned by the implementer it names is not waiting for them")
        psql(admin, "DELETE FROM ticket_board.tickets WHERE id = 'SYRD-906';")

        # No wake while the wait is real.
        board.clear_queue_rows()
        check(board.generate_wakeups() == 0, "no wake is due while the reservation holds")
        check(board.wake_key("SYRD-901") == "", "nothing should be claimed yet")

        # THE RESERVATION CLEARS. One specific hand-off, and it says capacity is
        # available because it actually is.
        # Deferred out of the implementation lane, which is how a reservation
        # most often ends without the ticket being finished.
        board.move("SYRD-950", "backlog", "main")
        check(not board.reservation_is_current("SYRD-901"),
              "a finished reservation must stop reading as current")
        check(board.generate_wakeups() == 1, "the end of the wait is announced once")
        rows = for_ticket(board.queued(), "SYRD-901")
        check(len(rows) == 1, f"exactly one wake notification: {rows}")
        wake = rows[0]
        check(wake["target_role"] == "director", f"the wake goes to the director: {wake}")
        check("can be routed to main now" in str(wake["message"]), f"capacity wording: {wake}")
        check("SYRD-950" in str(wake["message"]), f"it names the reservation that ended: {wake}")
        check(board.wake_key("SYRD-901") == "main:SYRD-950",
              "the identity is claimed durably")

        # EXACTLY ONCE, ACROSS A BUSY PANE AND A RESTART. The claim is written
        # with the notification, so neither a deferral nor a fresh listener can
        # mint a second.
        check(board.generate_wakeups() == 0, "a second pass adds nothing")
        deferred = board.deliver(lambda _target: True, rounds=1)
        check(not board.sent, f"a busy pane defers it rather than dropping it: {board.sent}")
        check(len(for_ticket(board.queued(), "SYRD-901")) == 1,
              "the deferred wake is still queued")
        psql(admin, """
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp()
WHERE ticket_id = 'SYRD-901';
""")
        board.deliver(lambda _target: False, rounds=1)
        check(len(board.sent) == 1, f"delivered exactly once: {board.sent}")
        check("can be routed to main now" in board.sent[0][1], f"and it is the wake: {board.sent}")
        check(not for_ticket(board.queued(), "SYRD-901"), "acknowledged, so the row is gone")
        check(board.generate_wakeups() == 0,
              "a listener pass after delivery must not mint a second wake")

        # ORDINARY ELIGIBILITY RESUMES. The wait is over, so the ticket is
        # ordinary work again and the reminder selector may have it back.
        board.clear_queue_rows()
        psql(admin, "DELETE FROM ticket_board.tickets WHERE id = 'SYRD-902';")
        board.generate_turn_end()
        check(len(for_ticket(board.queued(), "SYRD-901")) == 1,
              "a ticket whose wait ended is reminded like any other")

        # A RESTART MINTS NOTHING. A listener that has never seen this ticket
        # reads the same claim and the same queue, so the wake it already
        # delivered stays delivered.
        restarted = TicketBoardNotifyListener(
            conninfo=listener,
            sender=lambda target, message: board.sent.append((target, message)),
            activity_gate=lambda _target: False,
            poll_seconds=0,
            target_exists=lambda _target: True,
        )
        # With the queue emptied first, so anything this listener sends could
        # only be a wake it decided to mint again. Leaving the ordinary reminder
        # from the section above in place would have it deliver that instead and
        # the check would pass for the wrong reason.
        board.clear_queue_rows()
        before_restart = len(board.sent)
        restarted.listen_once(max_notifications=1)
        check(len(board.sent) == before_restart,
              f"a fresh listener must not redeliver a wake: {board.sent[before_restart:]}")
        check(board.generate_wakeups() == 0,
              "and must not mint one either")

        # A BLOCKED TICKET IS NOT CAPACITY EITHER. Announcing one would be this
        # ticket's own complaint wearing a different hat -- and the claim is
        # left unmade, so the hand-off arrives when the blocker clears.
        board.add_ticket("SYRD-907", "Blocked while queued", "analysis", "director")
        board.add_ticket("SYRD-908", "The blocker", "analysis", "director")
        board.set_queue("SYRD-907", "main", "SYRD-950")
        board.block("SYRD-907", "SYRD-908")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 0, "a blocked ticket is not woken")
        check(board.wake_key("SYRD-907") == "", "and its identity is left unclaimed")
        board.unblock("SYRD-907")
        check(board.generate_wakeups() == 1, "unblocking releases the hand-off it deferred")
        check(len(for_ticket(board.queued(), "SYRD-907")) == 1,
              "and it is the one wake, not a backlog of them")
        psql(admin, "DELETE FROM ticket_board.tickets WHERE id IN ('SYRD-907', 'SYRD-908');")
        board.clear_queue_rows()

        # A REROUTE RETARGETS BOTH THE IDENTITY AND THE WAKE CONDITION.
        board.add_ticket("SYRD-960", "Ops is working on this", "in_progress", "ops")
        board.set_queue("SYRD-901", "ops", "SYRD-960")
        check(board.wake_key("SYRD-901") == "",
              "a new queue identity clears the claim the old one made")
        check(board.reservation_is_current("SYRD-901"),
              "the new reservation reads as current")
        board.clear_queue_rows()
        board.generate_turn_end()
        check(not for_ticket(board.queued(), "SYRD-901"),
              "the reroute re-suppresses against its new reservation")
        check(board.generate_wakeups() == 0, "and no wake is due for the new wait")

        # A RESERVATION TAKEN OVER BY OTHER WORK IS NOT CAPACITY. The wait ends
        # -- the named ticket no longer holds ops -- but saying "route it now"
        # would send the director straight back into a full lane.
        # SYRD-960 has to leave the lane before SYRD-961 can enter it: serial
        # focus would otherwise bounce SYRD-961 straight back to backlog, which
        # is the constraint this whole queue exists to preserve.
        board.move("SYRD-960", "backlog", "ops")
        board.add_ticket("SYRD-961", "Ops picked this up instead", "in_progress", "ops")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 1, "the named reservation ending is still announced")
        wake = for_ticket(board.queued(), "SYRD-901")[0]
        check("SYRD-961" in str(wake["message"]), f"it names what holds ops now: {wake}")
        check("can be routed to ops now" not in str(wake["message"]),
              f"and does not claim capacity that does not exist: {wake}")

        # A STALE IDENTITY IS NOT A REASON TO STAY QUIET. A queued_behind that
        # names a ticket which never held this implementer -- or no ticket at
        # all -- fails open to ordinary reminders rather than silencing the
        # ticket for good.
        board.set_queue("SYRD-901", "ops", "SYRD-999")
        check(not board.reservation_is_current("SYRD-901"),
              "a reservation naming a ticket that does not exist is not current")
        board.clear_queue_rows()
        board.generate_turn_end()
        check(len(for_ticket(board.queued(), "SYRD-901")) == 1,
              "a stale queue identity must not suppress reminders")

        board.set_queue("SYRD-901", "ops", "SYRD-950")
        check(not board.reservation_is_current("SYRD-901"),
              "a superseded identity -- SYRD-961 holds ops, not SYRD-950 -- is not current")

        # CLEARING THE FIELDS RESTORES ELIGIBILITY IMMEDIATELY, AND LEAVES
        # NOTHING TO WAKE.
        board.set_queue("SYRD-901", "", "")
        check(not board.reservation_is_current("SYRD-901"),
              "cleared fields cannot be a current reservation")
        board.clear_queue_rows()
        board.generate_turn_end()
        check(len(for_ticket(board.queued(), "SYRD-901")) == 1,
              "clearing the queue fields restores ordinary idle eligibility")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 0, "a cleared queue has no reservation to wake from")
        check(board.wake_key("SYRD-901") == "", "and no claim is left behind")

        # THE TWO NOTIFICATION FIXES HAVE TO COMPOSE. SYRD-108 discards any
        # notification carrying a queue identity the ticket has since moved
        # past, reading `queued_for` and `reserved_by` off the payload. A wake
        # carries a queue identity by its nature, so it is subject to that check
        # -- and must survive it while the identity is unchanged. It did not:
        # the first merge of the two was textually clean and dropped every wake
        # at claim, because the wake named the reservation `queued_behind` and
        # the check read `reserved_by` as empty.
        board.add_ticket("SYRD-910", "Composes with the queue-notice drop", "analysis", "director")
        board.add_ticket("SYRD-970", "Main holds this", "in_progress", "main")
        board.set_queue("SYRD-910", "main", "SYRD-970")
        board.move("SYRD-970", "backlog", "main")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 1, "a wake is due for SYRD-910")
        before = len(board.sent)
        board.deliver(lambda _target: False, rounds=1)
        check(len(board.sent) == before + 1,
              f"an unchanged identity must still be delivered: {board.sent[before:]}")
        check("can be routed to main now" in board.sent[-1][1],
              f"and it is the wake: {board.sent[-1]}")
        dropped = [row for row in board.trace("SYRD-910")
                   if row["event"] == "drop" and row["busy_reason"] == "superseded_serial_focus_queue"]
        check(not dropped, f"nothing should have discarded it: {dropped}")

        # And the other direction: a reroute between enqueue and send moves the
        # identity, so the wake is discarded rather than telling the Director
        # about a wait that has been retargeted. The claim moves with it, so the
        # new identity gets its own wake when its own reservation ends.
        # A genuinely different identity, so there is a fresh wake to move. Setting
        # the same two values back would change nothing and mint nothing, which is
        # the claim working rather than a case worth testing.
        board.add_ticket("SYRD-971", "Ops holds this", "in_progress", "ops")
        board.set_queue("SYRD-910", "ops", "SYRD-971")
        check(board.wake_key("SYRD-910") == "", "the new identity clears the delivered claim")
        board.move("SYRD-971", "backlog", "ops")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 1, "a wake is minted for the new identity")
        board.add_ticket("SYRD-972", "App holds this", "in_progress", "app")
        board.set_queue("SYRD-910", "app", "SYRD-972")
        before = len(board.sent)
        board.deliver(lambda _target: False, rounds=1)
        check(len(board.sent) == before,
              f"a wake whose identity moved must not be sent: {board.sent[before:]}")
        superseded = [row for row in board.trace("SYRD-910")
                      if row["event"] == "drop" and row["busy_reason"] == "superseded_serial_focus_queue"]
        check(superseded, "and it is discarded as a superseded queue notice")
        check(board.wake_key("SYRD-910") == "",
              "the reroute cleared the claim, so the new wait can be announced in its turn")

        # A HELD TICKET IS STILL THE DIRECTOR'S TO STEER BY HAND.
        board.set_queue("SYRD-901", "ops", "SYRD-961")
        psql(admin, "UPDATE ticket_board.tickets SET manually_controlled = true WHERE id = 'SYRD-901';")
        board.clear_queue_rows()
        check(board.generate_wakeups() == 0,
              "manual control keeps its silence when the reservation ends")

    print(f"ticket_board_queued_reminder_suppression_postgres_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
