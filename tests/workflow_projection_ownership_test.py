#!/usr/bin/env python3
"""SYRD-676: a Director changes its own workflow without root, however the tenant was provisioned.

PIE's Director could not change a role, a stage or a role prompt: `.switchyard/provision/`, `plan.json` and
`workflow.json` were root:root, which is what a provision finished by `resume-provision` leaves -- only a `switchyard
new` that got past its packet handed them to the tenant. Every workflow write, its `--dry-run` included, was refused
before the board was asked.

Two halves:

- The Director's side, unprivileged, against a real board on a temporary cluster: on a projection it cannot write,
  every workflow operation's dry run validates and asks the board, and writes nothing; a real write is refused
  before the board or a journal is touched, naming the repair; once the projection is the tenant's, the write lands
  and the projection carries the board's exact new revision -- the prompt a later pane launches with included. The
  before-run is main's `ticket-board-workflow`, from a git archive, on the same layout.
- Root's repair, in a user and mount namespace with the invoking account's subuid range mapped (SYRD-612's harness,
  `role_cutover_writes_test`): PIE's layout is given back -- the directory, `plan.json`, `workflow.json` -- byte for
  byte, root's record files beside them left root's; then every way a tenant could aim the repair elsewhere is
  refused with nothing changed, and an interrupted repair converges.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import importlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "d8ea9550e5c94338b0a2ee05b98e178794cc0090"  # main before SYRD-676
PROMPT = "Ops remit (SYRD-676).\n\n- Verify staging first.\n"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


def snapshot(base: Path) -> dict[str, tuple]:
    """Every entry under `base`, unfollowed: kind, owner, mode and, for a file, its digest."""
    seen = {}
    for path in sorted([base, *base.rglob("*")]):
        info = path.lstat()
        digest = ""
        if stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            digest = os.readlink(path)
        seen[str(path.relative_to(base))] = (stat.S_IFMT(info.st_mode), info.st_uid, info.st_gid,
                                             stat.S_IMODE(info.st_mode), info.st_nlink, digest)
    return seen


# ====================================================================== the Director's side, against a real board


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def director_side() -> None:
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board.workflow_config import validate

    with temporary_cluster(prefix="syrd676-db-", shutdown="immediate") as cluster:
        root, sock, port, db = cluster.root, cluster.socket_dir, cluster.port, "syrd676"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        app = t.TicketBoardApp(root / "frames", root / "assets", project="cerulean", ticket_prefix="PGU",
                               database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE))
        example = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        # With its designer active, so the design stage can be added: every Director operation is asked here.
        designer = next(r for r in example["roles"] if r["name"] == "designer")
        designer.update(active=True)
        document = validate(example)
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        saved_env = {k: os.environ.get(k) for k in ("TICKET_BOARD_WRITE_TOKEN", "TICKET_BOARD_CALLER_ROLE")}
        try:
            t.post_json(base, "/api/tickets/actions/configure_workflow", {"document": document, "expected_revision": 0},
                        caller="director")
            # PIE's layout as its Director meets it: the launcher configuration and layout its own, the directory,
            # plan.json and workflow.json not writable by it (root's on the host; read-only here).
            tenant = root / "tenant"
            provision = tenant / ".switchyard" / "provision"
            provision.mkdir(parents=True)
            project_dir = tenant
            (project_dir / "docs" / "onboarding").mkdir(parents=True)
            (tenant / ".switchyard" / "cerulean.project.json").write_text(
                json.dumps({"project": {"repository": str(project_dir)}, "workflow": document}), encoding="utf-8")
            config_path = provision / "cerulean.json"
            config_path.write_text(json.dumps({
                "project": "cerulean", "layout": "cerulean-konsole-layout.json", "roles": [{"role": "ops"}],
                "board_url": base, "worktree_base": str(root / "worktrees"), "repository": str(project_dir),
                "workflow": document}), encoding="utf-8")
            (provision / "cerulean-konsole-layout.json").write_text("{}\n", encoding="utf-8")
            (provision / "workflow.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
            (provision / "plan.json").write_text(json.dumps({"project": "cerulean", "workflow": document}) + "\n")
            os.chmod(config_path, 0o600)
            for name in ("workflow.json", "plan.json"):
                os.chmod(provision / name, 0o444)
            os.chmod(provision, 0o555)

            os.environ["TICKET_BOARD_WRITE_TOKEN"] = server.write_token
            os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
            importlib.reload(importlib.import_module("scripts.ticket_board.write_client"))
            wm = importlib.reload(importlib.import_module("scripts.workflow_manage"))
            common = ["--board-url", base, "--config", str(config_path)]

            def run(*argv: str) -> tuple[object, str, str]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        status = wm.main([*argv, *common])
                    except SystemExit as stop:
                        status = f"exit {stop.code}"
                    except Exception as exc:  # noqa: BLE001 - what it raised is the observation
                        status = f"raised {type(exc).__name__}: {exc}"
                return status, out.getvalue(), err.getvalue()

            def revision() -> int:
                import urllib.request

                with urllib.request.urlopen(base + "/api/workflow") as response:
                    return json.load(response)["revision"]

            changed = copy.deepcopy(document)
            next(r for r in changed["roles"] if r["name"] == "ops")["onboarding_prompt"] = PROMPT
            proposed = root / "proposed.json"
            proposed.write_text(json.dumps(changed), encoding="utf-8")
            before_tree, before_revision = snapshot(tenant), revision()

            # Every Director workflow operation, as a dry run, on a projection it cannot write.
            operations = (("apply", "--document", str(proposed)),
                          ("set-role-prompt", "--role", "ops", "--prompt", PROMPT),
                          ("clear-role-prompt", "--role", "ops"),
                          ("add-design-stage",),
                          ("enable-router", "--runtime", "claude"),
                          ("enable-documentation-gate",),
                          ("enable-publisher",))
            for operation in operations:
                status, out, err = run(*operation, "--dry-run")
                check(status == 0 and json.loads(out)["expected_revision"] == before_revision,
                      f"{operation[0]} --dry-run: validated and previewed by the board without write access: "
                      f"{status} {err[-400:]}")
                check(snapshot(tenant) == before_tree and revision() == before_revision,
                      f"{operation[0]} --dry-run: and nothing written, on the board or the projection")

            status, _out, err = run("set-role-prompt", "--role", "ops", "--prompt", PROMPT)
            check("projection path is not writable" in str(status)
                  and "`sudo switchyard upgrade cerulean --only workflow-projection`" in str(status),
                  f"a real write to a projection it cannot write is refused, naming the repair: {status} {err[-300:]}")
            check(snapshot(tenant) == before_tree and revision() == before_revision,
                  "refused before the board or a journal was touched")

            # main, on the very same layout and board: even the dry run is refused.
            with tempfile.TemporaryDirectory(prefix="syrd676-main.") as raw:
                tree = tree_at(BEFORE, Path(raw) / "tree")
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                done = subprocess.run([sys.executable, str(tree / "scripts" / "workflow_manage.py"), "set-role-prompt",
                                       "--role", "ops", "--prompt", PROMPT, "--dry-run", *common],
                                      capture_output=True, text=True, timeout=120, env=env, cwd=str(tree),
                                      stdin=subprocess.DEVNULL)
            check(done.returncode != 0 and "PermissionError: projection path is not writable" in done.stderr
                  and snapshot(tenant) == before_tree,
                  f"before: main refuses the dry run itself, before the board is asked: {done.returncode} "
                  f"{done.stderr[-300:]}")

            # What the repair leaves: the directory and the two documents the tenant's.
            os.chmod(provision, 0o755)
            for name in ("workflow.json", "plan.json"):
                os.chmod(provision / name, 0o644)
            status, out, err = run("set-role-prompt", "--role", "ops", "--prompt", PROMPT)
            landed = json.loads(out)["revision"] if status == 0 else None
            check(landed is not None and landed > before_revision and revision() == landed,
                  f"then the Director's write lands, without root, as the board's revision: {status} {err[-300:]}")
            import urllib.request

            with urllib.request.urlopen(base + "/api/workflow") as response:
                board = json.load(response)
            on_disk = json.loads((provision / "workflow.json").read_text())
            check(on_disk == validate(board["document"]) and json.loads((provision / "plan.json").read_text())["workflow"]
                  == validate(board["document"]),
                  "and the projection is the board's exact new document")
            projected = json.loads(config_path.read_text())
            ops = next(r for r in projected["roles"] if r["role"] == "ops")
            check(ops["env"].get("TICKET_BOARD_ROLE_ONBOARDING_PROMPT") == PROMPT,
                  f"so the ops pane launched next carries the new prompt: {ops.get('env')}")
            status, out, err = run("clear-role-prompt", "--role", "ops")
            projected = json.loads(config_path.read_text())
            ops = next(r for r in projected["roles"] if r["role"] == "ops")
            check(status == 0 and json.loads(out)["revision"] > landed and revision() == json.loads(out)["revision"]
                  and "TICKET_BOARD_ROLE_ONBOARDING_PROMPT" not in ops.get("env", {}),
                  f"and clearing it is as unprivileged, and leaves no prompt behind: {status} {err[-300:]}")
        finally:
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            server.shutdown()
            server.server_close()


# ====================================================================== root's repair, two real identities


def root_side(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import role_cutover_writes_test as harness

    from scripts import projection_ownership
    from scripts.ticket_board.workflow_config import validate

    owner, owner_uid, foreign_uid = harness.OWNER, harness.OWNER_UID, harness.FOREIGN_UID
    example = (tree / "examples/workflows/inspection.json").read_text()

    def for_project(slug: str) -> dict:
        return validate(json.loads(example.replace("cerulean", slug)), project=slug)

    document = for_project("p")
    from scripts.ticket_board.project_provision import workflow_document_digest

    # As root's packet publishes it -- its own record -- until a first workflow write projects the bare document.
    workflow_text = json.dumps({"project": "p", "digest": workflow_document_digest(document), "document": document},
                               indent=2, sort_keys=True) + "\n"
    out: dict = {}

    def pie(base: Path, name: str):
        """PIE's layout, in a home of its own: the directory and two documents root's, root's records beside them."""
        host = harness.Host(base / name)
        harness.accounts(host)
        project = host.home / "Projects" / "p"
        provision = project / ".switchyard" / "provision"
        config_path = host.make(provision / "p.json", json.dumps({"project": "p", "layout": "p-konsole-layout.json",
                                                                  "run_as_user": owner, "roles": []}), owner_uid, 0o600)
        host.make(provision / "p-konsole-layout.json", "{}\n", owner_uid)
        host.make(project / ".switchyard" / "p.project.json", json.dumps({"project": {"slug": "p"}}), owner_uid)
        os.lchown(provision, 0, 0)
        os.chmod(provision, 0o755)
        for record, text, mode in (("workflow.json", workflow_text, 0o644),
                                   ("plan.json", json.dumps({"project": "p", "owner_user": owner}) + "\n", 0o644),
                                   ("p-ticket-board.service", "[Service]\nExecStart=/bin/true\n", 0o644),
                                   ("49-p-role-control", "operator ALL=(root) NOPASSWD: /bin/true\n", 0o644),
                                   ("operator-commands.sh", "#!/bin/sh\ntrue\n", 0o755)):
            (provision / record).write_text(text, encoding="utf-8")
            os.lchown(provision / record, 0, 0)
            os.chmod(provision / record, mode)
        etc = base / name / "etc-switchyard" / "p"
        etc.mkdir(parents=True, mode=0o700)
        # Root's own copies, which a tenant's link would aim the repair at: valid, so only refusing the link stops it.
        (etc / "plan.json").write_text(json.dumps({"project": "p", "owner_user": owner}) + "\n", encoding="utf-8")
        (etc / "workflow.json").write_text(workflow_text, encoding="utf-8")
        for item in (etc / "plan.json", etc / "workflow.json"):
            os.chmod(item, 0o600)
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(etc.parent)
        return host, provision, config_path, SimpleNamespace(project="p", run_as_user=owner)

    with tempfile.TemporaryDirectory(prefix="syrd676.") as raw:
        base = Path(raw)
        base.chmod(0o755)

        # The repair, on PIE's layout.
        host, provision, config_path, config = pie(base, "pie")
        before = snapshot(host.base)
        would, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=False)
        out["dry"] = {"would": [Path(p).name for p in would], "problems": problems,
                      "unchanged": snapshot(host.base) == before}
        handed, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        after = snapshot(host.base)
        out["apply"] = {
            "handed": [Path(p).name for p in handed], "problems": problems,
            "owners": {name: after[str((provision / name).relative_to(host.base))][1]
                       for name in ("workflow.json", "plan.json", "p.json", "p-ticket-board.service",
                                    "49-p-role-control", "operator-commands.sh")},
            "dir_owner": after[str(provision.relative_to(host.base))][1],
            "bytes_kept": all(after[k][5] == before[k][5] and after[k][3] == before[k][3] for k in before),
            "only_these_changed": sorted(k for k in before if before[k] != after[k]),
            "etc_untouched": all(before[k] == after[k] for k in before if k.startswith("etc-switchyard")),
            "workflow_digest_kept": hashlib.sha256((provision / "workflow.json").read_bytes()).hexdigest()
                                    == hashlib.sha256(workflow_text.encode()).hexdigest(),
        }
        again, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        out["again"] = {"handed": again, "problems": problems, "unchanged": snapshot(host.base) == after}
        # As the tenant, now: its documents replaced atomically, root's records not. Through a descriptor of the
        # directory, opened before the switch: the fixture sits under the run's private TMPDIR, which only the
        # invoking account can traverse, and the directory's own permissions are what this asks about.
        directory = os.open(provision, os.O_RDONLY | os.O_DIRECTORY)
        child = os.fork()
        if child == 0:
            code = 0
            try:
                os.setgroups([])
                os.setgid(owner_uid)
                os.setuid(owner_uid)
                staged = os.open(".workflow.json.new", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory)
                os.write(staged, workflow_text.encode())
                os.close(staged)
                os.rename(".workflow.json.new", "workflow.json", src_dir_fd=directory, dst_dir_fd=directory)
                for record in ("p-ticket-board.service", "49-p-role-control"):
                    try:
                        os.close(os.open(record, os.O_WRONLY | os.O_APPEND, dir_fd=directory))
                        code = 3
                    except PermissionError:
                        pass
            except BaseException as exc:  # noqa: BLE001
                print(f"tenant child: {type(exc).__name__}: {exc}", file=sys.stderr)
                code = 2
            os._exit(code)
        _pid, status = os.waitpid(child, 0)
        os.close(directory)
        out["tenant_writes"] = os.waitstatus_to_exitcode(status)

        # What a tenant could leave there, each refused with nothing changed.
        def refused(label: str, plant) -> None:
            host, provision, config_path, config = pie(base, label)
            target = plant(host, provision)
            snap = snapshot(host.base)
            handed, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
            out[label] = {"handed": handed, "problems": problems, "unchanged": snapshot(host.base) == snap,
                          "target_owner": target.lstat().st_uid if target is not None else None}

        def linked(host, provision):
            target = host.base / "etc-switchyard" / "p" / "workflow.json"
            (provision / "workflow.json").unlink()
            os.symlink(target, provision / "workflow.json")
            return target

        def hard_linked(host, provision):
            target = host.base / "etc-switchyard" / "p" / "plan.json"
            (provision / "plan.json").unlink()
            os.link(target, provision / "plan.json")
            return target

        def fifo(host, provision):
            (provision / "workflow.json").unlink()
            os.mkfifo(provision / "workflow.json")
            return None

        def foreign(host, provision):
            os.lchown(provision / "plan.json", foreign_uid, foreign_uid)
            return provision / "plan.json"

        def other_project(host, provision):
            other = for_project("q")
            (provision / "workflow.json").write_text(json.dumps(other))
            return provision / "workflow.json"

        def other_record(host, provision):
            other = for_project("q")
            (provision / "workflow.json").write_text(json.dumps({"project": "q", "document": other,
                                                                 "digest": workflow_document_digest(other)}))
            return provision / "workflow.json"

        def forged_record(host, provision):
            forged = json.loads(workflow_text)
            forged["document"]["roles"][0]["label"] = "Forged"
            (provision / "workflow.json").write_text(json.dumps(forged))
            return provision / "workflow.json"

        def linked_directory(host, provision):
            # The provision directory a link to root's own: its configuration, layout and documents are all there.
            etc = host.base / "etc-switchyard" / "p"
            for name in ("p.json", "p-konsole-layout.json"):
                (etc / name).write_bytes((provision / name).read_bytes())
            shutil.rmtree(provision)
            os.symlink(etc, provision)
            return etc

        for label, plant in (("link", linked), ("hard link", hard_linked), ("fifo", fifo), ("foreign owner", foreign),
                             ("another project's workflow", other_project), ("linked directory", linked_directory),
                             ("another project's record", other_record), ("a record its digest does not match", forged_record)):
            refused(label, plant)

        # Outside the owner's roots: a configuration path that is not in its home.
        host, provision, config_path, config = pie(base, "escape")
        outside = host.base / "outside" / ".switchyard" / "provision"
        outside.mkdir(parents=True)
        (outside / "p.json").write_text(json.dumps({"project": "p", "layout": "x.json"}))
        os.chmod(outside / "p.json", 0o644)
        snap = snapshot(host.base)
        handed, problems = projection_ownership.restore_projection_ownership(config, outside / "p.json", apply=True)
        out["escape"] = {"handed": handed, "problems": problems, "unchanged": snapshot(host.base) == snap}

        # A configuration inside the home whose layout names a file outside it -- root's own copy.
        host, provision, config_path, config = pie(base, "layout escape")
        aimed = host.base / "etc-switchyard" / "p" / "workflow.json"
        os.chmod(config_path, 0o600)
        config_path.write_text(json.dumps({"project": "p", "layout": str(aimed), "run_as_user": owner, "roles": []}))
        snap = snapshot(host.base)
        handed, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        out["layout escape"] = {"handed": handed, "problems": problems, "unchanged": snapshot(host.base) == snap,
                                "target_owner": aimed.lstat().st_uid}

        # Interrupted: the workflow handed, the plan and the directory not yet. The next run finishes it.
        host, provision, config_path, config = pie(base, "interrupted")
        os.lchown(provision / "workflow.json", owner_uid, owner_uid)
        handed, problems = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        out["interrupted"] = {"handed": [Path(p).name for p in handed], "problems": problems,
                              "all_owner": all((provision / n).lstat().st_uid == owner_uid
                                               for n in ("workflow.json", "plan.json", "."))}

        # A handoff the kernel refuses part-way -- an unmapped uid, a read-only mount -- is said, never raised, and
        # the next run finishes it.
        host, provision, config_path, config = pie(base, "refused part-way")
        real_fchown, calls = os.fchown, []

        def second_refused(fd, uid, gid):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError(22, "Invalid argument")
            real_fchown(fd, uid, gid)

        os.fchown = second_refused
        try:
            first = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        finally:
            os.fchown = real_fchown
        second = projection_ownership.restore_projection_ownership(config, config_path, apply=True)
        out["refused part-way"] = {"first": [[Path(p).name for p in first[0]], first[1]],
                                   "second": [[Path(p).name for p in second[0]], second[1]],
                                   "all_owner": all((provision / n).lstat().st_uid == owner_uid
                                                    for n in ("workflow.json", "plan.json", "."))}

        # The narrow repair, as the upgrade command takes it, and as a dry run.
        from scripts import project_upgrade_command

        host, provision, config_path, config = pie(base, "command")
        # Here root's workflow.json is the bare document, as a projection written by root leaves it.
        (provision / "workflow.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        said: list[str] = []
        snap = snapshot(host.base)
        dry = project_upgrade_command.upgrade_project_command(config, config_path=config_path, dry_run=True,
                                                              only="workflow-projection", print_func=said.append)
        dry_unchanged = snapshot(host.base) == snap
        real = project_upgrade_command.upgrade_project_command(config, config_path=config_path, dry_run=False,
                                                               only="workflow-projection", print_func=said.append)
        out["command"] = {"dry": dry, "dry_unchanged": dry_unchanged, "real": real, "said": said,
                          "dir_owner": provision.lstat().st_uid,
                          "bare_workflow_owner": (provision / "workflow.json").lstat().st_uid}

        # resume-provision's handoff: what it says, and what it leaves.
        host, provision, config_path, config = pie(base, "resume")
        lines = projection_ownership.resume_handoff(config, config_path=config_path)
        out["resume"] = {"lines": lines, "dir_owner": provision.lstat().st_uid,
                         "workflow_owner": (provision / "workflow.json").lstat().st_uid}
    return out


def root_checks(got: dict) -> None:
    from scripts import resume_provision_command

    check(got["dry"] == {"would": ["provision", "workflow.json", "plan.json"], "problems": [], "unchanged": True},
          f"PIE's layout: a dry run names the directory and the two documents, and changes nothing: {got['dry']}")
    applied = got["apply"]
    check(applied["handed"] == ["provision", "workflow.json", "plan.json"] and applied["problems"] == [],
          f"PIE's layout: given back, the directory and the two documents: {applied}")
    check(applied["owners"] == {"workflow.json": 1500, "plan.json": 1500, "p.json": 1500, "p-ticket-board.service": 0,
                                "49-p-role-control": 0, "operator-commands.sh": 0} and applied["dir_owner"] == 1500,
          f"PIE's layout: the owner's now, root's unit, sudoers and packet records still root's: {applied}")
    check(applied["bytes_kept"] and applied["workflow_digest_kept"],
          f"PIE's layout: contents and modes untouched, so the workflow's digest is what it was: {applied}")
    check(applied["only_these_changed"] == ["home/tenant/Projects/p/.switchyard/provision",
                                            "home/tenant/Projects/p/.switchyard/provision/plan.json",
                                            "home/tenant/Projects/p/.switchyard/provision/workflow.json"]
          and applied["etc_untouched"],
          f"PIE's layout: nothing else changed, root's own /etc copy included: {applied['only_these_changed']}")
    check(got["again"] == {"handed": [], "problems": [], "unchanged": True},
          f"run again: nothing to do, nothing changed: {got['again']}")
    check(got["tenant_writes"] == 0,
          f"the tenant then replaces its workflow document, and cannot change root's unit or sudoers record: "
          f"exit {got['tenant_writes']}")
    for label, why in (("link", "is a symbolic link"), ("hard link", "has 2 links"),
                       ("fifo", "is neither a regular file nor a directory"), ("foreign owner", "is uid 1700's"),
                       ("another project's workflow", "is not a workflow of p's"),
                       ("another project's record", "is root's workflow record for 'q'"),
                       ("a record its digest does not match", "whose digest is not its document's"),
                       ("linked directory", "symbolic link")):
        case = got[label]
        check(case["handed"] == [] and case["unchanged"] and any(why in p for p in case["problems"]),
              f"{label}: refused, and nothing at all changed: {case}")
        if case["target_owner"] is not None and label in ("link", "hard link", "linked directory"):
            check(case["target_owner"] == 0, f"{label}: what it led to keeps its owner: {case}")
    check(got["escape"]["handed"] == [] and got["escape"]["unchanged"]
          and any("outside 1500's roots" in p or "cannot be read" in p for p in got["escape"]["problems"]),
          f"a configuration outside the owner's roots: refused, nothing changed: {got['escape']}")
    escape = got["layout escape"]
    check(escape["handed"] == [] and escape["unchanged"] and escape["target_owner"] == 0
          and any("outside 1500's roots" in p for p in escape["problems"]),
          f"a layout named outside the owner's roots: refused, nothing changed, root's copy root's: {escape}")
    check(got["interrupted"]["handed"] == ["provision", "plan.json"] and got["interrupted"]["all_owner"],
          f"an interrupted repair is finished by the next run: {got['interrupted']}")
    partway = got["refused part-way"]
    check(partway["first"][0] == ["provision"] and any("could not be given to 1500 (Invalid argument)" in p
                                                        for p in partway["first"][1])
          and partway["second"] == [["workflow.json", "plan.json"], []] and partway["all_owner"],
          f"a handoff refused part-way is said, not raised, and the next run finishes it: {partway}")
    command = got["command"]
    check(command["dry"] == 0 and command["dry_unchanged"] and command["real"] == 0 and command["dir_owner"] == 1500
          and command["bare_workflow_owner"] == 1500
          and any("would give 1500 back" in line for line in command["said"])
          and any("Root's own records beside them stay root's; no upgrade phase ran" in line for line in command["said"]),
          f"`switchyard upgrade p --only workflow-projection`, dry and then real: {command}")
    check(got["resume"]["dir_owner"] == 1500 and got["resume"]["workflow_owner"] == 1500
          and any("gave 1500 back its workflow projection" in line for line in got["resume"]["lines"]),
          f"resume-provision's handoff: {got['resume']}")
    fn = next(n for n in ast.parse(Path(resume_provision_command.__file__).read_text()).body
              if isinstance(n, ast.FunctionDef) and n.name == "_finish_provision_after_packet")
    at = next((i for i, s in enumerate(fn.body) if "projection_ownership.resume_handoff(" in ast.unparse(s)), None)
    start = next((i for i, s in enumerate(fn.body) if isinstance(s, ast.If) and ast.unparse(s.test) == "start_roles"), -1)
    check(at is not None and at < start, f"resume-provision hands it over before it starts any role: {at} < {start}")


def test_the_command_is_root_s_and_parses() -> None:
    sys.path[:0] = [str(ROOT)]
    from scripts import projection_ownership
    from scripts import team_launcher as launcher

    args = launcher._build_switchyard_upgrade_parser().parse_args(["pie", "--only", "workflow-projection", "--dry-run"])
    check((args.only, args.dry_run) == ("workflow-projection", True), "the narrow step parses, with --dry-run")
    if os.geteuid() != 0:
        said: list[str] = []
        status = projection_ownership.restore_command(SimpleNamespace(project="pie", run_as_user="pie-agent"),
                                                      config_path=Path("/nonexistent/pie.json"), dry_run=False,
                                                      print_func=said.append)
        check(status == 1 and "`sudo switchyard upgrade pie --only workflow-projection`" in said[0]
              and "Nothing was changed" in said[0], f"unprivileged, it names itself and changes nothing: {said}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--root-side":
        print(json.dumps(root_side(Path(sys.argv[2])), default=str))
        return 0
    test_the_command_is_root_s_and_parses()
    done = subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable,
                           str(Path(__file__).resolve()), "--root-side", str(ROOT)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    check(done.returncode == 0, f"the namespace run finished: {done.stdout[-500:]} {done.stderr[-3000:]}")
    sys.path[:0] = [str(ROOT)]
    root_checks(json.loads(done.stdout.strip().splitlines()[-1]))
    if os.environ.get("SYRD676_MUTATION_SKIP_BOARD") != "1":
        director_side()
    print(f"workflow_projection_ownership_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
