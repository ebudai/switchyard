#!/usr/bin/env python3
"""SYRD-541: oversized growth is reviewed before integration, with bounded Director exceptions.

Warnings alone did not keep files near the 1,250-line soft limit: the
pre-commit hook printed to the committer's stderr and nothing reached the
board. Now the board measures each candidate on its own commit cache and, once
a Director enables the review, refuses Audit's approval and the Director's
close while the candidate has an unresolved size finding -- growth the
candidate itself made past a file's allowance -- until it is split or reduced
or the Director approves a bounded exception at the measured size.

Production-built board (companion roles, schema.sql, the real
ticket-board-migrate, rbac.sql, a declared workflow) whose commit cache is a
disposable repository; the real write CLI against the real board server over
its Unix socket; the real pre-commit helper. The before-run is main's code and
SQL (7c573c7), in a child from a git archive.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0
NEW_FUNCTIONS = ("size_review_enabled", "size_ceilings", "record_size_scan", "approve_size_exception",
                 "enable_size_review", "ticket_size_review", "enforce_size_review")


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def lines(n: int, word: str = "x") -> str:
    return "".join(f"{word}{i} = {i}\n" for i in range(n))


def commit_files(repo: Path, branch: str, files: dict[str, str | None], message: str, *, start: str = "main") -> str:
    """A commit on `branch` (made from `start` if new) writing or deleting `files`."""
    existing = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", branch], capture_output=True)
    git(repo, "checkout", "-q", branch if existing.returncode == 0 else "-b", *(() if existing.returncode == 0 else (branch, start)))
    for path, text in files.items():
        target = repo / path
        if text is None:
            git(repo, "rm", "-q", path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(repo, "add", path)
    git(repo, "commit", "-qm", message)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from scripts.ticket_board.board_notifications import TicketBoardEventHub
    from scripts.ticket_board.server import TicketBoardUnixServer
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    has_review = (root / "scripts/ticket_board/size_review.py").exists()
    seen: dict = {"has_review": has_review}

    def build(cluster, db: str, *, migrate: bool) -> str:
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        if migrate:
            runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin},
                                    capture_output=True, text=True)
            assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        return admin

    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        # The project repository, which is also the board's commit cache.
        repo = cluster.root / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "t")
        base_files = {
            "scripts/edge.py": lines(1250), "scripts/big.py": lines(1300), "scripts/near.py": lines(1099),
            "scripts/small.py": lines(10), "scripts/ticket_board/schema.sql": lines(13000, "s"),
            "docs/long.md": lines(10), "third_party/vendored.py": lines(10), "tests/big_test.py": lines(1300),
        }
        for path, text in base_files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")

        db = "board"
        admin = build(cluster, db, migrate=True)
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean",
                               ticket_prefix="PGU", commit_git_dir=str(repo),
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")

        sock = cluster.root / "board.sock"
        servers: dict = {}

        def serve(role: str) -> None:
            """The real board server on its socket, admitting the caller as `role`."""
            if role not in servers:
                for srv, events in servers.values():
                    srv.shutdown()
                    srv.server_close()
                    events.close()
                servers.clear()
                events = TicketBoardEventHub(app)
                srv = TicketBoardUnixServer(sock, app, events=events, director_notifier=t.QuietNotifier(),
                                            role_authority=t.local_role_authority_as(role))
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                servers[role] = (srv, events)

        def cli(role: str, *args: str) -> tuple[int, str, str]:
            serve(role)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = write_cli.main(["--socket", str(sock), "--caller-role", role, *args])
                except SystemExit as exc:
                    code = int(exc.code or 0)
            return code, out.getvalue(), err.getvalue()

        def scalar(sql: str) -> str:
            return t.psql(admin, sql).strip()

        def at_audit(tid: str, sha: str) -> None:
            t.seed_postgres_ticket(admin, tid, title=tid, state="audit", assignee="audit", commit_hash=sha)

        def approve(tid: str) -> tuple[int, str, str]:
            return cli("audit", "audit-sign-off", tid, "--text", "reviewed")

        def new_candidate(sql: str) -> None:
            """The implementer's next candidate on the same ticket, without the round trip.

            Only the commit changes; the workflow's own triggers are suspended for
            this one fixture statement, as superuser, because the round trip
            (kick back, resubmit, inspect) is other machinery than this test's.
            """
            t.psql(admin, "SET session_replication_role = replica; " + sql)

        def state(tid: str) -> str:
            return scalar(f"SELECT state FROM ticket_board.tickets WHERE id = '{tid}';")

        def findings(tid: str) -> list:
            return json.loads(scalar(
                "SELECT coalesce(jsonb_agg(jsonb_build_array(path, state, reason, before_lines, after_lines, "
                f"ceiling_lines, revision) ORDER BY path), '[]')::text FROM ticket_board.size_findings WHERE ticket_id = '{tid}';"))

        # A candidate that crosses 1,250 with one file and grows another already over it.
        grow = commit_files(repo, "c1", {"scripts/edge.py": lines(1251), "scripts/big.py": lines(1305)}, "grow")
        # A legitimately no-code review: commit-exempt, no commit (SYRD-267).
        t.seed_postgres_ticket(admin, "PGU-30", title="PGU-30", state="audit", assignee="audit", commit_exempt=True)
        seen["no_code_before"] = (approve("PGU-30")[0], state("PGU-30"))
        # Before enabling, nothing is gated: defaults are unchanged.
        at_audit("PGU-1", grow)
        seen["disabled_approve"] = approve("PGU-1")[0]
        seen["disabled_state"] = state("PGU-1")
        if not has_review:
            seen["cli_has_enable"] = cli("director", "enable-size-review")[0]
            return seen

        seen["enable_preview"] = cli("director", "enable-size-review")
        seen["enabled_before_apply"] = scalar("SELECT ticket_board.size_review_enabled();")
        seen["enable_by_app"] = cli("app", "enable-size-review", "--apply")
        seen["enable"] = cli("director", "enable-size-review", "--apply")
        seen["inventory"] = json.loads(scalar("SELECT inventory::text FROM ticket_board.size_review_policy;"))

        # The supported integration step refuses unresolved growth.
        at_audit("PGU-2", grow)
        seen["refused"] = approve("PGU-2")
        seen["refused_state"] = state("PGU-2")
        seen["findings_after_refusal"] = findings("PGU-2")
        seen["packet"] = app.get_ticket("PGU-2").get("size_review")
        # Measuring the same candidate again changes nothing.
        approve("PGU-2")
        seen["findings_idempotent"] = findings("PGU-2")
        # Authority: App and Ops cannot except; a path with no open finding cannot be.
        seen["except_app"] = cli("app", "approve-size-exception", "PGU-2", "--path", "scripts/edge.py",
                                 "--rationale", "x", "--apply")
        seen["except_ops"] = cli("ops", "approve-size-exception", "PGU-2", "--path", "scripts/edge.py",
                                 "--rationale", "x", "--apply")
        seen["except_unfound"] = cli("director", "approve-size-exception", "PGU-2", "--path", "scripts/small.py",
                                     "--rationale", "x", "--apply")
        seen["except_preview"] = cli("director", "approve-size-exception", "PGU-2", "--path", "scripts/edge.py",
                                     "--rationale", "one cohesive owner")
        seen["exceptions_after_preview"] = scalar("SELECT count(*) FROM ticket_board.size_exceptions;")
        cli("director", "approve-size-exception", "PGU-2", "--path", "scripts/edge.py",
            "--rationale", "one cohesive owner", "--apply")
        seen["one_excepted_still_refused"] = approve("PGU-2")[0]
        seen["except_standing"] = cli("director", "approve-size-exception", "PGU-2", "--path", "scripts/big.py",
                                      "--rationale", "the transaction core; no further growth", "--standing", "--apply")
        seen["approved"] = approve("PGU-2")
        seen["approved_state"] = state("PGU-2")
        seen["packet_approved"] = app.get_ticket("PGU-2").get("size_review")
        seen["findings_excepted"] = findings("PGU-2")
        seen["exception_comment"] = scalar("SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-2' "
                                           "AND text LIKE 'Size exception%' ORDER BY position LIMIT 1;")

        # The close: the integration commit is measured too. A cherry-pick onto main
        # that also grows another file is refused; the audited candidate itself is not.
        git(repo, "checkout", "-q", "main")
        git(repo, "cherry-pick", grow)
        (repo / "tests/big_test.py").write_text(lines(1301))
        git(repo, "commit", "-qam", "integration with extra growth")
        integration = git(repo, "rev-parse", "HEAD")
        # The Director's recheck before pushing an integration that changed the tree: it moves nothing.
        seen["measure_by_app"] = cli("app", "measure-size", "PGU-2", "--commit", integration)
        measured = cli("director", "measure-size", "PGU-2", "--commit", integration)
        seen["measure"] = (measured[0], [(f["path"], f["state"], f["after"]) for f in json.loads(measured[1] or "{}").get("findings", [])
                                         if f["state"] == "open"], state("PGU-2"))
        seen["close_refused"] = cli("director", "mark-done", "PGU-2", "--commit-hash", integration)
        seen["close_refused_state"] = state("PGU-2")
        seen["close_ok"] = cli("director", "mark-done", "PGU-2", "--commit-hash", grow)[0]
        seen["close_ok_state"] = state("PGU-2")
        git(repo, "reset", "-q", "--hard", "HEAD~2")  # main back where it was

        # A standing exception is the file's allowance elsewhere; past it is renewed review.
        within = commit_files(repo, "c3", {"scripts/big.py": lines(1305)}, "within the standing ceiling")
        beyond = commit_files(repo, "c4", {"scripts/big.py": lines(1306)}, "past it")
        at_audit("PGU-3", within)
        seen["standing_within"] = (approve("PGU-3")[0], findings("PGU-3"))
        at_audit("PGU-4", beyond)
        seen["standing_beyond"] = (approve("PGU-4")[0], findings("PGU-4"))
        # A ticket-scoped exception does not travel; one ticket's growth is not another's allowance.
        other_edge = commit_files(repo, "c5", {"scripts/edge.py": lines(1251)}, "edge again")
        at_audit("PGU-5", other_edge)
        seen["scoped_does_not_travel"] = (approve("PGU-5")[0], findings("PGU-5"))

        # Repeated candidate growth on one ticket: one finding, updated in place and
        # reopened past its exception; a reduction resolves it.
        c6a = commit_files(repo, "c6", {"scripts/big.py": lines(1320)}, "grow a")
        at_audit("PGU-6", c6a)
        approve("PGU-6")
        seen["repeat_first"] = findings("PGU-6")
        c6b = commit_files(repo, "c6", {"scripts/big.py": lines(1330)}, "grow b")
        new_candidate(f"UPDATE ticket_board.tickets SET commit_hash = '{c6b}' WHERE id = 'PGU-6';")
        seen["stale_except"] = cli("director", "approve-size-exception", "PGU-6", "--path", "scripts/big.py",
                                   "--rationale", "x", "--apply")
        approve("PGU-6")
        seen["repeat_second"] = findings("PGU-6")
        cli("director", "approve-size-exception", "PGU-6", "--path", "scripts/big.py", "--rationale", "bounded",
            "--apply")
        c6c = commit_files(repo, "c6", {"scripts/big.py": lines(1331)}, "grow c")
        new_candidate(f"UPDATE ticket_board.tickets SET commit_hash = '{c6c}' WHERE id = 'PGU-6';")
        seen["reopened"] = (approve("PGU-6")[0], findings("PGU-6"))
        c6d = commit_files(repo, "c6", {"scripts/big.py": lines(1290)}, "split out")
        new_candidate(f"UPDATE ticket_board.tickets SET commit_hash = '{c6d}' WHERE id = 'PGU-6';")
        seen["reduced"] = (approve("PGU-6")[0], findings("PGU-6"))
        seen["notices_pgu6"] = scalar("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-6' "
                                      "AND event = 'enqueue' AND detail -> 'payload' ->> 'change_summary' LIKE 'size review%';")

        # Debt that does not grow, renames, new files, deletions, scope.
        debt = commit_files(repo, "c7", {"scripts/small.py": lines(11), "scripts/big.py": lines(1299)}, "debt")
        renamed = commit_files(repo, "c8", {"scripts/big.py": None, "scripts/core.py": lines(1300) + "extra = 1\n"},
                               "rename and grow")
        new_file = commit_files(repo, "c9", {"scripts/fresh.js": lines(1260), "docs/long.md": lines(5000),
                                             "docs/diagram.txt": lines(4000),
                                             "third_party/vendored.py": lines(3000),
                                             "scripts/ticket_board/schema.sql": lines(14000, "s")}, "new and scope")
        delete = commit_files(repo, "c10", {"scripts/big.py": None}, "delete")
        near = commit_files(repo, "c11", {"scripts/near.py": lines(1100)}, "warn band")
        for tid, sha in (("PGU-7", debt), ("PGU-8", renamed), ("PGU-9", new_file), ("PGU-10", delete),
                         ("PGU-11", near)):
            at_audit(tid, sha)
            seen[f"case_{tid}"] = (approve(tid)[0], findings(tid),
                                   [(f["path"], f["before"], f["after"], f["band"], f["finding"])
                                    for f in (app.get_ticket(tid).get("size_review") or {}).get("files", [])])

        # schema.sql's growth that its candidate's own migration carries is not a finding.
        block = lines(60, "m")
        mirrored = commit_files(repo, "c14", {"scripts/ticket_board/migrations/pgu999_x.sql": "BEGIN;\n" + block + "COMMIT;\n",
                                              "scripts/ticket_board/schema.sql": lines(13000, "s") + block}, "mirrored")
        at_audit("PGU-18", mirrored)
        seen["mirrored"] = (approve("PGU-18")[0], findings("PGU-18"))

        # Every declared approval is gated, not only Audit's.
        t.seed_postgres_ticket(admin, "PGU-19", title="PGU-19", state="inspection", assignee="inspector",
                               commit_hash=grow, needs_inspection=True)
        seen["inspector_approve"] = cli("inspector", "inspector-sign-off", "PGU-19")
        t.seed_postgres_ticket(admin, "PGU-20", title="PGU-20", state="user_review", assignee="user", commit_hash=grow,
                               audit_signoff=True, needs_user_signoff=True, user_signoff=False)
        seen["user_approve"] = cli("user", "user-sign-off", "PGU-20")
        seen["approval_states"] = (state("PGU-19"), state("PGU-20"))

        # The recovery windows are excluded, and only they: override and a merge's own close.
        at_audit("PGU-21", grow)
        seen["override_move"] = (cli("director", "override-move", "PGU-21", "--state", "director_review",
                                     "--assignee", "director")[0], state("PGU-21"))
        at_audit("PGU-22", grow)
        at_audit("PGU-23", grow)
        approve("PGU-22")  # its finding is open
        seen["merge"] = (cli("director", "merge", "PGU-22", "--target-id", "PGU-23")[0], state("PGU-22"))
        seen["merge_target_still_gated"] = (approve("PGU-23")[0], state("PGU-23"))

        # A stale measurement does not stand in for this action's: a path that skips
        # the board's measurement is refused; the board's own path measures again.
        clean = commit_files(repo, "c15", {"scripts/small.py": lines(13)}, "clean")
        at_audit("PGU-24", clean)
        cli("director", "measure-size", "PGU-24", "--commit", clean)
        t.psql(admin, "UPDATE ticket_board.size_scans SET scanned_at = clock_timestamp() - interval '20 minutes' "
                      "WHERE ticket_id = 'PGU-24';")
        try:
            app.perform_workflow_action("PGU-24", "audit_sign_off", {}, caller_role="audit")
            seen["stale"] = "ALLOWED"
        except Exception as exc:  # noqa: BLE001 -- the refusal is the answer
            seen["stale"] = str(exc).splitlines()[0]
        seen["fresh"] = (approve("PGU-24")[0], state("PGU-24"))

        # No code, after enabling: still decided by its other gates alone. Without
        # the exemption a missing commit is refused; with a commit, it is measured.
        t.seed_postgres_ticket(admin, "PGU-31", title="PGU-31", state="audit", assignee="audit", commit_exempt=True)
        seen["no_code_after"] = (approve("PGU-31")[0], state("PGU-31"))
        t.seed_postgres_ticket(admin, "PGU-32", title="PGU-32", state="audit", assignee="audit")
        seen["missing_commit"] = approve("PGU-32")
        t.seed_postgres_ticket(admin, "PGU-33", title="PGU-33", state="audit", assignee="audit", commit_exempt=True,
                               commit_hash=grow)
        seen["coded_exempt"] = (approve("PGU-33")[0], [row[:3] for row in findings("PGU-33")])

        # schema.sql growth no migration proves: refused, excepted at its measured size,
        # a fresh rescan of the same candidate passes, and growth past it is refused again.
        schema_only = commit_files(repo, "c16", {"scripts/ticket_board/schema.sql": lines(13000, "s") + lines(40, "q")},
                                   "schema-only sql")
        at_audit("PGU-34", schema_only)
        seen["schema_only_refused"] = (approve("PGU-34")[0], findings("PGU-34"))
        seen["schema_only_except"] = cli("director", "approve-size-exception", "PGU-34", "--path",
                                         "scripts/ticket_board/schema.sql", "--rationale", "one reviewed view",
                                         "--standing", "--apply")[0]
        seen["schema_only_rescan"] = (approve("PGU-34")[0], state("PGU-34"))
        # The same growth elsewhere is within the standing ceiling on a fresh scan;
        # one line past it is refused, the ceiling recorded on the finding.
        at_audit("PGU-36", schema_only)
        seen["schema_within"] = (approve("PGU-36")[0], findings("PGU-36"))
        beyond_schema = commit_files(repo, "c17", {"scripts/ticket_board/schema.sql": lines(13000, "s") + lines(41, "q")},
                                     "one more line")
        at_audit("PGU-35", beyond_schema)
        seen["schema_beyond"] = (approve("PGU-35")[0], findings("PGU-35"))

        # Scanner failure fails closed until a successful rescan.
        failing = commit_files(repo, "c12", {"scripts/small.py": lines(12)}, "small")
        at_audit("PGU-12", failing)
        git(repo, "branch", "-q", "-m", "main", "trunk")
        seen["scan_failed"] = (approve("PGU-12")[0], findings("PGU-12"))
        seen["scan_failed_except"] = cli("director", "approve-size-exception", "PGU-12", "--path", "*",
                                         "--rationale", "x", "--apply")
        git(repo, "branch", "-q", "-m", "trunk", "main")
        seen["rescanned"] = (approve("PGU-12")[0], findings("PGU-12"))

        # A commit the board cannot measure is refused, not waved through.
        at_audit("PGU-14", "ab" * 20)  # a full SHA the cache does not have: a failed scan, on the ticket
        seen["unknown_commit"] = (approve("PGU-14"), findings("PGU-14"))
        at_audit("PGU-16", "abc1234")  # not even resolvable to a full SHA: nothing to record against
        seen["unmeasured"] = approve("PGU-16")
        # Cancelling is not integration: an open finding never stops it.
        at_audit("PGU-17", grow)
        approve("PGU-17")  # refused: the finding is now open
        kicked = cli("audit", "audit-kick-back", "PGU-17", "--reason", "split edge.py first")[0]
        seen["cancel_with_finding"] = (kicked, state("PGU-17"), next((f[1] for f in findings("PGU-17")), None),
                                       cli("director", "cancel", "PGU-17", "--reason", "superseded")[0], state("PGU-17"))
        # The database decides authority itself, whatever the board admitted.
        sql_refusals = []
        for statement in ("SELECT ticket_board.approve_size_exception('PGU-5', 'scripts/edge.py', 'x', false, true)",
                          "SELECT ticket_board.enable_size_review(repeat('a', 40), '[]'::jsonb, '[]'::jsonb, false)"):
            try:
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "app")
                    conn.execute(statement)
                sql_refusals.append("ALLOWED")
            except Exception as exc:  # noqa: BLE001 -- the refusal is the answer
                sql_refusals.append(str(exc).splitlines()[0])
        seen["sql_refusals"] = sql_refusals
        # A commit-carrying submission is measured too, so the packet is there when Audit arrives.
        submitted = commit_files(repo, "c13", {"scripts/edge.py": lines(1260)}, "submitted")
        t.seed_postgres_ticket(admin, "PGU-15", title="PGU-15", state="in_progress", assignee="app")
        serve("app")
        from scripts.ticket_board.write_client import TicketBoardWriteClient
        TicketBoardWriteClient(socket_path=str(sock), caller_role="app")._ticket_action(
            "PGU-15", "submit_to_audit", {"commit_hash": submitted})
        seen["submitted"] = (state("PGU-15"), findings("PGU-15"))

        # Emergency recovery is not a size transition; the review does not touch it.
        at_audit("PGU-13", grow)
        seen["force_move"] = (cli("director", "force-move", "PGU-13", "--state", "director_review",
                                  "--assignee", "director")[0], state("PGU-13"))

        # Schema: a fresh schema.sql board installs what the migrated board has.
        fresh = build(cluster, "fresh", migrate=False)
        names = ",".join(f"'{n}'" for n in NEW_FUNCTIONS)
        definitions = ("SELECT string_agg(p.proname || md5(pg_get_functiondef(p.oid)), ',' ORDER BY p.proname) "
                       f"FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ({names});")
        tables = ("SELECT string_agg(table_name || ':' || column_name || ':' || data_type, ',' ORDER BY table_name, "
                  "ordinal_position) FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                  "AND table_name LIKE 'size_%';")
        grantees = ("SELECT string_agg(p.proname || '=' || (SELECT string_agg(DISTINCT coalesce(r.rolname, 'PUBLIC'), "
                    "'+' ORDER BY coalesce(r.rolname, 'PUBLIC')) FROM aclexplode(coalesce(p.proacl, "
                    "acldefault('f', p.proowner))) a LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE "
                    "a.privilege_type = 'EXECUTE'), ',' ORDER BY p.proname) FROM pg_proc p "
                    f"WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ({names});")
        seen["fresh_equals_migrated"] = [t.psql(fresh, definitions) == t.psql(admin, definitions),
                                         t.psql(fresh, tables) == t.psql(admin, tables)]
        seen["grantees"] = scalar(grantees)
        seen["grantees_fresh"] = t.psql(fresh, grantees).strip()
        for srv, events in servers.values():
            srv.shutdown()
            srv.server_close()
            events.close()
    return seen


def hook_cases(root: Path) -> dict:
    """The commit-time warning, through the real helper: staged blobs, bands, before and after."""
    out = {}
    with tempfile.TemporaryDirectory(prefix="syrd541-hook.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "t")
        (repo / "a.py").write_text(lines(1300))
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")

        def run() -> list[str]:
            done = subprocess.run([sys.executable, str(root / "scripts/warn_file_size_limit.py"),
                                   "--repo-root", str(repo)], capture_output=True, text=True)
            assert done.returncode == 0, done.stderr
            return done.stderr.splitlines()

        (repo / "a.py").write_text(lines(1302))
        git(repo, "add", "a.py")
        (repo / "a.py").write_text(lines(5))  # unstaged: not what is being committed
        out["staged_over"] = run()
        git(repo, "checkout", "-q", "--", "a.py")
        git(repo, "reset", "-q")
        (repo / "a.py").write_text(lines(1200))
        git(repo, "add", "a.py")
        (repo / "a.py").write_text(lines(2000))
        out["staged_band"] = run()
        git(repo, "reset", "-q", "--hard")
        (repo / "b.py").write_text(lines(1099))
        (repo / "notes.md").write_text(lines(3000))
        git(repo, "add", "-A")
        out["quiet"] = run()
    return out


def mirror_cases(root: Path) -> dict:
    """schema.sql's mirror proof on real git history, through file_size_policy itself."""
    sys.path.insert(0, str(root / "scripts"))
    from ticket_board import file_size_policy as policy

    schema, migrations = "scripts/ticket_board/schema.sql", "scripts/ticket_board/migrations"
    out = {}
    with tempfile.TemporaryDirectory(prefix="syrd541-mirror.") as tmp:
        repo = Path(tmp)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "t")
        (repo / migrations).mkdir(parents=True)
        base_schema = lines(1300, "s")
        old_block = "".join(f"CREATE TABLE old_{i} (id int);\n" for i in range(5))
        (repo / schema).write_text(base_schema)
        (repo / migrations / "pgu001_old.sql").write_text(old_block)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        base = git(repo, "rev-parse", "HEAD")
        block = "".join(f"CREATE FUNCTION f_{i}() RETURNS int LANGUAGE sql AS 'SELECT {i}';\n" for i in range(10))

        def case(label: str, schema_text: str, new_migration: str | None, touch_old: bool = False) -> None:
            git(repo, "checkout", "-q", "-B", label, base)
            (repo / schema).write_text(schema_text)
            if new_migration is not None:
                (repo / migrations / "pgu999_new.sql").write_text(new_migration)
            if touch_old:
                (repo / migrations / "pgu001_old.sql").write_text(old_block + "-- touched\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", label)
            growth = next(f for f in policy.measure(repo, base, "HEAD").files if f.path == schema)
            out[label] = (growth.before, growth.after, growth.mirrored, growth.unmirrored,
                          policy.classify(growth, None), policy.classify(growth, growth.after))
            git(repo, "checkout", "-q", "main")

        case("copies", base_schema + "SELECT 1;\n" * 100, "BEGIN;\nSELECT 1;\nCOMMIT;\n")
        case("proved", base_schema + "\n-- SYRD-X: identical to the migration.\n" + block, "BEGIN;\n" + block + "COMMIT;\n")
        case("reordered", base_schema + "".join(reversed(block.splitlines(keepends=True))), "BEGIN;\n" + block + "COMMIT;\n")
        case("historical", base_schema + old_block, None)
        case("historical_touched", base_schema + old_block, None, touch_old=True)
        case("twice_once", base_schema + block + block, "BEGIN;\n" + block + "COMMIT;\n")
        case("apart", block + base_schema + block, "BEGIN;\n" + block + "COMMIT;\n")
        noted = block.splitlines(keepends=True)
        case("annotated_differently", base_schema + "".join(noted[:5]) + "-- schema.sql's note\n" + "".join(noted[5:]),
             "BEGIN;\n" + "".join(noted[:5]) + "-- the migration's own note\n" + "".join(noted[5:]) + "COMMIT;\n")
        case("replaced", lines(1290, "s") + lines(10, "r"), None)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd541b-")))
        return 0
    before_commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "7c573c7"], check=True,
                                   capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="syrd541.") as tmp:
        before_root = tree_at(before_commit, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(tuple(before["no_code_before"]) == (0, "director_review"),
          f"before: a commit-exempt no-code review is approved: {before['no_code_before']}")
    check(before["disabled_approve"] == 0 and before["disabled_state"] != "audit" and before["cli_has_enable"] != 0,
          f"before: Audit approves a candidate that crosses 1,250 and grows an oversized file, and there is no "
          f"review to enable: {before}")

    a = scenario(ROOT, "syrd541a-")
    check(a["no_code_before"] == (0, "director_review") and a["no_code_after"] == (0, "director_review"),
          f"a commit-exempt no-code review is approved before and after enabling: {a['no_code_before']} {a['no_code_after']}")
    check(a["missing_commit"][0] != 0 and "audit_sign_off needs a candidate commit to measure" in a["missing_commit"][2],
          f"without the exemption, a missing commit is refused: {a['missing_commit'][2][:200]}")
    check(a["coded_exempt"][0] != 0 and ["scripts/edge.py", "open", "crossing"] in a["coded_exempt"][1],
          f"an exempt ticket that carries a commit is measured like any other: {a['coded_exempt']}")
    check(a["schema_only_refused"] == (1, [["scripts/ticket_board/schema.sql", "open", "growth", 13000, 13040, None, 1]])
          and a["schema_only_except"] == 0 and a["schema_only_rescan"][0] == 0 and a["schema_only_rescan"][1] != "audit",
          f"schema-only growth is refused, excepted at its measured size, and a fresh rescan passes: "
          f"{a['schema_only_refused']} {a['schema_only_rescan']}")
    check(a["schema_within"] == (0, []) and a["schema_beyond"][0] != 0
          and a["schema_beyond"][1] == [["scripts/ticket_board/schema.sql", "open", "growth", 13000, 13041, 13040, 1]],
          f"the ceiling bounds unproved schema growth as it bounds any file: within it passes, one line past it is "
          f"refused: {a['schema_within']} {a['schema_beyond']}")
    check(a["disabled_approve"] == 0 and a["disabled_state"] != "audit",
          f"until the Director enables it, nothing is gated: defaults are unchanged: {a['disabled_state']}")
    code, out, _ = a["enable_preview"]
    check(code == 0 and json.loads(out)["applied"] is False and a["enabled_before_apply"] == "f"
          and json.loads(out)["inventory"] == [{"path": "scripts/big.py", "lines": 1300},
                                               {"path": "scripts/ticket_board/schema.sql", "lines": 13000},
                                               {"path": "tests/big_test.py", "lines": 1300}],
          f"the preview names the baseline inventory -- retained debt, recorded and not approved; docs and vendored "
          f"code are out of scope -- and writes nothing: {out}")
    check(a["enable_by_app"][0] != 0 and "app cannot call enable_size_review" in a["enable_by_app"][2]
          and a["enable"][0] == 0 and a["inventory"] == json.loads(a["enable"][1])["inventory"],
          f"only the Director enables it, and the inventory is recorded: {a['enable_by_app'][2][:200]}")
    code, _, err = a["refused"]
    check(code != 0 and "size review: unresolved size findings" in err
          and "scripts/big.py (growth: 1300 -> 1305 lines)" in err and "scripts/edge.py (crossing: 1250 -> 1251 lines)" in err
          and a["refused_state"] == "audit",
          f"Audit's approval is refused, naming each finding; the ticket stays in review: {err[:400]}")
    check(a["findings_after_refusal"] == [["scripts/big.py", "open", "growth", 1300, 1305, None, 1],
                                          ["scripts/edge.py", "open", "crossing", 1250, 1251, None, 1]],
          f"the findings survive the refused transaction: {a['findings_after_refusal']}")
    packet = a["packet"]
    check(packet and packet["enabled"] and [(f["path"], f["before"], f["after"], f["finding"]) for f in packet["files"]]
          == [("scripts/big.py", 1300, 1305, "growth"), ("scripts/edge.py", 1250, 1251, "crossing")],
          f"the ticket carries the size evidence for the review packet: {packet}")
    check(a["findings_idempotent"] == a["findings_after_refusal"],
          f"measuring the same candidate again changes nothing: {a['findings_idempotent']}")
    check(a["except_app"][0] != 0 and "app cannot call approve_size_exception" in a["except_app"][2]
          and a["except_ops"][0] != 0 and "ops cannot call approve_size_exception" in a["except_ops"][2]
          and a["except_unfound"][0] != 0 and "no open size finding for scripts/small.py" in a["except_unfound"][2],
          "only the Director excepts, and only an open finding")
    preview = json.loads(a["except_preview"][1])
    check(preview["applied"] is False and preview["ceiling"] == 1251 and preview["before"] == 1250
          and preview["scope"] == "PGU-2" and a["exceptions_after_preview"] == "0",
          f"an exception is bounded to the measured size and previews without writing: {preview}")
    check(a["one_excepted_still_refused"] != 0 and a["except_standing"][0] == 0 and a["approved"][0] == 0
          and a["approved_state"] != "audit"
          and [row[1] for row in a["findings_excepted"]] == ["excepted", "excepted"],
          f"with every finding excepted, the approval goes through: {a['findings_excepted']}")
    check(a["exception_comment"].startswith("Size exception ") and "scripts/edge.py may be up to 1251 lines" in a["exception_comment"],
          f"the decision is attributed on the ticket: {a['exception_comment']}")
    check(a["measure_by_app"][0] != 0 and "app cannot call measure_size" in a["measure_by_app"][2]
          and a["measure"] == (0, [("tests/big_test.py", "open", 1301)], "director_review"),
          f"the Director rechecks an integration commit before pushing it, and nothing moves: {a['measure']}")
    code, _, err = a["close_refused"]
    check(code != 0 and "tests/big_test.py (growth: 1300 -> 1301 lines)" in err and a["close_refused_state"] != "done"
          and a["close_ok"] == 0 and a["close_ok_state"] == "done",
          f"the close measures the integration commit: extra growth is refused, the audited candidate closes: {err[:300]}")
    check(a["standing_within"][0] == 0 and a["standing_within"][1] == []
          and a["standing_beyond"][0] != 0 and a["standing_beyond"][1] == [["scripts/big.py", "open", "growth", 1300, 1306, 1305, 1]],
          f"a standing exception is the file's allowance; one line past it is a renewed review: {a['standing_beyond']}")
    check(a["scoped_does_not_travel"][0] != 0 and a["scoped_does_not_travel"][1][0][:3] == ["scripts/edge.py", "open", "crossing"],
          f"a ticket's exception is that ticket's: {a['scoped_does_not_travel']}")
    check(a["repeat_first"] == [["scripts/big.py", "open", "growth", 1300, 1320, 1305, 1]]
          and a["stale_except"][0] != 0 and "must be measured again" in a["stale_except"][2]
          and a["repeat_second"] == [["scripts/big.py", "open", "growth", 1300, 1330, 1305, 2]],
          f"one finding per file, updated in place as the candidate grows; an approval of a stale measurement is "
          f"refused: {a['repeat_second']} {a['stale_except'][2][:160]}")
    check(a["reopened"][0] != 0 and a["reopened"][1] == [["scripts/big.py", "open", "growth", 1300, 1331, 1330, 4]]
          and a["reduced"][0] == 0 and a["reduced"][1] == [["scripts/big.py", "resolved", "growth", 1300, 1331, 1330, 5]],
          f"past its exception the finding reopens; a reduction resolves it: {a['reopened']} {a['reduced']}")
    check(a["notices_pgu6"] == "0",
          "Audit measured its own review, so its own pane is not woken about it")
    check(a["case_PGU-7"][0] == 0 and a["case_PGU-7"][1] == [],
          f"unchanged and shrinking debt is never a finding: {a['case_PGU-7']}")
    check(a["case_PGU-8"][0] == 0 and a["case_PGU-8"][2] == [("scripts/core.py", 1300, 1301, "review", None)],
          f"a rename keeps the file's size history and its standing allowance (big.py's 1,305): {a['case_PGU-8']}")
    check(a["case_PGU-9"][0] != 0 and a["case_PGU-9"][1] == [["scripts/fresh.js", "open", "crossing", 0, 1260, None, 1],
                                                              ["scripts/ticket_board/schema.sql", "open", "growth", 13000, 14000, None, 1]]
          and ("scripts/ticket_board/schema.sql", 13000, 14000, "review", "growth") in a["case_PGU-9"][2]
          and not any(p.startswith(("docs/", "third_party/")) for p, *_ in a["case_PGU-9"][2]),
          f"a new file over the limit is a crossing whatever its language; schema.sql lines no migration carries "
          f"are schema-only growth; docs and vendored code are out of scope: {a['case_PGU-9']}")
    check(a["mirrored"] == (0, []),
          f"schema.sql growth that the candidate's own migration carries is not a finding: {a['mirrored']}")
    check(a["inspector_approve"][0] != 0 and "size review: unresolved size findings" in a["inspector_approve"][2]
          and a["user_approve"][0] != 0 and "size review: unresolved size findings" in a["user_approve"][2]
          and a["approval_states"] == ("inspection", "user_review"),
          f"every declared approval is gated: {a['inspector_approve'][2][:160]} / {a['user_approve'][2][:160]}")
    check(a["override_move"] == (0, "director_review") and a["merge"][0] == 0 and a["merge"][1] in ("done", "cancelled")
          and a["merge_target_still_gated"] == (1, "audit"),
          f"override and a merge's own close are excluded, and the merge target keeps its gate: "
          f"{a['override_move']} {a['merge']} {a['merge_target_still_gated']}")
    check(a["stale"].startswith("size review: the measurement of") and "not this action" in a["stale"]
          and a["fresh"][0] == 0 and a["fresh"][1] != "audit",
          f"a stale scan cannot satisfy an approval; the board's path measures again: {a['stale']} {a['fresh']}")
    check(a["case_PGU-10"][0] == 0 and a["case_PGU-10"][1] == [], "deleting a file is never a finding")
    check(a["case_PGU-11"][0] == 0 and a["case_PGU-11"][2] == [("scripts/near.py", 1099, 1100, "warn", None)],
          f"1,100 lines is reported, not gated: {a['case_PGU-11']}")
    check(a["scan_failed"][0] != 0 and a["scan_failed"][1][0][:3] == ["*", "open", "scan_failed"]
          and a["scan_failed_except"][0] != 0 and "cannot be excepted" in a["scan_failed_except"][2]
          and a["rescanned"][0] == 0 and a["rescanned"][1][0][:2] == ["*", "resolved"],
          f"a failed scan fails closed, cannot be excepted, and a successful rescan clears it: {a['scan_failed']} {a['rescanned']}")
    check(a["force_move"] == (0, "director_review"), f"narrated recovery is untouched: {a['force_move']}")
    (code, _, err), rows = a["unknown_commit"]
    check(code != 0 and "* (scan_failed)" in err and rows[0][:3] == ["*", "open", "scan_failed"],
          f"a commit the cache does not have is a failed scan on the ticket, and refused: {err[:200]}")
    check(a["unmeasured"][0] != 0 and "has not been measured" in a["unmeasured"][2],
          f"one that cannot even be resolved is refused as unmeasured: {a['unmeasured'][2][:200]}")
    check(a["cancel_with_finding"] == (0, "in_progress", "open", 0, "cancelled"),
          f"a kick-back and a cancel are not integration: an open finding stops neither: {a['cancel_with_finding']}")
    check(a["sql_refusals"] == ["role app cannot call merge", "role app cannot call merge"],
          f"the database refuses App itself, as it would any role without the integrator's capability: {a['sql_refusals']}")
    check(a["submitted"] == ("audit", [["scripts/edge.py", "open", "crossing", 1250, 1260, None, 1]]),
          f"a submission is measured, and the finding is on the ticket before review: {a['submitted']}")
    check(all(a["fresh_equals_migrated"]), f"a fresh schema.sql board installs the same: {a['fresh_equals_migrated']}")
    grants = dict(item.split("=", 1) for item in a["grantees"].split(","))
    check({k: grants[k] for k in NEW_FUNCTIONS} == {
        "size_review_enabled": "postgres+ticket_board_service", "size_ceilings": "postgres+ticket_board_service",
        "record_size_scan": "postgres+ticket_board_service", "approve_size_exception": "postgres+ticket_board_service",
        "enable_size_review": "postgres+ticket_board_service", "ticket_size_review": "postgres+ticket_board_service",
        "enforce_size_review": "postgres"} and a["grantees_fresh"] == a["grantees"],
          f"each function is executable only by its caller, on both boards: {grants}")

    # The review packet in the ticket view, from the JSON the board serves; not on the card.
    sys.path.insert(0, str(ROOT / "tests"))
    from active_work_delivery_frontend_test import function_source

    program = function_source("sizeReviewText") + """
process.stdout.write(JSON.stringify(%s.map((review) => sizeReviewText(review))));
""" % json.dumps([a["packet"], a["packet_approved"], None])
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    unresolved_line, approved_line, nothing = json.loads(proc.stdout)
    check(unresolved_line == f"Size review: unresolved for {a['packet']['candidate'][:12]} - scripts/big.py 1300 -> 1305 "
                            "lines (growth); scripts/edge.py 1250 -> 1251 lines (crossing)"
          and approved_line == "Size review: no unresolved growth (2 excepted) - at or over 1,100 lines: "
                               "scripts/big.py 1300 -> 1305, scripts/edge.py 1250 -> 1251"
          and nothing == "",
          f"the ticket view states the size evidence: {unresolved_line!r} {approved_line!r}")
    detail = function_source("renderDetail")
    check("const sizeText = sizeReviewText(ticket.size_review);" in detail and "meta.appendChild(sizeLine);" in detail
          and "size_review" not in function_source("renderCard"),
          "the ticket view shows it; the card does not (SYRD-266)")

    mirror = mirror_cases(ROOT)
    check(mirror["copies"] == (1300, 1400, 0, 100, "growth", None),
          f"one migration line does not excuse a hundred copies; a ceiling at the measured size covers them: {mirror['copies']}")
    check(mirror["proved"] == (1300, 1312, 12, 0, None, None),
          f"a block the candidate's migration carries is excused whole, its annotation with it: {mirror['proved']}")
    check(mirror["reordered"][3] == 10 and mirror["reordered"][4] == "growth",
          f"the same lines reordered prove nothing: {mirror['reordered']}")
    check(mirror["historical"][3] == 5 and mirror["historical"][4] == "growth",
          f"an unchanged historical migration proves nothing: {mirror['historical']}")
    check(mirror["historical_touched"][3] == 5 and mirror["historical_touched"][4] == "growth",
          f"touching a historical migration does not make its old lines proof: {mirror['historical_touched']}")
    check(mirror["twice_once"][2:5] == (0, 20, "growth"),
          f"a run holding two copies of the one migrated block is not proved at all -- conservative: "
          f"{mirror['twice_once']}")
    check(mirror["apart"][2:5] == (10, 10, "growth"),
          f"two separate copies of a block the migration holds once: one is proved, the other is not: {mirror['apart']}")
    check(mirror["annotated_differently"][2:5] == (11, 0, None),
          f"the same SQL annotated differently is still the migration's copy: {mirror['annotated_differently']}")
    check(mirror["replaced"][:2] == (1300, 1300) and mirror["replaced"][4] is None,
          f"replacing lines without growing is never a finding: {mirror['replaced']}")

    hooks = hook_cases(ROOT)
    check(hooks["staged_over"] == ["warning: a.py is 1302 lines (soft limit 1250) - consider splitting.",
                                   "note: a.py was 1300 at HEAD (+2); growth past the limit needs a split or a "
                                   "Director exception before integration."],
          f"the commit-time warning measures the staged blob, with its size at HEAD: {hooks['staged_over']}")
    check(hooks["staged_band"] == ["note: a.py is 1200 lines, nearing the 1250-line review limit; was 1300 at HEAD (-100)."],
          f"from 1,100 lines a note, measured on what is staged: {hooks['staged_band']}")
    check(hooks["quiet"] == [], f"under 1,100, and prose, are quiet: {hooks['quiet']}")
    print(f"size_review_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
