#!/usr/bin/env python3
"""SYRD-99: a queued reminder the board has already answered must not be sent.

A reminder, a nudge and an escalation all assert that the role owning a ticket
has not moved it. An awaiting-role handoff established after that wave was
generated asserts the opposite. `enqueue_idle_work_notifications()` already
refuses to generate reminders while a wait is active; the queue is the other
half, and it is where this went wrong live: an Ops escalation queued at 07:40:18
was deferred repeatedly on a busy Director pane and delivered at 07:50:56, more
than eight minutes after Ops handed SYRD-96 to the Director at 07:42:37.

Run against a real cluster, because the decision is made in SQL against the
ticket's current notification state and behind an RBAC grant. A fake connection
would assert the shape of a query rather than what the database answers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402


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
        "created": "2026-09-10T00:00:00+00:00", "updated": "2026-09-10T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def create_pane_roles(conninfo: str) -> None:
    psql(conninfo, "\n".join(
        f'CREATE ROLE {name} LOGIN;'
        for name in ('director', '"user"', 'ops', 'app', 'audit', 'inspector', 'perf', 'research', 'main')
    ))


class Board:
    """One disposable board, and the few things these cases do to it."""

    def __init__(self, admin: str, listener: str, service: str) -> None:
        self.admin = admin
        self.listener = listener
        self.service = service
        self.sent: list[tuple[str, str]] = []

    def add_ticket(self, ticket_id: str, assignee: str, *, idle_reminders: int = 1) -> None:
        """An in-progress ticket whose owner has already had one reminder.

        One reminder is what makes the next wave an escalation to the Director
        rather than another nudge to the owner, which is the live shape.
        """
        psql(self.admin, f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', 'Stall reporting', '', 'backlog', '{assignee}', 'Ready.',
    '2026-09-10T00:00:00+00:00', '2026-09-10T00:00:00+00:00',
    '{ticket_source(ticket_id, "Stall reporting", "backlog", assignee)}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = '{ticket_id}';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour',
    idle_reminder_count = {idle_reminders}
WHERE ticket_id = '{ticket_id}';
""")

    def generate_idle_wave(self, owner_role: str) -> None:
        """The real generator, so the row under test is the row the board makes."""
        psql(self.listener, f"""
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('{owner_role}', (clock_timestamp() - interval '30 minutes')::text),
    clock_timestamp()
);
""")

    def enqueue_reminder(self, ticket_id: str, kind: str, target_role: str, key: str) -> None:
        psql(self.admin, f"""
SELECT ticket_board.enqueue_notification(
    '{ticket_id}', '{kind}', '{target_role}',
    '{ticket_id} -- owner has not advanced it',
    jsonb_build_object('kind', '{kind}', 'id', '{ticket_id}'),
    '{key}', clock_timestamp()
);
""")

    def set_awaiting_role(self, ticket_id: str, awaiting: str, *, caller: str) -> None:
        """Through the same call a role makes, so the whole handoff happens."""
        psql(self.service, f"""
SELECT set_config('ticket_board.caller_role', '{caller}', false);
SELECT ticket_board.set_awaiting_role('{ticket_id}', '{awaiting}');
""")

    def clear_awaiting_role(self, ticket_id: str, *, caller: str) -> None:
        psql(self.service, f"""
SELECT set_config('ticket_board.caller_role', '{caller}', false);
SELECT ticket_board.clear_awaiting_role('{ticket_id}');
""")

    def end_busy_backoff(self, ticket_id: str) -> None:
        """The pane went idle, so the listener comes back to what it deferred.

        Only the rows a busy pane pushed into the future: the deferral schedule
        is not what these cases are about, and leaving the rest alone keeps the
        handoff's own bounded timings intact.
        """
        psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp()
WHERE ticket_id = '{ticket_id}' AND last_error IS NOT NULL;
""")

    def age_wait(self, ticket_id: str, age: str) -> None:
        psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_state
SET awaiting_since_at = clock_timestamp() - interval '{age}'
WHERE ticket_id = '{ticket_id}';
""")

    def queued(self, ticket_id: str) -> list[dict[str, object]]:
        raw = psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'kind', kind, 'target_role', target_role, 'attempts', attempts, 'last_error', last_error
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def trace(self, ticket_id: str) -> list[dict[str, object]]:
        raw = psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'event', event, 'kind', kind, 'target_role', target_role,
    'busy_reason', busy_reason, 'phase', detail ->> 'phase'
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.notification_trace WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def idle_reminder_count(self, ticket_id: str) -> int:
        return int(psql(self.listener, f"""
SELECT idle_reminder_count FROM ticket_board.ticket_notification_state
WHERE ticket_id = '{ticket_id}';
"""))

    def reset_idle_reminders(self, ticket_id: str, count: int) -> None:
        psql(self.admin, f"""
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
UPDATE ticket_board.ticket_notification_state
SET idle_reminder_count = {count},
    entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour'
WHERE ticket_id = '{ticket_id}';
""")

    def backdate_queued(self, ticket_id: str, age: str) -> None:
        """Make a queued row older than it is, so a wait can be newer than it."""
        psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_queue
SET created_at = clock_timestamp() - interval '{age}'
WHERE ticket_id = '{ticket_id}';
""")

    def deliver(self, *, busy: bool, rounds: int = 1) -> int:
        """Work the queue with the pane either busy or idle.

        A fresh listener per round, each allowed exactly one delivery. The budget
        counts deliveries over a listener's whole life, and a pass that has spent
        less than its budget waits on the channel for more -- so one long-lived
        listener with a large budget blocks here instead of returning. A round
        claims until it either delivers one notification or finds nothing due,
        and a drop is not a delivery, so drops never end a round early.
        """
        return self.deliver_with_gate(lambda _target: busy, rounds=rounds)

    def deliver_with_gate(self, gate: "Callable[[str], bool]", *, rounds: int = 1) -> int:
        """The same, with the caller deciding what the pane looks like each probe.

        The gate runs between claiming a notification and sending it, which is
        the window a handoff can land in on a real host: the probe samples a pane
        for seconds while the owning role is still working (SYRD-99).
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


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="ticket-board-superseded.") as cluster:
        dbname = "syrd_superseded_test"
        admin = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=postgres"
        listener_conninfo = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_listener"
        service = f"host={cluster.socket_dir} port={cluster.port} dbname={dbname} user=ticket_board_service"
        subprocess.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname],
            check=True, capture_output=True, text=True,
        )
        psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
        create_pane_roles(admin)
        psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
        board = Board(admin, listener_conninfo, service)

        # THE EXACT RACE. An Ops escalation to the Director, deferred while the
        # Director pane is busy, overtaken by Ops's own handoff, and then reached
        # by the listener once the pane goes idle.
        board.add_ticket("SYRD-901", "ops")
        board.generate_idle_wave("ops")
        queued = board.queued("SYRD-901")
        assert [row["kind"] for row in queued] == ["escalation"], queued
        assert queued[0]["target_role"] == "director", queued
        checks += 1

        assert board.deliver(busy=True) == 0, board.sent
        assert board.sent == [], board.sent
        deferred = board.queued("SYRD-901")
        assert deferred and deferred[0]["last_error"] == "pane busy", deferred
        checks += 1

        # Ops hands off. The escalation is now wrong, and nothing about the
        # ticket's state or assignee has changed to say so.
        board.set_awaiting_role("SYRD-901", "director", caller="ops")
        still_ops = psql(listener_conninfo, "SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = 'SYRD-901';")
        assert still_ops == "in_progress/ops", still_ops
        checks += 1

        # The pane goes idle. The escalation must not be sent.
        board.end_busy_backoff("SYRD-901")
        board.sent.clear()
        board.deliver(busy=False, rounds=6)
        escalations = [message for _target, message in board.sent if "may be stuck" in message]
        assert escalations == [], escalations
        remaining = [row for row in board.queued("SYRD-901") if row["kind"] == "escalation"]
        assert remaining == [], remaining
        checks += 1

        # And it says exactly why, distinguishably from a busy pane.
        trace = board.trace("SYRD-901")
        dropped = [row for row in trace if row["event"] == "drop" and row["kind"] == "escalation"]
        assert dropped and dropped[0]["busy_reason"] == "superseded_by_awaiting_role", trace
        assert any(row["event"] == "listener_discard" for row in trace), trace
        # Distinct from the deferral it spent eight minutes in, and from a
        # ticket that simply moved: reading the trace tells an operator which of
        # the three happened.
        deferred_reasons = {row["busy_reason"] for row in trace if row["event"] == "gate_defer"}
        assert deferred_reasons == {"busy"}, trace
        assert "superseded_by_awaiting_role" not in deferred_reasons, trace
        assert not any(row["busy_reason"] == "stale_notification" for row in trace), trace
        checks += 1

        # Removed without delivery accounting. Acking it would credit a reminder
        # nobody received, and the next wave would escalate on the strength of it.
        assert board.idle_reminder_count("SYRD-901") == 1, board.idle_reminder_count("SYRD-901")
        checks += 1

        # The handoff's own bounded schedule is untouched and still delivers.
        handoffs = [message for _target, message in board.sent if "is awaiting director" in message]
        assert handoffs, board.sent
        # And no part of that schedule is ever dropped for this reason. It cannot
        # be: a handoff's queue rows are written after the wait's own timestamp,
        # so none of them is newer than the wait it belongs to. Asserted rather
        # than assumed, because the whole point of the wait is that it still
        # notifies somebody.
        assert not any(
            row["kind"] == "awaiting_role" and row["busy_reason"] == "superseded_by_awaiting_role"
            for row in trace
        ), trace
        checks += 1

        # And the claim-time check is the one that caught it, because the wait
        # was already there when the listener came back.
        assert dropped[0]["phase"] == "claim", dropped
        checks += 1

        # THE OTHER WINDOW. A handoff can also land after the notification is
        # claimed, while the pane is still being probed -- a probe samples for
        # seconds and the owning role is working the whole time. Only the check
        # immediately before sending can see that one.
        board.clear_awaiting_role("SYRD-901", caller="ops")
        board.enqueue_reminder("SYRD-901", "escalation", "director", "escalation:SYRD-901:director:2")
        handed_off: list[bool] = []

        def hand_off_during_probe(_target: str) -> bool:
            if not handed_off:
                handed_off.append(True)
                board.set_awaiting_role("SYRD-901", "director", caller="ops")
            return False

        board.sent.clear()
        board.deliver_with_gate(hand_off_during_probe, rounds=4)
        assert handed_off, "the probe never ran, so nothing was raced"
        assert not any(
            "owner has not advanced it" in message for _t, message in board.sent
        ), board.sent
        assert [row for row in board.queued("SYRD-901") if row["kind"] == "escalation"] == []
        late = [
            row for row in board.trace("SYRD-901")
            if row["event"] == "drop" and row["busy_reason"] == "superseded_by_awaiting_role"
        ]
        assert any(row["phase"] == "pre_send_recheck" for row in late), late
        checks += 1

        # NO WAIT: a genuine escalation is still delivered.
        board.add_ticket("SYRD-902", "app")
        board.generate_idle_wave("app")
        board.sent.clear()
        board.deliver(busy=False, rounds=2)
        assert any("may be stuck" in message for _t, message in board.sent), board.sent
        assert board.queued("SYRD-902") == [], board.queued("SYRD-902")
        checks += 1

        # DROPPING IS NOT DELIVERING. `ack_notification` records delivery
        # accounting, and for an idle_reminder that means incrementing
        # idle_reminder_count -- which the next wave reads as "already reminded"
        # and turns into an escalation to the Director. Acking a reminder the
        # handoff answered would answer one false escalation by scheduling the
        # next (SYRD-32).
        board.reset_idle_reminders("SYRD-902", 0)
        board.generate_idle_wave("app")
        first_wave = board.queued("SYRD-902")
        assert [row["kind"] for row in first_wave] == ["idle_reminder"], first_wave
        assert first_wave[0]["target_role"] == "app", first_wave
        board.set_awaiting_role("SYRD-902", "director", caller="app")
        board.sent.clear()
        board.deliver(busy=False, rounds=4)
        assert not any(
            "owner has not advanced it" in message or "may be stuck" in message
            for _t, message in board.sent
        ), board.sent
        assert board.idle_reminder_count("SYRD-902") == 0, (
            "the dropped reminder was credited as delivered"
        )
        checks += 1

        # And the proof that the accounting matters: the next wave is still a
        # reminder to the owner, not an escalation to the Director.
        board.clear_awaiting_role("SYRD-902", caller="app")
        board.reset_idle_reminders("SYRD-902", board.idle_reminder_count("SYRD-902"))
        board.generate_idle_wave("app")
        second_wave = board.queued("SYRD-902")
        assert [row["kind"] for row in second_wave] == ["idle_reminder"], second_wave
        assert second_wave[0]["target_role"] == "app", second_wave
        checks += 1

        # A CLEARED WAIT suppresses nothing: the owner is answerable again.
        board.add_ticket("SYRD-903", "perf")
        board.set_awaiting_role("SYRD-903", "director", caller="perf")
        board.clear_awaiting_role("SYRD-903", caller="perf")
        board.enqueue_reminder("SYRD-903", "escalation", "director", "escalation:SYRD-903:director")
        board.sent.clear()
        board.deliver(busy=False, rounds=4)
        assert any(
            "owner has not advanced it" in message and "SYRD-903" in message
            for _t, message in board.sent
        ), board.sent
        checks += 1

        # AN EXPIRED WAIT suppresses nothing either: the window is what makes a
        # wait a wait, and the enqueue side uses the same predicate.
        board.add_ticket("SYRD-904", "main")
        board.set_awaiting_role("SYRD-904", "director", caller="main")
        board.age_wait("SYRD-904", "5 hours")
        board.enqueue_reminder("SYRD-904", "escalation", "director", "escalation:SYRD-904:director")
        # Older than the wait, so the only thing standing between this reminder
        # and a drop is the window having closed.
        board.backdate_queued("SYRD-904", "6 hours")
        board.sent.clear()
        board.deliver(busy=False, rounds=4)
        assert any(
            "owner has not advanced it" in message and "SYRD-904" in message
            for _t, message in board.sent
        ), board.sent
        checks += 1

        # A WAIT THAT PREDATES THE REMINDER does not eat it. Only a handoff made
        # after the wave was generated can have answered it; an older wait that is
        # still open must not silently swallow later legitimate work.
        board.add_ticket("SYRD-905", "research")
        board.set_awaiting_role("SYRD-905", "director", caller="research")
        board.enqueue_reminder("SYRD-905", "escalation", "director", "escalation:SYRD-905:director")
        board.sent.clear()
        board.deliver(busy=False, rounds=4)
        assert any(
            "owner has not advanced it" in message and "SYRD-905" in message
            for _t, message in board.sent
        ), board.sent
        checks += 1

        # A RECREATED WAIT supersedes the reminders queued before it, and only
        # those. Same ticket, so the contrast is exact: clear the wait, queue a
        # reminder, then hand off again. That reminder is now answered, and the
        # one above -- queued while the earlier wait was already open -- was not.
        board.clear_awaiting_role("SYRD-905", caller="research")
        board.enqueue_reminder("SYRD-905", "escalation", "director", "escalation:SYRD-905:director:2")
        board.set_awaiting_role("SYRD-905", "director", caller="research")
        board.sent.clear()
        board.deliver(busy=False, rounds=4)
        assert not any(
            "owner has not advanced it" in message for _t, message in board.sent
        ), board.sent
        assert [row for row in board.queued("SYRD-905") if row["kind"] == "escalation"] == []
        recreated = board.trace("SYRD-905")
        assert any(
            row["event"] == "drop" and row["busy_reason"] == "superseded_by_awaiting_role"
            for row in recreated
        ), recreated
        checks += 1

    print(f"ticket_board_superseded_reminder_postgres_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
