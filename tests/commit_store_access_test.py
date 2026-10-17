#!/usr/bin/env python3
"""SYRD-604: a repaired boundary includes the board service's read of its commit store.

MEFP's board verifies commits against `/data/git/fixpatch`, as `boardsvc` with
the unit's `Group=stellaris-agent`. `/data/git` is `sapi-agent:sapi-agent 0750`
with no ACL, so the board cannot open the store -- and `repair-boundary`, whose
phase deliberately grants nothing on a path another account owns, reported the
boundary repaired. It never asked whether the store was reachable.

Here the kernel is the referee. A user namespace with the invoking account's
subuid and subgid ranges mapped gives real uids, real groups and real ACLs;
`/etc/passwd` and `/etc/group` are bind-mounted so the accounts have MEFP's
names. Every verdict the probe gives is checked against `git` run with exactly
the unit's identity (`setpriv`), and the repair is run against root's own
installed packet. Nothing outside the temporary tree is touched.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, Path(__file__).resolve().parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.commit_store_access import OperatorDependency, commit_store_problems  # noqa: E402
from scripts.ticket_board.project_provision import build_plan, repository_boundary_phase  # noqa: E402
from resume_provision_test import SEAM, namespaces_available  # noqa: E402

PROJECT = "mefp"
OWNER, OWNER_UID = "stellaris-agent", 1500
SERVICE, SERVICE_UID = "boardsvc", 1600
FOREIGN, FOREIGN_UID = "sapi-agent", 1700
#: A group the shared service account is listed in, which the unit does not
#: give the process (SYRD-586): access through it is not access.
SHARED, SHARED_GID = "syrd-roles", 1800

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True, **kwargs)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def as_board(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """`argv` with the identity MEFP's unit gives the board: the service uid, the tenant group only."""
    return subprocess.run(
        ["setpriv", f"--reuid={SERVICE_UID}", f"--regid={OWNER_UID}", f"--groups={OWNER_UID}", *argv],
        capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_NOSYSTEM": "1"},
    )


def board_verifies(store: Path, commit: str) -> bool:
    return as_board(["git", "-c", f"safe.directory={store}", f"--git-dir={store}",
                     "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]).returncode == 0


def name_the_accounts(root: Path) -> None:
    """MEFP's account names over the namespace's real uids, and a `sudo` that means root."""
    passwd = root / "passwd"
    ours = {OWNER, SERVICE, FOREIGN, SHARED}

    def host(path: str) -> str:
        # The host may have accounts by these names; theirs would win the lookup.
        return "".join(line for line in Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
                       if line.split(":", 1)[0] not in ours)

    passwd.write_text(
        host("/etc/passwd")
        + f"{OWNER}:x:{OWNER_UID}:{OWNER_UID}::{root}/home/{OWNER}:/bin/sh\n"
        + f"{SERVICE}:x:{SERVICE_UID}:{SERVICE_UID}::/nonexistent:/usr/bin/nologin\n"
        + f"{FOREIGN}:x:{FOREIGN_UID}:{FOREIGN_UID}::/nonexistent:/usr/bin/nologin\n",
        encoding="utf-8",
    )
    group = root / "group"
    group.write_text(
        host("/etc/group")
        + f"{OWNER}:x:{OWNER_UID}:\n{SERVICE}:x:{SERVICE_UID}:\n{FOREIGN}:x:{FOREIGN_UID}:\n"
        + f"{SHARED}:x:{SHARED_GID}:{SERVICE}\n",
        encoding="utf-8",
    )
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    run(["mount", "--bind", str(passwd), "/etc/passwd"])
    run(["mount", "--bind", str(group), "/etc/group"])
    stubs = root / "bin"
    stubs.mkdir()
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    os.environ["PATH"] = f"{stubs}{os.pathsep}{os.environ['PATH']}"


def bare_store(path: Path, scratch: Path) -> str:
    """A real bare repository holding one commit, owned by the foreign account."""
    work = scratch / "work"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    run(["git", "init", "-q", str(work)], env=env)
    (work / "f").write_text("x\n", encoding="utf-8")
    run(["git", "-C", str(work), "add", "f"], env=env)
    run(["git", "-C", str(work), "commit", "-qm", "c"], env=env)
    commit = run(["git", "-C", str(work), "rev-parse", "HEAD"], env=env).stdout.strip()
    run(["git", "clone", "-q", "--bare", str(work), str(path)], env=env)
    shutil.rmtree(work)
    return commit


def foreign(path: Path, mode: int) -> None:
    for item in [path, *path.rglob("*")]:
        os.lchown(item, FOREIGN_UID, FOREIGN_UID)
    os.chmod(path, mode)


def snapshot(*paths: Path) -> list[tuple]:
    """Owner, group, mode and ACL of everything under each path: what 'untouched' means."""
    seen = []
    for top in paths:
        for item in [top, *sorted(top.rglob("*"))]:
            status = item.lstat()
            acl = run(["getfacl", "-cpn", str(item)]).stdout
            seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode), acl))
    return seen


def plan_for(home: Path, store: str, **overrides):
    arguments = dict(project=PROJECT, owner_user=OWNER, owner_home=home, service_user=SERVICE,
                     project_repository=home / "Projects" / PROJECT, commit_git_dir=store)
    arguments.update(overrides)
    return build_plan(**arguments)


def probe_matches_kernel(plan, store: Path, commit: str, expect: bool, what: str) -> list[str]:
    problems = commit_store_problems(plan)
    check((problems == []) is expect, f"{what}: probe says {problems}")
    check(board_verifies(store, commit) is expect, f"{what}: the kernel disagrees with the probe ({expect})")
    return problems


def probe_cases(root: Path) -> None:
    home = root / "home" / OWNER
    home.mkdir(parents=True)
    data = root / "data"
    git_root = data / "git"
    git_root.mkdir(parents=True)
    store = git_root / "fixpatch"
    commit = bare_store(store, root)
    os.chown(home, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    foreign(git_root, 0o750)
    foreign(store, 0o750)
    plan = plan_for(home, str(store))

    # 1. MEFP's shape: the first closed component is named with its owner,
    #    mode and the identity it refuses, and it is an operator dependency.
    problems = probe_matches_kernel(plan, store, commit, False, "MEFP shape")
    check(len(problems) == 1 and isinstance(problems[0], OperatorDependency), f"one dependency: {problems}")
    text = problems[0]
    check(f"{git_root} is {FOREIGN}:{FOREIGN} mode 0750, and no ACL entry gives it traverse (--x)" in text, text)
    check(f"{SERVICE} (uid {SERVICE_UID}, groups {OWNER})" in text and str(store) in text, text)
    check("repair-boundary cannot close this" in text and "outside" in text, text)

    # 2. A printed remedy must run: the grant it names, made as written, opens
    #    the store to the board -- read/traverse only, no mode or owner changed.
    commands = [part.strip("`") for part in text.split("the read/traverse-only grant is: ", 1)[1]
                .split(". Then run", 1)[0].split("; ")]
    check(len(commands) == 2 and all(c.startswith("sudo setfacl ") for c in commands), f"{commands}")
    check(all(f"g:{OWNER}:" in c and "w" not in c.split()[-2] for c in commands), f"{commands}")
    owners_before = [(p, u, g, m) for p, u, g, m, _a in snapshot(git_root)]
    for command in commands:
        run(shlex.split(command))
    check([(p, u, g, m) for p, u, g, m, _a in snapshot(git_root)] == owners_before, "the remedy changed a mode or owner")
    probe_matches_kernel(plan, store, commit, True, "after the printed grant")
    check(as_board(["touch", str(store / "objects" / "planted")]).returncode != 0, "the grant gave write")
    run(["setfacl", "-R", "-b", str(git_root)])

    # 3. Ancestor open, store closed: the store itself is what is named.
    os.chmod(git_root, 0o751)
    problems = probe_matches_kernel(plan, store, commit, False, "store closed")
    check(f"{store} is {FOREIGN}:{FOREIGN} mode 0750" in problems[0], problems[0])

    # 4. Reachable through the owning group: the unit's group owns both.
    for item in [git_root, store, *store.rglob("*")]:
        os.lchown(item, FOREIGN_UID, OWNER_UID)
    probe_matches_kernel(plan, store, commit, True, "owning group is the tenant group")

    # 5. Owning group matched but granting nothing beats 'other': denied.
    os.chmod(git_root, 0o705)
    probe_matches_kernel(plan, store, commit, False, "group class matched, other ignored")
    os.chmod(git_root, 0o750)

    # 6. A named entry for the service user beats its group and 'other'.
    run(["setfacl", "-m", f"u:{SERVICE}:---", str(store)])
    probe_matches_kernel(plan, store, commit, False, "named user entry denies")
    run(["setfacl", "-b", str(store)])
    foreign(git_root, 0o750)
    foreign(store, 0o750)

    # 7. Named group entries, and a mask that clips them.
    run(["setfacl", "-m", f"g:{OWNER}:--x", str(git_root)])
    run(["setfacl", "-R", "-m", f"g:{OWNER}:rX", str(store)])
    probe_matches_kernel(plan, store, commit, True, "named group entries")
    run(["setfacl", "-n", "-m", "m::---", str(git_root)])
    problems = probe_matches_kernel(plan, store, commit, False, "mask clips the named entry")
    check("and its ACL does not give it traverse (--x)" in problems[0], problems[0])
    run(["setfacl", "-m", "m::r-x", str(git_root)])

    # 8. Only a group the service account is listed in, not one its unit
    #    gives it: not reachable, and the kernel run as the unit agrees.
    run(["setfacl", "-x", f"g:{OWNER}", str(git_root)])
    run(["setfacl", "-m", f"g:{SHARED}:--x", str(git_root)])
    problems = probe_matches_kernel(plan, store, commit, False, "the shared account's own group")
    check(f"groups {OWNER})" in problems[0] and SHARED not in problems[0].split(":")[0], problems[0])
    run(["setfacl", "-x", f"g:{SHARED}", str(git_root)])
    run(["setfacl", "-m", f"g:{OWNER}:--x", str(git_root)])

    # 9. Inside the repository: an unreadable objects directory is named.
    os.chmod(store / "objects", 0o700)
    run(["setfacl", "-m", f"g:{OWNER}:---", str(store / "objects")])
    problems = probe_matches_kernel(plan, store, commit, False, "objects closed")
    check(f"{store / 'objects'} is" in problems[0], problems[0])
    run(["setfacl", "-m", f"g:{OWNER}:r-x", str(store / "objects")])
    probe_matches_kernel(plan, store, commit, True, "objects reopened")

    # 10. A store not cloned yet is not an access objection; a store reached
    #     through a link is judged where it resolves.
    check(commit_store_problems(plan_for(home, str(git_root / "absent.git"))) == [], "absent store objected")
    unknown = plan_for(home, str(git_root / "absent.git"), service_user="nobody-here")
    check(commit_store_problems(unknown) == [], "an absent store needed the service identity")
    problems = commit_store_problems(plan_for(home, str(store), service_user="nobody-here"))
    check(len(problems) == 1 and isinstance(problems[0], OperatorDependency)
          and "nobody-here does not exist" in problems[0], f"present store, unknown service: {problems}")
    link = data / "link.git"
    link.symlink_to(store)
    check(commit_store_problems(plan_for(home, str(link))) == [], "a link to a readable store objected")
    run(["setfacl", "-x", f"g:{OWNER}", str(git_root)])
    problems = commit_store_problems(plan_for(home, str(link)))
    check(len(problems) == 1 and f"{git_root} is" in problems[0], f"link to closed store: {problems}")

    # 11. In the owner home: an ordinary objection the repair offers to close;
    #     with board_service_traversal=false, nothing grants it, so a dependency.
    inside = home / "fixpatch.git"
    bare_store(inside, root / "s2")
    for item in [inside, *inside.rglob("*")]:
        os.lchown(item, OWNER_UID, OWNER_UID)
    # The home's group is the unit's group, so its 0710 already lets the
    # board through; the store's own 0700 is what stops it.
    os.chmod(inside, 0o700)
    problems = commit_store_problems(plan_for(home, str(inside)))
    check(len(problems) == 1 and not isinstance(problems[0], OperatorDependency)
          and f"{inside} is {OWNER}:{OWNER} mode 0700" in problems[0], f"in-home: {problems}")
    problems = commit_store_problems(plan_for(home, str(inside), board_service_traversal=False))
    check(len(problems) == 1 and isinstance(problems[0], OperatorDependency)
          and "board_service_traversal=false" in problems[0], f"no traversal: {problems}")


def tenant(root: Path, store: Path):
    """An installed MEFP whose boundary is closed apart from what each case opens."""
    home = root / "home" / OWNER
    base = home / f"{PROJECT}-worktrees"
    (base / "main").mkdir(parents=True)
    (base / "main" / "source.py").write_text("secret\n", encoding="utf-8")
    (home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git" / "objects").mkdir(parents=True)
    for item in [home, *home.rglob("*")]:
        os.chown(item, OWNER_UID, OWNER_UID)
    for path in (root, root / "home"):
        os.chmod(path, 0o711)
    os.chmod(home, 0o710)
    for path in (base, base / "main"):
        os.chmod(path, 0o750)
    run(["setfacl", "-m", "d:u::rwx,d:g::r-x,d:o::---", str(base)])
    plan = plan_for(home, str(store))
    launcher.install_privileged_artifacts(plan, launcher.render_privileged_artifacts(plan))
    return home, base, plan


def repair_cases(root: Path) -> None:
    from repository_boundary_repair_test import journal_entries, readiness, repair
    from root_workflow_record_test import tenant_identity

    os.environ[SEAM] = str(root)
    os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(root / "journal")
    git_root = root / "data" / "git"
    git_root.mkdir(parents=True)
    store = git_root / "fixpatch"
    commit = bare_store(store, root)
    foreign(git_root, 0o750)
    foreign(store, 0o750)
    home, base, plan = tenant(root, store)

    def identity():
        return tenant_identity(home, owner=OWNER)

    def outcome():
        return journal_entries(root, PROJECT)[-1]

    packet = (root / PROJECT / "operator-commands.sh").read_text(encoding="utf-8")
    phase, _problem = repository_boundary_phase(packet)
    check(phase and not any(str(git_root) in line for line in phase), "the phase names the foreign store")
    untouched = snapshot(git_root)

    # 1. Everything else closed, the store unreachable: the dry run fails and
    #    says why, and records that.
    with identity():
        status, said = repair(PROJECT)
    joined = "\n".join(said)
    check(status == 1, f"dry run exit {status}: {joined}")
    check(f"{git_root} is {FOREIGN}:{FOREIGN} mode 0750" in joined and "operator outside" in joined, joined)
    check("Resolve the operator dependency" in joined and "already closed" not in joined, joined)
    check((outcome()["status"], outcome()["detail"]) == ("failed", "dry-run; commit store unreachable"), f"{outcome()}")
    dry_dependency = [line for line in said if "repair-boundary cannot close this" in line]

    # 2. The apply runs nothing it cannot use, fails, and leaves the phase
    #    incomplete in the journal. The foreign tree is exactly as it was.
    with identity():
        status, said = repair(PROJECT, apply=True)
    joined = "\n".join(said)
    check(status == 1 and "nothing was run" in joined and "repository boundary is closed" not in joined, joined)
    check([line for line in said if "repair-boundary cannot close this" in line] == dry_dependency,
          "the dry run and the apply name different dependencies")
    check((outcome()["status"], outcome()["exit_status"], outcome()["detail"])
          == ("failed", 1, "commit store unreachable"), f"{outcome()}")
    check(snapshot(git_root) == untouched, "the repair changed the foreign store")

    # 3. With the tenant's own boundary open too, the apply closes that and
    #    still does not report success while the store is unreachable.
    os.chmod(base, 0o755)
    with identity():
        status, said = repair(PROJECT, apply=True)
    joined = "\n".join(said)
    check(status == 1 and "still open after the repair" in joined and str(git_root) in joined, joined)
    check(stat.S_IMODE(base.stat().st_mode) == 0o750, "the repairable half was not repaired")
    check(outcome()["detail"] == "commit store unreachable", f"{outcome()}")
    check(snapshot(git_root) == untouched, "the repair changed the foreign store")

    # 4. Readiness names the dependency without offering the repair as its cure.
    with identity():
        problems = readiness(plan)
    store_lines = [line for line in problems if str(git_root) in line]
    check(len(store_lines) == 1 and "--apply`" not in store_lines[0].split("Then run")[0]
          and not store_lines[0].endswith("--apply`"), f"{store_lines}")
    check(not board_verifies(store, commit), "fixture: the board must not yet reach the store")

    # 5. Once the operator grants read/traverse, the same repair finds the
    #    boundary closed and the board resolves a commit from the store.
    run(["setfacl", "-m", f"g:{OWNER}:--x", str(git_root)])
    run(["setfacl", "-R", "-m", f"g:{OWNER}:rX,d:g:{OWNER}:rX", str(store)])
    with identity():
        status, said = repair(PROJECT, apply=True)
    check(status == 0 and any("already closed" in line for line in said), f"{said}")
    check(board_verifies(store, commit), "the board cannot resolve a commit after the grant")


def main() -> int:
    if "--privileged-child" in sys.argv:
        with tempfile.TemporaryDirectory(prefix="syrd604.") as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            name_the_accounts(root)
            (root / "probe").mkdir()
            (root / "probe").chmod(0o755)
            probe_cases(root / "probe")
            (root / "repair").mkdir()
            (root / "repair").chmod(0o755)
            repair_cases(root / "repair")
        print(f"commit_store_access_test: {CHECKS} checks ok")
        return 0
    for tool in ("setfacl", "getfacl", "unshare", "setpriv", "git"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    assert namespaces_available(), "user namespaces are required; this suite must not silently skip"
    done = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable,
         str(Path(__file__).resolve()), "--privileged-child"],
        text=True, capture_output=True,
    )
    print((done.stdout + done.stderr).strip())
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
