#!/usr/bin/env python3
"""SYRD-630: root chooses which account runs git, and what a role worktree is, by nothing a path leads to.

As root, the git chokepoint chose the account by resolving the target and
every rule's root, and with no rule matched, by who owned what the target led
to -- root itself for a root-owned directory a tenant's link or `..` reached,
or another account's tree. The owner runner decided whether `mkdir -p` ran as
the owner or as root the same way, the project's rules compared role
directories by `resolve()`, and the role worktrees were found and their `.git`
read by path, the `gitdir:` they named resolved.

Now root matches rules on the path as written -- normalised and absolute, or
refused -- walks the target by descriptor from `/` and refuses a link at it or
above it, runs git itself only in a tree root alone controls, and refuses an
unmatched target otherwise. Role worktrees are looked at and read by descriptor
from the owner's root, and `gitdir:` is compared as written. Each case runs as
root in a user namespace, under a recorder of every pathname lookup in the
tenant's home (which must see none), against a recording runner, and proves a
foreign tree and every decoy a link names unchanged byte for byte.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import control_repository_writes_test as fixture  # noqa: E402
from control_repository_writes_test import OWNER, Host, Runner, lookups, snapshot, standing_in  # noqa: E402
from scripts import launcher_checkout, owner_git, project_worktrees  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def git(target: Path | str, rules, runner: Runner) -> subprocess.CompletedProcess:
    return launcher.run_owner_correct_git(["git", "-C", str(target), "status"], runner=runner, owner_rules=rules)


def as_owner(call: list[str]) -> bool:
    return call[:3] == ["sudo", "-u", OWNER]


def with_role(host: Host) -> None:
    host.config = replace(host.config, roles=[replace(Host.role(), workdir=str(host.home / "trees" / "ops"))])


def chosen_by_rule(host: Host) -> None:
    """Every target a rule names runs as the rule's account, matched as written: nothing looked up by path."""
    host.bare(host.repository)
    with_role(host)
    with lookups(host.homes) as seen:
        rules = project_worktrees._config_git_owner_rules(host.config)
        for target in (host.store, host.repository, host.home / "trees" / "ops", host.home / "trees" / "missing"):
            runner = Runner()
            done = git(target, rules, runner)
            check(done.returncode == 0 and runner.calls == [["sudo", "-u", OWNER, "git", "-C", str(target), "status"]],
                  f"{target.relative_to(host.home)} runs as the owner: {done.stderr} {runner.calls}")
        runner = Runner()
        launcher.run_owner_correct_git(["git", "clone", "--bare", str(host.repository), str(host.store)],
                                       runner=runner, owner_rules=rules)
        check(runner.calls and as_owner(runner.calls[0]), f"the clone's destination names the owner: {runner.calls}")
    check(seen == [], f"the rules and every choice made without a lookup in the home: {seen}")


def links(host: Host) -> None:
    """A link at the target or at any directory above it: refused before git runs, as anyone."""
    host.bare(host.repository)
    rules = project_worktrees._config_git_owner_rules(host.config)
    for index, at in enumerate([host.store, *reversed(host.chain), host.home / ".local", host.home]):
        place, before = host.decoy(f"link-{index}")
        aside = host.swap(at, place)
        runner = Runner()
        with lookups(host.homes) as seen:
            done = git(host.store, rules, runner)
        host.unswap(at, aside)
        check(done.returncode == 125 and "is a link" in str(done.stderr) and runner.calls == [] and seen == []
              and snapshot(place) == before and host.foreign_unchanged(),
              f"a link at {at}: refused, git run by nobody: {done.stderr} {runner.calls} {seen}")


def unmatched(host: Host) -> None:
    """A target no rule names is refused -- never run as root, never as whoever owns what it leads to."""
    host.bare(host.repository)
    place, before = host.decoy("unmatched")
    (host.home / "trees").mkdir()
    os.lchown(host.home / "trees", fixture.OWNER_UID, fixture.OWNER_UID)
    reach = host.home / "trees" / "reach"
    os.symlink(place, reach)
    for target, label in ((host.chain[-1], "a root-owned directory in the home"),
                          (host.repository, "the owner's own directory"),
                          (reach / "inner", "a root-owned directory a link leads to"),
                          (host.base / "foreign", "another account's tree")):
        runner = Runner()
        with lookups(host.homes) as seen:
            done = git(target, [], runner)
        check(done.returncode == 125 and runner.calls == [] and seen == [] and snapshot(place) == before
              and host.foreign_unchanged(), f"{label}: refused, run by nobody: {done.stderr} {runner.calls} {seen}")


def lexical(host: Host) -> None:
    """A target or a rule that is not a normalised absolute path is refused, never resolved into a match."""
    host.bare(host.repository)
    trees = host.home / "trees"
    trees.mkdir()
    rules = [launcher.GitOwnerRule(trees, OWNER)]
    escape = f"{trees}/../../../foreign"
    for target in (escape, f"{trees}/../Projects/p", "trees/ops"):
        runner = Runner()
        with lookups(host.homes) as seen:
            done = git(target, rules, runner)
        check(done.returncode == 125 and "normalised absolute" in str(done.stderr) and runner.calls == [] and seen == []
              and host.foreign_unchanged(), f"{target}: refused as written: {done.stderr} {runner.calls} {seen}")
    runner = Runner()
    with lookups(host.homes) as seen:
        done = git(host.store, [launcher.GitOwnerRule(Path(f"{trees}/../.local/state"), OWNER)], runner)
    check(done.returncode == 125 and runner.calls == [] and seen == [],
          f"a rule whose root is not as written matches nothing: {done.stderr} {runner.calls} {seen}")


def raced(host: Host) -> None:
    """The target swapped for a link to another account's tree after root walked it: the account stays the rule's."""
    host.bare(host.repository)
    rules = project_worktrees._config_git_owner_rules(host.config)
    real_walk = owner_git._walk_git_target
    moved: list[Path] = []

    def walk(text):
        answer = real_walk(text)
        moved.append(host.swap(host.store, host.foreign))
        return answer

    runner = Runner()
    with standing_in(owner_git, _walk_git_target=walk), lookups(host.homes) as seen:
        done = git(host.store, rules, runner)
    host.unswap(host.store, moved[0])
    check(done.returncode == 0 and runner.calls == [["sudo", "-u", OWNER, "git", "-C", str(host.store), "status"]]
          and seen == [] and host.foreign_unchanged(),
          f"swapped after the walk: still the owner's account, never the tree's: {runner.calls} {seen}")


def owner_runner(host: Host) -> None:
    """The owner runner as root: `mkdir -p` in the owner's roots as the owner, anywhere else refused, by the path as
    written; git through the chokepoint."""
    runner = Runner()
    wrapped = launcher._owner_project_git_runner(
        owner_user=OWNER, project_dir=host.repository, owned_roots=launcher._control_repository_owned_roots(host.config),
        runner=runner)
    place, before = host.decoy("runner")
    with lookups(host.homes) as seen:
        made = wrapped(["mkdir", "-p", str(host.home / "trees")])
        away = wrapped(["mkdir", "-p", str(host.home / "trees" / ".." / ".." / ".." / "decoy-runner" / "made")])
        probe = wrapped(["test", "-x", "/usr/bin/env"])
        status = wrapped(["git", "-C", str(host.repository), "status"])
    check(made.returncode == 0 and away.returncode == 125 and probe.returncode == 0 and status.returncode == 0,
          f"{made.returncode} {away.returncode} {away.stderr} {probe.returncode} {status.stderr}")
    check(runner.calls == [["sudo", "-u", OWNER, "mkdir", "-p", str(host.home / "trees")], ["test", "-x", "/usr/bin/env"],
                           ["sudo", "-u", OWNER, "git", "-C", str(host.repository), "status"]],
          f"made as the owner, the escape never made, root's own probe unchanged: {runner.calls}")
    check(seen == [] and snapshot(place) == before, f"decided as written, nothing looked up: {seen}")


class Worktree:
    """Each role worktree fixture: the role's `.git` as the case writes it, the owner's."""

    def __init__(self, host: Host) -> None:
        host.bare(host.repository)
        with_role(host)
        self.host = host
        self.trees = host.home / "trees"
        self.workdir = self.trees / "ops"
        self.entry = host.store / "worktrees" / "ops"
        self.entry.mkdir(parents=True)

    def make(self, dot_git: str | None) -> None:
        for item in (self.trees, self.workdir):
            item.mkdir(exist_ok=True)
            os.lchown(item, fixture.OWNER_UID, fixture.OWNER_UID)
        if dot_git is not None:
            (self.workdir / ".git").write_text(dot_git)
            os.lchown(self.workdir / ".git", fixture.OWNER_UID, fixture.OWNER_UID)

    def run(self, runner: Runner | None = None):
        runner = runner or Runner()
        with lookups(self.host.homes) as seen:
            result = launcher.ensure_control_role_worktrees(self.host.config, refresh=True, runner=runner)
        gits = [call for call in runner.calls if "git" in call]
        return result, runner, gits, seen


def role_worktrees(host: Host) -> None:
    """`ensure_control_role_worktrees` whole as root, the chokepoint real: present or not, and what `.git` names,
    decided by descriptor; every git the owner's."""
    tree = Worktree(host)
    result, runner, gits, seen = tree.run()
    added = ["sudo", "-u", OWNER, "git", "--git-dir", str(host.store), "worktree", "add", "--detach", str(tree.workdir)]
    check(result.ok and any(call[:len(added)] == added for call in gits) and all(as_owner(call) for call in gits)
          and seen == [], f"a missing worktree is added by the owner: {result.failed_roles} {gits} {seen}")
    tree.make(f"gitdir: {tree.entry}\n")
    result, runner, gits, seen = tree.run()
    checked = ["sudo", "-u", OWNER, "git", "-C", str(tree.workdir), "rev-parse", "--is-inside-work-tree"]
    check(checked in gits and all(as_owner(call) for call in gits) and seen == [],
          f"this store's worktree is checked and refreshed by the owner: {result.failed_roles} {gits} {seen}")
    before = snapshot(host.home)
    result_again, _runner, gits_again, seen = tree.run()
    check(gits_again == gits and seen == [] and snapshot(host.home) == before, "a second run does the same, changing nothing")


def role_worktree_refusals(host: Host) -> None:
    """A `.git` that is a link, a repository, or names another store -- written or reached through `..` -- and a
    worktree or a directory above it that is a link: refused, no git run in it, nothing it names read or changed."""
    tree = Worktree(host)
    cases = [
        ("link", None, "is a link"),
        ("repository", None, "is a repository of its own"),
        ("foreign store", f"gitdir: {host.foreign}/worktrees/ops\n", f"is a worktree of {host.foreign}"),
        ("replaced store", f"gitdir: {host.store}/worktrees/gone\n", "has since been replaced"),
        ("dotted", f"gitdir: {tree.workdir}/../../.local/state/switchyard/projects/p/control.git/worktrees/ops\n",
         "nothing here adopts or resets another store's worktree"),
        ("relative", "gitdir: ../../.local/state/switchyard/projects/p/control.git/worktrees/ops\n",
         "nothing here adopts or resets another store's worktree"),
    ]
    for label, dot_git, expected in cases:
        tree.make(dot_git)
        if label == "link":
            os.symlink(host.foreign / "config", tree.workdir / ".git")
        if label == "repository":
            (tree.workdir / ".git").mkdir()
        result, runner, gits, seen = tree.run()
        reason = result.failed_roles.get("ops", "")
        check(expected in reason and "token" not in reason and not any(str(tree.workdir) in " ".join(c) for c in gits)
              and all(as_owner(call) for call in gits) and seen == [] and host.foreign_unchanged(),
              f"{label}: refused, no git in it: {reason!r} {gits} {seen}")
        fixture.PAUSED.append(True)
        try:
            shutil.rmtree(tree.workdir)
        finally:
            fixture.PAUSED.pop()
    for at in (tree.workdir, tree.trees):
        place, before = host.decoy(f"worktree-{at.name}")
        (place / "ops").mkdir()
        before = snapshot(place)
        tree.make(f"gitdir: {tree.entry}\n")
        aside = host.swap(at, place)
        result, runner, gits, seen = tree.run()
        host.unswap(at, aside)
        reason = result.failed_roles.get("ops", "")
        check(not result.ok and ("link" in reason) and not any(str(tree.workdir) in " ".join(c) for c in gits)
              and seen == [] and snapshot(place) == before,
              f"{at.relative_to(host.home)} a link: refused, nothing through it: {reason!r} {gits} {seen}")
    # Swapped for a link after root found the worktree: git still runs as the owner, never as root.
    tree.make(f"gitdir: {tree.entry}\n")
    place, before = host.decoy("worktree-race")
    real_present = project_worktrees._role_worktree_present_as_root
    moved: list[Path] = []

    def present(config, role_path):
        answer = real_present(config, role_path)
        if not moved:
            moved.append(host.swap(tree.workdir, place))
        return answer

    with standing_in(project_worktrees, _role_worktree_present_as_root=present):
        result, runner, gits, seen = tree.run()
    host.unswap(tree.workdir, moved[0])
    check(not any(call[:1] == ["git"] for call in runner.calls) and all(as_owner(call) for call in gits)
          and seen == [] and snapshot(place) == before,
          f"swapped after it was found: nothing run as root, nothing through the link: {result.failed_roles} {gits} {seen}")


def checkout_runner(host: Host) -> None:
    """The launcher checkout's account as root: never who owns what its path leads to."""
    host.bare(host.repository)
    place, before = host.decoy("checkout")
    (host.home / "src").mkdir()
    os.lchown(host.home / "src", fixture.OWNER_UID, fixture.OWNER_UID)
    os.symlink(place, host.home / "src" / "away")
    for repo in (host.repository, host.home / "src" / "away" / "inner"):
        with lookups(host.homes) as seen:
            runner, error = launcher_checkout._launcher_checkout_runner(repo, runner=Runner())
        check(runner is None and "refusing git target" in error and seen == [] and snapshot(place) == before,
              f"{repo}: no account chosen by path: {error!r} {seen}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd630.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        for index, case in enumerate((chosen_by_rule, links, unmatched, lexical, raced, owner_runner, role_worktrees,
                                      role_worktree_refusals, checkout_runner)):
            host = Host(base / str(index))
            fixture.accounts(host)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"owner_git_root_account_test: privileged child ran {CHECKS} checks")
        return 0
    for tool in ("setfacl", "getfacl", "unshare"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    done = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
         "--privileged-child"], capture_output=True, text=True)
    print((done.stdout + done.stderr).strip())
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
