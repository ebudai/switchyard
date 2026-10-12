"""Installing the board supervisor, and moving one tenant's board onto it and back (SYRD-586).

Each command plans by default and changes nothing; `--apply` runs it as root.
Each run is recorded in the tenant's rollout journal, or in `_host`'s for the
install, through `rollout_journal.Attempt`.

- `install`: writes `switchyard-board-supervisor.service`, then enables and
  starts it. With no tenant records it supervises nothing, so installing it
  changes no tenant.
- `migrate <p>`: moves one tenant, and touches no other.
  1. installs the drop-in that makes systemd refuse to start
     `<p>-ticket-board.service` while the tenant record exists, then reloads systemd;
  2. disables and stops that unit; the board is down from here;
  3. writes the desired state and the tenant record;
  4. has the supervisor reload, which starts the child;
  5. verifies the child runs, the board answers on its own port, and the unit
     stays inactive with the drop-in loaded.
  If a step after the first fails, the steps already taken are undone in
  reverse and the unit is started again, so a failed migration leaves the
  tenant as it was.
- `rollback <p>`: the reverse. It stops the child and removes the record, the
  desired state and the drop-in. It enables the unit again if it was enabled,
  starts it if the tenant was meant to be running, and verifies the board answers.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from scripts import board_supervisor as supervisor_mod
from scripts import board_supervisor_spec as spec_mod
from scripts.ticket_board import rollout_journal

INSTALLED_SCRIPT = "/opt/switchyard/current/scripts/board_supervisor.py"
VERIFY_TIMEOUT_SECONDS = 60.0
HOST_JOURNAL = "_host"


def render_supervisor_unit(script: str = INSTALLED_SCRIPT) -> str:
    """The one host service. It runs from the shared release, as every Switchyard command does.

    `KillMode=mixed` sends the stop to the supervisor alone, which stops each
    board in turn and leaves each desired state as it was. The kernel's SIGKILL
    reaches whatever is left only after the stop times out.
    """
    return f"""[Unit]
Description=Switchyard board supervisor (one isolated child per tenant board)
Documentation=file:///opt/switchyard/current/docs/board-supervisor.md
After=network.target postgresql.service
StartLimitIntervalSec=5min
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I -B {script} serve
Restart=always
RestartSec=5s
KillMode=mixed
TimeoutStopSec=150s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def render_drop_in(project: str, paths: spec_mod.Paths) -> str:
    return f"""# Written by switchyard-board-supervisor migrate {project} (SYRD-586).
# While {paths.tenant_record(project)} exists, the supervisor runs this board,
# and systemd refuses to start a second copy of it on the same port and socket.
# `switchyard-board-supervisor rollback {project} --apply` removes this file.
[Unit]
AssertPathExists=!{paths.tenant_record(project)}
"""


def write_root_file(path: Path, text: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def default_probe(port: int, timeout: float) -> tuple[bool, str]:
    """The board answers its own /api/board, as the deploy smoke check asks."""
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=2.0) as response:
                if response.status == 200:
                    return True, f"http://127.0.0.1:{port}/api/board answered 200"
                last = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(0.5)
    return False, f"http://127.0.0.1:{port}/api/board did not answer within {timeout:g}s: {last}"


class Run:
    """Commands, output and the journal for one migrate, rollback or install."""

    def __init__(self, project: str, argv: list[str], *, apply: bool,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 out: Callable[[str], None] = print) -> None:
        self.project, self.apply, self.runner, self._out = project, apply, runner, out
        self.attempt = rollout_journal.Attempt(project, argv) if apply else None
        if self.attempt is not None:
            self.attempt.open()

    def say(self, text: str) -> None:
        self._out(text)
        if self.attempt is not None:
            self.attempt.write("stdout", text + "\n")

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        result = self.runner(["systemctl", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, timeout=120)
        self.say(f"  $ systemctl {' '.join(args)} -> exit {result.returncode}"
                 + (f": {result.stderr.strip()[:300]}" if result.returncode and result.stderr else ""))
        return result

    def query(self, *args: str) -> str:
        result = self.runner(["systemctl", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=30)
        return str(result.stdout or "").strip()

    def finish(self, ok: bool, detail: str) -> int:
        self.say(f"switchyard-board-supervisor: {detail}")
        if self.attempt is not None:
            self.attempt.close(status="succeeded" if ok else "failed", exit_status=0 if ok else 1, detail=detail)
            self._out(f"switchyard-board-supervisor: recorded in {self.attempt.directory}")
        return 0 if ok else 1


def resolve_owner(spec: spec_mod.ChildSpec, override: str) -> tuple[str, str]:
    """(owner account, where it came from). The account allowed to control this tenant's board."""
    if override:
        return override, "--owner"
    named = dict(spec.environment).get("TICKET_BOARD_TENANT_USER", "")
    if named:
        return named, "the unit's TICKET_BOARD_TENANT_USER"
    root = Path(spec.working_directory).parent
    try:
        owner = pwd.getpwuid(root.stat().st_uid).pw_name
    except (OSError, KeyError):
        return "", ""
    return ("", "") if owner == "root" else (owner, f"the owner of {root}")


def migration_problems(project: str, paths: spec_mod.Paths, owner_override: str) -> tuple[list[str], dict]:
    facts: dict = {}
    problems: list[str] = []
    if not spec_mod.valid_project(project):
        return [f"{project!r} is not a project slug"], facts
    if paths.tenant_record(project).exists():
        problems.append(f"{project} is already supervised ({paths.tenant_record(project)} exists)")
    try:
        spec = spec_mod.read_unit_spec(project, paths)
    except spec_mod.SpecError as exc:
        return problems + [str(exc)], facts
    facts["spec"] = spec
    foreign = spec_mod.foreign_drop_ins(project, paths)
    if foreign:
        problems.append(f"drop-ins {', '.join(map(str, foreign))} change the unit under systemd and the "
                        "supervisor would not apply them; fold them into the unit or remove them first")
    owner, source = resolve_owner(spec, owner_override)
    try:
        owner_uid = pwd.getpwnam(owner).pw_uid if owner else None
    except KeyError:
        owner_uid = None
    if owner_uid is None:
        problems.append(f"no owner account for {project} could be resolved{f' ({owner!r})' if owner else ''}; "
                        "pass --owner")
    facts["owner"], facts["owner_source"] = owner, source
    for path, _optional in spec.environment_files:
        try:
            spec_mod.read_environment_file(path, allowed_owners={0, owner_uid if owner_uid is not None else 0})
        except spec_mod.SpecError as exc:
            if "Permission denied" in str(exc) and os.geteuid() != 0:
                facts.setdefault("unchecked", []).append(f"{path} is unreadable to this account; --apply checks it as root")
            else:
                problems.append(str(exc))
    for path in spec.read_write_paths:
        if os.path.exists(path) and os.path.realpath(path) != os.path.normpath(path):
            problems.append(f"ReadWritePaths= entry {path} resolves through a link to {os.path.realpath(path)}; "
                            "the supervisor refuses that")
        if not os.path.exists(path):
            if os.geteuid() == 0:
                problems.append(f"ReadWritePaths= entry {path} does not exist")
            else:
                facts.setdefault("unchecked", []).append(f"{path} is not visible to this account")
    if spec.port is None:
        problems.append(f"{spec.unit_path} names no --port, so the migration cannot verify the board answers")
    return problems, facts


def describe_spec(spec: spec_mod.ChildSpec) -> list[str]:
    return [
        f"  unit: {spec.unit_path}",
        f"  child: user {spec.user}, gid {spec.gid}, groups {list(spec.groups)}, cwd {spec.working_directory}",
        f"  release {spec.release() or '-'}, port {spec.port}, socket {spec.socket or '-'}",
        f"  runtime directory /run/{spec.runtime_directory or '-'} ({spec.runtime_directory_mode:04o}); "
        f"ProtectSystem={spec.protect_system}, ProtectHome={spec.protect_home}, "
        f"ReadWritePaths={' '.join(spec.read_write_paths) or '-'}, NoNewPrivileges={spec.no_new_privileges}",
        f"  restart after {spec.restart_sec:g}s, at most {spec.start_limit_burst} starts in "
        f"{spec.start_limit_interval:g}s; stop timeout {spec.stop_timeout:g}s",
        *(f"  note: {note}" for note in spec.notes),
    ]


def wait_for_child(project: str, paths: spec_mod.Paths, request: Callable[..., dict], timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            answer = request({"verb": "status", "project": project}, paths=paths, timeout=10.0)
        except supervisor_mod.SupervisorUnavailable:
            answer = {}
        tenants = answer.get("tenants") or []
        if tenants and tenants[0].get("state") == "running" and tenants[0].get("pid"):
            return tenants[0]
        if tenants and tenants[0].get("state") == "failed":
            return tenants[0]
        time.sleep(0.5)
    return None


def wait_until_released(project: str, paths: spec_mod.Paths, request: Callable[..., dict], timeout: float) -> bool:
    """Whether the supervisor has let go of the tenant: its child has exited and it is no longer listed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            answer = request({"verb": "status", "project": project}, paths=paths, timeout=10.0)
        except supervisor_mod.SupervisorUnavailable:
            return False
        if not answer.get("tenants"):
            return True
        time.sleep(0.5)
    return False


def migrate(project: str, paths: spec_mod.Paths, *, apply: bool, owner_override: str = "",
            runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            request: Callable[..., dict] = supervisor_mod.request,
            probe: Callable[[int, float], tuple[bool, str]] = default_probe,
            verify_timeout: float = VERIFY_TIMEOUT_SECONDS, out: Callable[[str], None] = print) -> int:
    problems, facts = migration_problems(project, paths, owner_override)
    if apply and os.geteuid() != 0:
        problems.append("--apply runs as root")
    if apply and not problems:
        try:
            request({"verb": "status"}, paths=paths, timeout=10.0)
        except supervisor_mod.SupervisorUnavailable as exc:
            problems.append(f"{exc}; run `switchyard-board-supervisor install --apply` first")
    unit = paths.unit_name(project)
    run = Run(project, ["switchyard-board-supervisor", "migrate", project, "--apply"],
              apply=apply and not problems, runner=runner, out=out)
    was_enabled = run.query("is-enabled", unit) == "enabled"
    was_active = run.query("is-active", unit) == "active"
    # A tenant whose board is not running now -- suspended, or stopped by an
    # operator -- is supervised stopped, and stays that way until someone starts it.
    desired = "running" if was_active else "stopped"
    if not apply:
        out(f"switchyard-board-supervisor: migrate {project} (plan; nothing was changed)")
        if "spec" in facts:
            out("\n".join(describe_spec(facts["spec"])))
            out(f"  owner: {facts['owner'] or '-'} ({facts['owner_source'] or 'unresolved'}); root and the owner "
                "may start, stop and restart it")
        out(f"  {unit} is {'enabled' if was_enabled else 'not enabled'} and {'active' if was_active else 'inactive'}, "
            f"so the supervisor will keep it {desired}")
        for note in facts.get("unchecked", []):
            out(f"  unchecked: {note}")
        for problem in problems:
            out(f"  problem: {problem}")
        out(f"  steps with --apply, as root:\n"
            f"    1. install {paths.drop_in_path(project)} and daemon-reload\n"
            f"    2. systemctl disable --now {unit}   (the board is down from here)\n"
            f"    3. write {paths.desired_path(project)} ({desired}) and {paths.tenant_record(project)}\n"
            f"    4. the supervisor reloads{' and starts the child' if desired == 'running' else ''}\n"
            f"    5. verify: {'child running, the board answers on its port, ' if desired == 'running' else 'the tenant is supervised and stopped, '}"
            f"{unit} inactive with the drop-in loaded\n"
            f"    a failure after step 1 undoes the steps in reverse and starts {unit} again\n"
            f"  roll back later with: switchyard-board-supervisor rollback {project} --apply")
        return 1 if problems else 0
    if problems:
        for problem in problems:
            out(f"switchyard-board-supervisor: migrate {project}: {problem}")
        return 1
    spec: spec_mod.ChildSpec = facts["spec"]
    run.say(f"switchyard-board-supervisor: migrating {project} from {unit} "
            f"({'enabled' if was_enabled else 'not enabled'}, {'active' if was_active else 'inactive'})")
    for line in describe_spec(spec):
        run.say(line)
    undo: list[Callable[[], None]] = []

    def unwind(reason: str) -> int:
        run.say(f"switchyard-board-supervisor: {reason}; undoing the migration")
        for step in reversed(undo):
            step()
        return run.finish(False, f"migrate {project} failed and was undone: {reason}")

    write_root_file(paths.drop_in_path(project), render_drop_in(project, paths))
    run.say(f"1. installed {paths.drop_in_path(project)}")
    undo.append(lambda: (paths.drop_in_path(project).unlink(missing_ok=True), run.systemctl("daemon-reload")))
    if run.systemctl("daemon-reload").returncode != 0:
        return unwind("systemd did not reload")
    stopped = run.systemctl("disable", "--now", unit)
    undo.append(lambda: (run.systemctl("enable", unit) if was_enabled else None,
                         run.systemctl("start", unit) if was_active else None))
    if stopped.returncode != 0 or run.query("is-active", unit) == "active":
        return unwind(f"{unit} did not stop")
    run.say(f"2. {unit} is disabled and stopped")
    record = spec_mod.TenantRecord(project=project, owner=facts["owner"], unit=unit, unit_was_enabled=was_enabled,
                                   unit_was_active=was_active, migrated_at=supervisor_mod.now_text())
    write_root_file(paths.desired_path(project), desired + "\n")
    write_root_file(paths.tenant_record(project), json.dumps(record.as_json(), indent=2) + "\n")
    run.say(f"3. recorded {paths.tenant_record(project)} (owner {facts['owner']}) and desired state {desired}")

    def release() -> None:
        paths.tenant_record(project).unlink(missing_ok=True)
        paths.desired_path(project).unlink(missing_ok=True)
        try:
            request({"verb": "reload"}, paths=paths, timeout=30.0)
        except supervisor_mod.SupervisorUnavailable as exc:
            run.say(f"  the supervisor did not answer the reload: {exc}")
        # The unit is started again next, so the child must be gone first:
        # two boards cannot share one port and socket.
        if not wait_until_released(project, paths, request, spec.stop_timeout + 15):
            run.say(f"  the supervisor has not confirmed {project}'s child exited")
    undo.append(release)
    try:
        answer = request({"verb": "reload"}, paths=paths, timeout=30.0)
    except supervisor_mod.SupervisorUnavailable as exc:
        return unwind(str(exc))
    run.say(f"4. supervisor reload: {', '.join(answer.get('changed') or []) or 'no change'}")
    if desired == "stopped":
        try:
            listed = request({"verb": "status", "project": project}, paths=paths, timeout=10.0).get("tenants") or []
        except supervisor_mod.SupervisorUnavailable as exc:
            return unwind(str(exc))
        if not listed or listed[0].get("state") != "stopped":
            return unwind(f"the supervisor does not list {project} as stopped: {listed}")
        if run.query("is-active", unit) == "active":
            return unwind(f"{unit} is active under systemd")
        return run.finish(True, f"migrated {project}: supervised and stopped, as its unit was; start it with "
                                f"`switchyard-board-supervisor start {project}`")
    tenant = wait_for_child(project, paths, request, verify_timeout)
    if tenant is None or tenant.get("state") != "running":
        return unwind(f"the supervisor did not run {project}: "
                      + (supervisor_mod.describe_tenant(tenant) if tenant else "no running child in time"))
    answered, detail = probe(spec.port, verify_timeout)
    run.say(f"5. child pid {tenant['pid']} running; {detail}")
    if not answered:
        return unwind(detail)
    if run.query("is-active", unit) == "active":
        return unwind(f"{unit} is active under systemd as well")
    if str(paths.drop_in_path(project)) not in run.query("show", unit, "-p", "DropInPaths", "--value"):
        return unwind(f"systemd does not report {paths.drop_in_path(project)} loaded for {unit}")
    run.say(f"   {unit} is inactive and carries the drop-in")
    return run.finish(True, f"migrated {project}: supervised child pid {tenant['pid']}; "
                            f"roll back with `switchyard-board-supervisor rollback {project} --apply`")


def rollback(project: str, paths: spec_mod.Paths, *, apply: bool,
             runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
             request: Callable[..., dict] = supervisor_mod.request,
             probe: Callable[[int, float], tuple[bool, str]] = default_probe,
             unit_active: Callable[[str], bool] = supervisor_mod.default_unit_active,
             verify_timeout: float = VERIFY_TIMEOUT_SECONDS, out: Callable[[str], None] = print) -> int:
    problems: list[str] = []
    unit = paths.unit_name(project)
    try:
        record = spec_mod.read_tenant_record(project, paths)
    except spec_mod.SpecError as exc:
        record = None
        problems.append(f"{project} is not supervised: {exc}")
    try:
        spec = spec_mod.read_unit_spec(project, paths)
    except spec_mod.SpecError as exc:
        spec = None
        problems.append(f"{unit} cannot run the board again: {exc}")
    desired = "running"
    try:
        desired = paths.desired_path(project).read_text(encoding="utf-8").strip() or "running"
    except OSError:
        pass
    if apply and os.geteuid() != 0:
        problems.append("--apply runs as root")
    start_unit = desired == "running"
    if not apply:
        out(f"switchyard-board-supervisor: rollback {project} (plan; nothing was changed)")
        for problem in problems:
            out(f"  problem: {problem}")
        enable = f"enable {unit}" if record and record.unit_was_enabled else f"leave {unit} disabled"
        start = "start it (desired running)" if start_unit else "leave it stopped (desired stopped)"
        verify = f"verify {unit} is active and the board answers" if start_unit else f"leave {unit} inactive"
        out(f"  steps with --apply, as root:\n"
            f"    1. the supervisor stops {project}'s child   (the board is down from here)\n"
            f"    2. remove {paths.tenant_record(project)} and {paths.desired_path(project)}; the supervisor reloads\n"
            f"    3. remove {paths.drop_in_path(project)} and daemon-reload\n"
            f"    4. {enable}; {start}\n"
            f"    5. {verify}")
        return 1 if problems else 0
    if problems:
        for problem in problems:
            out(f"switchyard-board-supervisor: rollback {project}: {problem}")
        return 1
    run = Run(project, ["switchyard-board-supervisor", "rollback", project, "--apply"], apply=True,
              runner=runner, out=out)
    try:
        answer = request({"verb": "stop", "project": project}, paths=paths)
        run.say(f"1. supervisor stop: {supervisor_mod.describe_tenant(answer['tenant']) if answer.get('tenant') else answer}")
        if not answer.get("ok"):
            return run.finish(False, f"rollback {project} stopped at step 1: {answer.get('error')}; nothing else changed")
    except supervisor_mod.SupervisorUnavailable as exc:
        if unit_active(spec_mod.SUPERVISOR_UNIT):
            return run.finish(False, f"rollback {project} stopped at step 1: {exc}, while "
                                     f"{spec_mod.SUPERVISOR_UNIT} is active; nothing changed")
        run.say(f"1. {spec_mod.SUPERVISOR_UNIT} is not running, so no child of it is")
    paths.tenant_record(project).unlink(missing_ok=True)
    paths.desired_path(project).unlink(missing_ok=True)
    try:
        answer = request({"verb": "reload"}, paths=paths, timeout=30.0)
        run.say(f"2. removed the tenant record and desired state; supervisor reload: "
                f"{', '.join(answer.get('changed') or []) or 'no change'}")
    except supervisor_mod.SupervisorUnavailable:
        run.say("2. removed the tenant record and desired state")
    paths.drop_in_path(project).unlink(missing_ok=True)
    try:
        paths.drop_in_path(project).parent.rmdir()
    except OSError:
        pass
    run.systemctl("daemon-reload")
    run.say(f"3. removed {paths.drop_in_path(project)}")
    if record.unit_was_enabled:
        run.systemctl("enable", unit)
    if not start_unit:
        return run.finish(True, f"rolled back {project} to {unit}, left stopped as the tenant was")
    run.systemctl("start", unit)
    if run.query("is-active", unit) != "active":
        return run.finish(False, f"rollback {project}: {unit} did not become active; the tenant has no board")
    answered, detail = probe(spec.port, verify_timeout) if spec.port else (False, "the unit names no port")
    run.say(f"4. {unit} is active; {detail}")
    return run.finish(answered, f"rolled back {project} to {unit}" if answered
                      else f"rollback {project}: {unit} is active but the board does not answer: {detail}")


def install(paths: spec_mod.Paths, *, apply: bool, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
            request: Callable[..., dict] = supervisor_mod.request, script: str = INSTALLED_SCRIPT,
            out: Callable[[str], None] = print) -> int:
    unit_path = paths.unit_dir / spec_mod.SUPERVISOR_UNIT
    if not apply:
        out(f"switchyard-board-supervisor: install (plan; nothing was changed)\n"
            f"  1. write {unit_path} running {script}\n"
            f"  2. create {paths.tenants_dir()} and {paths.state_root}\n"
            f"  3. systemctl daemon-reload; systemctl enable --now {spec_mod.SUPERVISOR_UNIT}\n"
            f"  4. verify it answers on {paths.control_socket()}\n"
            "  It supervises no tenant until one is migrated, so this changes no tenant.")
        return 0
    if os.geteuid() != 0:
        out("switchyard-board-supervisor: install --apply runs as root")
        return 1
    if not Path(script).is_file():
        out(f"switchyard-board-supervisor: {script} does not exist; install a Switchyard release that has it first")
        return 1
    run = Run(HOST_JOURNAL, ["switchyard-board-supervisor", "install", "--apply"], apply=True, runner=runner, out=out)
    write_root_file(unit_path, render_supervisor_unit(script))
    # Readable by everyone whatever root's umask: a tenant's own deploy decides
    # whether its board is supervised by whether its record exists.
    for directory in (paths.config_root, paths.tenants_dir(), paths.state_root):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o755)
    run.say(f"wrote {unit_path}")
    run.systemctl("daemon-reload")
    if run.systemctl("enable", "--now", spec_mod.SUPERVISOR_UNIT).returncode != 0:
        return run.finish(False, f"{spec_mod.SUPERVISOR_UNIT} did not start")
    deadline = time.monotonic() + 30.0
    while True:
        try:
            answer = request({"verb": "status"}, paths=paths, timeout=5.0)
            break
        except supervisor_mod.SupervisorUnavailable as exc:
            if time.monotonic() >= deadline:
                return run.finish(False, f"{spec_mod.SUPERVISOR_UNIT} is not answering: {exc}")
            time.sleep(0.5)
    return run.finish(True, f"{spec_mod.SUPERVISOR_UNIT} is running and supervising "
                            f"{len(answer.get('tenants') or [])} tenant(s)")


def command(args: argparse.Namespace, paths: spec_mod.Paths) -> int:
    if args.command == "render-unit":
        sys.stdout.write(render_supervisor_unit())
        return 0
    if args.command == "install":
        return install(paths, apply=args.apply)
    if args.command == "migrate":
        return migrate(args.project, paths, apply=args.apply, owner_override=args.owner,
                       verify_timeout=args.verify_timeout)
    return rollback(args.project, paths, apply=args.apply, verify_timeout=args.verify_timeout)
