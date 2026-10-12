#!/usr/bin/env python3
"""SYRD-586: one host supervisor runs every tenant board, each as its own isolated child.

Nothing here is a stand-in for the supervisor, its exec stage or the unit it
reads. The boards are units rendered by provisioning's own `render_board_unit`,
and the supervisor is the shipped `scripts/board_supervisor.py serve`, run as
root in a user namespace (`unshare --map-auto --map-root-user --mount --net
--pid`). Only the board program is a fixture: a small HTTP server in place of
`ticket-board.py`, which needs PostgreSQL. It reports who it runs as and
what it can write, and exits 0 on SIGTERM as the real one does. A fixture
`systemctl` answers `is-active` for the supervisor.

Inside, `boardsvc` (uid 900, also a member of `shared` in /etc/group, as
`boardsvc` is of `syrd-roles` on the live host) runs two tenants' boards,
`alpha` and `beta`, each with its own group, home, port, socket, database URL
and environment file.

The checks:
- each board runs with its own identity, groups, environment, release and
  sandbox, and writes only its own directories;
- an unrequested clean exit is restarted and leaves the neighbour untouched,
  while an intentional stop survives a supervisor restart;
- control is per tenant and per owner; status, restart (upgrade), the start
  limit, reload and refusals behave as documented.

Runs from this tree, or from another tree given as the first argument.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parents[1]
USERS = {"boardsvc": 900, "alpha": 1001, "beta": 1002, "stranger": 1003}
SHARED_GID = 1500
PORTS = {"alpha": 21001, "beta": 21002, "gamma": 21003, "delta": 21004, "epsilon": 21005, "zeta": 21006}
failures: list[str] = []
CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(("ok   " if condition else "FAIL ") + message, flush=True)
    if not condition:
        failures.append(message)


FIXTURE_BOARD = r'''
import argparse, http.server, json, os, signal, socket, sys, threading, urllib.parse
parser = argparse.ArgumentParser()
for name in ("--host", "--unix-socket", "--frames", "--assets", "--commit-git-dir"):
    parser.add_argument(name, default="")
parser.add_argument("--port", type=int)
args = parser.parse_args()
here = os.path.dirname(os.path.realpath(__file__))
release = os.path.dirname(here)
if os.path.exists(os.path.join(release, "EXIT_AT_START")):
    print("fixture board: exiting at start", flush=True)
    sys.exit(0)
if os.path.exists(os.path.join(release, "HANG_AT_START")):
    print("fixture board: never answering", flush=True)
    while True:
        __import__("time").sleep(60)
version = open(os.path.join(release, "VERSION")).read().strip()

class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/board":
            status = open("/proc/self/status").read()
            self.answer({"pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid(), "groups": sorted(os.getgroups()),
                         "version": version, "release": release, "cwd": os.getcwd(),
                         "nnp": [l.split()[1] for l in status.splitlines() if l.startswith("NoNewPrivs")][0],
                         "env": {k: v for k, v in os.environ.items()}})
        elif url.path == "/write":
            target = urllib.parse.parse_qs(url.query)["path"][0]
            try:
                with open(target, "w") as handle:
                    handle.write("x")
                self.answer({"wrote": True})
            except OSError as exc:
                self.answer({"wrote": False, "errno": exc.errno})
        elif url.path == "/exit":
            self.answer({"exiting": os.getpid()})
            threading.Timer(0.2, lambda: os._exit(0)).start()
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass

server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
unix.bind(args.unix_socket); unix.listen(4)
def stop(signum, frame):
    print(f"fixture board: received {signal.Signals(signum).name}; stopping", flush=True)
    threading.Thread(target=server.shutdown, daemon=True).start()
signal.signal(signal.SIGTERM, stop)
print(f"fixture board {version} pid {os.getpid()} listening on {args.port}", flush=True)
server.serve_forever()
sys.exit(0)
'''

FAKE_SYSTEMCTL = r'''#!/bin/sh
# is-active --quiet UNIT: "active" only for units listed in $FAKE_ACTIVE_UNITS.
[ "$1" = is-active ] || { echo "fixture systemctl: unexpected $*" >&2; exit 2; }
for unit in $(cat "$FAKE_ACTIVE_UNITS" 2>/dev/null); do [ "$unit" = "$3" ] && exit 0; done
exit 3
'''


# ---------------------------------------------------------------- inside the namespace


class Namespace:
    def __init__(self, base: Path, tree: Path) -> None:
        self.base, self.tree = base, tree
        self.units = base / "units"
        self.config = base / "config"
        self.state = base / "state"
        self.runtime = Path("/run/switchyard-board-supervisor")
        self.log = base / "supervisor.log"
        self.process: subprocess.Popen | None = None

    def args(self) -> list[str]:
        return ["--config-root", str(self.config), "--state-root", str(self.state),
                "--runtime-root", str(self.runtime), "--unit-dir", str(self.units)]

    def cli(self, *argv: str, user: str = "root", timeout: float = 120) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-I", "-B", str(self.tree / "scripts" / "board_supervisor.py"), *self.args(), *argv]
        if user != "root":
            uid = USERS[user]
            command = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *command]
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              env={"PATH": os.environ["PATH"]})

    def start_supervisor(self) -> None:
        handle = open(self.log, "a")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.tree / "scripts" / "board_supervisor.py"), *self.args(), "serve"],
            stdout=handle, stderr=subprocess.STDOUT,
            env={"PATH": f"{self.base / 'bin'}:/usr/bin:/bin", "LANG": "C.UTF-8",
                 "FAKE_ACTIVE_UNITS": str(self.base / "active-units")})
        wait_for(lambda: self.runtime.joinpath("control.sock").exists(), 15)

    def stop_supervisor(self) -> int:
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=120)

    def status(self, project: str) -> dict:
        answer = self.cli("status", project, "--json")
        tenants = json.loads(answer.stdout or "[]")
        return tenants[0] if tenants else {}


def wait_for(predicate, timeout: float, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get(port: int, path: str = "/api/board") -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def setup_identities(base: Path) -> None:
    (base / "passwd").write_text("root:x:0:0::/root:/bin/sh\n" + "".join(
        f"{name}:x:{uid}:{uid}::/home/{name}:/usr/bin/nologin\n" for name, uid in USERS.items()))
    (base / "group").write_text("root:x:0:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid in USERS.items())
                                + f"shared:x:{SHARED_GID}:boardsvc\n")
    for name in ("passwd", "group"):
        subprocess.run(["mount", "--bind", str(base / name), f"/etc/{name}"], check=True)


def render_unit(tree: Path, project: str, port: int, *, extra: str = "") -> str:
    """The unit provisioning renders for this tenant, through its own renderer."""
    sys.path.insert(0, str(tree))
    from types import SimpleNamespace

    from scripts.ticket_board import provision_board_service

    home = f"/home/{project}" if project in USERS else "/home/alpha"
    owner = project if project in USERS else "alpha"
    plan = SimpleNamespace(
        project=project, project_name=f"{project.title()} Project", service_user="boardsvc",
        roles_group=owner, role_accounts=(), operation_allowed_roles=(),
        board_current=f"{home}/{project}-ticketboard-live/current", board_root=f"{home}/{project}-ticketboard-live",
        runtime_directory=f"{project}-ticket-board", port=port,
        socket_path=f"/run/{project}-ticket-board/ticket-board.sock",
        frame_dir=f"{home}/.claude/{project}-ticket-frames", asset_dir=f"{home}/.claude/{project}-tickets-assets",
        commit_git_dir=f"{home}/{project}-cache.git", owner_home=home, owner_user=owner,
        ticket_prefix=project.upper(), database=f"{project}_ticket_board", service_role="ticket_board_service",
        board_database_url=f"postgresql:///{project}_ticket_board?host=/var/run/postgresql&user=ticket_board_service",
        draft_roles=("designer",), implementer_roles=("main",), assignee_roles=("main", "audit"),
        caller_roles=("director", "main"),
    )
    os.environ["TICKET_BOARD_PYTHON"] = sys.executable
    text = provision_board_service.render_board_unit(plan)
    if extra:
        text = text.replace("[Install]", f"{extra}\n\n[Install]") if extra.startswith("[") else \
            text.replace("NoNewPrivileges=true", f"{extra}\nNoNewPrivileges=true")
    # Faster than the shipped 30s, so the suite can watch restarts happen.
    return text.replace("RestartSec=30s", "RestartSec=1s").replace("StartLimitIntervalSec=5min", "StartLimitIntervalSec=60s")


def setup_tenant(ns, project: str, *, version: str = "v1", extra: str = "", owner: str = "",
                 record: bool = True) -> None:
    owner = owner or (project if project in USERS else "alpha")
    home = Path(f"/home/{owner}")
    live = home / f"{project}-ticketboard-live"
    release = live / "releases" / version
    (release / "scripts").mkdir(parents=True, exist_ok=True)
    (release / "scripts" / "ticket-board.py").write_text(FIXTURE_BOARD)
    (release / "VERSION").write_text(version + "\n")
    current = live / "current"
    if current.is_symlink():
        current.unlink()
    current.symlink_to(release)
    for directory in (home / ".claude" / f"{project}-tickets-assets", home / ".claude" / f"{project}-ticket-frames"):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, USERS[owner], USERS[owner])
        directory.chmod(0o775)
    env_dir = home / ".config" / project
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / "ticket-board.env"
    env_file.write_text(f"TICKET_BOARD_TENANT_REPORT_TOKEN=secret-{project}\nFROM_ENV_FILE=\"{project} file\"\n"
                        "PYTHONUNBUFFERED=from-file\n")
    os.chown(env_file, USERS[owner], USERS[owner])
    env_file.chmod(0o640)
    ns.units.mkdir(parents=True, exist_ok=True)
    unit = ns.units / f"{project}-ticket-board.service"
    unit.write_text(render_unit(ns.tree, project, PORTS[project], extra=extra))
    unit.chmod(0o644)
    if not record:
        return
    record = ns.config / "tenants" / f"{project}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"schema": "switchyard.board-supervisor.tenant.v1", "project": project,
                                  "owner": owner, "unit": f"{project}-ticket-board.service",
                                  "unit_was_enabled": True, "unit_was_active": True, "migrated_at": "fixture"}))
    record.chmod(0o644)


def inner(base: Path, tree: Path) -> int:
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/run"], check=True)
    Path("/run/user").mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/home"], check=True)
    setup_identities(base)
    for name, uid in USERS.items():
        Path(f"/home/{name}").mkdir(mode=0o755)
        os.chown(f"/home/{name}", uid, uid)
    (base / "bin").mkdir()
    (base / "bin" / "systemctl").write_text(FAKE_SYSTEMCTL)
    (base / "bin" / "systemctl").chmod(0o755)
    ns = Namespace(base, tree)
    setup_tenant(ns, "alpha")
    setup_tenant(ns, "beta")
    try:
        run_checks(ns)
    finally:
        if ns.process and ns.process.poll() is None:
            ns.stop_supervisor()
        print("---- supervisor log ----")
        print(ns.log.read_text() if ns.log.exists() else "(none)")
    print(json.dumps({"failures": failures, "checks": CHECKS}))
    return 1 if failures else 0


def run_checks(ns: Namespace) -> None:
    ns.start_supervisor()
    alpha = wait_for(lambda: get(PORTS["alpha"]), 20)
    beta = wait_for(lambda: get(PORTS["beta"]), 20)
    check(alpha and beta, "the supervisor started both tenants' boards from their records")
    if not (alpha and beta):
        return

    # -- identity, groups, environment, release ------------------------------------
    check(alpha["uid"] == 900 and alpha["gid"] == USERS["alpha"] and beta["gid"] == USERS["beta"],
          f"each board runs as boardsvc with its own tenant's group: alpha {alpha['uid']}:{alpha['gid']}, "
          f"beta {beta['uid']}:{beta['gid']}")
    check(alpha["groups"] == sorted({USERS["alpha"], SHARED_GID}) and USERS["beta"] not in alpha["groups"]
          and USERS["alpha"] not in beta["groups"],
          f"groups are systemd's (Group=, the user's /etc/group memberships, SupplementaryGroups=), never the neighbour's: "
          f"alpha {alpha['groups']}, beta {beta['groups']}")
    check(alpha["env"]["TICKET_BOARD_DATABASE_URL"].startswith("postgresql:///alpha_ticket_board")
          and beta["env"]["TICKET_BOARD_DATABASE_URL"].startswith("postgresql:///beta_ticket_board")
          and alpha["env"]["PGUSER"] == "ticket_board_service" and alpha["env"]["TICKET_BOARD_PROJECT"] == "alpha"
          and alpha["env"]["TICKET_BOARD_PROJECT_NAME"] == "Alpha Project",
          "each board has its own database URL and project identity from its own unit")
    check(alpha["env"]["TICKET_BOARD_TENANT_REPORT_TOKEN"] == "secret-alpha"
          and beta["env"]["TICKET_BOARD_TENANT_REPORT_TOKEN"] == "secret-beta"
          and alpha["env"]["FROM_ENV_FILE"] == "alpha file" and alpha["env"]["PYTHONUNBUFFERED"] == "from-file",
          "each board's credentials come from its own owner's environment file, which beats Environment= as in systemd")
    check(alpha["env"]["HOME"] == "/home/alpha" and alpha["env"]["USER"] == "boardsvc"
          and alpha["env"]["RUNTIME_DIRECTORY"] == "/run/alpha-ticket-board"
          and alpha["env"]["PATH"] == "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
          and alpha["env"].get("LANG") == "C.UTF-8" and "FAKE_ACTIVE_UNITS" not in alpha["env"],
          "the environment is systemd's, not the supervisor's: unit HOME, USER, RUNTIME_DIRECTORY, PATH, locale only")
    check(alpha["release"] == "/home/alpha/alpha-ticketboard-live/releases/v1"
          and alpha["cwd"] == "/home/alpha/alpha-ticketboard-live/releases/v1" and alpha["nnp"] == "1",
          f"each runs its own release, in its WorkingDirectory, with no_new_privs: {alpha['cwd']} nnp={alpha['nnp']}")
    runtime = os.stat("/run/alpha-ticket-board")
    check(runtime.st_uid == 900 and runtime.st_gid == USERS["alpha"] and (runtime.st_mode & 0o777) == 0o750
          and Path("/run/alpha-ticket-board/ticket-board.sock").exists(),
          f"its RuntimeDirectory is boardsvc:alpha 0750 and holds its socket: {runtime.st_uid}:{runtime.st_gid} "
          f"{runtime.st_mode & 0o777:o}")

    # -- the sandbox ---------------------------------------------------------------
    beta_assets = Path("/home/beta/.claude/beta-tickets-assets")
    beta_assets.chmod(0o777)  # so only the mount, not the mode, can stop alpha's board
    own = get(PORTS["alpha"], "/write?path=/home/alpha/.claude/alpha-tickets-assets/probe")
    other = get(PORTS["alpha"], f"/write?path={beta_assets}/probe")
    etc = get(PORTS["alpha"], "/write?path=/etc/probe")
    check(own == {"wrote": True} and other == {"wrote": False, "errno": 30} and etc == {"wrote": False, "errno": 30},
          f"alpha's board writes its own assets, and gets EROFS on beta's world-writable assets and /etc: "
          f"{own} {other} {etc}")
    beta_assets.chmod(0o775)

    # -- an unrequested clean exit is recovered; the neighbour is untouched ------------
    first = ns.status("alpha")
    beta_pid = ns.status("beta").get("pid")
    get(PORTS["alpha"], "/exit")
    recovered = wait_for(lambda: (lambda s: s if s.get("pid") not in (None, first.get("pid")) and s["state"] == "running"
                                  else None)(ns.status("alpha")), 20)
    check(recovered and recovered["restarts"] == 1 and recovered["last_exit"]["status"] == 0
          and recovered["last_exit"]["requested"] is False and recovered["last_exit"]["pid"] == first.get("pid"),
          f"a clean exit 0 the supervisor did not ask for is restarted: {recovered}")
    check(wait_for(lambda: get(PORTS["alpha"]), 10) and ns.status("beta").get("pid") == beta_pid,
          "alpha answers again and beta's board never restarted")
    log = ns.log.read_text()
    check(f"tenant=alpha pid={first.get('pid')} exited with status 0, which the supervisor did not request" in log
          and f"tenant=alpha pid={(recovered or {}).get('pid')} started (automatic restart 1)" in log,
          "the journal names the tenant and both child pids")
    check(f"tenant=alpha pid={first.get('pid')}: fixture board v1 pid {first.get('pid')} listening" in log,
          "the board's own output is tagged with its tenant and pid")

    # -- control is per tenant and per owner ------------------------------------------
    refused = ns.cli("stop", "alpha", user="beta")
    stranger = ns.cli("restart", "beta", user="stranger")
    check(refused.returncode == 1 and "only root and its owner may" in refused.stderr
          and stranger.returncode == 1 and get(PORTS["alpha"]) and get(PORTS["beta"]),
          f"another tenant's owner and a stranger are refused, and nothing stops: {refused.stderr.strip()!r}")
    anyone = ns.cli("status", user="stranger")
    check(anyone.returncode == 0 and "alpha: running" in anyone.stdout and "beta: running" in anyone.stdout,
          "anyone may read status")
    stopped = ns.cli("stop", "alpha", user="alpha")
    check(stopped.returncode == 0 and ns.status("alpha").get("state") == "stopped" and get(PORTS["alpha"]) is None
          and read(ns.state / "alpha.desired") == "stopped" and ns.status("beta").get("pid") == beta_pid,
          f"alpha's owner stops alpha; it is persisted stopped and beta keeps running: {stopped.stdout.strip()}")
    check(not Path("/run/alpha-ticket-board").exists(), "a stopped board's runtime directory is removed")
    time.sleep(2.5)
    check(ns.status("alpha").get("state") == "stopped" and (ns.status("alpha").get("last_exit") or {}).get("requested") is True,
          "an intentional stop is not restarted")

    # -- a supervisor restart keeps each tenant's desired state ----------------------------
    check(ns.stop_supervisor() == 0 and get(PORTS["beta"]) is None, "stopping the supervisor stops every board")
    check(not (ns.state / "beta.desired").exists() and read(ns.state / "alpha.desired") == "stopped",
          "and leaves each tenant's desired state as it was: beta never asked (so running), alpha stopped")
    ns.start_supervisor()
    check(wait_for(lambda: get(PORTS["beta"]), 20) and ns.status("alpha").get("state") == "stopped"
          and get(PORTS["alpha"]) is None,
          "after it starts again, beta runs and suspended alpha stays stopped")
    started = ns.cli("start", "alpha", user="alpha")
    check(started.returncode == 0 and wait_for(lambda: get(PORTS["alpha"]), 10), "its owner starts alpha again")

    # -- an upgrade restarts one tenant onto its new release -----------------------------
    beta_pid = ns.status("beta").get("pid")
    before = ns.status("alpha").get("pid")
    setup_tenant(ns, "alpha", version="v2")
    upgraded = ns.cli("restart", "alpha", user="alpha")
    after = wait_for(lambda: get(PORTS["alpha"]), 10) or {}
    check(upgraded.returncode == 0 and f"pid {before} -> " in upgraded.stdout and after.get("version") == "v2"
          and after.get("pid") != before and ns.status("beta").get("pid") == beta_pid,
          f"restart moves alpha onto its new release and leaves beta alone: {upgraded.stdout.strip()}")

    # -- the start limit, and an explicit restart clearing it ----------------------------
    setup_tenant(ns, "gamma", owner="alpha")
    Path("/home/alpha/gamma-ticketboard-live/releases/v1/EXIT_AT_START").write_text("")
    reloaded = ns.cli("reload")
    failed = wait_for(lambda: (lambda s: s if s.get("state") == "failed" else None)(ns.status("gamma")), 30)
    check("added gamma" in reloaded.stdout and failed and "starts within 60s" in failed["detail"],
          f"a board that keeps exiting is left failed at its start limit, not flapping: {failed}")
    Path("/home/alpha/gamma-ticketboard-live/releases/v1/EXIT_AT_START").unlink()
    cleared = ns.cli("restart", "gamma", user="alpha")
    check(cleared.returncode == 0 and wait_for(lambda: get(PORTS["gamma"]), 10),
          f"an explicit restart clears the limit: {cleared.stdout.strip() or cleared.stderr.strip()}")

    # -- refusals --------------------------------------------------------------------
    setup_tenant(ns, "delta", owner="alpha", extra="PrivateTmp=yes")
    (ns.base / "active-units").write_text("epsilon-ticket-board.service\n")
    setup_tenant(ns, "epsilon", owner="alpha")
    ns.cli("reload")
    delta = wait_for(lambda: (lambda s: s if s.get("detail") else None)(ns.status("delta")), 10) or {}
    epsilon = wait_for(lambda: (lambda s: s if s.get("detail") else None)(ns.status("epsilon")), 10) or {}
    check("[Service] PrivateTmp= is not something the supervisor applies" in delta.get("detail", "")
          and get(PORTS["delta"]) is None,
          f"a unit setting the supervisor cannot honour is refused, by name: {delta.get('detail')}")
    check("epsilon-ticket-board.service is active under systemd" in epsilon.get("detail", "")
          and get(PORTS["epsilon"]) is None,
          f"a tenant whose unit is still active is refused: {epsilon.get('detail')}")
    setup_tenant(ns, "zeta", owner="alpha")
    zeta_assets = Path("/home/alpha/.claude/zeta-tickets-assets")
    zeta_assets.rmdir()
    zeta_assets.symlink_to("/home/beta/.claude/beta-tickets-assets")
    ns.cli("reload")
    wait_for(lambda: ns.status("zeta").get("state") in ("backoff", "failed"), 10)
    check(get(PORTS["zeta"]) is None and "ReadWritePaths= entry /home/alpha/.claude/zeta-tickets-assets resolves "
          "through a link to /home/beta/.claude/beta-tickets-assets" in ns.log.read_text(),
          "a ReadWritePaths= entry that is a link to another tenant's directory refuses the start (stricter than systemd)")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
        idle.connect(str(ns.runtime / "control.sock"))
        idle.settimeout(30)
        started_idle = time.monotonic()
        try:
            dropped = idle.recv(1) == b""
        except OSError:
            dropped = False
        waited = time.monotonic() - started_idle
    check(dropped and 9 <= waited <= 25 and ns.cli("status", "beta").returncode == 0,
          f"a control client that never finishes its request is dropped after about ten seconds ({waited:.1f}s), "
          "and others are served meanwhile")
    env_file = Path("/home/alpha/.config/alpha/ticket-board.env")
    env_file.chmod(0o660)
    refused_env = ns.cli("restart", "alpha")
    check(refused_env.returncode == 1 and "writable by its group or others" in refused_env.stderr,
          f"a group-writable environment file refuses the start: {refused_env.stderr.strip()[:200]}")
    env_file.chmod(0o640)
    check(ns.cli("restart", "alpha").returncode == 0 and wait_for(lambda: get(PORTS["alpha"]), 10),
          "and the restart succeeds once it is fixed")

    # -- reload releases a tenant whose record is gone -------------------------------------
    gamma_pid = ns.status("gamma").get("pid")
    (ns.config / "tenants" / "gamma.json").unlink()
    released = ns.cli("reload")
    check("released gamma" in released.stdout and wait_for(lambda: get(PORTS["gamma"]) is None, 10)
          and wait_for(lambda: not ns.status("gamma"), 10),
          f"a tenant whose record is removed is stopped and forgotten (pid {gamma_pid})")
    published = json.loads(ns.runtime.joinpath("status.json").read_text())
    check(sorted(t["project"] for t in published["tenants"]) == ["alpha", "beta", "delta", "epsilon", "zeta"]
          and oct(ns.runtime.joinpath("status.json").stat().st_mode & 0o777) == "0o644",
          "status.json is world-readable and lists exactly the supervised tenants")


# ---------------------------------------------------------------- outside


def main() -> int:
    if "--inner" in sys.argv:
        index = sys.argv.index("--inner")
        return inner(Path(sys.argv[index + 1]), Path(sys.argv[index + 2]))
    base = Path(tempfile.mkdtemp(prefix="syrd586-sup."))
    try:
        base.chmod(0o755)
        tree = base / "tree"
        shutil.copytree(ROOT / "scripts", tree / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(Path(__file__), base / "board_supervisor_test.py")
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        ran = subprocess.run(
            ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private", "--net", "--pid",
             "--fork", "--mount-proc", sys.executable, "-B", str(base / "board_supervisor_test.py"),
             "--inner", str(base), str(tree)],
            capture_output=True, text=True, timeout=400)
        print(ran.stdout)
        if ran.stderr.strip():
            print(ran.stderr)
        result = {}
        for line in reversed(ran.stdout.splitlines()):
            if line.startswith("{\"failures\""):
                result = json.loads(line)
                break
        if not result:
            print("FAIL the namespace run reported no result")
            return 1
        if result["failures"] or ran.returncode:
            print(f"board_supervisor_test: {len(result['failures'])} of {result['checks']} checks failed")
            return 1
        print(f"board_supervisor_test: {result['checks']} checks ok")
        return 0
    finally:
        subprocess.run(["unshare", "--map-auto", "--map-root-user", sys.executable, "-c",
                        "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(base)])


if __name__ == "__main__":
    raise SystemExit(main())
