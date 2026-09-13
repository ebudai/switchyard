#!/usr/bin/env python3
"""SYRD-107: a held ticket still hands work over when it changes owner.

Manual control is a deliberate gate bypass and a deliberate silence: a ticket the
Director is steering by hand should not nudge anybody, should not be dragged
through configured gates, and should not announce every edit. What it also did
was suppress the one message that is not a reminder -- the handoff itself.

The live case (SYRD-96): Ops submitted for audit, the ticket moved from
in_progress/ops to audit/audit with the hold still set, and Audit was told
nothing. It sat idle knowing about the work only from session context, and the
Director had to hand-deliver a comment to wake it.

So: an actionable transition that changes WHO OWNS the ticket enqueues the
ordinary durable notification, held or not. Everything else manual control
silences stays silent, and each of those is a case below.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from scripts.ticket_board.workflow_config import validate
from temporary_cluster import temporary_cluster


def transitions(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(jsonb_build_object("
        "'target', target_role, 'message', message, 'kind', kind, 'dedupe', dedupe_key) ORDER BY id), '[]'::jsonb) "
        f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' AND kind = 'transition';",
    )
    return json.loads(raw)


def held(admin: str, ticket: str) -> bool:
    return t.psql(
        admin, f"SELECT manually_controlled::text FROM ticket_board.tickets WHERE id = '{ticket}';"
    ) == "true"


def hold(app, admin: str, ticket: str) -> None:
    """Put the ticket under manual control the way a Director does.

    Through the board's own operation rather than an UPDATE, so the hold is set
    by the path that really sets it. Creating a ticket announces it, which is a
    different path and not what any of this is about, so the queue is cleared
    afterwards: every assertion below is then a statement about the TRANSITION.
    """
    app.update_ticket(ticket, {"manually_controlled": True}, caller_role="director")
    t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")


MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu933_syrd107_manual_control_handoff.sql"
PREVIOUS_MIGRATION = "pgu932_syrd100_control_capabilities.sql"


def test_the_upgrade_carries_the_same_behaviour_to_an_existing_board(cluster) -> None:
    """A tenant that already exists gets this through the migration, or not at all.

    schema.sql is applied once, at install. The same handoff is driven on a
    board built from the release before this one and then upgraded, so the
    migration is what is under test rather than a copy of it.
    """
    db = "manual_control_upgraded"
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    def released(path: str) -> str:
        """One file as it stood in the release before this migration.

        Asked of the previous migration's own commit rather than of
        origin/main, which stopped describing "before" the moment this release
        merged -- and a test whose old tenant already carries the fix proves
        nothing about the upgrade.
        """
        adding = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
             f"scripts/ticket_board/migrations/{PREVIOUS_MIGRATION}"],
            text=True, capture_output=True, check=True,
        ).stdout.strip().splitlines()
        assert adding, PREVIOUS_MIGRATION
        return subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{adding[-1]}:scripts/ticket_board/{path}"],
            text=True, capture_output=True, check=True,
        ).stdout

    t.psql(admin, released("schema.sql"))
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    # That release's grants too, not this branch's: rbac.sql grants on the
    # functions of the schema beside it, and a tenant running the previous
    # release does not have this branch's yet.
    t.psql(admin, released("rbac.sql"))
    frames = cluster.root / f"frames-{db}"
    assets = cluster.root / f"assets-{db}"
    frames.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    app = t.TicketBoardApp(
        frames, assets, project="cerulean", ticket_prefix="PGU",
        database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
    )
    t.seed_postgres_ticket(admin, "PGU-1", title="Ops work", state="in_progress", assignee="ops")
    hold(app, admin, "PGU-1")

    # Before the upgrade: the defect, on the release this tenant is running.
    app.submit_to_audit_without_commit("PGU-1", "no code", caller_role="ops")
    assert transitions(admin, "PGU-1") == [], "the old release already notified"

    t.psql(admin, MIGRATION.read_text())

    t.seed_postgres_ticket(admin, "PGU-2", title="More ops work", state="in_progress", assignee="ops")
    hold(app, admin, "PGU-2")
    app.submit_to_audit_without_commit("PGU-2", "no code", caller_role="ops")
    queued = transitions(admin, "PGU-2")
    assert len(queued) == 1 and queued[0]["target"] == "audit", queued
    # Applying it twice is a no-op, the way the runner may replay a tail.
    t.psql(admin, MIGRATION.read_text())
    assert transitions(admin, "PGU-2") == queued


def board(cluster, db: str, *, declarative: bool):
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    t.psql(admin, t.RBAC_PATH.read_text())
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
        document = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
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
    return app, admin


def run_checks(app, admin: str, *, label: str, declarative: bool) -> None:
    def submit(ticket: str, reason: str) -> None:
        """The same action, through whichever entry point this board has.

        A declared board routes it through perform_workflow_action, which names
        the action the trigger checks; a legacy board calls the named function
        directly. Both are driven so the fix cannot hold on one path only.
        """
        if declarative:
            app.perform_workflow_action(
                ticket, "submit_to_audit_without_commit", {"reason": reason}, caller_role="ops"
            )
        else:
            app.submit_to_audit_without_commit(ticket, reason, caller_role="ops")

    # --- the live case: held, and the owner changes -----------------------
    t.seed_postgres_ticket(admin, "PGU-1", title="Ops work", state="in_progress", assignee="ops")
    hold(app, admin, "PGU-1")
    submit("PGU-1", "no code: the finding is the deliverable")

    ticket = app.get_ticket("PGU-1")
    assert ticket["state"] == "audit" and ticket["assignee"] == "audit", (label, ticket)
    # The hold follows the ticket: nothing here clears it, and this does not
    # change that. What changes is that the new owner is told.
    assert held(admin, "PGU-1"), label
    queued = transitions(admin, "PGU-1")
    assert len(queued) == 1, (label, queued)
    assert queued[0]["target"] == "audit", (label, queued)
    assert queued[0]["kind"] == "transition", (label, queued)
    assert "PGU-1" in queued[0]["message"], (label, queued)

    # --- narrowness: an edit that does not move the ticket -----------------
    app.update_ticket("PGU-1", {"title": "Ops work, retitled"}, caller_role="director")
    assert transitions(admin, "PGU-1") == queued, (label, "an in-place edit announced a transition")

    # --- narrowness: held, moves, but the owner does not change ------------
    # Driven with the Director's own move, which is what actually steers a held
    # ticket; the workflow actions belong to the roles that own the stages.
    # analysis and dat are both the Director's, in the legacy table and in the
    # declared document alike, so this crosses a stage without changing hands.
    t.seed_postgres_ticket(admin, "PGU-2", title="Stays with the Director", state="analysis", assignee="director")
    hold(app, admin, "PGU-2")
    app.force_move_ticket("PGU-2", "dat", "director", caller_role="director")
    assert app.get_ticket("PGU-2")["state"] == "dat", label
    assert transitions(admin, "PGU-2") == [], (label, transitions(admin, "PGU-2"))

    # --- narrowness: held, moves, and nobody owns the destination ----------
    t.seed_postgres_ticket(admin, "PGU-3", title="Parked", state="in_progress", assignee="ops")
    hold(app, admin, "PGU-3")
    app.force_move_ticket("PGU-3", "backlog", "unassigned", caller_role="director")
    assert app.get_ticket("PGU-3")["state"] == "backlog", label
    assert transitions(admin, "PGU-3") == [], (label, transitions(admin, "PGU-3"))

    # --- narrowness: a blocked ticket still says nothing -------------------
    t.seed_postgres_ticket(admin, "PGU-4", title="Blocked work", state="in_progress", assignee="ops")
    t.seed_postgres_ticket(admin, "PGU-5", title="The blocker", state="in_progress", assignee="app")
    hold(app, admin, "PGU-4")
    # Written directly: the blocker is the fixture, and set_blockers belongs to
    # the board's own writer rather than to this connection.
    t.psql(
        admin,
        "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved) "
        "VALUES ('PGU-4', 'PGU-5', 0, false);",
    )
    # A declared board refuses the move outright; the legacy path lets it
    # through and the trigger stays silent. Either way the invariant is the
    # same: a blocked ticket hands nothing to anybody.
    try:
        submit("PGU-4", "no code")
    except Exception as exc:  # noqa: BLE001 - the refusal is one of two outcomes
        assert "blocker" in str(exc), (label, exc)
    assert transitions(admin, "PGU-4") == [], (label, transitions(admin, "PGU-4"))

    # --- and an ordinary unheld ticket is unchanged ------------------------
    t.seed_postgres_ticket(admin, "PGU-6", title="Ordinary work", state="in_progress", assignee="ops")
    t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-6';")
    submit("PGU-6", "no code")
    ordinary = transitions(admin, "PGU-6")
    assert len(ordinary) == 1 and ordinary[0]["target"] == "audit", (label, ordinary)


def main() -> int:
    with temporary_cluster(prefix="manual-control-handoff-", shutdown="immediate") as cluster:
        # The same trigger serves both, and transition_target_role dispatches to
        # the declared workflow or the legacy table, so both are driven.
        legacy_app, legacy_admin = board(cluster, "manual_control_legacy", declarative=False)
        run_checks(legacy_app, legacy_admin, label="legacy", declarative=False)
        declared_app, declared_admin = board(cluster, "manual_control_declared", declarative=True)
        run_checks(declared_app, declared_admin, label="declarative", declarative=True)
        test_the_upgrade_carries_the_same_behaviour_to_an_existing_board(cluster)
    print("manual_control_owner_handoff_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
