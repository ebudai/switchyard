#!/usr/bin/env python3
"""SYRD-799: ./install tells each tenant's Director, once, through its own board, that a new release is ready.

The User relayed each release to every tenant's Director by hand (SYRD-791/796: sapi and otto). Now ./install ends
with scripts/release_notice.py, as root: each registered tenant whose board runs another release is told, through
`ticket_board.note_release_available` -- the one write root makes, for the database superuser alone -- and nothing is
upgraded. Here:
* the decision, with every outside fact stood in for: two tenants behind and one current give exactly two notices,
  with the three exact commands and the commits between; an unreachable board is listed and stops nothing; a tenant
  root's registry does not register is not looked at; a rerun tells nobody;
* the psql call, against a real cluster: the values reach the function as psql variables, so a notice holding quotes
  and a semicolon is stored as written;
* the board's side, on a production-built board (SYRD-772's rig, real server and listener): told once and 'already'
  after, one Director notice delivered once, and the write refused to every board role.
* SYRD-868: a tenant's notice names its own reports that the release resolves -- done upstream on a commit between its
  release and the new one -- above the subjects, so sixty subjects capped to twenty never drop it; a tenant with none
  gets no such line. Root reads them from each board's report_provenance as the database superuser (real cluster),
  and the commits between from git, merges included.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import release_notice as rn  # noqa: E402

CHECKS = 0
NEW, OLD_A, OLD_B = "a" * 40, "b" * 40, "c" * 40


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


# -- the decision ---------------------------------------------------------------------------------------------------
def test_who_is_told() -> None:
    tenants = [SimpleNamespace(project=p, database=f"{p}_ticket_board") for p in ("otto", "pie", "sapi", "syrd", "test11")]
    runs = {"otto": OLD_A, "sapi": OLD_B, "syrd": NEW}
    recorded: set[tuple[str, str]] = set()
    calls: list = []

    def current(project: str) -> str:
        if project == "pie":
            raise OSError("pie's board socket /run/pie-ticket-board/ticket-board.sock does not answer")
        return runs[project]

    def board(database: str, new: str, cur: str, text: str) -> str:
        calls.append((database, new, cur, text))
        if (database, new) in recorded:  # the board's own record: once per release
            return "already"
        recorded.add((database, new))
        return "told"

    def once() -> str:
        return rn.run(new=NEW, repo=ROOT, user="eric", tenants=lambda: (tenants, ["mefp: root's provision record "
                                                                                   "cannot name its owner and database"]),
                      registered=lambda project: project != "test11", current_release=current, tell_board=board,
                      subjects_for=lambda cur, new: [f"SYRD-{n}: change {n}" for n in range(3)])

    try:
        line = once()
    except Exception as exc:  # a crash is not an answer: the run must say what it found, whatever a board does
        line = f"raised {type(exc).__name__}: {exc}"
    check(not line.startswith("raised"), f"one unreachable board stops nothing: {line}")
    told = {database: text for database, _new, _cur, text in calls}
    check(sorted(told) == ["otto_ticket_board", "sapi_ticket_board"],
          f"two tenants behind: exactly two notices, the current one and the unregistered one untold: {sorted(told)}")
    sapi = told["sapi_ticket_board"]
    for action in ("preview-upgrade", "upgrade-tenant-release", "deploy-release"):
        check(f"switchyard privileged-action sapi {action} commit={NEW}" in sapi, f"sapi's notice carries {action}: {sapi}")
    check(f"switchyard release-status sapi" in sapi and NEW in sapi and OLD_B in sapi and "SYRD-0: change 0" in sapi
          and "Nothing has been upgraded" in sapi and "sapi" in sapi and "otto" not in sapi,
          f"and both commits, the commits between, the check, and that nothing was upgraded -- its own tenant's: {sapi}")
    check(line.startswith(f"switchyard: told 2 Directors release {NEW[:12]} is ready: otto, sapi")
          and "skipped syrd" in line and "not told:" in line and "pie (pie's board socket" in line and "mefp:" in line
          and "test11" not in line,
          f"one line says who was told, who skipped, and who was not, and why: {line}")
    calls.clear()
    again = once()
    check(again.startswith("switchyard: told 0 Directors") and "otto (already told)" in again
          and "sapi (already told)" in again, f"a rerun tells nobody: {again}")


def test_the_notice_is_bounded() -> None:
    text = rn.message("sapi", OLD_A, NEW, [f"SYRD-{n}: " + "x" * 300 for n in range(60)])
    check(len(text) <= rn.MAX_MESSAGE_CHARS and "... and 40 more" in text and f"commit={NEW}" in text,
          f"sixty long commits: capped, and the commands are still there: {len(text)}")
    text = rn.message("sapi", OLD_A, NEW, [])
    check("not in the installing checkout" in text, "commits it cannot read are said to be unknown")


def test_the_notice_names_the_reports_it_resolves() -> None:
    tenants = [SimpleNamespace(project=p, database=f"{p}_ticket_board") for p in ("mefp", "sapi", "syrd")]
    runs = {"mefp": OLD_A, "sapi": OLD_B, "syrd": NEW}
    fixed, merged, later, theirs = "1" * 40, "2" * 40, "3" * 40, "4" * 40
    boards = {"syrd_ticket_board": [("SYRD-866", "sapi", fixed), ("SYRD-9", "sapi", merged[:12]),
                                    ("SYRD-900", "sapi", later), ("SYRD-901", "mefp", theirs)],
              "sapi_ticket_board": [], "mefp_ticket_board": []}
    told: dict[str, str] = {}

    def board(database: str, new: str, cur: str, text: str) -> str:
        told[database] = text
        return "told"

    rn.run(new=NEW, repo=ROOT, user="eric", tenants=lambda: (tenants, []), registered=lambda project: True,
           current_release=lambda project: runs[project], tell_board=board,
           subjects_for=lambda cur, new: [f"SYRD-{n}: " + "x" * 300 for n in range(60)],
           commits_for=lambda cur, new: ["5" * 40, fixed, merged], reports_on=lambda database: boards[database])
    sapi, mefp = told["sapi_ticket_board"], told["mefp_ticket_board"]
    line = "This release resolves your reports SYRD-9, SYRD-866 on syrd's board."
    check(line in sapi and "SYRD-900" not in sapi and "SYRD-901" not in sapi and "... and 40 more" in sapi
          and sapi.index(line) < sapi.index("What changed"),
          f"sapi's notice names its reports closed between its release and the new one, short commits too, above the "
          f"capped subjects; not one closed later, nor another tenant's: {sapi}")
    check("resolves your report" not in mefp, f"mefp, whose report is not in this release, gets no such line: {mefp}")


def test_root_reads_the_commits_between() -> None:
    parent, merge = "79280dab3b326d41ae1048464516b4fe2b8c35ea", "a1ac46226afb230a65107b8c95680ae82046d5ac"
    check(rn.commits(ROOT, parent, merge) == [merge] and rn.commits(ROOT, merge, "f" * 40) == [],
          "the commits between two releases, by full hash; none when that history is not there")
    first, joined = "e4dcc38e2ca5b6ecc6373bc4b096a12befc79525", "53f7f44d2869b0ff6f9ec8d5bc5f5fa31596b276"
    check(joined in rn.commits(ROOT, first, joined), "a merge too: a report may close on its integration merge")


def test_a_board_older_than_release_notices_says_so() -> None:
    error = ":<stdin>:1: ERROR:  function ticket_board.note_release_available(unknown, unknown, unknown) does not exist"
    try:
        rn.tell("sapi_ticket_board", NEW, OLD_A, "x",
                runner=lambda *_a, **_k: subprocess.CompletedProcess([], 3, "", error))
        said = "told"
    except RuntimeError as exc:
        said = str(exc)
    check("predates release notices" in said and "yourself" in said,
          f"a board whose release predates release notices: the reason says what to do, not psql's words: {said}")


def test_not_root_tells_nobody_and_fails_nothing() -> None:
    import os

    if os.geteuid() == 0:
        return
    done = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "release_notice.py"), "--source-repo", str(ROOT)],
                          capture_output=True, text=True, timeout=60)
    check(done.returncode == 0 and "only root can tell them" in done.stdout,
          f"run by another account it says so and succeeds, so ./install never fails on it: {done.stdout}{done.stderr}")


# -- the board's side, and the psql call ---------------------------------------------------------------------------
def test_the_board_records_it_once_and_only_root_may() -> None:
    import user_review_serial_release_test as urs
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd799-", shutdown="immediate") as cluster:
        admin = urs.build_board(fixture, cluster, ROOT, "board")
        board = urs.Board(fixture, cluster, admin, "board", "a")
        try:
            board.app.apply_workflow(urs.workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            fixture.seed_postgres_ticket(admin, "PGU-1", title="PGU-1", state="analysis", assignee="director",
                                         commit_exempt=True)
            board.one_pass()

            def as_superuser(text: str) -> str:
                """rn.tell's own command, its runuser prefix and socket swapped for this cluster's superuser."""
                def runner(command, **kwargs):
                    start = command.index("psql")
                    swapped = [part if not part.startswith("host=") else admin for part in command[start:]]
                    return subprocess.run(swapped, **kwargs)
                return rn.tell("board", NEW, OLD_A, text, runner=runner)

            hostile = "Release ready; it's `x'); DROP TABLE ticket_board.tickets; --` and \"quoted\" $1 :'notice'"
            check(as_superuser(hostile) == "told", "root tells the board: told")
            stored = board.sql("SELECT message FROM ticket_board.ticket_notification_queue WHERE dedupe_key = "
                               f"'release_available:{NEW}';")
            check(stored == hostile and board.sql("SELECT count(*) FROM ticket_board.tickets;") == "1",
                  f"the notice is stored exactly as written, quotes and all; nothing ran: {stored!r}")
            sent = board.one_pass()
            to_director = [m for t, m in sent if t == "pgu-director:0.0" and m == hostile]
            check(len(to_director) == 1 and board.one_pass() == [], f"one Director notice, delivered once: {sent}")
            try:
                again = as_superuser("again")
            except Exception as exc:  # e.g. the record's own key refusing a second row: not the answer asked for
                again = f"raised {type(exc).__name__}: {str(exc)[-120:]}"
            check(again == "already" and board.sql(
                      f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE dedupe_key = 'release_available:{NEW}';") == "0",
                  f"the same release again: 'already', and nothing is queued: {again}")
            refused = {}
            for role in ("ticket_board_service", "ticket_board_listener", "director", "main"):
                try:
                    board.sql(f"SET ROLE {role}; SELECT ticket_board.note_release_available('{'d' * 40}', '{OLD_A}', 'x');")
                    refused[role] = "allowed"
                except Exception as exc:
                    refused[role] = "denied" if "permission denied" in str(exc) else str(exc)[:120]
            check(set(refused.values()) == {"denied"}, f"no board role may make root's write: {refused}")
            check(board.sql("SELECT count(*) FROM ticket_board.release_notices;") == "1",
                  "and none of them recorded anything")

            # SYRD-868: root reads the reports this board closed done on a commit, with who filed them.
            for ticket, state, commit in (("PGU-2", "done", "1" * 40), ("PGU-3", "cancelled", "2" * 40),
                                          ("PGU-4", "done", ""), ("PGU-5", "done", "3" * 40)):
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             commit_exempt=True)
                board.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET "
                          f"state = '{state}', commit_hash = '{commit}' WHERE id = '{ticket}';")
                if ticket != "PGU-5":  # not a report: filed on this board itself
                    board.sql("INSERT INTO ticket_board.report_provenance (ticket_id, origin_project, recorded_from) "
                              f"VALUES ('{ticket}', 'sapi', 'filing');")

            def superuser(command, **kwargs):
                start = command.index("psql")
                return subprocess.run([part if not part.startswith("host=") else admin
                                       for part in command[start:]], **kwargs)

            check(rn.closed_reports("board", runner=superuser) == [("PGU-2", "sapi", "1" * 40)],
                  "root reads exactly the reports closed done on a commit, and who filed each")
        finally:
            board.close()

        # A quiet board, when Directors upgrade: its only Director ticket is Done, so the notice's anchor comes from the
        # trace of the last hand-over to the Director. It is still delivered: a notice recorded and never delivered
        # would be skipped as "already told" by every later install (Audit of 54b6cb7e).
        quiet_admin = urs.build_board(fixture, cluster, ROOT, "quiet", roles=False)
        quiet = urs.Board(fixture, cluster, quiet_admin, "quiet", "q")
        try:
            quiet.app.apply_workflow(urs.workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            fixture.seed_postgres_ticket(quiet_admin, "PGU-1", title="PGU-1", state="analysis", assignee="director",
                                         commit_exempt=True)
            quiet.act("PGU-1", "route", "director", state="in_progress", assignee="main")
            quiet.one_pass()
            quiet.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'done', "
                      "assignee = 'unassigned' WHERE id = 'PGU-1';")
            quiet.sql("INSERT INTO ticket_board.notification_trace (ticket_id, target_role, kind, event) "
                      "SELECT 'PGU-1', 'director', 'transition', 'send' WHERE NOT EXISTS (SELECT FROM "
                      "ticket_board.notification_trace WHERE ticket_id = 'PGU-1' AND target_role = 'director' "
                      "AND kind = 'transition' AND event = 'send');")
            anchor = quiet.sql("SELECT ticket_board.provider_hold_anchor('director') || '|' || (SELECT state FROM "
                               "ticket_board.tickets WHERE id = 'PGU-1');")
            check(anchor == "PGU-1|done", f"the board's only Director ticket is done, and is the anchor: {anchor}")

            def quiet_tell(text: str) -> str:
                def runner(command, **kwargs):
                    start = command.index("psql")
                    return subprocess.run([part if not part.startswith("host=") else quiet_admin
                                           for part in command[start:]], **kwargs)
                return rn.tell("quiet", NEW, OLD_A, text, runner=runner)

            check(quiet_tell("Release ready for a quiet board.") == "told", "told")
            delivered = [m for t, m in quiet.one_pass() if t == "pgu-director:0.0" and m == "Release ready for a quiet board."]
            check(len(delivered) == 1 and quiet.one_pass() == [],
                  f"delivered to the Director once, though its anchor ticket is done: {delivered}")
        finally:
            quiet.close()


def main() -> int:
    for test in (test_who_is_told, test_the_notice_is_bounded, test_the_notice_names_the_reports_it_resolves,
                 test_root_reads_the_commits_between, test_a_board_older_than_release_notices_says_so,
                 test_not_root_tells_nobody_and_fails_nothing,
                 test_the_board_records_it_once_and_only_root_may):
        test()
    print(f"release_notice_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
