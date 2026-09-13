#!/usr/bin/env python3
"""SYRD-120: work sitting in the Director's own stage that nobody was told about.

Live on 2026-09-13, SYRD-111, SYRD-118 and SYRD-119 were all in analysis with
assignee `unassigned`. `/api/board` drew all three cards in the analysis column,
which the declared workflow gives the Director to own, `ticket-board-read queue
director` returned nothing, and no notification had ever fired. The board was
displaying somebody's work while denying, when asked directly, that it was
theirs.

Both halves came from reading ASSIGNMENT where the actionable fact is OWNERSHIP
OF THE STAGE. The ordinary notification resolves a stage whose notify policy is
`assignee` to the assignee, and `unassigned` owns nothing; `needs_director()`
asked whether the ticket was assigned to the director. Neither was reading the
one thing that made the work actionable.

Real cluster, real declared workflow, real trigger paths, real listener, and the
real read client -- because the defect was three layers agreeing with each other
about the wrong question.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.read_client import director_payload, queue_payload  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "cerulean"


class Board:
    """One disposable board, and the few things these cases do to it."""

    def __init__(self, app: TicketBoardApp, admin: str, listener: str) -> None:
        self.app = app
        self.admin = admin
        self.listener = listener
        self.sent: list[tuple[str, str]] = []

    # -- what the board did --------------------------------------------------

    def create(self, title: str, *, state: str = "analysis", assignee: str = "unassigned") -> str:
        created = self.app.create_ticket(
            title=title, body="", screenshot=None, assignee=assignee,
            needs_user_signoff=False, state=state, caller_role="director",
        )
        return str(created["id"])

    def act(self, ticket_id: str, action: str, **payload: str) -> dict[str, Any]:
        return self.app.perform_workflow_action(ticket_id, action, payload, caller_role="director")

    def hold(self, ticket_id: str, held: bool) -> None:
        self.app.update_ticket(
            ticket_id, {"manually_controlled": held}, caller_role="director"
        )

    def force_move(self, ticket_id: str, state: str, assignee: str) -> None:
        self.app.force_move_ticket(ticket_id, state, assignee, caller_role="director")

    # -- what the board says -------------------------------------------------

    def stage(self, ticket_id: str) -> str:
        return t.psql(self.listener, f"""
SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket_id}';
""")

    def queued(self, ticket_id: str) -> list[dict[str, Any]]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(jsonb_agg(jsonb_build_object(
    'id', id, 'kind', kind, 'target_role', target_role, 'message', message,
    'state', payload ->> 'state', 'assignee', payload ->> 'assignee',
    'last_error', last_error
) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def triage_notices(self, ticket_id: str) -> list[dict[str, Any]]:
        return [row for row in self.queued(ticket_id) if row["kind"] == "triage"]

    def discarded(self, ticket_id: str) -> list[str]:
        raw = t.psql(self.listener, f"""
SELECT coalesce(jsonb_agg(coalesce(detail ->> 'reason', '') ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.notification_trace
WHERE ticket_id = '{ticket_id}' AND event = 'discard';
""")
        return json.loads(raw)

    def in_director_queue(self, ticket_id: str) -> bool:
        snapshot = self.app.snapshot()
        return any(
            row["id"] == ticket_id
            for row in queue_payload(snapshot, "director").get("director", [])
        )

    def in_director_attention(self, ticket_id: str) -> bool:
        return any(row["id"] == ticket_id for row in director_payload(self.app.snapshot()))

    # -- delivery ------------------------------------------------------------

    def deliver(self, *, busy: bool = False, rounds: int = 1) -> int:
        return self.deliver_with_gate(lambda _target: busy, rounds=rounds)

    def deliver_with_gate(self, gate: Callable[[str], bool], *, rounds: int = 1) -> int:
        """A fresh listener per round, as the SYRD-99 and SYRD-108 suites do.

        A listener under its budget waits on the channel for more work instead
        of returning, so each round gets its own; a fresh one each time is also
        the restart this has to survive.
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
            worked += listener.listen_once(max_notifications=4)
        return worked

    def end_busy_backoff(self, ticket_id: str) -> None:
        t.psql(self.admin, f"""
UPDATE ticket_board.ticket_notification_queue
SET next_attempt_at = clock_timestamp()
WHERE ticket_id = '{ticket_id}' AND last_error IS NOT NULL;
""")

    def delivered(self, ticket_id: str) -> list[str]:
        return [message for _target, message in self.sent if ticket_id in message]


def case_the_three_live_shapes(board: Board) -> int:
    """SYRD-111, SYRD-118 and SYRD-119 as they actually arrived."""
    subjects = [board.create(f"Live shape {index}") for index in range(3)]
    for subject in subjects:
        assert board.stage(subject) == "analysis/unassigned", board.stage(subject)
        notices = board.triage_notices(subject)
        assert len(notices) == 1, board.queued(subject)
        assert notices[0]["target_role"] == "director", notices[0]
        assert subject in notices[0]["message"] and "analysis" in notices[0]["message"], notices[0]
        # The board and the two read paths now say the same thing about it.
        assert board.in_director_attention(subject), subject
        assert board.in_director_queue(subject), subject
    board.deliver(rounds=3)
    for subject in subjects:
        assert len(board.delivered(subject)) == 1, board.sent
        assert board.triage_notices(subject) == [], board.queued(subject)
    return 1


def case_every_path_into_the_stage(board: Board) -> int:
    """The ways a ticket can arrive in an owned stage with no assignee.

    A declared transition cannot produce this shape: the executor forces the
    assignee to the destination's first owner, so release, reopen and the route
    back out of parking all land on somebody. The shape is still legal --
    `require_stage_owner_assignee` permits `unassigned` in an owned stage on
    purpose -- and the paths that bypass the executor are exactly the ones the
    three live tickets came in through. Both halves are asserted here: the
    declared paths announce through the ordinary notification and must not also
    raise a triage notice, and the others must.
    """
    # Create: how SYRD-111, SYRD-118 and SYRD-119 actually arrived.
    created = board.create("Created unassigned")
    assert board.stage(created) == "analysis/unassigned", board.stage(created)
    assert len(board.triage_notices(created)) == 1, board.queued(created)

    # A manual transition: the Director moving a ticket by hand, which is the
    # one move that is not the executor's.
    moved = board.create("Hand-moved", state="analysis", assignee="director")
    assert board.triage_notices(moved) == [], board.queued(moved)
    board.force_move(moved, "analysis", "unassigned")
    assert len(board.triage_notices(moved)) == 1, board.queued(moved)

    # The Director's generic edit, which can also take an assignee away.
    edited = board.create("Edited to unassigned", state="analysis", assignee="director")
    board.app.director_edit_ticket(
        edited, {"assignee": "unassigned"}, reason="unassigning for triage",
        caller_role="director",
    )
    assert board.stage(edited) == "analysis/unassigned", board.stage(edited)
    assert len(board.triage_notices(edited)) == 1, board.queued(edited)

    # Draft release: let out into analysis, and the executor gives it to the
    # stage owner. Assigned work is the ordinary notification's business,
    # whether or not it decides to say anything -- here it stays quiet because
    # the Director released it themselves -- and never this one's.
    drafted = board.create("Released draft", state="draft")
    assert board.triage_notices(drafted) == [], board.queued(drafted)
    board.act(drafted, "release_draft")
    assert board.stage(drafted) == "analysis/director", board.stage(drafted)
    assert board.triage_notices(drafted) == [], board.queued(drafted)

    # Return: work that was finished and is wanted again.
    returned = board.create("Returned work", state="analysis", assignee="director")
    board.force_move(returned, "done", "unassigned")
    board.act(returned, "reopen", reason="wanted again")
    assert board.stage(returned) == "analysis/director", board.stage(returned)
    assert board.triage_notices(returned) == [], board.queued(returned)

    # The way back out of parking.
    parked = board.create("Deferred then routed", state="analysis", assignee="director")
    board.act(parked, "defer")
    assert board.stage(parked) == "backlog/unassigned", board.stage(parked)
    assert board.triage_notices(parked) == [], board.queued(parked)
    board.act(parked, "route", reason="back off the shelf")
    assert board.stage(parked) == "analysis/director", board.stage(parked)
    assert board.triage_notices(parked) == [], board.queued(parked)
    return 1


def case_one_notice_per_arrival(board: Board) -> int:
    """Arrival, not residence, and not once per edit while it waits."""
    subject = board.create("Edited while waiting")
    assert len(board.triage_notices(subject)) == 1, board.queued(subject)
    for index in range(3):
        board.app.update_ticket(
            subject, {"comment": {"who": "director", "text": f"note {index}"}},
            caller_role="director",
        )
    board.app.update_ticket(subject, {"body": "rewritten"}, caller_role="director")
    assert len(board.triage_notices(subject)) == 1, board.queued(subject)

    # Delivered once -- and then the queue row is gone, which is where "once per
    # arrival" stops being free. An edit after delivery must not raise a second
    # notice: a ticket that sits untriaged is a handoff that was made, not a
    # reason to keep announcing it.
    board.deliver()
    assert len(board.delivered(subject)) == 1, board.sent
    assert board.triage_notices(subject) == [], board.queued(subject)
    board.app.update_ticket(
        subject, {"comment": {"who": "director", "text": "still thinking"}},
        caller_role="director",
    )
    board.app.update_ticket(subject, {"body": "rewritten again"}, caller_role="director")
    assert board.triage_notices(subject) == [], board.queued(subject)
    board.deliver(rounds=2)
    assert len(board.delivered(subject)) == 1, board.sent
    board.act(subject, "route", assignee="main")
    board.force_move(subject, "analysis", "unassigned")
    assert len(board.triage_notices(subject)) == 1, board.queued(subject)
    board.deliver()
    assert len(board.delivered(subject)) == 2, board.sent
    return 1


def case_acting_on_it_supersedes_the_notice(board: Board) -> int:
    """Routing, deferring and cancelling all answer the question it asks."""
    for action, payload, expected in (
        ("route", {"assignee": "main"}, "in_progress/main"),
        ("defer", {}, "backlog/unassigned"),
        ("cancel", {}, "cancelled/unassigned"),
    ):
        subject = board.create(f"Superseded by {action}")
        assert len(board.triage_notices(subject)) == 1, board.queued(subject)
        board.act(subject, action, **payload)
        assert board.stage(subject) == expected, (action, board.stage(subject))
        # Removed from the queue, with a reason, rather than left beside a
        # ticket that has moved on (SYRD-108's lesson).
        assert board.triage_notices(subject) == [], (action, board.queued(subject))
        assert "superseded_unassigned_triage" in board.discarded(subject), board.discarded(subject)
        # And nothing stale is delivered afterwards.
        before = len(board.sent)
        board.deliver(rounds=2)
        stale = [message for _target, message in board.sent[before:] if "no assignee" in message]
        assert stale == [], stale
    return 1


def case_a_deliberate_hold_is_silent(board: Board) -> int:
    """Manual control is the Director asking for silence, and it is honoured.

    This is the live sequence on SYRD-120 itself: the Director held the ticket
    to stop a stream of false nudges. A hold that silenced everything except
    this notice would have reproduced the thing it was applied to stop.
    """
    held = board.create("Held on purpose", state="analysis", assignee="director")
    board.hold(held, True)
    board.force_move(held, "analysis", "unassigned")
    assert board.triage_notices(held) == [], board.queued(held)
    assert board.deliver() == 0 or board.delivered(held) == [], board.sent

    # A hold applied after the notice takes the notice with it.
    late = board.create("Held after arriving")
    assert len(board.triage_notices(late)) == 1, board.queued(late)
    board.hold(late, True)
    assert board.triage_notices(late) == [], board.queued(late)

    # Clearing the hold is the moment the work becomes actionable again.
    board.hold(late, False)
    assert len(board.triage_notices(late)) == 1, board.queued(late)
    board.deliver()
    assert len(board.delivered(late)) == 1, board.sent
    return 1


def case_blocked_work_waits_for_its_blocker(board: Board) -> int:
    """Work that cannot start is not work anyone can triage yet."""
    blocker = board.create("Blocking work", state="analysis", assignee="director")
    dependent = board.create("Blocked work")
    board.app.update_ticket(
        dependent,
        {"blocked_by": [blocker], "blocked_reason": "waiting for the blocker"},
        caller_role="director",
    )
    board.deliver(rounds=2)
    board.sent.clear()
    assert board.triage_notices(dependent) == [], board.queued(dependent)
    return 1


def case_nobody_is_invented(board: Board) -> int:
    """Where there is no owner to derive, the board stays quiet.

    Each of these is reached by moving a ticket that HAS a notice into a stage
    that must not have one, so the case proves both halves at once: nothing new
    is raised, and the notice from the stage it left does not survive the move.
    """
    # Three roles own the implementation stage. Choosing one would be this code
    # making the assignment whose absence is the entire subject. That shape
    # cannot be built on this board at all -- an implementation stage refuses an
    # assignee none of its owners holds -- so the rule is asked of the database
    # directly, once per stage, which is also the clearest statement of it.
    derived = json.loads(t.psql(board.admin, """
SELECT coalesce(jsonb_object_agg(x->>'name',
    coalesce(ticket_board.unassigned_stage_owner(x->>'name', 'unassigned'), '')), '{}'::jsonb)::text
FROM jsonb_array_elements(ticket_board.declared_workflow()->'stages') x;
"""))
    assert derived["analysis"] == "director", derived
    assert derived["draft"] == "director", derived
    assert derived["inspection"] == "inspector", derived
    assert derived["in_progress"] == "", derived      # three owners: no answer to derive
    assert derived["user_review"] == "", derived      # owned, announces nobody, on purpose
    assert derived["backlog"] == "", derived          # parking is not a queue
    assert derived["done"] == "" and derived["cancelled"] == "", derived
    # And an assignee who does own the stage is not untriaged work.
    assert t.psql(board.admin, """
SELECT coalesce(ticket_board.unassigned_stage_owner('analysis', 'director'), '');
""") == ""

    # A stage the document says notifies nobody is a deliberate silence, not a
    # gap: user_review is owned and announces nothing on purpose.
    quiet = board.create("Awaiting the user")
    assert len(board.triage_notices(quiet)) == 1, board.queued(quiet)
    board.force_move(quiet, "user_review", "unassigned")
    assert board.triage_notices(quiet) == [], board.queued(quiet)

    # Parking is not a queue: deferred work waits for nobody and nudges nobody.
    parked = board.create("Parked")
    assert len(board.triage_notices(parked)) == 1, board.queued(parked)
    board.act(parked, "defer")
    assert board.stage(parked) == "backlog/unassigned", board.stage(parked)
    assert board.triage_notices(parked) == [], board.queued(parked)
    assert not board.in_director_queue(parked), "deferred work is not waiting for the director"

    # And an assigned ticket in the same stage is somebody's already: the
    # ordinary notification owns that case, and this one must not double it.
    assigned = board.create("Assigned triage", state="analysis", assignee="director")
    assert board.triage_notices(assigned) == [], board.queued(assigned)
    return 1


def case_a_hold_that_arrives_mid_flight_is_still_honoured(board: Board) -> int:
    """The second half of the deliberate silence, at the delivery end.

    Removing the queued notice covers the ordinary case, but a notice already
    claimed by a listener is out of the queue's reach: the row is deleted and
    the listener still holds what it read. The listener asks the same question
    again before it sends, which is where that window closes. The hold here is
    applied with transition notifications suppressed, so the trigger's removal
    never runs and delivery is the only thing left to refuse it.
    """
    subject = board.create("Held between claim and send")
    assert len(board.triage_notices(subject)) == 1, board.queued(subject)
    t.psql(board.admin, f"""
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT set_config('ticket_board.suppress_transition_notify', 'on', false);
UPDATE ticket_board.tickets SET manually_controlled = true WHERE id = '{subject}';
""")
    assert len(board.triage_notices(subject)) == 1, "the removal must not have run"
    board.deliver(rounds=2)
    assert board.delivered(subject) == [], board.sent
    assert board.triage_notices(subject) == [], board.queued(subject)
    return 1


def case_a_busy_pane_keeps_the_notice(board: Board) -> int:
    """Retries, busy deferral and restart recovery are the queue's, not a
    second mechanism's -- which is why this goes through the same queue."""
    subject = board.create("Announced to a busy pane")
    assert board.deliver(busy=True) == 0, board.sent
    assert board.delivered(subject) == [], board.sent
    deferred = [row for row in board.triage_notices(subject) if row["last_error"]]
    assert deferred, board.queued(subject)

    # A restart does not lose it and does not mint a second one.
    assert len(board.triage_notices(subject)) == 1, board.queued(subject)
    board.end_busy_backoff(subject)
    board.deliver(rounds=3)
    assert len(board.delivered(subject)) == 1, board.sent
    assert board.triage_notices(subject) == [], board.queued(subject)
    return 1


MIGRATIONS = ROOT / "scripts/ticket_board/migrations"
MINE = "pgu936_syrd120_unassigned_triage_notice.sql"
PREVIOUS = "pgu935_syrd118_proven_publication.sql"


def at_release(migration_name: str, path: str) -> str:
    """One file as it stood in the release that added `migration_name`."""
    adding = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
         f"scripts/ticket_board/migrations/{migration_name}"],
        text=True, capture_output=True, check=True,
    ).stdout.strip().splitlines()
    assert adding, migration_name
    return subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{adding[-1]}:{path}"],
        text=True, capture_output=True, check=True,
    ).stdout


def case_the_upgrade_carries_it_to_an_existing_board(cluster, tmpdir: Path) -> int:
    """schema.sql is applied once, at install, so an existing tenant gets this
    through the migration or not at all -- and the tickets that produced the
    report are already sitting in that state when it lands.
    """
    dbname = "syrd120_upgraded"
    admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
    service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
    listener_conninfo = t.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    t.psql(admin, at_release(PREVIOUS, "scripts/ticket_board/schema.sql"))
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    t.psql(admin, at_release(PREVIOUS, "scripts/ticket_board/rbac.sql"))

    frames = tmpdir / f"frames-{dbname}"
    assets = tmpdir / f"assets-{dbname}"
    frames.mkdir()
    assets.mkdir()
    app = TicketBoardApp(
        frames, assets, project=PROJECT, ticket_prefix="PGU", database_url=service,
    )
    cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
    app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
    board = Board(app, admin, listener_conninfo)

    # The board as it was: three tickets in the Director's stage, owned by
    # nobody, announced to nobody. This is the defect, on the release that has
    # it, driven through the same create path the live ones came in through.
    live = [board.create(f"Waiting since before the upgrade {index}") for index in range(3)]
    held = board.create("Held before the upgrade")
    board.hold(held, True)
    for subject in live:
        assert board.queued(subject) == [], board.queued(subject)

    names = [path.name for path in sorted(MIGRATIONS.glob("*.sql")) if path.name != MINE]
    t.psql(
        admin,
        "CREATE TABLE IF NOT EXISTS ticket_board.schema_migrations ("
        "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());\n"
        + "INSERT INTO ticket_board.schema_migrations(name) VALUES "
        + ",".join(f"('{name}')" for name in names)
        + " ON CONFLICT DO NOTHING;",
    )
    applied = subprocess.run(
        [str(ROOT / "scripts/ticket-board-migrate")],
        text=True, capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TICKET_BOARD_ADMIN_DATABASE_URL": admin,
        },
    )
    assert applied.returncode == 0, applied.stderr or applied.stdout
    assert f"apply {MINE}" in applied.stderr, applied.stderr
    t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

    # The backlog of already-waiting work is announced once each, and the
    # deliberate hold is still a deliberate hold.
    for subject in live:
        notices = board.triage_notices(subject)
        assert len(notices) == 1, (subject, board.queued(subject))
        assert notices[0]["target_role"] == "director", notices[0]
    assert board.triage_notices(held) == [], board.queued(held)

    # Applying the same migration again announces nothing twice.
    t.psql(admin, (MIGRATIONS / MINE).read_text(encoding="utf-8"))
    for subject in live:
        assert len(board.triage_notices(subject)) == 1, board.queued(subject)

    # And the upgraded board behaves like a fresh one from here on.
    after = board.create("Arrived after the upgrade")
    assert len(board.triage_notices(after)) == 1, board.queued(after)
    board.deliver(rounds=5)
    for subject in [*live, after]:
        assert len(board.delivered(subject)) == 1, board.sent
    assert board.delivered(held) == [], board.sent
    return 1


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd120-triage-", shutdown="immediate") as cluster:
        dbname = "syrd120_triage"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        listener_conninfo = t.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(prefix="syrd120-assets.") as tmpdir:
            root = Path(tmpdir)
            (root / "frames").mkdir()
            (root / "assets").mkdir()
            app = TicketBoardApp(
                root / "frames", root / "assets",
                project=PROJECT, ticket_prefix="PGU", database_url=service,
            )
            cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            board = Board(app, admin, listener_conninfo)

            for case in (
                case_the_three_live_shapes,
                case_every_path_into_the_stage,
                case_one_notice_per_arrival,
                case_acting_on_it_supersedes_the_notice,
                case_a_deliberate_hold_is_silent,
                case_a_hold_that_arrives_mid_flight_is_still_honoured,
                case_blocked_work_waits_for_its_blocker,
                case_nobody_is_invented,
                case_a_busy_pane_keeps_the_notice,
            ):
                board.sent.clear()
                checks += case(board)

            checks += case_the_upgrade_carries_it_to_an_existing_board(cluster, root)

    print(f"unassigned_triage_notice_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
