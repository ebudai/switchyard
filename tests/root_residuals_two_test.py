#!/usr/bin/env python3
"""SYRD-760: the SYRD-606 residuals after SYRD-667 -- hard links, a make-and-reopen race, root's Python path.

Each is a place where root's in-process work on a tenant's path could still be steered:

1. a single-file handoff (`tenant_paths attrs`, behind `owner_files._chown_project_file`) chowned a hard link to
   somebody else's inode -- SYRD-667 refused that only in `chown_tree`;
2. a recursive ACL grant (`tenant_paths acl --recursive`) wrote the ACL onto a hard-linked file of somebody else's;
3. `desktop_layout_writer._open_owned_directory_chain` made a directory, opened the name again and gave what it
   opened away -- the account owning the parent can rename another directory onto that name in between;
4. `install-all-repository-hooks.sh` ran `python3 -c 'import json ...'` as root with the current directory first on
   the import path, and ran `repository_hooks.py` as a file, whose `from scripts import` cannot resolve as root;
5. `repository_hooks` in root's own tree ran git over the repository's `.git` and config before proving them root's.

Here, as root in a user, mount, pid and network namespace with real uids. The hard links are made by root on the
tenant's behalf: on a host with `fs.protected_hardlinks=0` the tenant could make them itself (this host has it on).
The race is injected at the one moment it needs: right after the `mkdir`, the made directory is replaced by another.

Main's code (from a git archive) chowns the linked root-only file to the tenant, writes the ACL onto it, gives a
root-owned non-empty directory to the tenant, runs a planted `json.py` as root, crashes on `from scripts import` as
root, and runs git over a tenant-written config and a `.git` file pointing into the tenant's home. The candidate
refuses each before changing anything, while the tenant's own hard-linked files, an honest new directory, and the
rollout itself still work.
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
BEFORE = "ed9bdbae24816e20e10de652d51801d45bd2939e"  # main before SYRD-760
OWNER, OWNER_UID = "pie-agent", 1018
OTHER, OTHER_UID = "otto-agent", 1019
HOME = Path("/home") / OWNER
OTHER_HOME = Path("/home") / OTHER
VICTIM = Path("/etc/switchyard/victim.json")
GITLOG = Path("/stubs/git.log")
ACL = "system.posix_acl_access"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def sh(*argv: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), check=True, capture_output=True, text=True, **kwargs)


def fingerprint(path: Path) -> list:
    info = os.lstat(path)
    try:
        acl = os.getxattr(path, ACL, follow_symlinks=False).hex()
    except OSError:
        acl = ""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if stat.S_ISREG(info.st_mode) else ""
    return [info.st_uid, info.st_gid, f"{stat.S_IMODE(info.st_mode):04o}", digest, acl]


def enter_root_owned_root(stage: Path) -> None:
    """Become root of a filesystem namespace root really owns (as `root_owner_from_record_test` does)."""
    new = stage / "newroot"
    new.mkdir()
    sh("mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", str(new))
    for directory in ("usr", "etc", "dev", "proc", "tmp", "run", "home", "opt", "stage", "var/tmp", "data/git",
                      "stubs/bin"):
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
        f"{n}:x:{u}:{u}::{h}:/bin/bash\n" for n, u, h in accounts), encoding="utf-8")
    Path("/etc/group").write_text("root:x:0:\n" + "".join(f"{n}:x:{u}:\n" for n, u, _ in accounts), encoding="utf-8")
    os.makedirs("/run/root-home", mode=0o700)
    for name, uid, home in accounts:
        os.makedirs(home, mode=0o700)
        os.chown(home, uid, uid)
    VICTIM.write_text(json.dumps({"secret": "root's alone"}) + "\n", encoding="utf-8")
    VICTIM.chmod(0o600)
    # git, recording every call before it runs the real one.
    Path("/stubs/bin/git").write_text(f'#!/bin/sh\necho "git $*" >> {GITLOG}\nexec /usr/bin/git "$@"\n')
    Path("/stubs/bin/git").chmod(0o755)
    os.environ["PATH"] = "/stubs/bin:/usr/bin:/bin"
    os.environ["HOME"] = "/run/root-home"
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

    sys.path.insert(0, str(release))
    from scripts import team_launcher as launcher

    out: dict = {"victim_before": fingerprint(VICTIM)}

    def owned(path: Path, uid: int, text: str = "") -> Path:
        path.write_text(text or path.name, encoding="utf-8")
        os.chown(path, uid, uid)
        return path

    # 1. A single-file handoff of a hard link to root's file, and of the owner's own hard-linked file.
    planted = HOME / "handoff.json"
    os.link(VICTIM, planted)  # what the tenant itself could do with protected_hardlinks=0
    own = owned(HOME / "own.json", OWNER_UID)
    os.link(own, HOME / "own-copy.json")
    os.chmod(own, 0o600)
    results = {}
    said: list[str] = []

    def runner(argv, **kwargs):  # the operator sees tenant_paths' stderr; here it is kept to be read
        done = subprocess.run(argv, capture_output=True, text=True)
        said.append(done.stderr)
        return done

    for name, path in (("planted", planted), ("own", own)):
        try:
            launcher._chown_project_file(owner_user=OWNER, path=path, runner=runner)
            results[name] = "handed over"
        except SystemExit as stop:
            results[name] = f"refused: {stop} [{said[-1].strip()}]"
    out["handoff"] = results
    out["victim_after_handoff"] = fingerprint(VICTIM)
    os.chown(VICTIM, 0, 0)
    planted.unlink()

    # 2. A recursive ACL grant over a tree holding a hard link to root's file; and over the owner's own.
    for name, linked in (("planted", VICTIM), ("own", own)):
        tree = HOME / f"tree-{name}"
        tree.mkdir()
        os.chown(tree, OWNER_UID, OWNER_UID)
        os.link(linked, tree / "linked.json")
        done = subprocess.run(["/usr/bin/python3", "-I", str(release / "scripts" / "tenant_paths.py"), "--root",
                               str(HOME), "acl", "--modify", f"u:{OTHER}:rw", "--recursive", str(tree)],
                              capture_output=True, text=True)
        out[f"acl/{name}"] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-400:],
                              "acl": bool(fingerprint(linked)[4])}
    out["victim_after_acl"] = fingerprint(VICTIM)

    # 3. The make-and-reopen race: right after the mkdir, the made directory is replaced by a root-owned, non-empty
    #    one already in the owner's home (renaming an entry there is something the owner can do).
    from scripts import desktop_layout_writer

    decoy = HOME / "decoy"
    decoy.mkdir(mode=0o700)
    (decoy / "root-record.json").write_text("{}", encoding="utf-8")
    real_mkdir = os.mkdir

    def racing_mkdir(name, mode=0o777, *, dir_fd=None):
        real_mkdir(name, mode, dir_fd=dir_fd)
        if name == "state":
            os.rmdir(name, dir_fd=dir_fd)
            os.rename("decoy", name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    os.mkdir = racing_mkdir
    try:
        fd, problem, _made = desktop_layout_writer._open_owned_directory_chain(HOME, ["state", "layouts"],
                                                                              uid=OWNER_UID, gid=OWNER_UID)
    finally:
        os.mkdir = real_mkdir
    if fd >= 0:
        os.close(fd)
    swapped = HOME / "state"
    out["race"] = {"problem": problem, "swapped_owner": os.lstat(swapped).st_uid,
                   "record_still_there": (swapped / "root-record.json").exists()}
    fd, problem, made = desktop_layout_writer._open_owned_directory_chain(HOME, ["fresh", "layouts"],
                                                                         uid=OWNER_UID, gid=OWNER_UID)
    if fd >= 0:
        os.close(fd)
    out["honest_mkdir"] = {"problem": problem, "made": made, "owner": os.lstat(HOME / "fresh").st_uid
                           if (HOME / "fresh").exists() else None}

    # 4. The host-wide rollout as root, from a directory the tenant owns holding a json.py.
    repo = HOME / "repo"
    subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(repo)], check=True)
    sh("chown", "-R", f"{OWNER_UID}:{OWNER_UID}", str(repo))
    config = HOME / "tenant.json"
    owned(config, OWNER_UID, json.dumps({"project": "pie", "repository": str(repo), "run_as_user": OWNER}))
    registry = Path("/etc/switchyard/projects")
    registry.mkdir()
    (registry / "pie.json").write_text(json.dumps({"config_path": str(config)}), encoding="utf-8")
    trap = OTHER_HOME / "work"
    trap.mkdir()
    marker = Path("/tmp/planted-json-ran")
    (trap / "json.py").write_text(f"open({str(marker)!r}, 'w').write('ran as uid ' + str(__import__('os').getuid()))\n"
                                  "raise SystemExit('planted json.py')\n", encoding="utf-8")
    sh("chown", "-R", f"{OTHER_UID}:{OTHER_UID}", str(trap))
    done = subprocess.run(["bash", str(release / "scripts" / "install-all-repository-hooks.sh")], cwd=trap,
                          capture_output=True, text=True, timeout=300,
                          env={**os.environ, "SWITCHYARD_BARE_REPO_ROOT": "/data/git",
                               "SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry),
                               "SWITCHYARD_PLATFORM_WORKFLOW_REPOSITORIES": ""})
    out["rollout"] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-1200:],
                      "planted_ran": marker.read_text() if marker.exists() else "",
                      "hook": (repo / ".git" / "hooks" / "pre-commit").exists()}
    # And from a directory without one, so the installer itself is what is measured.
    marker.unlink(missing_ok=True)
    done = subprocess.run(["bash", str(release / "scripts" / "install-all-repository-hooks.sh")], cwd="/",
                          capture_output=True, text=True, timeout=300,
                          env={**os.environ, "SWITCHYARD_BARE_REPO_ROOT": "/data/git",
                               "SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry),
                               "SWITCHYARD_PLATFORM_WORKFLOW_REPOSITORIES": ""})
    out["rollout_clean_dir"] = {"code": done.returncode, "said": (done.stdout + done.stderr)[-1200:],
                                "hook": (repo / ".git" / "hooks" / "pre-commit").exists()}

    # 5. Root's own tree: a bare repository whose config the tenant owns, and a checkout whose .git is a file
    #    pointing into the tenant's home. Is git run over them before they are proven root's?
    from scripts import repository_hooks

    bare = Path("/data/git/plat.git")
    subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(bare)], check=True)
    os.chown(bare / "config", OTHER_UID, OTHER_UID)
    evil = OTHER_HOME / "evil.git"
    subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(evil)], check=True)
    sh("chown", "-R", f"{OTHER_UID}:{OTHER_UID}", str(evil))
    checkout = Path("/data/git/wt")
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {evil}\n", encoding="utf-8")
    honest = Path("/data/git/honest.git")
    subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(honest)], check=True)
    for name, repository in (("tenant-config", bare), ("gitfile", checkout), ("honest", honest)):
        GITLOG.write_text("", encoding="utf-8")
        try:
            installed = repository_hooks.install_repository_policy(repository, source_root=release, project="plat",
                                                                   local_warning=False, main_guard=True)
            answer = f"installed {len(installed)}"
        except BaseException as exc:  # noqa: BLE001
            answer = f"{type(exc).__name__}: {exc}"
        out[f"root-tree/{name}"] = {"answer": answer[-500:], "git": GITLOG.read_text(encoding="utf-8").splitlines()}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "skills",
                              "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, mode: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd760.") as tmp:
        stage = Path(tmp)
        os.chmod(stage, 0o755)
        shutil.copy2(Path(__file__), stage / "suite.py")
        code = stage / "tree"
        for part in ("scripts", "config", "examples", "skills", "docs"):
            if (tree / part).exists():
                shutil.copytree(tree / part, code / part, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        subprocess.run(["chmod", "-R", "a+rX", str(stage), str(code)], check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_", "GIT_"))}
        child = subprocess.run(["unshare", "--map-auto", "--map-root-user", "--mount", "--propagation", "private",
                                "--pid", "--fork", "--mount-proc", "--net", sys.executable, str(stage / "suite.py"),
                                "--inner", str(code), tmp, mode], capture_output=True, text=True,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1800, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    check(out["victim_after_handoff"][0] == OWNER_UID,
          f"before: the single-file handoff chowns root's file to the tenant through a hard link: {out['victim_after_handoff']}")
    check(out["acl/planted"]["acl"],
          f"before: the recursive ACL grant writes the ACL onto root's file through a hard link: {out['acl/planted']}")
    check(out["race"]["swapped_owner"] == OWNER_UID and out["race"]["record_still_there"],
          f"before: the directory swapped in after the mkdir -- root's, non-empty -- is given to the tenant: {out['race']}")
    check("ran as uid 0" in out["rollout"]["planted_ran"],
          f"before: the rollout runs a json.py planted in the operator's directory, as root: {out['rollout']}")
    check("ModuleNotFoundError" in out["rollout_clean_dir"]["said"] or "No module named 'scripts'" in out["rollout_clean_dir"]["said"],
          f"before: run as a file, the installer cannot import scripts as root: {out['rollout_clean_dir']}")
    check(any("config --local --get core.hooksPath" in line for line in out["root-tree/tenant-config"]["git"])
          and out["root-tree/gitfile"]["git"],
          f"before: git runs as root over a tenant-written config and a .git file into the tenant's home before "
          f"either is proven: {out['root-tree/tenant-config']} {out['root-tree/gitfile']}")


def candidate(out: dict) -> None:
    handoff = out["handoff"]
    check(handoff["planted"].startswith("refused: ") and "has 2 hard links and belongs to uid 0" in handoff["planted"]
          and out["victim_after_handoff"] == out["victim_before"],
          f"a hard link to root's file is not handed over, and root's file is unchanged: {handoff} {out['victim_after_handoff']}")
    check(handoff["own"] == "handed over", f"the owner's own hard-linked file is handed over as before: {handoff}")
    planted = out["acl/planted"]
    check(planted["code"] == 3 and "has 2 hard links and belongs to uid 0" in planted["said"] and not planted["acl"]
          and out["victim_after_acl"] == out["victim_before"],
          f"the recursive ACL grant refuses a hard link to root's file, which stays without an ACL: {planted}")
    own = out["acl/own"]
    check(own["code"] == 0 and own["acl"], f"and grants over the owner's own hard-linked files as before: {own}")
    race = out["race"]
    check("changed between being made and being opened; nothing was given away" in race["problem"]
          and race["swapped_owner"] == 0 and race["record_still_there"],
          f"a directory swapped in after the mkdir is not given away: {race}")
    honest = out["honest_mkdir"]
    check(honest["problem"] == "" and honest["owner"] == OWNER_UID and len(honest["made"]) == 2,
          f"a directory made and opened undisturbed is the owner's, as before: {honest}")
    rollout = out["rollout"]
    check(rollout["planted_ran"] == "" and rollout["code"] == 0 and rollout["hook"],
          f"the rollout, from a directory holding a planted json.py, does not run it, and installs the hook: {rollout}")
    clean = out["rollout_clean_dir"]
    check(clean["code"] == 0 and "No module named" not in clean["said"] and clean["hook"],
          f"the installer runs as root as the scripts module: {clean}")
    for name, why in (("tenant-config", "its git config /data/git/plat.git/config is not in a tree root alone controls"),
                      ("gitfile", "/data/git/wt/.git is not a directory")):
        got = out[f"root-tree/{name}"]
        check(why in got["answer"] and got["git"] == [],
              f"root's own tree, {name}: refused before git runs at all: {got}")
    got = out["root-tree/honest"]
    check(got["answer"].startswith("installed") and got["git"],
          f"a repository root alone controls still gets its guard: {got}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree, sys.argv[2]), indent=1, default=str))
        return 0
    after = run(ROOT, "candidate")
    if os.environ.get("SYRD760_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd760-before.") as tmp:
            os.chmod(tmp, 0o755)
            before(run(tree_at(BEFORE, Path(tmp) / "before"), "before"))
    candidate(after)
    print(f"root_residuals_two_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
