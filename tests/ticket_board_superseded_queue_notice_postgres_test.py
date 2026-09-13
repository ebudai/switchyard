#!/usr/bin/env python3
"""SYRD-108: a serial-focus queue notice the Director has already overtaken.

Live on SYRD-107: the Director routed one analysis ticket three times while
every implementer held a reservation. The announcement queued for App behind
SYRD-91 was still waiting when later routes rewrote the ticket to Ops/SYRD-106
and then to Main/SYRD-93. It was delivered anyway, six busy retries later,
telling the Director to wait for a reservation nobody was waiting on -- and the
intermediate Ops announcement sat in the queue beside the current Main one.

Nothing in the ordinary currentness check could have caught it. A queue
announcement is a `ticket_update` for the Director, and the ticket never moves:
the redirect puts it back in the same holding stage with the same assignee
every time. Ticket id, state and assignee are identical across all three, so
all three read as current. What distinguishes them is the queue identity the
announcement was written for, and that is what `queued_for_assignee` and
`queued_behind_ticket` record.

Run against a real cluster with a real configured workflow, because the
announcement is written by a trigger from a redirect the database decides. A
fake connection would assert the shape of a query rather than which of three
indistinguishable notifications the board still means.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

# Every pane this suite could open would otherwise ask the live tenant's
# systemd user manager for a transient scope (SYRD-55).
from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "cerulean"
SUBJECT = "PGU-9"
#: One reserved implementation ticket per implementer, so every route of the
#: subject is redirected rather than accepted. These are the SYRD-91/106/93 of
#: the live reproduction.
RESERVATIONS = (("PGU-1", "app"), ("PGU-2", "ops"), ("PGU-3", "main"))


class Board:
    """One disposable board, and the few things these cases do to it."""

    def __init__(self, app: TicketBoardApp, admin: str, listener: str) -> None:
        self.app = app
        self.admin = admin
        self.listener = listener
        self.sent: list[tuple[str, str]] = []

    def route(self, ticket_id: str, assignee: str) -> dict[str, Any]:
        """The Director's ordinary route, through the action the board declares.

        Not a hand-written UPDATE: the redirect, the durable queue fields and
        the announcement are all decided inside that call, and writing any of
        them here would be asserting my own arithmetic back to me.
        """
        return self.app.perform_workflow_action(
            ticket_id, "route", {"assignee": assignee}, caller_role="director"
        )

    def urgent_comment(self, ticket_id: str, text: str, *, who: str) -> None:
        """A comment that notifies the Director, in the Director's own stage.

        Two things have to be true for a comment to reach anyone here. A role is
        not told about its own comment, so this one comes from an implementer
        rather than the Director; and an implementer's ordinary comment does not
        notify at all, so it is marked urgent. What matters for the control is
        that the result is a plain `ticket_update` for the Director with no
        queue identity in it, in exactly the analysis/director state the
        superseded announcements were sitting in.
        """
        self.app.update_ticket(
            ticket_id,
            {"comment": {"who": who, "text": text, "urgent": True}},
            caller_role=who,
        )

    def queue_identity(self, ticket_id: str = SUBJECT) -> tuple[str, str]:
        raw = t.psql(self.listener, f"""
SELECT queued_for_assignee || '|' || queued_behind_ticket
FROM ticket_board.tickets WHERE id = '{ticket_id}';
""")
        queued_for, _, behind = raw.partition("|")
        return queued_for, behind

    def stage(self, ticket_id: str = SUBJECT) -> str:
        return t.psql(self.listener, f"""
SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';
""")

    def queued(self, ticket_id: str = SUBJECT) -> list[dict[str, Any]]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', id, 'kind', kind, 'target_role', target_role, 'last_error', last_error,
    'queued_for', payload ->> 'queued_for', 'reserved_by', payload ->> 'reserved_by'
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def trace(self, ticket_id: str = SUBJECT) -> list[dict[str, Any]]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'notification_id', notification_id,
    'event', event, 'kind', kind, 'busy_reason', busy_reason,
    'phase', detail ->> 'phase', 'reason', detail ->> 'reason',
    'announced_queued_for', detail ->> 'announced_queued_for',
    'announced_reserved_by', detail ->> 'announced_reserved_by',
    'current_queued_for', detail ->> 'current_queued_for',
    'current_reserved_by', detail ->> 'current_reserved_by'
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.notification_trace WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def idle_reminder_count(self, ticket_id: str = SUBJECT) -> int:
        return int(t.psql(self.listener, f"""
SELECT idle_reminder_count FROM ticket_board.ticket_notification_state
WHERE ticket_id = '{ticket_id}';
"""))

    def end_busy_backoff(self, ticket_id: str = SUBJECT) -> None:
        """The pane went idle, so the listener returns to what it deferred."""
        t.psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp()
WHERE ticket_id = '{ticket_id}' AND last_error IS NOT NULL;
""")

    def deliver(self, *, busy: bool, rounds: int = 1) -> int:
        return self.deliver_with_gate(lambda _target: busy, rounds=rounds)

    def deliver_with_gate(self, gate: Callable[[str], bool], *, rounds: int = 1) -> int:
        """Work the queue, with the caller deciding what the pane looks like.

        A fresh listener per round, each allowed one delivery, for the reason
        the SYRD-99 suite records: a listener under its budget waits on the
        channel for more instead of returning. The gate runs between claiming a
        notification and sending it, which is where a reroute lands on a real
        host -- the Director is at the composer, and the probe watches them.
        """
        worked = 0
        for _round in range(rounds):
            listener = TicketBoardNotifyListener(
                conninfo=self.listener,
                project=PROJECT,
                sender=lambda target, message: self.sent.append((target, message)),
                activity_gate=gate,
                poll_seconds=0,
                target_exists=lambda _target: True,
            )
            worked += listener.listen_once(max_notifications=1)
        return worked


def queue_messages(board: Board, who: str) -> list[str]:
    return [message for _target, message in board.sent if f"is queued for {who}" in message]


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd108-queue-notice-", shutdown="immediate") as cluster:
        dbname = "syrd108_queue_notice"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        listener_conninfo = t.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))
        for ticket_id, owner in RESERVATIONS:
            t.seed_postgres_ticket(
                admin, ticket_id, title="Reserved work", state="in_progress",
                assignee=owner, commit_exempt=True,
            )
        t.seed_postgres_ticket(
            admin, SUBJECT, title="Queued subject", state="analysis", assignee="director"
        )

        with tempfile.TemporaryDirectory(prefix="syrd108-assets.") as tmpdir:
            root = Path(tmpdir)
            (root / "frames").mkdir()
            (root / "assets").mkdir()
            app = TicketBoardApp(
                root / "frames", root / "assets",
                project=PROJECT, ticket_prefix="PGU", database_url=service,
            )
            cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
            # The live board holds a queued ticket in analysis/director, which
            # is what makes every announcement share a state and an assignee.
            # Queueing somewhere the Director does not own would hide the bug
            # behind the ordinary target-role check instead of exposing it.
            cfg["queue"] = {"stage": "analysis", "assignee": "director"}
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            board = Board(app, admin, listener_conninfo)

            # THE LIVE SEQUENCE. App, then Ops, then Main, with the Director's
            # pane busy throughout so nothing is delivered in between.
            board.route(SUBJECT, "app")
            assert board.stage() == "analysis/director", board.stage()
            assert board.queue_identity() == ("app", "PGU-1"), board.queue_identity()
            checks += 1

            assert board.deliver(busy=True) == 0, board.sent
            assert board.sent == [], board.sent
            deferred = [row for row in board.queued() if row["last_error"] == "pane busy"]
            assert deferred, board.queued()
            checks += 1

            board.route(SUBJECT, "ops")
            board.route(SUBJECT, "main")
            # Three announcements, indistinguishable by everything the old check
            # looked at, and the ticket has not moved once.
            announcements = [row for row in board.queued() if row["queued_for"]]
            assert [row["queued_for"] for row in announcements] == ["app", "ops", "main"], announcements
            assert board.stage() == "analysis/director", board.stage()
            assert board.queue_identity() == ("main", "PGU-3"), board.queue_identity()
            checks += 1

            # The pane goes idle. Only the instruction the board still means
            # may be delivered, and only once.
            board.end_busy_backoff()
            board.sent.clear()
            board.deliver(busy=False, rounds=8)
            assert queue_messages(board, "app") == [], board.sent
            assert queue_messages(board, "ops") == [], board.sent
            assert len(queue_messages(board, "main")) == 1, board.sent
            checks += 1

            # Removed, not left behind: the Ops notice in the live report sat in
            # the queue beside the current one.
            assert [row for row in board.queued() if row["queued_for"]] == [], board.queued()
            checks += 1

            # And it says which identity it was written for and which one the
            # board holds now, distinguishably from a busy pane and from a
            # ticket that simply moved.
            trace = board.trace()
            dropped = [
                row for row in trace
                if row["event"] == "drop" and row["busy_reason"] == "superseded_serial_focus_queue"
            ]
            assert len(dropped) == 2, trace
            assert {row["announced_queued_for"] for row in dropped} == {"app", "ops"}, dropped
            assert {row["announced_reserved_by"] for row in dropped} == {"PGU-1", "PGU-2"}, dropped
            assert {row["current_queued_for"] for row in dropped} == {"main"}, dropped
            assert {row["current_reserved_by"] for row in dropped} == {"PGU-3"}, dropped
            checks += 1

            discards = [
                row for row in trace
                if row["event"] == "listener_discard" and row["reason"] == "superseded_serial_focus_queue"
            ]
            assert len(discards) == 2, trace
            # Never acked, and asserted on the row the database writes for
            # itself rather than the one the listener asks for: `discard_notification`
            # records `discard` and `ack_notification` records `ack`, so this is
            # what distinguishes removing a notification from crediting it as
            # delivered (SYRD-32, SYRD-99).
            discarded_ids = {
                row["notification_id"] for row in trace
                if row["event"] == "discard" and row["busy_reason"] == "superseded_serial_focus_queue"
            }
            assert len(discarded_ids) == 2, trace
            # No ack anywhere in the life of either of those notifications. The
            # current announcement beside them is acked, which is what makes
            # this an assertion about the superseded ones rather than about
            # ticket_update notifications in general.
            assert not any(
                row["event"] in {"ack", "listener_ack"} and row["notification_id"] in discarded_ids
                for row in trace
            ), [row for row in trace if row["notification_id"] in discarded_ids]
            assert not any(row["busy_reason"] == "stale_notification" for row in trace), trace
            checks += 1

            # The drop creates nothing on its own, and touches no reminder
            # counter: a queue notice is not a reminder and must not be credited
            # to the Director as one.
            assert board.idle_reminder_count() == 0, board.idle_reminder_count()
            assert board.queued() == [], board.queued()
            checks += 1

            # Busy retry is preserved for a notice that is still current: the
            # repair must drop superseded announcements, not deferred ones.
            board.route(SUBJECT, "app")
            board.sent.clear()
            assert board.deliver(busy=True) == 0, board.sent
            still_queued = [row for row in board.queued() if row["queued_for"] == "app"]
            assert still_queued and still_queued[0]["last_error"] == "pane busy", board.queued()
            busy_defers = [row for row in board.trace() if row["event"] == "gate_defer"]
            assert any(row["busy_reason"] == "busy" for row in busy_defers), busy_defers
            checks += 1

            # THE RACE. Claimed while the App identity was current, rerouted to
            # Ops during the activity probe, so only the recheck immediately
            # before the send can catch it.
            board.end_busy_backoff()
            board.sent.clear()
            rerouted: list[str] = []

            def reroute_during_probe(_target: str) -> bool:
                if not rerouted:
                    rerouted.append("ops")
                    board.route(SUBJECT, "ops")
                return False

            board.deliver_with_gate(reroute_during_probe, rounds=1)
            assert rerouted == ["ops"], rerouted
            assert queue_messages(board, "app") == [], board.sent
            raced = [
                row for row in board.trace()
                if row["event"] == "drop"
                and row["busy_reason"] == "superseded_serial_focus_queue"
                and row["phase"] == "pre_send_recheck"
            ]
            assert raced, board.trace()
            assert raced[-1]["announced_queued_for"] == "app", raced
            assert raced[-1]["current_queued_for"] == "ops", raced
            checks += 1

            # THE CONTROL. An ordinary comment notification, in the same
            # unchanged analysis/director state, is not a queue announcement and
            # must still be delivered.
            t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{SUBJECT}';")
            board.sent.clear()
            board.urgent_comment(SUBJECT, "Ops note that is not a queue announcement.", who="ops")
            plain = [row for row in board.queued() if not row["queued_for"]]
            assert plain, board.queued()
            board.deliver(busy=False, rounds=4)
            # The board sends its own summary rather than the comment body,
            # which is the point: this delivery is indistinguishable from a
            # superseded announcement by state, assignee and target role, and
            # is kept only because it carries no queue identity.
            assert any(
                "new comment" in message and "is queued for" not in message
                for _target, message in board.sent
            ), board.sent
            assert not any(
                row["event"] == "drop" and row["busy_reason"] == "superseded_serial_focus_queue"
                for row in board.trace()[-4:]
            ), board.trace()[-4:]
            checks += 1

            # THE HOLD ENDING. The Director gives up on placing it and defers
            # it instead. That route is not held, so the board clears the queue
            # bookkeeping -- and an announcement still waiting to say "route it
            # again once App frees up" is now describing a queue that does not
            # exist.
            t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{SUBJECT}';")
            board.route(SUBJECT, "app")
            assert board.queue_identity() == ("app", "PGU-1"), board.queue_identity()
            pending = [row for row in board.queued() if row["queued_for"] == "app"]
            assert pending, board.queued()
            board.app.perform_workflow_action(SUBJECT, "defer", {}, caller_role="director")
            assert board.stage() == "backlog/unassigned", board.stage()
            assert board.queue_identity() == ("", ""), board.queue_identity()
            board.sent.clear()
            board.deliver(busy=False, rounds=4)
            assert queue_messages(board, "app") == [], board.sent
            assert [row for row in board.queued() if row["queued_for"]] == [], board.queued()
            ended = [
                row for row in board.trace()
                if row["event"] == "drop"
                and row["busy_reason"] == "superseded_serial_focus_queue"
                and row["current_queued_for"] == ""
            ]
            assert ended, board.trace()[-6:]
            checks += 1

    print(f"ticket_board_superseded_queue_notice_postgres_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
