#!/usr/bin/env python3
"""SYRD-855: one tenant rolls back to an earlier release already staged for it, and select-shared-release has a caller.

On 2026-11-22 syrd needed to go back from bceacabf to cdc3a741 and nothing could do it: `deploy-release` refused a
commit root's pin did not name, `preview-upgrade` refused the recorded source, the operator's `upgrade --source-repo`
was refused by the launcher running it (an upgrade stages only the release it runs), and `select-shared-release`
asked root for that same upgrade -- from a caller who had to be a pane and a person at once.

Now `rollback-release` (the control pane, pre-authorized) and `select-shared-release` (an administrator, from their
own terminal) ask root for `deploy-release <p> --commit <A> --rollback [--any-staged]`. Driven through the helper's
real request parsing and `run_privileged_action`, whose runner hands the argv to the tree's own `switchyard_main` as
root in-process. Every service step goes to a recording runner that answers as systemd, PostgreSQL and the deploy
would; the deploy really moves the board's `current`, and the board then reports release A. Main's catalogue and
helper are run in a child from a git worktree for the reproduction.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "bceacabfeb57f42d5dd5b5de85daae07d0663952"  # main before SYRD-855
A, B, OTHER = "a" * 40, "b" * 40, "f" * 40
NEWER = ["pgu1051_syrd842_research_briefs.sql", "pgu1052_syrd843_plan_gate.sql",
         "pgu1053_syrd848_documentation_pair.sql", "pgu1054_syrd849_findings_publication.sql",
         "pgu1055_syrd852_finding_mark.sql"]
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def guarded(tree: Path) -> None:
    """A child's host is its own: the guard names any read of this host's state, and root's records are the run's."""
    sys.path[:0] = [str(tree), str(tree / "tests"), str(ROOT / "tests")]
    os.environ.setdefault("SWITCHYARD_TENANT_CONTROL_ROOT", tempfile.mkdtemp(prefix="syrd855-control."))
    import host_state_guard

    host_state_guard.install()
    import host_record_redirects  # noqa: F401


def untouched(name: str) -> None:
    import host_state_guard

    host_state_guard.assert_untouched(name)


class Host:
    """systemd, PostgreSQL and the deploy, as the recording runner answers them."""

    def __init__(self, tenant, board, unit_dir: Path, trusted_release: Path, target: str, *, applied, answers,
                 deploy="ok") -> None:
        self.tenant, self.board, self.unit_dir = tenant, board, unit_dir
        self.trusted_release, self.target = trusted_release, target
        self.applied, self.answers, self.deploy = applied, answers, deploy
        self.steps: list[str] = []
        self.listener = "active"

    def move_board(self, commit: str) -> None:
        current = self.tenant.board_root / "current"
        current.unlink()
        current.symlink_to(self.tenant.board_root / "releases" / commit)

    def __call__(self, argv, **kwargs):
        text = " ".join(str(part) for part in argv)
        result = lambda code, out="": SimpleNamespace(returncode=code, stdout=out,
                                                      stderr="" if code == 0 else "simulated failure")
        if argv[:4] == ["runuser", "-u", "postgres", "--"] and "psql" in argv and argv[-2] == "-c":
            assert argv[4:6] == ["env", "-i"] and kwargs.get("cwd") == "/", argv
            sql = argv[-1]
            if sql == "DO $$ BEGIN NULL; END $$;":
                self.steps.append("postgres probe")
                return result(0)
            if sql == "SELECT name FROM ticket_board.schema_migrations":
                self.steps.append("read applied migrations")
                return result(0, "\n".join(self.applied) + "\n")
            for expression, answer in self.answers.items():
                if f"({expression})" in sql:
                    self.steps.append(f"ask {expression}")
                    return result(0, answer + "\n") if answer in ("true", "false") else result(2)
            raise AssertionError(f"unmodelled query: {sql}")
        if argv[:4] == ["runuser", "-u", "postgres", "--"]:
            assert any(part.startswith(str(self.trusted_release) + "/") for part in argv), argv
            self.steps.append("migrate as postgres" if argv[-1].endswith("ticket-board-migrate") else "rbac as postgres")
            return result(0)
        if "systemctl --user is-active" in text:
            return result(0 if self.listener == "active" else 3, self.listener)
        if text.startswith("systemctl is-active ") and text.endswith("-ticket-board.service"):
            return result(0, "active")
        if "systemctl --user stop" in text:
            self.steps.append("stop listener")
            self.listener = "inactive"
            return result(0)
        if "systemctl --user daemon-reload && systemctl --user restart" in text:
            self.steps.append("start listener")
            self.listener = "active"
            return result(0)
        if "sudo install" in text:
            self.steps.append("install templates")
            return result(0)
        if argv[0] == "install":
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
            if "install units" not in self.steps:
                self.steps.append("install units")
            return result(0)
        if argv[:2] in (["systemctl", "daemon-reload"], ["systemctl", "restart"]):
            return result(0)
        if "deploy-restart" in text:
            words = shlex.split(argv[-1])
            assert "TICKET_BOARD_SKIP_MIGRATIONS=1" in words, words
            assert str(self.trusted_release) in (self.unit_dir / "porter-ticket-board.service").read_text()
            self.steps.append("deploy-restart")
            if self.deploy == "ok":
                self.move_board(self.target)
                self.board.build_id = self.target
                return result(0)
            return result(1)
        self.steps.append("other: " + text[:80])
        return result(0)


def scenarios(tree: Path) -> dict:
    guarded(tree)
    import release_phase_journal_test as journal
    from scripts import team_launcher as launcher
    from scripts import tenant_release_deploy
    from scripts.ticket_board import board_identity
    from scripts.ticket_board import privileged_helper as helper
    from scripts.ticket_board import privileged_operations as operations
    from scripts.ticket_board.project_provision import build_plan, render_board_unit, render_canary_unit

    seen: dict = {}

    def run(name: str, *, action: str = "rollback-release", target: str = A, previous: str = A,
            staged: bool = True, installed: tuple[str, ...] = (A, B), applied=None, answers=None,
            deploy: str = "ok", runtime: bool = True) -> None:
        applied = ["schema.sql", "pgu1_fixture.sql", *NEWER] if applied is None else applied
        answers = {"NOT ticket_board.research_declared()": "true",
                   "ticket_board.plan_policy() IS NULL": "true"} if answers is None else answers
        with tempfile.TemporaryDirectory(prefix="syrd855.") as raw:
            tmp = Path(raw)
            tenant = journal.Tenant(tmp, deployed=B)
            if staged:
                release = tenant.board_root / "releases" / A
                release.mkdir(parents=True)
                (release / ".pgu-deploy-sha").write_text(A + "\n", encoding="utf-8")
            releases = tmp / "opt-switchyard" / "releases"
            for commit in installed:
                release = releases / commit
                (release / "scripts" / "ticket_board" / "migrations").mkdir(parents=True, exist_ok=True)
                (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}))
                for script in ("ticket-board-service.sh", "ticket-board-migrate"):
                    (release / "scripts" / script).write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                (release / "scripts" / "ticket_board" / "rbac.sql").write_text("SELECT 1;\n", encoding="utf-8")
                (release / "scripts" / "ticket_board" / "migrations" / "pgu1_fixture.sql").write_text("SELECT 1;\n")
                if commit == B:
                    for newer in NEWER:
                        (release / "scripts" / "ticket_board" / "migrations" / newer).write_text("SELECT 1;\n")
                if runtime:
                    (release / "scripts" / "ticket_board" / "release_units.py").write_text("# renderer\n")
            units = launcher.privileged_provision_dir("porter", root=launcher.switchyard_privileged_provision_root())
            units.mkdir(parents=True, exist_ok=True)
            plan = build_plan(project="porter", owner_user=getpass.getuser(), owner_home=tmp / "tenant",
                              board_root=tenant.board_root, port=29117, commit_git_dir=tmp / "cache.git",
                              asset_dir=tmp / "assets", frame_dir=tmp / "frames")
            (units / "porter-ticket-board.service").write_text(render_board_unit(plan), encoding="utf-8")
            (units / "porter-ticket-board-canary.service").write_text(render_canary_unit(plan), encoding="utf-8")
            (units / "porter-ticket-board-notify-listener.service").write_text("[Unit]\n", encoding="utf-8")
            unit_dir = tmp / "systemd"
            unit_dir.mkdir()
            if B in installed:
                # What B's deploy installed: root's exact render of B, which names the tenant's database.
                for unit, payload in tenant_release_deploy._candidate_units(
                        SimpleNamespace(provisioned_system_unit=units / "porter-ticket-board.service"), "porter",
                        releases / B, 26991).items():
                    (unit_dir / unit.name).write_bytes(payload)
            pin_path = launcher.privileged_upgrade_source_path(tenant.config)
            pin_path.write_text(json.dumps({"schema": launcher.UPGRADE_SOURCE_SCHEMA, "project": "porter",
                                            "source_repo": str(releases / B), "commit_git_dir": "/data/git/cache",
                                            "deploy_ref": B}), encoding="utf-8")
            os.chmod(pin_path, 0o600)
            note = launcher.release_rollback_path("porter")
            note.write_text(json.dumps({"schema": launcher.RELEASE_ROLLBACK_SCHEMA, "project": "porter",
                                        "upgrading_to": B, "previous_board_commit": previous,
                                        "previous_staged_commit": previous}), encoding="utf-8")
            os.chmod(note, 0o600)
            real_euid = launcher.os.geteuid
            launcher.os.geteuid = lambda: 0
            try:
                launcher.record_upgrade_phase(tenant.config, config_path=tenant.config_path, phase="release",
                                              state="done", detail=f"serving {B}")
            finally:
                launcher.os.geteuid = real_euid
            board = journal.FakeBoard(B)
            host = Host(tenant, board, unit_dir, releases / target, target, applied=applied, answers=answers,
                        deploy=deploy)
            restaged: list[str] = []
            saved = (launcher._resolve_switchyard_project, launcher._open_board_url, launcher.os.geteuid,
                     launcher.subprocess.run, operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID,
                     tenant_release_deploy.SYSTEM_UNIT_DIR, tenant_release_deploy.TRUSTED_UNIT_OWNER_UID,
                     board_identity.effective_unit, launcher.refresh_staged_role_tooling,
                     launcher.refresh_role_pane_hooks, board_identity.board_opener)
            # Root reads its board over the unit's own connection; the fake board stands in for that connection.
            board_identity.board_opener = lambda config, given=None: given or board
            launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(
                slug="porter", config_path=tenant.config_path)
            launcher._open_board_url = board
            launcher.os.geteuid = lambda: 0
            operations.SHARED_RELEASES = str(releases)
            operations.TRUSTED_OWNER_UID = os.getuid()
            tenant_release_deploy.SYSTEM_UNIT_DIR = unit_dir
            tenant_release_deploy.TRUSTED_UNIT_OWNER_UID = os.getuid()

            def attest(*_args, **_kwargs):
                if board.build_id != target:
                    raise board_identity.BoardIdentityError(f"board reports {board.build_id}, not {target}")
                return SimpleNamespace(release_commit=target)

            board_identity.effective_unit = attest
            # Staging itself is its own suites'; here, which release it was staged from, and only after the deploy.
            launcher.refresh_staged_role_tooling = lambda config, *, release_root, **_k: (
                restaged.append(f"tooling from {Path(release_root).name}"), host.steps.append("restage tooling"),
                [])[-1]
            launcher.refresh_role_pane_hooks = lambda config, **_k: (host.steps.append("refresh hooks"), [])[-1]
            launcher.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"unmodelled: {a[:1]}"))
            printed: list[str] = []
            attempts: list[dict] = []

            class Attempt:
                attempt = "0001"

                def __init__(self, project, command, **kwargs):
                    attempts.append({"project": project, "command": list(command), **kwargs})

                def describe_job(self, job):
                    attempts[-1]["job"] = job

                def open(self):
                    return tmp / "rollout"

                def close(self, **kwargs):
                    attempts[-1]["closed"] = kwargs

            def dispatch(command, *, timeout):
                if command[:2] != [operations.LAUNCHER, "deploy-release"]:
                    return SimpleNamespace(returncode=f"not dispatched: {command}")
                return SimpleNamespace(returncode=launcher.switchyard_main_for_test(command[1:], host, printed))

            try:
                parsed, values = helper.parse_request([action, "project=porter", f"commit={target}"])
                try:
                    code = helper.run_privileged_action(parsed, values, project="porter", attempt_factory=Attempt,
                                                       runner=dispatch, print_func=printed.append)
                except helper.Refused as exc:
                    code, printed = "refused", [str(exc)]
            finally:
                (launcher._resolve_switchyard_project, launcher._open_board_url, launcher.os.geteuid,
                 launcher.subprocess.run, operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID,
                 tenant_release_deploy.SYSTEM_UNIT_DIR, tenant_release_deploy.TRUSTED_UNIT_OWNER_UID,
                 board_identity.effective_unit, launcher.refresh_staged_role_tooling,
                 launcher.refresh_role_pane_hooks, board_identity.board_opener) = saved
            seen[name] = {
                "code": code, "steps": host.steps, "said": "\n".join(printed), "restaged": restaged,
                "board": launcher._current_tenant_release(tenant.board_root)[1], "build": board.build_id,
                "listener": host.listener, "release": launcher.upgrade_phase_state(tenant.trusted(), "release"),
                "pin": launcher.read_upgrade_source(tenant.config), "attempts": attempts,
                "releases": str(releases),
            }

    run("rolled back")
    run("not the release the last upgrade replaced", previous=OTHER)
    run("an administrator selects any staged release", action="select-shared-release", previous=OTHER)
    run("not staged for the tenant", staged=False)
    run("root does not hold it", installed=(B,))
    run("already on it", target=B, previous=B)
    run("a migration nobody declared", applied=["schema.sql", "pgu1_fixture.sql", *NEWER, "pgu9999_unchecked.sql"])
    run("the research stage is declared", answers={"NOT ticket_board.research_declared()": "false",
                                                   "ticket_board.plan_policy() IS NULL": "true"})
    run("a condition cannot be read", answers={"NOT ticket_board.research_declared()": "error",
                                               "ticket_board.plan_policy() IS NULL": "true"})
    run("the deploy fails", deploy="fail")
    run("no migration crossed", applied=["schema.sql", "pgu1_fixture.sql"], answers={})
    untouched("tenant_release_rollback_test scenarios")
    return seen


def install_test_dispatch() -> None:
    """`switchyard_main` with the host's runner handed to the deploy, the one seam."""
    from scripts import team_launcher as launcher
    from scripts import tenant_release_deploy

    def main_for_test(argv, host, printed):
        original = tenant_release_deploy.switchyard_deploy_release_command

        def with_host(project, *, commit, **kwargs):
            return original(project, commit=commit, runner=host, print_func=printed.append, **kwargs)

        launcher.switchyard_deploy_release_command = with_host
        try:
            return launcher.switchyard_main(list(argv))
        finally:
            launcher.switchyard_deploy_release_command = original

    launcher.switchyard_main_for_test = main_for_test


def callers(tree: Path) -> dict:
    """Who the helper and the front door let ask for select-shared-release and rollback-release, outside a pane."""
    guarded(tree)
    from scripts.ticket_board import polkit_preflight, privileged_actions
    from scripts.ticket_board import privileged_front_door as front
    from scripts.ticket_board import privileged_helper as helper
    from scripts.ticket_board import privileged_install
    from scripts.ticket_board import privileged_operations as operations

    def no_pane(*_a, **_k):
        raise helper.Refused("cannot establish which pane this was run from")

    out: dict = {}
    reached: list[str] = []
    saved = (privileged_install.verify_installation, helper.caller_identity, helper.authorizing_board,
             helper.run_privileged_action, operations.trusted_release_root)
    privileged_install.verify_installation = lambda *a, **k: []
    helper.caller_identity = no_pane
    helper.authorizing_board = lambda action, project, **_k: (lambda *_a: {}, "http://board")
    helper.run_privileged_action = lambda action, values, **k: (reached.append(action.name), 0)[-1]
    operations.trusted_release_root = lambda commit, **_k: f"/opt/switchyard/releases/{commit}"
    try:
        for action in ("select-shared-release", "rollback-release"):
            reached.clear()
            code = helper.main([action, "project=porter", f"commit={A}"])
            out[f"helper {action}"] = {"code": code, "reached": list(reached)}

        def decide(answer):
            return lambda action_id, **_k: polkit_preflight.PolkitDecision(answer, action_id, "")

        policy = privileged_actions.render_policy("/helper")

        def plan(action, *, interactive, answer=polkit_preflight.NEEDS_AUTHENTICATION):
            built = front.plan_action("porter", action, {"commit": A}, identity=no_pane, decide=decide(answer),
                                      verify=lambda *a, **k: [], read_policy=lambda _p: policy,
                                      interactive=lambda: interactive)
            return {"ready": built.ready, "problems": built.problems}

        if "interactive" in front.plan_action.__code__.co_varnames:
            out["terminal select"] = plan("select-shared-release", interactive=True)
            out["no terminal select"] = plan("select-shared-release", interactive=False)
            out["terminal rollback"] = plan("rollback-release", interactive=True)
        else:
            try:
                out["terminal select"] = {"ready": front.plan_action(
                    "porter", "select-shared-release", {"commit": A}, identity=no_pane,
                    decide=decide(polkit_preflight.NEEDS_AUTHENTICATION), verify=lambda *a, **k: [],
                    read_policy=lambda _p: policy).ready}
            except helper.Refused as exc:
                out["terminal select"] = {"ready": False, "problems": [str(exc)]}
    finally:
        (privileged_install.verify_installation, helper.caller_identity, helper.authorizing_board,
         helper.run_privileged_action, operations.trusted_release_root) = saved
    untouched("tenant_release_rollback_test callers")
    return out


def note_reads(tree: Path) -> dict:
    """The previous-release check on root's note as a file, a link to it, and a file others may write (SYRD-858)."""
    guarded(tree)
    from scripts import tenant_release_rollback as rollback

    out: dict = {}
    opened: list[int] = []
    real_open = rollback.os.open
    with tempfile.TemporaryDirectory(prefix="syrd858.") as raw:
        tmp = Path(raw)
        note = tmp / "release-rollback.json"
        note.write_text(json.dumps({"upgrading_to": B, "previous_board_commit": A}), encoding="utf-8")
        os.chmod(note, 0o600)
        link = tmp / "linked.json"
        link.symlink_to(note)
        rollback.os.open = lambda path, flags, *rest: (opened.append(flags), real_open(path, flags, *rest))[-1]
        try:
            out["file"] = rollback.previous_release_problem(note, current=B, commit=A)
            out["link"] = rollback.previous_release_problem(link, current=B, commit=A)
            # A link swapped in after a look at the path: the look passes, as it would have for the real file, and
            # the read is still refused, because it is made through a descriptor that does not follow links.
            real_lstat = rollback.os.lstat
            rollback.os.lstat = lambda path, *a, **k: real_lstat(note)
            try:
                out["swapped"] = rollback.previous_release_problem(link, current=B, commit=A)
            finally:
                rollback.os.lstat = real_lstat
        finally:
            rollback.os.open = real_open
        # Whose the note is (SYRD-859): no chown without root, so the opened file's fstat reports the owner. Root's
        # own (uid 0) is accepted, which shows the seam alone refuses nothing; another account's is refused.
        real_fstat = rollback.os.fstat

        def owned_by(uid: int):
            def fstat(descriptor):
                info = real_fstat(descriptor)
                return os.stat_result((*info[:4], uid, *info[5:10]))
            return fstat

        for label, uid in (("owned by root", 0), ("owned by another account", os.getuid() + 4242)):
            rollback.os.fstat = owned_by(uid)
            try:
                out[label] = rollback.previous_release_problem(note, current=B, commit=A)
            finally:
                rollback.os.fstat = real_fstat
        os.chmod(note, 0o620)
        out["group writable"] = rollback.previous_release_problem(note, current=B, commit=A)
        # A FIFO at the note's path: opening it to read used to wait for a writer before any check (Audit, SYRD-858).
        # Asked in a thread, so a hang is seen as one rather than turned into an error the reader would swallow.
        fifo = tmp / "fifo.json"
        os.mkfifo(fifo, 0o600)
        answer: list[str] = []
        asking = threading.Thread(target=lambda: answer.append(
            rollback.previous_release_problem(fifo, current=B, commit=A)), daemon=True)
        asking.start()
        asking.join(10)
        if asking.is_alive():
            out["fifo"] = "hung"
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))  # its reader is there: release it
            asking.join(5)
        else:
            out["fifo"] = answer[0]
    out["nofollow"] = bool(opened) and all(flags & os.O_NOFOLLOW for flags in opened)
    untouched("tenant_release_rollback_test note reads")
    return out


def catalogue(tree: Path) -> dict:
    sys.path[:0] = [str(tree)]
    from scripts.ticket_board import privileged_actions as actions
    from scripts.ticket_board import privileged_operations as operations

    operations.trusted_release_root = lambda commit, **_k: f"/opt/switchyard/releases/{commit}"
    values = {"project": "porter", "commit": A}
    return {name: operations.OPERATIONS[name](values) if name in operations.OPERATIONS else None
            for name in ("rollback-release", "select-shared-release")} | {
        "authentication": {a.name: a.authentication for a in actions.CATALOGUE}}


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] in ("--scenarios", "--callers", "--catalogue", "--note"):
        tree = Path(sys.argv[2])
        if sys.argv[1] == "--scenarios":
            guarded(tree)
            install_test_dispatch()
            print("RESULT " + json.dumps(scenarios(tree)))
        else:
            mode = {"--callers": callers, "--catalogue": catalogue, "--note": note_reads}[sys.argv[1]]
            print("RESULT " + json.dumps(mode(tree)))
        return 0

    def child(mode: str, tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), mode, str(tree)], text=True,
                              capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd855-before.") as raw:
        # Main's scripts, read out of the repository without writing to it (a worktree would).
        before_tree = Path(raw) / "tree"
        before_tree.mkdir()
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], capture_output=True,
                                 check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(before_tree)], input=archive, check=True)
        old_catalogue = child("--catalogue", before_tree)
        old_callers = child("--callers", before_tree)
    check(old_catalogue["rollback-release"] is None
          and old_catalogue["select-shared-release"][1:] == ["upgrade", "porter", "--source-repo",
                                                             f"/opt/switchyard/releases/{A}", "--deploy-ref", A],
          f"reproduced: main has no rollback, and select-shared-release asks the running launcher to upgrade to "
          f"another release, which it refuses for every release but its own: {old_catalogue}")
    check(old_callers["helper select-shared-release"] == {"code": 1, "reached": []}
          and old_callers["terminal select"]["ready"] is False,
          f"reproduced: outside a pane neither the helper nor the front door lets an administrator ask for it: "
          f"{old_callers}")

    new_catalogue = child("--catalogue", ROOT)
    check(new_catalogue["rollback-release"][1:] == ["deploy-release", "porter", "--commit", A, "--rollback"]
          and new_catalogue["authentication"]["rollback-release"] == "yes",
          f"rollback-release is the control pane's, pre-authorized, and asks root for the rollback: {new_catalogue}")
    check(new_catalogue["select-shared-release"][1:] == ["deploy-release", "porter", "--commit", A, "--rollback",
                                                         "--any-staged"]
          and new_catalogue["authentication"]["select-shared-release"] == "auth_admin",
          f"select-shared-release is the administrator's wider rollback: {new_catalogue}")

    new_callers = child("--callers", ROOT)
    check(new_callers["helper select-shared-release"] == {"code": 0, "reached": ["select-shared-release"]},
          f"the helper runs select-shared-release for an administrator outside any pane: {new_callers}")
    check(new_callers["helper rollback-release"] == {"code": 1, "reached": []},
          f"a pre-authorized action still needs the control pane: {new_callers}")
    check(new_callers["terminal select"] == {"ready": True, "problems": []},
          f"from a terminal, the front door leaves polkit's prompt to the administrator there: {new_callers}")
    check(not new_callers["no terminal select"]["ready"]
          and "no terminal" in " ".join(new_callers["no terminal select"]["problems"]),
          f"with no terminal to ask on, it says so instead of waiting: {new_callers}")
    check(not new_callers["terminal rollback"]["ready"]
          and "cannot establish which pane" in " ".join(new_callers["terminal rollback"]["problems"]),
          f"rollback-release from outside a pane is refused before privilege: {new_callers}")

    now = child("--scenarios", ROOT)
    ok = now["rolled back"]
    check(ok["code"] == 0 and ok["board"] == A and ok["build"] == A and ok["listener"] == "active"
          and ok["release"] == "done",
          f"rolled back: the board serves A, the board reports A, the listener runs, the phase is closed: {ok}")
    check(ok["pin"]["deploy_ref"] == A and ok["pin"]["source_repo"] == f"{ok['releases']}/{A}"
          and ok["pin"]["commit_git_dir"] == "/data/git/cache",
          f"root's pin names A's installed release, and keeps the commit cache: {ok['pin']}")
    check(ok["restaged"] == [f"tooling from {A}"]
          and ok["steps"][-2:] == ["restage tooling", "refresh hooks"],
          f"the role tooling and hooks are restaged from A, after the board serves it: {ok['steps']}")
    check(ok["steps"][:4] == ["read applied migrations", "ask NOT ticket_board.research_declared()",
                              "ask ticket_board.plan_policy() IS NULL", "postgres probe"]
          and "migrate as postgres" in ok["steps"] and "deploy-restart" in ok["steps"]
          and ok["steps"].index("stop listener") < ok["steps"].index("deploy-restart")
          < ok["steps"].index("start listener"),
          f"the schema is checked first, then the deploy runs as any deploy does: {ok['steps']}")
    check(all(f"{name}" in ok["said"] for name in NEWER) and "stay applied" in ok["said"]
          and "rolled back to" in ok["said"],
          f"it names the migrations that stay applied, and says it is rolled back: {ok['said'][-800:]}")
    attempt = ok["attempts"][0]
    check(attempt["command"][1:] == ["deploy-release", "porter", "--commit", A, "--rollback"]
          and attempt["closed"]["status"] == "succeeded" and attempt["job"]["action"] == "rollback-release"
          and attempt["target_commit"] == A,
          f"through the helper's own dispatch, journalled as the rollback: {attempt}")

    admin = now["an administrator selects any staged release"]
    check(admin["code"] == 0 and admin["board"] == A and admin["release"] == "done"
          and admin["attempts"][0]["command"][-1] == "--any-staged",
          f"select-shared-release takes a staged release the last upgrade did not replace: {admin}")

    unchanged = {"board": B, "release": "done"}
    for name, said in (
        ("not the release the last upgrade replaced", f"replaced by {B}"),
        ("not staged for the tenant", "never had"),
        ("already on it", "already serves"),
        ("a migration nobody declared", "pgu9999_unchecked.sql is not declared"),
        ("the research stage is declared", "only while the research stage is not declared, and it is declared"),
        ("a condition cannot be read", "could not be read from the database"),
    ):
        result = now[name]
        check(result["code"] == 1 and {k: result[k] for k in unchanged} == unchanged
              and result["pin"]["deploy_ref"] == B and not result["restaged"]
              and "deploy-restart" not in result["steps"] and "stop listener" not in result["steps"]
              and said in result["said"] and "nothing of porter's was rolled back" in result["said"],
              f"{name}: refused with the reason, nothing stopped, deployed, pinned or restaged: {result}")
    missing = now["root does not hold it"]
    check(missing["code"] == "refused" and "no trusted source on this host holds release" in missing["said"]
          and not missing["attempts"] and missing["board"] == B,
          f"a release root does not hold is refused before privilege, with nothing journalled: {missing}")
    failed = now["the deploy fails"]
    check(failed["code"] == 1 and failed["board"] == B and failed["pin"]["deploy_ref"] == B
          and failed["pin"]["source_repo"] == f"{failed['releases']}/{B}" and not failed["restaged"]
          and failed["listener"] == "active" and "names " + B + " again" in failed["said"],
          f"a deploy that fails leaves the board on B, puts the pin back, and restages nothing: {failed}")
    quiet = now["no migration crossed"]
    check(quiet["code"] == 0 and quiet["board"] == A and "stay applied" not in quiet["said"],
          f"with no newer migration there is nothing to declare: {quiet}")
    notes = child("--note", ROOT)
    check(notes["file"] == "", f"root's own note naming the release replaced is accepted: {notes}")
    check(notes["link"] and "holds no note" in notes["link"],
          f"a link to that same note is refused, never followed (SYRD-858): {notes}")
    check(notes["swapped"] and "holds no note" in notes["swapped"],
          f"a link swapped in after a look at the path is refused by the read itself (SYRD-858): {notes}")
    check(notes["owned by root"] == "", f"a note root owns is root's record (SYRD-859): {notes}")
    check(notes["owned by another account"] and "holds no note" in notes["owned by another account"],
          f"a note another account owns is not root's record, and is refused (SYRD-859): {notes}")
    check(notes["group writable"] and "holds no note" in notes["group writable"],
          f"a note others could write is not root's record: {notes}")
    check(notes["nofollow"] is True, f"every open of the note refuses to follow a link: {notes}")
    check(notes["fifo"] != "hung" and "holds no note" in notes["fifo"],
          f"a FIFO at the note's path is refused at once, never waited on (SYRD-858): {notes}")
    print(f"tenant_release_rollback_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
