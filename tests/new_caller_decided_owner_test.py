#!/usr/bin/env python3
"""SYRD-761: `switchyard new`'s caller hands the board phase P0's owner and project directory, whatever the artifact
says by then.

SYRD-758's guarantee -- root acts for the owner and project directory it decided, not what the artifact says once the
owner holds it -- rests on `switchyard_new_command` passing P0's own `owner_user` and `project_dir` to
`_prepare_new_project_board`, which forwards them to `new_project_command` as `decided`. `new_decided_owner_test`
drives the board phase directly, so a caller that re-read the owner or directory from the artifact after the handoff
went unnoticed.

Here, as root in a user, mount, pid and network namespace with real uids, the real `switchyard_new_command` runs from
the top: P0 (`_resolve_new_project_choices`) for real -- with `--from` a root-written artifact, and without it -- then
the host checks (P1) standing in, and the accounts phase (P2) standing in for what matters here: the artifact is in
`.switchyard`, `.switchyard` and the artifact are handed to the owner, and the owner rewrites the artifact (another
owner, `root`, or another tenant's directory as the repository). Then the real board phase, and the real
`new_project_command`, with `build_plan` standing in to record the owner and repository root would provision for, and
stop. The board phase is observed as it is called, and passes the call on unchanged.

The board phase receives P0's owner and directory on every route; a rewritten owner is refused, naming both accounts,
before any plan; a rewritten repository changes nothing; the untouched artifact provisions for P0's owner and
directory.
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
    for directory in (HOME / "Projects", PROJECT):
        directory.mkdir(mode=0o755)
        os.chown(directory, OWNER_UID, OWNER_UID)
    Path("/var/lib/linger").mkdir(parents=True)
    Path("/var/lib/linger", OWNER).touch()  # an existing owner `new` may reuse

    sys.path.insert(0, str(release))
    from scripts import owner_writes
    from scripts import team_launcher as launcher

    def write_artifact() -> None:
        """The artifact as root writes it, while `.switchyard` is still root's."""
        switchyard = PROJECT / ".switchyard"
        if switchyard.exists():
            shutil.rmtree(switchyard)
        switchyard.mkdir(mode=0o755)
        launcher._write_initial_switchyard_project_artifact(
            project_name=NAME, slug=SLUG, owner_user=OWNER, project_dir=PROJECT, artifact_path=ARTIFACT,
            design_document=PROJECT / launcher.SWITCHYARD_DESIGN_FILE_NAME, owner_shell="/bin/bash",
            include_designer=False)

    def rewrite(**project_fields: str):
        def action() -> None:
            document = json.loads(ARTIFACT.read_text(encoding="utf-8"))
            document["project"].update(project_fields)
            temporary = ARTIFACT.with_name("swap.json")
            temporary.write_text(json.dumps(document), encoding="utf-8")
            os.replace(temporary, ARTIFACT)
        return action

    def run_new(route: str, tamper) -> dict:
        owner_writes.TRUSTED_PROJECT_ROOTS.clear()
        seen: dict = {}
        saved = {name: getattr(launcher, name) for name in ("_check_new_project_preflight",
                                                             "_prepare_new_project_accounts",
                                                             "_prepare_new_project_board", "build_plan")}

        def preflight(**kwargs):  # P1: the host checks, which this namespace cannot answer
            return SimpleNamespace(effective_source_repo=release, precheck_plan=None,
                                   selected_role_clis=kwargs["selected_role_clis"],
                                   selected_router=kwargs["selected_router"],
                                   selected_documentation=kwargs["selected_documentation"], worktree_branch="main")

        def accounts(**kwargs):  # P2: the artifact in `.switchyard`, both handed to the owner, who then rewrites it
            if kwargs["from_artifact"] is None:
                write_artifact()
            for path in (PROJECT / ".switchyard", ARTIFACT):
                os.chown(path, OWNER_UID, OWNER_UID)
            seen["tampered"] = as_owner(tamper) if tamper else "-"
            return SimpleNamespace(provision_dir=PROJECT / ".switchyard" / "provision")

        def board(**kwargs):  # P3, observed as called and run for real
            seen["board_owner"], seen["board_dir"] = kwargs["owner_user"], str(kwargs["project_dir"])
            return saved["_prepare_new_project_board"](**kwargs)

        def build_plan(**kwargs):
            seen.update(owner=kwargs.get("owner_user"), repository=str(kwargs.get("project_repository") or ""))
            raise Stop()

        launcher._check_new_project_preflight, launcher._prepare_new_project_accounts = preflight, accounts
        launcher._prepare_new_project_board, launcher.build_plan = board, build_plan
        if route == "--from":
            write_artifact()
            given = {"from_artifact": ARTIFACT}
        else:
            given = {"slug": SLUG, "project_name": NAME, "agent_name": OWNER, "project_path": PROJECT,
                     "role_clis": (("director", "claude"), ("main", "claude"))}
        try:
            launcher.switchyard_new_command(
                **given, source_repo=release, yes=True, headless=True, allow_existing_owner_user=True,
                no_upstream_report=True, no_agy_credential=True, git_init=False, runner=subprocess.run,
                port_in_use=lambda port: False, socket_exists=lambda path: False, input_func=lambda prompt: "",
                print_func=lambda *a, **k: None)
            seen["answer"] = "returned"
        except Stop:
            seen["answer"] = "planned"
        except SystemExit as stop:
            seen["answer"] = f"refused: {stop}"
        except BaseException as exc:  # noqa: BLE001
            seen["answer"] = f"{type(exc).__name__}: {exc}"
        finally:
            for name, value in saved.items():
                setattr(launcher, name, value)
        seen["trusted"] = {owner: sorted(roots) for owner, roots in owner_writes.TRUSTED_PROJECT_ROOTS.items()}
        return seen

    out: dict = {}
    cases = {"untouched": None, "foreign-owner": rewrite(owner_user=OTHER), "root-owner": rewrite(owner_user="root"),
             "foreign-repository": rewrite(repository=str(OTHER_HOME / "work"))}
    for route in ROUTES:
        for name, tamper in cases.items():
            out[f"{route}|{name}"] = run_new(route, tamper)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd761.") as tmp:
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


ROUTES = ("--from", "--agent-name")


def before(out: dict) -> None:
    for route in ROUTES:
        got = out[f"{route}|foreign-owner"]
        check(got["tampered"] == "done" and got["board_owner"] == OWNER and got["answer"] == "planned"
              and got["owner"] == OTHER,
              f"before, {route}: the caller hands the board phase P0's owner, but the board phase provisions for the "
              f"account the owner's rewrite names: {got}")


def candidate(out: dict) -> None:
    for route in ROUTES:
        for name in ("untouched", "foreign-owner", "root-owner", "foreign-repository"):
            got = out[f"{route}|{name}"]
            check(got.get("board_owner") == OWNER and got.get("board_dir") == str(PROJECT),
                  f"{route}, {name}: the board phase receives P0's owner and project directory: {got}")
        got = out[f"{route}|untouched"]
        check(got["answer"] == "planned" and got["owner"] == OWNER and got["repository"] == str(PROJECT)
              and got["trusted"] == {OWNER: [str(PROJECT)]},
              f"{route}, untouched: root provisions for, and trusts, P0's owner and directory: {got}")
        for name, owner in (("foreign-owner", OTHER), ("root-owner", "root")):
            got = out[f"{route}|{name}"]
            check(got["tampered"] == "done" and got["answer"].startswith("refused: switchyard: ")
                  and f"now names {owner!r} as {SLUG}'s owner, but this run decided on {OWNER!r}" in got["answer"]
                  and "owner" not in got and owner not in got["trusted"],
                  f"{route}, {name}: the owner's rewrite is refused before any plan, naming both accounts: {got}")
        got = out[f"{route}|foreign-repository"]
        check(got["tampered"] == "done" and got["answer"] == "planned" and got["owner"] == OWNER
              and got["repository"] == str(PROJECT) and got["trusted"] == {OWNER: [str(PROJECT)]},
              f"{route}, foreign-repository: a rewritten repository changes nothing: {got}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD761_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd761-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"new_caller_decided_owner_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
