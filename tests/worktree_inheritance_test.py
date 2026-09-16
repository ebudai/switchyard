#!/usr/bin/env python3
"""SYRD-181: a repaired worktree base stays repaired when the next tree arrives.

SYRD-146 closed syrd's repository boundary at journal 0090: the base 0750, no
world-readable trees, the socket group's entry gone. By journal 0092 two later
ticket worktrees -- syrd-178-main and syrd-180-app -- stood at 0755. Nothing had
undone the repair. `git worktree add` had simply made two directories with the
ordinary umask, and the repair only ever spoke about the trees that existed when
it ran.

Creating this ticket's own worktree made a third, which is the whole defect in
one command:

    $ git worktree add .../syrd-worktrees/syrd-181-main origin/main
    $ stat -c '%a' .../syrd-worktrees/syrd-181-main
    755

A mode on the base cannot fix that and neither can a sweep, because the thing
being fixed has not been created yet. A DEFAULT ACL can: the kernel intersects
it with whatever mode the creator asks for, so a directory made here is closed
whoever makes it and however -- git, the launcher, an implementer's own
command, a script nobody has written yet.

The kernel decides here too. Real uids in a user namespace, real ACLs, real
`git worktree add`, and `setpriv` asking the board service account to read what
it must not.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    INHERITED_WORKTREE_CLOSURE,
    build_plan,
    render_operator_commands,
    repository_boundary_phase,
    repository_boundary_statements,
)
from resume_provision_test import SEAM, namespaces_available  # noqa: E402

PROJECT = "porter"
OWNER_UID = 1500
SERVICE_UID = 1600
OTHER_UID = 1700
SOCKET_GID = 1900
SECRET = "the tenant's source, across every role and every ticket\n"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def require_tools() -> None:
    for tool in ("setfacl", "getfacl", "unshare", "setpriv", "git", "find"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True, **kwargs)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


# --------------------------------------------------------------------------
# what the packet says, before any of it is run
# --------------------------------------------------------------------------


def live_plan(**overrides):
    arguments = {
        "project": "syrd",
        "owner_user": "switchyard-agent",
        "owner_home": Path("/home/switchyard-agent"),
        "commit_git_dir": "/home/switchyard-agent/syrd-source-cache.git",
    }
    arguments.update(overrides)
    return build_plan(**arguments)


def test_the_boundary_phase_closes_what_does_not_exist_yet() -> None:
    """The repair has to speak about the next worktree, not only the last."""
    phase, problem = repository_boundary_phase(render_operator_commands(live_plan()))
    check(not problem, f"the phase still parses: {problem}")
    joined = "\n".join(phase)
    base = "/home/switchyard-agent/syrd-worktrees"
    check(
        f"sudo setfacl -m '{INHERITED_WORKTREE_CLOSURE}' '{base}'" in joined,
        f"the base is given an inherited closure: {joined}",
    )
    # Inside the same guard as the sweep: a partial run must not die on a base
    # that has not been made yet, and both lines are about the same directory.
    statements = [s for s in repository_boundary_statements(phase) if any("setfacl -m 'd:" in l for l in s)]
    check(len(statements) == 1, f"one statement carries it: {statements}")
    check(
        statements[0][0].strip().startswith("if [ -d ") and statements[0][-1].strip() == "fi",
        f"and it is guarded on the base existing: {statements[0]}",
    )


def test_the_closure_grants_nobody_anything_new() -> None:
    """It is the base's own access, made inheritable -- not a new grant."""
    parts = INHERITED_WORKTREE_CLOSURE.split(",")
    # Every entry is `default:<kind>::<bits>` -- the empty middle field is what
    # makes it the file's own owner, group and world rather than a named
    # principal. An inherited grant to somebody by name would hand every future
    # worktree to them, which is the opposite of the repair.
    parsed = {}
    for part in parts:
        kind, name, bits = part.split(":", 1)[1].split(":")
        check(name == "", f"nothing is granted to anybody by name: {part}")
        parsed[kind] = bits
    check(parsed == {"u": "rwx", "g": "r-x", "o": "---"}, f"the base's own access: {parsed}")


# --------------------------------------------------------------------------
# the sequence the ticket names, against a real tree
# --------------------------------------------------------------------------


def as_account(uid: int, argv: list[str], *, groups: str = "") -> bool:
    """Whether that account can do this. The kernel answers, not a mode."""
    privileges = [f"--reuid={uid}", f"--regid={uid}"]
    privileges.append(f"--groups={groups}" if groups else "--clear-groups")
    return subprocess.run(["setpriv", *privileges, *argv], capture_output=True).returncode == 0


def apply_boundary(plan, *, sudo_stub: Path) -> None:
    """Run the packet's boundary phase, the way `repair-boundary` runs it."""
    phase, problem = repository_boundary_phase(render_operator_commands(plan))
    assert not problem, problem
    environment = {**os.environ, "PATH": f"{sudo_stub}{os.pathsep}{os.environ['PATH']}"}
    for statement in repository_boundary_statements(phase):
        done = subprocess.run(
            ["sh", "-c", "\n".join(statement)], env=environment, text=True, capture_output=True
        )
        assert done.returncode == 0, (statement, done.stderr)


def sudo_that_only_execs(root: Path) -> Path:
    """`sudo` is how the packet spells "as root"; this namespace already is."""
    stubs = root / "bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    return stubs


def tenant_worktree(source: Path, path: Path) -> None:
    """A worktree created the way a ticket's is: `git worktree add`, as the tenant.

    Run as the owner rather than as root, because that is who runs it, and the
    mode a new directory gets is decided by the creator's umask -- 022 here, the
    ordinary one, which is exactly what reopened the boundary.
    """
    done = subprocess.run(
        ["setpriv", f"--reuid={OWNER_UID}", f"--regid={OWNER_UID}", "--clear-groups",
         "git", "-C", str(source), "worktree", "add", "--detach", str(path)],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(source.parent), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    assert done.returncode == 0, (done.stdout, done.stderr)


def fixture(root: Path, *, per_role: bool = False):
    """syrd's shape: a home, a base with trees in it, and a source to branch."""
    home = root / "home" / "tenant"
    base = home / f"{PROJECT}-worktrees"
    source = home / "source"
    for name in ("main", "director"):
        (base / name).mkdir(parents=True)
        (base / name / "source.py").write_text(SECRET, encoding="utf-8")
    source.mkdir(parents=True)
    # The control repository the tenant's worktrees are linked to on a host.
    # The retirement half of the packet names it, so a fixture without one is a
    # fixture the packet cannot finish against.
    (home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git").mkdir(parents=True)
    run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(source)])
    (source / "file.txt").write_text(SECRET, encoding="utf-8")
    run(["git", "-C", str(source), "add", "file.txt"])
    run(["git", "-C", str(source), "-c", "user.email=t@e", "-c", "user.name=T",
         "commit", "-q", "-m", "seed"])
    for path in (root, root / "home"):
        os.chmod(path, 0o711)
    for path in home.rglob("*"):
        os.chown(path, OWNER_UID, OWNER_UID)
    for path in (home, base, source):
        os.chown(path, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    # The legacy state: the base and its trees as an older version left them.
    os.chmod(base, 0o755)
    for child in base.iterdir():
        os.chmod(child, 0o755)
    # The traversal the board service really has on a tenant's home: the packet
    # grants it so the service can reach the board release beneath. It is the
    # grant that makes "world-readable under here" mean something.
    run(["setfacl", "-m", f"u:{SERVICE_UID}:--x", str(home)])

    arguments = {
        "project": PROJECT,
        "owner_user": str(OWNER_UID),
        "owner_home": home,
        "service_user": str(SERVICE_UID),
        "commit_git_dir": str(home / f"{PROJECT}-source-cache.git"),
    }
    plan = build_plan(**arguments)
    if per_role:
        from dataclasses import replace

        accounts = (("director", f"{PROJECT}-director"), ("main", f"{PROJECT}-main"))
        plan = replace(
            plan,
            role_accounts=accounts,
            roles_group=project_provision.roles_group_name(PROJECT),
            role_worktrees=tuple((role, str(base / role)) for role, _ in accounts),
        )
    return home, base, source, plan


def writable_account_database() -> str:
    """Give this namespace an /etc it may write.

    A per-role tenant's packet creates the repository group and adds the role
    accounts to it -- `groupadd`, `gpasswd` -- and those take a lock file in
    /etc, which no namespace can do against the host's read-only copy. So the
    host's /etc is copied into this namespace and mounted over the real one:
    the account database is the host's plus the fixture's accounts, and every
    line of the packet runs as the real command against a real database.
    Nothing in the product is told about any of it, and the mount is private to
    this process tree.
    """
    # Outside any fixture directory: it is mounted over, and a mount point
    # cannot be removed by the temporary directory that contains it.
    etc = Path(tempfile.mkdtemp(prefix="syrd181-etc."))
    run(["mount", "--make-rprivate", "/"])
    run(["mount", "-t", "tmpfs", "tmpfs", str(etc)])
    # Best effort: this process cannot read root-only files like /etc/shadow --
    # nor, since SYRD-176, root's own provisioning directory -- and none of
    # them is what the account tools need. What they do need is asserted below.
    subprocess.run(["cp", "-a", "/etc/.", str(etc)], capture_output=True, text=True)
    for name, mode in (("shadow", 0o600), ("gshadow", 0o600)):
        target = etc / name
        if not target.exists():
            target.write_text("", encoding="utf-8")
        target.chmod(mode)
    passwd = etc / "passwd"
    passwd.write_text(
        Path("/etc/passwd").read_text(encoding="utf-8")
        + "".join(
            f"{name}:x:{uid}:{uid}::/nonexistent:/bin/sh\n"
            for name, uid in (
                (str(OWNER_UID), OWNER_UID),
                (str(SERVICE_UID), SERVICE_UID),
                (str(OTHER_UID), OTHER_UID),
                (f"{PROJECT}-director", 1801),
                (f"{PROJECT}-main", 1802),
            )
        ),
        encoding="utf-8",
    )
    group = etc / "group"
    group.write_text(
        Path("/etc/group").read_text(encoding="utf-8")
        + "".join(
            f"{name}:x:{gid}:\n"
            for name, gid in (
                (str(OWNER_UID), OWNER_UID),
                (str(SERVICE_UID), SERVICE_UID),
                (str(OTHER_UID), OTHER_UID),
                (f"{PROJECT}-roles", SOCKET_GID),
            )
        ),
        encoding="utf-8",
    )
    run(["mount", "--bind", str(etc), "/etc"])
    for probe in ("passwd", "group", "nsswitch.conf"):
        assert Path("/etc", probe).is_file(), f"the namespace's /etc is missing {probe}"
    import grp as grp_module

    assert grp_module.getgrnam(f"{PROJECT}-roles").gr_gid == SOCKET_GID
    return str(etc)


def privileged_cases() -> int:
    global CHECKS
    writable_account_database()
    with tempfile.TemporaryDirectory(prefix="syrd181-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        stubs = sudo_that_only_execs(root)
        home, base, source, plan = fixture(root)

        # 1. The legacy state, and the repair that closes it. This is SYRD-171
        #    and SYRD-175 doing what they already did.
        check(
            as_account(SERVICE_UID, ["cat", str(base / "main" / "source.py")]),
            "the fixture must start open, or nothing below proves anything",
        )
        apply_boundary(plan, sudo_stub=stubs)
        check(stat.S_IMODE(base.lstat().st_mode) == 0o750, oct(base.lstat().st_mode))
        check(
            not as_account(SERVICE_UID, ["cat", str(base / "main" / "source.py")]),
            "the trees that existed are closed",
        )

        # 2. The defect: a worktree created AFTER the repair, the ordinary way.
        #    This is the sequence from the ticket -- repair, then `git worktree
        #    add` -- and the mode it lands with is the whole finding.
        ticket_tree = base / "porter-181-main"
        tenant_worktree(source, ticket_tree)
        check(ticket_tree.is_dir(), "the worktree was created")
        check(
            stat.S_IMODE(ticket_tree.lstat().st_mode) & 0o007 == 0,
            f"a new worktree is closed to the world: {oct(ticket_tree.lstat().st_mode)}",
        )
        check(
            not as_account(SERVICE_UID, ["cat", str(ticket_tree / "file.txt")]),
            "and the board service cannot read it",
        )
        check(
            not as_account(SERVICE_UID, ["test", "-x", str(ticket_tree)], groups=str(SOCKET_GID)),
            "not through the socket group either",
        )
        check(
            not as_account(OTHER_UID, ["cat", str(ticket_tree / "file.txt")]),
            "and neither can an unrelated account",
        )
        check(
            as_account(OWNER_UID, ["cat", str(ticket_tree / "file.txt")]),
            "while the tenant reads its own worktree",
        )
        # Both halves of the closure, separately. The base standing in front of
        # it is not the reason it is unreachable: a principal let through the
        # base still cannot read the tree.
        run(["setfacl", "-m", f"u:{SERVICE_UID}:--x", str(base)])
        check(
            as_account(SERVICE_UID, ["test", "-x", str(base)]),
            "the fixture let the board service through the base",
        )
        check(
            not as_account(SERVICE_UID, ["cat", str(ticket_tree / "file.txt")]),
            "and it still cannot read the worktree behind it",
        )
        run(["setfacl", "-x", f"u:{SERVICE_UID}", str(base)])

        # 3. The detection says so before it happens, and stops saying so after.
        #    A base with no inherited closure is an open boundary even when
        #    every tree in it is closed.
        run(["setfacl", "-k", str(base)])
        objections = launcher.repository_boundary_problems(plan)
        check(
            any("inherited closure" in objection for objection in objections),
            f"a base that will reopen is reported: {objections}",
        )
        apply_boundary(plan, sudo_stub=stubs)
        check(
            not any("inherited closure" in objection for objection in launcher.repository_boundary_problems(plan)),
            f"and the repair settles it: {launcher.repository_boundary_problems(plan)}",
        )

        # 4. Repeatable, both halves. Running the repair again changes nothing,
        #    and each new worktree is closed like the last.
        entries_before = launcher._acl_entries(base, runner=subprocess.run)
        apply_boundary(plan, sudo_stub=stubs)
        check(
            launcher._acl_entries(base, runner=subprocess.run) == entries_before,
            "re-running the repair leaves the base exactly as it was",
        )
        for index in range(3):
            tree = base / f"porter-repeat-{index}"
            tenant_worktree(source, tree)
            check(
                stat.S_IMODE(tree.lstat().st_mode) & 0o007 == 0,
                f"worktree {index} is closed: {oct(tree.lstat().st_mode)}",
            )
        check(launcher.repository_boundary_problems(plan) == [],
              f"and the boundary stays closed: {launcher.repository_boundary_problems(plan)}")

        # 5. Creation, reuse and cleanup still work for the tenant. A closed
        #    worktree is a worktree.
        reuse = base / "porter-repeat-0"
        check(as_account(OWNER_UID, ["git", "-C", str(reuse), "status", "--porcelain"]),
              "the tenant can work in it")
        run(["setpriv", f"--reuid={OWNER_UID}", f"--regid={OWNER_UID}", "--clear-groups",
             "git", "-C", str(source), "worktree", "remove", str(reuse)],
            env={**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null"})
        check(not reuse.exists(), "and remove it")
        tenant_worktree(source, reuse)
        check(stat.S_IMODE(reuse.lstat().st_mode) & 0o007 == 0, "and create it again, closed")

        # 6. The commit store the board service must read is untouched by any
        #    of this.
        store = home / f"{PROJECT}-source-cache.git"
        check(store.is_dir(), "the packet created the commit store")
        check(as_account(SERVICE_UID, ["ls", str(store)]), "which the board service still reads")
        check(not as_account(SERVICE_UID, ["touch", str(store / "planted")]), "and cannot write")

    # 7. A per-role tenant is the same boundary, on its own fixture.
    with tempfile.TemporaryDirectory(prefix="syrd181-per-role.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        stubs = sudo_that_only_execs(root)
        home, base, source, plan = fixture(root, per_role=True)
        apply_boundary(plan, sudo_stub=stubs)
        tree = base / "porter-181-app"
        tenant_worktree(source, tree)
        check(stat.S_IMODE(tree.lstat().st_mode) & 0o007 == 0,
              f"a per-role tenant's new worktree is closed too: {oct(tree.lstat().st_mode)}")
        check(not as_account(SERVICE_UID, ["cat", str(tree / "file.txt")]),
              "and the board service cannot read it")

    # 8. A path with spaces and shell metacharacters is a path.
    with tempfile.TemporaryDirectory(prefix="syrd181-odd.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        stubs = sudo_that_only_execs(root)
        odd = root / "a tenant's home $(touch pwned) ; rm -rf ."
        odd.mkdir(parents=True)
        os.chown(odd, OWNER_UID, OWNER_UID)
        os.chmod(odd, 0o710)
        source = odd / "source"
        source.mkdir()
        (odd / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git").mkdir(parents=True)
        run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(source)])
        (source / "file.txt").write_text(SECRET, encoding="utf-8")
        run(["git", "-C", str(source), "add", "file.txt"])
        run(["git", "-C", str(source), "-c", "user.email=t@e", "-c", "user.name=T",
             "commit", "-q", "-m", "seed"])
        for path in (source, source / "file.txt", odd):
            os.chown(path, OWNER_UID, OWNER_UID)
        for path in odd.rglob("*"):
            os.chown(path, OWNER_UID, OWNER_UID)
        for path in source.rglob("*"):
            os.chown(path, OWNER_UID, OWNER_UID)
        plan = build_plan(
            project=PROJECT, owner_user=str(OWNER_UID), owner_home=odd,
            service_user=str(SERVICE_UID), commit_git_dir=str(odd / f"{PROJECT}-source-cache.git"),
        )
        apply_boundary(plan, sudo_stub=stubs)
        check(not (root / "pwned").exists() and not (Path.cwd() / "pwned").exists(),
              "the shell got no vote on which path was closed")
        base = odd / f"{PROJECT}-worktrees"
        tree = base / "porter-181-odd"
        tenant_worktree(source, tree)
        check(stat.S_IMODE(tree.lstat().st_mode) & 0o007 == 0,
              f"and a worktree under it is closed: {oct(tree.lstat().st_mode)}")
    return CHECKS


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    unprivileged = CHECKS
    require_tools()
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable,
             str(Path(__file__).resolve()), "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("worktree_inheritance_test: user namespaces unavailable; privileged half skipped")
    print(f"worktree_inheritance_test: {unprivileged} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    if "--privileged-child" in sys.argv:
        for name, value in sorted(globals().items()):
            if name.startswith("test_") and callable(value):
                value()
        total = privileged_cases()
        print(f"worktree_inheritance_test: privileged child ran {total} checks")
        raise SystemExit(0)
    raise SystemExit(main())
