#!/usr/bin/env python3
"""SYRD-758: `switchyard new` acts for the owner and project directory it decided, not what the artifact says later.

Root writes the project design artifact into `<project>/.switchyard`, hands that directory to the owner
(`new_project_phases.py`, `_chown_switchyard_project_files`), and only then reaches the database and board phase,
which called `new_project_command(from_artifact=<that file>)`. That command re-read the artifact by path and took its
`owner_user` and `repository` as the owner root acts for and the project root it trusts for that owner's writes
(`owner_writes.trust_project_root`). A reused owner that rewrote the file in between chose both.

Here, as root in a user, mount, pid and network namespace with real uids: the real artifact writer writes the
artifact as root, `.switchyard` is the owner's, and the owner itself then rewrites it -- naming another tenant, or
`root`, as the owner; naming another tenant's directory as the repository; or replacing it with a link to a file only
root can read. Root's `new_project_command` then runs as the board phase runs it, with `build_plan` standing in to
record the owner and repository root would provision for, and stop.

- Main's code (from a git archive) provisions for the rewritten owner (`otto-agent`, `root`), trusts the rewritten
  repository as that owner's project root, and reads the linked file.
- The candidate refuses a rewritten owner before anything is built, naming the artifact and both accounts; a
  rewritten repository changes nothing (root provisions for, and trusts, the directory it decided); and the link is
  refused rather than read. The untouched artifact provisions as before. The board phase passes the decided owner
  and directory.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "03fef767ed59347cda8a6389e191243dacb9adf4"  # main before SYRD-758
SLUG, NAME = "pie", "Personal Inference Engine"
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019
HOME = Path("/home") / OWNER
OTHER_HOME = Path("/home") / OTHER
PROJECT = HOME / "Projects" / "pie"
ARTIFACT = PROJECT / ".switchyard" / f"{SLUG}.project.json"
SECRET = Path("/etc/switchyard/root-only.json")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str) -> None:
    subprocess.run(list(argv), check=True, capture_output=True)


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns (as `root_owner_from_record_test` does)."""
    new = stage / "newroot"
    new.mkdir()
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(new))
    for directory in ("usr", "etc", "dev", "proc", "tmp", "run", "home", "opt", "stage", "var/tmp"):
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
    Path("/etc/switchyard").mkdir(mode=0o755)
    accounts = ((OWNER, OWNER_UID, HOME), (OTHER, OTHER_UID, OTHER_HOME))
    Path("/etc/passwd").write_text("root:x:0:0::/run/root-home:/bin/sh\n" + "".join(
        f"{name}:x:{uid}:{uid}::{home}:/bin/bash\n" for name, uid, home in accounts), encoding="utf-8")
    Path("/etc/group").write_text("root:x:0:\n" + "".join(f"{name}:x:{uid}:\n" for name, uid, _ in accounts),
                                  encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
    (OTHER_HOME / "work").mkdir()
    os.chown(OTHER_HOME / "work", OTHER_UID, OTHER_UID)
    for directory in (HOME / "Projects", PROJECT, PROJECT / ".switchyard"):
        directory.mkdir(mode=0o755)
        os.chown(directory, OWNER_UID, OWNER_UID)
    SECRET.write_text(json.dumps({"secret": "root's alone"}), encoding="utf-8")
    SECRET.chmod(0o600)

    sys.path.insert(0, str(release))
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    def write_artifact(project: Path = PROJECT) -> None:
        """The artifact as `switchyard new` writes it as root, before handing `.switchyard` over."""
        artifact = project / ".switchyard" / ARTIFACT.name
        if artifact.exists() or artifact.is_symlink():
            artifact.unlink()
        launcher._write_initial_switchyard_project_artifact(
            project_name=NAME, slug=SLUG, owner_user=OWNER, project_dir=project, artifact_path=artifact,
            design_document=project / launcher.SWITCHYARD_DESIGN_FILE_NAME, owner_shell="/bin/bash")
        os.chown(artifact, OWNER_UID, OWNER_UID)

    def rewrite(**project_fields: str):
        def action() -> None:
            document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
            document["project"].update(project_fields)
            temporary = ARTIFACT.with_name("swap.json")
            temporary.write_text(json.dumps(document), encoding="utf-8")
            os.replace(temporary, ARTIFACT)
        return action

    def link() -> None:
        ARTIFACT.unlink()
        ARTIFACT.symlink_to(SECRET)

    def board_phase(project: Path = PROJECT) -> dict:
        """`new_project_command` as the board phase calls it, up to the plan it would build."""
        artifact = project / ".switchyard" / ARTIFACT.name
        owner_writes.TRUSTED_PROJECT_ROOTS.clear()
        seen: dict = {}
        real_build = launcher.build_plan

        def build_plan(**kwargs):
            seen.update(owner=kwargs.get("owner_user"), repository=str(kwargs.get("project_repository") or ""))
            raise Stop()

        launcher.build_plan = build_plan
        real_load = launcher._load_json
        by_path: list[str] = []

        def load_json(path, *args, **kwargs):
            if Path(path) == artifact:
                by_path.append(str(path))
            return real_load(path, *args, **kwargs)

        launcher._load_json = load_json
        extra = {"decided": (OWNER, project)} if mode == "candidate" else {}
        try:
            launcher.new_project_command(SLUG, from_artifact=artifact, owner_home=HOME, source_repo=release,
                                         output_dir=project / ".switchyard" / "provision", execute=True,
                                         require_owner_user=False, enable_owner_linger=False,
                                         print_func=lambda *a, **k: None, **extra)
            seen["answer"] = "returned"
        except Stop:
            seen["answer"] = "planned"
        except SystemExit as stop:
            seen["answer"] = f"refused: {stop}"
        except BaseException as exc:  # noqa: BLE001
            seen["answer"] = f"{type(exc).__name__}: {exc}"
        finally:
            launcher.build_plan = real_build
            launcher._load_json = real_load
        seen["read_by_path"] = len(by_path)
        seen["trusted"] = {owner: sorted(roots) for owner, roots in owner_writes.TRUSTED_PROJECT_ROOTS.items()}
        return seen

    out: dict = {}
    cases = {"untouched": None, "foreign-owner": rewrite(owner_user=OTHER), "root-owner": rewrite(owner_user="root"),
             "foreign-repository": rewrite(repository=str(OTHER_HOME / "work")), "link": link}
    for name, tamper in cases.items():
        write_artifact()
        out[f"{name}/tampered"] = as_owner(tamper) if tamper else "-"
        out[name] = board_phase()

    # A project directory outside the owner's home (`--project-path`): the decided directory is one of the owner's
    # roots for the read too, so it is still read by descriptor.
    outside = Path("/srv/pie")
    for directory in (outside.parent, outside, outside / ".switchyard"):
        directory.mkdir(exist_ok=True)
    for directory in (outside, outside / ".switchyard"):
        os.chown(directory, OWNER_UID, OWNER_UID)
    owner_writes.trust_project_root(OWNER, outside)  # as `switchyard new` does before it writes there
    write_artifact(outside)
    out["outside-home"] = board_phase(outside)  # which starts with no project root trusted

    # And the board phase itself passes what was decided, whatever the artifact says by then.
    passed: dict = {}
    real_command = launcher.new_project_command
    launcher.new_project_command = lambda project, **kwargs: passed.update(kwargs) or 1
    try:
        launcher._prepare_new_project_board(
            source_repo=release, workflow_config=None, commit_git_dir=None, port=None, database=None,
            home_base=Path("/home"), port_in_use=lambda port: False, socket_exists=lambda path: False,
            registry_dir=None, print_func=lambda *a, **k: None, artifact_path=ARTIFACT,
            director_onboarding=PROJECT / ".switchyard" / "DIRECTOR_ONBOARDING.md", owner_user=OWNER,
            project_dir=PROJECT, resolved_slug=SLUG, runner=subprocess.run, selected_role_efforts={},
            selected_role_models={}, stages=SimpleNamespace(begin=lambda *a, **k: None),
            provision_dir=PROJECT / ".switchyard" / "provision")
    finally:
        launcher.new_project_command = real_command
    out["board_phase_decided"] = [str(part) for part in passed["decided"]] if "decided" in passed else None
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd758.") as tmp:
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
    for name, owner in (("foreign-owner", OTHER), ("root-owner", "root")):
        got = out[name]
        check(out[f"{name}/tampered"] == "done" and got["answer"] == "planned" and got["owner"] == owner
              and owner in got["trusted"],
              f"before, {name}: the owner rewrites the artifact after the handoff, and root provisions for, and trusts "
              f"a project root for, the account it now names: {got}")
    got = out["foreign-repository"]
    check(got["answer"] == "planned" and got["repository"] == str(OTHER_HOME / "work")
          and str(OTHER_HOME / "work") in got["trusted"].get(OWNER, []),
          f"before, foreign-repository: root trusts another tenant's directory as the owner's project root: {got}")
    got = out["link"]
    check("field 'schema' must be non-empty" in got["answer"],
          f"before, link: root reads the root-only file through the owner's link (it parses, and lacks a schema): {got}")
    check(out["untouched"]["read_by_path"] >= 2,
          f"before: root reads the owner's artifact by path, once for the artifact and again for the workflow: "
          f"{out['untouched']}")
    check(out["board_phase_decided"] is None, f"before: the board phase passes nothing decided: {out['board_phase_decided']}")


def candidate(out: dict) -> None:
    untouched = out["untouched"]
    check(untouched["answer"] == "planned" and untouched["owner"] == OWNER and untouched["repository"] == str(PROJECT)
          and untouched["trusted"] == {OWNER: [str(PROJECT)]},
          f"the artifact as root wrote it provisions for the decided owner and directory, as before: {untouched}")
    outside = out["outside-home"]
    check(outside["answer"] == "planned" and outside["owner"] == OWNER and outside["repository"] == "/srv/pie",
          f"a decided project directory outside the owner's home provisions for that directory: {outside}")
    reads = {name: out[name]["read_by_path"] for name in ("untouched", "foreign-owner", "root-owner", "foreign-repository", "link",
                                                          "outside-home")}
    check(set(reads.values()) == {0},
          f"root never reads the owner's artifact by path: one descriptor read serves the owner, the plan and the "
          f"workflow: {reads}")
    for name, owner in (("foreign-owner", OTHER), ("root-owner", "root")):
        got = out[name]
        check(out[f"{name}/tampered"] == "done" and got["answer"].startswith("refused: switchyard: ")
              and f"{ARTIFACT} now names {owner!r} as {SLUG}'s owner, but this run decided on {OWNER!r}" in got["answer"]
              and "owner" not in got and owner not in got["trusted"],
              f"{name}: the owner's rewrite is refused before any plan is built, and nothing is trusted for "
              f"{owner}: {got}")
    got = out["foreign-repository"]
    check(out["foreign-repository/tampered"] == "done" and got["answer"] == "planned" and got["owner"] == OWNER
          and got["repository"] == str(PROJECT) and got["trusted"] == {OWNER: [str(PROJECT)]},
          f"a rewritten repository changes nothing: root provisions for, and trusts, only the directory it decided: {got}")
    got = out["link"]
    check(out["link/tampered"] == "done" and got["answer"].startswith(("refused: ", "OSError", "PermissionError"))
          and "root's alone" not in json.dumps(got) and "owner" not in got,
          f"an artifact replaced by a link to a file only root can read is not read through: {got}")
    check(out["board_phase_decided"] == [OWNER, str(PROJECT)],
          f"the board phase passes the owner and directory it decided: {out['board_phase_decided']}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD758_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd758-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"new_decided_owner_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
