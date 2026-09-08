#!/usr/bin/env python3
"""SYRD-77: the director's ordinary same-stage reassignment, end to end.

Against real PostgreSQL and the real HTTP boundary, because every layer that
refused this operation before refused it for a different reason: the capability
gate in the server, the field allow-list in edit_fields, and the configured
transition check in the database. A test that stubs any of them proves nothing.
"""

import copy
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import write_client  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402


REASON = "Main is not running; App picks this up."


def rejected(fn, reason=""):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- the refusal itself is the assertion
        if reason:
            assert reason in str(exc), str(exc)
        return str(exc)
    raise AssertionError(f"expected rejection containing {reason!r}")


def queued_messages(admin, ticket_id, role):
    raw = t.psql(
        admin,
        "SELECT message FROM ticket_board.ticket_notification_queue "
        f"WHERE ticket_id='{ticket_id}' AND target_role='{role}' ORDER BY id;",
    )
    return [line for line in raw.splitlines() if line]


def notification_kinds(admin, ticket_id, role):
    raw = t.psql(
        admin,
        "SELECT kind FROM ticket_board.ticket_notification_queue "
        f"WHERE ticket_id='{ticket_id}' AND target_role='{role}' ORDER BY id;",
    )
    return [line for line in raw.splitlines() if line]


def clear_queue(admin, ticket_id=None):
    where = f" WHERE ticket_id='{ticket_id}'" if ticket_id else ""
    t.psql(admin, f"DELETE FROM ticket_board.ticket_notification_queue{where};")


def gates(ticket):
    return {
        key: ticket[key]
        for key in (
            "needs_audit",
            "needs_inspection",
            "needs_user_signoff",
            "audit_signoff",
            "inspector_signoff",
            "user_signoff",
            "commit_hash",
            "commit_exempt",
            "workflow_flags",
        )
    }


def director_capabilities(document):
    return next(role for role in document["roles"] if role["name"] == "director")["capabilities"]


def reassignment_comments(ticket):
    return [
        comment
        for comment in ticket["comments"]
        if comment["text"].startswith("Director reassignment in ")
    ]


def main():
    # The CLI resolves a Unix socket from the environment when none is passed.
    # This suite talks to its own throwaway board and must never reach a live one.
    for name in ("TICKET_BOARD_SOCKET", "PGU_TICKET_BOARD_SOCKET"):
        os.environ.pop(name, None)
    os.environ["TICKET_BOARD_TEST_MODE"] = "1"

    with temporary_cluster(prefix="reassign-db-", shutdown="immediate") as cluster:
        root = cluster.root
        sock = cluster.socket_dir
        port = cluster.port
        db = "reassign_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())

        # One implementation ticket per implementer: the serial reservation is
        # the thing under test, so the fixture must not start out violating it.
        t.seed_postgres_ticket(
            admin, "PGU-1", title="Runtime handoff", state="in_progress", assignee="main", commit_exempt=True
        )
        t.seed_postgres_ticket(
            admin, "PGU-2", title="App active work", state="in_progress", assignee="app", commit_exempt=True
        )
        t.seed_postgres_ticket(admin, "PGU-3", title="Waiting on triage", state="analysis", assignee="director")
        t.seed_postgres_ticket(admin, "PGU-4", title="Unreleased draft", state="draft")

        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        document = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            run_checks(app, admin, base, server.write_token, document)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    print("ticket_board_director_reassign_test: ok")
    return 0


def run_checks(app, admin, base, write_token, document):
    def reassign(ticket_id, assignee, *, reason=REASON, caller="director"):
        return app.reassign_ticket(ticket_id, assignee, reason=reason, caller_role=caller)

    # ACCEPTANCE -- no worker anywhere is registered. Ownership is board state,
    # not a live process, so every reassignment below runs with the outgoing and
    # incoming panes equally absent. This is the SYRD-66 case that started it:
    # handing work away from a role that is not running.
    assert t.psql(admin, "SELECT count(*) FROM ticket_board.role_runtime_assignments;") == "0"

    # ACCEPTANCE -- only the director. Each refusal is atomic: the ticket keeps
    # its owner, and no comment or notification is left behind.
    for role in ("main", "app", "ops", "audit", "inspector", "user"):
        rejected(lambda role=role: reassign("PGU-1", "ops", caller=role), "cannot call reassign")
    assert app.get_ticket("PGU-1")["assignee"] == "main"
    assert reassignment_comments(app.get_ticket("PGU-1")) == []
    # An unattributed call is refused rather than falling back to the service
    # identity: an ownership change nobody signed is not an ownership change.
    # The configured-actor boundary catches this before reassign's own check.
    rejected(lambda: reassign("PGU-1", "ops", caller=None), "invalid configured caller role")
    assert app.get_ticket("PGU-1")["assignee"] == "main"

    # ACCEPTANCE -- the operation refuses what it cannot do honestly.
    rejected(lambda: reassign("PGU-1", "ops", reason="   "), "requires a reason")
    rejected(lambda: reassign("PGU-1", "main"), "already assigned to main")
    rejected(lambda: reassign("PGU-1", "nobody"), "invalid assignee")
    rejected(lambda: reassign("PGU-404", "ops"), "ticket not found")
    rejected(lambda: reassign("PGU-4", "ops"), "release_draft")
    # It never moves a ticket to suit an owner: analysis belongs to the
    # director, so an implementer there is a refusal, not a relocation.
    rejected(lambda: reassign("PGU-3", "app"), "is not an owner of stage analysis")
    assert app.get_ticket("PGU-3")["assignee"] == "director"

    # ACCEPTANCE -- the ordinary case: same stage, same gates, new owner.
    before = app.get_ticket("PGU-1")
    clear_queue(admin)
    reassigned = reassign("PGU-1", "ops")
    assert (reassigned["state"], reassigned["assignee"]) == ("in_progress", "ops"), reassigned
    assert gates(reassigned) == gates(before), (gates(reassigned), gates(before))
    assert reassigned["queued_for_assignee"] == "" and reassigned["queued_behind_ticket"] == ""

    # ACCEPTANCE -- the audit trail names both owners and the reason.
    trail = reassignment_comments(reassigned)
    assert len(trail) == 1, trail
    assert trail[0]["who"] == "director", trail
    assert trail[0]["text"] == f"Director reassignment in in_progress: main -> ops. Reason: {REASON}", trail

    # ACCEPTANCE -- exactly one notification, to the new owner only.
    assert queued_messages(admin, "PGU-1", "main") == []
    delivered = queued_messages(admin, "PGU-1", "ops")
    assert delivered == ["PGU-1 -- Runtime handoff is now assigned to you"], delivered
    assert notification_kinds(admin, "PGU-1", "ops") == ["transition"], notification_kinds(admin, "PGU-1", "ops")
    # Immediately actionable work is the role's current work, and says so.
    assert reassigned["active_work_highlight"] is True, reassigned
    assert reassigned["active_work_owner_role"] == "ops", reassigned

    # ACCEPTANCE -- a reserved target queues instead of taking a second ticket.
    clear_queue(admin)
    queued = reassign("PGU-1", "app")
    assert (queued["state"], queued["assignee"]) == ("backlog", "unassigned"), queued
    assert queued["queued_for_assignee"] == "app", queued
    assert queued["queued_behind_ticket"] == "PGU-2", queued
    assert queued["active_work_highlight"] is False, queued
    # App is told nothing, because nothing is actionable for app yet. The
    # director is told once, and told what is holding it.
    assert queued_messages(admin, "PGU-1", "app") == []
    director_notice = queued_messages(admin, "PGU-1", "director")
    assert len(director_notice) == 1, director_notice
    assert "queued for app" in director_notice[0] and "PGU-2" in director_notice[0], director_notice
    # The reservation still holds: app owns exactly the ticket it already had.
    assert t.psql(
        admin,
        "SELECT count(*) FROM ticket_board.tickets WHERE state='in_progress' AND assignee='app';",
    ) == "1"
    assert t.psql(admin, "SELECT ticket_board.ticket_current_reserved_ticket('app');") == "PGU-2"
    # Both the reason and the queue outcome survive, in that order.
    trail = reassignment_comments(queued)
    assert trail[-1]["text"] == f"Director reassignment in in_progress: ops -> app. Reason: {REASON}", trail
    assert any("queued for app" in comment["text"] for comment in queued["comments"]), queued["comments"]

    # ACCEPTANCE -- the marker does not outlive the reservation that set it.
    # Backlog declares no notification, so this correctly announces nothing.
    clear_queue(admin)
    released = reassign("PGU-1", "main", reason="Parking it with Main for now.")
    assert (released["state"], released["assignee"]) == ("backlog", "main"), released
    assert released["queued_for_assignee"] == "" and released["queued_behind_ticket"] == "", released
    assert queued_messages(admin, "PGU-1", "main") == []

    # ACCEPTANCE -- a blocked ticket still changes hands, and still stays quiet.
    t.post_json(
        base,
        "/api/tickets/PGU-2/actions/set_blockers",
        {"blocked_by": ["PGU-3"], "blocked_reason": "Waiting on triage."},
        caller="director",
    )
    blocked_before = app.get_ticket("PGU-2")
    assert blocked_before["blocked_by"] == ["PGU-3"], blocked_before
    clear_queue(admin)
    blocked = reassign("PGU-2", "ops", reason="App is oversubscribed.")
    assert (blocked["state"], blocked["assignee"]) == ("in_progress", "ops"), blocked
    assert blocked["blocked_by"] == ["PGU-3"], blocked
    assert gates(blocked) == gates(blocked_before), (gates(blocked), gates(blocked_before))
    assert queued_messages(admin, "PGU-2", "ops") == [], queued_messages(admin, "PGU-2", "ops")
    assert blocked["active_work_highlight"] is False, blocked

    # ACCEPTANCE -- the HTTP boundary publishes it, and gates it the same way.
    clear_queue(admin)
    response = t.post_json(
        base,
        "/api/tickets/PGU-2/actions/reassign",
        {"assignee": "app", "reason": "Unblocking App's own change."},
        caller="director",
    )
    assert response["ticket"]["assignee"] == "app", response
    assert response["ticket"]["state"] == "in_progress", response
    refused = t.post_json(
        base,
        "/api/tickets/PGU-2/actions/reassign",
        {"assignee": "ops", "reason": "Not mine to make."},
        caller="main",
        expect=403,
    )
    assert "cannot call reassign" in str(refused), refused
    assert app.get_ticket("PGU-2")["assignee"] == "app"
    missing_reason = t.post_json(
        base,
        "/api/tickets/PGU-2/actions/reassign",
        {"assignee": "ops"},
        caller="director",
        expect=400,
    )
    assert "requires a reason" in str(missing_reason), missing_reason

    # ACCEPTANCE -- the CLI publishes it too, over the same boundary.
    clear_queue(admin)
    exit_code = write_client.main(
        [
            "--board-url",
            base,
            "--caller-role",
            "director",
            "--write-token",
            write_token,
            "reassign",
            "PGU-2",
            "--assignee",
            "ops",
            "--reason",
            "Returning it to Ops.",
        ]
    )
    assert exit_code == 0, exit_code
    assert app.get_ticket("PGU-2")["assignee"] == "ops"
    assert reassignment_comments(app.get_ticket("PGU-2"))[-1]["text"].endswith("Reason: Returning it to Ops."), (
        app.get_ticket("PGU-2")["comments"]
    )

    # ACCEPTANCE -- the capability is what grants it. A tenant document that
    # does not name it takes the operation away again, from the director too.
    without = copy.deepcopy(document)
    director_role = next(role for role in without["roles"] if role["name"] == "director")
    director_role["capabilities"] = [c for c in director_role["capabilities"] if c != "reassign"]
    revision = app.workflow_document()["revision"]
    app.apply_workflow(validate(without), expected_revision=revision, dry_run=False, caller_role="director")
    ungranted = t.post_json(
        base,
        "/api/tickets/PGU-2/actions/reassign",
        {"assignee": "main", "reason": "Should not be possible."},
        caller="director",
        expect=403,
    )
    assert "cannot call reassign" in str(ungranted), ungranted
    assert app.get_ticket("PGU-2")["assignee"] == "ops"
    # ... and the database refuses it independently of the server's gate.
    rejected(lambda: app.reassign_ticket("PGU-2", "main", reason="Direct.", caller_role="audit"), "cannot call reassign")
    # ACCEPTANCE -- the upgrade grants it back, once. The document now in the
    # database is exactly what a tenant configured before this capability
    # existed, so running the migration over it is the real upgrade case. The
    # function bodies in that file are generated from schema.sql and held equal
    # to it by ticket_board_schema_function_migration_test; what only the
    # upgrade path can have is this backfill.
    migration = (ROOT / "scripts/ticket_board/migrations/pgu926_syrd77_director_reassign.sql").read_text()
    stale_revision = app.workflow_document()["revision"]
    t.psql(admin, "BEGIN;\n" + migration + "\nCOMMIT;")
    granted = app.workflow_document()
    assert granted["revision"] != stale_revision, granted["revision"]
    assert "reassign" in director_capabilities(granted["document"]), granted["document"]
    # The projected role rows are what the database actually checks per call.
    assert t.psql(
        admin,
        "SELECT definition->'capabilities' ? 'reassign' FROM ticket_board.workflow_roles WHERE name='director';",
    ) == "t"
    restored = t.post_json(
        base,
        "/api/tickets/PGU-2/actions/reassign",
        {"assignee": "main", "reason": "Restored capability."},
        caller="director",
    )
    assert restored["ticket"]["assignee"] == "main", restored

    # Re-running it changes nothing, and a director who removes the capability
    # afterwards has made a decision the next upgrade must not quietly undo.
    t.psql(admin, "BEGIN;\n" + migration + "\nCOMMIT;")
    assert app.workflow_document()["revision"] == granted["revision"]
    removed = copy.deepcopy(granted["document"])
    director_role = next(role for role in removed["roles"] if role["name"] == "director")
    director_role["capabilities"] = [c for c in director_role["capabilities"] if c != "reassign"]
    app.apply_workflow(
        validate(removed),
        expected_revision=granted["revision"],
        dry_run=False,
        caller_role="director",
    )
    deliberate = app.workflow_document()["revision"]
    t.psql(admin, "BEGIN;\n" + migration + "\nCOMMIT;")
    after = app.workflow_document()
    assert after["revision"] == deliberate, (after["revision"], deliberate)
    assert "reassign" not in director_capabilities(after["document"]), after["document"]

    app.apply_workflow(
        validate(document),
        expected_revision=after["revision"],
        dry_run=False,
        caller_role="director",
    )
    assert reassign("PGU-2", "ops", reason="Capability reconfigured.")["assignee"] == "ops"


if __name__ == "__main__":
    raise SystemExit(main())
