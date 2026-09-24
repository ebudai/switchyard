#!/usr/bin/env python3
"""SYRD-256: a tenant report must file on a board running a declared workflow.

The MEFP Director's live upstream report got past the report token (SYRD-238)
and then failed with

    invalid configured caller role: <NULL>

and no ticket was created. The report-only HTTP action authenticates
X-Ticket-Board-Report-Token and calls ticket_board.file_report with no caller
role, by design: the report credential is not a role. On a legacy board that
was fine. On a declared-workflow board it was refused twice over:

1. file_report called require_actor, whose declared branch resolves
   current_app_actor(), which refuses a NULL role;
2. file_report then rebuilds the ticket's export projection with an UPDATE,
   and enforce_declared_ticket_update asks current_app_actor() again before
   it does anything else.

Fixing only the first leaves the second, which is how this was found.

The fix must not work by lending the report path a role. So this suite drives
the whole boundary on a declared board, over real HTTP against a real cluster:
the report files; a bad or missing token, a write token, and a protected field
are still refused; the report token still opens nothing else; and in SQL, the
non-role actor named for the refresh can move, reassign and flag nothing, can
never be a role, and does not outlive the statement it was named for.

Finally, the board MEFP is running was built before this migration, so the
upgrade path is driven too: a declared board holding the old file_report
refuses the report, and applying pgu957 on top of it repairs it.
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
import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import definition, owning_migration  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
MIGRATION = MIGRATIONS / "pgu957_syrd256_report_only_file_report.sql"
#: The copy every board built before this ticket is running.
OLD_COPY = MIGRATIONS / "pgu765_tenant_file_report.sql"
SCHEMA = ROOT / "scripts" / "ticket_board" / "schema.sql"
WORKFLOW = ROOT / "examples" / "workflows" / "inspection.json"
REPORT = "/api/tickets/actions/file_report"


def shipped_report_actor() -> str:
    """The actor file_report actually names -- read from it, not restated here.

    Every guarantee below is about THIS string. A constant in the test would
    let the function name something else (a real role, say) while the suite
    went on proving things about a string nobody uses.
    """
    import re

    named = re.findall(
        r"set_config\('ticket_board\.workflow_actor',\s*'([^']*)',\s*true\)",
        definition("file_report"),
    )
    actors = [value for value in named if value]
    assert len(actors) == 1, f"file_report names exactly one actor: {named}"
    return actors[0]


REPORT_ACTOR = shipped_report_actor()

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refused(call, expected: str) -> str:
    try:
        call()
    except AssertionError as exc:
        # `t.psql` raises AssertionError carrying PostgreSQL's own message.
        assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}, and it was allowed")


def ticket_count(admin: str) -> int:
    return int(t.psql(admin, "SELECT count(*) FROM ticket_board.tickets;"))


def a_board(cluster, db: str, *, first: bool = True):
    """A fresh board with RBAC, served over HTTP with a report token.

    Login roles belong to the cluster, not the database, so only the first
    board in a cluster creates them.
    """
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, SCHEMA.read_text())
    if first:
        t.create_roles(admin)
    t.psql(admin, t.RBAC_PATH.read_text())
    frames, assets = cluster.root / f"{db}-frames", cluster.root / f"{db}-assets"
    frames.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    board = t.TicketBoardApp(
        frames, assets, project="cerulean", ticket_prefix="PGU",
        database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
    )
    server = t.TicketBoardServer(
        ("127.0.0.1", 0), board, director_notifier=t.QuietNotifier(),
        report_token=t.TEST_REPORT_TOKEN,
    )
    t.TEST_WRITE_TOKEN = server.write_token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return admin, board, server, f"http://127.0.0.1:{server.server_port}"


def declare_workflow(base: str, board) -> None:
    """Through the real handler, as the Director -- how a board gets one."""
    cfg = validate(json.loads(WORKFLOW.read_text()))
    t.post_json(
        base, "/api/tickets/actions/configure_workflow",
        {"document": cfg, "expected_revision": 0}, caller="director",
    )
    check(board.workflow_configuration() is not None, "the board runs a declared workflow")


def report(base: str, **fields) -> dict:
    payload = {"title": "Upstream defect", "body": "from mefp", "origin_project": "mefp", **fields}
    return t.post_report_json(base, REPORT, payload, expect=201)["ticket"]


def test_a_report_files_on_a_declared_board(base: str, admin: str) -> None:
    """The live failure, and what the ticket says must be true instead."""
    created = report(base, external_source_ref="mefp:MEFP-42")

    check(created["state"] == "analysis", f"lands in analysis: {created}")
    check(created["assignee"] == "unassigned", f"owned by nobody: {created}")
    check(created["origin_project"] == "mefp", f"carrying its origin: {created}")
    check(created["external_source_ref"] == "mefp:MEFP-42", f"and its source ref: {created}")
    check(created["needs_audit"] is True, f"with audit required: {created}")
    check(created["parent_id"] == "", f"and no parent: {created}")

    # The export projection is complete, not the partial one the INSERT wrote:
    # it was rebuilt the way every other writer rebuilds it.
    projection = json.loads(t.psql(
        admin,
        f"SELECT source_json::text FROM ticket_board.tickets WHERE id = '{created['id']}';",
    ))
    rebuilt = json.loads(t.psql(
        admin, f"SELECT ticket_board.build_ticket_source_json('{created['id']}')::text;"
    ))
    check(projection == rebuilt, "the stored projection is the fully rebuilt one")
    check("blocked_reason" in projection, f"including fields the INSERT omits: {sorted(projection)}")


def test_the_report_only_boundary_holds_on_a_declared_board(base: str, admin: str) -> None:
    before = ticket_count(admin)
    payload = {"title": "Nope", "body": "", "origin_project": "mefp"}

    missing = t.post_report_json(base, REPORT, payload, token=None, expect=403)
    check("missing or invalid X-Ticket-Board-Report-Token" in str(missing), f"no token: {missing}")
    wrong = t.post_report_json(base, REPORT, payload, token="wrong-token", expect=403)
    check("missing or invalid X-Ticket-Board-Report-Token" in str(wrong), f"wrong token: {wrong}")
    # The write token is a different credential and does not open this door.
    write_token = t.post_report_json(base, REPORT, payload, token=t.TEST_WRITE_TOKEN, expect=403)
    check("missing or invalid X-Ticket-Board-Report-Token" in str(write_token),
          f"a write token is not a report token: {write_token}")

    for protected in (
        {"state": "in_progress", "assignee": "app"},
        {"commit_hash": "0" * 40},
        {"needs_audit": False},
        {"audit_signoff": True},
        {"manually_controlled": True},
        {"parent_id": "PGU-1"},
    ):
        rejected = t.post_report_json(base, REPORT, {**payload, **protected}, expect=403)
        check("file_report cannot set" in str(rejected), f"{sorted(protected)}: {rejected}")

    # And the report token opens nothing but the report.
    for path, body in (
        ("/api/tickets/actions/create_ticket", {"title": "Nope", "body": ""}),
        ("/api/tickets/PGU-1/actions/add_comment", {"text": "Nope"}),
        ("/api/tickets/PGU-1/actions/route", {"state": "backlog", "assignee": "app"}),
        ("/api/tickets/actions/configure_workflow", {"document": {}, "expected_revision": 1}),
    ):
        rejected = t.post_report_json(base, path, body, expect=403)
        check("missing or invalid X-Ticket-Board-Write-Token" in str(rejected),
              f"{path} with a report token: {rejected}")

    check(ticket_count(admin) == before, "and none of those created a ticket")


def test_the_named_actor_carries_no_authority(admin: str, service: str) -> None:
    """What the refresh is allowed to name, and what naming it buys.

    Driven as the superuser on purpose. The service role has no direct UPDATE
    on tickets at all, so trying this as the service is refused by the grant
    before the trigger ever runs -- which would prove nothing about the
    trigger. The superuser bypasses the grant and still fires it, so what is
    tested here is exactly the question: given the report actor and nothing
    else, what will enforce_declared_ticket_update allow? Nothing that moves,
    reassigns or flags a ticket.
    """
    ticket = report_via_sql(service)

    def as_report_actor(statement: str) -> str:
        return t.psql(admin, f"""
BEGIN;
SELECT set_config('ticket_board.workflow_actor', '{REPORT_ACTOR}', true);
{statement}
COMMIT;
""")

    refused(
        lambda: as_report_actor(
            f"UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = '{ticket}';"),
        "unauthorized configured transition",
    )
    refused(
        lambda: as_report_actor(
            f"UPDATE ticket_board.tickets SET assignee = 'director' WHERE id = '{ticket}';"),
        "only director may reassign",
    )
    refused(
        lambda: as_report_actor(
            f"UPDATE ticket_board.tickets SET needs_audit = false WHERE id = '{ticket}';"),
        "flag change requires authorized workflow action",
    )
    state = t.psql(admin, f"SELECT state || '/' || assignee || '/' || needs_audit "
                          f"FROM ticket_board.tickets WHERE id = '{ticket}';")
    check(state == "analysis/unassigned/true", f"and the ticket did not move: {state}")


def test_the_named_actor_can_never_be_a_role(admin: str) -> None:
    """The guarantee the whole approach rests on, enforced by PostgreSQL."""
    refused(
        lambda: t.psql(admin, f"""
INSERT INTO ticket_board.workflow_roles (name, definition)
VALUES ('{REPORT_ACTOR}', '{{}}'::jsonb);
"""),
        "violates check constraint",
    )
    roles = t.psql(admin, "SELECT string_agg(name, ',') FROM ticket_board.workflow_roles;")
    check(REPORT_ACTOR not in roles.split(","), f"and no role carries that name: {roles}")


def test_the_named_actor_does_not_outlive_the_refresh(service: str) -> None:
    """Scoped to one statement, like the executor scopes the same setting."""
    left = t.psql(service, """
BEGIN;
SELECT ticket_board.file_report('Scoped', 'body', 'mefp', '');
SELECT 'after:' || coalesce(current_setting('ticket_board.workflow_actor', true), '');
COMMIT;
""").splitlines()
    after = [line for line in left if line.startswith("after:")]
    check(len(after) == 1, f"the probe ran and reported: {left}")
    check(after[0] == "after:", f"the setting is cleared before file_report returns: {after[0]!r}")


def test_only_the_service_may_call_it(admin: str) -> None:
    """The authority check file_report now makes for itself.

    `postgres` has EXECUTE on everything, so this reaches the function body
    rather than stopping at a missing grant -- which is what makes it a test
    of the check and not of rbac.sql.
    """
    refused(
        lambda: t.psql(admin, "SELECT ticket_board.file_report('Nope', '', 'mefp', '');"),
        "cannot call file_report; ticket_board_service is the only database writer",
    )


def test_an_upgraded_board_is_repaired_by_the_migration(cluster) -> None:
    """MEFP's board predates this: it holds pgu765's copy until pgu957 runs."""
    admin, board, server, base = a_board(cluster, "syrd256_upgrade", first=False)
    try:
        declare_workflow(base, board)
        t.psql(admin, OLD_COPY.read_text())
        broken = t.post_report_json(
            base, REPORT, {"title": "Before", "body": "", "origin_project": "mefp"}, expect=400
        )
        check("invalid configured caller role: <NULL>" in str(broken),
              f"the old copy reproduces the live failure: {broken}")
        check(ticket_count(admin) == 0, "and creates nothing")

        t.psql(admin, MIGRATION.read_text())
        created = report(base, title="After")
        check(created["state"] == "analysis", f"the migration repairs it: {created}")
        check(ticket_count(admin) == 1, "exactly one ticket, the repaired one")

        # Idempotent: a deploy that replays the tail must not fail on it.
        t.psql(admin, MIGRATION.read_text())
        check(report(base, title="Replayed")["state"] == "analysis", "replaying it is harmless")
    finally:
        server.shutdown()
        server.server_close()


def test_a_fresh_board_and_an_upgraded_board_run_the_same_function() -> None:
    check(owning_migration("file_report") == MIGRATION,
          f"pgu957 owns file_report: {owning_migration('file_report').name}")
    check(definition("file_report") in MIGRATION.read_text(),
          "schema.sql's copy is the migration's, character for character")
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    check(names.index(MIGRATION.name) > names.index(OLD_COPY.name),
          "and it applies after the copy it replaces")
    # Code only: the body's comments explain why require_actor is NOT called,
    # and a substring check over them would fail on the explanation.
    code = "\n".join(
        line.split("--", 1)[0] for line in definition("file_report").splitlines()
    )
    check("require_actor" not in code, "file_report no longer resolves a role at all")
    check("current_app_actor" not in code, "not even indirectly")


def report_via_sql(service: str) -> str:
    return t.psql(service, "SELECT ticket_board.file_report('Via SQL', '', 'mefp', '');")


def main() -> int:
    with temporary_cluster(prefix="syrd256-report-", shutdown="immediate") as cluster:
        # The legacy control first: the report worked here and must still.
        admin, board, server, base = a_board(cluster, "syrd256_declared")
        service = t.conninfo(cluster.socket_dir, cluster.port, "syrd256_declared", t.SERVICE_ROLE)
        try:
            legacy = report(base, title="Before any workflow is declared")
            check(legacy["state"] == "analysis", f"a legacy board files it: {legacy}")

            declare_workflow(base, board)
            test_a_report_files_on_a_declared_board(base, admin)
            test_the_report_only_boundary_holds_on_a_declared_board(base, admin)
            test_the_named_actor_carries_no_authority(admin, service)
            test_the_named_actor_can_never_be_a_role(admin)
            test_the_named_actor_does_not_outlive_the_refresh(service)
            test_only_the_service_may_call_it(admin)
        finally:
            server.shutdown()
            server.server_close()
        test_an_upgraded_board_is_repaired_by_the_migration(cluster)
    test_a_fresh_board_and_an_upgraded_board_run_the_same_function()
    print(f"tenant_report_declared_workflow_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
