#!/usr/bin/env python3
"""SYRD-586: moving tenant boards from their units to the host supervisor, and back, at the real paths.

Everything runs as root in a user namespace (see board_supervisor_test.py),
at the paths production uses. Fresh tmpfs mounts stand in for
`/etc/systemd/system`, `/etc/switchyard`, `/var/lib/switchyard`, `/run` and
`/home`, and this tree is bound over `/opt/switchyard/current`'s release, so
the commands run with no path overrides.

The one stand-in is `systemctl`. It is a small model of systemd's unit
manager: enabled and active state per unit, starting a unit's `ExecStart=`
with its `Environment=`, and `AssertPathExists=!` drop-ins refusing a start.
The supervisor itself is started the way `install` leaves it started: the
model runs the `ExecStart=` of the unit `install` wrote.

Covered:
- `install`, then migrating a running tenant; the board then answers from the
  supervisor's child, and systemd refuses the old unit;
- `switchyard status`, `board_services` (suspend, resume and upgrade's paths)
  and `ticket-board-service.sh` reaching the supervised board, for its owner
  and for nobody else;
- rollback onto the unit;
- a suspended tenant migrating as stopped;
- a failed migration undoing itself;
- a foreign drop-in refusing the move;
- teardown releasing a supervised board.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parents[1]
UNITS = Path("/etc/systemd/system")
CONFIG = Path("/etc/switchyard/board-supervisor")
STATE = Path("/var/lib/switchyard/board-supervisor")
JOURNAL = Path("/var/lib/switchyard/rollout")
failures: list[str] = []
CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(("ok   " if condition else "FAIL ") + message, flush=True)
    if not condition:
        failures.append(message)


FAKE_SYSTEMCTL = r'''#!/usr/bin/python3
"""A model of the unit manager, enough for the board supervisor's migration (SYRD-586 test)."""
import os, shlex, signal, subprocess, sys, time
from pathlib import Path
UNITS = Path("/etc/systemd/system")
STATE = Path("/run/fake-systemd")
STATE.mkdir(exist_ok=True)
with (STATE / "calls.log").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
args = [a for a in sys.argv[1:] if a not in ("--quiet", "--no-pager", "-l")]
quiet = "--quiet" in sys.argv

def alive(unit):
    try:
        pid = int((STATE / f"{unit}.pid").read_text())
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    return pid if stat.rsplit(")", 1)[1].split()[0] != "Z" else None

def enabled(unit):
    return (STATE / f"{unit}.enabled").exists()

def unit_lines(unit):
    return [line.strip() for line in (UNITS / unit).read_text().splitlines()]

def start(unit):
    if alive(unit):
        return 0
    for conf in sorted((UNITS / f"{unit}.d").glob("*.conf")) if (UNITS / f"{unit}.d").is_dir() else []:
        for line in conf.read_text().splitlines():
            if line.startswith("AssertPathExists=!") and Path(line.split("!", 1)[1]).exists():
                print(f"Assertion failed on job for {unit}.", file=sys.stderr)
                return 1
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
    exec_start, cwd = "", "/"
    for line in unit_lines(unit):
        key, _, value = line.partition("=")
        if key == "ExecStart":
            exec_start = value
        elif key == "Environment":
            for word in shlex.split(value):
                name, _, item = word.partition("=")
                env[name] = item
        elif key == "WorkingDirectory":
            cwd = value
        elif key == "RuntimeDirectory":
            Path("/run", value).mkdir(exist_ok=True)
    out = open(STATE / f"{unit}.log", "a")
    process = subprocess.Popen(shlex.split(exec_start), env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    (STATE / f"{unit}.pid").write_text(str(process.pid))
    time.sleep(0.5)
    return 0 if alive(unit) else 1

def stop(unit):
    pid = alive(unit)
    if pid:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while alive(unit) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(unit):
            os.killpg(pid, signal.SIGKILL)
    (STATE / f"{unit}.pid").unlink(missing_ok=True)
    for line in unit_lines(unit):
        if line.startswith("RuntimeDirectory="):
            subprocess.run(["rm", "-rf", "--", str(Path("/run", line.split("=", 1)[1]))])
    return 0

verb = args[0]
now = "--now" in args
units = [a for a in args[1:] if a.endswith(".service")]
code = 0
if verb == "is-active":
    state = "active" if alive(units[0]) else "inactive"
    if not quiet:
        print(state)
    code = 0 if state == "active" else 3
elif verb == "is-enabled":
    print("enabled" if enabled(units[0]) else "disabled")
    code = 0 if enabled(units[0]) else 1
elif verb == "enable":
    (STATE / f"{units[0]}.enabled").write_text("")
    code = start(units[0]) if now else 0
elif verb == "disable":
    (STATE / f"{units[0]}.enabled").unlink(missing_ok=True)
    code = stop(units[0]) if now else 0
elif verb in ("start", "stop", "restart"):
    if verb in ("stop", "restart"):
        stop(units[0])
    code = start(units[0]) if verb != "stop" else 0
elif verb == "show":
    prop = args[args.index("-p") + 1]
    if prop == "DropInPaths" and (UNITS / f"{units[0]}.d").is_dir():
        print(" ".join(str(p) for p in sorted((UNITS / f"{units[0]}.d").glob("*.conf"))))
    else:
        print("")
elif verb in ("daemon-reload", "reset-failed", "status"):
    code = 0
else:
    print(f"fake systemctl: unexpected {sys.argv[1:]}", file=sys.stderr)
    code = 2
sys.exit(code)
'''


def supervisor_cli(tree: Path, *argv: str, user: str = "root", timeout: float = 200) -> subprocess.CompletedProcess[str]:
    from board_supervisor_test import USERS

    command = [sys.executable, "-I", "-B", str(tree / "scripts" / "board_supervisor.py"), *argv]
    if user != "root":
        command = ["setpriv", f"--reuid={USERS[user]}", f"--regid={USERS[user]}", "--clear-groups", *command]
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=dict(os.environ))


def systemctl(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", *argv], capture_output=True, text=True, timeout=60)


def unit_state(unit: str) -> tuple[str, str]:
    return systemctl("is-enabled", unit).stdout.strip(), systemctl("is-active", unit).stdout.strip()


def attempt(function, *args, **kwargs):
    """A call's result, or the exception it raised as a value, so a wrong path fails a check, not the run."""
    try:
        return function(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - SystemExit and the no-systemctl guard alike
        return exc


def journal_attempts(project: str) -> list[dict]:
    index = JOURNAL / project / "index.jsonl"
    try:
        return [json.loads(line) for line in index.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def last_attempt_text(project: str) -> tuple[dict, str]:
    entries = journal_attempts(project)
    finish = [entry for entry in entries if entry.get("event") == "finish"]
    if not finish:
        return {}, ""
    directory = JOURNAL / project / finish[-1]["attempt"]
    return finish[-1], (directory / "stdout.log").read_text()


def inner(base: Path, tree: Path) -> int:
    sys.path.insert(0, str(base))
    sys.path.insert(0, str(tree))
    support = importlib.import_module("board_supervisor_test")
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    release = Path(os.path.realpath("/opt/switchyard/current"))
    check(release.is_dir() and release != Path("/opt/switchyard/current"),
          f"precondition: /opt/switchyard/current is a link to a release directory ({release})")
    for target in ("/run", "/home", "/etc/switchyard", "/var/lib/switchyard", "/etc/systemd/system"):
        subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", target], check=True)
    subprocess.run(["mount", "--bind", str(tree), str(release)], check=True)
    Path("/run/user").mkdir()
    support.setup_identities(base)
    for name, uid in support.USERS.items():
        Path(f"/home/{name}").mkdir(mode=0o755)
        os.chown(f"/home/{name}", uid, uid)
    bin_dir = base / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(FAKE_SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    os.environ.pop("SWITCHYARD_ROLLOUT_JOURNAL_ROOT", None)
    layout = SimpleNamespace(units=UNITS, config=CONFIG, tree=tree)
    for project in ("alpha", "beta"):
        support.setup_tenant(layout, project, record=False)
    support.setup_tenant(layout, "gamma", owner="alpha", record=False)
    support.setup_tenant(layout, "delta", owner="alpha", record=False)
    (UNITS / "delta-ticket-board.service.d").mkdir()
    (UNITS / "delta-ticket-board.service.d" / "override.conf").write_text("[Service]\nEnvironment=EXTRA=1\n")
    for project in ("alpha", "gamma", "delta"):
        systemctl("enable", "--now", f"{project}-ticket-board.service")
    try:
        run_checks(tree, support)
    finally:
        supervisor_cli(tree, "status")
        systemctl("stop", "switchyard-board-supervisor.service")
        for project in ("alpha", "beta", "gamma", "delta"):
            systemctl("stop", f"{project}-ticket-board.service")
        log = Path("/run/fake-systemd/switchyard-board-supervisor.service.log")
        print("---- supervisor log ----")
        print(log.read_text() if log.exists() else "(none)")
    print(json.dumps({"failures": failures, "checks": CHECKS}))
    return 1 if failures else 0


def run_checks(tree: Path, support) -> None:
    get, wait_for, PORTS = support.get, support.wait_for, support.PORTS
    unit_answer = get(PORTS["alpha"])
    check(unit_answer and unit_answer["uid"] == 0 and unit_state("alpha-ticket-board.service") == ("enabled", "active"),
          "before: alpha's board is its unit's process (root here, in the model) and the unit is enabled and active")

    # -- install ---------------------------------------------------------------
    planned = supervisor_cli(tree, "install")
    check(planned.returncode == 0 and "plan; nothing was changed" in planned.stdout
          and not (UNITS / "switchyard-board-supervisor.service").exists(),
          "install without --apply only plans")
    previous_umask = os.umask(0o077)
    installed = supervisor_cli(tree, "install", "--apply")
    os.umask(previous_umask)
    modes = {str(path): oct(path.stat().st_mode & 0o777) for path in (CONFIG, CONFIG / "tenants", STATE)}
    check(set(modes.values()) == {"0o755"}, f"install leaves its directories readable whatever root's umask: {modes}")
    unit_text = (UNITS / "switchyard-board-supervisor.service").read_text() if \
        (UNITS / "switchyard-board-supervisor.service").exists() else ""
    from scripts import board_supervisor_migration as migration

    check(installed.returncode == 0 and unit_text == migration.render_supervisor_unit()
          and "ExecStart=/usr/bin/python3 -I -B /opt/switchyard/current/scripts/board_supervisor.py serve" in unit_text
          and unit_state("switchyard-board-supervisor.service") == ("enabled", "active"),
          f"install --apply writes the rendered unit and the supervisor runs from its ExecStart: "
          f"{installed.stdout.strip()[-200:]} {installed.stderr.strip()[-300:]}")
    host = journal_attempts("_host")
    check([entry["event"] for entry in host] == ["start", "finish"] and host[-1]["status"] == "succeeded",
          f"the install is recorded in the host's rollout journal: {[e.get('event') for e in host]}")

    # -- migrate a running tenant --------------------------------------------------
    plan = supervisor_cli(tree, "migrate", "alpha")
    check(plan.returncode == 0 and "owner: alpha (the unit's TICKET_BOARD_TENANT_USER)" in plan.stdout
          and "so the supervisor will keep it running" in plan.stdout and "problem:" not in plan.stdout
          and not (CONFIG / "tenants" / "alpha.json").exists()
          and unit_state("alpha-ticket-board.service") == ("enabled", "active"),
          f"migrate without --apply plans, names the owner, and changes nothing: {plan.stdout.strip()[:400]}")
    applied = supervisor_cli(tree, "migrate", "alpha", "--apply")
    record = json.loads((CONFIG / "tenants" / "alpha.json").read_text()) if (CONFIG / "tenants" / "alpha.json").exists() else {}
    status = json.loads(supervisor_cli(tree, "status", "alpha", "--json").stdout or "[]")
    answer = wait_for(lambda: get(PORTS["alpha"]), 10)
    check(applied.returncode == 0 and record.get("owner") == "alpha" and record.get("unit_was_enabled") is True
          and status and status[0]["state"] == "running" and answer and answer["uid"] == 900
          and answer["pid"] == status[0]["pid"],
          f"migrate --apply hands alpha to the supervisor; the board now answers from its child as boardsvc: "
          f"{applied.stdout.strip()[-300:]} {applied.stderr.strip()[-300:]}")
    check(unit_state("alpha-ticket-board.service") == ("disabled", "inactive")
          and (UNITS / "alpha-ticket-board.service.d" / "50-switchyard-board-supervisor.conf").exists()
          and (STATE / "alpha.desired").read_text().strip() == "running",
          "its unit is disabled and inactive, carries the drop-in, and the desired state is running")
    refused = systemctl("start", "alpha-ticket-board.service")
    check(refused.returncode != 0 and "Assertion failed" in refused.stderr and get(PORTS["alpha"])["uid"] == 900,
          "a stray `systemctl start` of the old unit is refused and the supervised board keeps serving")
    finish, text = last_attempt_text("alpha")
    check(finish.get("status") == "succeeded" and f"child pid {status[0]['pid'] if status else '?'} running" in text
          and "2. alpha-ticket-board.service is disabled and stopped" in text,
          f"the rollout journal records the migration, naming the child pid: {text[-300:]}")

    # -- status, board_services and the deploy script reach the supervised board ------
    from scripts import board_services, project_status

    lines = attempt(project_status.board_supervisor_lines, ["alpha", "gamma"])
    check(isinstance(lines, list) and lines and lines[0] == "BOARD SERVICES (supervised by switchyard-board-supervisor.service)"
          and any(line.startswith(f"  alpha: running (desired running), pid {answer['pid']}") for line in lines)
          and "  gamma: gamma-ticket-board.service (not supervised)" in lines,
          f"switchyard status names the supervised tenant and its child pid: {lines}")
    facts = attempt(project_status.root_recorded_tenant_facts, "alpha")
    check(isinstance(facts, tuple) and facts[0] is True and project_status.root_recorded_tenant_facts("gamma")[0] is True,
          "root-recorded facts read alpha from the supervisor (its unit is inactive) and gamma from its unit")

    def no_systemctl(*args, **kwargs):
        raise AssertionError(f"systemctl reached for a supervised board: {args}")

    config = SimpleNamespace(project="alpha")
    stopped = attempt(board_services._board_system_unit_action, config, "stop", runner=no_systemctl)
    check(stopped == [] and get(PORTS["alpha"]) is None
          and attempt(board_services.board_system_unit_is_active, config, runner=no_systemctl) is False
          and (STATE / "alpha.desired").read_text().strip() == "stopped",
          f"suspending a supervised board stops the supervisor's child, never the unit: {stopped}")
    started = attempt(board_services._board_system_unit_action, config, "start", runner=no_systemctl)
    check(started == [] and wait_for(lambda: get(PORTS["alpha"]), 10)
          and attempt(board_services.board_system_unit_is_active, config, runner=no_systemctl) is True,
          f"resuming starts it again: {started}")
    before = get(PORTS["alpha"])["pid"]
    activated = attempt(board_services.activate_board_authority, config, runner=no_systemctl, restart=True, print_func=print)
    check(activated == [] and wait_for(lambda: (get(PORTS["alpha"]) or {}).get("pid") not in (None, before), 10),
          "an upgrade's activation restarts the supervised board")
    shell = tree / "scripts" / "ticket-board-service.sh"
    owner_env = {"PATH": os.environ["PATH"], "HOME": "/home/alpha", "TICKET_BOARD_PROJECT": "alpha",
                 "TICKET_BOARD_OWNER_HOME": "/home/alpha", "TICKET_BOARD_COMMIT_GIT_DIR": "/home/alpha/alpha-cache.git"}
    as_owner = subprocess.run(["setpriv", "--reuid=1001", "--regid=1001", "--clear-groups", "bash", "-c",
                               f'source {shell}; systemctl_system restart "$SERVICE_NAME"'],
                              capture_output=True, text=True, env=owner_env, timeout=200)
    as_other = subprocess.run(["setpriv", "--reuid=1002", "--regid=1002", "--clear-groups", "bash", "-c",
                               f'source {shell}; systemctl_system stop "$SERVICE_NAME"'],
                              capture_output=True, text=True, env=dict(owner_env, HOME="/home/beta"), timeout=200)
    check(as_owner.returncode == 0 and "restart alpha: pid" in as_owner.stdout and as_other.returncode != 0
          and "only root and its owner may" in as_other.stderr and get(PORTS["alpha"]),
          f"the deploy script restarts the supervised board for its owner and refuses anyone else: "
          f"{as_owner.stdout.strip()[-200:]} {as_owner.stderr.strip()[-200:]} / {as_other.stderr.strip()[-200:]}")
    stop_cmd = subprocess.run(["setpriv", "--reuid=1001", "--regid=1001", "--clear-groups", "bash", str(shell), "stop"],
                              capture_output=True, text=True, env=owner_env, timeout=200)
    status_cmd = subprocess.run(["setpriv", "--reuid=1001", "--regid=1001", "--clear-groups", "bash", str(shell), "status"],
                                capture_output=True, text=True, env=owner_env, timeout=200)
    check(stop_cmd.returncode == 0 and get(PORTS["alpha"]) is None and "alpha: stopped (desired stopped)" in status_cmd.stdout,
          f"`ticket-board-service.sh stop` and `status` act on the supervisor's child: {status_cmd.stdout.strip()}")
    supervisor_cli(tree, "start", "alpha", user="alpha")
    wait_for(lambda: get(PORTS["alpha"]), 10)
    from scripts import project_role_plan_support

    calls: list[list[str]] = []

    def role_add_runner(argv, **kwargs):
        calls.append(list(argv))
        if argv[:3] == ["sudo", "systemctl", "restart"]:
            raise AssertionError("role-add restarted the supervised board's unit")
        return subprocess.CompletedProcess(argv, 0, "", "")

    before = get(PORTS["alpha"])["pid"]
    restarted = attempt(project_role_plan_support._install_and_restart_board_unit,
                        SimpleNamespace(project="alpha", board_unit="alpha-ticket-board.service"),
                        board_unit_path=UNITS / "alpha-ticket-board.service", runner=role_add_runner)
    check(restarted is None and [call[:3] for call in calls] == [["sudo", "install", "-m"], ["sudo", "systemctl", "daemon-reload"]]
          and wait_for(lambda: (get(PORTS["alpha"]) or {}).get("pid") not in (None, before), 10),
          f"adding a role installs the unit and restarts the supervised board through the supervisor: {restarted!r} {calls}")
    check(board_services.SUPERVISOR_UNIT == importlib.import_module("scripts.board_supervisor_spec").SUPERVISOR_UNIT
          and "switchyard-board-supervisor.service" in board_services.managed_unit_names(config),
          "residual-process containment treats the supervisor's control group as managed")

    # -- rollback ------------------------------------------------------------------
    plan = supervisor_cli(tree, "rollback", "alpha")
    check(plan.returncode == 0 and "enable alpha-ticket-board.service; start it" in plan.stdout
          and (CONFIG / "tenants" / "alpha.json").exists(),
          f"rollback without --apply plans and changes nothing: {plan.stdout.strip()[:300]}")
    rolled = supervisor_cli(tree, "rollback", "alpha", "--apply")
    unit_answer = wait_for(lambda: get(PORTS["alpha"]), 10)
    check(rolled.returncode == 0 and not (CONFIG / "tenants" / "alpha.json").exists()
          and not (STATE / "alpha.desired").exists()
          and not (UNITS / "alpha-ticket-board.service.d").exists()
          and unit_state("alpha-ticket-board.service") == ("enabled", "active")
          and unit_answer and unit_answer["uid"] == 0
          and json.loads(supervisor_cli(tree, "status", "alpha", "--json").stdout or "[1]") == [],
          f"rollback --apply returns alpha to its unit, and the supervisor forgets it: {rolled.stdout.strip()[-300:]} "
          f"{rolled.stderr.strip()[-300:]}")
    finish, text = last_attempt_text("alpha")
    check(finish.get("status") == "succeeded" and "rolled back alpha to alpha-ticket-board.service" in text,
          "the rollback is recorded in the tenant's rollout journal")

    # -- a suspended tenant migrates stopped ----------------------------------------------
    applied = supervisor_cli(tree, "migrate", "beta", "--apply")
    status = json.loads(supervisor_cli(tree, "status", "beta", "--json").stdout or "[]")
    check(applied.returncode == 0 and status and status[0]["state"] == "stopped" and status[0]["desired"] == "stopped"
          and get(PORTS["beta"]) is None and unit_state("beta-ticket-board.service") == ("disabled", "inactive"),
          f"a tenant whose unit was inactive is supervised stopped and stays down: {applied.stdout.strip()[-200:]}")

    # -- a failed migration undoes itself ------------------------------------------------------
    Path("/home/alpha/gamma-ticketboard-live/releases/v1/HANG_AT_START").write_text("")
    failed = supervisor_cli(tree, "migrate", "gamma", "--apply", "--verify-timeout", "4")
    Path("/home/alpha/gamma-ticketboard-live/releases/v1/HANG_AT_START").unlink()
    check(failed.returncode == 1 and "failed and was undone" in failed.stdout
          and not (CONFIG / "tenants" / "gamma.json").exists() and not (STATE / "gamma.desired").exists()
          and not (UNITS / "gamma-ticket-board.service.d" / "50-switchyard-board-supervisor.conf").exists()
          and unit_state("gamma-ticket-board.service") == ("enabled", "active")
          and json.loads(supervisor_cli(tree, "status", "gamma", "--json").stdout or "[1]") == [],
          f"a migration whose board never answers is undone and gamma's unit runs again: {failed.stdout.strip()[-400:]}")
    finish, _ = last_attempt_text("gamma")
    check(finish.get("status") == "failed" and "failed and was undone" in finish.get("detail", ""),
          f"the failed attempt is recorded as failed: {finish.get('detail')}")

    # -- a foreign drop-in refuses the move ----------------------------------------------------
    plan = supervisor_cli(tree, "migrate", "delta")
    applied = supervisor_cli(tree, "migrate", "delta", "--apply")
    check(plan.returncode == 1 and "override.conf change the unit under systemd" in plan.stdout
          and applied.returncode == 1 and not (CONFIG / "tenants" / "delta.json").exists()
          and unit_state("delta-ticket-board.service") == ("enabled", "active") and not journal_attempts("delta"),
          "a unit with someone else's drop-in is not migrated, and nothing is changed or journaled")

    # -- teardown releases a supervised board ----------------------------------------------------
    from scripts import project_teardown, team_launcher

    supervisor_cli(tree, "start", "beta")
    wait_for(lambda: get(PORTS["beta"]), 10)
    plan_fields = SimpleNamespace(
        project="beta", owner_user="beta", database="beta_ticket_board", board_root="/home/beta/beta-ticketboard-live",
        board_unit="beta-ticket-board.service", listener_unit="beta-ticket-board-notify-listener.service",
        tmpfiles_name="beta.conf", polkit_name="50-beta.rules", tenant_control_sudoers_name="47-beta-control")
    actions = attempt(project_teardown._switchyard_teardown_actions,
        plan_fields, registry_path=Path("/r/beta.json"), home_base=Path("/home"),
        remove_owner_home=False, remove_owner_user=False)
    unsupervised = project_teardown._switchyard_teardown_actions(
        SimpleNamespace(**dict(vars(plan_fields), project="gamma", board_unit="gamma-ticket-board.service")),
        registry_path=Path("/r/gamma.json"), home_base=Path("/home"), remove_owner_home=False, remove_owner_user=False)
    actions = actions if isinstance(actions, tuple) else (SimpleNamespace(label=repr(actions), command=("false",)),)
    check(actions[0].label.startswith("release beta's board from switchyard-board-supervisor.service")
          and actions[1].label == "stop and disable system board service beta-ticket-board.service"
          and unsupervised[0].label == "stop and disable system board service gamma-ticket-board.service"
          and len(actions) == len(unsupervised) + 1,
          f"teardown releases a supervised board first, and an unsupervised tenant's plan is unchanged: {actions[0].label}")
    released = subprocess.run(list(actions[0].command), capture_output=True, text=True, timeout=200)
    check(released.returncode == 0 and get(PORTS["beta"]) is None and not (CONFIG / "tenants" / "beta.json").exists()
          and not (UNITS / "beta-ticket-board.service.d").exists()
          and json.loads(supervisor_cli(tree, "status", "beta", "--json").stdout or "[1]") == [],
          f"running that step stops the child and removes every supervisor record of beta: {released.stdout[-200:]} "
          f"{released.stderr[-200:]}")
    check(team_launcher.PROJECT_SLUG_RE.pattern == importlib.import_module("scripts.board_supervisor_spec").PROJECT_SLUG_RE.pattern,
          "the supervisor's slug rule is the launcher's")


def main() -> int:
    if "--inner" in sys.argv:
        index = sys.argv.index("--inner")
        return inner(Path(sys.argv[index + 1]), Path(sys.argv[index + 2]))
    base = Path(tempfile.mkdtemp(prefix="syrd586-mig."))
    try:
        base.chmod(0o755)
        tree = base / "tree"
        for part in ("scripts", "config", "deploy"):
            if (ROOT / part).exists():
                shutil.copytree(ROOT / part, tree / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("board_supervisor_test.py", "board_supervisor_migration_test.py"):
            shutil.copy2(Path(__file__).with_name(name), base / name)
        subprocess.run(["chmod", "-R", "a+rX", str(base)], check=True)
        ran = subprocess.run(
            ["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private", "--net", "--pid",
             "--fork", "--mount-proc", sys.executable, "-B", str(base / "board_supervisor_migration_test.py"),
             "--inner", str(base), str(tree)],
            capture_output=True, text=True, timeout=500)
        print(ran.stdout)
        if ran.stderr.strip():
            print(ran.stderr)
        result = next((json.loads(line) for line in reversed(ran.stdout.splitlines()) if line.startswith('{"failures"')), {})
        if not result:
            print("FAIL the namespace run reported no result")
            return 1
        if result["failures"] or ran.returncode:
            print(f"board_supervisor_migration_test: {len(result['failures'])} of {result['checks']} checks failed")
            return 1
        print(f"board_supervisor_migration_test: {result['checks']} checks ok")
        return 0
    finally:
        subprocess.run(["unshare", "--map-auto", "--map-root-user", sys.executable, "-c",
                        "import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", str(base)])


if __name__ == "__main__":
    raise SystemExit(main())
