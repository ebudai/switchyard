#!/usr/bin/env python3
"""SYRD-609: the paths a tenant's configuration names are held to the owner's roots when root acts on them.

A tenant's configuration names its session directory, each role's directory
and each role's name -- a component of that role's state store. Root acted on
them as given: `install -d -o` on the session directory followed a link there,
the cutover's ownership walk accepted a role named `../../etc`, and the
cutover handed any worktree a role named to the owner with `chown -R`. Now:

* `owner_writes.within_owner_root` holds a path to the owner's home, its
  runtime directory, or a project directory root chose for it, as written:
  never resolved, never with `..`;
* `ensure_owner_state_dirs` makes or corrects the two state directories with
  the descriptor helper rooted there, and leaves a path outside alone;
* the cutover's walk, its worktree handoff and root's role-state repair refuse
  a path outside before anything is given away;
* a role's name must be a single path component.

Each case proves a foreign tree unchanged byte for byte (contents, owner,
mode, ACL, xattrs). The privileged half runs in a user namespace with the
invoking account's subuid range mapped; the owner account is a stand-in for
the lookups only.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402,I001
from scripts import owner_writes  # noqa: E402
from scripts import role_identity_cutover  # noqa: E402

OWNER, OWNER_UID, FOREIGN_UID = "1500", 1500, 1700
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def captured(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True)


def snapshot(top: Path) -> list[tuple]:
    seen = []
    for item in [top, *sorted(top.rglob("*"))]:
        status = item.lstat()
        if stat.S_ISLNK(status.st_mode):
            seen.append((str(item), status.st_uid, "link", os.readlink(item)))
            continue
        xattrs = sorted((name, os.getxattr(item, name)) for name in os.listxattr(item) if name.startswith("user."))
        body = item.read_bytes() if stat.S_ISREG(status.st_mode) else b""
        seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode),
                     run(["getfacl", "-cpn", str(item)]).stdout, xattrs, body))
    return seen


def refused(call, *needles: str) -> str:
    try:
        call()
    except SystemExit as stop:
        text = str(stop)
        assert all(needle in text for needle in needles), (text, needles)
        return text
    raise AssertionError(f"not refused; expected {needles}")


def config_for(home: Path, *, session_dir: Path, roles=()) -> launcher.ProjectConfig:
    return launcher.ProjectConfig(
        project="p", project_name="P", ticket_prefix="P", layout=home / "layout.json", session_dir=session_dir,
        board_url="http://127.0.0.1:1", board_socket="/run/p.sock", upstream_report_url="",
        upstream_report_token_file="", run_as_user=OWNER, pane_launcher=None, repository=None,
        control_repository=None, worktree_base=None, worktree_remote="origin", worktree_branch="main",
        roles=list(roles), role_state_isolation=True)


# --------------------------------------------------------------------------
# the unprivileged half
# --------------------------------------------------------------------------


def test_a_path_is_within_only_as_written() -> None:
    me = launcher.current_user_name()
    home = owner_writes.owner_home(me)
    check(owner_writes.within_owner_root(me, Path(home) / ".local" / "state" / "p"), "a path in the home is within")
    # (`.` never reaches it: pathlib drops it; `..` it keeps, and that is refused rather than resolved.)
    for outside in ("/etc/passwd", f"{home}/../root", f"{home}/x/../../root", "relative/x", f"{home}x"):
        check(not owner_writes.within_owner_root(me, Path(outside)), f"{outside!r} is not within, never normalised in")
    check(not owner_writes.within_owner_root("", Path(home)), "with no owner nothing is within")


def test_a_role_name_is_one_path_component() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd609-names.") as raw:
        provision = Path(raw)
        for name in ("../../etc", "a/b", "..", "."):
            config_path = provision / "p.json"
            config_path.write_text(json.dumps({"project": "p", "roles": [{"role": name, "cli": "claude", "slot": 0}]}))
            refused(lambda: launcher.load_project_config("p", config_path), "single path component")
            check(True, f"a role named {name!r} is refused when the configuration is read")


# --------------------------------------------------------------------------
# the privileged half
# --------------------------------------------------------------------------


class Host:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home" / "tenant"
        self.state = self.home / ".local" / "state" / "p"
        self.state.mkdir(parents=True)
        for item in [self.home, *self.home.rglob("*")]:
            os.lchown(item, OWNER_UID, OWNER_UID)
        for item in (base, base / "home"):
            os.chmod(item, 0o755)
        self.foreign = base / "foreign"
        (self.foreign / "roles" / "r").mkdir(parents=True)
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        for item in [self.foreign, *self.foreign.rglob("*")]:
            os.lchown(item, FOREIGN_UID, FOREIGN_UID)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1600:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched

    def plant(self, at: Path, target: Path) -> Path:
        aside = at.with_name(at.name + ".aside")
        if at.exists() or at.is_symlink():
            os.rename(at, aside)
        at.symlink_to(target)
        os.lchown(at, OWNER_UID, OWNER_UID)
        return aside

    def unplant(self, at: Path, aside: Path) -> None:
        at.unlink()
        if aside.exists() or aside.is_symlink():
            os.rename(aside, at)


def owner_lookups(home: Path, runtime: Path) -> None:
    real = pwd.getpwnam

    def getpwnam(name: str):
        if name == OWNER:
            return pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(home), "/bin/sh"))
        return real(name)

    pwd.getpwnam = getpwnam
    launcher.current_user_name = lambda: "root"
    launcher.runtime_dir_for_uid = lambda uid: runtime


def state_dirs(host: Host) -> None:
    sessions = host.state / "sessions"
    panes = host.state / "panes"
    config = config_for(host.home, session_dir=sessions)
    launcher.ensure_owner_state_dirs(config, pane_state_dir=panes, runner=captured)
    check(all((d.lstat().st_uid, stat.S_IMODE(d.lstat().st_mode)) == (OWNER_UID, 0o700) for d in (sessions, panes)),
          "both state directories are made the owner's, 0700")
    os.lchown(sessions, 0, 0)
    launcher.ensure_owner_state_dirs(config, pane_state_dir=panes, runner=captured)
    check(sessions.lstat().st_uid == OWNER_UID, "a root-owned one is given back, as install -d would")
    # A link at the session directory, or at a directory above it: refused, never followed.
    for at in (sessions, host.state):
        aside = host.plant(at, host.foreign)
        refused(lambda: launcher.ensure_owner_state_dirs(config, pane_state_dir=panes, runner=captured),
                "symbolic link")
        host.unplant(at, aside)
        check(host.foreign_unchanged(), f"a link at {at.name}: refused, the foreign tree untouched")
    # A session directory outside the owner's roots is left alone -- however it is spelled.
    for outside in (host.foreign / "roles", Path(f"{host.home}/../../foreign")):
        launcher.ensure_owner_state_dirs(replace(config, session_dir=outside), pane_state_dir=panes, runner=captured)
        check(host.foreign_unchanged(), f"a session directory outside the home ({outside}) is never given away")
    # The owner's runtime directory is one of its roots.
    runtime = launcher.runtime_dir_for_uid(OWNER_UID)
    launcher.ensure_owner_state_dirs(replace(config, session_dir=runtime / "p"), pane_state_dir=panes, runner=captured)
    check((runtime / "p").lstat().st_uid == OWNER_UID, "a session directory in the owner's runtime directory is made")


def cutover_walk(host: Host) -> None:
    ids = (OWNER_UID, OWNER_UID)
    store = host.state / "roles" / "r"
    store.mkdir(parents=True)
    (store / "session.json").write_text("{}")
    os.lchown(store / "session.json", 0, 0)
    check(role_identity_cutover._assign_tree_owner(store, ids, owner_user=OWNER) == ""
          and (store / "session.json").lstat().st_uid == OWNER_UID, "a role's store in the home is given to the owner")
    for outside in (host.foreign / "roles" / "r", Path(f"{host.state}/roles/../../../../foreign")):
        reason = role_identity_cutover._assign_tree_owner(outside, ids, owner_user=OWNER)
        check("outside" in reason and host.foreign_unchanged(),
              f"a store outside the home ({outside}) is refused before it is walked: {reason!r}")
    # The worktree handoff: every role is checked before any tree is given away.
    roles = [SimpleNamespace(role="ok", workdir=str(host.home)), SimpleNamespace(role="far", workdir=str(host.foreign))]
    problems = role_identity_cutover._worktrees_outside_owner(OWNER, roles)
    check(len(problems) == 1 and problems[0].startswith("far:"), f"a worktree outside the home is named: {problems}")


def role_state_repair(host: Host) -> None:
    from scripts import role_state_ownership

    role = SimpleNamespace(role="r", run_as_user="")
    printed: list[str] = []
    config = config_for(host.home, session_dir=host.foreign, roles=[role])
    launcher.role_session_dir = lambda config, role: config.session_dir / "roles" / role.role
    launcher.role_state_ownership_problems = lambda config: ([(config.session_dir / "file", "wrong owner")], [])
    check(role_state_ownership.repair_role_state_ownership(config, print_func=printed.append) is False
          and "outside" in printed[-1] and host.foreign_unchanged(),
          f"root's role-state repair refuses a store outside the home, changing nothing: {printed}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd609.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        for index, case in enumerate((state_dirs, cutover_walk, role_state_repair)):
            host = Host(base / str(index))
            runtime = base / str(index) / "run" / "user" / str(OWNER_UID)
            runtime.mkdir(parents=True)
            os.chmod(runtime.parent, 0o755)
            os.lchown(runtime, OWNER_UID, OWNER_UID)
            owner_lookups(host.home, runtime)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"config_path_bounds_test: privileged child ran {CHECKS} checks")
        return 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    unprivileged = CHECKS
    for tool in ("setfacl", "getfacl", "unshare"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"
    done = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, str(Path(__file__).resolve()),
         "--privileged-child"], capture_output=True, text=True)
    print((done.stdout + done.stderr).strip())
    print(f"config_path_bounds_test: {unprivileged} unprivileged checks ok")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
