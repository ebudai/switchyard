#!/usr/bin/env python3
"""SYRD-548: a tenant report may ask for Backlog, and the Director's policy decides where it lands.

Over real HTTP against real clusters, end to end: a tenant is connected for
reports by the narrow `upgrade --only upstream-report` step (and nothing else),
its pane environment is read back from its configuration, and the real
`ticket-board-write file-report --defer` runs with exactly that environment.

On a board whose Director has set no policy the report lands in Triage with the
request written on it; once the Director's report-intake policy honours Backlog
requests it lands in Backlog; a report that asks for nothing is unchanged. Only
the Director may set the policy, and a report may ask for nothing but Backlog.

Twice: on schema.sql, and on a board built before pgu981 and upgraded the way
every board is -- the migration and every one after it, then rbac.sql.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import tenant_report_declared_workflow_test as r  # noqa: E402  (isolates the tmux bus on import)
import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import definition, migrations_from, owning_migration, rbac_before, schema_before  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from upstream_report_credential_test import _host, _sandbox_home, _tenant  # noqa: E402

from scripts import team_launcher  # noqa: E402
from scripts import upstream_report  # noqa: E402

MIGRATION = ROOT / "scripts" / "ticket_board" / "migrations" / "pgu981_syrd548_report_intake.sql"
CHECKS = 0
KEPT = "keeps reports in Triage"
HONOURED = "honours that request"


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


@contextlib.contextmanager
def sandbox_home(resolver):
    """connect_upstream_report's home default is bound at import; the sandbox replaces it there."""
    defaults = upstream_report.connect_upstream_report.__kwdefaults__
    previous = defaults["home_for_user"]
    defaults["home_for_user"] = resolver
    try:
        yield
    finally:
        defaults["home_for_user"] = previous


def connect_tenant(tmp: Path, base: str) -> dict[str, str]:
    """The narrow step against a real board; returns the pane environment it gives the tenant."""
    registry, board_env, _owner, resolver = _host(tmp, board_url=base)
    board_env.parent.mkdir(parents=True, exist_ok=True)
    board_env.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={t.TEST_REPORT_TOKEN}\n", encoding="utf-8")
    config, path = _tenant(tmp)
    check("TICKET_BOARD_REPORT_URL" not in config.roles[0].env, "before: the pane has no report URL")
    said: list[str] = []
    with sandbox_home(resolver):
        status = team_launcher.upgrade_project_command(
            config, config_path=path, only="upstream-report", upstream_report_url=base,
            registry_dir=registry, print_func=said.append,
        )
    check(status == 0, said)
    return dict(team_launcher.load_project_config("mefp", path).roles[0].env)


def file_report(env: dict[str, str], *, defer: bool, title: str) -> tuple[int, dict, str]:
    """The real CLI, with exactly the pane's environment."""
    from scripts.ticket_board import write_cli, write_client

    keys = ("TICKET_BOARD_REPORT_URL", "TICKET_BOARD_REPORT_ORIGIN_PROJECT", "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE")
    out, err = io.StringIO(), io.StringIO()
    # The tenant's own board, which every pane has: a closed port, so a report that
    # went anywhere but the report URL would fail here.
    own = {"TICKET_BOARD_URL": "http://127.0.0.1:9", "TICKET_BOARD_PROJECT": "mefp"}
    with patch.dict(os.environ, {**own, **{k: env[k] for k in keys}}, clear=False):
        for stale in ("TICKET_BOARD_TENANT_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN", "TICKET_BOARD_REPORT_TOKEN_FILE"):
            os.environ.pop(stale, None)
        importlib.reload(write_client)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = write_cli.main(["file-report", "--title", title, "--body", "from mefp",
                                     *(["--defer"] if defer else [])])
    importlib.reload(write_client)
    return status, (json.loads(out.getvalue()) if status == 0 else {}), err.getvalue()


def service_notes(ticket: dict) -> list[str]:
    return [c["text"] for c in ticket.get("comments") or [] if c.get("who") == "ticket_board_service"]


def set_intake(base: str, decision: str, *, caller: str, expect: int = 200) -> dict:
    return t.post_json(base, "/api/tickets/actions/set_report_intake",
                       {"backlog_requests": decision, "reason": f"{caller} decides reports"}, caller=caller, expect=expect)


def exercise(base: str, admin: str, service: str, env: dict[str, str], label: str) -> None:
    # Nothing chosen: a Backlog request lands in Triage, the request written on it.
    intake = json.loads(t.psql(service, "SELECT ticket_board.report_intake()::text;"))
    check(intake["backlog_requests"] == "triage", f"{label}: no policy means Triage: {intake}")
    status, ticket, err = file_report(env, defer=True, title=f"{label}: deferred, no policy")
    check(status == 0 and ticket["state"] == "analysis" and ticket["assignee"] == "unassigned", (status, ticket, err))
    notes = service_notes(ticket)
    check(len(notes) == 1 and "requested Backlog" in notes[0] and KEPT in notes[0] and "mefp" in notes[0], notes)
    check(err == "", f"{label}: the CLI saw the request recorded: {err}")

    # A report that asks for nothing is the report it always was.
    status, ticket, _err = file_report(env, defer=False, title=f"{label}: plain")
    check(status == 0 and ticket["state"] == "analysis" and service_notes(ticket) == [], ticket)

    # A report may ask for Backlog and nothing else, and still sets no stage itself.
    before = r.ticket_count(admin)
    for payload, expected in (({"requested_stage": "done"}, "may request backlog or nothing"),
                              ({"requested_stage": "in_progress"}, "may request backlog or nothing"),
                              ({"state": "backlog"}, "file_report cannot set: state")):
        refused = t.post_report_json(base, r.REPORT, {"title": "x", "body": "", "origin_project": "mefp", **payload},
                                     expect=400 if "requested_stage" in payload else 403)
        check(expected in json.dumps(refused), (payload, refused))
    # The database refuses one too, behind the board's own check.
    r.refused(lambda: t.psql(service, "SELECT ticket_board.file_report('x', '', 'mefp', '', 'done');"),
              "a report may request backlog or nothing, not done")
    check(r.ticket_count(admin) == before, f"{label}: refused reports create nothing")

    # Only the Director sets the policy -- at the door and in the database.
    refused = set_intake(base, "backlog", caller="ops", expect=403)
    check(refused == "ops cannot call set_report_intake", f"refused at the door: {refused!r}")
    r.refused(lambda: t.psql(service, "SELECT set_config('ticket_board.caller_role', 'ops', false);"
                                       "SELECT ticket_board.set_report_intake(true, 'ops wants it');"),
              "role ops cannot call director_edit")
    unchanged = json.loads(t.psql(service, "SELECT ticket_board.report_intake()::text;"))
    check(unchanged["backlog_requests"] == "triage", f"{label}: refused, the policy is unchanged: {unchanged}")
    answer = set_intake(base, "backlog", caller="director")["report_intake"]
    check(answer["backlog_requests"] == "backlog" and answer["set_by"] == "director", answer)

    # Honoured: the request lands in Backlog, and says why.
    status, ticket, err = file_report(env, defer=True, title=f"{label}: deferred, honoured")
    check(status == 0 and ticket["state"] == "backlog" and ticket["assignee"] == "unassigned", (ticket, err))
    notes = service_notes(ticket)
    check(len(notes) == 1 and HONOURED in notes[0], notes)
    status, ticket, _err = file_report(env, defer=False, title=f"{label}: plain, policy set")
    check(ticket["state"] == "analysis", "the policy honours requests; it does not defer every report")

    # And back: the Director's choice is the board's.
    set_intake(base, "triage", caller="director")
    status, ticket, _err = file_report(env, defer=True, title=f"{label}: deferred, policy withdrawn")
    check(ticket["state"] == "analysis" and KEPT in service_notes(ticket)[0], ticket)


def fresh_board(cluster) -> None:
    admin, board, server, base = r.a_board(cluster, "syrd548_fresh")
    service = t.conninfo(cluster.socket_dir, cluster.port, "syrd548_fresh", t.SERVICE_ROLE)
    try:
        # A board with no declared workflow admits by its legacy table: the Director only.
        refused = set_intake(base, "backlog", caller="ops", expect=403)
        check(refused == "ops cannot call set_report_intake", f"legacy board, refused at the door: {refused!r}")
        unchanged = json.loads(t.psql(service, "SELECT ticket_board.report_intake()::text;"))
        check(unchanged["backlog_requests"] == "triage" and unchanged["set_by"] == "", unchanged)
        r.declare_workflow(base, board)
        with tempfile.TemporaryDirectory(prefix="syrd548-fresh.") as raw:
            exercise(base, admin, service, connect_tenant(Path(raw), base), "schema.sql")
    finally:
        server.shutdown()
        server.server_close()


def upgraded_board(cluster) -> None:
    """A board as it shipped before pgu981, upgraded the way every board is."""
    db = "syrd548_upgraded"
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, schema_before(MIGRATION))
    t.psql(admin, rbac_before(MIGRATION))
    old = t.psql(admin, "SELECT to_regprocedure('ticket_board.file_report(text,text,text,text,text)') IS NULL;")
    check(old.strip() == "t", "the board predates the five-argument form")
    for migration in migrations_from(MIGRATION):
        t.psql(admin, migration.read_text())
    t.psql(admin, t.RBAC_PATH.read_text())
    frames, assets = cluster.root / f"{db}-frames", cluster.root / f"{db}-assets"
    frames.mkdir(exist_ok=True)
    assets.mkdir(exist_ok=True)
    board = t.TicketBoardApp(frames, assets, project="cerulean", ticket_prefix="PGU",
                             database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
    server = t.TicketBoardServer(("127.0.0.1", 0), board, director_notifier=t.QuietNotifier(),
                                 report_token=t.TEST_REPORT_TOKEN)
    t.TEST_WRITE_TOKEN = server.write_token
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    service = t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE)
    try:
        r.declare_workflow(base, board)
        with tempfile.TemporaryDirectory(prefix="syrd548-upgraded.") as raw:
            exercise(base, admin, service, connect_tenant(Path(raw), base), "upgraded")
    finally:
        server.shutdown()
        server.server_close()


def test_a_report_may_ask_for_backlog_and_nothing_else() -> None:
    from scripts.ticket_board.report_intake import requested_stage

    check([requested_stage(v) for v in (None, "", " Backlog ", "backlog")] == ["", "", "backlog", "backlog"],
          "the request every client sent before, and Backlog however it is written")
    for value in ("done", "analysis", "in_progress", 3, ["backlog"]):
        try:
            requested_stage(value)
        except ValueError as exc:
            check("backlog or nothing" in str(exc) or "must be a string" in str(exc), str(exc))
        else:
            check(False, f"{value!r} was accepted as a request")


def test_schema_and_migration_carry_the_same_objects() -> None:
    check(owning_migration("file_report") == MIGRATION, owning_migration("file_report").name)
    text = MIGRATION.read_text()
    schema = (ROOT / "scripts" / "ticket_board" / "schema.sql").read_text()
    block = text[text.index("CREATE TABLE IF NOT EXISTS ticket_board.report_intake_policy"):
                 text.index("REVOKE ALL ON ticket_board.report_intake_policy FROM PUBLIC;")].rstrip()
    check(block in schema, "schema.sql carries pgu981's table and functions character for character")
    check(definition("set_report_intake") in text and definition("report_intake") in text, "both policy functions")


def main() -> int:
    test_a_report_may_ask_for_backlog_and_nothing_else()
    test_schema_and_migration_carry_the_same_objects()
    with temporary_cluster(prefix="syrd548-report-", shutdown="immediate") as cluster:
        fresh_board(cluster)
        upgraded_board(cluster)
    print(f"report_intake_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
