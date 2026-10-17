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
    """The verifier's own call (`commit_cache.resolve_known_commit`), made as the unit's identity."""
    from scripts.ticket_board.commit_cache import commit_repo_git_args

    return as_board([*commit_repo_git_args(store), "cat-file", "-e", f"{commit}^{{commit}}"]).returncode == 0


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


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(*args: str) -> str:
    return run(["git", *args], env={**os.environ, **GIT_ENV}).stdout.strip()


def working_repo(path: Path, *extra: str) -> str:
    """A real working repository holding one commit; `extra` goes to `git init`."""
    git("init", "-q", *extra, str(path))
    (path / "f").write_text("x\n", encoding="utf-8")
    git("-C", str(path), "add", "f")
    git("-C", str(path), "commit", "-qm", "c")
    return git("-C", str(path), "rev-parse", "HEAD")


def bare_store(path: Path, scratch: Path) -> str:
    """A real bare repository holding one commit as a loose object."""
    work = scratch / f"seed-{path.name}"
    commit = working_repo(work)
    git("clone", "-q", "--bare", str(work), str(path))
    shutil.rmtree(work)
    return commit


def own(path: Path, uid: int, gid: int, directory_mode: int = 0o750, file_mode: int = 0o640) -> None:
    for item in [path, *path.rglob("*")]:
        os.lchown(item, uid, gid)
        if not item.is_symlink():
            os.chmod(item, directory_mode if item.is_dir() else file_mode)


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
    # The grant for a store reached through a link is made on the store, and works.
    run(["setfacl", "-m", f"g:{OWNER}:--x", str(git_root)])
    run(["setfacl", "-R", "-b", str(store)])
    link_plan = plan_for(home, str(link))
    problems = probe_matches_kernel(link_plan, link, commit, False, "link to a closed store")
    commands = [part.strip("`") for part in problems[0].split("the read/traverse-only grant is: ", 1)[1]
                .split(". Then run", 1)[0].split("; ")]
    check(commands == [f"sudo setfacl -R -m g:{OWNER}:rX,d:g:{OWNER}:rX {store}"], f"{commands}")
    run(shlex.split(commands[0]))
    probe_matches_kernel(link_plan, link, commit, True, "link, after the printed grant")
    run(["setfacl", "-x", f"g:{OWNER}", str(git_root)])

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

    layout_cases(root, home)


def layout_cases(root: Path, home: Path) -> None:
    """Everything git reads to resolve a commit, in every layout the verifier opens (audit of 0a0d3f70)."""
    lab = root / "layouts"
    lab.mkdir()
    os.chmod(lab, 0o755)

    # 12. A bare store whose top is readable but whose commit object is not
    #     (the audit's second reproduction).
    bare = lab / "loose.git"
    commit = bare_store(bare, lab)
    own(bare, FOREIGN_UID, OWNER_UID)
    plan = plan_for(home, str(bare))
    probe_matches_kernel(plan, bare, commit, True, "group-readable bare store")
    loose = bare / "objects" / commit[:2] / commit[2:]
    check(loose.is_file(), "fixture: the commit must be a loose object")
    os.chmod(loose, 0o000)
    problems = probe_matches_kernel(plan, bare, commit, False, "loose commit object closed")
    check(f"{loose} is {FOREIGN}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(loose, 0o640)

    # 13. Packed storage: the pack closed, its index open.
    git(f"--git-dir={bare}", "repack", "-q", "-a", "-d")
    git(f"--git-dir={bare}", "prune-packed")
    own(bare, FOREIGN_UID, OWNER_UID)
    check(not loose.exists(), "fixture: the commit must now be packed")
    pack = next((bare / "objects" / "pack").glob("*.pack"))
    probe_matches_kernel(plan, bare, commit, True, "packed and readable")
    os.chmod(pack, 0o600)
    problems = probe_matches_kernel(plan, bare, commit, False, "pack closed")
    check(f"{pack} is" in problems[0], problems[0])
    os.chmod(pack, 0o640)

    # 14. A working repository the service owns: read through `.git`, and
    #     closed when `.git` is (the audit's first reproduction).
    work = lab / "work"
    commit = working_repo(work)
    own(work, SERVICE_UID, OWNER_UID)
    plan = plan_for(home, str(work))
    probe_matches_kernel(plan, work, commit, True, "working repository the service owns")
    os.chmod(work / ".git", 0o000)
    problems = probe_matches_kernel(plan, work, commit, False, "working repository with .git closed")
    check(f"{work / '.git'} is {SERVICE}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(work / ".git", 0o750)

    # 15. The same repository owned by another account is readable and still
    #     refused: git's ownership check, which no grant changes.
    own(work, FOREIGN_UID, OWNER_UID)
    problems = probe_matches_kernel(plan, work, commit, False, "working repository another account owns")
    check(isinstance(problems[0], OperatorDependency) and "dubious ownership" in problems[0]
          and f"{work} is {FOREIGN}" in problems[0] and "setfacl" not in problems[0], problems[0])

    # 16. A gitfile: `.git` names a git directory kept elsewhere.
    separate, vault = lab / "separate", lab / "vault"
    gitdir = vault / "separate.git"
    vault.mkdir()
    commit = working_repo(separate, f"--separate-git-dir={gitdir}")
    own(separate, SERVICE_UID, OWNER_UID)
    own(vault, SERVICE_UID, OWNER_UID)
    plan = plan_for(home, str(separate))
    probe_matches_kernel(plan, separate, commit, True, "gitfile layout")
    os.chmod(vault, 0o000)
    problems = probe_matches_kernel(plan, separate, commit, False, "the directory above the gitfile's target closed")
    check(f"{vault} is {SERVICE}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(vault, 0o750)
    os.chmod(gitdir / "objects", 0o000)
    problems = probe_matches_kernel(plan, separate, commit, False, "gitfile's git directory closed")
    check(f"{gitdir / 'objects'} is" in problems[0], problems[0])
    os.chmod(gitdir / "objects", 0o750)

    # 17. A linked worktree: HEAD in its own git directory, objects in the
    #     common one named by `commondir`.
    main, linked = lab / "main", lab / "linked"
    commit = working_repo(main)
    git("-C", str(main), "worktree", "add", "-q", "--detach", str(linked))
    own(main, SERVICE_UID, OWNER_UID)
    own(linked, SERVICE_UID, OWNER_UID)
    plan = plan_for(home, str(linked))
    probe_matches_kernel(plan, linked, commit, True, "linked worktree")
    os.chmod(main, 0o000)
    problems = probe_matches_kernel(plan, linked, commit, False, "the main worktree above the common directory closed")
    check(f"{main} is {SERVICE}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(main, 0o750)
    os.chmod(main / ".git" / "objects", 0o000)
    problems = probe_matches_kernel(plan, linked, commit, False, "linked worktree's common objects closed")
    check(f"{main / '.git' / 'objects'} is" in problems[0], problems[0])
    os.chmod(main / ".git" / "objects", 0o750)

    # 17b. A git directory whose `commondir` names a repository elsewhere:
    #      the directories above that one are on the path too.
    elsewhere, sideways, front = lab / "elsewhere", lab / "sideways", lab / "front"
    elsewhere.mkdir()
    commit = bare_store(elsewhere / "common.git", lab)
    sideways.mkdir()
    (sideways / "HEAD").write_text(commit + "\n", encoding="utf-8")
    (sideways / "commondir").write_text(f"{elsewhere / 'common.git'}\n", encoding="utf-8")
    front.mkdir()
    (front / ".git").write_text(f"gitdir: {sideways}\n", encoding="utf-8")
    for path in (elsewhere, sideways, front):
        own(path, SERVICE_UID, OWNER_UID)
    plan = plan_for(home, str(front))
    probe_matches_kernel(plan, front, commit, True, "commondir elsewhere")
    os.chmod(elsewhere, 0o000)
    problems = probe_matches_kernel(plan, front, commit, False, "the directory above a commondir elsewhere closed")
    check(f"{elsewhere} is {SERVICE}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(elsewhere, 0o750)

    # 18. Alternates: the commit lives in another repository's objects.
    source, shared = lab / "source.git", lab / "shared.git"
    commit = bare_store(source, lab)
    git("clone", "-q", "--bare", "--shared", str(source), str(shared))
    own(source, FOREIGN_UID, OWNER_UID)
    own(shared, FOREIGN_UID, OWNER_UID)
    check((shared / "objects" / "info" / "alternates").is_file()
          and not (shared / "objects" / commit[:2]).exists(), "fixture: the commit must be only in the alternate")
    plan = plan_for(home, str(shared))
    probe_matches_kernel(plan, shared, commit, True, "alternate readable")
    os.chmod(source / "objects", 0o700)
    problems = probe_matches_kernel(plan, shared, commit, False, "alternate object store closed")
    check(f"{source / 'objects'} is" in problems[0], problems[0])
    os.chmod(source / "objects", 0o750)
    os.chmod(source, 0o700)
    problems = probe_matches_kernel(plan, shared, commit, False, "the repository above the alternate closed")
    check(f"{source} is {FOREIGN}:{OWNER} mode 0700" in problems[0], problems[0])
    os.chmod(source / "objects", 0o700)
    problems = probe_matches_kernel(plan, shared, commit, False, "the alternate and the repository above it closed")
    commands = [part.strip("`") for part in problems[0].split("the read/traverse-only grant is: ", 1)[1]
                .split(". Then run", 1)[0].split("; ")]
    check(commands == [f"sudo setfacl -m g:{OWNER}:--x {source}",
                       f"sudo setfacl -R -m g:{OWNER}:rX,d:g:{OWNER}:rX {source / 'objects'}"], f"{commands}")
    for command in commands:
        run(shlex.split(command))
    probe_matches_kernel(plan, shared, commit, True, "after the printed grant on the alternate")

    # 19. `objects` itself a link to a directory elsewhere (the audit's second
    #     reproduction of 7fa34b51): git follows it, and so does the probe.
    linked_db, moved = lab / "linked-db.git", lab / "moved" / "objects"
    commit = bare_store(linked_db, lab)
    moved.parent.mkdir()
    shutil.move(str(linked_db / "objects"), str(moved))
    (linked_db / "objects").symlink_to(moved)
    own(linked_db, FOREIGN_UID, OWNER_UID)
    own(moved.parent, FOREIGN_UID, OWNER_UID)
    plan = plan_for(home, str(linked_db))
    probe_matches_kernel(plan, linked_db, commit, True, "objects is a link, target readable")
    target = moved / commit[:2] / commit[2:]
    os.chmod(target, 0o000)
    problems = probe_matches_kernel(plan, linked_db, commit, False, "objects is a link, commit object closed")
    check(f"{target} is {FOREIGN}:{OWNER} mode 0000" in problems[0], problems[0])
    commands = [part.strip("`") for part in problems[0].split("the read/traverse-only grant is: ", 1)[1]
                .split(". Then run", 1)[0].split("; ")]
    check(commands == [f"sudo setfacl -R -m g:{OWNER}:rX,d:g:{OWNER}:rX {moved}"], f"{commands}")
    run(shlex.split(commands[0]))
    probe_matches_kernel(plan, linked_db, commit, True, "objects is a link, after the printed grant")
    run(["setfacl", "-R", "-b", str(moved)])
    os.chmod(target, 0o640)
    os.chmod(moved.parent, 0o700)
    problems = probe_matches_kernel(plan, linked_db, commit, False, "objects is a link, the target's parent closed")
    check(f"{moved.parent} is {FOREIGN}:{OWNER} mode 0700" in problems[0], problems[0])
    os.chmod(moved.parent, 0o750)

    # 20. One fan-out directory a link (the audit's first reproduction).
    fanned, elsewhere_fan = lab / "fanned.git", lab / "fan-elsewhere"
    commit = bare_store(fanned, lab)
    elsewhere_fan.mkdir()
    shutil.move(str(fanned / "objects" / commit[:2]), str(elsewhere_fan / commit[:2]))
    (fanned / "objects" / commit[:2]).symlink_to(elsewhere_fan / commit[:2])
    own(fanned, FOREIGN_UID, OWNER_UID)
    own(elsewhere_fan, FOREIGN_UID, OWNER_UID)
    plan = plan_for(home, str(fanned))
    probe_matches_kernel(plan, fanned, commit, True, "fan-out link, target readable")
    target = elsewhere_fan / commit[:2] / commit[2:]
    os.chmod(target, 0o000)
    problems = probe_matches_kernel(plan, fanned, commit, False, "fan-out link, commit object closed")
    check(f"{target} is {FOREIGN}:{OWNER} mode 0000" in problems[0], problems[0])
    os.chmod(target, 0o640)
    os.chmod(elsewhere_fan / commit[:2], 0o700)
    problems = probe_matches_kernel(plan, fanned, commit, False, "fan-out link, target directory closed")
    check(f"{elsewhere_fan / commit[:2]} is {FOREIGN}:{OWNER} mode 0700" in problems[0], problems[0])
    os.chmod(elsewhere_fan / commit[:2], 0o750)

    # 21. A link cycle inside the object database ends; it is walked once.
    (fanned / "objects" / "loop").symlink_to(fanned / "objects")
    probe_matches_kernel(plan, fanned, commit, True, "a link cycle in the object database")


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

    # 6. Readable down to the top-level `objects`, but not the commit object:
    #    neither the dry run nor the apply may call that closed.
    loose = store / "objects" / commit[:2] / commit[2:]
    mode = stat.S_IMODE(loose.stat().st_mode)
    os.chmod(loose, 0o000)
    untouched = snapshot(git_root)
    for apply in (False, True):
        with identity():
            status, said = repair(PROJECT, apply=apply)
        joined = "\n".join(said)
        check(status == 1 and str(loose) in joined and "already closed" not in joined
              and "repository boundary is closed" not in joined, f"loose object closed, apply={apply}: {joined}")
    check(snapshot(git_root) == untouched and not board_verifies(store, commit), "loose object case")
    os.chmod(loose, mode)

    # 7. The same path as a working repository: open, then with `.git` closed.
    shutil.rmtree(store)
    store.mkdir()
    commit = working_repo(store)
    own(store, SERVICE_UID, OWNER_UID)
    with identity():
        status, said = repair(PROJECT, apply=True)
    check(status == 0 and any("already closed" in line for line in said) and board_verifies(store, commit),
          f"working repository open: {said}")
    os.chmod(store / ".git", 0o000)
    untouched = snapshot(git_root)
    for apply in (False, True):
        with identity():
            status, said = repair(PROJECT, apply=apply)
        joined = "\n".join(said)
        check(status == 1 and str(store / ".git") in joined and "already closed" not in joined
              and "repository boundary is closed" not in joined, f".git closed, apply={apply}: {joined}")
    check(snapshot(git_root) == untouched and not board_verifies(store, commit), ".git closed case")

    # 8 and 9. A bare store whose `objects` is a link, and one whose commit's
    #    fan-out directory is a link, each to a tree elsewhere: open, then with
    #    the commit object closed behind the link (audit of 7fa34b51).
    moved = root / "moved"
    for variant in ("objects", "fan-out"):
        shutil.rmtree(store)
        if moved.exists():
            shutil.rmtree(moved)
        moved.mkdir()
        commit = bare_store(store, root)
        if variant == "objects":
            shutil.move(str(store / "objects"), str(moved / "objects"))
            (store / "objects").symlink_to(moved / "objects")
            target = moved / "objects" / commit[:2] / commit[2:]
        else:
            shutil.move(str(store / "objects" / commit[:2]), str(moved / commit[:2]))
            (store / "objects" / commit[:2]).symlink_to(moved / commit[:2])
            target = moved / commit[:2] / commit[2:]
        own(store, FOREIGN_UID, OWNER_UID)
        own(moved, FOREIGN_UID, OWNER_UID)
        with identity():
            status, said = repair(PROJECT, apply=True)
        check(status == 0 and any("already closed" in line for line in said) and board_verifies(store, commit),
              f"{variant} link open: {said}")
        os.chmod(target, 0o000)
        untouched = snapshot(git_root) + snapshot(moved)
        for apply in (False, True):
            with identity():
                status, said = repair(PROJECT, apply=apply)
            joined = "\n".join(said)
            check(status == 1 and str(target) in joined and "already closed" not in joined
                  and "repository boundary is closed" not in joined, f"{variant} link, apply={apply}: {joined}")
        check(snapshot(git_root) + snapshot(moved) == untouched and not board_verifies(store, commit),
              f"{variant} link case")


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
