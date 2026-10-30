#!/usr/bin/env python3
"""SYRD-632: `deploy-release` closes the release phase against a board that answers, then puts it back.

Live on MEFP, 2026-10-18 22:28 EDT, release 35a0a604: the tenant was suspended
on purpose; `deploy-release` started its board for the close, closed, and put
the board back -- and the close found connection refused, no build and no
workflow, so the phase stayed open. systemd's record of that board run:
`Duration: 1.170s`, `status=0/SUCCESS`, which is the board's SIGTERM exit, the
command's own stop. The board's unit is `Type=simple`: `systemctl start`
returns when the process has forked, not when it serves, and the close ran in
that gap. The operator's recovery waited for `/api/board` and then closed the
same release cleanly.

The SYRD-577 suite's host brings a board up the instant `systemctl start`
returns, so it never saw the gap. Here the board is a real process: its unit's
start returns at once, the process binds its port only after a delay, it
answers `/api/board` and `/api/workflow` over real HTTP with the build its
release names, and `systemctl stop` sends it SIGTERM. So a close that reads it
too early, or after it is stopped, is refused exactly as a real board refuses
it. The tenant, its journals and root's pin are `release_phase_journal_test`'s
disposable tenant; the command is the real `switchyard_deploy_release_command`
as root in-process. Every pane, window and worker path is replaced by one that
records being reached. The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import getpass
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "146e79012e1b5c1316ca3d061df2faf5f0d8ed17"  # main before SYRD-632
OLD, NEW = "a" * 40, "e" * 40
#: How long after its unit is started the board binds its port.
BIND_DELAY = 0.8
CHECKS = 0
LIFECYCLE_PATHS = ("launch_project", "resume_tenant", "launch_konsole_window", "replace_presentation_window_command",
                   "launch_presentation", "presentation_action", "recover_display", "start_project")
BOARD = r"""
import http.server, json, signal, sys, time
port, build_file, delay, mode = int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
time.sleep(delay)
if mode == "never":
    time.sleep(3600)
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith("/api/workflow"):
            body = {"document": {"roles": [], "migrations": {"director_onboarding": True}}}
        else:
            body = {"build_id": open(build_file).read().strip(), "tickets": []}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *_a):
        pass
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Host:
    """systemd for one tenant: a real board process behind its unit, and the owner's listener unit."""

    def __init__(self, tenant, work: Path, unit_dir: Path, trusted_release: Path, *, exact_units: bool,
                 board_unit: str, listener: str, board_mode: str = "ok") -> None:
        self.tenant, self.work, self.mode = tenant, work, board_mode
        self.unit_dir, self.trusted_release = unit_dir, trusted_release
        self.exact_units = exact_units
        self.port = free_port()
        self.build_file = work / "build"
        self.build_file.write_text(self._serving() + "\n", encoding="utf-8")
        self.listener = listener
        self.process: subprocess.Popen | None = None
        self.steps: list[str] = []
        self.started_at = 0.0
        if board_unit == "active":
            self._start(delay=0.0)
            self._wait_until_serving()

    def _serving(self) -> str:
        return (self.tenant.board_root / "current").resolve().name

    def _start(self, *, delay: float = BIND_DELAY) -> None:
        self.process = subprocess.Popen([sys.executable, "-c", BOARD, str(self.port), str(self.build_file), str(delay),
                                         self.mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started_at = time.monotonic()

    def _stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=10)
        self.process = None

    def _wait_until_serving(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def active(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def opener(self, url: str):
        """The board's HTTP, for real: whatever the board says, or the refusal a closed port gives."""
        from urllib.parse import urlparse
        from urllib.request import urlopen

        path = urlparse(url).path or "/api/board"
        try:
            response = urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2)
        except OSError as exc:
            self.steps.append(f"read refused {path}")
            raise OSError(f"connection refused ({exc})") from exc
        self.steps.append(f"read {path}")
        return response

    def move_board(self, commit: str) -> None:
        release = self.tenant.board_root / "releases" / commit
        release.mkdir(parents=True, exist_ok=True)
        (release / ".pgu-deploy-sha").write_text(commit + "\n", encoding="utf-8")
        current = self.tenant.board_root / "current"
        current.unlink()
        current.symlink_to(release)
        self.build_file.write_text(commit + "\n", encoding="utf-8")

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
            self.listener = "active"
            return done(0)
        if text.startswith("systemctl is-active ") and text.endswith("-ticket-board.service"):
            return done(0 if self.active() else 3, "active" if self.active() else "inactive")
        if text.startswith("systemctl stop ") and text.endswith("-ticket-board.service"):
            self.steps.append("board stop")
            self._stop()
            return done(0)
        if text.startswith("systemctl start ") and text.endswith("-ticket-board.service"):
            # Type=simple: started once the process has forked, serving only once it has bound.
            self.steps.append("board start")
            self._start()
            return done(0)
        if "sudo install" in text:
            self.steps.append("install templates")
            return done(0)
        if argv[0] == "install":
            source, target = Path(argv[-2]), Path(argv[-1])
            restoring = source.read_text(encoding="utf-8").startswith("old ")
            shutil.copyfile(source, target)
            step = "restore units" if restoring else "install exact units"
            if step not in self.steps:
                self.steps.append(step)
            return done(0)
        if argv[:2] == ["systemctl", "daemon-reload"]:
            return done(0)
        if argv[:2] == ["systemctl", "restart"]:
            self.steps.append("restart restored board")
            self._stop()
            self._start(delay=0.0)
            self._wait_until_serving()
            return done(0)
        if argv[:4] == ["runuser", "-u", "postgres", "--"]:
            # SYRD-706: root migrates and grants first, as the cluster's account, from its trusted release.
            assert any(part.startswith(str(self.trusted_release) + "/") for part in argv), argv
            self.steps.append("migrate as postgres" if argv[-1].endswith("ticket-board-migrate") else "rbac as postgres")
            return done(0)
        if "deploy-restart" in text:
            # The tenant's own deploy-restart: the release moves, the unit restarts, and its smoke check waits
            # for the board to answer before it returns.
            if self.exact_units:
                words = shlex.split(argv[-1])
                proof_values = [word.split("=", 1)[1] for word in words
                                if word.startswith("TICKET_BOARD_PROVISIONED_SYSTEM_UNIT=")]
                board_path = self.unit_dir / "porter-ticket-board.service"
                canary_path = self.unit_dir / "porter-ticket-board-canary.service"
                assert len(proof_values) == 1 and Path(proof_values[0]).read_bytes() == board_path.read_bytes()
                assert str(self.trusted_release) in board_path.read_text(encoding="utf-8")
                assert str(self.trusted_release) in canary_path.read_text(encoding="utf-8")
                self.steps.append("deploy-restart exact candidate")
            else:
                self.steps.append("deploy-restart")
            self.move_board(NEW)
            self._stop()
            self._start()
            serving = self._wait_until_serving()
            self.steps.append("deploy-restart smoke ok" if serving else "deploy-restart smoke failed")
            return done(0 if serving else 1)
        self.steps.append("other: " + text[:120])
        return done(0)


def deploy(root: Path, work: Path, *, board_unit: str, listener: str, deployed: str, board_mode: str = "ok",
           ready_seconds: float | None = None, exact_installed: bool = False) -> dict:
    """`switchyard deploy-release porter --commit NEW` against a prepared tenant in that state."""
    import release_phase_journal_test as journal
    from scripts import team_launcher as launcher
    try:
        from scripts.ticket_board import board_identity, privileged_operations
        from scripts.ticket_board.project_provision import build_plan, render_board_unit, render_canary_unit
    except ImportError:
        # The historical comparison predates the root-release modules. Keep
        # its original generic-unit fixture so the before assertion remains a
        # test of the readiness bug it was written for.
        board_identity = privileged_operations = None

    tenant = journal.Tenant(work, deployed=deployed)
    releases = work / "opt-switchyard" / "releases"
    for commit in (NEW, OLD):
        release = releases / commit
        (release / "scripts").mkdir(parents=True, exist_ok=True)
        (release / launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(json.dumps({"commit": commit}), encoding="utf-8")
        (release / "scripts" / "ticket-board-service.sh").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        # SYRD-700: a release new enough to run under its exact unit carries the renderer.
        (release / "scripts" / "ticket_board").mkdir(exist_ok=True)
        (release / "scripts" / "ticket_board" / "release_units.py").write_text("# renderer\n", encoding="utf-8")
        # SYRD-706: what root migrates and grants from, as the cluster's account.
        (release / "scripts" / "ticket_board" / "migrations").mkdir(exist_ok=True)
        (release / "scripts" / "ticket_board" / "rbac.sql").write_text("SELECT 1;\n", encoding="utf-8")
        (release / "scripts" / "ticket-board-migrate").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    units = launcher.privileged_provision_dir("porter", root=launcher.switchyard_privileged_provision_root())
    units.mkdir(parents=True, exist_ok=True)
    if board_identity is not None:
        plan = build_plan(
            project="porter", owner_user=getpass.getuser(), owner_home=work / "tenant",
            board_root=tenant.board_root, port=29117, commit_git_dir=work / "cache.git",
            asset_dir=work / "assets", frame_dir=work / "frames",
        )
        (units / "porter-ticket-board.service").write_text(render_board_unit(plan), encoding="utf-8")
        (units / "porter-ticket-board-canary.service").write_text(render_canary_unit(plan), encoding="utf-8")
    else:
        for name in ("ticket-board", "ticket-board-canary"):
            (units / f"porter-{name}.service").write_text("[Unit]\n", encoding="utf-8")
    (units / "porter-ticket-board-notify-listener.service").write_text("[Unit]\n", encoding="utf-8")
    unit_dir = work / "systemd"
    unit_dir.mkdir()
    (unit_dir / "porter-ticket-board.service").write_text("old board unit\n", encoding="utf-8")
    (unit_dir / "porter-ticket-board-canary.service").write_text("old canary unit\n", encoding="utf-8")
    pin = launcher.privileged_upgrade_source_path(tenant.config)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(json.dumps({"schema": launcher.UPGRADE_SOURCE_SCHEMA, "project": "porter",
                               "source_repo": str(releases / NEW), "commit_git_dir": "/data/git/cache",
                               "deploy_ref": NEW}), encoding="utf-8")
    os.chmod(pin, 0o600)
    host = Host(tenant, work, unit_dir, releases / NEW, exact_units=board_identity is not None, board_unit=board_unit,
                listener=listener, board_mode=board_mode)
    reached: list[str] = []
    from scripts import tenant_release_deploy as deploy_module

    if exact_installed and board_identity is not None and hasattr(deploy_module, "_serves_exact_units"):
        # SYRD-700: a tenant already on the release under root's exact unit for it has nothing to converge.
        rendered = deploy_module._candidate_units(
            SimpleNamespace(provisioned_system_unit=units / "porter-ticket-board.service"), "porter", releases / NEW,
            26991)
        for name in ("porter-ticket-board.service", "porter-ticket-board-canary.service"):  # both, as root renders them
            (unit_dir / name).write_bytes(next(v for k, v in rendered.items() if k.name == name))
    if ready_seconds is not None and hasattr(deploy_module, "BOARD_READY_SECONDS"):
        deploy_module.BOARD_READY_SECONDS = ready_seconds
    if board_identity is not None:
        privileged_operations.SHARED_RELEASES = str(releases)
        privileged_operations.TRUSTED_OWNER_UID = os.getuid()
        deploy_module.SYSTEM_UNIT_DIR = unit_dir
        deploy_module.TRUSTED_UNIT_OWNER_UID = os.getuid()

        def attest(*_args, **_kwargs):
            build = host.build_file.read_text(encoding="utf-8").strip()
            if build != NEW:
                raise board_identity.BoardIdentityError(f"board reports {build}, not root release {NEW}")
            return SimpleNamespace(release_commit=NEW)

        board_identity.effective_unit = attest
    launcher.os.geteuid = lambda: 0
    launcher.record_upgrade_phase(tenant.config, config_path=tenant.config_path, phase="release", state="ready",
                                  detail="prepared")
    launcher._resolve_switchyard_project = lambda project, **_k: SimpleNamespace(slug="porter",
                                                                                  config_path=tenant.config_path)
    launcher._open_board_url = host.opener
    launcher.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"unmodelled: {a[:1]}"))
    for name in LIFECYCLE_PATHS:
        if hasattr(launcher, name):
            setattr(launcher, name, lambda *a, _n=name, **k: reached.append(_n) or 0)
    said: list[str] = []
    started = time.monotonic()
    try:
        code = launcher.switchyard_deploy_release_command("porter", commit=NEW, runner=host, opener=host.opener,
                                                         print_func=said.append)
    finally:
        board_active = host.active()
        host._stop()
    return {"code": code, "said": "\n".join(said), "steps": host.steps, "board_active": board_active,
            "listener": host.listener, "release": launcher.upgrade_phase_state(tenant.trusted(), "release"),
            "serving": launcher._current_tenant_release(tenant.board_root)[1], "reached": reached,
            "seconds": round(time.monotonic() - started, 2),
            "board_unit": (unit_dir / "porter-ticket-board.service").read_text(encoding="utf-8")}


def scenario(root: Path, work: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        sys.path.insert(0, extra)
    out = {}
    cases = {
        "suspended, already on the release": dict(board_unit="inactive", listener="inactive", deployed=NEW,
                                                  exact_installed=True),
        "suspended, deployed here": dict(board_unit="inactive", listener="inactive", deployed=OLD),
        "running, already on the release": dict(board_unit="active", listener="active", deployed=NEW,
                                                exact_installed=True),
        "running, deployed here": dict(board_unit="active", listener="active", deployed=OLD),
        "suspended, a board that never answers": dict(board_unit="inactive", listener="inactive", deployed=NEW,
                                                      board_mode="never", ready_seconds=2.0, exact_installed=True),
    }
    for name, kwargs in cases.items():
        case_dir = work / re.sub(r"\W+", "-", name)
        case_dir.mkdir()
        out[name] = deploy(root, case_dir, **kwargs)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd632.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def reads_before_stop(seen: dict) -> list[str]:
    stop = seen["steps"].index("board stop") if "board stop" in seen["steps"] else len(seen["steps"])
    return [step for step in seen["steps"][:stop] if step.startswith("read")]


def before(out: dict) -> None:
    seen = out["suspended, already on the release"]
    check(seen["code"] != 0 and seen["release"] == "blocked"
          and any(step.startswith("read refused") for step in seen["steps"]) and seen["steps"][-1] == "board stop",
          f"before: MEFP's sequence -- the board is started for the close, the close reads it before it answers and "
          f"is refused, the phase is left blocked, and the board is stopped again: {seen}")


def candidate(out: dict) -> None:
    seen = out["suspended, already on the release"]
    steps = seen["steps"]
    check(seen["code"] == 0 and seen["release"] == "done" and seen["serving"] == NEW,
          f"MEFP's sequence: the phase is closed against the board's live build: {seen}")
    start, stop = steps.index("board start"), steps.index("board stop")
    reads = reads_before_stop(seen)
    check(start < stop and stop == len(steps) - 1 and "read /api/board" in reads
          and all(not step.startswith("read") for step in steps[stop:]),
          f"in order: the board is started, read once it answers, and stopped last; nothing reads it after: {steps}")
    check(not seen["board_active"] and seen["listener"] == "inactive" and "listener start" not in steps
          and seen["reached"] == [] and "ends as it began: suspended" in seen["said"],
          f"it ends suspended: board and listener inactive, the listener never started, no pane, window or worker "
          f"path reached, and it says so: {seen}")
    deployed = out["suspended, deployed here"]
    check(deployed["code"] == 0 and deployed["release"] == "done" and deployed["serving"] == NEW
          and "install exact units" in deployed["steps"]
          and "deploy-restart exact candidate" in deployed["steps"]
          and NEW in deployed["board_unit"]
          and deployed["steps"].index("deploy-restart smoke ok") < deployed["steps"].index("read /api/board")
          < deployed["steps"].index("board stop") == len(deployed["steps"]) - 1
          and not deployed["board_active"] and "listener start" not in deployed["steps"] and deployed["reached"] == [],
          f"deployed here: deploy and health, the close while the board is live, then the board stopped; the "
          f"listener is never started: {deployed}")
    for name in ("running, already on the release", "running, deployed here"):
        running = out[name]
        check(running["code"] == 0 and running["release"] == "done" and running["board_active"]
              and "board stop" not in running["steps"] and running["listener"] == "active"
              and running["reached"] == [] and "ends as it began: active" in running["said"],
              f"{name}: closed, and left running as it was, with no pane, window or worker path: {running}")
    never = out["suspended, a board that never answers"]
    check(never["code"] != 0 and never["release"] == "blocked" and "did not answer within 2s" in never["said"]
          and never["steps"][-1] == "board stop" and not never["board_active"] and never["listener"] == "inactive"
          and never["reached"] == [] and never["seconds"] < 20,
          f"a board that never answers: waited for, bounded, the phase recorded blocked with the reason, and the "
          f"board stopped again: {never}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD632_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd632-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"deploy_release_board_readiness_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
