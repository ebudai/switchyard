#!/usr/bin/env python3
"""SYRD-577: an upgrade and a release deploy leave a tenant as active or as suspended as they found it.

On 2026-10-10 MEFP was suspended on purpose, so its pull queue could not assign
work. Closing its prepared release needed a board that answered, so the
operator ran `switchyard start mefp`, and the worker pool began assigning. The
board deploy needed none of that.

- **`deploy-release`** reads the board unit's and the listener's state first and
  says how the tenant began. The listener is started afterwards only if it was
  running. A board that was stopped serves only for the deploy's health check
  and the close, then is stopped again. A board already on the release is
  started only for the close. Every exit ends with whether the tenant ends as it
  began, naming any component that could not be put back.
- **`upgrade`** does not run a pending role-account cutover -- which restarts
  every role -- for a tenant with no running role. It says so and names the
  explicit `switchyard start`.
- **Neither** opens, presents, recovers or replaces a window, or starts or stops
  a worker, for a tenant whose workers were live with no window, or which had
  one.

The deploy runs against a disposable journalled tenant whose board release
really moves, with a recording host whose board unit, listener and HTTP board
follow each other. The upgrade runs through the upgrade suite's own tenant
harness. Every start, present and window path is replaced by one that fails the
case if it is reached.
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
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from scripts import team_launcher as launcher  # noqa: E402

OLD, NEW, OTHER = "a" * 40, "e" * 40, "f" * 40
CHECKS = 0
LIFECYCLE_PATHS = ("launch_project", "resume_tenant", "launch_konsole_window", "replace_presentation_window_command",
                   "launch_presentation", "presentation_action", "recover_display", "start_project")


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Host:
    """systemd, the deploy and the HTTP board, following each other as a host's would."""

    def __init__(self, tenant, board, *, board_unit: str, listener: str, units="ok", deploy="ok",
                 listener_start="ok", board_stop="ok", board_start="ok") -> None:
        self.tenant, self.http = tenant, board
        self.board_unit, self.listener = board_unit, listener
        self.outcome = dict(units=units, deploy=deploy, listener_start=listener_start, board_stop=board_stop,
                            board_start=board_start)
        self.steps: list[str] = []

    def opener(self, url: str):
        if self.board_unit != "active":
            raise OSError("connection refused: the board is not running")
        return self.http(url)

    def move_board(self, commit: str) -> None:
        release = self.tenant.board_root / "releases" / commit
        release.mkdir(parents=True, exist_ok=True)
        (release / ".pgu-deploy-sha").write_text(commit + "\n", encoding="utf-8")
        current = self.tenant.board_root / "current"
        current.unlink()
        current.symlink_to(release)

    def __call__(self, argv, **_kwargs):
        text = " ".join(str(part) for part in argv)
        done = lambda code, out="": SimpleNamespace(returncode=code, stdout=out, stderr="" if code == 0 else "simulated")
        if "systemctl --user is-active" in text:
            return done(0 if self.listener == "active" else 3, self.listener)
        if "systemctl --user stop" in text:
            self.steps.append("listener stop")
            self.listener = "inactive"
            return done(0)
        if "systemctl --user daemon-reload && systemctl --user restart" in text:
            self.steps.append("listener start")
            if self.outcome["listener_start"] == "ok":
                self.listener = "active"
                return done(0)
            return done(1)
        if text.startswith("systemctl is-active ") and text.endswith("-ticket-board.service"):
            return done(0 if self.board_unit == "active" else 3, self.board_unit)
        if text.startswith("systemctl stop ") and text.endswith("-ticket-board.service"):
            self.steps.append("board stop")
            if self.outcome["board_stop"] == "ok":
                self.board_unit = "inactive"
                return done(0)
            return done(1)
        if text.startswith("systemctl start ") and text.endswith("-ticket-board.service"):
            self.steps.append("board start")
            if self.outcome["board_start"] == "ok":
                self.board_unit = "active"
                return done(0)
            return done(1)
        if "sudo install" in text:
            self.steps.append("install units")
            return done(0 if self.outcome["units"] == "ok" else 1)
        if "deploy-restart" in text:
            # The tenant's own deploy-restart restarts the board unit: a stopped
            # one comes up, whether the deploy then succeeds or rolls back.
            self.steps.append("deploy-restart")
            self.board_unit = "active"
            outcome = self.outcome["deploy"]
            if outcome in ("ok", "moved", "wrong build"):
                self.move_board(NEW)
                self.http.build_id = OTHER if outcome == "wrong build" else NEW
            return done(0 if outcome in ("ok", "wrong build") else 1)
        self.steps.append("other: " + text[:120])
        return done(0)


def deploy(*, board_unit: str, listener: str, deployed: str = OLD, **outcome) -> dict:
    """`switchyard deploy-release porter --commit NEW` against a prepared tenant in that state."""
    import release_phase_journal_test as journal

    with tempfile.TemporaryDirectory(prefix="syrd577-deploy.") as raw:
        tmp = Path(raw)
        tenant = journal.Tenant(tmp, deployed=deployed)
        releases = tmp / "opt-switchyard" / "releases"
        for commit in (NEW, OLD):
            release = releases / commit
            (release / "scripts").mkdir(parents=True, exist_ok=True)
            (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}), encoding="utf-8")
            (release / "scripts" / "ticket-board-service.sh").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        units = launcher.privileged_provision_dir("porter", root=launcher.switchyard_privileged_provision_root())
        units.mkdir(parents=True, exist_ok=True)
        for unit in ("ticket-board", "ticket-board-canary", "ticket-board-notify-listener"):
            (units / f"porter-{unit}.service").write_text("[Unit]\n", encoding="utf-8")
        pin = launcher.privileged_upgrade_source_path(tenant.config)
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(json.dumps({"schema": launcher.UPGRADE_SOURCE_SCHEMA, "project": "porter",
                                   "source_repo": str(releases / NEW), "commit_git_dir": "/data/git/cache",
                                   "deploy_ref": NEW}), encoding="utf-8")
        os.chmod(pin, 0o600)
        board = journal.FakeBoard(deployed)
        host = Host(tenant, board, board_unit=board_unit, listener=listener, **outcome)
        saved = {name: getattr(launcher, name) for name in ("_resolve_switchyard_project", "_open_board_url")}
        saved_lifecycle = {name: getattr(launcher, name) for name in LIFECYCLE_PATHS if hasattr(launcher, name)}
        saved_euid, saved_run = launcher.os.geteuid, launcher.subprocess.run
        reached: list[str] = []
        try:
            launcher.os.geteuid = lambda: 0
            launcher.record_upgrade_phase(tenant.config, config_path=tenant.config_path, phase="release",
                                          state="ready", detail="prepared")
            launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(
                slug="porter", config_path=tenant.config_path)
            launcher._open_board_url = host.opener
            launcher.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"unmodelled: {a[:1]}"))
            for name in saved_lifecycle:
                setattr(launcher, name, lambda *a, _n=name, **k: reached.append(_n) or 0)
            said: list[str] = []
            code = launcher.switchyard_deploy_release_command(
                "porter", commit=NEW, runner=host, opener=host.opener, print_func=said.append)
            release = launcher.upgrade_phase_state(tenant.trusted(), "release")
            serving = launcher._current_tenant_release(tenant.board_root)[1]
        finally:
            for name, value in {**saved, **saved_lifecycle}.items():
                setattr(launcher, name, value)
            launcher.os.geteuid, launcher.subprocess.run = saved_euid, saved_run
    return {"code": code, "said": "\n".join(said), "steps": host.steps, "board_unit": host.board_unit,
            "listener": host.listener, "release": release, "serving": serving, "reached": reached}


def nothing_started(seen: dict) -> bool:
    """No pane, window or worker path, and no tmux or terminal, was reached."""
    others = [step for step in seen["steps"] if step.startswith("other:")]
    return seen["reached"] == [] and not any(word in step for step in others for word in ("tmux", "konsole", "launch"))


def test_a_suspended_tenant_is_deployed_closed_and_left_suspended() -> None:
    seen = deploy(board_unit="inactive", listener="inactive")
    check(seen["code"] == 0 and seen["serving"] == NEW and seen["release"] == "done",
          f"the release is deployed and its phase closed: {seen['code']} {seen['serving']} {seen['release']}\n{seen['said']}")
    check(seen["board_unit"] == "inactive" and seen["listener"] == "inactive",
          f"and the board and listener are inactive again, as they were: {seen['board_unit']} {seen['listener']}")
    check("listener start" not in seen["steps"] and seen["steps"][-1] == "board stop",
          f"the listener was never started -- no notification delivery -- and the board went down last: {seen['steps']}")
    check(nothing_started(seen), f"no pane, window or worker was started: {seen['reached']} {seen['steps']}")
    check("began suspended" in seen["said"] and "ends as it began: suspended" in seen["said"],
          f"the output says how it began and that it ended so: {seen['said']}")


def test_a_suspended_tenant_already_on_the_release_is_closed_without_being_started() -> None:
    seen = deploy(board_unit="inactive", listener="inactive", deployed=NEW)
    check(seen["code"] == 0 and seen["release"] == "done",
          f"the phase is closed against the board's own answer: {seen['code']} {seen['release']}\n{seen['said']}")
    check(seen["steps"] == ["board start", "board stop"] and seen["board_unit"] == "inactive"
          and seen["listener"] == "inactive",
          f"the board came up only for the close and went down; the listener was untouched: {seen['steps']}")
    check(nothing_started(seen) and "ends as it began: suspended" in seen["said"], f"nothing else: {seen['said']}")


def test_a_running_tenant_is_deployed_and_left_running() -> None:
    seen = deploy(board_unit="active", listener="active")
    check(seen["code"] == 0 and seen["release"] == "done" and seen["board_unit"] == "active"
          and seen["listener"] == "active",
          f"deployed, closed, and still running: {seen}")
    check(seen["steps"] == ["listener stop", "install units", "deploy-restart", "listener start"],
          f"the sequence a running tenant always had, and no board stop: {seen['steps']}")
    check(nothing_started(seen) and "began active" in seen["said"] and "ends as it began: active" in seen["said"],
          f"no pane or window, and the output says so: {seen['said']}")


def test_a_running_board_whose_listener_was_stopped_keeps_it_stopped() -> None:
    seen = deploy(board_unit="active", listener="inactive")
    check(seen["code"] == 0 and seen["board_unit"] == "active" and seen["listener"] == "inactive"
          and "listener start" not in seen["steps"] and "partly running" in seen["said"],
          f"a listener that was down stays down: {seen['steps']} {seen['said']}")


def test_failures_leave_truthful_journals_and_a_lifecycle_report() -> None:
    cases = {
        "the unit install fails": dict(units="fail"),
        "the deploy fails, board unchanged": dict(deploy="fail"),
        "the deploy fails, board moved": dict(deploy="moved"),
        "the board serves another build": dict(deploy="wrong build"),
    }
    for label, outcome in cases.items():
        seen = deploy(board_unit="inactive", listener="inactive", **outcome)
        check(seen["code"] != 0 and seen["release"] != "done",
              f"{label}: it fails and the phase is not recorded done: {seen['code']} {seen['release']}")
        check(seen["board_unit"] == "inactive" and seen["listener"] == "inactive" and "listener start" not in seen["steps"],
              f"{label}: the tenant is put back suspended: {seen['board_unit']} {seen['listener']} {seen['steps']}")
        check("ends as it began: suspended" in seen["said"] and nothing_started(seen),
              f"{label}: and says so, with no window or pane: {seen['said']}")
    blocked = deploy(board_unit="inactive", listener="inactive", units="fail")
    check(blocked["release"] == "blocked", f"a failed step is journalled blocked: {blocked['release']}")


def test_what_cannot_be_put_back_is_named_and_fails_the_command() -> None:
    stuck = deploy(board_unit="inactive", listener="inactive", board_stop="fail")
    check(stuck["code"] != 0 and stuck["release"] == "done" and stuck["board_unit"] == "active",
          f"the deploy and close succeeded, but the board would not stop: {stuck['code']} {stuck['board_unit']}")
    check("not restored:" in stuck["said"] and "did NOT end as it began" in stuck["said"]
          and "ends as it began" not in stuck["said"].replace("did NOT end as it began", ""),
          f"so it says what was not restored and never that it was: {stuck['said']}")
    listener = deploy(board_unit="active", listener="active", listener_start="fail")
    check(listener["code"] != 0 and "did NOT end as it began" in listener["said"],
          f"a listener that was running and did not come back is not reported restored: {listener['said']}")
    start = deploy(board_unit="inactive", listener="inactive", deployed=NEW, board_start="fail")
    check(start["code"] != 0 and start["release"] != "done" and start["board_unit"] == "inactive"
          and "ends as it began: suspended" in start["said"],
          f"a board that will not start for the close: no close, and still suspended: {start['said']}")


# --- the upgrade ---------------------------------------------------------------------------------------------------


class Refused:
    """Every start, present and window path, failing the case if it is reached."""

    def __enter__(self):
        from scripts import presentation_controller

        self.reached: list[str] = []
        self.saved = []
        for module, names in ((launcher, LIFECYCLE_PATHS),
                              (presentation_controller, ("presentation_action", "launch_presentation"))):
            for name in names:
                if hasattr(module, name):
                    self.saved.append((module, name, getattr(module, name)))
                    setattr(module, name, lambda *a, _n=name, **k: self.reached.append(_n) or 0)
        return self

    def __exit__(self, *exc):
        for module, name, value in self.saved:
            setattr(module, name, value)


def _upgrade_once(*, live: bool, window: bool) -> tuple[int, str, object, list[str]]:
    """One root upgrade of a single-owner declared-workflow tenant whose workers are live or gone."""
    import team_launcher_upgrade_cutover_test as upgrade_suite
    from scripts import presentation_windows

    with tempfile.TemporaryDirectory(prefix="syrd577-upgrade.") as raw:
        config_path, _ = upgrade_suite._declarative_tenant(Path(raw))
        upgrade_suite._mark_projection_migrated(config_path)
        with upgrade_suite._RunningTenant(config_path, account_uid=os.getuid()) as tenant:
            # First the one-time repatriation onto the project account, which a
            # legacy fixture needs and which runs only with the workers stopped;
            # the upgrade measured is the next one, in the state this case is in.
            tenant.live = False
            prepared, said, _m = upgrade_suite._upgrade(config_path, as_root=True, exists=set(), runner=tenant.runner(),
                                                        board=upgrade_suite._board_with_marker(True))
            assert prepared == 0 and json.loads(config_path.read_text(encoding="utf-8")).get("role_state_isolation"), said
            tenant.launches.clear()
            tenant.stops.clear()
            tenant.listener_calls.clear()
            tenant.live = live
            saved = (launcher.presentation_window_processes, launcher.presentation_is_attached)
            windows = [presentation_windows.PresentationWindowProcess(4242, os.getuid(), "porter-layout")] if window else []
            launcher.presentation_window_processes = lambda *a, **k: list(windows)
            launcher.presentation_is_attached = lambda *a, **k: bool(windows)
            try:
                with Refused() as refused:
                    result, output, _m = upgrade_suite._upgrade(
                        config_path, as_root=True, exists=set(), runner=tenant.runner(),
                        board=upgrade_suite._board_with_marker(True))
            finally:
                launcher.presentation_window_processes, launcher.presentation_is_attached = saved
            return result, output, tenant, refused.reached


def test_an_upgrade_of_a_fully_stopped_tenant_starts_nothing() -> None:
    result, output, tenant, reached = _upgrade_once(live=False, window=False)
    check(result == 0, f"the stopped tenant is prepared: {result}\n{output[-600:]}")
    check(tenant.launches == [] and tenant.stops == [] and not tenant.live,
          f"no worker was started or stopped, and none is live: {tenant.launches} {tenant.stops} {tenant.live}")
    check(reached == [], f"no start, present or window path was reached: {reached}")
    check(not any("listener" in call and ("start" in call or "restart" in call) for call in tenant.listener_calls),
          f"and the listener was not started: {tenant.listener_calls}")


def test_an_upgrade_keeps_live_headless_workers_and_opens_no_window() -> None:
    result, output, tenant, reached = _upgrade_once(live=True, window=False)
    check(result == 0 and tenant.launches == [] and tenant.stops == [] and tenant.live,
          f"live workers are neither stopped nor restarted: {result} {tenant.launches} {tenant.stops}\n{output[-400:]}")
    check(reached == [], f"and no window is opened, presented or replaced: {reached}")


def test_an_upgrade_neither_replaces_nor_duplicates_an_existing_window() -> None:
    result, output, tenant, reached = _upgrade_once(live=True, window=True)
    check(result == 0 and tenant.launches == [] and tenant.stops == [] and reached == [],
          f"with a window already open, nothing is replaced or opened beside it: {result} {reached}\n{output[-400:]}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"upgrade_lifecycle_preservation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
