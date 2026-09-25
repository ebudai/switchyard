#!/usr/bin/env python3
"""SYRD-178: a held ticket's Inspector handoff is delivered, not thrown away.

SYRD-146 was submitted to inspection commit-exempt after its live rollout. The
Board moved it, assigned Inspector and enqueued the handoff. Nothing was ever
sent: `active_work_notified_at` stayed empty, the listener kept running with its
original PID, and the work sat there until the User noticed it.

The enqueue was never the problem. SYRD-107 had already settled that a held
ticket still announces a transition that changes hands -- and the listener then
dropped exactly those rows, because `_notification_is_current` voided every
transition for a ticket carrying `manually_controlled`. A Director steering a
live rollout by hand sets that flag, so the handoff out of a steered ticket was
discarded as stale on its first claim and on every retry after it.

Manual control is a deliberate silence for reminders. A transition notification
is not a reminder: it exists only because somebody other than the recipient
moved the ticket into a stage that role owns, and both of those were decided
when the row was written. Parked and blocked still stop delivery -- neither says
the work can be done now -- and that narrowness is a case below.

Both board kinds are driven. syrd's board carries a declared workflow, so a fix
proved only against the legacy tables would be a fix syrd never runs.
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

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import os  # noqa: E402

# The board resolves a submitted commit against its own copy of the project
# repository. Naming one this host really has is what lets the ordinary
# committed submission be driven here as the non-regression it is.
os.environ.setdefault("TICKET_BOARD_COMMIT_GIT_DIR", "/data/git/pgu.git")

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.notify_listener import ROLE_TO_TARGET, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

#: Where the listener sends, taken from the listener rather than spelled out:
#: the pane target carries the project this process is configured for, and a
#: literal here would assert the suite's environment instead of the delivery.
PROJECT = "cerulean"
INSPECTOR_PANE = ROLE_TO_TARGET["inspector"]
AUDIT_PANE = ROLE_TO_TARGET["audit"]
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


# --------------------------------------------------------------------------
# one board of each kind
# --------------------------------------------------------------------------


def board(cluster, db: str, *, declarative: bool):
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text(encoding="utf-8"))
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))
    frames = cluster.root / f"frames-{db}"
    assets = cluster.root / f"assets-{db}"
    frames.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    app = t.TicketBoardApp(
        frames,
        assets,
        project="cerulean",
        ticket_prefix="PGU",
        database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
    )
    if declarative:
        document = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text(encoding="utf-8")))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            t.post_json(
                f"http://127.0.0.1:{server.server_port}",
                "/api/tickets/actions/configure_workflow",
                {"document": document, "expected_revision": 0},
                caller="director",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    listener_conninfo = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
    return app, admin, listener_conninfo


# --------------------------------------------------------------------------
# what the board holds, read back rather than assumed
# --------------------------------------------------------------------------


def queued(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(jsonb_build_object('target', target_role, 'kind', kind, "
        "'message', message, 'state', payload->>'new_state') ORDER BY id), '[]'::jsonb)::text "
        f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';",
    )
    return json.loads(raw)


def trace(admin: str, ticket: str, *, events: tuple[str, ...] = ()) -> list[dict]:
    clause = ""
    if events:
        clause = " AND event IN (" + ", ".join(f"'{event}'" for event in events) + ")"
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(jsonb_build_object('event', event, 'kind', kind, "
        "'target', target_role, 'reason', busy_reason) ORDER BY id), '[]'::jsonb)::text "
        f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}'{clause};",
    )
    return json.loads(raw)


def notified_at(admin: str, ticket: str, role: str, state: str) -> str:
    """`active_work_notified_at` at its source: a durable `send` row.

    The board's own projection reads exactly this, which is why an empty value
    on SYRD-146 meant nothing had ever been delivered.
    """
    return t.psql(
        admin,
        "SELECT coalesce(max(ts)::text, '') FROM ticket_board.notification_trace "
        f"WHERE ticket_id = '{ticket}' AND target_role = '{role}' AND kind = 'transition' "
        f"AND event = 'send' AND ticket_state_at_event = '{state}';",
    )


def deliver(listener_conninfo: str, *, busy: bool = False) -> list[tuple[str, str]]:
    """One listener, one pass. A new one each time is what a restart is."""
    sent: list[tuple[str, str]] = []
    listener = TicketBoardNotifyListener(
        conninfo=listener_conninfo,
        # The project this board's declared workflow is written for. A listener
        # validates the document it reads against its own project, and on a
        # host those two agree because the tenant owns both.
        project=PROJECT,
        sender=lambda target, message: sent.append((target, message)),
        submission_witness=lambda target, _since: any(t == target for t, _m in sent),  # the pane takes what it is sent
        activity_gate=lambda _target: busy,
        poll_seconds=0,
        target_exists=lambda _target: True,
    )
    listener.listen_once(max_notifications=4)
    return sent


def role_went_idle(listener_conninfo: str, role: str) -> int:
    """The pane reported idle, through the product's own requeue path.

    A deferral for a busy pane is deliberately put into backoff; what ends that
    backoff on a live board is the listener seeing the role idle and calling
    this. Using it here is how the suite waits without sleeping out the retry.
    """
    return int(
        t.psql(
            listener_conninfo,
            "SELECT ticket_board.reset_notification_backoff_for_idle_roles("
            f"jsonb_build_object('{role}', clock_timestamp()::text));",
        )
    )


def hold(app, ticket: str) -> None:
    """Manual control, set the way a Director steering a rollout sets it.

    Through the board's own operation rather than an UPDATE, and deliberately
    without clearing the queue: the row already waiting there is the handoff
    this suite is about, and the live failure is precisely that holding the
    ticket is what killed it.
    """
    app.update_ticket(ticket, {"manually_controlled": True}, caller_role="director")


# --------------------------------------------------------------------------
# the cases
# --------------------------------------------------------------------------


def retire(app, admin: str, ticket: str) -> None:
    """Finish with a ticket so its implementer is free again.

    An implementer holds one piece of reserved work at a time; a board that
    still counts a ticket against a role parks the next one in backlog instead
    of letting it be submitted. Deferring is the Director's own way to put a
    ticket down, it is configured from every stage these cases reach, and a
    ticket in backlog is not reserved work.

    The Director's own override is what does it: leaving a review stage
    ordinarily requires that stage's sign-off, and a fixture putting a ticket
    down is exactly the out-of-band move `force_move` exists for. Its
    notification is suppressed, so nothing here can be mistaken for a handoff.
    """
    app.force_move_ticket(
        ticket, "backlog", "unassigned", suppress_notification=True, caller_role="director"
    )
    t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")


def run_checks(app, admin: str, listener_conninfo: str, *, label: str, declarative: bool) -> None:
    def submit_without_commit(ticket: str, role: str) -> None:
        reason = "live rollout completed; the deliverable is the host state"
        if declarative:
            app.perform_workflow_action(
                ticket, "submit_to_audit_without_commit", {"reason": reason}, caller_role=role
            )
        else:
            app.submit_to_audit_without_commit(ticket, reason, caller_role=role)

    def submit_with_commit(ticket: str, role: str, commit: str) -> None:
        if declarative:
            app.perform_workflow_action(ticket, "submit_to_audit", {"commit_hash": commit}, caller_role=role)
        else:
            app.update_ticket(
                ticket, {"state": "audit", "commit_hash": commit}, caller_role=role
            )

    def sign_off(ticket: str) -> None:
        """The Inspector's own sign-off, through this board's entry point.

        A legacy board takes it as the patch the server builds for that
        operation; a declared board names the action the workflow document
        configures. Both are driven, because syrd's board is the second.
        """
        if declarative:
            app.perform_workflow_action(ticket, "inspector_sign_off", {}, caller_role="inspector")
        else:
            app.update_ticket(
                ticket, {"state": "audit", "inspector_signoff": True}, caller_role="inspector"
            )

    def seed(ticket: str, assignee: str) -> None:
        t.seed_postgres_ticket(
            admin, ticket, title="Live rollout", state="in_progress",
            assignee=assignee, needs_inspection=True,
        )
        # Creating a ticket announces it. That is a different path; clearing it
        # here makes every assertion below a statement about the TRANSITION.
        t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")

    def fail(detail: str) -> str:
        return f"[{label}] {detail}"

    def announcement(ticket: str, stage: str) -> str:
        """What this board says when a ticket arrives in a review stage.

        A declared board words it from the stage's own label; the legacy tables
        carry the older phrasing. The wording is not what this suite is about,
        so it is taken from the board rather than asserted as a constant --
        what is asserted is that it is sent, once, to the right pane.
        """
        if declarative:
            return f"{ticket} -- Live rollout entered {stage.capitalize()}"
        return f"{ticket} -- Live rollout ready for {stage}"

    # 1. The live shape: commit-exempt submission to inspection, and the
    #    Director takes hold of the rollout while the handoff is still queued.
    seed("PGU-1", "ops")
    submit_without_commit("PGU-1", "ops")
    ticket = app.get_ticket("PGU-1")
    check(
        ticket["state"] == "inspection" and ticket["assignee"] == "inspector" and ticket["commit_exempt"],
        fail(f"submitted commit-exempt to inspection: {ticket['state']}/{ticket['assignee']}"),
    )
    rows = queued(admin, "PGU-1")
    check(
        [row["target"] for row in rows] == ["inspector"]
        and rows[0]["message"] == announcement("PGU-1", "inspection"),
        fail(f"one durable handoff, addressed to Inspector: {rows}"),
    )
    hold(app, "PGU-1")
    check(
        t.psql(admin, "SELECT manually_controlled::text FROM ticket_board.tickets WHERE id = 'PGU-1';") == "true",
        fail("the hold is set"),
    )

    sent = deliver(listener_conninfo)
    check(
        sent == [(INSPECTOR_PANE, announcement("PGU-1", "inspection"))],
        fail(f"the Inspector is told, held or not: {sent}"),
    )
    check(notified_at(admin, "PGU-1", "inspector", "inspection") != "", fail("and the send is durable evidence"))
    check(queued(admin, "PGU-1") == [], fail("delivery drains the queue"))
    check(trace(admin, "PGU-1", events=("drop",)) == [], fail(f"nothing dropped: {trace(admin, 'PGU-1')}"))

    # 2. Exactly once. A second pass -- a fresh listener, which is what a
    #    restart is -- sends nothing and rewrites no evidence.
    first = notified_at(admin, "PGU-1", "inspector", "inspection")
    check(deliver(listener_conninfo) == [], fail("a second pass has nothing to send"))
    check(notified_at(admin, "PGU-1", "inspector", "inspection") == first, fail("and changes nothing"))

    # 3. An Inspector that is actually working is not interrupted, and the
    #    handoff survives both the deferral and the listener that deferred it.
    seed("PGU-2", "app")
    submit_without_commit("PGU-2", "app")
    hold(app, "PGU-2")
    check(deliver(listener_conninfo, busy=True) == [], fail("a busy Inspector is not nudged"))
    check(len(queued(admin, "PGU-2")) == 1, fail(f"the handoff is still queued: {queued(admin, 'PGU-2')}"))
    check(notified_at(admin, "PGU-2", "inspector", "inspection") == "", fail("and nothing is recorded as sent"))
    check(trace(admin, "PGU-2", events=("gate_defer",)) != [], fail(f"the deferral is traced: {trace(admin, 'PGU-2')}"))
    check(role_went_idle(listener_conninfo, "inspector") == 1, fail("the idle pane releases exactly that row"))
    sent = deliver(listener_conninfo)
    check(
        sent == [(INSPECTOR_PANE, announcement("PGU-2", "inspection"))],
        fail(f"delivered once the pane is idle: {sent}"),
    )
    check(deliver(listener_conninfo) == [], fail("exactly once, across the interruption"))
    retire(app, admin, "PGU-2")

    # 4. Inspector sign-off hands the work on, once. A held ticket is the
    #    Director's to move, so the ordinary ticket is the one that advances.
    seed("PGU-3", "main")
    submit_without_commit("PGU-3", "main")
    check(deliver(listener_conninfo) == [(INSPECTOR_PANE, announcement("PGU-3", "inspection"))],
          fail("the Inspector is told about the ordinary ticket too"))
    sign_off("PGU-3")
    after = app.get_ticket("PGU-3")
    check(after["state"] == "audit" and after["inspector_signoff"], fail(f"signed off into audit: {after['state']}"))
    rows = queued(admin, "PGU-3")
    check([row["target"] for row in rows] == ["audit"], fail(f"one Audit handoff: {rows}"))
    sent = deliver(listener_conninfo)
    check(sent == [(AUDIT_PANE, announcement("PGU-3", "audit"))], fail(f"Audit is told: {sent}"))
    check(deliver(listener_conninfo) == [], fail("exactly once"))
    check(notified_at(admin, "PGU-3", "audit", "audit") != "", fail("durable evidence for Audit too"))

    # 5. Non-regression: an ordinary committed submission is unchanged.
    retire(app, admin, "PGU-3")
    seed("PGU-4", "app")
    # A commit the board can actually resolve: it validates the hash against
    # its own copy of the project repository, and a made-up one is refused
    # before any of this is reached.
    commit = t.main_commit()
    submit_with_commit("PGU-4", "app", commit)
    ordinary = app.get_ticket("PGU-4")
    check(
        ordinary["state"] == "inspection" and not ordinary["commit_exempt"]
        and ordinary["commit_hash"] == commit,
        fail(f"committed submission: {ordinary['state']} {ordinary['commit_hash']}"),
    )
    check(deliver(listener_conninfo) == [(INSPECTOR_PANE, announcement("PGU-4", "inspection"))],
          fail("and it is delivered the same way"))

    # 6. A stale stage announcement is still dropped. An arrival message left
    #    queued does not survive the ticket moving on.
    retire(app, admin, "PGU-4")
    seed("PGU-5", "main")
    t.psql(
        admin,
        """
SELECT ticket_board.enqueue_notification(
    'PGU-5', 'transition', 'main', 'PGU-5 -- Live rollout is active again',
    jsonb_build_object('kind', 'transition', 'id', 'PGU-5', 'new_state', 'in_progress',
                       'assignee', 'main', 'target_role', 'main'),
    'transition:PGU-5:stale-stage'
);
""",
    )
    submit_without_commit("PGU-5", "main")
    hold(app, "PGU-5")
    sent = deliver(listener_conninfo)
    check(
        sent == [(INSPECTOR_PANE, announcement("PGU-5", "inspection"))],
        fail(f"only the stage the ticket is actually in is announced: {sent}"),
    )
    stale = [row for row in trace(admin, "PGU-5", events=("drop",)) if row["reason"] == "stale_notification"]
    check(stale != [], fail(f"and the stale one is dropped, with its reason: {trace(admin, 'PGU-5')}"))

    # 7. Narrowness: a held ticket's REMINDERS stay silent. Manual control is
    #    still a deliberate silence for everything that is not a handoff.
    # PGU-1 is the held ticket from case 1, sitting in inspection with its
    # handoff already delivered. A reminder about it is the thing manual
    # control is for, and it stays silent.
    t.psql(
        admin,
        """
SELECT ticket_board.enqueue_notification(
    'PGU-1', 'nudge', 'inspector', 'NUDGE PGU-1 -- Live rollout is waiting in inspection',
    jsonb_build_object('kind', 'nudge', 'id', 'PGU-1', 'new_state', 'inspection',
                       'assignee', 'inspector', 'target_role', 'inspector'),
    'nudge:PGU-1:held'
);
""",
    )
    check(deliver(listener_conninfo) == [], fail("a held ticket does not nudge anybody"))
    check(
        [row for row in trace(admin, "PGU-1", events=("drop",)) if row["kind"] == "nudge"] != [],
        fail(f"and the silence is recorded rather than lost: {trace(admin, 'PGU-1')}"),
    )

    # 8. Narrowness: a blocked ticket still hands nothing over. A dependency
    #    that has not resolved is not a ticket anybody can act on, and that
    #    rule is older than this one (SYRD-148).
    #
    #    Parking is not tested beside it because a ticket cannot be parked
    #    here: the board forces `parked = false` on any update that leaves
    #    backlog, so an inspection ticket carrying it is a state this board
    #    does not have. It stays in the delivery gate as the backlog-stage
    #    rule it is.
    retire(app, admin, "PGU-5")
    seed("PGU-8", "app")
    submit_without_commit("PGU-8", "app")
    t.seed_postgres_ticket(admin, "PGU-9", title="Open blocker", state="analysis", assignee="unassigned")
    # Creating the blocker announces it to the Director; that is a different
    # path, and leaving it queued would be delivered by the pass below.
    t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-9';")
    t.psql(
        admin,
        "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position) "
        "VALUES ('PGU-8', 'PGU-9', 1);",
    )
    check(deliver(listener_conninfo) == [], fail("a blocked ticket hands nothing over"))
    check(notified_at(admin, "PGU-8", "inspector", "inspection") == "", fail("and records no send"))

    # 9. Every row the trace wrote for the live shape is accounted for: the
    #    handoff was enqueued once and sent once, and nothing else happened.
    explained = [
        (row["event"], row["kind"], row["target"], row["reason"])
        for row in trace(admin, "PGU-1", events=("enqueue", "send", "drop", "gate_defer"))
    ]
    check(
        explained == [
            # The ticket's own arrival, announced to its implementer when the
            # fixture created it. Its queue row is the one `seed` drops, which
            # is why nothing was sent for it; the trace keeps the record.
            ("enqueue", "transition", "ops", None),
            # The handoff: enqueued by the submission, delivered to an idle
            # Inspector, held ticket and all.
            ("enqueue", "transition", "inspector", None),
            ("send", "transition", "inspector", "idle"),
            # The reminder from case 7: enqueued, and silenced by the hold.
            ("enqueue", "nudge", "inspector", None),
            ("drop", "nudge", "inspector", "stale_notification"),
        ],
        fail(f"the trace explains every enqueue, suppression, retry and delivery: {explained}"),
    )


def main() -> int:
    with temporary_cluster(prefix="syrd178-inspector-handoff.") as cluster:
        for declarative in (False, True):
            label = "declared" if declarative else "legacy"
            app, admin, listener_conninfo = board(
                cluster, f"syrd178_{label}", declarative=declarative
            )
            run_checks(app, admin, listener_conninfo, label=label, declarative=declarative)
    print(f"inspector_handoff_delivery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
