#!/usr/bin/env python3
"""SYRD-531: the catalogued `deploy-release` deploys the prepared release and closes the phase.

On MEFP the Director ran the pinned preview (0068) and `upgrade-tenant-release`
(0069); both exited 0, the source was right, the tooling was refreshed -- and the
board stayed on 49abeb4 with the release phase `ready`, because an upgrade
prepares and leaves the board deploy owed, printed as an operator's sequence.
The Director skill says the deploy is the Director's, through `deploy-release`;
but that action ran `upgrade <p> --deploy-ref <c>`, the same preparation again.

Now `deploy-release` runs `switchyard deploy-release <p> --commit <c>` as the
helper's root: for a tenant prepared for exactly that commit, from root's
installed release of it, it stops the listener, installs root's copy of the
units, runs the tenant's own deploy-restart, starts the listener, and closes the
phase only by `release-status --close`'s re-proof against the running board.

Driven through the helper's real request parsing and dispatch
(`run_privileged_action`), whose runner hands the argv it built to the tree's
own `switchyard_main`, as root in-process; every service step goes to a
recording runner that answers as systemd and the deploy would, and the deploy
really moves the board's `current`. The board's live build is a stand-in, as in
`release_phase_journal_test`, whose tenant this reuses. The reproduction runs
main's catalogue in a child, from a git worktree.
"""

from __future__ import annotations

import json
import getpass
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "4ebb84630852b3329baaef886d3b6acd33ee02fb"  # main before SYRD-531
OLD, NEW, OTHER = "a" * 40, "e" * 40, "f" * 40
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def catalogue(tree: Path) -> dict:
    """What main's catalogue asks root to run for the two actions."""
    sys.path.insert(0, str(tree))
    from scripts.ticket_board import privileged_operations as operations

    values = {"project": "porter", "commit": NEW}
    return {name: operations.OPERATIONS[name](values) for name in ("deploy-release", "upgrade-tenant-release", "preview-upgrade")}


class Host:
    """systemd and the deploy, as the recording runner answers them."""

    def __init__(self, tenant, board, unit_dir: Path, trusted_release: Path,
                 *, stop="ok", units="ok", deploy="ok", start="ok", restart="ok", database="ok", postgres="ok",
                 smoke="ok", holds="ok", listener="active") -> None:
        self.tenant, self.board = tenant, board
        self.unit_dir = unit_dir
        self.trusted_release = trusted_release
        self.outcome = {"stop": stop, "units": units, "deploy": deploy, "start": start, "restart": restart,
                        "database": database, "postgres": postgres, "smoke": smoke, "holds": holds}
        self.steps: list[str] = []
        # SYRD-700: whether root's template chain installed a staged (legacy) board or canary itself.
        self.legacy_installed = False
        self.listener = listener
        # SYRD-854: the listener unit as `systemctl --user show` reports it to the watch after the switch.
        self.restarts, self.pid = 0, 4100

    def move_board(self, commit: str) -> None:
        release = self.tenant.board_root / "releases" / commit
        release.mkdir(parents=True, exist_ok=True)
        (release / ".pgu-deploy-sha").write_text(commit + "\n", encoding="utf-8")
        current = self.tenant.board_root / "current"
        current.unlink()
        current.symlink_to(release)

    def __call__(self, argv, **_kwargs):
        text = " ".join(str(part) for part in argv)
        result = lambda code, out="": SimpleNamespace(returncode=code, stdout=out, stderr="" if code == 0 else "simulated failure")
        if "systemctl --user show" in text:
            # SYRD-854: a listener that crashes on the new release restarts between every two reads.
            if not self.steps or self.steps[-1] != "watch listener":
                self.steps.append("watch listener")
            on_new = (self.tenant.board_root / "current").resolve().name == NEW
            crashing = self.outcome["holds"] == "crash everywhere" or (self.outcome["holds"] == "crash" and on_new)
            if self.listener == "active" and crashing:
                self.restarts, self.pid = self.restarts + 1, self.pid + 1
                return result(0, f"ActiveState=activating\nSubState=auto-restart\nNRestarts={self.restarts}\nMainPID=0\n")
            state = "ActiveState=active\nSubState=running" if self.listener == "active" else "ActiveState=inactive\nSubState=dead"
            return result(0, f"{state}\nNRestarts={self.restarts}\nMainPID={self.pid}\n")
        if argv[0] == "runuser" and "SELECT name FROM ticket_board.schema_migrations" in text:
            # SYRD-854 with SYRD-855's rule: the migrations the database applied are the new release's.
            self.steps.append("read applied migrations")
            applied = sorted(path.name for path in (self.trusted_release / "scripts/ticket_board/migrations").iterdir())
            return result(0, "\n".join(applied) + "\n")
        if argv[0] == "runuser" and "ticket_board.workflow_configuration" in text:
            # SYRD-854: the smoke reads the tenant's declared workflow as the cluster's account, and writes nothing.
            assert argv[1:4] == ["-u", "postgres", "--"] and argv[4:6] == ["env", "-i"] and _kwargs.get("cwd") == "/"
            self.steps.append("read declared workflow")
            return result(0, json.dumps({"project": "porter", "roles": [], "stages": []}))
        if argv[0] == "runuser" and "ticket_board.workflow_stages" in text:
            # SYRD-866: and its live board's stages, the same way; part of that read.
            assert argv[1:4] == ["-u", "postgres", "--"] and argv[4:6] == ["env", "-i"] and _kwargs.get("cwd") == "/"
            return result(0, "")
        if argv[0] == "runuser" and "release_smoke.py" in text:
            # SYRD-854: the release smoke, as the tenant owner, against root's installed release of the commit.
            assert argv[1:4] == ["-u", getpass.getuser(), "--"] and str(self.trusted_release) in argv, argv
            self.steps.append("release smoke")
            if self.outcome["smoke"] == "ok":
                return result(0, "release smoke: every module imports\nrelease smoke: READY\n")
            return result(1, "release smoke: NOT READY\nticket_board/publisher_stage.py:254 imports "
                             "scripts.ticket_board.research_stage, and 'scripts' does not import in the release layout\n")
        if "systemctl --user is-active" in text:
            return result(0 if self.listener == "active" else 3, self.listener)
        if text.startswith("systemctl is-active ") and text.endswith("-ticket-board.service"):
            # SYRD-577: the board's state is read first, so the deploy can leave it
            # as it was; these scenarios are a running tenant's.
            return result(0, "active")
        if "systemctl --user stop" in text:
            self.steps.append("stop listener")
            if self.outcome["stop"] == "ok":
                self.listener = "inactive"
                return result(0)
            return result(1)
        if "systemctl --user daemon-reload && systemctl --user restart" in text:
            self.steps.append("start listener")
            if self.outcome["start"] == "ok":
                self.listener = "active"
                return result(0)
            return result(1)
        if "sudo install" in text:
            self.steps.append("install templates")
            self.legacy_installed = self.legacy_installed or "/etc/systemd/system/porter-ticket-board" in text
            return result(0 if self.outcome["units"] == "ok" else 1)
        if argv[0] == "install":
            source, target = Path(argv[-2]), Path(argv[-1])
            restoring = source.read_text(encoding="utf-8").startswith("old ")
            shutil.copyfile(source, target)
            step = "restore units" if restoring else "install exact units"
            if step not in self.steps:
                self.steps.append(step)
            return result(0)
        if argv[:2] == ["systemctl", "daemon-reload"]:
            return result(0)
        if argv[:2] == ["systemctl", "restart"]:
            if self.steps and self.steps[-1] == "restore units":
                self.steps.append("restart restored board")
                return result(0)
            self.steps.append("restart board")
            return result(0 if self.outcome["restart"] == "ok" else 1)
        if argv[:4] == ["runuser", "-u", "postgres", "--"] and argv[-2:] == ["-c", "DO $$ BEGIN NULL; END $$;"]:
            # SYRD-826: the deploy asks first whether a fresh connection runs plpgsql, as the cluster's account.
            assert argv[4:6] == ["env", "-i"] and _kwargs.get("cwd") == "/", argv
            self.steps.append("postgres probe")
            return result(0 if self.outcome["postgres"] == "ok" else 3)
        if argv[:4] == ["runuser", "-u", "postgres", "--"]:
            # SYRD-706: root's own database work, as the cluster's account, from root's trusted release only.
            assert argv[4:6] == ["env", "-i"] and _kwargs.get("cwd") == "/", argv
            assert any(part.startswith(str(self.trusted_release) + "/") for part in argv), argv
            self.steps.append("migrate as postgres" if argv[-1].endswith("ticket-board-migrate") else "rbac as postgres")
            return result(0 if self.outcome["database"] == "ok" else 1)
        if "deploy-restart" in text:
            words = shlex.split(argv[-1])
            # SYRD-706: the tenant's deploy skips the database work root has done.
            assert "TICKET_BOARD_SKIP_MIGRATIONS=1" in words, words
            proof_values = [word.split("=", 1)[1] for word in words
                            if word.startswith("TICKET_BOARD_PROVISIONED_SYSTEM_UNIT=")]
            board_unit = self.unit_dir / "porter-ticket-board.service"
            canary_unit = self.unit_dir / "porter-ticket-board-canary.service"
            assert len(proof_values) == 1 and Path(proof_values[0]).read_bytes() == board_unit.read_bytes()
            assert str(self.trusted_release) in board_unit.read_text(encoding="utf-8")
            assert str(self.trusted_release) in canary_unit.read_text(encoding="utf-8")
            self.steps.extend(("canary exact candidate", "deploy-restart exact candidate"))
            outcome = self.outcome["deploy"]
            if outcome in ("ok", "moved", "wrong build"):
                self.move_board(NEW)
                self.board.build_id = OTHER if outcome == "wrong build" else NEW
            self.steps.append("health passed" if outcome in ("ok", "wrong build") else "health failed")
            return result(0 if outcome in ("ok", "wrong build") else 1)
        self.steps.append("other: " + text[:80])
        return result(0)


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import release_phase_journal_test as journal
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_helper as helper
    from scripts.ticket_board import privileged_operations as operations
    from scripts.ticket_board import board_identity
    from scripts.ticket_board.project_provision import build_plan, render_board_unit, render_canary_unit
    from scripts import tenant_release_deploy

    seen: dict = {}

    def run(name: str, *, deployed: str = OLD, pin: str | None = NEW, root: bool = True,
            action: str = "deploy-release", installed: tuple[str, ...] = (NEW, OLD), exact_installed: bool = False,
            runtime: bool = True, installed_canary: str = "old", previous: tuple[str, ...] = ("pgu1_fixture.sql",),
            added: tuple[str, ...] = (), **outcome) -> None:
        with tempfile.TemporaryDirectory(prefix="syrd531.") as raw:
            tmp = Path(raw)
            tenant = journal.Tenant(tmp, deployed=deployed)
            releases = tmp / "opt-switchyard" / "releases"
            releases.mkdir(parents=True, exist_ok=True)
            for commit in installed:
                release = releases / commit
                (release / "scripts").mkdir(parents=True, exist_ok=True)
                (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}), encoding="utf-8")
                (release / "scripts" / "ticket-board-service.sh").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                # SYRD-706: what root migrates and grants from, as the cluster's account.
                (release / "scripts" / "ticket_board" / "migrations").mkdir(parents=True, exist_ok=True)
                (release / "scripts" / "ticket_board" / "migrations" / "pgu1_fixture.sql").write_text("SELECT 1;\n")
                for migration in added if commit == NEW else ():  # SYRD-854: migrations the release applies
                    (release / "scripts" / "ticket_board" / "migrations" / migration).write_text("SELECT 1;\n")
                (release / "scripts" / "ticket_board" / "rbac.sql").write_text("SELECT 1;\n", encoding="utf-8")
                (release / "scripts" / "ticket-board-migrate").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                # SYRD-700: a release new enough to run under its exact unit carries the renderer.
                if runtime or commit != NEW:
                    (release / "scripts" / "ticket_board").mkdir(exist_ok=True)
                    (release / "scripts" / "ticket_board" / "release_units.py").write_text("# renderer\n",
                                                                                            encoding="utf-8")
            # SYRD-854: the migrations the release the board serves now carries, for the revert's schema rule.
            served = (tenant.board_root / "current").resolve() / "scripts" / "ticket_board" / "migrations"
            served.mkdir(parents=True, exist_ok=True)
            # The tenant's own copy even claims what the new release added: root decides from its own release.
            for migration in (*previous, *added):
                (served / migration).write_text("SELECT 1;\n")
            units = launcher.privileged_provision_dir("porter", root=launcher.switchyard_privileged_provision_root())
            units.mkdir(parents=True, exist_ok=True)
            plan = build_plan(
                project="porter", owner_user=getpass.getuser(), owner_home=tmp / "tenant",
                board_root=tenant.board_root, port=29117, commit_git_dir=tmp / "cache.git",
                asset_dir=tmp / "assets", frame_dir=tmp / "frames",
            )
            (units / "porter-ticket-board.service").write_text(render_board_unit(plan), encoding="utf-8")
            (units / "porter-ticket-board-canary.service").write_text(render_canary_unit(plan), encoding="utf-8")
            (units / "porter-ticket-board-notify-listener.service").write_text("[Unit]\n", encoding="utf-8")
            unit_dir = tmp / "systemd"
            unit_dir.mkdir()
            (unit_dir / "porter-ticket-board.service").write_text("old board unit\n", encoding="utf-8")
            def exact(release: str, unit: str) -> bytes:
                rendered = tenant_release_deploy._candidate_units(
                    SimpleNamespace(provisioned_system_unit=units / "porter-ticket-board.service"), "porter",
                    releases / release, 26991)
                return next(v for k, v in rendered.items() if k.name == unit)

            if exact_installed:
                # SYRD-700: root's exact render of NEW is what is installed.
                (unit_dir / "porter-ticket-board.service").write_bytes(exact(NEW, "porter-ticket-board.service"))
            # The canary as a host may hold it: legacy, root's render of NEW, or of another release (SYRD-700).
            (unit_dir / "porter-ticket-board-canary.service").write_bytes(
                {"old": b"old canary unit\n", "exact": None, "other release": None}[installed_canary]
                or exact(NEW if installed_canary == "exact" else OLD, "porter-ticket-board-canary.service"))
            if pin:
                path = launcher.privileged_upgrade_source_path(tenant.config)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"schema": launcher.UPGRADE_SOURCE_SCHEMA, "project": "porter",
                                            "source_repo": str(releases / pin), "commit_git_dir": "/data/git/cache",
                                            "deploy_ref": pin}), encoding="utf-8")
                os.chmod(path, 0o600)
            real_euid = launcher.os.geteuid
            launcher.os.geteuid = lambda: 0
            try:
                # Where 0069 left MEFP: prepared, the phase root's and `ready`.
                launcher.record_upgrade_phase(tenant.config, config_path=tenant.config_path, phase="release",
                                              state="ready", detail="prepared")
            finally:
                launcher.os.geteuid = real_euid
            board = journal.FakeBoard(deployed)
            host = Host(tenant, board, unit_dir, releases / NEW, **outcome)
            saved = (launcher._resolve_switchyard_project, launcher._open_board_url, launcher.os.geteuid,
                     launcher.subprocess.run, operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID,
                     tenant_release_deploy.SYSTEM_UNIT_DIR, tenant_release_deploy.TRUSTED_UNIT_OWNER_UID,
                     board_identity.effective_unit, board_identity.board_opener)
            # As root the close asks the board the installed unit names (SYRD-769); this host has no such unit, so
            # it asks the stand-in board, as every other read here does.
            board_identity.board_opener = lambda _config, _opener=None: None
            launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(slug="porter", config_path=tenant.config_path)
            launcher._open_board_url = board
            launcher.os.geteuid = (lambda: 0) if root else (lambda: 1006)
            operations.SHARED_RELEASES = str(releases)
            operations.TRUSTED_OWNER_UID = os.getuid()
            tenant_release_deploy.SYSTEM_UNIT_DIR = unit_dir
            tenant_release_deploy.TRUSTED_UNIT_OWNER_UID = os.getuid()
            def attest(*_args, **_kwargs):
                if board.build_id != NEW:
                    raise board_identity.BoardIdentityError(
                        f"board reports {board.build_id}, not root release {NEW}"
                    )
                return SimpleNamespace(release_commit=NEW)

            board_identity.effective_unit = attest
            # Anything the command runs without being handed a runner is a
            # boundary this test did not model: refuse it loudly.
            launcher.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"unmodelled subprocess: {a[:1]}"))
            printed: list[str] = []
            attempts: list[dict] = []

            class Attempt:
                attempt = "0001"

                def __init__(self, project, command, **kwargs):
                    attempts.append({"project": project, "command": list(command), **kwargs})

                def open(self):
                    return tmp / "rollout"

                def close(self, **kwargs):
                    attempts[-1]["closed"] = kwargs

            def dispatch(command, *, timeout):
                if command[:2] != [operations.LAUNCHER, "deploy-release"]:
                    return SimpleNamespace(returncode=f"not dispatched: {command}")
                return SimpleNamespace(returncode=launcher.switchyard_main_for_test(command[1:], host, printed))

            try:
                parsed_action, values = helper.parse_request([action, "project=porter", f"commit={NEW}"])
                code = helper.run_privileged_action(parsed_action, values, project="porter", attempt_factory=Attempt,
                                                   runner=dispatch, print_func=printed.append)
            finally:
                (launcher._resolve_switchyard_project, launcher._open_board_url, launcher.os.geteuid,
                 launcher.subprocess.run, operations.SHARED_RELEASES, operations.TRUSTED_OWNER_UID,
                 tenant_release_deploy.SYSTEM_UNIT_DIR, tenant_release_deploy.TRUSTED_UNIT_OWNER_UID,
                 board_identity.effective_unit, board_identity.board_opener) = saved
            seen[name] = {
                "code": code, "steps": host.steps, "said": "\n".join(printed),
                "board": launcher._current_tenant_release(tenant.board_root)[1],
                "listener": host.listener,
                "release": launcher.upgrade_phase_state(tenant.trusted(), "release"),
                "attempts": attempts,
                "board_unit": (unit_dir / "porter-ticket-board.service").read_text(encoding="utf-8"),
                "legacy_installed": host.legacy_installed,
                "canary_unit": (unit_dir / "porter-ticket-board-canary.service").read_text(encoding="utf-8"),
            }

    run("unprepared", pin=OLD)
    run("deployed", )
    run("deploy fails, board unchanged", deploy="fail")
    run("deploy fails, board moved", deploy="moved")
    run("listener does not come back", start="fail")
    run("live board serves another build", deploy="wrong build")
    run("already deployed", deployed=NEW, exact_installed=True, installed_canary="exact")
    run("exact board, legacy canary", deployed=NEW, exact_installed=True)
    run("exact board, another release's canary", deployed=NEW, exact_installed=True, installed_canary="other release")
    run("already named, legacy unit", deployed=NEW)
    run("already named, legacy unit, restart fails", deployed=NEW, restart="fail")
    run("not root", root=False)
    run("release predates exact units", runtime=False)
    run("prepared release not installed", installed=(OLD,))
    run("database work fails", database="fail")
    # SYRD-854: the smoke before anything live changes, and the listener watched after the switch.
    run("release smoke not ready", smoke="fail")
    run("listener crash-loops on the release", holds="crash")
    run("listener crash-loops, the release migrated", holds="crash", added=("pgu2_new.sql",))
    run("listener crash-loops on both releases", holds="crash everywhere")
    run("listener was crash-looping before the deploy", listener="activating")
    return seen


def install_test_dispatch() -> None:
    """`switchyard_main` with the host's runner handed to the deploy, the one seam."""
    from scripts import team_launcher as launcher
    from scripts import tenant_release_deploy

    def main_for_test(argv, host, printed):
        original = tenant_release_deploy.switchyard_deploy_release_command

        def with_host(project, *, commit, **kwargs):
            now = [0.0]  # SYRD-854: the listener watch's clock, advanced only by its own sleeps
            return original(project, commit=commit, runner=host, print_func=printed.append, clock=lambda: now[0],
                            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), **kwargs)

        launcher.switchyard_deploy_release_command = with_host
        try:
            return launcher.switchyard_main(list(argv))
        finally:
            launcher.switchyard_deploy_release_command = original

    launcher.switchyard_main_for_test = main_for_test


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] in ("--catalogue", "--scenarios"):
        tree = Path(sys.argv[2])
        if sys.argv[1] == "--catalogue":
            print("RESULT " + json.dumps(catalogue(tree)))
        else:
            sys.path[:0] = [str(tree), str(tree / "tests")]
            install_test_dispatch()
            print("RESULT " + json.dumps(scenarios(tree)))
        return 0

    def child(mode: str, tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), mode, str(tree)], text=True,
                              capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd531-before.") as raw:
        # The commit's files alone: a sweep's repository is read-only, so no worktree can be added to it.
        before_tree = Path(raw) / "tree"
        before_tree.mkdir()
        subprocess.run(f"git -C {shlex.quote(str(ROOT))} archive {BEFORE} scripts | tar -C {shlex.quote(str(before_tree))} -x",
                       shell=True, check=True)
        before = child("--catalogue", before_tree)
    check(before["deploy-release"] == before["upgrade-tenant-release"]
          and before["deploy-release"][1:] == ["upgrade", "porter", "--deploy-ref", NEW],
          f"reproduced: main's `deploy-release` asks root for the same preparation as `upgrade-tenant-release`: {before}")

    after = child("--catalogue", ROOT)
    check(after["deploy-release"][1:] == ["deploy-release", "porter", "--commit", NEW]
          and after["upgrade-tenant-release"] == before["upgrade-tenant-release"] and after["preview-upgrade"] == before["preview-upgrade"],
          f"now it asks for the deploy; preparation and the pinned preview are unchanged: {after}")

    now = child("--scenarios", ROOT)
    ok = now["deployed"]
    check(ok["code"] == 0 and ok["steps"] == [
              "postgres probe", "read declared workflow", "release smoke", "stop listener", "install templates",
              "install exact units", "migrate as postgres", "rbac as postgres", "canary exact candidate",
              "deploy-restart exact candidate", "health passed", "start listener", "watch listener"],
          f"a prepared tenant: the release smoke, listener down, root's units, the deploy, listener up and watched -- in "
          f"that order: {ok['steps']} {ok['said'][-600:]}")
    check("release smoke: READY" in ok["said"] and "REVERTED" not in ok["said"],
          f"SYRD-854: a healthy deploy says the smoke passed and reverts nothing: {ok['said'][-800:]}")
    check(ok["board"] == NEW and ok["release"] == "done" and ok["listener"] == "active",
          f"the board serves the release and the phase is closed against it: {ok}")
    check(NEW in ok["board_unit"],
          f"the legitimate deployment leaves the exact root release selected: {ok['board_unit']}")
    check(ok["attempts"][0]["command"][1:] == ["deploy-release", "porter", "--commit", NEW]
          and ok["attempts"][0]["closed"]["status"] == "succeeded",
          f"through the helper's own dispatch, recorded as an attempt: {ok['attempts']}")
    check("live board build" in ok["said"] and "Panes are not restarted" in ok["said"], f"and says what it proved: {ok['said'][-500:]}")
    for name in ("deployed", "live board serves another build", "already deployed", "already named, legacy unit"):
        said = now[name]["said"]
        check("Nothing was deployed, restarted or rolled back" not in said and "Close it with" not in said,
              f"{name}: the close does not claim a deploy it did not see, or ask to be run: {said[-400:]}")

    unprepared = now["unprepared"]
    check(unprepared["code"] == 1 and unprepared["steps"] == [] and unprepared["board"] == OLD and unprepared["release"] == "ready"
          and f"was prepared for {OLD}, not {NEW}" in unprepared["said"],
          f"preparation is not activation: an unprepared tenant is refused before anything runs: {unprepared}")
    failed = now["deploy fails, board unchanged"]
    check(failed["code"] == 1 and failed["steps"] == [
              "postgres probe", "read declared workflow", "release smoke", "stop listener", "install templates", "install exact units", "migrate as postgres", "rbac as postgres", "canary exact candidate",
              "deploy-restart exact candidate",
              "health failed", "restore units", "restart restored board", "start listener"]
          and failed["board"] == OLD and failed["listener"] == "active" and failed["release"] == "blocked",
          f"a failed deploy that left the board: the listener comes back, the phase is blocked: {failed}")
    check(failed["board_unit"] == "old board unit\n",
          f"failed health restores the captured board unit before the listener: {failed}")
    database = now["database work fails"]
    check(database["code"] == 1 and database["steps"] == [
              "postgres probe", "read declared workflow", "release smoke", "stop listener", "install templates", "install exact units", "migrate as postgres", "restore units",
              "restart restored board", "start listener"]
          and database["board"] == OLD and database["listener"] == "active" and database["release"] == "blocked"
          and "the database work for" in database["said"] and "as postgres failed" in database["said"],
          f"SYRD-706: root's migration fails: no tenant deploy runs, the units come back, the phase is blocked: {database}")
    moved = now["deploy fails, board moved"]
    check(moved["code"] == 1 and moved["steps"] == [
              "postgres probe", "read declared workflow", "release smoke", "stop listener", "install templates", "install exact units", "migrate as postgres", "rbac as postgres", "canary exact candidate",
              "deploy-restart exact candidate",
              "health failed", "restore units", "restart restored board"]
          and moved["listener"] == "inactive" and moved["release"] == "blocked" and "left stopped" in moved["said"],
          f"a failed deploy that moved the board: the listener is left down, said so: {moved}")
    down = now["listener does not come back"]
    check(down["code"] == 1 and down["release"] == "blocked" and "listener is not running" in down["said"],
          f"deployed but the listener is not back: not closed: {down}")
    wrong = now["live board serves another build"]
    check(wrong["code"] == 3 and wrong["release"] == "blocked" and wrong["board"] == OLD
          and wrong["board_unit"] == "old board unit\n",
          f"another live build restores selector and units and reports rolled back: {wrong}")
    already = now["already deployed"]
    check(already["code"] == 0 and already["steps"] == ["postgres probe"] and already["release"] == "done",
          f"a board already serving the release is only closed, nothing restarted: {already}")
    # SYRD-700: no path installs root's staged (legacy-shaped) board or canary itself; they are templates.
    check(not any(result["legacy_installed"] for result in now.values()),
          f"the template chain never installs a staged board or canary: "
          f"{[n for n, r in now.items() if r['legacy_installed']]}")
    converged = now["already named, legacy unit"]
    check(converged["code"] == 0 and converged["steps"] == [
              "postgres probe", "stop listener", "install templates", "install exact units", "restart board", "start listener",
              "watch listener"]
          and converged["release"] == "done" and NEW in converged["board_unit"]
          and "-E -s -B" in converged["board_unit"] and "EnvironmentFile=" not in converged["board_unit"],
          f"a tenant already naming the release under a legacy unit converges on root's exact unit: no deploy, "
          f"a restart under it, the phase closed: {converged}")
    # Audit's reproduction: an exact board is not proof on its own; the canary must be root's render of the same
    # release too, or both converge.
    for name in ("exact board, legacy canary", "exact board, another release's canary"):
        both = now[name]
        check(both["code"] == 0 and both["steps"] == [
                  "postgres probe", "stop listener", "install templates", "install exact units", "restart board", "start listener",
                  "watch listener"]
              and both["release"] == "done" and f"releases/{NEW}/scripts/ticket-board.py" in both["canary_unit"]
              and f"releases/{OLD}" not in both["canary_unit"] and "/bin/sh" not in both["canary_unit"],
              f"{name}: both units converge on root's render of the release: {both}")
    unconverged = now["already named, legacy unit, restart fails"]
    check(unconverged["code"] == 3 and unconverged["steps"] == [
              "postgres probe", "stop listener", "install templates", "install exact units", "restart board", "restore units",
              "restart restored board", "start listener"]
          and unconverged["board_unit"] == "old board unit\n" and unconverged["release"] == "blocked",
          f"a board that will not serve under its exact unit gets its previous unit back, rolled back: {unconverged}")
    old_runtime = now["release predates exact units"]
    check(old_runtime["code"] == 1 and old_runtime["steps"] == [] and old_runtime["release"] == "ready"
          and "predates exact root-release board units" in old_runtime["said"]
          and old_runtime["board_unit"] == "old board unit\n",
          f"a release that cannot run under its exact unit is refused before anything runs: {old_runtime}")
    missing = now["prepared release not installed"]
    check(missing["code"] == 1 and missing["steps"] == [] and missing["release"] == "ready"
          and f"root holds no installed release of {NEW}" in missing["said"],
          f"a pin whose release root does not hold is refused before anything runs: {missing}")
    unprivileged = now["not root"]
    check(unprivileged["code"] == 1 and unprivileged["steps"] == [] and "Nothing was deployed" in unprivileged["said"],
          f"not root: nothing: {unprivileged}")
    # SYRD-854: the release smoke refuses before anything live changes.
    smoked = now["release smoke not ready"]
    check(smoked["code"] == 1 and smoked["steps"] == ["postgres probe", "read declared workflow", "release smoke"]
          and smoked["board"] == OLD and smoked["listener"] == "active" and smoked["release"] == "blocked"
          and smoked["board_unit"] == "old board unit\n"
          and "NOT READY" in smoked["said"] and "'scripts' does not import in the release layout" in smoked["said"]
          and "nothing was deployed: the board, its database and the listener were not touched" in smoked["said"],
          f"a release the smoke finds NOT READY: no listener stop, no migration, no unit, the reason recorded: {smoked}")
    # SYRD-854: a listener that does not hold on the new release.
    deploy = ["postgres probe", "read declared workflow", "release smoke", "stop listener", "install templates",
              "install exact units", "migrate as postgres", "rbac as postgres", "canary exact candidate",
              "deploy-restart exact candidate", "health passed", "start listener", "watch listener"]
    reverted = now["listener crash-loops on the release"]
    check(reverted["code"] == 3 and reverted["steps"] == deploy + ["read applied migrations", "restore units",
                                                                   "restart restored board", "start listener",
                                                                   "watch listener"]
          and reverted["board"] == OLD and reverted["board_unit"] == "old board unit\n"
          and reverted["listener"] == "active" and reverted["release"] == "blocked"
          and "REVERTED" in reverted["said"] and "did not hold" in reverted["said"]
          and f"on {OLD} it holds" in reverted["said"] and "ends as it began" in reverted["said"],
          f"a listener crash-looping on the release: board and listener back on the previous release, which holds, "
          f"rolled back, the phase blocked: {reverted}")
    kept = now["listener crash-loops, the release migrated"]
    check(kept["code"] == 1 and kept["steps"] == deploy + ["read applied migrations", "stop listener"]
          and kept["board"] == NEW and kept["listener"] == "inactive" and kept["release"] == "blocked"
          and "NOT REVERTED" in kept["said"]
          and "pgu2_new.sql is not declared safe for older code" in kept["said"],
          f"where the release applied a migration not declared safe for the previous release's code (the rollback's "
          f"rule): nothing reverted, the loop stopped, the phase blocked with the migration named: {kept}")
    both = now["listener crash-loops on both releases"]
    check(both["code"] == 3 and both["board"] == OLD and both["release"] == "blocked"
          and f"on {OLD} it does not hold either" in both["said"] and "did NOT end as it began" in both["said"],
          f"a previous release that does not hold either is said, loudly, to need an operator: {both['said'][-900:]}")
    looping = now["listener was crash-looping before the deploy"]
    check(looping["code"] == 0 and looping["steps"] == deploy and looping["listener"] == "active"
          and looping["release"] == "done" and "restarting; the deploy starts it after the switch" in looping["said"],
          f"a listener restarting when the deploy began is started after the switch and watched, not left stopped: "
          f"{looping}")
    for name, result in now.items():
        expected = "succeeded" if result["code"] == 0 else "rolled_back" if result["code"] == 3 else "failed"
        check(result["attempts"] and result["attempts"][0]["closed"]["status"] == expected,
              f"{name}: the helper's attempt record tells the truth: {result['attempts']}")
    print(f"catalogued_deploy_release_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
