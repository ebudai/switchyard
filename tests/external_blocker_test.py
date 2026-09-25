#!/usr/bin/env python3
"""SYRD-270: a ticket can wait on another board's work, durably, and is released on purpose.

MEFP-4's Ops pushed a commit the MEFP board could not see: the board's commit
repository was the historical one until Switchyard operator ticket SYRD-269
changed it. MEFP's board had no way to record that wait -- blocked_by accepted
only its own tickets and await-role only a role with a pane -- so Ops opened a
Director dependency, the Director could do nothing with it and cleared it, and
it was opened again: the same non-decision handed to the Director over and
over.

An external blocker, `project:PREFIX-N`, is the wait. This replays MEFP-4 on a
real cluster, through the real `ticket-board-write` CLI and HTTP server, with
two real commit repositories standing in for the board's historical and
canonical ones:

* it keeps the ticket in in_progress/ops and suppresses every reminder, the
  Director escalations and any new dependency handoff, and refuses submission;
* nothing that moves anywhere releases it -- not a ticket called SYRD-269;
* only the Director releases it, with a reason, and a release that names a
  commit is refused until THIS board resolves that commit;
* the release moves nothing: Ops submits, and the review gate still applies.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_director_defer_backlog_test as defer_suite  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import write_client  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
REF = "syrd:SYRD-269"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def repositories(root: Path) -> tuple[Path, Path, str]:
    """A historical repository without Ops's commit, and the canonical one with it."""
    work = root / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    for name, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
        git("config", name, value, cwd=work)
    (work / "README").write_text("base\n")
    git("add", "README", cwd=work)
    git("commit", "-q", "-m", "base", cwd=work)
    historical = root / "historical.git"
    git("clone", "-q", "--bare", str(work), str(historical))
    (work / "provisioning.txt").write_text("preserved\n")
    git("add", "provisioning.txt", cwd=work)
    git("commit", "-q", "-m", "MEFP-4 provisioning", cwd=work)
    wanted = git("rev-parse", "HEAD", cwd=work)
    canonical = root / "canonical.git"
    git("clone", "-q", "--bare", str(work), str(canonical))
    # Neither may fetch its way to the commit: the historical repository must
    # stay without it, exactly as the MEFP board's did.
    for repo in (historical, canonical):
        git(f"--git-dir={repo}", "remote", "remove", "origin")
    return historical, canonical, wanted


@contextlib.contextmanager
def served(app):
    server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
    t.TEST_WRITE_TOKEN = server.write_token
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.write_token
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def cli(board: tuple[str, str], role: str, *argv: str) -> tuple[int, str]:
    """The real ticket-board-write, over HTTP, as `role`."""
    base, token = board
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = write_client.main(
                ["--board-url", base, "--caller-role", role, f"--write-token={token}", *argv]
            )
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue() + err.getvalue()


def queued(admin: str, ticket_id: str) -> list[tuple[str, str]]:
    raw = t.psql(admin, f"""
SELECT coalesce(jsonb_agg(jsonb_build_array(kind, target_role) ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
    return [tuple(item) for item in json.loads(raw)]


def clear_queue(admin: str) -> None:
    t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")


def every_reminder(admin: str, listener: str) -> None:
    """Run each reminder the board generates, for the owner and the Director, as overdue."""
    for role in ("ops", "director"):
        t.psql(listener, f"""
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text), clock_timestamp());
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text),
    clock_timestamp(), interval '0 seconds', '{{}}'::jsonb);
""")
    t.psql(admin, "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '6 hours');")


def blockers(admin: str, ticket_id: str) -> list[tuple[str, bool]]:
    raw = t.psql(admin, f"""
SELECT coalesce(jsonb_agg(jsonb_build_array(blocker_ticket_id, resolved) ORDER BY position), '[]'::jsonb)::text
FROM ticket_board.ticket_blockers WHERE ticket_id = '{ticket_id}';
""")
    return [tuple(item) for item in json.loads(raw)]


def run(cluster, root: Path) -> None:
    historical, canonical, wanted = repositories(root)
    _app, admin = defer_suite.board(cluster, "mefp", copy.deepcopy(defer_suite.CANONICAL))
    database_url = t.conninfo(cluster.socket_dir, cluster.port, "mefp", t.SERVICE_ROLE)
    listener = t.conninfo(cluster.socket_dir, cluster.port, "mefp", "ticket_board_listener")

    def app_on(repo: Path):
        return t.TicketBoardApp(
            root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=database_url, commit_git_dir=repo,
        )

    historical_app = app_on(historical)
    t.seed_postgres_ticket(admin, "PGU-4", title="Preserve provisioning", state="in_progress", assignee="ops")

    # The probe arrives: before any wait, an idle owner IS reminded.
    clear_queue(admin)
    every_reminder(admin, listener)
    check(queued(admin, "PGU-4"), "the reminders this test runs do reach an unblocked ticket")
    clear_queue(admin)

    with served(historical_app) as board:
        # Ops cannot record the wait itself: blockers are the Director's.
        code, output = cli(board, "ops", "set-blockers", "PGU-4", "--blocked-by", REF, "--blocked-reason", "x")
        check(code != 0 and "cannot call set_blockers" in output, f"ops cannot set a blocker: {code} {output}")

        # A qualified reference to this board's own ticket is refused.
        code, output = cli(board, "director", "set-blockers", "PGU-4", "--blocked-by", "cerulean:PGU-9",
                           "--blocked-reason", "x")
        check(code != 0 and "names a ticket on this board; block on PGU-9 instead" in output,
              f"a local ticket is blocked on by its bare id: {output}")

        # The Director records the wait: MEFP-4 waits on Switchyard's SYRD-269.
        code, output = cli(board, "director", "set-blockers", "PGU-4", "--blocked-by", "SYRD:syrd-269",
                           "--blocked-reason", "Waiting for SYRD-269 to point this board at the canonical repository.")
        check(code == 0, f"the Director records an external blocker: {output}")
        ticket = historical_app.get_ticket("PGU-4")
        check((ticket["state"], ticket["assignee"]) == ("in_progress", "ops"),
              f"the ticket keeps its stage and owner: {ticket['state']}/{ticket['assignee']}")
        check(ticket["blocked_by"] == [REF] and ticket["blockers"] == [{"id": REF, "resolved": False}],
              f"normalized to project:PREFIX-N and unresolved: {ticket['blocked_by']} {ticket['blockers']}")
        check(blockers(admin, "PGU-4") == [(REF, False)], f"{blockers(admin, 'PGU-4')}")

        # Nothing nags anybody about it.
        clear_queue(admin)
        every_reminder(admin, listener)
        check(queued(admin, "PGU-4") == [], f"no reminder or escalation while it waits: {queued(admin, 'PGU-4')}")

        # Ops cannot reopen the Director handoff that kept repeating.
        code, output = cli(board, "ops", "request-dependency", "PGU-4", "--role", "director",
                           "--reason", "Please chase SYRD-269 again.")
        check(code != 0 and "unresolved blocker prevents an awaiting-role handoff: " + REF in output,
              f"no Director dependency can be opened behind it: {output}")
        check(queued(admin, "PGU-4") == [], f"and nothing was queued: {queued(admin, 'PGU-4')}")

        # And the work cannot go forward: no gate is skipped while it waits.
        try:
            historical_app.perform_workflow_action("PGU-4", "submit_to_audit", {"commit_hash": wanted[:12]},
                                                   caller_role="ops")
        except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
            refusal = str(exc)
        else:
            refusal = ""
        check("unknown commit_hash" in refusal or "unresolved blocker prevents forward promotion" in refusal,
              f"submission is refused: {refusal!r}")

        # Nothing that happens elsewhere releases it -- not even a ticket that is
        # called SYRD-269 going to done.
        t.seed_postgres_ticket(admin, "SYRD-269", title="Operator repoint", state="in_progress", assignee="ops")
        moved = historical_app.force_move_ticket("SYRD-269", "done", "unassigned", suppress_notification=True,
                                                 caller_role="director")
        check(moved["state"] == "done", f"the local SYRD-269 is done: {moved['state']}")
        check(blockers(admin, "PGU-4") == [(REF, False)],
              f"a ticket named SYRD-269 finishing does not release it: {blockers(admin, 'PGU-4')}")

        # Only the Director releases it.
        code, output = cli(board, "ops", "release-external-blocker", "PGU-4", "--ref", REF,
                           "--reason", "I think it is done")
        check(code != 0 and "cannot call release_external_blocker" in output, f"ops cannot release it: {output}")
        code, output = cli(board, "director", "release-external-blocker", "PGU-4", "--ref", "PGU-9",
                           "--reason", "x")
        check(code != 0 and "not an external blocker: PGU-9" in output, f"a local id is not released here: {output}")

        # While the board still verifies against the historical repository, a
        # release that names the commit is refused, and the wait stands.
        code, output = cli(board, "director", "release-external-blocker", "PGU-4", "--ref", REF,
                           "--reason", "SYRD-269 is done", "--commit", wanted)
        check(code != 0 and "unknown commit_hash" in output,
              f"the board cannot see the commit yet, so the release is refused: {output}")
        check(blockers(admin, "PGU-4") == [(REF, False)], f"and the wait stands: {blockers(admin, 'PGU-4')}")

    # SYRD-269 lands: the board now verifies against the canonical repository.
    canonical_app = app_on(canonical)
    # The commit resolves now, so only the wait can refuse a submission -- and
    # it does: the board seeing the commit is not the Director releasing it.
    try:
        canonical_app.perform_workflow_action("PGU-4", "submit_to_audit", {"commit_hash": wanted[:12]},
                                              caller_role="ops")
    except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
        refusal = str(exc)
    else:
        refusal = ""
    check("unresolved blocker prevents forward promotion: " + REF in refusal,
          f"a known commit is still refused while the wait stands: {refusal!r}")
    check(canonical_app.get_ticket("PGU-4")["state"] == "in_progress", "and nothing moved")
    # A release says why, at the database, whoever calls it.
    try:
        canonical_app.release_external_blocker("PGU-4", ref=REF, reason="  ", caller_role="director")
    except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
        unexplained = str(exc)
    else:
        unexplained = ""
    check("releasing an external blocker requires a reason" in unexplained,
          f"a release without a reason is refused: {unexplained!r}")
    check(blockers(admin, "PGU-4") == [(REF, False)], "and the wait stands")
    comments_before = len(canonical_app.get_ticket("PGU-4")["comments"])
    clear_queue(admin)
    with served(canonical_app) as board:
        code, output = cli(board, "director", "release-external-blocker", "PGU-4", "--ref", REF,
                           "--reason", "The board now verifies against /data/git/fixpatch.", "--commit", wanted[:12])
        check(code == 0, f"now it resolves, and the Director releases the wait: {output}")
        ticket = canonical_app.get_ticket("PGU-4")
        check(ticket["blocked_by"] == [] and ticket["blockers"] == [] and ticket["blocked_reason"] == "",
              f"the external blocker is gone: {ticket['blocked_by']} {ticket['blockers']} {ticket['blocked_reason']!r}")
        check((ticket["state"], ticket["assignee"]) == ("in_progress", "ops"),
              f"and nothing moved: {ticket['state']}/{ticket['assignee']}")
        released = ticket["comments"][comments_before:]
        check(len(released) == 1 and released[0]["who"] == "director"
              and released[0]["text"] == (f"External blocker {REF} released: The board now verifies against "
                                          f"/data/git/fixpatch.\nThis board resolves commit {wanted}."),
              f"the release is on the record, with the full commit the board resolved: {released}")
        check(queued(admin, "PGU-4") == [("ticket_update", "ops")],
              f"and the owner is told: {queued(admin, 'PGU-4')}")
        code, output = cli(board, "director", "release-external-blocker", "PGU-4", "--ref", REF, "--reason", "again")
        check(code != 0 and f"ticket PGU-4 has no external blocker {REF}" in output,
              f"a release is one release: {output}")

    # The release submitted nothing: Ops takes the commit through the gate.
    submitted = canonical_app.perform_workflow_action(
        "PGU-4", "submit_to_audit", {"commit_hash": wanted[:12]}, caller_role="ops")
    check(submitted["state"] == "audit" and submitted["commit_hash"] == wanted,
          f"Ops submits the real commit to review: {submitted['state']} {submitted['commit_hash']}")
    check(not submitted["audit_signoff"], "and the review has not been skipped")


MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu960_syrd270_external_blockers.sql"


def run_upgrade(cluster) -> None:
    """A board provisioned before this change takes the migration, twice, and then works."""
    base = subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    before = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{base}:scripts/ticket_board/schema.sql"],
        check=True, capture_output=True, text=True,
    ).stdout
    check("ticket_board.external_blocker_pattern" not in before, "the upgrade starts from a schema without it")
    db = "upgraded"
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, before)
    for _ in range(2):
        t.psql(admin, MIGRATION.read_text())
    t.psql(admin, t.RBAC_PATH.read_text())
    t.seed_postgres_ticket(admin, "PGU-4", title="Preserve provisioning", state="in_progress", assignee="ops")
    service = t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE)
    t.psql(service, """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.set_blockers('PGU-4', ARRAY['syrd:SYRD-269'], 'Waiting on SYRD-269.');
""")
    check(blockers(admin, "PGU-4") == [(REF, False)], f"an upgraded board records it: {blockers(admin, 'PGU-4')}")
    try:
        t.psql(service, """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.set_blockers('PGU-4', ARRAY['pgu:PGU-9'], 'Mislabelled local ticket.');
""")
    except AssertionError as exc:
        own = str(exc)
    else:
        own = ""
    check("external blocker pgu:PGU-9 names a ticket on this board; block on PGU-9 instead" in own,
          f"the database itself refuses a qualified reference to its own board: {own!r}")
    constraint = t.psql(admin, """
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ticket_blockers_blocker_ticket_id_check';
""")
    check("ticket_id_pattern()" in constraint and "external_blocker_pattern()" in constraint,
          f"and its constraint admits both forms, still local ids: {constraint}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd270.") as tmp:
        with temporary_cluster(prefix="syrd270-", shutdown="immediate") as cluster:
            run(cluster, Path(tmp))
            run_upgrade(cluster)
    print(f"external_blocker_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
