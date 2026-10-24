#!/usr/bin/env python3
"""SYRD-656: presentation-state writes leave the board service's traverse to the control repository working.

On PIE the control repository and the presentation state share
`~/.local/state/switchyard/projects/pie`. `repair-boundary` gives the board
service a named ACL traverse down to `control.git`. The next presentation
write -- `_write_private_json_atomic`, as root (`parent_mode=0700`) or as the
owner (`_ensure_private_dir`) -- set that directory to 0700, which sets the
ACL mask to nothing: the named traverse stayed listed and stopped working,
and the board could not verify commits again until the next manual repair.

Here, as root in a user and mount namespace with real uids and an ACL-capable
tmpfs: the owner's home and a real bare control repository, the board service
account and the tenant's roles group are real; the boundary is put in place
by `repair-boundary`'s own apply core -- the boundary phase taken from the
packet `render_operator_commands` renders for the plan, every statement run
through `repository_boundary_repair._run_helper` -- and
`repository_boundary_problems` must be empty. Then two ordinary launches'
state writes, each once as root for the owner and once as the owner itself:
`_locked_state` and `_write_state`, as `launch_presentation` runs them, and a
role-runtime journal's `RuntimeJournal.write`. The boundary must stay closed
and the traverse effective after every one. The before-run is main's code.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "ae5d4eed98c0758c6d1b2ed439ba030406ce5a24"  # main before SYRD-656
OWNER, OWNER_UID, SERVICE, SERVICE_UID, ROLES, ROLES_GID = "pie-agent", 1018, "boardsvc", 1020, "pie-roles", 1021
HOME = Path("/run/h") / OWNER
STATE = HOME / ".local" / "state" / "switchyard" / "projects" / "pie"
CONTROL = STATE / "control.git"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def acl(path: Path) -> list[str]:
    done = subprocess.run(["getfacl", "-n", "-p", "-E", str(path)], capture_output=True, text=True)
    return sorted(line for line in done.stdout.splitlines() if line and not line.startswith("#"))


def inner(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", "/run")
    passwd, group = work / "passwd", work / "group"
    passwd.write_text(f"root:x:0:0::/root:/bin/sh\n{OWNER}:x:{OWNER_UID}:{OWNER_UID}::{HOME}:/bin/sh\n"
                      f"{SERVICE}:x:{SERVICE_UID}:{SERVICE_UID}::/run/boardsvc:/bin/sh\n", encoding="utf-8")
    group.write_text(f"root:x:0:\n{OWNER}:x:{OWNER_UID}:\n{SERVICE}:x:{SERVICE_UID}:\n{ROLES}:x:{ROLES_GID}:{SERVICE}\n",
                     encoding="utf-8")
    sh("mount", "--bind", str(passwd), "/etc/passwd")
    sh("mount", "--bind", str(group), "/etc/group")
    os.makedirs("/run/h", mode=0o755)
    # The owner's home and state, closed to everybody else, and a real bare control repository in it.
    for directory in (HOME, *[HOME / p for p in (".local", ".local/state", ".local/state/switchyard",
                                                 ".local/state/switchyard/projects")], STATE):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chown(directory, OWNER_UID, OWNER_UID)
        os.chmod(directory, 0o700)
    sh("git", "init", "-q", "--bare", str(CONTROL))
    sh("chown", "-R", f"{OWNER_UID}:{OWNER_UID}", str(CONTROL))
    config_path = STATE / "pie.json"
    config_path.write_text("{}\n", encoding="utf-8")
    os.chown(config_path, OWNER_UID, OWNER_UID)

    from scripts import presentation_state_document as presentation
    from scripts import role_runtime
    from scripts import team_launcher as launcher
    from scripts.repository_boundary_repair import _run_helper
    from scripts.ticket_board import project_provision as provision
    from scripts.ticket_board.provision_operator_packet import render_operator_commands
    from scripts.ticket_board.provision_repository_boundary import (repository_boundary_phase,
                                                                     repository_boundary_statements)
    from scripts.ticket_board.provision_tenant_paths import TENANT_PATHS_PREFIX

    plan = provision.build_plan(project="pie", owner_user=OWNER, owner_home=HOME, commit_git_dir=CONTROL)
    out: dict = {"plan": {"service_user": plan.service_user, "roles_group": plan.roles_group,
                          "commit_git_dir": plan.commit_git_dir}}

    def problems() -> list[str]:
        return [str(p) for p in launcher.repository_boundary_problems(plan, runner=subprocess.run)]

    def observed(label: str) -> dict:
        status = os.stat(STATE)
        return {"label": label, "problems": problems(), "mode": f"{stat.S_IMODE(status.st_mode):04o}",
                "acl": acl(STATE), "files": sorted(p.name for p in STATE.iterdir())}

    out["before_repair"] = observed("before repair")
    phase, phase_problem = repository_boundary_phase(render_operator_commands(plan))
    said: list[str] = []
    refusals = [r for r in (_run_helper(s, TENANT_PATHS_PREFIX, preview=False, planned=set(), say=said.append)
                            for s in repository_boundary_statements(phase)) if r]
    out["repair"] = {"phase_problem": phase_problem, "statements": len(phase), "refusals": refusals,
                     **observed("after repair")}

    config = SimpleNamespace(run_as_user=OWNER, project="pie", repository=None)
    state_path = presentation.presentation_state_path(config, config_path=config_path)
    journal_path = role_runtime.journal_path_for(config, config_path=config_path, role_name="ops")
    out["paths"] = [str(state_path), str(journal_path)]

    def launch_writes(n: int) -> None:
        """One ordinary launch's state writes, as launch_presentation and a runtime switch make them."""
        with presentation._locked_state(state_path, config=config, runner=subprocess.run):
            presentation._write_state(config, state_path, {"schema": "test", "launch": n, "slots": []},
                                      runner=subprocess.run)
        role_runtime.RuntimeJournal(project="pie", role="ops", owner_user=OWNER, config_path=str(config_path),
                                    steps_applied=[f"launch {n}"]).write(journal_path)

    def as_owner(n: int) -> str:
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the owner's child
            os.close(reader)
            said = b""
            try:
                os.setgroups([OWNER_UID])
                os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
                os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
                launch_writes(n)
            except BaseException as exc:  # noqa: BLE001
                said = repr(exc).encode()
            os.write(writer, said)
            os._exit(0)
        os.close(writer)
        result = os.read(reader, 65536).decode()
        os.close(reader)
        os.waitpid(pid, 0)
        return result

    for n in (1, 2):
        try:
            launch_writes(n)
            failed = ""
        except BaseException as exc:  # noqa: BLE001
            failed = repr(exc)
        out[f"launch{n}_root"] = {"failed": failed, **observed(f"launch {n} as root for the owner")}
        out[f"launch{n}_owner"] = {"failed": as_owner(n), **observed(f"launch {n} as the owner")}
    # A project whose state directory does not exist yet: both writers still make it the owner's alone.
    import inspect

    out["fresh"] = None  # main's writer has no parent policy to ask for
    if "private_parent" in inspect.signature(launcher._write_private_json_atomic).parameters:
        fresh = {"root": STATE.parent / "fresh-root" / "presentation.json", "owner": STATE.parent / "fresh-owner" / "presentation.json"}
        launcher._write_private_json_atomic(fresh["root"], {"slots": []}, owner_user=OWNER, private_parent=False)
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the owner's child
            code = 0
            try:
                os.setgroups([OWNER_UID]); os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID); os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
                launcher._write_private_json_atomic(fresh["owner"], {"slots": []}, owner_user=OWNER, private_parent=False)
            except BaseException:  # noqa: BLE001
                code = 1
            os._exit(code)
        os.waitpid(pid, 0)
        out["fresh"] = {name: [os.stat(path.parent).st_uid, f"{stat.S_IMODE(os.stat(path.parent).st_mode):04o}"]
                        if path.parent.exists() else None for name, path in fresh.items()}
    out["written"] = {name: [os.stat(path).st_uid, f"{stat.S_IMODE(os.stat(path).st_mode):04o}",
                             json.loads(path.read_text()).get("launch", json.loads(path.read_text()).get("steps_applied"))]
                      for name, path in (("state", state_path), ("journal", journal_path))}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd656.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                sys.executable, str(Path(__file__).resolve()), "--inner", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=600)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


STEPS = ("launch1_root", "launch1_owner", "launch2_root", "launch2_owner")


def before(out: dict) -> None:
    check(out["repair"]["problems"] == [], f"before: repair-boundary closes the boundary: {out['repair']}")
    after = out["launch1_root"]
    check(after["problems"] and "cannot read its commit store" in after["problems"][0] and after["mode"] == "0700"
          and "mask::---" in after["acl"],
          f"before: the first presentation write sets the shared directory 0700, the ACL mask to nothing, and the "
          f"board service can no longer reach control.git: {after}")


def candidate(out: dict) -> None:
    repair = out["repair"]
    check(out["before_repair"]["problems"] and repair["phase_problem"] == "" and repair["refusals"] == []
          and repair["problems"] == [],
          f"the boundary starts open, and repair-boundary's own apply closes it: {out['before_repair']} {repair}")
    granted = [line for line in repair["acl"] if line.startswith(f"user:{SERVICE_UID}:")]
    check(granted and "x" in granted[0] and any(line.startswith("mask::") and "x" in line for line in repair["acl"]),
          f"it does so with a named traverse for the board service, effective under the mask: {repair['acl']}")
    for name in STEPS:
        step = out[name]
        check(step["failed"] == "" and step["problems"] == [] and step["mode"] == repair["mode"]
              and step["acl"] == repair["acl"],
              f"{step['label']}: the write succeeds, and the shared directory keeps repair-boundary's mode and ACL, "
              f"so the boundary stays closed with the board's traverse effective: {step}")
    check(out["fresh"] == {"root": [OWNER_UID, "0700"], "owner": [OWNER_UID, "0700"]},
          f"a state directory that does not exist yet is made the owner's, 0700, by either writer: {out['fresh']}")
    check(out["written"] == {"state": [OWNER_UID, "0600", 2], "journal": [OWNER_UID, "0600", ["launch 2"]]},
          f"the state and the journal are written, the owner's, 0600: {out['written']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD656_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd656-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"presentation_state_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
