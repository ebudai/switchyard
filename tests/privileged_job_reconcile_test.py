#!/usr/bin/env python3
"""SYRD-113 slice B: the board service records root's privileged jobs on their tickets.

Slice A made each privileged run a job in root's rollout journal: its action,
values, originating ticket, requesting role and release, with its output
captured and bounded. Nothing brought it to the board, so a result still had
to be copied from a pane by hand.

The board service now reads those records and records each job on its ticket,
once. The Director required that:
- the reader is the board service, never the listener, whose account every
  role shares;
- a record is believed only as root wrote it: no link followed, root's owner,
  not writable by others, root's or the tenant's group, this project, this
  board's ticket, its own bounds, and checksums that agree with root's index;
- role, listener and report-token paths cannot make the write;
- a restart or an outage changes nothing, because a job is keyed by its
  attempt id and a finished job never changes.

The board is built the production way (companion roles, schema.sql, the real
migrate runner, rbac.sql, a declared workflow). The journal is written by
slice A's real `Attempt` and real streaming runner, on real child processes.
The test cannot create root-owned files, so the records' owner is the test's
own uid, passed to the reader as the one it believes. The reader's default,
root, is also exercised: it refuses these records. It runs on main, from a git
archive in a child process, and on this tree.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "d8f36e1a6f97bbbca89f01ae54623b8e8115451a"  # main before SYRD-113 slice B
COMMIT = "b" * 40
CHECKS = 0
CHILD = r'''
import sys, time
mode = sys.argv[1]
print(f"{mode}: preparing", flush=True)
print("API_TOKEN=never-on-the-board", flush=True)
if mode == "hang":
    time.sleep(600)
print(f"{mode}: finished", file=sys.stderr, flush=True)
sys.exit({"ok": 0, "fail": 5}[mode])
'''


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "scripts"), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board import privileged_actions as pa
    from scripts.ticket_board import privileged_helper as ph
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    try:
        from scripts.ticket_board import privileged_jobs as pj
    except ImportError as exc:
        pj = None
        seen["module"] = f"{type(exc).__name__}: {exc}"
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dbname = "board"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        done = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                              env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert done.returncode == 0, done.stderr[-1500:]
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())

        def new_app():
            return fixture.TicketBoardApp(tmp / "frames", tmp / "assets", project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname,
                                                                        fixture.SERVICE_ROLE))

        app = new_app()
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
        for ticket in ("PGU-1", "PGU-2", "PGU-3"):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="in_progress",
                                         assignee={"PGU-1": "app", "PGU-2": "main", "PGU-3": "ops"}[ticket],
                                         commit_exempt=True)

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        # Root's journal, written by slice A's own machinery.
        journal = tmp / "journal"
        os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(journal)
        child = tmp / "child.py"
        child.write_text(CHILD)
        action = pa.action_for("deploy-release")

        def run(mode: str, ticket: str, *, timeout: float = 60.0) -> str:
            before = set(os.listdir(journal / "pgu")) if (journal / "pgu").exists() else set()

            def runner(command, *, timeout, sink=None, real=ph._run_command):
                return real(command, timeout=timeout, sink=sink, terminal={"stdout": io.BytesIO(), "stderr": io.BytesIO()},
                            grace=1.0)
            runner.streams_output = True
            ph.run_privileged_action(action, {"project": "pgu", "commit": COMMIT, "ticket": ticket}, project="pgu",
                                     attempt_factory=ph._attempt_factory, runner=runner,
                                     command_for=lambda name, values: [sys.executable, str(child), mode],
                                     timeout=timeout, print_func=lambda _line: None, requester_role="director")
            return sorted(set(os.listdir(journal / "pgu")) - before - {"index.jsonl"})[0]

        def rows(statement: str) -> list:
            return json.loads(sql(f"SELECT coalesce(json_agg(x), '[]')::text FROM ({statement}) x;"))

        def board_state() -> dict:
            return {
                "jobs": rows("SELECT job_id, ticket_id, state, exit_status, requester_role, action FROM "
                             "ticket_board.privileged_jobs ORDER BY job_id") if pj else None,
                "comments": rows("SELECT ticket_id, who, urgent, text FROM ticket_board.ticket_comments "
                                 "WHERE who = 'privileged-job' ORDER BY id"),
                "queue": rows("SELECT ticket_id, target_role, dedupe_key FROM ticket_board.ticket_notification_queue "
                              "WHERE dedupe_key LIKE 'privileged-job:%' ORDER BY id"),
            }

        def ticket_json(ticket: str) -> dict:
            import urllib.request
            with urllib.request.urlopen(f"{base}/api/tickets/{ticket}", timeout=10) as response:
                return json.loads(response.read())

        stand_in = {"owner_uid": os.getuid(), "gids": {os.getgid()}}
        ok_job = run("ok", "PGU-1")
        fail_job = run("fail", "PGU-2")
        hang_job = run("hang", "PGU-3", timeout=2.0)
        seen["jobs"] = {"ok": ok_job, "fail": fail_job, "hang": hang_job}
        if pj is None:
            seen["ticket_json_keys"] = sorted(ticket_json("PGU-1"))
            server.shutdown()
            server.server_close()
            return seen

        # A job root has not finished yet, as the helper leaves it while the operation runs.
        from scripts.ticket_board import rollout_journal as rj

        live = rj.Attempt("pgu", [sys.executable, str(child), "ok"], target_commit=COMMIT)
        live.describe_job({"action": "deploy-release", "action_id": action.action_id,
                           "arguments": {"commit": COMMIT, "project": "pgu"}, "ticket": "PGU-1",
                           "requester_role": "director", "release_commit": COMMIT})
        live.open()
        live.write("stdout", "deploying\n")
        seen["jobs"]["live"] = live.attempt
        seen["first"] = pj.reconcile(app, **stand_in)
        seen["after_first"] = board_state()
        seen["ticket_json"] = ticket_json("PGU-1").get("privileged_jobs")
        seen["second"] = pj.reconcile(app, **stand_in)
        seen["after_second"] = board_state()
        live.close(status="succeeded", exit_status=0, detail=action.action_id)
        seen["finished"] = pj.reconcile(app, **stand_in)
        seen["after_finished"] = board_state()
        # Restart, and an outage: a fresh service sees a delivered notice gone and reads every record again.
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE dedupe_key LIKE 'privileged-job:%';")
        seen["restart"] = pj.reconcile(new_app(), **stand_in)
        seen["after_restart"] = board_state()

        # The reconciler thread, as the service starts it, with the stand-in owner.
        stop = threading.Event()
        logged: list[str] = []
        late = run("ok", "PGU-1")
        thread = pj.start_reconciler(app, interval=0.5, stop=stop, log=logged.append, **stand_in)
        deadline = time.time() + 15
        while time.time() < deadline and not any(late in line for line in logged):
            time.sleep(0.2)
        stop.set()
        thread.join(timeout=5)
        seen["thread_logged"] = logged
        seen["after_thread"] = board_state()

        # Forgeries and damage: each refused, with nothing recorded.
        def forged(name: str, damage) -> list[str]:
            target = journal / "pgu" / name
            shutil.copytree(journal / "pgu" / ok_job, target)
            fresh = json.loads((target / "result.json").read_text())
            fresh["attempt"] = name
            os.chmod(target / "result.json", 0o644)
            (target / "result.json").write_text(json.dumps(fresh))
            damage(target)
            jobs, refused = pj.scan("pgu", "PGU", root=journal, **stand_in)
            shutil.rmtree(target, ignore_errors=True)
            if target.is_symlink():
                target.unlink()
            return [line for line in refused if name in line] + [job["job_id"] for job in jobs if job["job_id"] == name]

        def chmod(path: Path, mode: int) -> None:
            os.chmod(path, mode)

        def edit(path: Path, text: str) -> None:
            os.chmod(path, 0o644)
            path.write_text(text)
            os.chmod(path, 0o444)

        seen["forged"] = {
            "group-writable result": forged("8001-20261012T000001Z", lambda t: chmod(t / "result.json", 0o664)),
            "world-writable directory": forged("8002-20261012T000002Z", lambda t: chmod(t, 0o757)),
            "linked log": forged("8003-20261012T000003Z", lambda t: ((t / "stdout.log").unlink(),
                                                                    (t / "stdout.log").symlink_to(journal / "pgu" / ok_job / "stdout.log"))),
            "edited log": forged("8004-20261012T000004Z", lambda t: edit(t / "stdout.log", "all fine\n")),
            "unchained result": forged("8005-20261012T000005Z", lambda t: None),
            "another board's ticket": forged("8006-20261012T000006Z", lambda t: edit(
                t / "result.json", json.dumps({**json.loads((t / "result.json").read_text()),
                                               "job": {**json.loads((t / "result.json").read_text())["job"], "ticket": "OTTO-1"}}))),
            "another project": forged("8007-20261012T000007Z", lambda t: edit(
                t / "result.json", json.dumps({**json.loads((t / "result.json").read_text()), "project": "otto"}))),
            "over its bound": forged("8008-20261012T000008Z", lambda t: edit(t / "stdout.log", "x" * (pj.LOG_LIMIT_BYTES + 1))),
        }
        other_group = next((gid for gid in os.getgroups() if gid != os.getgid()), None)
        if other_group is not None:
            seen["forged"]["another group"] = forged("8010-20261012T000010Z",
                                                     lambda t: os.chown(t / "result.json", -1, other_group))
        linked = journal / "pgu" / "8009-20261012T000009Z"
        linked.symlink_to(journal / "pgu" / ok_job)
        seen["forged"]["linked directory"] = [line for line in pj.scan("pgu", "PGU", root=journal, **stand_in)[1]
                                              if linked.name in line]
        linked.unlink()
        # The reader's own default believes root alone; these records are the test's.
        try:
            seen["default_owner"] = pj.scan("pgu", "PGU", root=journal)
        except pj.Refused as refused:
            seen["default_owner"] = f"Refused: {refused}"

        # The database's own rule, apart from the reader's skipping: a finished job is recorded once.
        again = json.dumps({"job_id": ok_job, "ticket": "PGU-1", "action": "deploy-release", "state": "failed",
                            "exit_status": 9, "record_dir": "/elsewhere"})
        rerun = json.dumps({"job_id": ok_job, "ticket": "PGU-1", "action": "deploy-release", "state": "running",
                            "record_dir": "/elsewhere"})
        service_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        seen["direct_again"] = [fixture.psql(service_url, "SELECT set_config('ticket_board.privileged_job_reconciler', 'on', "
                                                          f"false); SELECT ticket_board.record_privileged_job('{payload}');").strip().splitlines()[-1]
                                for payload in (again, rerun)]
        seen["after_direct_again"] = board_state()

        # The service as it ships: the board process, started as the unit starts it, runs the reconciler
        # with its own default -- root's records alone -- so it refuses this journal, and says so.
        board_process = subprocess.Popen(
            [sys.executable, str(root / "scripts" / "ticket-board.py"), "--database", service_url,
             "--frames", str(tmp / "frames-service"), "--assets", str(tmp / "assets-service"), "--port", "0",
             "--unix-socket", str(tmp / "service.sock")],
            env={**os.environ, "TICKET_BOARD_PROJECT": "pgu", "TICKET_BOARD_TICKET_PREFIX": "PGU",
                 "TICKET_BOARD_PRIVILEGED_JOB_INTERVAL": "0.5"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        said = []
        deadline = time.time() + 30
        import selectors

        watch = selectors.DefaultSelector()
        watch.register(board_process.stderr, selectors.EVENT_READ)
        try:
            while time.time() < deadline and not any("privileged job refused" in line for line in said):
                if not watch.select(timeout=max(deadline - time.time(), 0)):
                    break
                line = board_process.stderr.readline()
                if not line:
                    break
                said.append(line.strip())
        finally:
            watch.close()
            board_process.terminate()
            board_process.wait(timeout=10)
        seen["service_said"] = [line for line in said if "privileged job" in line]

        # Who may make the write.
        service = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        listener = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        forged_job = json.dumps({"job_id": "7777-20261012T000000Z", "ticket": "PGU-1", "action": "deploy-release",
                                 "state": "succeeded", "exit_status": 0, "record_dir": "/forged"})

        def attempt(conn: str, statement: str) -> str:
            try:
                fixture.psql(conn, statement)
            except AssertionError as refused:
                return " ".join(str(refused).split())[:200]
            return "ok"

        seen["writes"] = {
            "service without the reconciler": attempt(service, f"SET ticket_board.caller_role = 'director'; "
                                                               f"SELECT ticket_board.record_privileged_job('{forged_job}');"),
            "listener": attempt(listener, f"SELECT set_config('ticket_board.privileged_job_reconciler', 'on', false); "
                                          f"SELECT ticket_board.record_privileged_job('{forged_job}');"),
            "listener table": attempt(listener, "SELECT count(*) FROM ticket_board.privileged_jobs;"),
        }
        fixture.TEST_WRITE_TOKEN = server.write_token
        for role in ("director", "app"):
            try:
                fixture.post_json(base, "/api/tickets/PGU-1/actions/record_privileged_job", json.loads(forged_job), caller=role)
                seen["writes"][f"api as {role}"] = "ok"
            except AssertionError as refused:
                seen["writes"][f"api as {role}"] = " ".join(str(refused).split())[:200]
        seen["after_writes"] = board_state()

        # Parity: a fresh install from schema.sql and rbac.sql matches the migrated board.
        shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                 "coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text FROM pg_proc p "
                 "WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN "
                 "('record_privileged_job', 'ticket_privileged_jobs', 'finished_privileged_jobs');")
        seen["migrated_shape"] = json.loads(sql(shape))
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        seen["fresh_shape"] = json.loads(fixture.psql(fresh, shape).strip())
        seen["table_acl"] = [sql("SELECT coalesce(relacl::text, '') FROM pg_class WHERE oid = 'ticket_board.privileged_jobs'::regclass;"),
                             fixture.psql(fresh, "SELECT coalesce(relacl::text, '') FROM pg_class "
                                                 "WHERE oid = 'ticket_board.privileged_jobs'::regclass;").strip()]
        server.shutdown()
        server.server_close()
    return seen


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd113bb-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd113b.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    after = scenario(ROOT, "syrd113ba-")
    a, jobs = after, after["jobs"]
    ok, fail, hang, live = jobs["ok"], jobs["fail"], jobs["hang"], jobs["live"]

    check(before.get("module", "").startswith("ImportError") and "privileged_jobs" not in before["ticket_json_keys"],
          f"main: nothing reads root's job records, and a ticket carries none: {before.get('module')}")

    def row(job, ticket, state, code):
        return {"job_id": job, "ticket_id": ticket, "state": state, "exit_status": code, "requester_role": "director",
                "action": "deploy-release"}

    check(sorted(a["first"]["recorded"]) == sorted([f"{ok}: completed", f"{fail}: completed", f"{hang}: completed",
                                                    f"{live}: running"]) and a["first"]["refused"] == []
          and a["after_first"]["jobs"] == [row(ok, "PGU-1", "succeeded", 0), row(fail, "PGU-2", "failed", 5),
                                           row(hang, "PGU-3", "timed_out", None), row(live, "PGU-1", "running", None)],
          f"each job root recorded for a ticket is recorded on it, with its state, exit and requester: {a['after_first']['jobs']}")
    comments = a["after_first"]["comments"]
    check([(c["ticket_id"], c["who"], c["urgent"]) for c in comments]
          == [("PGU-1", "privileged-job", False), ("PGU-2", "privileged-job", True), ("PGU-3", "privileged-job", True)],
          f"each finished job writes one machine-attributed event, urgent when it failed or timed out; the running "
          f"one writes none: {[(c['ticket_id'], c['urgent']) for c in comments]}")
    listed = {job["job_id"]: job for job in a["ticket_json"] or []}
    first = comments[0]["text"]
    check(ok in listed and f"Privileged job {ok}: deploy-release succeeded." in first and "Asked by director, with commit=" in first
          and listed[ok]["record_dir"] in first and listed[ok]["artifacts"]["stdout.log"] in first
          and listed[ok]["artifacts"]["stderr.log"] in first and listed[ok]["result_sha256"] in first
          and "ok: preparing" in first and "API_TOKEN=<redacted>" in first
          and "failed with exit status 5" in comments[1]["text"] and "timed out and was stopped" in comments[2]["text"],
          f"the event says what ran, for whom, how it ended, where root's record is and its checksums, and the "
          f"redacted last lines: {first}")
    check(set(listed) == {ok, live} and listed[live]["state"] == "running" and listed[ok]["final_lines"][-1].endswith("ok: finished")
          and "never-on-the-board" not in json.dumps(a) ,
          f"the ticket's JSON lists its jobs, running ones included, and no secret reaches the board: {sorted(listed)}")
    queue = [(q["ticket_id"], q["target_role"], q["dedupe_key"]) for q in a["after_first"]["queue"]]
    check(sorted(queue) == sorted([("PGU-1", "app", f"privileged-job:{ok}:app"),
                                   ("PGU-2", "main", f"privileged-job:{fail}:main"),
                                   ("PGU-2", "director", f"privileged-job:{fail}:director"),
                                   ("PGU-3", "ops", f"privileged-job:{hang}:ops"),
                                   ("PGU-3", "director", f"privileged-job:{hang}:director")]),
          f"a success wakes the ticket's owner; a failure or a timeout wakes the owner and the Director; once each: {queue}")
    check(a["second"] == {"recorded": [f"{live}: unchanged"], "refused": []} and a["after_second"] == a["after_first"],
          f"the next pass re-reads only the unfinished job, and changes nothing: {a['second']}")
    check(a["finished"] == {"recorded": [f"{live}: completed"], "refused": []}
          and a["after_finished"]["jobs"][-1] == row(live, "PGU-1", "succeeded", 0)
          and len(a["after_finished"]["comments"]) == 4 and a["after_finished"]["queue"][-1]["target_role"] == "app",
          f"when the running job finishes it is completed once, with its one event and one wake: {a['finished']}")
    check(a["restart"] == {"recorded": [], "refused": []} and a["after_restart"]["jobs"] == a["after_finished"]["jobs"]
          and a["after_restart"]["comments"] == a["after_finished"]["comments"] and a["after_restart"]["queue"] == [],
          "a restarted service, after every notice was delivered, reads root's records again and repeats nothing")
    check(any(line.endswith(": completed") for line in a["thread_logged"]) and len(a["after_thread"]["jobs"]) == 5
          and len(a["after_thread"]["queue"]) == 1,
          f"the service's own reconciler thread records a job root finished while it ran: {a['thread_logged']}")
    reasons = {
        "group-writable result": "its result is writable by its group or by anybody (mode 664)",
        "world-writable directory": "its directory is writable by its group or by anybody (mode 757)",
        "linked log": "its stdout.log cannot be opened without following a link",
        "edited log": "its stdout.log does not hash to what its result recorded",
        "unchained result": "its result does not hash to what root's index recorded at finish",
        "another board's ticket": "its job names 'OTTO-1', not a ticket of this board",
        "another project": "its result names 'otto'",
        "over its bound": "its stdout.log is 4259841 bytes, over its 4259840-byte bound",
        "linked directory": "its directory cannot be opened without following a link",
        "another group": "its result belongs to group",
    }
    for case, reason in reasons.items():
        found = a["forged"][case]
        check(len(found) == 1 and reason in found[0], f"{case}: refused, and recorded nowhere: {found}")
    check(a["direct_again"] == ["unchanged", "unchanged"] and a["after_direct_again"] == a["after_thread"],
          f"the database records a finished job once: neither a different ending nor a return to running changes "
          f"it or says anything: {a['direct_again']}")
    check(len(a["service_said"]) == 1 and "the journal could not be read" in a["service_said"][0]
          and "may be replaced by somebody other than root" in a["service_said"][0],
          f"the board process as it ships runs the reconciler, believing root's records alone: {a['service_said']}")
    check(str(a["default_owner"]).startswith("Refused: ") and "may be replaced by somebody other than root" in a["default_owner"],
          f"the reader's own default believes root alone: {a['default_owner']}")
    w = a["writes"]
    check(w["service without the reconciler"].startswith("ERROR: only the board service's privileged-job reconciler")
          and w["listener"] == "ERROR: permission denied for function record_privileged_job"
          and "403" in w["api as director"] and "403" in w["api as app"]
          and a["after_writes"]["jobs"] == a["after_thread"]["jobs"] and a["after_writes"]["comments"] == a["after_thread"]["comments"],
          f"no role request, no listener and no service session but the reconciler's can record a job: {w}")
    check(a["migrated_shape"] == a["fresh_shape"] and len(a["fresh_shape"]) == 3
          and all(shape[2] == "{postgres=X/postgres,ticket_board_service=X/postgres}" for shape in a["fresh_shape"])
          and a["table_acl"][0] == a["table_acl"][1] and "ticket_board_listener=r/" in a["table_acl"][0]
          and "=arwd" not in a["table_acl"][0].replace("postgres=arwdDxtm", ""),
          f"schema.sql and rbac.sql install what the migration does; only the service runs the functions, and the "
          f"table is readable and written by none: {a['fresh_shape']}")
    print(f"privileged_job_reconcile_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
