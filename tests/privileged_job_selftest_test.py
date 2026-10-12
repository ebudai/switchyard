#!/usr/bin/env python3
"""SYRD-113 slice C: `job-selftest`, the disposable acceptance of the privileged-job path.

The ticket's acceptance asks for a disposable success and failure run through
the real privileged boundary, with both results reaching their ticket and no
terminal output copied by hand. No catalogued action could be made to fail
safely on demand, so the Director approved one inert action:
- **Its values:** it requires project, originating ticket and
  `outcome=succeed|fail`, and accepts no other or free-text value.
- **What it runs:** a fixed program that prints fixed synthetic lines and a
  fixed fake credential, reads no credential and no inherited environment,
  and changes nothing.
- **Its boundary:** the existing control-pane identity check and polkit
  policy, unchanged.

This suite runs the helper's installed entry point, `main()`, against a real
board: production-built, with a declared workflow and process authority, and
the control role registered to a live process. These are the real ones:
- the control-caller check;
- the ticket check;
- the journal;
- the streaming runner;
- the action's own operation, its fixed program run as the helper runs it.

Only the host's own facts are stood in: the installed boundary, which this
host's root installs; the pane identity, since the test is not in a pane; and
the board URL, which the registry would give. The board's reconciler then
records the results on the ticket. It runs on main, from a git archive in a
child process, and on this tree.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
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
BEFORE = "2c23f01012e472e6ae90f80c52370c9203572f3f"  # main before SYRD-113 slice C
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples", "config", "switchyard"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def snapshot(*roots: Path) -> dict:
    """Every file under the roots, with its bytes' hash: what a run must leave as it found."""
    found = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import privileged_helper_boundary_test as boundary
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board import peer_identity
    from scripts.ticket_board import privileged_helper as ph
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, "board")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "board"])
        fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        done = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                              env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert done.returncode == 0, done.stderr[-1500:]
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = fixture.TicketBoardApp(tmp / "frames", tmp / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        os.environ["TICKET_BOARD_PROCESS_AUTHORITY"] = "1"  # the unit's setting: authority is the registered process
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
        fixture.seed_postgres_ticket(admin, "PGU-1", title="PGU-1", state="in_progress", assignee="app", commit_exempt=True)

        # The Director's pane, and a sibling's: two live processes; the board registers the first as the controller.
        director = subprocess.Popen(["sleep", "600"])
        sibling = subprocess.Popen(["sleep", "600"])
        identities = {name: ph.CallerIdentity(proc.pid, peer_identity.read_process(proc.pid).start_time, os.getuid())
                      for name, proc in (("director", director), ("sibling", sibling))}
        fixture.psql(admin, "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                            "session_dir, process_pid, process_start_time, process_uid) VALUES ('director', 'claude', "
                            f"'pgu-director:0.0', '/w', '/s', {director.pid}, {identities['director'].start_time}, {os.getuid()});")

        journal = tmp / "journal"
        os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(journal)
        home = tmp / "home"
        home.mkdir()
        (home / "keep").write_text("untouched\n")

        def helper(argv: list[str], caller: str) -> dict:
            out, err = io.StringIO(), io.StringIO()
            before = sorted(os.listdir(journal / "pgu")) if (journal / "pgu").exists() else []
            files = snapshot(home)
            with boundary.swapped(ph, privileged_install=boundary._StubInstall([]),
                                  caller_identity=lambda *a, **k: identities[caller],
                                  board_url_for=lambda project, **k: base), \
                    boundary.swapped(po, LAUNCHER=launcher), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), contextlib.chdir(home):
                code = ph.main(argv)
            after = sorted(os.listdir(journal / "pgu")) if (journal / "pgu").exists() else []
            new = [name for name in after if name not in before and name != "index.jsonl"]
            record = {}
            if new:
                directory = journal / "pgu" / new[0]
                record = {"result": json.loads((directory / "result.json").read_text()),
                          "logs": {s: (directory / f"{s}.log").read_text() for s in ("stdout", "stderr")}}
            return {"code": code, "out": out.getvalue(), "err": err.getvalue()[-600:], "new": new, "record": record,
                    "unchanged": snapshot(home) == files}

        # Before anything privileged ran on this host there is no journal, and so no job.
        try:
            from scripts.ticket_board import privileged_jobs as pj
            seen["no_journal"] = pj.reconcile(app, owner_uid=os.getuid(), gids={os.getgid()})
        except Exception as exc:  # noqa: BLE001 - main's reader
            seen["no_journal"] = f"{type(exc).__name__}: {exc}"
        # The pinned launcher is root's installed release, /opt/switchyard/current/switchyard; here it is this
        # tree's own wrapper, the file that release carries.
        from scripts.ticket_board import privileged_operations as po
        launcher = str(root / "switchyard")
        good = ["job-selftest", "project=pgu", "ticket=PGU-1"]
        seen["succeed"] = helper([*good, "outcome=succeed"], "director")
        seen["fail"] = helper([*good, "outcome=fail"], "director")
        seen["refused"] = {
            "a sibling's pane": helper([*good, "outcome=succeed"], "sibling"),
            "no ticket": helper(["job-selftest", "project=pgu", "outcome=succeed"], "director"),
            "another board's ticket": helper(["job-selftest", "project=pgu", "ticket=OTTO-1", "outcome=succeed"], "director"),
            "a ticket the board does not hold": helper(["job-selftest", "project=pgu", "ticket=PGU-99", "outcome=succeed"], "director"),
            "an outcome not offered": helper([*good, "outcome=maybe"], "director"),
            "free text": helper([*good, "outcome=succeed; id"], "director"),
            "an extra value": helper([*good, "outcome=succeed", "command=/bin/sh"], "director"),
            "an environment value": helper([*good, "outcome=succeed", "env=API_TOKEN=x"], "director"),
            "a flag": helper([*good, "outcome=succeed", "--verbose"], "director"),
            "a repeated value": helper([*good, "outcome=succeed", "outcome=fail"], "director"),
        }
        for proc in (director, sibling):
            proc.kill()
            proc.wait()

        # The board records both on the ticket, as its service does, believing the test's own uid as root.
        try:
            from scripts.ticket_board import privileged_jobs as pj
            seen["reconciled"] = pj.reconcile(app, owner_uid=os.getuid(), gids={os.getgid()})
            seen["events"] = json.loads(fixture.psql(admin, "SELECT coalesce(json_agg(text ORDER BY id), '[]')::text FROM "
                                                            "ticket_board.ticket_comments WHERE who = 'privileged-job';").strip())
        except Exception as exc:  # noqa: BLE001 - main has no such action, and its reader no journal to read
            seen["reconciled"] = f"{type(exc).__name__}: {exc}"
        server.shutdown()
        server.server_close()
    module = root / "scripts" / "ticket_board" / "privileged_selftest.py"
    seen["program"] = module.read_text(encoding="utf-8") if module.exists() else None
    seen["wrappers"] = {name: (root / name).read_text(encoding="utf-8") for name in ("switchyard", "scripts/switchyard")}
    seen["direct"] = {" ".join(argv): [subprocess.run([str(root / "switchyard"), *argv], capture_output=True, text=True,
                                                      cwd=str(root)).returncode]
                      for argv in (["job-selftest", "pgu", "--outcome", "succeed"], ["job-selftest", "pgu", "--outcome", "fail"],
                                   ["job-selftest", "pgu", "--outcome", "x"], ["job-selftest", "pgu"],
                                   ["job-selftest", "pgu", "--outcome", "fail", "extra"], ["job-selftest", "/etc", "--outcome", "fail"])}
    return seen


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print("RESULT " + json.dumps(scenario(Path(sys.argv[2]), "syrd113sb-")))
        return 0

    def child(tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree)], text=True,
                              capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd113s.") as tmp:
        before = child(tree_at(BEFORE, Path(tmp) / "before"))
    after = child(ROOT)
    fake = ("selftest-fake-credential-0000", "selftest-fake-bearer-0000")

    check(all(run["code"] == 1 and run["new"] == [] and "'job-selftest' is not a catalogued action" in run["err"]
              for run in (before["succeed"], before["fail"])),
          "main: no catalogued action can be made to succeed or fail on demand without touching the host")
    job = {"action": "job-selftest", "action_id": "org.switchyard.privileged.job-selftest",
           "arguments": {"outcome": "succeed", "project": "pgu"}, "release_commit": "", "requester_role": "director",
           "ticket": "PGU-1"}
    for outcome, code, status in (("succeed", 0, "succeeded"), ("fail", 1, "failed")):
        run = after[outcome]
        record = run["record"]
        lines = [line.split(" ", 1)[1] for line in record["logs"]["stdout"].splitlines() + record["logs"]["stderr"].splitlines()]
        check(run["code"] == code and len(run["new"]) == 1 and record["result"]["status"] == status
              and record["result"]["job"] == {**job, "arguments": {"outcome": outcome, "project": "pgu"}},
              f"{outcome}: the control pane's run exits {code} and is recorded {status}, as a job for its ticket: "
              f"{record['result']['status']}")
        check(lines == ["switchyard job-selftest: a synthetic privileged job; nothing on this host is read or changed",
                        "switchyard job-selftest: API_TOKEN=<redacted>",
                        f"switchyard job-selftest: outcome {outcome}",
                        "switchyard job-selftest: Authorization: <redacted>"]
              and not any(secret in json.dumps(record) for secret in fake)
              and fake[0] in run["out"] and fake[1] in run["err"],
              f"{outcome}: only its fixed lines are recorded, the fake credential redacted before it is written, while "
              f"the pane shows what ran: {lines}")
        check(run["unchanged"], f"{outcome}: the working directory and home it ran in are left exactly as they were")
    reasons = {
        "a sibling's pane": "only director's registered process may run a privileged action",
        "no ticket": "job-selftest requires ticket",
        "another board's ticket": "pgu's board has no ticket OTTO-1 to report this job to",
        "a ticket the board does not hold": "pgu's board has no ticket PGU-99 to report this job to",
        "an outcome not offered": "outcome must be succeed or fail: 'maybe'",
        "free text": "outcome must be succeed or fail: 'succeed; id'",
        "an extra value": "refusing unexpected ['command']",
        "an environment value": "refusing unexpected ['env']",
        "a flag": "job-selftest takes no options; refusing '--verbose'",
        "a repeated value": "outcome was given twice",
    }
    for case, reason in reasons.items():
        run = after["refused"][case]
        check(run["code"] == 1 and run["new"] == [] and reason in run["err"] and run["unchanged"],
              f"{case}: refused before anything is run or recorded: {run['err'][-200:]}")
    check(after["no_journal"] == {"recorded": [], "refused": []} and before["no_journal"].startswith("FileNotFoundError"),
          f"a host that has run nothing privileged has no journal, and the board reads that as no jobs rather than an "
          f"error, where main's reader raised: {after['no_journal']} | {before['no_journal']}")
    check(after["reconciled"] == {"recorded": [f"{after['succeed']['new'][0]}: completed",
                                               f"{after['fail']['new'][0]}: completed"], "refused": []}
          and "job-selftest succeeded." in after["events"][0] and "job-selftest failed with exit status 1." in after["events"][1]
          and not any(secret in json.dumps(after["events"]) for secret in fake),
          f"the board records both on their ticket with nothing copied by hand: {after['reconciled']}")
    program = ast.parse(after["program"])
    imports = [ast.unparse(node) for node in ast.walk(program) if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = {node.id for node in ast.walk(program) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(program) if isinstance(node, ast.Attribute)}
    forbidden = {"open", "exec", "eval", "compile", "__import__", "os", "subprocess", "socket", "shutil", "pathlib",
                 "environ", "getenv", "input", "stdin"}
    check(imports == ["import sys"] and not (names | attributes) & forbidden,
          f"the self-test imports only sys and touches no file, process, socket or environment: {imports} "
          f"{sorted((names | attributes) & forbidden)}")
    sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
    from scripts.ticket_board import privileged_operations as po
    command = po.command_for("job-selftest", {"project": "pgu", "outcome": "fail", "ticket": "PGU-1"})
    check(command == [po.LAUNCHER, "job-selftest", "pgu", "--outcome", "fail"] and po.LAUNCHER.startswith("/opt/switchyard/"),
          f"root runs it as it runs every catalogued operation: the pinned launcher with literals and validated values, "
          f"the ticket not among them: {command}")
    for name, text in after["wrappers"].items():
        body = ast.parse(text).body
        order = [ast.unparse(node) for node in body if isinstance(node, (ast.If, ast.ImportFrom))]
        branch = next((i for i, line in enumerate(order) if "job-selftest" in line), None)
        launcher_import = order.index("from scripts.team_launcher import switchyard_main")
        check(branch is not None and branch < launcher_import
              and "from scripts.ticket_board.privileged_selftest import main as job_selftest" in order[branch],
              f"{name} hands job-selftest to its module before it imports the launcher or anything else")
    check(after["direct"] == {"job-selftest pgu --outcome succeed": [0], "job-selftest pgu --outcome fail": [1],
                              "job-selftest pgu --outcome x": [2], "job-selftest pgu": [2],
                              "job-selftest pgu --outcome fail extra": [2], "job-selftest /etc --outcome fail": [2]},
          f"the launcher's own answer: success exits 0, failure 1, and any other shape is refused: {after['direct']}")
    print(f"privileged_job_selftest_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
