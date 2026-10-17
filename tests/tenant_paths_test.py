#!/usr/bin/env python3
"""SYRD-605: root's changes to a tenant's paths never follow a link, and say so when they refuse.

`scripts/tenant_paths.py` is what the operator packet, `repair-boundary` and
every other generated root operation now run for a directory or file inside a
tenant's home or a role's home. The Director's acceptance, case by case:

* a planted link, a component swapped concurrently, and a link introduced
  during a recursive walk each leave the foreign tree's owner, mode and ACLs
  exactly as they were, exit nonzero, and are recorded as a refusal -- in
  `repair-boundary`'s journal and, for the packet, in the record
  `switchyard-record-rollout` keeps;
* ordinary real directories converge, and a second run changes nothing;
* preview and apply name the same paths and meet the same refusal.

The unprivileged half compares the helper's ACL and mode work with the real
`setfacl`, `install -d` and `find -exec chmod` on twin trees. The privileged
half runs in a user namespace with the invoking account's subuid range mapped,
so ownership, ACLs and mounts are the kernel's, and a "concurrent" swap is made
at the exact moment the helper opens the component, through its one open seam.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, Path(__file__).resolve().parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from scripts import tenant_paths  # noqa: E402

HELPER = str(ROOT / "scripts" / "tenant_paths.py")
OWNER_UID, SERVICE_UID, FOREIGN_UID = 1500, 1600, 1700
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True, **kwargs)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def helper(root: Path, *words: str, preview: bool = False) -> subprocess.CompletedProcess[str]:
    """The helper as the packet runs it: a separate `python3 -I` process."""
    argv = [sys.executable, "-I", HELPER, "--root", str(root), *(["--preview"] if preview else []), *words]
    return subprocess.run(argv, capture_output=True, text=True)


def snapshot(*tops: Path) -> list[tuple]:
    """Owner, group, mode, link target and ACL of everything under each top, links not followed."""
    seen = []
    for top in tops:
        for item in [top, *sorted(top.rglob("*"))]:
            status = item.lstat()
            if stat.S_ISLNK(status.st_mode):
                seen.append((str(item), status.st_uid, status.st_gid, "link", os.readlink(item)))
                continue
            acl = run(["getfacl", "-cpn", str(item)]).stdout
            seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode), acl))
    return seen


def relative(entries: list[tuple], top: Path) -> list[tuple]:
    return [(str(Path(e[0]).relative_to(top)), *e[1:]) for e in entries]


# --------------------------------------------------------------------------
# the unprivileged half: the same answers as the real tools
# --------------------------------------------------------------------------


def twin(base: Path) -> Path:
    tree = base
    (tree / "x" / "y").mkdir(parents=True)
    (tree / "x" / "f").write_text("f\n", encoding="utf-8")
    (tree / "x" / "y" / "g").write_text("g\n", encoding="utf-8")
    os.chmod(tree / "x" / "f", 0o640)
    os.chmod(tree / "x" / "y" / "g", 0o750)
    os.chmod(tree / "x" / "y", 0o755)
    (tree / "x" / "link").symlink_to("/etc")
    return tree


def test_the_helper_does_what_setfacl_install_and_find_do() -> None:
    me = run(["id", "-un"]).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="syrd605-twin.") as raw:
        a, b = Path(raw) / "a", Path(raw) / "b"
        twin(a), twin(b)
        tools = [
            ["setfacl", "-R", "-m", "u:nobody:rX", "x"],
            ["setfacl", "-R", "-m", "d:u:nobody:rX", "x"],
            ["setfacl", "-m", "g:daemon:--x", "."],
            ["setfacl", "-m", "d:u::rwx,d:g::r-x,d:o::---", "x"],
            ["setfacl", "-x", "u:nobody", "x/y"],
            ["setfacl", "-R", "-x", "d:u:nobody", "x"],
            ["install", "-d", "-m", "0750", "-o", me, "-g", me, "z"],
            ["install", "-d", "-m", "0750", "-o", me, "-g", me, "z/w"],
            ["find", "x", "-mindepth", "1", "-maxdepth", "1", "-type", "d", "-exec", "chmod", "o-rwx", "{}", "+"],
            ["find", "x", "-type", "d", "-exec", "setfacl", "-m", "d:u:daemon:rx", "{}", "+"],
            ["chmod", "0600", "x/f"],
        ]
        for argv in tools:
            run(argv, cwd=b)
        steps = [
            ("acl", "--modify", "u:nobody:rX", "--recursive", f"{a}/x"),
            ("acl", "--modify", "d:u:nobody:rX", "--recursive", f"{a}/x"),
            ("acl", "--modify", "g:daemon:--x", str(a)),
            ("acl", "--modify", "d:u::rwx,d:g::r-x,d:o::---", f"{a}/x"),
            ("acl", "--remove", "u:nobody", f"{a}/x/y"),
            ("acl", "--remove", "d:u:nobody", "--recursive", f"{a}/x"),
            ("dir", "--mode", "0750", "--owner", me, "--group", me, f"{a}/z", f"{a}/z/w"),
            ("close-children", f"{a}/x"),
            ("acl", "--modify", "d:u:daemon:rx", "--recursive", "--directories-only", f"{a}/x"),
            ("attrs", "--mode", "0600", f"{a}/x/f"),
        ]
        for words in steps:
            done = helper(a, *words)
            check(done.returncode == 0, f"{words}: {done.stderr}")
        check(relative(snapshot(a), a) == relative(snapshot(b), b),
              "owner, mode and ACL of every entry match the real tools', links left as links")
        # Converged: on a fresh tree, the grants, directories and closures run
        # twice, and the second run changes nothing and says nothing. (Not the
        # removals or the chmod: replaying an add after its removal re-adds it,
        # with the real tools as with this.)
        c = twin(Path(raw) / "c")
        monotone = [tuple(str(w).replace(str(a), str(c)) for w in steps[index]) for index in (0, 1, 2, 3, 6, 7, 8)]
        first = [helper(c, *words) for words in monotone]
        check(all(d.returncode == 0 for d in first), f"the first run: {[d.stderr for d in first]}")
        settled = snapshot(c)
        again = [helper(c, *words) for words in monotone]
        check(snapshot(c) == settled, "the tree is exactly as the first run left it")
        check(all(d.returncode == 0 for d in again)
              and not [line for d in again for line in d.stdout.splitlines() if "leave " not in line],
              f"a second run changes nothing: {[d.stdout for d in again]}")


def test_arguments_that_are_not_a_tenant_path_are_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd605-args.") as raw:
        home = Path(raw) / "home"
        home.mkdir()
        for words, why in (
            (("acl", "--modify", "u:nobody:r", "/etc/passwd"), "is not inside"),
            (("acl", "--modify", "u:nobody:r", f"{home}/../x"), "not a normalised absolute path"),
            (("acl", "--modify", "u:nobody:r", "relative"), "not a normalised absolute path"),
            (("acl", "--modify", "u:no-such-account-605:r", str(home)), "is not an account"),
            (("acl", "--remove", "u::", str(home)), "only a named entry can be removed"),
            (("dir", "--mode", "0999", "--owner", "x", "--group", "x", str(home)), "is not an octal mode"),
        ):
            done = helper(home, *words)
            check(done.returncode not in (0, tenant_paths.REFUSED_EXIT) and why in done.stderr, f"{words}: {done.stderr}")
        done = helper(home, "acl", "--remove", "g:no-such-group-605", "--if-group", "no-such-group-605", str(home))
        check(done.returncode == 0 and not done.stdout, f"a removal for a group the host lacks does nothing: {done.stdout}{done.stderr}")


# --------------------------------------------------------------------------
# the privileged half: real uids, real ACLs, real mounts
# --------------------------------------------------------------------------


class Host:
    """A tenant home, a foreign tree it must never reach, and a way to run the helper in-process."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home" / "tenant"
        self.foreign = root / "foreign"
        (self.home / "store.git" / "objects" / "ab").mkdir(parents=True)
        (self.home / "store.git" / "objects" / "ab" / "cdef").write_text("object\n", encoding="utf-8")
        (self.home / "worktrees" / "main").mkdir(parents=True)
        (self.home / "worktrees" / "main" / "src.py").write_text("src\n", encoding="utf-8")
        for item in [self.home, *self.home.rglob("*")]:
            os.lchown(item, OWNER_UID, OWNER_UID)
        os.chmod(root, 0o755)
        os.chmod(root / "home", 0o755)
        os.chmod(self.home, 0o750)
        (self.foreign / "inner").mkdir(parents=True)
        (self.foreign / "inner" / "secret").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "file").write_text("not the tenant's either\n", encoding="utf-8")
        for item in [self.foreign, *self.foreign.rglob("*")]:
            os.lchown(item, FOREIGN_UID, FOREIGN_UID)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", f"u:{SERVICE_UID}:r--", str(self.foreign / "file")])
        self.untouched = snapshot(self.foreign)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched

    def plant(self, at: Path, target: Path) -> None:
        """Replace `at` with a link to `target`, owned by the tenant as the tenant would make it."""
        if at.is_symlink() or at.is_file():
            at.unlink()
        elif at.exists():
            shutil.rmtree(at)
        at.symlink_to(target)
        os.lchown(at, OWNER_UID, OWNER_UID)

    def inprocess(self, *words: str, preview: bool = False, swap=None) -> tuple[int, list[str], list[str]]:
        """Run the helper in-process; `swap(name)` runs just before it opens a component called `name`."""
        said, refused = [], []
        real = tenant_paths._openat

        def watched(name, flags, dir_fd):
            if swap is not None:
                swap(name)
            return real(name, flags, dir_fd)

        tenant_paths._openat = watched
        try:
            status = tenant_paths.run(["--root", str(self.home), *words], report=said.append,
                                      refuse=refused.append, preview=preview)
        finally:
            tenant_paths._openat = real
        return status, said, refused


def planted_links(root: Path) -> None:
    """A link the tenant left, where each operation would act, and on the way there."""
    host = Host(root / "planted")
    home, foreign = host.home, host.foreign
    owner = ("--owner", str(OWNER_UID), "--group", str(OWNER_UID))
    cases = {
        "dir on a link": (lambda: host.plant(home / "worktrees", foreign), ("dir", "--mode", "0750", *owner, f"{home}/worktrees")),
        "dir below a link": (lambda: host.plant(home / "worktrees", foreign), ("dir", "--mode", "0750", *owner, f"{home}/worktrees/new")),
        "attrs on a link": (lambda: host.plant(home / "key", foreign / "file"), ("attrs", "--mode", "0644", *owner, f"{home}/key")),
        "acl on a link": (lambda: host.plant(home / "store.git", foreign), ("acl", "--modify", f"u:{SERVICE_UID}:rX", f"{home}/store.git")),
        "recursive acl on a link": (lambda: host.plant(home / "store.git", foreign),
                                    ("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git")),
        "removal on a link": (lambda: host.plant(home / "store.git", foreign),
                              ("acl", "--remove", f"u:{SERVICE_UID}", "--recursive", "--if-exists", f"{home}/store.git")),
        "children of a link": (lambda: host.plant(home / "worktrees", foreign), ("close-children", f"{home}/worktrees")),
        "chown-tree on a link": (lambda: host.plant(home / "worktrees", foreign),
                                 ("chown-tree", "--owner", str(OWNER_UID), "--if-exists", f"{home}/worktrees")),
        "install below a link": (lambda: host.plant(home / "worktrees", foreign),
                                 ("install", "--mode", "0644", *owner, HELPER, f"{home}/worktrees/unit")),
    }
    for label, (plant, words) in cases.items():
        plant()
        preview = helper(home, *words, preview=True)
        apply = helper(home, *words)
        check(apply.returncode == tenant_paths.REFUSED_EXIT and "symbolic link" in apply.stderr and host.foreign_unchanged(),
              f"{label}: refused, nonzero, the foreign tree untouched: {apply.returncode} {apply.stderr}")
        check(preview.returncode == apply.returncode and preview.stderr == apply.stderr,
              f"{label}: the preview meets the same refusal: {preview.stderr!r} {apply.stderr!r}")
    # A link at the name an install writes is replaced by the file, never written through.
    (home / "units").mkdir()
    os.lchown(home / "units", OWNER_UID, OWNER_UID)
    host.plant(home / "units" / "unit", foreign / "file")
    done = helper(home, "install", "--mode", "0644", *owner, HELPER, f"{home}/units/unit")
    check(done.returncode == 0 and (home / "units" / "unit").is_file() and not (home / "units" / "unit").is_symlink()
          and host.foreign_unchanged(), f"an install replaces a link at its name and writes nothing through it: {done.stderr}")
    # A home that is itself a link, and a link above the home the tenant made: refused.
    target = root / "planted" / "elsewhere"
    target.mkdir()
    host.plant(target / "h", foreign)
    os.lchown(target, OWNER_UID, OWNER_UID)
    done = helper(target / "h", "acl", "--modify", f"u:{SERVICE_UID}:rX", str(target / "h"))
    check(done.returncode == tenant_paths.REFUSED_EXIT and host.foreign_unchanged(), f"a home that is a link: {done.stderr}")
    os.lchown(target / "h", 0, 0)
    done = helper(target / "h", "acl", "--modify", f"u:{SERVICE_UID}:rX", str(target / "h"))
    check(done.returncode == tenant_paths.REFUSED_EXIT and host.foreign_unchanged(),
          f"a home that is a link is refused even when root made the link: {done.stderr}")
    via = root / "planted" / "via"
    via.symlink_to(home.parent)
    os.lchown(via, OWNER_UID, OWNER_UID)
    done = helper(via / "tenant", "acl", "--modify", f"u:{SERVICE_UID}:--x", str(via / "tenant"))
    check(done.returncode == tenant_paths.REFUSED_EXIT and f"{via} is a symbolic link" in done.stderr,
          f"a link above the home made by the tenant is refused: {done.stderr}")
    os.lchown(via, 0, 0)
    done = helper(via / "tenant", "acl", "--modify", f"u:{SERVICE_UID}:--x", str(via / "tenant"))
    check(done.returncode == 0, f"a link above the home root made is followed, as /home -> /var/home is: {done.stderr}")


def concurrent_swaps(root: Path) -> None:
    """The path changed between root's look and root's act: the open itself is where the tenant wins or loses."""
    host = Host(root / "swaps")
    home, foreign = host.home, host.foreign
    owner = ("--owner", str(OWNER_UID), "--group", str(OWNER_UID))

    def swapping(name: str, at: Path, target: Path):
        done = []

        def swap(opened: str) -> None:
            if opened == name and not done:
                done.append(True)
                host.plant(at, target)
        return swap

    # A component of the path, swapped for a link just before it is opened.
    status, said, refused = host.inprocess("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git/objects",
                                           swap=swapping("store.git", home / "store.git", foreign))
    check(status == tenant_paths.REFUSED_EXIT and refused and "symbolic link" in refused[0] and host.foreign_unchanged(),
          f"a component swapped before it is opened: {refused}")
    host.plant(home / "store.git", root / "swaps" / "gone")  # leave no link behind for the next case
    (home / "store.git").unlink()
    (home / "store.git" / "objects" / "ab").mkdir(parents=True)
    # A file swapped between root's lstat and root's open of it.
    (home / "key").write_text("key\n", encoding="utf-8")
    os.lchown(home / "key", OWNER_UID, OWNER_UID)
    status, said, refused = host.inprocess("attrs", "--mode", "0600", *owner, f"{home}/key",
                                           swap=swapping("key", home / "key", foreign / "file"))
    check(status == tenant_paths.REFUSED_EXIT and refused and host.foreign_unchanged(),
          f"a file swapped between the look and the open: {refused}")
    # Swapped for a link to a foreign FIFO with a writer waiting on it. Refusing
    # after opening would be too late: opening the FIFO is itself what releases
    # the writer. Root never opens it.
    pipe = foreign / "pipe"
    os.mkfifo(pipe)
    os.lchown(pipe, FOREIGN_UID, FOREIGN_UID)
    writer = subprocess.Popen([sys.executable, "-c", f"open({str(pipe)!r}, 'w').close()"])
    deadline = time.time() + 10
    while time.time() < deadline and "wait_for_partner" not in Path(f"/proc/{writer.pid}/wchan").read_text(errors="replace"):
        time.sleep(0.05)  # until the writer is waiting in open() for a reader
    try:
        (home / "key").unlink() if (home / "key").is_symlink() else None
        (home / "key").write_text("key\n", encoding="utf-8")
        status, said, refused = host.inprocess("attrs", "--mode", "0600", *owner, f"{home}/key",
                                               swap=swapping("key", home / "key", pipe))
        time.sleep(0.5)
        check(status == tenant_paths.REFUSED_EXIT and writer.poll() is None,
              f"a link to a foreign FIFO is never opened: the writer is still waiting: {refused} {writer.poll()}")
    finally:
        writer.kill()
        writer.wait()
        pipe.unlink()
    # Swapped for something that is not a link -- a FIFO -- which O_NOFOLLOW lets
    # through: what is opened is checked to be what was looked at.
    (home / "key").unlink() if (home / "key").is_symlink() else None
    (home / "key").write_text("key\n", encoding="utf-8")

    def to_fifo(opened: str) -> None:
        if opened == "key" and (home / "key").is_file():
            (home / "key").unlink()
            os.mkfifo(home / "key")
            os.lchown(home / "key", OWNER_UID, OWNER_UID)

    status, said, refused = host.inprocess("attrs", "--mode", "0600", *owner, f"{home}/key", swap=to_fifo)
    check(status == tenant_paths.REFUSED_EXIT and refused and "replaced" in refused[0]
          and stat.S_IMODE((home / "key").lstat().st_mode) != 0o600, f"a file swapped for a FIFO: {refused}")
    (home / "key").unlink()
    # The directory the walk is ON, swapped once it holds it: nothing follows,
    # because everything below is opened from the descriptor it already has.
    status, said, refused = host.inprocess("dir", "--mode", "0750", *owner, f"{home}/worktrees/main/deep",
                                           swap=swapping("deep", home / "worktrees", foreign))
    check(status == tenant_paths.REFUSED_EXIT and refused and "changed while root was walking it" in refused[0]
          and not (foreign / "main").exists() and host.foreign_unchanged(),
          f"swapping a directory root already holds changes nothing outside the home, and is refused: {status} {refused}")


def links_during_recursion(root: Path) -> None:
    """A link that appears where the walk had listed a directory."""
    host = Host(root / "recursion")
    home, foreign = host.home, host.foreign
    for name in ("cd", "ef"):
        (home / "store.git" / "objects" / name).mkdir()
        os.lchown(home / "store.git" / "objects" / name, OWNER_UID, OWNER_UID)

    def swap(opened: str) -> None:
        if opened == "ef" and (home / "store.git" / "objects" / "ef").is_dir() and not (home / "store.git" / "objects" / "ef").is_symlink():
            host.plant(home / "store.git" / "objects" / "ef", foreign)

    status, said, refused = host.inprocess("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git",
                                           swap=swap)
    check(status == tenant_paths.REFUSED_EXIT and refused and ("symbolic link" in refused[0] or "replaced" in refused[0])
          and host.foreign_unchanged(), f"a link introduced mid-walk: refused, nothing past it changed: {refused}")
    # Replaced by another real directory, not a link: O_NOFOLLOW lets it through,
    # and what is opened is checked to be what was listed.
    (home / "store.git" / "objects" / "ef").unlink()
    for name in ("ef", "decoy"):
        (home / "store.git" / "objects" / name).mkdir()
        os.lchown(home / "store.git" / "objects" / name, OWNER_UID, OWNER_UID)

    def to_decoy(opened: str) -> None:
        decoy = home / "store.git" / "objects" / "decoy"
        if opened == "ef" and decoy.exists():
            os.rename(decoy, home / "store.git" / "objects" / "ef")

    status, said, refused = host.inprocess("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git",
                                           swap=to_decoy)
    check(status == tenant_paths.REFUSED_EXIT and refused and "replaced" in refused[0],
          f"a directory replaced by another between listing and opening: {refused}")
    (home / "store.git" / "objects" / "ef").rmdir()
    host.plant(home / "store.git" / "objects" / "ef", foreign)
    # A link that was already there is left as it is: never followed, never changed.
    status, said, refused = host.inprocess("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git")
    check(status == 0 and any("leave " in line and "ef" in line for line in said) and host.foreign_unchanged(),
          f"a link already in the tree is left alone: {said} {refused}")
    status, said, refused = host.inprocess("chown-tree", "--owner", str(SERVICE_UID), f"{home}/store.git")
    check(status == 0 and os.lstat(home / "store.git" / "objects" / "ef").st_uid == SERVICE_UID and host.foreign_unchanged(),
          f"chown-tree changes a link as a link, never its target: {refused}")


def mount_crossing(root: Path) -> None:
    host = Host(root / "mounts")
    mount_point = host.home / "store.git" / "objects" / "mounted"
    mount_point.mkdir()
    run(["mount", "-t", "tmpfs", "tmpfs", str(mount_point)])
    try:
        (mount_point / "inside").write_text("on another filesystem\n", encoding="utf-8")
        done = helper(host.home, "acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", str(host.home / "store.git"))
        check(done.returncode == tenant_paths.REFUSED_EXIT and "another filesystem" in done.stderr,
              f"a mount inside the home is not crossed: {done.stderr}")
        check("user:%d" % SERVICE_UID not in run(["getfacl", "-cpn", str(mount_point / "inside")]).stdout
              and "user:%d" % SERVICE_UID not in run(["getfacl", "-cpn", str(mount_point)]).stdout,
              "and nothing on it was changed -- not even the mount point itself")
    finally:
        run(["umount", str(mount_point)])


def preview_names_what_apply_changes(root: Path) -> None:
    host = Host(root / "parity")
    home = host.home
    owner = ("--owner", str(OWNER_UID), "--group", str(OWNER_UID))
    steps = [("dir", "--mode", "0750", *owner, f"{home}/worktrees", f"{home}/new/deeper"),
             ("acl", "--modify", f"u:{SERVICE_UID}:rX", "--recursive", f"{home}/store.git"),
             ("acl", "--modify", "d:u::rwx,d:g::r-x,d:o::---", "--if-exists", f"{home}/new/deeper"),
             ("close-children", f"{home}/worktrees")]
    os.lchown(home / "worktrees", SERVICE_UID, SERVICE_UID)  # wrong owner: the apply will change it
    os.chmod(home / "worktrees", 0o777)
    before = snapshot(home)
    planned: set[str] = set()
    previewed = []
    for words in steps:
        said = []
        status = tenant_paths.run(["--root", str(home), *words], report=said.append, refuse=said.append,
                                  preview=True, planned=planned)
        check(status == 0, f"preview {words}: {said}")
        previewed.extend(said)
    check(snapshot(home) == before, "the preview changed nothing, though the apply will")
    applied = []
    for words in steps:
        status, said, refused = host.inprocess(*words)
        check(status == 0 and not refused, f"apply {words}: {refused}")
        applied.extend(said)
    named = lambda lines: sorted(line.split(":")[0].replace("create ", "").split(" once")[0] for line in lines)
    check(named(previewed) == named(applied), f"the preview names exactly the paths the apply changes: {previewed} {applied}")


def repair_meets_a_swap_after_its_preview(root: Path) -> None:
    """`repair-boundary` previews clean, then the tenant swaps; the apply refuses on its own descriptors."""
    from repository_boundary_repair_test import PROJECT, fixture, journal_entries, name_the_namespaces_groups, repair
    from resume_provision_test import SEAM
    from root_workflow_record_test import tenant_identity

    work = root / "repair"
    work.mkdir()
    work.chmod(0o755)
    name_the_namespaces_groups(work)
    os.environ[SEAM] = str(work)
    os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(work / "journal")
    home, base, _control, _plan = fixture(work)
    foreign = work / "foreign"
    foreign.mkdir()
    (foreign / "secret").write_text("x\n", encoding="utf-8")
    for item in (foreign, foreign / "secret"):
        os.lchown(item, FOREIGN_UID, FOREIGN_UID)
        os.chmod(item, 0o700)
    untouched = snapshot(foreign)
    real_open, real_run = tenant_paths._openat, tenant_paths.run
    mode: list[bool] = []
    swapped = []

    def run_noting(argv, **kwargs):
        mode.append(bool(kwargs.get("preview")))
        return real_run(argv, **kwargs)

    def watched(name, flags, dir_fd):
        # The whole preview passes; the swap is made once the apply is running,
        # on its first open of the base.
        if name == base.name and mode and mode[-1] is False and not swapped:
            swapped.append(True)
            shutil.rmtree(base)
            base.symlink_to(foreign)
            os.lchown(base, OWNER_UID, OWNER_UID)
        return real_open(name, flags, dir_fd)

    tenant_paths._openat, tenant_paths.run = watched, run_noting
    try:
        with tenant_identity(home, owner=str(OWNER_UID)):
            status, said = repair(PROJECT, apply=True)
    finally:
        tenant_paths._openat, tenant_paths.run = real_open, real_run
    check(True in mode and False in mode and mode.index(False) > mode.index(True), f"previewed, then applied: {mode}")
    record = journal_entries(work, PROJECT)[-1]
    check(swapped and status == 1 and any("refused:" in line and "symbolic link" in line for line in said)
          and any("changed after the preview" in line for line in said),
          f"the apply refuses the swap it meets after a clean preview: {status} {said[-4:]}")
    check(record["status"] == "failed" and record["detail"].startswith("refused: "),
          f"and the journal records the refusal: {record.get('status')} {record.get('detail')}")
    check(snapshot(foreign) == untouched, "the foreign tree is exactly as it was")


def packet_refusal_is_recorded(root: Path) -> None:
    """The full packet's helper lines, run as the packet runs them: under `set -e`, inside the rollout recorder."""
    from repository_boundary_repair_test import fixture, name_the_namespaces_groups  # noqa: F401
    from scripts.ticket_board.project_provision import build_plan, render_operator_commands

    work = root / "packet"
    work.mkdir()
    work.chmod(0o755)
    home = work / "home" / "tenant"
    home.mkdir(parents=True)
    os.chown(home, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o750)
    os.chmod(work / "home", 0o755)
    plan = build_plan(project="p605", owner_user=str(OWNER_UID), owner_home=home, service_user=str(SERVICE_UID),
                      project_repository=home / "Projects" / "p605", commit_git_dir=str(home / "store.git"))
    lines = [line.strip() for line in render_operator_commands(plan).splitlines()
             # Not the unit install (its source is root's staged file) nor the key modes (the key is the owner's
             # own `ssh-keygen`, which this fixture does not run): every directory and ACL line is here.
             if line.strip().startswith('sudo /usr/bin/python3 -I "$switchyard_tenant_paths" ')
             and "' 'install' '" not in line and "' 'attrs' '" not in line]
    check(len(lines) > 20, f"the packet changes the home through the helper, line by line: {len(lines)}")
    foreign = work / "foreign"
    foreign.mkdir()
    os.lchown(foreign, FOREIGN_UID, FOREIGN_UID)
    os.chmod(foreign, 0o700)
    stubs = work / "bin"
    stubs.mkdir()
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    script = work / "operator-commands.sh"
    script.write_text("set -euo pipefail\n" + f"switchyard_tenant_paths='{HELPER}'\n" + "\n".join(lines) + "\n", encoding="utf-8")
    environment = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                   "SWITCHYARD_ROLLOUT_JOURNAL_ROOT": str(work / "journal")}
    recorder = ROOT / "scripts" / "switchyard-record-rollout"

    def record(label: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        done = subprocess.run([sys.executable, str(recorder), "p605", "--label", label, "--", "bash", str(script)],
                              capture_output=True, text=True, env=environment)
        from scripts.ticket_board.rollout_journal import attempts
        entry = attempts("p605", root=work / "journal")[-1]
        result = json.loads((Path(entry["directory"]) / "result.json").read_text(encoding="utf-8"))
        logs = "".join(p.read_text(encoding="utf-8") for p in Path(entry["directory"]).glob("*.log"))
        return done, {**result, "logs": logs}

    # A real tree first: it converges, and a second run changes nothing.
    first, first_record = record("first")
    check(first.returncode == 0 and first_record["status"] == "completed", f"the packet's lines converge a real home: {first.stdout[-600:]} {first.stderr[-600:]}")
    converged = snapshot(home)
    second, _second_record = record("second")
    check(second.returncode == 0 and snapshot(home) == converged, "and a second run leaves it exactly as it is")
    # Then the tenant plants a link where the packet makes the worktree base.
    base = home / "p605-worktrees"
    shutil.rmtree(base)
    base.symlink_to(foreign)
    os.lchown(base, OWNER_UID, OWNER_UID)
    untouched = snapshot(foreign)
    third, third_record = record("planted")
    check(third.returncode != 0 and third_record["status"] == "failed" and "symbolic link" in third_record["logs"],
          f"the packet stops nonzero and the recorder keeps the refusal: {third.returncode} {third_record['status']} {third_record['logs'][-400:]}")
    check(snapshot(foreign) == untouched, "the foreign tree is exactly as it was")


def authority_unit_install(root: Path) -> None:
    """The identity cutover's unit install: the listener into the owner's home through the helper, not `install -D`."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch

    from scripts import team_launcher as launcher
    from scripts.board_authority_install import install_board_authority_files

    host = Host(root / "authority")
    home, foreign = host.home, host.foreign
    staged = root / "authority" / "staged" / "p605"
    staged.mkdir(parents=True)
    (staged / "p605-listener.service").write_text("[Unit]\nDescription=listener\n", encoding="utf-8")
    destination = home / ".config" / "systemd" / "user" / "p605-listener.service"
    owner = str(OWNER_UID)

    def attempt() -> list[str]:
        with ExitStack() as stack:
            stack.enter_context(patch.object(launcher, "home_dir_for_user", lambda user: home if user == owner else None))
            stack.enter_context(patch.object(launcher, "switchyard_privileged_provision_root", lambda: staged.parent))
            stack.enter_context(patch.object(launcher, "privileged_provision_dir", lambda project, *, root: Path(root) / project))
            stack.enter_context(patch.object(launcher, "authority_unit_installs", lambda config, *, config_path=None:
                                             [("p605-listener.service", destination, ["-o", owner, "-g", owner])]))

            def runner(argv, **kwargs):
                if argv[:1] == ["systemctl"]:
                    return SimpleNamespace(returncode=0)
                return subprocess.run(argv, **kwargs)

            return install_board_authority_files(SimpleNamespace(project="p605", run_as_user=owner), runner=runner,
                                                 print_func=lambda line: None)

    (home / ".config").mkdir()
    os.lchown(home / ".config", OWNER_UID, OWNER_UID)
    host.plant(home / ".config" / "systemd", foreign)
    problems = attempt()
    check(problems and host.foreign_unchanged() and not (foreign / "user").exists(),
          f"a link under the owner's .config: the unit is not installed through it: {problems}")
    (home / ".config" / "systemd").unlink()
    problems = attempt()
    status = destination.lstat()
    check(problems == [] and stat.S_ISREG(status.st_mode) and (status.st_uid, status.st_gid) == (OWNER_UID, OWNER_UID)
          and stat.S_IMODE(status.st_mode) == 0o644 and (home / ".config" / "systemd").lstat().st_uid == OWNER_UID,
          f"and a real home gets its unit, the owner's, at 0644, its directories the owner's too: {problems}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd605.") as raw:
        root = Path(raw)
        root.chmod(0o755)
        subprocess.run(["mount", "--make-rprivate", "/"], check=True)
        for case in (planted_links, concurrent_swaps, links_during_recursion, mount_crossing,
                     preview_names_what_apply_changes, repair_meets_a_swap_after_its_preview, packet_refusal_is_recorded,
                     authority_unit_install):
            case(root)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"tenant_paths_test: privileged child ran {CHECKS} checks")
        return 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    unprivileged = CHECKS
    for tool in ("setfacl", "getfacl", "unshare", "mount"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    done = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable, str(Path(__file__).resolve()),
         "--privileged-child"], capture_output=True, text=True)
    print((done.stdout + done.stderr).strip())
    print(f"tenant_paths_test: {unprivileged} unprivileged checks ok")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
