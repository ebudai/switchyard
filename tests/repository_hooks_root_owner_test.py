#!/usr/bin/env python3
"""SYRD-614: root installs a repository's Git policy hooks as the repository's owner, never as root there.

On upgrade, after `new` and in the host-wide rollout, root read the tenant's
config by path, ran git with `safe.directory` set to the repository -- turning
git's ownership check off -- followed a linked git directory, resolved and used
a tenant-set `core.hooksPath` wherever it led, and wrote, chowned and renamed
hooks there by path.

Now root finds who owns the config or repository by descriptor from `/`,
refusing a link at it or above it, and only inside that account's own roots;
then a child that has become that account reads the config and installs
everything as itself, with git's ownership check left on. Root installs as
itself only in a tree root alone controls, and its per-hook installers refuse
any other root call. Each case runs as root in a user namespace (owner 1500,
foreign tree 1700), under a recorder of every pathname lookup root makes in the
tenant's home (which must see none), and proves the foreign tree and every
decoy unchanged byte for byte: contents, owner, mode, ACL and xattrs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import control_repository_writes_test as fixture  # noqa: E402
from control_repository_writes_test import lookups, snapshot  # noqa: E402
from root_tree_stand_in import roots_own  # noqa: E402
from scripts import repository_hooks  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from scripts.policy_hook_repair import repair_repository_policy_hooks  # noqa: E402

OWNER, OWNER_UID, FOREIGN, FOREIGN_UID = "1500", 1500, "1700", 1700
CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(*args: str, cwd: Path | None = None) -> None:
    """The fixture's own setup, as root: its git trusts the fixture, which the code under test never does."""
    argv = ["git", "-c", "safe.directory=*", *args[1:]] if args[0] == "git" else list(args)
    subprocess.run(argv, check=True, capture_output=True, text=True, cwd=cwd,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})


def give(top: Path, uid: int, *, mode: int | None = None) -> None:
    for item in [top, *top.rglob("*")]:
        os.lchown(item, uid, uid)
        if mode is not None and item.is_dir() and not item.is_symlink():
            os.chmod(item, mode)


def refusal(action) -> str:
    """Why `action` refused, or "" when it ran: any failure is a refusal here, and a check reads what it says."""
    try:
        action()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return ""


class Tenant:
    """A tenant's home with its project checkout, commit store and provision config, a foreign tree beside it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.homes = base / "home"
        self.home = self.homes / "tenant"
        self.repository = self.home / "Projects" / "p"
        self.store = self.home / ".local" / "state" / "switchyard" / "projects" / "p" / "control.git"
        self.provision = self.repository / ".switchyard" / "provision"
        self.config = self.provision / "p.json"
        self.repository.mkdir(parents=True)
        run("git", "init", "-q", "-b", "main", str(self.repository))
        self.store.parent.mkdir(parents=True)
        run("git", "init", "-q", "--bare", str(self.store))
        self.provision.mkdir(parents=True)
        self.config.write_text(json.dumps({"project": "p", "repository": str(self.repository),
                                           "control_repository": str(self.store)}))
        give(self.home, OWNER_UID)
        for item in (base, self.homes, self.home):
            os.chmod(item, 0o755)
        self.foreign = base / "foreign"
        self.foreign.mkdir()
        (self.foreign / "file").write_text("not the tenant's\n")
        (self.foreign / "config").write_text(json.dumps({"project": "q", "repository": str(self.foreign / "repo.git")}))
        run("git", "init", "-q", "--bare", str(self.foreign / "repo.git"))
        give(self.foreign, FOREIGN_UID, mode=0o700)
        subprocess.run(["setfacl", "-m", "u:1800:r--", str(self.foreign / "file")], check=True)
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched

    def decoy(self, label: str) -> tuple[Path, list[tuple]]:
        place = self.base / f"decoy-{label}"
        (place / "inner").mkdir(parents=True)
        (place / "inner" / "file").write_text("root's\n")
        return place, snapshot(place)

    def hooks(self) -> list[Path]:
        return sorted([*(self.repository / ".git" / "hooks").glob("*"), *(self.store / "hooks").glob("*")])


def accounts(tenant: Tenant) -> None:
    entries = {OWNER: pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(tenant.home), "/bin/sh")),
               FOREIGN: pwd.struct_passwd((FOREIGN, "x", FOREIGN_UID, FOREIGN_UID, "", str(tenant.base / "away"), "/bin/sh"))}
    real_name, real_uid = pwd.getpwnam, pwd.getpwuid
    pwd.getpwnam = lambda name: entries[name] if name in entries else real_name(name)
    pwd.getpwuid = lambda uid: next((e for e in entries.values() if e.pw_uid == uid), None) or real_uid(uid)
    launcher.current_user_name = lambda: "root"


def install(tenant: Tenant, path: Path | None = None):
    return repository_hooks.install_project_config(path or tenant.config, source_root=ROOT)


def ordinary(tenant: Tenant) -> None:
    """The config's owner reads it and installs both repositories' hooks as itself; a second run converges."""
    with lookups(tenant.homes) as seen:
        installed = install(tenant)
    names = sorted(path.name for path in installed)
    check(names == ["pre-commit", "pre-commit", "update", "update"] and seen == [],
          f"both repositories' hooks, nothing looked up by root: {installed} {seen}")
    made = [*tenant.hooks(), tenant.repository / ".git" / "config", tenant.store / "config"]
    check(all(item.lstat().st_uid == OWNER_UID for item in made),
          f"every hook and git config is the owner's: {[(str(i), i.lstat().st_uid) for i in made if i.lstat().st_uid != OWNER_UID]}")
    check(tenant.foreign_unchanged(), "the foreign tree is untouched")
    before = snapshot(tenant.home)
    with lookups(tenant.homes) as seen:
        again = install(tenant)
    check(sorted(again) == sorted(installed) and seen == [] and snapshot(tenant.home) == before,
          f"a second run converges, changing nothing: {seen}")


def links(tenant: Tenant) -> None:
    """A link at the config or any directory above it: refused before anything is read or installed."""
    for index, at in enumerate([tenant.config, tenant.provision, tenant.provision.parent, tenant.repository,
                                tenant.repository.parent, tenant.home]):
        place, before = tenant.decoy(f"link-{index}")
        aside = at.with_name(at.name + ".aside")
        os.rename(at, aside)
        os.symlink(place, at)
        hooks_before = snapshot(aside) if aside.is_dir() else None
        with lookups(tenant.homes) as seen:
            why = refusal(lambda: install(tenant))
        os.unlink(at)
        os.rename(aside, at)
        check("is a link" in why and seen == [] and snapshot(place) == before and tenant.foreign_unchanged()
              and (hooks_before is None or snapshot(at) == [(str(at) + i[0][len(str(aside)):], *i[1:]) for i in hooks_before]),
              f"a link at {at.relative_to(tenant.base)}: refused, nothing read or installed: {why!r} {seen}")


def outside(tenant: Tenant) -> None:
    """A config root may not act for: outside its owner's roots, another account's, root's, missing."""
    elsewhere = tenant.base / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "p.json").write_text(tenant.config.read_text())
    give(elsewhere, OWNER_UID)
    planted = tenant.provision / "q.json"
    planted.write_text(tenant.config.read_text())
    os.lchown(planted, FOREIGN_UID, FOREIGN_UID)
    rooted = tenant.provision / "r.json"
    rooted.write_text(tenant.config.read_text())
    for path, expected in ((elsewhere / "p.json", "outside 1500's own roots"),
                           (planted, "outside 1700's own roots"),
                           (rooted, "not in a tree root alone controls"),
                           (tenant.provision / "missing.json", "does not exist"),
                           (Path(f"{tenant.provision}/../provision/p.json"), "normalised absolute path")):
        before = snapshot(tenant.home)
        with lookups(tenant.homes) as seen:
            why = refusal(lambda: install(tenant, path))
        check(expected in why and seen == [] and snapshot(tenant.home) == before and tenant.foreign_unchanged(),
              f"{path}: refused, nothing installed: {why!r} {seen}")


def named(tenant: Tenant) -> None:
    """What the config names is reached as the owner: another account's repository, a `core.hooksPath`, a git
    directory or a hooks directory that leads to a tree the owner cannot write -- refused by the kernel, unchanged."""
    place, before = tenant.decoy("named")

    def foreign_repository():
        tenant.config.write_text(json.dumps({"project": "p", "repository": str(tenant.foreign / "repo.git")}))

    def hooks_path():
        run("git", "-C", str(tenant.repository), "config", "core.hooksPath", str(place / "inner"))

    def linked_git_dir():
        os.rename(tenant.repository / ".git", tenant.repository / ".git.aside")
        os.symlink(tenant.foreign / "repo.git", tenant.repository / ".git")

    def linked_hooks():
        hooks = tenant.repository / ".git" / "hooks"
        shutil.rmtree(hooks)
        os.symlink(place / "inner", hooks)

    for label, plant in (("another account's repository", foreign_repository), ("a hooksPath to root's tree", hooks_path),
                         ("a linked git directory", linked_git_dir), ("a linked hooks directory", linked_hooks)):
        saved_config = tenant.config.read_bytes()
        plant()
        give(tenant.repository, OWNER_UID)
        os.lchown(tenant.config, OWNER_UID, OWNER_UID)
        with lookups(tenant.homes) as seen:
            why = refusal(lambda: install(tenant))
        check("installing as 1500" in why and seen == [], f"{label}: refused as the owner: {why!r} {seen}")
        check(snapshot(place) == before and tenant.foreign_unchanged(), f"{label}: nothing written where it leads")
        tenant.config.write_bytes(saved_config)
        if (tenant.repository / ".git.aside").exists():
            os.unlink(tenant.repository / ".git")
            os.rename(tenant.repository / ".git.aside", tenant.repository / ".git")
        if (tenant.repository / ".git" / "hooks").is_symlink():
            os.unlink(tenant.repository / ".git" / "hooks")
        if plant is hooks_path:
            run("git", "-C", str(tenant.repository), "config", "--unset-all", "core.hooksPath")
        give(tenant.repository, OWNER_UID)


def repositories(tenant: Tenant) -> None:
    """`--repository` as root: its owner, inside its roots; root's own tree only when root alone controls it."""
    with lookups(tenant.homes) as seen:
        installed = repository_hooks.install_repository_policy(tenant.store, source_root=ROOT, project="p")
    check(len(installed) == 2 and all(p.lstat().st_uid == OWNER_UID for p in installed) and seen == [],
          f"the owner's repository, installed as the owner: {installed} {seen}")
    elsewhere = tenant.base / "elsewhere.git"
    run("git", "init", "-q", "--bare", str(elsewhere))
    give(elsewhere, OWNER_UID)
    mine = tenant.home / "root.git"
    run("git", "init", "-q", "--bare", str(mine))
    for path, expected in ((elsewhere, "outside 1500's own roots"), (mine, "not in a tree root alone controls"),
                           (tenant.foreign / "repo.git", "outside 1700's own roots")):
        before = snapshot(path) if path != tenant.foreign / "repo.git" else None
        with lookups(tenant.homes) as seen:
            why = refusal(lambda: repository_hooks.install_repository_policy(path, source_root=ROOT, project="p"))
        check(expected in why and seen == [] and (before is None or snapshot(path) == before) and tenant.foreign_unchanged(),
              f"{path}: refused: {why!r} {seen}")
    # Root's own tree, proven root's alone (stood in: under a namespace `/` is not root's): root installs as itself,
    # and its git keeps the ownership check on.
    owned = tenant.base / "rooted" / "own.git"
    owned.parent.mkdir()
    run("git", "init", "-q", "--bare", str(owned))
    argv: list[list[str]] = []
    real_run = subprocess.run

    def recording(args, *rest, **kwargs):
        argv.append(list(args))
        return real_run(args, *rest, **kwargs)

    repository_hooks.subprocess.run = recording
    try:
        with roots_own(owned.parent):
            installed = repository_hooks.install_repository_policy(owned, source_root=ROOT, project="p")
    finally:
        repository_hooks.subprocess.run = real_run
    gits = [call for call in argv if call[:1] == ["git"]]
    check(len(installed) == 2 and all(p.lstat().st_uid == 0 for p in installed) and gits
          and not any("safe.directory" in " ".join(call) for call in gits),
          f"root's own tree: installed as root, git's ownership check left on: {installed} {gits[:2]}")


def root_config(tenant: Tenant) -> None:
    """Root's own configuration, in a tree root alone controls (stood in), still installs each repository it
    names as that repository's owner: a root-owned directory in the home not at all, the tenant's as the tenant."""
    etc = tenant.base / "etc"
    etc.mkdir()
    config = etc / "p.json"
    mine = tenant.home / "root.git"
    run("git", "init", "-q", "--bare", str(mine))
    config.write_text(json.dumps({"project": "p", "repository": str(mine)}))
    before = snapshot(mine)
    with roots_own(etc), lookups(tenant.homes) as seen:
        why = refusal(lambda: install(tenant, config))
    check("not in a tree root alone controls" in why and snapshot(mine) == before and seen == [],
          f"a root-owned directory in the home, named by root's config: refused, untouched: {why!r} {seen}")
    config.write_text(json.dumps({"project": "p", "repository": str(tenant.repository)}))
    got: list[tuple[Path, ...]] = []
    with roots_own(etc), lookups(tenant.homes) as seen:
        why = refusal(lambda: got.append(install(tenant, config)))
    check(not why and len(got[0]) == 2 and all(p.lstat().st_uid == OWNER_UID for p in got[0]) and seen == [],
          f"the tenant's repository, named by root's config, installed as the tenant: {why!r} {got} {seen}")


def owner_git_keeps_the_check(tenant: Tenant) -> None:
    """In the owner's child git runs with no `safe.directory`; root's per-hook installers refuse a root call."""
    owner = pwd.getpwnam(OWNER)

    def seen_by_owner():
        captured: list[str] = []
        real_run = repository_hooks.subprocess.run

        def recording(args, *rest, **kwargs):
            captured.append(" ".join(args))
            return real_run(args, *rest, **kwargs)

        repository_hooks.subprocess.run = recording
        repository_hooks._git(tenant.repository, "rev-parse", "--git-common-dir")
        return [Path(f"/{os.geteuid()}"), *(Path("/" + call.replace("/", "|")) for call in captured)]

    said = [str(path) for path in repository_hooks._acting(owner, seen_by_owner)]
    check(said[0] == f"/{OWNER_UID}" and len(said) == 2 and "safe.directory" not in said[1],
          f"the child is the owner, and its git trusts nothing extra: {said}")
    for name, call in (("install_local_warning", lambda: repository_hooks.install_local_warning(tenant.repository, source_root=ROOT)),
                       ("install_main_guard", lambda: repository_hooks.install_main_guard(tenant.store, project="p"))):
        before = snapshot(tenant.home)
        why = refusal(call)
        check("as root" in why and snapshot(tenant.home) == before, f"{name} as root, directly: refused: {why!r}")


def callers(tenant: Tenant) -> None:
    """Upgrade's repair and `new`'s registry install reach the same boundary."""
    said: list[str] = []
    with lookups(tenant.homes) as seen:
        repaired = repair_repository_policy_hooks(tenant.config, source_repo=ROOT, print_func=said.append)
    check(len(repaired) == 4 and seen == [] and all(p.lstat().st_uid == OWNER_UID for p in repaired),
          f"upgrade's repair installs as the owner: {said} {seen}")
    place, before = tenant.decoy("callers")
    os.rename(tenant.provision, tenant.provision.with_name("provision.aside"))
    os.symlink(place, tenant.provision)
    said.clear()
    repaired = repair_repository_policy_hooks(tenant.config, source_repo=ROOT, print_func=said.append)
    os.unlink(tenant.provision)
    os.rename(tenant.provision.with_name("provision.aside"), tenant.provision)
    check(repaired == () and any("could not repair" in line and "is a link" in line for line in said)
          and snapshot(place) == before, f"a linked provision directory: a warning, nothing installed: {said}")
    registry = tenant.base / "registry"
    registry.mkdir()
    before_entries = repository_hooks.registry_snapshot(["new", "--registry-dir", str(registry)])
    (registry / "p.json").write_text(json.dumps({"config_path": str(tenant.config), "project": "p"}))
    with lookups(tenant.homes) as seen:
        installed = repository_hooks.install_new_registry_entries(["new", "--registry-dir", str(registry)],
                                                                 before_entries, source_root=ROOT)
    check(len(installed) == 4 and seen == [] and all(p.lstat().st_uid == OWNER_UID for p in installed),
          f"`new`'s registry install, as the owner: {installed} {seen}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd614.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        for index, case in enumerate((ordinary, links, outside, named, repositories, root_config, owner_git_keeps_the_check,
                                      callers)):
            tenant = Tenant(base / str(index))
            accounts(tenant)
            with contextlib.redirect_stdout(io.StringIO()):
                case(tenant)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"repository_hooks_root_owner_test: privileged child ran {CHECKS} checks")
        return 0
    for tool in ("setfacl", "getfacl", "unshare", "git"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    done = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
         "--privileged-child"], capture_output=True, text=True)
    print((done.stdout + done.stderr).strip())
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
