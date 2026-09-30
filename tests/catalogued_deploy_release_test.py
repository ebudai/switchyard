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
import os
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

    def __init__(self, tenant, board, *, stop="ok", units="ok", deploy="ok", start="ok") -> None:
        self.tenant, self.board = tenant, board
        self.outcome = {"stop": stop, "units": units, "deploy": deploy, "start": start}
        self.steps: list[str] = []
        self.listener = "active"

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
        if "systemctl --user is-active" in text:
            return result(0 if self.listener == "active" else 3, self.listener)
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
            self.steps.append("install units")
            return result(0 if self.outcome["units"] == "ok" else 1)
        if "deploy-restart" in text:
            self.steps.append("deploy-restart")
            outcome = self.outcome["deploy"]
            if outcome in ("ok", "moved", "wrong build"):
                self.move_board(NEW)
                self.board.build_id = OTHER if outcome == "wrong build" else NEW
            return result(0 if outcome in ("ok", "wrong build") else 1)
        self.steps.append("other: " + text[:80])
        return result(0)


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import release_phase_journal_test as journal
    from scripts import team_launcher as launcher
    from scripts.ticket_board import privileged_helper as helper
    from scripts.ticket_board import privileged_operations as operations

    seen: dict = {}

    def run(name: str, *, deployed: str = OLD, pin: str | None = NEW, root: bool = True,
            action: str = "deploy-release", installed: tuple[str, ...] = (NEW, OLD), **outcome) -> None:
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
            units = launcher.privileged_provision_dir("porter", root=launcher.switchyard_privileged_provision_root())
            units.mkdir(parents=True, exist_ok=True)
            for unit in ("ticket-board", "ticket-board-canary", "ticket-board-notify-listener"):
                (units / f"porter-{unit}.service").write_text("[Unit]\n", encoding="utf-8")
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
            host = Host(tenant, board, **outcome)
            saved = (launcher._resolve_switchyard_project, launcher._open_board_url, launcher.os.geteuid,
                     launcher.subprocess.run)
            launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(slug="porter", config_path=tenant.config_path)
            launcher._open_board_url = board
            launcher.os.geteuid = (lambda: 0) if root else (lambda: 1006)
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
                 launcher.subprocess.run) = saved
            seen[name] = {
                "code": code, "steps": host.steps, "said": "\n".join(printed),
                "board": launcher._current_tenant_release(tenant.board_root)[1],
                "listener": host.listener,
                "release": launcher.upgrade_phase_state(tenant.trusted(), "release"),
                "attempts": attempts,
            }

    run("unprepared", pin=OLD)
    run("deployed", )
    run("deploy fails, board unchanged", deploy="fail")
    run("deploy fails, board moved", deploy="moved")
    run("listener does not come back", start="fail")
    run("live board serves another build", deploy="wrong build")
    run("already deployed", deployed=NEW)
    run("not root", root=False)
    run("prepared release not installed", installed=(OLD,))
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
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), BEFORE], check=True)
        try:
            before = child("--catalogue", before_tree)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
    check(before["deploy-release"] == before["upgrade-tenant-release"]
          and before["deploy-release"][1:] == ["upgrade", "porter", "--deploy-ref", NEW],
          f"reproduced: main's `deploy-release` asks root for the same preparation as `upgrade-tenant-release`: {before}")

    after = child("--catalogue", ROOT)
    check(after["deploy-release"][1:] == ["deploy-release", "porter", "--commit", NEW]
          and after["upgrade-tenant-release"] == before["upgrade-tenant-release"] and after["preview-upgrade"] == before["preview-upgrade"],
          f"now it asks for the deploy; preparation and the pinned preview are unchanged: {after}")

    now = child("--scenarios", ROOT)
    ok = now["deployed"]
    check(ok["code"] == 0 and ok["steps"] == ["stop listener", "install units", "deploy-restart", "start listener"],
          f"a prepared tenant: listener down, root's units, the deploy, listener up -- in that order: {ok['steps']} {ok['said'][-600:]}")
    check(ok["board"] == NEW and ok["release"] == "done" and ok["listener"] == "active",
          f"the board serves the release and the phase is closed against it: {ok}")
    check(ok["attempts"][0]["command"][1:] == ["deploy-release", "porter", "--commit", NEW]
          and ok["attempts"][0]["closed"]["status"] == "succeeded",
          f"through the helper's own dispatch, recorded as an attempt: {ok['attempts']}")
    check("live board build" in ok["said"] and "Panes are not restarted" in ok["said"], f"and says what it proved: {ok['said'][-500:]}")
    for name in ("deployed", "live board serves another build", "already deployed"):
        said = now[name]["said"]
        check("Nothing was deployed, restarted or rolled back" not in said and "Close it with" not in said,
              f"{name}: the close does not claim a deploy it did not see, or ask to be run: {said[-400:]}")

    unprepared = now["unprepared"]
    check(unprepared["code"] == 1 and unprepared["steps"] == [] and unprepared["board"] == OLD and unprepared["release"] == "ready"
          and f"was prepared for {OLD}, not {NEW}" in unprepared["said"],
          f"preparation is not activation: an unprepared tenant is refused before anything runs: {unprepared}")
    failed = now["deploy fails, board unchanged"]
    check(failed["code"] == 1 and failed["steps"] == ["stop listener", "install units", "deploy-restart", "start listener"]
          and failed["board"] == OLD and failed["listener"] == "active" and failed["release"] == "blocked",
          f"a failed deploy that left the board: the listener comes back, the phase is blocked: {failed}")
    moved = now["deploy fails, board moved"]
    check(moved["code"] == 1 and moved["steps"] == ["stop listener", "install units", "deploy-restart"]
          and moved["listener"] == "inactive" and moved["release"] == "blocked" and "left stopped" in moved["said"],
          f"a failed deploy that moved the board: the listener is left down, said so: {moved}")
    down = now["listener does not come back"]
    check(down["code"] == 1 and down["release"] == "blocked" and "listener is not running" in down["said"],
          f"deployed but the listener is not back: not closed: {down}")
    wrong = now["live board serves another build"]
    check(wrong["code"] == 1 and wrong["release"] == "blocked" and wrong["board"] == NEW,
          f"the running board reports another build: the phase is not closed: {wrong}")
    already = now["already deployed"]
    check(already["code"] == 0 and already["steps"] == [] and already["release"] == "done",
          f"a board already serving the release is only closed, nothing restarted: {already}")
    missing = now["prepared release not installed"]
    check(missing["code"] == 1 and missing["steps"] == [] and missing["release"] == "ready"
          and f"root holds no installed release of {NEW}" in missing["said"],
          f"a pin whose release root does not hold is refused before anything runs: {missing}")
    unprivileged = now["not root"]
    check(unprivileged["code"] == 1 and unprivileged["steps"] == [] and "Nothing was deployed" in unprivileged["said"],
          f"not root: nothing: {unprivileged}")
    for name, result in now.items():
        check(result["attempts"] and result["attempts"][0]["closed"]["status"] == ("succeeded" if result["code"] == 0 else "failed"),
              f"{name}: the helper's attempt record tells the truth: {result['attempts']}")
    print(f"catalogued_deploy_release_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
