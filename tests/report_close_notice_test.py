#!/usr/bin/env python3
"""SYRD-827: when a ticket a tenant filed upstream ends, that tenant's Director is told once, on its own board.

Two production-built boards in one cluster (schema.sql, roles, rbac.sql, every migration; real HTTP servers and the
real notify listener with an injected sender -- SYRD-772's rig): an upstream board that holds the reports, with a
report secret and binding key as a provisioned board has, and a reporting board ("sapi") whose listener pass asks it,
through root's registry record and the bound report credential file, exactly as on a host. The upstream board's
commit repository is a scratch git repository in which one closing commit is in the reporter's release and one is
not.

Here:
* nothing is told while a report is open;
* a report closed Done on a commit the reporter's release contains gives one Director notice on the reporting board,
  with the commit; a second pass tells nothing more;
* SYRD-868: one closed Done on a commit the reporter's release does not contain is recorded with no notice, then or
  later (the release notice that carries it names it); closed again after a reopen, the same;
* a cancelled report gives its Director's reason, a commit or none; a Done close with no commit, or on a commit
  whether the release contains cannot be told, gives one notice each;
* SYRD-868, the Director's cases: a no-code close (the board's own no-code submission, then Done) carries the reason
  that submission recorded -- the configuration step -- and a won't-fix closed as Done with no commit carries its
  Director's latest comment; no release will tell either reporter anything;
* a report reopened and closed again tells again, once;
* a close older than 30 days is recorded, not told; another project's report is never told to this one;
* the read refuses a missing or shared credential and gives each project its own reports only;
* an unreachable upstream tells nothing and is logged once, not per pass;
* a listener that is not the service (a test's) never asks;
* one bad entry in an ancestor's answer -- malformed, or refused by this board -- is skipped and logged once, and the
  valid entries beside it and from the next ancestor are still told (Director, SYRD-827 DAT);
* an upstream title or reason reaches the Director with no control character: no escape sequence, carriage return or
  bell is typed into its pane; its newlines are kept.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import user_review_serial_release_test as urs  # noqa: E402 - its production-built board, listener and helpers

CHECKS = 0
SECRET, BINDING = "s827-shared-report-secret", "s827-binding-key-of-the-upstream-board"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def git(repo: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def history(scratch: Path) -> tuple[Path, str, str, str]:
    """A repository: release R contains commit IN; commit OUT is on a branch R does not have."""
    repo = scratch / "control"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for name in ("base", "in", "release"):
        git(repo, "commit", "-q", "--allow-empty", "-m", name)
        if name == "in":
            inside = git(repo, "rev-parse", "HEAD")
    release = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "side", "HEAD~2")
    git(repo, "commit", "-q", "--allow-empty", "-m", "out")
    outside = git(repo, "rev-parse", "HEAD")
    return repo / ".git", release, inside, outside


def report(board, admin_url: str, ticket: str, project: str, *, title: str) -> None:
    board.fixture.seed_postgres_ticket(admin_url, ticket, title=title, state="analysis", assignee="unassigned",
                                       commit_exempt=True)
    board.sql(f"INSERT INTO ticket_board.report_provenance (ticket_id, origin_project, external_source_ref, "
              f"recorded_from) VALUES ('{ticket}', '{project}', '{project}-1', 'filing');")


def end(board, ticket: str, state: str, commit: str = "", *, days_ago: int = 0) -> None:
    """As the workflow leaves it: the terminal stage, its commit, and when it changed."""
    board.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET "
              f"state = '{state}', assignee = 'unassigned', commit_hash = '{commit}', "
              f"updated_at = clock_timestamp() - interval '{days_ago} days' WHERE id = '{ticket}';")


def reopen(board, ticket: str) -> None:
    """A reopen, as the board records one (tickets_zz_reopen_record): the record, then the open stage."""
    board.sql("INSERT INTO ticket_board.ticket_reopen_records (ticket_id, reopened_by, from_state, to_state, assignee, "
              f"commit_hash, commit_exempt, signoffs) SELECT id, 'director', state, 'in_progress', assignee, "
              f"commit_hash, commit_exempt, '{{}}'::jsonb FROM ticket_board.tickets WHERE id = '{ticket}';")
    board.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'in_progress' "
              f"WHERE id = '{ticket}';")


def read_status(base: str, token: str | None, release: str = "") -> tuple[int, object]:
    url = f"{base}/api/report-status" + (f"?release={release}" if release else "")
    req = urllib.request.Request(url, headers={"X-Ticket-Board-Report-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def authors(board, ticket: str) -> str:
    return board.sql(f"SELECT string_agg(who, ',') FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket}';")


def comments(board, ticket: str) -> str:
    return board.sql("SELECT string_agg(who || ': ' || text, ' || ' ORDER BY position) FROM ticket_board.ticket_comments "
                     f"WHERE ticket_id = '{ticket}';")


def no_code_state(board) -> str:
    return board.sql("SELECT (SELECT count(*) FROM ticket_board.ticket_no_code_reasons)::text || ' ' || "
                     "(SELECT count(*) FROM pg_trigger WHERE tgname = 'tickets_z_no_code_reason')::text;") + " " + \
        comments(board, "PGU-16")


def queue_state(board) -> str:
    """What the board queued and traced, for a failure's message."""
    queue = board.sql("SELECT string_agg(id || ':' || kind || ':' || target_role || ':' || coalesce(payload->>'kind', '') "
                      "|| ':' || coalesce(claimed_at::text, '-') || ':' || attempts, ' | ') "
                      "FROM ticket_board.ticket_notification_queue;")
    trace = board.sql("SELECT string_agg(coalesce(event, '') || ':' || coalesce(busy_reason, '') || ':' || "
                      "coalesce(kind, ''), ' | ' ORDER BY id) FROM ticket_board.notification_trace;")
    return f"queue={queue} trace={trace}"


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.levelname} {record.getMessage()}")


def test_report_close_notices() -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board import report_closure
    from scripts.ticket_board.report_follow_up import bound_token
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd827-", shutdown="immediate",
                           initdb_args=("--username=postgres", "--encoding=UTF8")) as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd827.") as raw:
        scratch = Path(raw)
        git_dir, release, inside, outside = history(scratch)
        os.environ["TICKET_BOARD_COMMIT_GIT_DIR"] = str(git_dir)  # the upstream board's own commit repository
        up_admin = urs.build_board(fixture, cluster, ROOT, "up")
        up = urs.Board(fixture, cluster, up_admin, "up", "u")
        rep_admin = urs.build_board(fixture, cluster, ROOT, "rep", roles=False)
        rep = urs.Board(fixture, cluster, rep_admin, "rep", "r")
        # The upstream board as provisioned: a report secret and a binding key, served over HTTP.
        served = fixture.TicketBoardServer(("127.0.0.1", 0), up.app, director_notifier=fixture.QuietNotifier(),
                                           report_token=SECRET, report_binding_key=BINDING)
        threading.Thread(target=served.serve_forever, daemon=True).start()
        upstream = f"http://127.0.0.1:{served.server_port}"
        try:
            for board in (up, rep):
                board.app.apply_workflow(urs.workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            # The reporting board's Director has a ticket to carry a notice, as a working board does.
            fixture.seed_postgres_ticket(rep_admin, "PGU-1", title="anchor", state="analysis", assignee="director",
                                         commit_exempt=True)
            rep.one_pass()
            # Root's registry and the credential file, as `switchyard upgrade --only upstream-report` leaves them.
            registry = scratch / "registry"
            registry.mkdir()
            schema = "switchyard.project-registry.v1"
            (registry / "sapi.json").write_text(json.dumps({"schema": schema, "slug": "sapi", "name": "sapi",
                                                            "report_parent": {"project": "syrd", "url": upstream,
                                                                              "ticket_prefix": "PGU"}}))
            (registry / "syrd.json").write_text(json.dumps({"schema": schema, "slug": "syrd", "name": "syrd"}))
            credential = scratch / "home" / ".config" / "sapi" / "upstream-report.env"
            credential.parent.mkdir(parents=True)
            credential.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN={bound_token(BINDING, 'sapi')}\n")
            environ = {"SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry), "HOME": str(scratch / "home"),
                       report_closure.INTERVAL_ENV: "0"}
            records = Records()
            log = logging.getLogger("syrd827.listener")
            log.addHandler(records)
            log.setLevel(logging.INFO)
            listener = SimpleNamespace(report_closures=True, project="sapi", logger=log,
                                       _report_closure_build=release)

            with psycopg.connect(rep.listener_url, autocommit=True) as conn:  # as the listener connects
                def tell() -> list[str]:
                    report_closure.run(listener, conn, environ=environ)
                    return [m for t, m in rep.one_pass() if t == "pgu-director:0.0"]

                report(up, up_admin, "PGU-7", "sapi", title="Restart drops the pane")
                report(up, up_admin, "PGU-8", "sapi", title="Banner covers the composer")
                report(up, up_admin, "PGU-9", "sapi", title="A duplicate")
                report(up, up_admin, "PGU-10", "sapi", title="Long ago")
                report(up, up_admin, "PGU-11", "mefp", title="A report of another project")
                for ticket in ("PGU-12", "PGU-13", "PGU-14", "PGU-15", "PGU-16"):
                    report(up, up_admin, ticket, "sapi", title=f"Report {ticket}")
                check(tell() == [] and rep.sql("SELECT count(*) FROM ticket_board.report_closures;") == "0",
                      "nothing is told, or recorded, while every report is open")

                end(up, "PGU-7", "done", inside)
                told = tell()
                check(len(told) == 1 and "PGU-7" in told[0] and "is now done" in told[0] and "syrd" in told[0]
                      and inside in told[0] and "already contains it" in told[0]
                      and f"ticket-board-read --board-url {upstream} ticket PGU-7" in told[0],
                      f"a report closed Done: one notice, its commit, in this release, and how to read it: {told} "
                      f"log={records.lines} {queue_state(rep)}")
                check(tell() == [], "and the next pass tells nothing more")

                end(up, "PGU-8", "done", outside)
                check(tell() == [] and tell() == [] and rep.sql(
                          "SELECT string_agg(close_key || ':' || told::text, ',') FROM ticket_board.report_closures "
                          "WHERE ticket_id = 'PGU-8';") == "done#0:false",
                      "SYRD-868: a commit the release does not have: recorded, and no notice, then or later -- the "
                      "release that carries it names it")

                fixture.TEST_WRITE_TOKEN = up.server.write_token  # the rig keeps one token: the last board built's
                commented = up.act("PGU-9", "add_comment", "director", text="Won't fix: a duplicate of PGU-7.")
                fixture.TEST_WRITE_TOKEN = rep.server.write_token
                end(up, "PGU-9", "cancelled")
                told = tell()
                check(len(told) == 1 and "is now cancelled" in told[0] and "Won't fix: a duplicate of PGU-7." in told[0],
                      f"a cancelled report: its Director's reason: {told} {str(commented)[:300]} {authors(up, 'PGU-9')}")

                end(up, "PGU-12", "done", "0" * 40)  # a commit no repository of the upstream board's has
                # A no-code close, through the board's own no-code submission, then Done.
                # PGU-13 was filed exempt, as a report may be; PGU-16 was not, so the no-code move exempts it.
                up.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'in_progress', "
                       "assignee = CASE id WHEN 'PGU-13' THEN 'main' ELSE 'app' END, commit_exempt = (id = 'PGU-13') "
                       "WHERE id IN ('PGU-13', 'PGU-16');")
                config = "Only configuration: set TICKET_BOARD_REPORT_CLOSE_SECONDS=300 in the tenant's board env."
                other = "Only configuration: give the listener unit Restart=always."
                fixture.TEST_WRITE_TOKEN = up.server.write_token
                submitted = up.act("PGU-13", "submit_to_audit_without_commit", "main", reason=config)
                exempted = up.act("PGU-16", "submit_to_audit_without_commit", "app", reason=other)
                later = up.act("PGU-13", "add_comment", "audit", text="Audit: no code, as said.")
                won_t = up.act("PGU-15", "add_comment", "director", text="Won't fix: this works as designed.")
                # An ordinary move of an exempt ticket is no no-code move: what it leaves newest is not its reason.
                chatter = up.act("PGU-15", "add_comment", "ops", text="Ops: looking at it.")
                routed = up.act("PGU-15", "route", "director", state="in_progress", assignee="ops")
                routed_to = up.sql("SELECT state || ':' || commit_exempt::text FROM ticket_board.tickets WHERE id = 'PGU-15';")
                fixture.TEST_WRITE_TOKEN = rep.server.write_token
                end(up, "PGU-13", "done")
                end(up, "PGU-14", "cancelled", outside)
                end(up, "PGU-15", "done")
                end(up, "PGU-16", "done")
                told = tell()
                said = {ticket: [m for m in told if f" {ticket} " in m] for ticket in ("PGU-12", "PGU-13", "PGU-14",
                                                                                       "PGU-15", "PGU-16")}
                check(all("ok" in answer for answer in (submitted, exempted, later, won_t, chatter, routed)) and len(told) == 5
                      and len(said["PGU-12"]) == 1 and "could not be told" in said["PGU-12"][0]
                      and len(said["PGU-14"]) == 1 and "is now cancelled" in said["PGU-14"][0],
                      f"where no release will tell -- cannot be told, no commit, cancelled with a commit the release "
                      f"lacks -- one notice each: {[a for a in (submitted, exempted, later, won_t, chatter, routed) if 'ok' not in a]} "
                      f"{no_code_state(up)}")
                check(len(said["PGU-13"]) == 1 and "closed with no commit, so no release will carry it" in said["PGU-13"][0]
                      and f"Reason, as its board recorded it: {config}" in said["PGU-13"][0],
                      f"a no-code close: its notice carries the reason its no-code submission recorded: {said['PGU-13']} "
                      f"{comments(up, 'PGU-13')}")
                check(len(said["PGU-16"]) == 1 and f"Reason, as its board recorded it: {other}" in said["PGU-16"][0],
                      f"and one the no-code move made exempt: {said['PGU-16']} {comments(up, 'PGU-16')}")
                check(len(said["PGU-15"]) == 1 and "Reason, as its board recorded it: Won't fix: this works as designed."
                      in said["PGU-15"][0] and routed_to == "in_progress:true",
                      f"a won't-fix closed as Done, routed on the way: its Director's reason: {said['PGU-15']} {routed_to}")

                reopen(up, "PGU-8")
                end(up, "PGU-8", "done", outside)
                check(tell() == [] and rep.sql("SELECT string_agg(close_key || ':' || told::text, ',' ORDER BY close_key) "
                                               "FROM ticket_board.report_closures WHERE ticket_id = 'PGU-8';")
                      == "done#0:false,done#1:false", "reopened and closed again outside the release: the same")

                reopen(up, "PGU-7")
                check(tell() == [], "a reopened report is open again: nothing is told")
                end(up, "PGU-7", "done", inside)
                told = tell()
                check(len(told) == 1 and "PGU-7" in told[0] and tell() == [],
                      f"closed again: told again, once: {told}")

                end(up, "PGU-10", "done", inside, days_ago=60)
                end(up, "PGU-11", "done", inside)
                check(tell() == [] and rep.sql("SELECT told::text FROM ticket_board.report_closures "
                                               "WHERE ticket_id = 'PGU-10';") == "false"
                      and rep.sql("SELECT count(*) FROM ticket_board.report_closures WHERE ticket_id = 'PGU-11';") == "0",
                      "a close older than 30 days is recorded, not told; another project's is neither")
                check(rep.sql("SELECT count(*) FROM ticket_board.report_closures WHERE told;") == "8",
                      "eight closes told in all: PGU-7 twice, PGU-9, PGU-12 to PGU-16")
                check(not [line for line in records.lines if line.startswith(("ERROR", "WARNING"))],
                      f"and no pass failed or warned along the way: {records.lines}")

            # One bad entry is one entry: two ancestors, syrd's answer holding a malformed entry and one this board
            # refuses (a reopen count past what a close records) beside a valid close; root2's answer a valid close.
            chained = scratch / "registry-two"
            chained.mkdir()
            (chained / "sapi.json").write_text(json.dumps({"schema": schema, "slug": "sapi", "name": "sapi",
                                                           "report_parent": {"project": "syrd", "url": "http://syrd.test",
                                                                             "ticket_prefix": "SYRD"}}))
            (chained / "syrd.json").write_text(json.dumps({"schema": schema, "slug": "syrd", "name": "syrd",
                                                           "report_parent": {"project": "root2", "url": "http://root2.test",
                                                                             "ticket_prefix": "ROOT"}}))
            (chained / "root2.json").write_text(json.dumps({"schema": schema, "slug": "root2", "name": "root2"}))
            second = credential.parent / "report-credentials" / "root2.env"
            second.parent.mkdir()
            second.write_text("TICKET_BOARD_TENANT_REPORT_TOKEN=syrdr1.sapi.root2-credential\n")
            hostile = "Evil\x1b[31mred\r\x07 title\u009b2J end"
            answers = {
                "http://syrd.test": {"reports": [
                    {"id": "syrd-40", "state": "done", "commit_hash": inside, "reopens": 0, "title": "lower-case id"},
                    {"id": "SYRD-41", "state": "done", "commit_hash": inside, "reopens": 10 ** 10, "title": "refused"},
                    # Shapes that fail inside the notice or its key, not in the board (Audit, round 2):
                    {"id": "SYRD-44", "state": "cancelled", "commit_hash": "", "reopens": 0, "title": "t", "reason": 5},
                    {"id": "SYRD-45", "state": "done", "commit_hash": inside, "reopens": "abc", "title": "t"},
                    {"id": "SYRD-46", "state": "cancelled", "commit_hash": "", "reopens": 0, "title": "t", "reason": ["x"]},
                    {"id": "SYRD-42", "state": "done", "commit_hash": inside, "reopens": 0, "title": "valid beside them",
                     "release_contains": True},
                    {"id": "SYRD-43", "state": "cancelled", "commit_hash": "", "reopens": 0, "title": hostile,
                     "reason": "first line\x1b]0;owned\x07\nsecond line\r\x7f\u0085done"}]},
                "http://root2.test": {"reports": [
                    {"id": "ROOT-5", "state": "done", "commit_hash": outside, "reopens": 0, "title": "from the next ancestor",
                     "release_contains": None},
                    # An answer that says not contained with no commit to contain: no release will carry it.
                    {"id": "ROOT-6", "state": "done", "commit_hash": "", "reopens": 0, "title": "no commit",
                     "release_contains": False}]},
            }
            records.lines.clear()
            with psycopg.connect(rep.listener_url, autocommit=True) as conn:
                two = {**environ, "SWITCHYARD_PROJECT_REGISTRY_DIR": str(chained)}
                told = []
                for _ in range(2):
                    report_closure.run(listener, conn, environ=two, fetch=lambda url, token, build: answers[url])
                    told += [m for t, m in rep.one_pass() if t == "pgu-director:0.0"]
            warned = [line for line in records.lines if line.startswith("WARNING")]
            check(sorted(m.split()[2] for m in told) == ["ROOT-5", "ROOT-6", "SYRD-42", "SYRD-43"],
                  f"the valid closes beside a bad entry, and from the next ancestor, are told: {told} {records.lines}")
            joined = "\n".join(warned)
            check(len(warned) == 5 and "'syrd-40' is malformed" in joined and "'SYRD-41' was refused by this board" in joined
                  and all(f"'SYRD-{n}' is malformed" in joined for n in (44, 45, 46))
                  and not any(line.startswith("ERROR") for line in records.lines),
                  f"each bad entry is logged once over two passes, as what it is, and no pass failed: {records.lines}")
            cancelled = next((m for m in told if "SYRD-43" in m), "")
            check(cancelled and not any(c in cancelled for c in "\x1b\r\x07\x7f\u009b\u0085")
                  and "Evil[31mred title2J end" in cancelled and "first line]0;owned\nsecond linedone" in cancelled,
                  f"an upstream title and reason arrive without control characters, newlines kept: {cancelled!r}")

            # The read itself.
            code, said = read_status(upstream, None)
            check(code == 403, f"no credential: refused: {code} {said}")
            code, said = read_status(upstream, SECRET)
            check(code == 403 and "own report credential" in str(said), f"the shared secret: refused: {code} {said}")
            code, said = read_status(upstream, bound_token(BINDING, "mefp"), release)
            check(code == 200 and [r["id"] for r in said["reports"]] == ["PGU-11"]
                  and said["reports"][0]["release_contains"] is True,
                  f"mefp's credential reads mefp's reports only: {said}")
            code, said = read_status(upstream, bound_token(BINDING, "sapi"), "not-a-commit")
            check(code == 400, f"a release that is not a commit: refused: {code} {said}")

            # An unreachable upstream: nothing told, logged once.
            dead = scratch / "registry-dead"
            dead.mkdir()
            (dead / "sapi.json").write_text(json.dumps({"schema": schema, "slug": "sapi", "name": "sapi",
                                                        "report_parent": {"project": "syrd", "url": "http://127.0.0.1:9",
                                                                          "ticket_prefix": "PGU"}}))
            (dead / "syrd.json").write_text(json.dumps({"schema": schema, "slug": "syrd", "name": "syrd"}))
            records.lines.clear()
            with psycopg.connect(rep.listener_url, autocommit=True) as conn:
                before = rep.sql("SELECT count(*) FROM ticket_board.report_closures;")
                for _ in range(3):
                    report_closure.run(listener, conn, environ={**environ, "SWITCHYARD_PROJECT_REGISTRY_DIR": str(dead)})
                warnings = [line for line in records.lines if line.startswith("WARNING")]
                check(len(warnings) == 1 and "syrd's report status could not be read" in warnings[0]
                      and rep.sql("SELECT count(*) FROM ticket_board.report_closures;") == before
                      and not any(t == "pgu-director:0.0" for t, _m in rep.one_pass()),
                      f"an unreachable upstream: nothing told, one warning over three passes: {records.lines}")

                asked: list[str] = []
                not_service = SimpleNamespace(project="sapi", logger=log)
                report_closure.run(not_service, conn, environ=environ, fetch=lambda *a, **k: asked.append(a) or {})
                check(asked == [], "a listener that is not the service asks nothing")
        finally:
            os.environ.pop("TICKET_BOARD_COMMIT_GIT_DIR", None)
            served.shutdown()
            served.server_close()
            rep.close()
            up.close()


def test_the_listener_service_alone_turns_it_on() -> None:
    text = (ROOT / "scripts/ticket_board/notify_listener.py").read_text(encoding="utf-8")
    check("listener.report_closures = True" in text and "report_closure.run(self, conn)" in text,
          "the listener service turns the pass on and runs it each loop")


def main() -> int:
    test_the_listener_service_alone_turns_it_on()
    test_report_close_notices()
    print(f"report_close_notice_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
