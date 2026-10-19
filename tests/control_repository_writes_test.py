#!/usr/bin/env python3
"""SYRD-613: root's repair of a control repository's ownership follows no link.

On `new`, `add-role` and launch, root found which of the commit store and the
directories above it were not the owner's by looking at each by path --
`exists`, `resolve`, `lstat`, a walk of the store -- and then ran `chown` on
those directories and `chown -R` on the store, by path. A link refused when
checked was still followed when swapped in after the check, and a link above
the store sent the check somewhere else entirely. The store's state and its
recorded origin were read the same way, and the origin was printed back.

Now root opens the owner's home once and reaches everything below it by
descriptor: a link at the store or at any directory above it is refused, one
inside the store is changed as a link, a path outside the owner's managed
root is refused, never normalised in, and the state and origin are read on
descriptors. Each case runs the whole repair -- and `ensure_control_repository`
around it -- as root under a recorder of every pathname lookup or change in
the tenant's home (which must see none), and proves a foreign tree unchanged
byte for byte: contents, owner, mode, ACL and xattrs. The privileged half runs
in a user namespace with the invoking account's subuid range mapped; the owner
is a stand-in for the account lookups.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402,I001
from scripts import tenant_paths  # noqa: E402

OWNER, OWNER_UID, FOREIGN_UID = "1500", 1500, 1700
CHECKS = 0
PAUSED: list[bool] = []


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


class Runner:
    """Every argv root would run, recorded; each succeeds and changes nothing, `on` first given its chance."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.on is not None:
            self.on(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def chowned(self) -> bool:
        return any("chown" in " ".join(call) for call in self.calls)


def snapshot(top: Path) -> list[tuple]:
    seen = []
    for item in [top, *sorted(top.rglob("*"))]:
        status = item.lstat()
        if stat.S_ISLNK(status.st_mode):
            seen.append((str(item), status.st_uid, status.st_gid, "link", os.readlink(item)))
            continue
        xattrs = sorted((name, os.getxattr(item, name)) for name in os.listxattr(item) if name.startswith("user."))
        body = item.read_bytes() if stat.S_ISREG(status.st_mode) else b""
        seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode),
                     run(["getfacl", "-cpn", str(item)]).stdout, xattrs, body))
    return seen


@contextlib.contextmanager
def lookups(top: Path):
    """Every pathname lookup or change of something under `top` -- by `pathlib` or `os` -- while this holds.

    Descriptor-relative calls (a name with `dir_fd`) are what root is meant to use, and are not recorded; nor is what
    the test itself does to the tree while `PAUSED`.
    """
    seen: list[str] = []
    prefix = str(top).rstrip("/") + "/"

    def note(*targets, **kwargs):
        if PAUSED or any(kwargs.get(key) is not None for key in ("dir_fd", "src_dir_fd", "dst_dir_fd")):
            return
        for target in targets:
            text = os.fsdecode(target) if isinstance(target, (str, bytes, os.PathLike)) else ""
            if text.startswith(prefix):
                seen.append(text)

    patches = []
    for holder, name in ((Path, "exists"), (Path, "is_dir"), (Path, "is_file"), (Path, "is_symlink"), (Path, "stat"),
                         (Path, "lstat"), (Path, "resolve"), (Path, "read_text"), (Path, "read_bytes"),
                         (Path, "iterdir"), (os, "stat"), (os, "lstat"), (os, "access"), (os, "readlink"),
                         (os.path, "exists"), (os.path, "lexists"), (os.path, "isdir"), (os.path, "isfile"),
                         (os.path, "islink"), (os.path, "realpath"), (builtins, "open"), (io, "open"), (os, "open"),
                         (os, "unlink"), (os, "remove"), (os, "rename"), (os, "replace"), (os, "mkdir"),
                         (os, "makedirs"), (os, "rmdir"), (os, "chmod"), (os, "chown"), (os, "lchown"),
                         (os, "symlink"), (os, "link"), (os, "utime"), (os, "listdir"), (os, "scandir")):
        real = getattr(holder, name)

        def spy(*args, __real=real, __name=name, **kwargs):
            note(*(args[:2] if __name in ("rename", "replace", "symlink", "link") else args[:1] or
                   [kwargs.get("path", "")]), **kwargs)
            return __real(*args, **kwargs)

        patches.append((holder, name, real))
        setattr(holder, name, spy)
    try:
        yield seen
    finally:
        for holder, name, real in patches:
            setattr(holder, name, real)


@contextlib.contextmanager
def standing_in(holder, **names):
    saved = {name: getattr(holder, name) for name in names}
    for name, value in names.items():
        setattr(holder, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(holder, name, value)


class Host:
    """A tenant's home whose commit store root left behind as root's, and a foreign tree beside it."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.homes = base / "home"
        self.home = self.homes / "tenant"
        self.home.mkdir(parents=True)
        os.lchown(self.home, OWNER_UID, OWNER_UID)
        for item in (base, self.homes):
            os.chmod(item, 0o755)
        self.foreign = base / "foreign"
        self.foreign.mkdir()
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "config").write_text('[remote "origin"]\n\turl = https://token@example.invalid/secret\n')
        for item in [self.foreign, *self.foreign.rglob("*")]:
            os.lchown(item, FOREIGN_UID, FOREIGN_UID)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1800:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)
        self.repository = self.home / "Projects" / "p"
        self.repository.mkdir(parents=True)
        for item in (self.home / "Projects", self.repository):
            os.lchown(item, OWNER_UID, OWNER_UID)
        local = self.home / ".local"
        local.mkdir()
        os.lchown(local, OWNER_UID, OWNER_UID)
        #: Root-owned, as root's earlier runs left them: the directories above the store, and the store.
        self.chain = [local / "state", local / "state" / "switchyard", local / "state" / "switchyard" / "projects",
                      local / "state" / "switchyard" / "projects" / "p"]
        self.store = self.chain[-1] / "control.git"
        for item in self.chain:
            item.mkdir()
        self.config = launcher.ProjectConfig(
            project="p", project_name="P", ticket_prefix="P", layout=self.home / "layout.json",
            session_dir=self.chain[-1], board_url="http://127.0.0.1:1", board_socket="/run/p.sock",
            upstream_report_url="", upstream_report_token_file="", run_as_user=OWNER, pane_launcher=None,
            repository=self.repository, control_repository=self.store, worktree_base=self.home / "trees",
            worktree_remote="origin", worktree_branch="main", roles=[self.role()], role_state_isolation=True)

    @staticmethod
    def role() -> launcher.RoleConfig:
        return launcher.RoleConfig(
            role="ops", slot=0, detached=False, tmux_session="p-ops", target="p-ops:0.0", workdir="", cli=["codex"],
            model="", model_arg="--model", effort="", yolo=False, extra_args=[], resume_mode="", resume_flag="",
            resume_subcommand="", fresh_session_per_ticket=False, live_commands=["codex"], env={}, run_as_user="")

    def bare(self, url: Path | str) -> None:
        """The store as a bare repository root cloned, every entry root's, with a link inside to the foreign tree."""
        for name in ("objects/pack", "refs/heads"):
            (self.store / name).mkdir(parents=True)
        (self.store / "HEAD").write_text("ref: refs/heads/main\n")
        (self.store / "objects" / "pack" / "pack-1.pack").write_bytes(b"PACK")
        (self.store / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
        (self.store / "away").symlink_to(self.foreign / "file")

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched

    def owner_of(self, path: Path) -> tuple[int, int]:
        status = path.lstat()
        return status.st_uid, status.st_gid

    def swap(self, at: Path, target: Path) -> Path:
        """Replace `at` with a link the owner made -- by `os` alone, so the recorder around it sees nothing."""
        aside = at.with_name(at.name + ".aside")
        PAUSED.append(True)
        try:
            os.rename(at, aside)
            os.symlink(target, at)
            os.lchown(at, OWNER_UID, OWNER_UID)
        finally:
            PAUSED.pop()
        return aside

    def unswap(self, at: Path, aside: Path) -> None:
        PAUSED.append(True)
        try:
            os.unlink(at)
            os.rename(aside, at)
        finally:
            PAUSED.pop()

    def decoy(self, label: str) -> tuple[Path, list[tuple]]:
        """A root-owned directory outside the home, as the target of a link, and how it looks now."""
        place = self.base / f"decoy-{label}"
        (place / "inner").mkdir(parents=True)
        (place / "inner" / "file").write_text("root's\n")
        return place, snapshot(place)


def accounts(host: Host) -> None:
    real_name = pwd.getpwnam
    stand_in = pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(host.home), "/bin/sh"))
    pwd.getpwnam = lambda name: stand_in if name == OWNER else real_name(name)
    launcher.current_user_name = lambda: "root"


def repair(host: Host, runner: Runner | None = None):
    return launcher.repair_control_repository_ownership(host.config, runner=runner or Runner())


def ordinary(host: Host) -> None:
    """The store and the directories above it come back to the owner by descriptor; a second run changes nothing."""
    host.bare(host.repository)
    runner = Runner()
    with lookups(host.homes) as seen:
        result = repair(host, runner)
    every = [*host.chain, *[host.store, *sorted(host.store.rglob("*"))]]
    check(result.ok and seen == [] and not runner.chowned(),
          f"the whole repair, nothing looked up by path, no chown run: {result.failed_roles} {seen} {runner.calls}")
    check(all(host.owner_of(item) == (OWNER_UID, OWNER_UID) for item in every),
          f"every directory above the store and everything in it is the owner's: "
          f"{[(str(i), host.owner_of(i)) for i in every if host.owner_of(i) != (OWNER_UID, OWNER_UID)]}")
    check((host.store / "away").is_symlink() and host.foreign_unchanged(),
          "the link inside the store is changed as a link; what it names keeps its owner")
    before = snapshot(host.home)
    with lookups(host.homes) as seen:
        again = repair(host)
    check(again.ok and seen == [] and snapshot(host.home) == before, f"a second run converges, changing nothing: {seen}")


def before_the_clone(host: Host) -> None:
    """No store yet: the directories above where it goes are repaired, and nothing is made."""
    with lookups(host.homes) as seen:
        result = repair(host)
    check(result.ok and seen == [] and not host.store.exists()
          and all(host.owner_of(item) == (OWNER_UID, OWNER_UID) for item in host.chain),
          f"the directories above a missing store are the owner's, the store not made: {result.failed_roles} {seen}")


def planted(host: Host) -> None:
    """A link at the store or at any directory above it, there before the repair: refused, nothing through it."""
    host.bare(host.repository)
    for index, at in enumerate([host.store, *reversed(host.chain), host.home / ".local"]):
        place, before = host.decoy(f"planted-{index}")
        aside = host.swap(at, place)
        with lookups(host.homes) as seen:
            result = repair(host)
        host.unswap(at, aside)
        reason = result.failed_roles.get("ops", "")
        check(not result.ok and "refusing to repair control repository ownership" in reason
              and "symbolic link" in reason and seen == [] and snapshot(place) == before and host.foreign_unchanged(),
              f"a link at {at.relative_to(host.home)}: refused, nothing through it re-owned: {reason!r} {seen}")


def swapped(host: Host) -> None:
    """A link swapped in after root looked -- between seeing and opening a directory, and between the walk to the
    store and the walk through it: refused each time, and what the link names keeps its owner."""
    host.bare(host.repository)
    real_open = tenant_paths.Root.open_directory
    for index, at in enumerate([host.store, host.chain[-1], host.chain[1]]):
        place, before = host.decoy(f"open-{index}")
        moved: list[Path] = []

        def open_directory(self, parent, name, display, expect=None, at=at, place=place):
            if display == str(at) and not moved:
                moved.append(host.swap(at, place))
            return real_open(self, parent, name, display, expect)

        with standing_in(tenant_paths.Root, open_directory=open_directory), lookups(host.homes) as seen:
            result = repair(host)
        host.unswap(at, moved[0])
        reason = result.failed_roles.get("ops", "")
        check(not result.ok and ("replaced" in reason or "symbolic link" in reason) and seen == []
              and snapshot(place) == before and host.foreign_unchanged(),
              f"{at.relative_to(host.home)} swapped for a link between look and open: refused: {reason!r} {seen}")
    real_tree = tenant_paths.chown_tree
    place, before = host.decoy("tree")
    moved = []

    def chown_tree(root, path, uid, gid, if_exists):
        moved.append(host.swap(host.chain[-1], place))
        return real_tree(root, path, uid, gid, if_exists)

    with standing_in(tenant_paths, chown_tree=chown_tree), lookups(host.homes) as seen:
        result = repair(host)
    host.unswap(host.chain[-1], moved[0])
    check(not result.ok and "symbolic link" in result.failed_roles["ops"] and seen == []
          and snapshot(place) == before and host.foreign_unchanged(),
          f"the store's parent swapped for a link between the walk and the tree: refused: {result.failed_roles} {seen}")


def traversal(host: Host) -> None:
    """A directory inside the store swapped for a link while the tree is walked: changed as a link, never entered."""
    host.bare(host.repository)
    place, before = host.decoy("traversal")
    target = host.store / "objects" / "pack"
    real_listdir = os.listdir
    objects = (host.store / "objects").lstat().st_ino
    moved: list[Path] = []

    def listdir(where=".", *, __real=real_listdir):
        if isinstance(where, int) and not moved and os.fstat(where).st_ino == objects:
            moved.append(host.swap(target, place))
        return __real(where)

    os.listdir = listdir
    try:
        with lookups(host.homes) as seen:
            result = repair(host)
    finally:
        os.listdir = real_listdir
    check(moved and result.ok and seen == [] and target.is_symlink() and host.owner_of(target) == (OWNER_UID, OWNER_UID)
          and snapshot(place) == before and host.foreign_unchanged(),
          f"a link swapped in during the walk is re-owned as a link, its target untouched: {result.failed_roles} {seen}")
    host.unswap(target, moved[0])
    # And swapped between being listed and being opened: the open is bound to what was listed, so it is refused.
    real_open = tenant_paths.Root.open_directory
    place, before = host.decoy("traversal-open")
    moved.clear()

    def open_directory(self, parent, name, display, expect=None):
        if display == str(target) and not moved:
            moved.append(host.swap(target, place))
        return real_open(self, parent, name, display, expect)

    with standing_in(tenant_paths.Root, open_directory=open_directory), lookups(host.homes) as seen:
        result = repair(host)
    host.unswap(target, moved[0])
    check(not result.ok and "symbolic link" in result.failed_roles["ops"] and seen == []
          and snapshot(place) == before and host.foreign_unchanged(),
          f"a directory in the store swapped between listing and opening: refused: {result.failed_roles} {seen}")


def outside(host: Host) -> None:
    """A store outside the owner's managed root is refused as written, never normalised in; so is a missing owner."""
    from dataclasses import replace

    for store in (host.chain[-1] / ".." / ".." / ".." / ".." / ".." / ".." / "foreign" / "control.git",
                  host.foreign / "control.git", host.chain[2] / "control.git", host.home / "control.git"):
        with lookups(host.homes) as seen:
            result = launcher.repair_control_repository_ownership(replace(host.config, control_repository=store),
                                                                  runner=Runner())
        check(not result.ok and "is not a switchyard-managed control directory" in result.failed_roles["ops"]
              and seen == [] and host.foreign_unchanged()
              and all(host.owner_of(item) == (0, 0) for item in host.chain),
              f"{store}: refused, nothing looked at or changed: {result.failed_roles} {seen}")
    with lookups(host.homes) as seen:
        result = launcher.repair_control_repository_ownership(replace(host.config, run_as_user="1999"), runner=Runner())
    check(not result.ok and "target user '1999' does not exist" in result.failed_roles["ops"] and seen == [],
          f"an owner that is no account: refused before anything is looked at: {result.failed_roles} {seen}")


class Git:
    """The git root had run, from the runner's record: through the real chokepoint, which names the owner (SYRD-630)."""

    def __init__(self, runner: Runner) -> None:
        self.calls = [call for call in runner.calls if "git" in call]

    def ran(self, word: str) -> bool:
        return any(word in call for call in self.calls)


def ensure(host: Host, runner: Runner | None = None) -> tuple[object, Git, list[str]]:
    runner = runner or Runner()
    with lookups(host.homes) as seen:
        result = launcher.ensure_control_repository(host.config, runner=runner)
    git = Git(runner)
    check(all(call[:3] == ["sudo", "-u", OWNER] for call in git.calls), f"every git the owner's: {git.calls}")
    return result, git, seen


def whole(host: Host) -> None:
    """`ensure_control_repository` whole as root: repair, state and origin all by descriptor."""
    # The placeholder provisioning left: repaired, then cloned into.
    host.store.mkdir()
    result, git, seen = ensure(host)
    check(result.ok and git.ran("clone") and seen == [] and host.owner_of(host.store) == (OWNER_UID, OWNER_UID),
          f"an empty store: repaired, read by descriptor, cloned into: {result.failed_roles} {git.calls} {seen}")
    # A ready store of this repository: fetched into, not cloned.
    host.store.rmdir()
    host.bare(host.repository)
    result, git, seen = ensure(host)
    check(result.ok and not git.ran("clone") and git.ran("fetch") and seen == [],
          f"a ready store: fetched into, not cloned: {result.failed_roles} {git.calls} {seen}")
    # Somebody else's data at the store's path: refused, as before.
    (host.store / "HEAD").unlink()
    result, git, seen = ensure(host)
    check(not result.ok and "is not a Git repository" in result.failed_roles["ops"] and not git.calls and seen == [],
          f"an occupied store is refused: {result.failed_roles} {seen}")
    (host.store / "HEAD").write_text("ref: refs/heads/main\n")
    # Another checkout's store: refused, naming what it records -- read by descriptor.
    (host.store / "config").write_text('[remote "origin"]\n\turl = /elsewhere/repo\n')
    result, git, seen = ensure(host)
    check(not result.ok and "/elsewhere/repo" in result.failed_roles["ops"] and not git.ran("fetch") and seen == [],
          f"another checkout's store: refused by its recorded origin: {result.failed_roles} {seen}")
    # The store's config a link to a file only root can read: refused, and nothing in it is reported.
    (host.store / "config").unlink()
    (host.store / "config").symlink_to(host.foreign / "config")
    result, git, seen = ensure(host)
    reason = result.failed_roles.get("ops", "")
    check(not result.ok and "token" not in reason and "symbolic link" in reason and not git.ran("fetch")
          and seen == [] and host.foreign_unchanged(),
          f"a linked store config: refused, nothing root alone can read reported: {reason!r} {seen}")
    (host.store / "config").unlink()
    (host.store / "config").write_text(f'[remote "origin"]\n\turl = {host.repository}\n')
    # The store's parent swapped for a link after the repair, while the owner makes it: nothing read through it.
    place, before = host.decoy("whole")
    (place / "control.git").mkdir()
    (place / "control.git" / "config").write_text('[remote "origin"]\n\turl = https://token@example.invalid/r\n')
    before = snapshot(place)
    moved: list[Path] = []
    runner = Runner()
    runner.on = lambda argv: moved.append(host.swap(host.chain[-1], place)) if argv[:2] == ["mkdir", "-p"] else None
    result, git, seen = ensure(host, runner)
    host.unswap(host.chain[-1], moved[0])
    reason = result.failed_roles.get("ops", "")
    check(not result.ok and "symbolic link" in reason and "token" not in reason and not git.calls and seen == []
          and snapshot(place) == before and host.foreign_unchanged(),
          f"a link swapped in above the store after the repair: refused, nothing read through it: {reason!r} {seen}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd613.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        for index, case in enumerate((ordinary, before_the_clone, planted, swapped, traversal, outside, whole)):
            host = Host(base / str(index))
            accounts(host)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"control_repository_writes_test: privileged child ran {CHECKS} checks")
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
