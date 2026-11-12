#!/usr/bin/env python3
"""SYRD-762: a runtime switch changes only the runtime; the role's identity is the one root's checked load held.

SYRD-757 holds every root configuration load to `root_role_account_refusal`. A runtime switch then re-reads the
tenant's file to write the projection (`runtime_projection_files` -> `workflow_launcher._tenant_json`), and, as root,
`_with_projected_role` rebuilt the switched role from that re-read -- its `run_as_user` included -- unchecked. A
tenant that rewrote its file between root's load and that re-read chose the account the rest of the switch acts
for: for a legacy configuration, the stopped-project repair (`stopped_project_model_repair.clear_role_records`) then
set aside a session record in that account's tree.

Here, as root in a user, mount, pid and network namespace with real uids: root's provisioning record and registry
entry are the product's, the configuration is the product's own made legacy (`role_state_isolation: false`), and
root's load passes SYRD-757's check. At the moment root re-reads the file for the projection, the owner -- in a child
that has become it -- rewrites the role: its account to another tenant, `root`, or a name root never made, or its
workdir into another tenant's home. `write_runtime_projection` then runs as the switch runs it, and its result is
handed to `clear_role_records` as the repair hands it.

Main's code (from a git archive) returns the role with the rewritten account, and the repair supersedes the other
tenant's session record. The candidate refuses before writing anything -- the file is exactly the owner's rewrite,
the other tenant's record untouched -- and an undisturbed switch still projects the new runtime as before.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "ce510a91ee01091abcd21397989da1ee995b2437"  # main before SYRD-762
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019
HOME = Path("/home") / OWNER
OTHER_HOME = Path("/home") / OTHER
CHECKOUT = HOME / "Projects" / "pie"
PROVISION = CHECKOUT / ".switchyard" / "provision"
REWRITES = {"foreign": {"run_as_user": OTHER}, "root": {"run_as_user": "root"}, "unmade": {"run_as_user": "ghost"},
            "workdir": {"workdir": str(OTHER_HOME)}}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def digest_tree(top: Path) -> dict:
    found = {}
    for path in [top, *sorted(top.rglob("*"))]:
        info = os.lstat(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.S_ISREG(info.st_mode) else ""
        found[str(path)] = [info.st_uid, f"{stat.S_IMODE(info.st_mode):04o}", digest]
    return found


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns (as `root_owner_from_record_test` does)."""
    new = stage / "newroot"
    new.mkdir()
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(new))
    for directory in ("usr", "etc", "dev", "proc", "tmp", "run", "home", "opt", "stage", "var/lib/switchyard",
                      "var/tmp", "stubs/bin", "stubs/sbin"):
        (new / directory).mkdir(parents=True, exist_ok=True)
    (new / "tmp").chmod(0o1777)
    for link, target in (("bin", "usr/bin"), ("sbin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib")):
        (new / link).symlink_to(target)
    sh("mount", "--rbind", "/usr", str(new / "usr"))
    sh("mount", "--rbind", "/dev", str(new / "dev"))
    sh("mount", "--bind", str(stage), str(new / "stage"))
    os.chroot(new)
    os.chdir("/")
    sh("mount", "-t", "proc", "proc", "/proc")


def as_owner(action) -> str:
    """Run `action()` in a child that has become the owner: what the tenant itself can do."""
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the owner's child
        os.close(reader)
        try:
            os.setgroups([OWNER_UID])
            os.setresgid(OWNER_UID, OWNER_UID, OWNER_UID)
            os.setresuid(OWNER_UID, OWNER_UID, OWNER_UID)
            action()
            said = "done"
        except BaseException as exc:  # noqa: BLE001
            said = f"{type(exc).__name__}: {exc}"
        os.write(writer, said.encode())
        os._exit(0)
    os.close(writer)
    out = b""
    while chunk := os.read(reader, 65536):
        out += chunk
    os.close(reader)
    os.waitpid(pid, 0)
    return out.decode()


def inner(root: Path, work: Path, mode: str) -> dict:
    sh("ip", "link", "set", "lo", "up")
    enter_root_owned_root(work)
    root = Path("/stage") / root.relative_to(work)
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))
    for place in ("/etc/switchyard", "/etc/systemd/system", "/etc/sudoers.d", "/etc/polkit-1/rules.d", "/etc/tmpfiles.d"):
        Path(place).mkdir(parents=True, exist_ok=True)
    Path("/stubs/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo inactive; exit 3;; esac\nexit 0\n')
    for cli in ("tmux", "claude", "codex", "sudo", "runuser"):
        Path(f"/stubs/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    for place in (Path("/stubs/sbin"), Path("/stubs/bin")):
        for stub in place.iterdir():
            stub.chmod(0o755)
    os.environ["PATH"] = "/stubs/sbin:/stubs/bin:/usr/bin:/bin"
    accounts = ((OWNER, OWNER_UID, HOME), (OTHER, OTHER_UID, OTHER_HOME))
    Path("/etc/passwd").write_text("root:x:0:0::/run/root-home:/bin/sh\nboardsvc:x:1500:1500::/nonexistent:/bin/false\n"
                                   + "".join(f"{n}:x:{u}:{u}::{h}:/bin/bash\n" for n, u, h in accounts), encoding="utf-8")
    Path("/etc/group").write_text("root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{n}:x:{u}:\n" for n, u, _ in accounts),
                                  encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
    for directory in (HOME / "Projects", CHECKOUT, CHECKOUT / ".switchyard", PROVISION):
        directory.mkdir(mode=0o700)
        os.chown(directory, OWNER_UID, OWNER_UID)

    sys.path.insert(0, str(release))
    from scripts import runtime_projection, stopped_project_model_repair, workflow_launcher
    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    plan = build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                      service_user="boardsvc", project_repository=CHECKOUT)
    launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
    config_path = launcher.write_new_project_launcher_artifacts(plan, PROVISION, repository=CHECKOUT,
                                                                project_name=NAME, print_func=lambda *a, **k: None)
    registry = Path("/etc/switchyard/projects")
    registry.mkdir(exist_ok=True)
    (registry / f"{SLUG}.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": SLUG,
                                                        "name": NAME, "config_path": str(config_path)}))
    generated = json.loads(config_path.read_text(encoding="utf-8"))
    legacy = json.loads(json.dumps(generated))
    legacy["role_state_isolation"] = False
    for role in legacy["roles"]:
        if role["role"] == "main":
            role["cli"] = ["codex"]

    # The other tenant's session record for this project's main role, where a legacy role account's records are.
    target = next(role["target"] for role in legacy["roles"] if role["role"] == "main")
    records = OTHER_HOME / ".local" / "state" / f"{SLUG}-ticket-board" / "pane-sessions"
    records.mkdir(parents=True)
    record = records / launcher.session_file_name(target)
    record.write_text(json.dumps({"target": target, "session_id": "otto-session", "cli": "codex"}), encoding="utf-8")
    for path in [OTHER_HOME / ".local", *sorted((OTHER_HOME / ".local").rglob("*"))]:
        os.chown(path, OTHER_UID, OTHER_UID)

    def reset() -> None:
        config_path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
        os.chown(config_path, OWNER_UID, OWNER_UID)

    def switch(rewrite: dict | None) -> dict:
        reset()
        config = launcher.load_project_config(SLUG, config_path)  # root's checked load
        real = workflow_launcher._tenant_json
        said: list[str] = []

        def tenant_json(path, owner_user):
            if rewrite is not None and Path(path) == config_path and not said:
                def action() -> None:
                    document = json.loads(config_path.read_text(encoding="utf-8"))
                    for role in document["roles"]:
                        if role["role"] == "main":
                            role.update(rewrite)
                    config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
                said.append(as_owner(action))
            return real(path, owner_user)

        workflow_launcher._tenant_json = tenant_json
        before_other = digest_tree(OTHER_HOME)
        rewritten = None
        try:
            updated = runtime_projection.write_runtime_projection(
                config, config_path=config_path, role_name="main", runtime="claude", runner=subprocess.run)
            role = launcher._role_by_name(updated, "main")
            answer = {"returned": True, "account": launcher.role_run_as_user(updated, role), "workdir": role.workdir}
            try:
                stopped_project_model_repair.clear_role_records(updated, role, pane_state_dir=Path("/tmp/pane-state"))
                answer["repair"] = "done"
            except BaseException as exc:  # noqa: BLE001
                answer["repair"] = f"{type(exc).__name__}: {exc}"[-300:]
        except BaseException as exc:  # noqa: BLE001
            answer = {"returned": False, "refused": f"{type(exc).__name__}: {exc}"[-500:]}
        finally:
            workflow_launcher._tenant_json = real
        main = next(r for r in json.loads(config_path.read_text(encoding="utf-8"))["roles"] if r["role"] == "main")
        answer.update(tamper=said[0] if said else "-", file_cli=main.get("cli"), file_account=main.get("run_as_user"),
                      other_unchanged=digest_tree(OTHER_HOME) == before_other,
                      other_record=record.exists())
        return answer

    out = {name: switch(rewrite) for name, rewrite in REWRITES.items()}
    out["undisturbed"] = switch(None)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd762.") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        code = stage / "tree"
        for part in ("scripts", "config", "examples", "skills", "docs"):
            if (tree / part).exists():
                shutil.copytree(tree / part, code / part, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage), str(code)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp, mode], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1200, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    got = out["foreign"]
    check(got["returned"] and got["account"] == OTHER and not got["other_record"] and not got["other_unchanged"],
          f"before: the role comes back with the account the owner wrote after root's load, and the repair sets aside "
          f"the other tenant's session record: {got}")
    for name in ("root", "unmade"):
        got = out[name]
        check(got["returned"] and got["account"] == REWRITES[name]["run_as_user"],
              f"before, {name}: the rewritten account is what root holds: {got}")
    got = out["workdir"]
    check(got["returned"] and got["workdir"] == str(OTHER_HOME),
          f"before: and a rewritten workdir into another tenant's home: {got}")


def candidate(out: dict) -> None:
    for name, rewrite in REWRITES.items():
        got = out[name]
        field = next(iter(rewrite))
        check(got["tamper"] == "done" and not got["returned"] and "RoleRuntimeRefusal" in got["refused"]
              and f"now gives main another {field} than root read for this switch; a runtime switch changes only the "
                  "runtime. Nothing was written." in got["refused"],
              f"{name}: the owner's rewrite between root's load and the projection is refused, naming the field: {got}")
        check(got["file_cli"] == ["codex"] and got["other_unchanged"] and got["other_record"],
              f"{name}: nothing was written -- the file is the owner's rewrite, still codex -- and the other tenant's "
              f"tree is untouched: {got}")
    got = out["undisturbed"]
    check(got["returned"] and got["account"] == OWNER and got["file_cli"] == ["claude"] and got["other_unchanged"],
          f"an undisturbed switch projects the new runtime and keeps the owner's account, as before: {got}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD762_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd762-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"projection_identity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
