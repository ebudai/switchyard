#!/usr/bin/env python3
"""SYRD-759: root writes `new`'s artifacts by path only into a directory nobody but root can change.

SYRD-666 refused root's DRY-RUN writes into another tenant's tree. The executing forms still wrote by path into an
operator-named directory outside the new owner's roots, and a `--from` artifact outside them was written back by
path; `team-launcher`'s own root dispatch also skipped the root-record check every `switchyard` entry point runs
(SYRD-663), so a configuration root holds no record of chose its owner -- blank, which is root.

Here, as root in a user, mount, pid and network namespace with real uids, another tenant (`otto-agent`) plants links
in its own tree to root-owned places, and the routes run as root:

- `team-launcher <p> new --execute --new-output-dir D` (`new_project_command`), up to the step after the desktop
  policy is written: D = a link in otto's home to `/etc/sudoers.d`, an unregistered directory otto owns, a directory
  of root's alone, and none (root's own `mkdtemp`);
- `switchyard new --output-dir D` (`_prepare_new_project_accounts`, the steps before it standing in): the same link,
  root's own directory, and none (the project's provision directory, the owner's, by descriptor);
- the `--from` artifact written back with the workflow (`write_new_project_launcher_artifacts`), the artifact a link
  in otto's tree to a root-only file;
- `team-launcher pie pane detach-role main` as root, for a configuration naming no owner that root holds no record
  of, and for one root's records name.

Main's code (from a git archive) writes into `/etc/sudoers.d` through otto's link, leaves a root-owned file in otto's
directory, overwrites the root-only file through the artifact link, and rewrites the unrecorded configuration as
root. The candidate refuses each before writing, leaves every planted target byte for byte unchanged, and still
writes root's own and the owner's directories as before.
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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "16c2a6ddaefcb47118a792e49367d14d1c94ce02"  # main before SYRD-759
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019
HOME = Path("/home") / OWNER
OTHER_HOME = Path("/home") / OTHER
PROJECT = HOME / "Projects" / "pie"
PROVISION = PROJECT / ".switchyard" / "provision"
SUDOERS = Path("/etc/sudoers.d")
TARGET = Path("/etc/switchyard/root-only.json")
LINKED = OTHER_HOME / "prov"  # otto's link to /etc/sudoers.d
UNREGISTERED = Path("/srv/otto-out")  # a directory otto owns, in no registered project
ROOTS_OWN = Path("/root-out")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def snapshot(*roots: Path) -> dict:
    found = {}
    for top in roots:
        for path in [top, *sorted(top.rglob("*"))] if top.is_dir() and not top.is_symlink() else [top]:
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
                      "var/tmp", "srv", "stubs/bin", "stubs/sbin"):
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


class Stop(Exception):
    pass


def inner(root: Path, work: Path, mode: str) -> dict:
    sh("ip", "link", "set", "lo", "up")
    enter_root_owned_root(work)
    root = Path("/stage") / root.relative_to(work)
    release = Path("/opt/sw")
    for part in ("scripts", "config", "examples", "skills", "docs"):
        if (root / part).exists():
            shutil.copytree(root / part, release / part, symlinks=True)
    sh("chmod", "-R", "a+rX", str(release))
    for place in ("/etc/switchyard", "/etc/systemd/system", "/etc/polkit-1/rules.d", "/etc/tmpfiles.d"):
        Path(place).mkdir(parents=True, exist_ok=True)
    SUDOERS.mkdir(mode=0o750)
    (SUDOERS / "README").write_text("root's sudoers drop-ins\n", encoding="utf-8")
    TARGET.write_text(json.dumps({"secret": "root's alone"}) + "\n", encoding="utf-8")
    TARGET.chmod(0o600)
    ROOTS_OWN.mkdir(mode=0o700)
    Path("/stubs/sbin/systemctl").write_text('#!/bin/sh\ncase "$*" in *is-active*) echo inactive; exit 3;; esac\nexit 0\n')
    for cli in ("tmux", "claude", "codex", "agy", "copilot", "hermes", "konsole", "sudo", "runuser"):
        Path(f"/stubs/bin/{cli}").write_text(f'#!/bin/sh\necho "refused stub: {cli} $*" >&2\nexit 97\n')
    for place in (Path("/stubs/sbin"), Path("/stubs/bin")):
        for stub in place.iterdir():
            stub.chmod(0o755)
    accounts = ((OWNER, OWNER_UID, HOME), (OTHER, OTHER_UID, OTHER_HOME))
    Path("/etc/passwd").write_text("root:x:0:0::/run/root-home:/bin/sh\nboardsvc:x:1500:1500::/nonexistent:/bin/false\n"
                                   + "".join(f"{n}:x:{u}:{u}::{h}:/bin/bash\n" for n, u, h in accounts), encoding="utf-8")
    Path("/etc/group").write_text("root:x:0:\nboardsvc:x:1500:\n" + "".join(f"{n}:x:{u}:\n" for n, u, _ in accounts),
                                  encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
    for directory in (HOME / "Projects", PROJECT, PROJECT / ".switchyard"):
        directory.mkdir(mode=0o755)
        os.chown(directory, OWNER_UID, OWNER_UID)
    # What otto plants in its own tree: a link to root's sudoers directory, a directory of its own outside any
    # registered project, and a design artifact that is a link to a file only root can read.
    LINKED.symlink_to(SUDOERS)
    os.lchown(LINKED, OTHER_UID, OTHER_UID)
    UNREGISTERED.mkdir()
    os.chown(UNREGISTERED, OTHER_UID, OTHER_UID)
    designs = OTHER_HOME / "designs"
    designs.mkdir()
    os.chown(designs, OTHER_UID, OTHER_UID)
    (designs / f"{SLUG}.project.json").symlink_to(TARGET)
    os.lchown(designs / f"{SLUG}.project.json", OTHER_UID, OTHER_UID)
    watched = (SUDOERS, TARGET, OTHER_HOME, UNREGISTERED)

    sys.path.insert(0, str(release))
    from scripts import browser_inheritance
    from scripts import team_launcher as launcher
    from scripts.ticket_board.project_provision import build_plan

    out: dict = {}

    def attempt(action) -> dict:
        before = snapshot(*watched)
        try:
            action()
            answer = "returned"
        except Stop:
            answer = "reached the next step"
        except SystemExit as stop:
            answer = f"refused: {stop}"
        except BaseException as exc:  # noqa: BLE001
            answer = f"{type(exc).__name__}: {exc}"
        after = snapshot(*watched)
        return {"answer": answer[-700:], "unchanged": after == before,
                "changed": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}

    # 1. team-launcher <p> new --execute --new-output-dir D, up to the step after the desktop policy.
    launcher.precheck_new_project = lambda *a, **k: None

    def stop_after_policy(*a, **k):
        raise Stop()

    real_worktree_base = launcher._new_project_worktree_base
    launcher._new_project_worktree_base = stop_after_policy
    Path("/srv/root-link").symlink_to(UNREGISTERED)  # root's own link, into otto's directory
    for name, directory in (("linked", LINKED), ("unregistered", UNREGISTERED), ("roots-own", ROOTS_OWN),
                            ("default", None), ("sticky-name", Path("/tmp/pie-out")),
                            ("root-link", Path("/srv/root-link"))):
        made: list[str] = []
        real_dir = launcher._new_project_artifact_dir
        launcher._new_project_artifact_dir = lambda project: made.append(str(real_dir(project))) or Path(made[-1])
        out[f"execute/{name}"] = attempt(lambda: launcher.new_project_command(
            SLUG, owner_user=OWNER, repository=PROJECT, owner_home=HOME, source_repo=release,
            desktop_policy=Path("headless"), output_dir=directory, execute=True, require_owner_user=False,
            print_func=lambda *a, **k: None))
        launcher._new_project_artifact_dir = real_dir
        written = Path(made[0]) if made else (directory or Path("/nonexistent"))
        out[f"execute/{name}"]["policy_written"] = (written / "desktop-policy.json").exists() and \
            not written.is_symlink()
    out["sudoers_policy_after_execute"] = (SUDOERS / "desktop-policy.json").exists()
    launcher._new_project_worktree_base = real_worktree_base

    # 2. switchyard new --output-dir D, at the end of the accounts phase; the steps before it stand in.
    if (SUDOERS / "desktop-policy.json").exists():  # main's code put one there in route 1
        (SUDOERS / "desktop-policy.json").unlink()
    for stand_in in ("_ensure_board_service_user", "_ensure_board_service_peer_auth", "verify_agent_clis_for_owner",
                     "_write_switchyard_onboarding_files", "_write_initial_switchyard_project_artifact",
                     "_chown_switchyard_project_files", "_install_switchyard_onboarding_docs",
                     "_require_existing_project_git_repository"):
        setattr(launcher, stand_in, lambda *a, **k: None)
    launcher._ensure_owner_user_and_project_dir = lambda *a, **k: SimpleNamespace(created=False, shell_path="")
    browser_inheritance.inherit_desktop_browser = lambda *a, **k: "kept"
    policy = {"mode": "headless", "project": SLUG, "tenant_user": OWNER}
    for name, directory in (("linked", LINKED), ("roots-own", ROOTS_OWN / "switchyard-new"), ("project", None)):
        out[f"accounts/{name}"] = attempt(lambda: launcher._prepare_new_project_accounts(
            from_artifact=None, output_dir=directory, home_base=Path("/home"), git_init=False,
            print_func=lambda *a, **k: None, agy_source_origin="unset", artifact_path=PROJECT / ".switchyard" / "x.json",
            design_document=PROJECT / "PROJECT_DESIGN.md", director_onboarding=PROJECT / ".switchyard" / "d.md",
            include_audit=True, include_designer=False, owner_shell="bash", owner_user=OWNER, project_dir=PROJECT,
            resolved_agy_credential_source="", resolved_project_name=NAME, resolved_slug=SLUG,
            runner=subprocess.run, selected_audit_roles=("audit",), selected_desktop_policy=policy,
            selected_implementer_roles=("main",), selected_role_efforts={}, selected_role_models={},
            stages=SimpleNamespace(begin=lambda *a, **k: None), effective_source_repo=release,
            precheck_plan=SimpleNamespace(service_user="boardsvc"), selected_role_clis=(), selected_router=(),
            selected_documentation=(), worktree_branch="main"))
        written = directory or PROVISION
        out[f"accounts/{name}"]["policy_written"] = (written / "desktop-policy.json").exists() and \
            not written.is_symlink()
    out["sudoers_policy_after_accounts"] = (SUDOERS / "desktop-policy.json").exists()

    # 3. The --from artifact written back with the workflow: a link in otto's tree to a root-only file.
    workflow = json.loads((release / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
    workflow = json.loads(json.dumps(workflow).replace(f'"{workflow["project"]}-', f'"{SLUG}-')
                          .replace(f'"project": "{workflow["project"]}"', f'"project": "{SLUG}"'))
    plan = replace(build_plan(project=SLUG, project_name=NAME, owner_user=OWNER, owner_home=HOME, source_repo=release,
                              service_user="boardsvc", project_repository=PROJECT), workflow=workflow)
    printed: list[str] = []
    out_dir = ROOTS_OWN / "from-artifact"
    out_dir.mkdir(mode=0o700)
    out["from_artifact"] = attempt(lambda: launcher.write_new_project_launcher_artifacts(
        plan, out_dir, repository=PROJECT, project_name=NAME, artifact_path=designs / f"{SLUG}.project.json",
        print_func=printed.append))
    out["from_artifact"]["printed"] = [line for line in printed if "workflow back" in line]
    out["target_text"] = TARGET.read_text(encoding="utf-8")
    left = designs / f"{SLUG}.project.json"
    info = os.lstat(left)
    out["designs_after"] = {"link": stat.S_ISLNK(info.st_mode), "uid": info.st_uid, "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                            "text": "" if stat.S_ISLNK(info.st_mode) else left.read_text(encoding="utf-8")[:300]}

    # 4. team-launcher's own root dispatch, for a configuration naming no owner that root holds no record of.
    config_path = launcher.write_new_project_launcher_artifacts(
        replace(plan, workflow=None), PROVISION, repository=PROJECT, project_name=NAME, print_func=lambda *a, **k: None)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    # Without a control repository, which the loader requires a named account for, as SYRD-663's suite has it.
    unowned = {key: value for key, value in document.items() if key not in ("control_repository", "worktree_base")}
    config_path.write_text(json.dumps({**unowned, "run_as_user": ""}, indent=2), encoding="utf-8")
    os.chown(config_path, OWNER_UID, OWNER_UID)
    before = config_path.read_bytes()
    done = subprocess.run([sys.executable, str(release / "scripts" / "team-launcher"), SLUG, "pane", "detach-role",
                           "main", "--config", str(config_path)], capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL, env={"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin",
                                                         "HOME": "/run/root-home", "LANG": "C.UTF-8",
                                                         "PYTHONDONTWRITEBYTECODE": "1"})
    out["dispatch/unrecorded"] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-900:],
                                  "unchanged": config_path.read_bytes() == before}
    # And once root's records name the owner -- its provisioning record and registry entry -- the dispatch goes on.
    launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(replace(plan, workflow=None)))
    registry = Path("/etc/switchyard/projects")
    registry.mkdir(exist_ok=True)
    (registry / f"{SLUG}.json").write_text(json.dumps({"schema": "switchyard.project-registry.v1", "slug": SLUG,
                                                        "name": NAME, "config_path": str(config_path)}), encoding="utf-8")
    config_path.write_text(json.dumps({**document, "run_as_user": OWNER}, indent=2), encoding="utf-8")
    done = subprocess.run([sys.executable, str(release / "scripts" / "team-launcher"), SLUG, "pane", "detach-role",
                           "main", "--config", str(config_path)], capture_output=True, text=True, timeout=300,
                          stdin=subprocess.DEVNULL, env={"PATH": "/stubs/sbin:/stubs/bin:/usr/bin:/bin",
                                                         "HOME": "/run/root-home", "LANG": "C.UTF-8",
                                                         "PYTHONDONTWRITEBYTECODE": "1"})
    out["dispatch/recorded"] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-900:]}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd759.") as tmp:
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
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1800, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    check(out["sudoers_policy_after_execute"] and not out["execute/linked"]["unchanged"],
          f"before: team-launcher new --execute writes into /etc/sudoers.d through otto's link: {out['execute/linked']}")
    check(not out["execute/unregistered"]["unchanged"],
          f"before: and leaves root's file in a directory otto owns: {out['execute/unregistered']}")
    check(not out["accounts/linked"]["unchanged"],
          f"before: switchyard new --output-dir writes through otto's link: {out['accounts/linked']}")
    left = out["designs_after"]
    check(not left["link"] and "root's alone" in left["text"] and '"workflow"' in left["text"],
          f"before: the --from artifact is written back by path: root reads the root-only file through otto's link and "
          f"leaves its contents, with the workflow, in otto's directory: {left}")
    check(not out["dispatch/unrecorded"]["unchanged"],
          f"before: team-launcher's root dispatch rewrites a configuration root holds no record of: "
          f"{out['dispatch/unrecorded']}")


def candidate(out: dict) -> None:
    for route in ("execute", "accounts"):
        got = out[f"{route}/linked"]
        check(got["answer"].startswith("refused: ") and f"{SUDOERS} is a link or not a directory" not in got["answer"]
              and f"{OTHER_HOME} is owned by uid {OTHER_UID}, not root" in got["answer"]
              and "Nothing was written into it." in got["answer"] and got["unchanged"],
              f"{route}, an output directory that is otto's link to /etc/sudoers.d: refused, naming otto's directory, "
              f"before anything is written; nothing planted changed: {got}")
    check(not out["sudoers_policy_after_execute"] and not out["sudoers_policy_after_accounts"],
          "and nothing reached /etc/sudoers.d")
    got = out["execute/sticky-name"]
    check(got["answer"].startswith("refused: ") and "/tmp can be written by others" in got["answer"]
          and not got["policy_written"],
          f"a directory still to be made in sticky /tmp, where anybody could make that name first: refused: {got}")
    got = out["execute/root-link"]
    check(got["answer"].startswith("refused: ") and "/srv/root-link is a link or not a directory" in got["answer"]
          and got["unchanged"], f"a link, even root's own, is not a directory root writes through: {got}")
    got = out["execute/unregistered"]
    check(got["answer"].startswith("refused: ") and f"{UNREGISTERED} is owned by uid {OTHER_UID}, not root" in got["answer"]
          and got["unchanged"],
          f"execute, a directory otto owns in no registered project: refused, unchanged: {got}")
    for name in ("execute/roots-own", "execute/default", "accounts/roots-own", "accounts/project"):
        got = out[name]
        check(got["answer"] in ("reached the next step", "returned") and got["policy_written"] and got["unchanged"],
              f"{name}: a directory only root controls (or the owner's own, by descriptor) is written as before: {got}")
    got = out["from_artifact"]
    check(got["printed"] and "not writing the workflow back" in got["printed"][0]
          and f"{OTHER_HOME} is owned by uid {OTHER_UID}, not root" in got["printed"][0] and got["unchanged"]
          and out["designs_after"]["link"] and out["target_text"] == '{"secret": "root\'s alone"}\n',
          f"the --from artifact outside the owner's roots is not written back, and the root-only file it links to is "
          f"unchanged: {got} {out['target_text'][:120]}")
    unrecorded = out["dispatch/unrecorded"]
    check(unrecorded["code"] != 0 and "team-launcher: root holds no record of pie that says whose project it is; run "
          "the command as the project's owner. Nothing was changed." in unrecorded["said"] and unrecorded["unchanged"],
          f"team-launcher's root dispatch refuses a configuration root holds no record of, before reading it: {unrecorded}")
    recorded = out["dispatch/recorded"]
    check("root holds no record" not in recorded["said"] and "Traceback" not in recorded["said"],
          f"and goes on for one root's records name: {recorded}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD759_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd759-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"new_output_dir_root_alone_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
