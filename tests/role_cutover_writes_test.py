#!/usr/bin/env python3
"""SYRD-612: root's role-cutover repatriation and runtime projection follow no link.

As root on a tenant's behalf, `upgrade` moved a role account's resumable state
into the owner's tree by path -- session records read with `read_text` and
written beside an `exists()` check, provider state copied with `copy2` after
`is_file()`, legacy worktrees handed over with `chown -R` -- and the cutover
transaction and its rollback read and wrote the configuration, the owner's
listener unit and the board's release pointer by path. Root's repair of a
stopped role's runtime read the tenant's configuration and projection by path
into a journal the owner can read, and took that journal's owner from a
by-path `stat` of the configuration. Each followed a link a tenant or a role
left at the file or at any directory above it.

Now each goes by descriptor from the right account's root, nothing is decided
by looking at a tenant path by name, and what root checks in the owner's tree
after a copy it checks as the owner. Each case below runs the whole caller as
root -- the runtime repair is `switch_role_runtime` itself, from its preflight
through its rollback to the journal's removal, and the cutover transaction is
`cutover_role_identities_command` itself, forward and rolled back, each with
links swapped in between its steps -- under a recorder of every pathname lookup or change in the
tenant's homes (which must see none), and proves a foreign tree unchanged byte
for byte -- contents, owner, mode, ACL and xattrs. Root that says it is the
Director is refused. The privileged half runs in a user namespace with
the invoking account's subuid range mapped; the owner and role accounts are
stand-ins for the lookups, named by number so the descriptor helper, in its own
process, resolves them too.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
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
from scripts import board_services, presentation_controller, role_identity_cutover, role_runtime  # noqa: E402
from scripts import runtime_projection  # noqa: E402
from scripts import stopped_project_model_repair, tenant_release_report  # noqa: E402

OWNER, OWNER_UID, ROLE, ROLE_UID, FOREIGN_UID = "1500", 1500, "1600", 1600, 1700
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


def runner(argv, **kwargs):
    """The host as root sees it: no role session is live, the descriptor helper runs for real, the rest succeeds."""
    if "has-session" in argv:
        return subprocess.CompletedProcess(argv, 1, "", "")
    if "--root" in argv:
        return subprocess.run(argv, capture_output=True, text=True)
    return subprocess.CompletedProcess(argv, 0, "", "")


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


def owned(path: Path) -> tuple[int, int]:
    status = path.lstat()
    return status.st_uid, stat.S_IMODE(status.st_mode)


@contextlib.contextmanager
def lookups(top: Path):
    """Every pathname lookup or change of something under `top` -- by `pathlib` or `os` -- while this holds.

    Descriptor-relative calls (a name with `dir_fd`) are what root is meant to use, and are not recorded; nor is what
    the test itself does to the tree while `PAUSED`. A child root forks to act as an account records into its own
    copy, which this never sees.
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
                         (Path, "lstat"), (Path, "resolve"), (Path, "read_text"), (Path, "read_bytes"), (os, "stat"),
                         (os, "lstat"), (os, "access"), (os, "readlink"), (os.path, "exists"), (os.path, "lexists"),
                         (os.path, "isdir"), (os.path, "isfile"), (os.path, "islink"), (os.path, "realpath"),
                         (builtins, "open"), (io, "open"), (os, "open"), (os, "unlink"), (os, "remove"),
                         (os, "rename"), (os, "replace"), (os, "mkdir"), (os, "makedirs"), (os, "rmdir"), (os, "chmod"),
                         (os, "chown"), (os, "lchown"), (os, "symlink"), (os, "link"), (os, "utime"), (os, "listdir"),
                         (os, "scandir")):
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


class Host:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.homes = base / "home"
        self.home = self.homes / "tenant"
        self.role_home = self.homes / "role"
        for home, uid in ((self.home, OWNER_UID), (self.role_home, ROLE_UID)):
            home.mkdir(parents=True)
            os.lchown(home, uid, uid)
        for item in (base, self.homes):
            os.chmod(item, 0o755)
        self.foreign = base / "foreign"
        self.foreign.mkdir()
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "secret.json").write_text(json.dumps({"target": "p-ops:0.0", "session_id": "stolen",
                                                              "roles": [{"role": "ops", "run_as_user": "root"}]}))
        for item in [self.foreign, *self.foreign.rglob("*")]:
            ids = 0 if item.name == "secret.json" else FOREIGN_UID
            os.lchown(item, ids, ids)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1800:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched and sorted(p.name for p in self.foreign.iterdir()) == ["file", "secret.json"]

    def make(self, path: Path, text: str, uid: int, mode: int = 0o644) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        for item in [path, *path.parents]:
            if item == self.homes:
                break
            if item.lstat().st_uid == 0:
                os.lchown(item, uid, uid)
        os.chmod(path, mode)
        return path

    def link(self, at: Path, target: Path, uid: int) -> Path:
        at.parent.mkdir(parents=True, exist_ok=True)
        at.symlink_to(target)
        os.lchown(at, uid, uid)
        return at

    def plant(self, at: Path, target: Path, uid: int = OWNER_UID) -> Path:
        aside = at.with_name(at.name + ".aside")
        os.rename(at, aside)
        self.link(at, target, uid)
        return aside

    def unplant(self, at: Path, aside: Path) -> None:
        at.unlink()
        os.rename(aside, at)


def accounts(host: Host) -> None:
    real_name, real_uid = pwd.getpwnam, pwd.getpwuid
    stand_ins = {OWNER: pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(host.home), "/bin/sh")),
                 ROLE: pwd.struct_passwd((ROLE, "x", ROLE_UID, ROLE_UID, "", str(host.role_home), "/bin/sh"))}
    by_uid = {entry.pw_uid: entry for entry in stand_ins.values()}
    pwd.getpwnam = lambda name: stand_ins[name] if name in stand_ins else real_name(name)
    pwd.getpwuid = lambda uid: by_uid[uid] if uid in by_uid else real_uid(uid)
    launcher.current_user_name = lambda: "root"


def role_config(workdir: Path, *, cli: str = "codex", run_as_user: str = ROLE) -> launcher.RoleConfig:
    return launcher.RoleConfig(
        role="ops", slot=0, detached=False, tmux_session="p-ops", target="p-ops:0.0", workdir=str(workdir), cli=[cli],
        model="", model_arg="--model", effort="", yolo=False, extra_args=[], resume_mode="", resume_flag="",
        resume_subcommand="", fresh_session_per_ticket=False, live_commands=[cli], env={}, run_as_user=run_as_user)


def project_config(host: Host, roles, *, isolation: bool = True) -> launcher.ProjectConfig:
    return launcher.ProjectConfig(
        project="p", project_name="P", ticket_prefix="P", layout=host.home / "layout.json",
        session_dir=host.home / ".local" / "state" / "switchyard" / "projects" / "p", board_url="http://127.0.0.1:1",
        board_socket="/run/p.sock", upstream_report_url="", upstream_report_token_file="", run_as_user=OWNER,
        pane_launcher=None, repository=None, control_repository=None, worktree_base=None, worktree_remote="origin",
        worktree_branch="main", roles=list(roles), role_state_isolation=isolation)


class Legacy:
    """A tenant with one role still bound to its own account: its records and Codex state, and its worktree."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.worktree = host.home / "wt"
        self.role = role_config(self.worktree)
        self.config = project_config(host, [self.role])
        self.source = launcher.account_session_dir(ROLE, project="p")
        self.record = self.source / launcher.session_file_name(self.role.target)
        self.transcript = host.role_home / ".codex" / "sessions" / "2026" / "rollout-S1.jsonl"
        host.make(self.record, json.dumps({"target": self.role.target, "session_id": "S1",
                                           "payload": {"transcript_path": str(self.transcript)}}), ROLE_UID, 0o600)
        host.make(self.transcript, "the conversation\n", ROLE_UID, 0o640)
        os.utime(self.transcript, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        host.make(self.transcript.with_name("other.jsonl"), "another\n", ROLE_UID)
        host.link(self.transcript.parent.parent / "leak.jsonl", host.foreign / "secret.json", ROLE_UID)
        host.link(self.transcript.parent.parent / "away", host.foreign, ROLE_UID)
        self.target = host.home / ".codex" / "sessions" / "2026"
        host.link(self.target / "other.jsonl", host.foreign / "file", OWNER_UID)
        host.make(self.worktree / "work.txt", "work\n", ROLE_UID)
        host.link(self.worktree / "theirs", host.foreign / "file", ROLE_UID)
        self.config_path = host.make(host.home / "p.json", json.dumps(
            {"project": "p", "roles": [{"role": "ops", "run_as_user": ROLE}], "role_state_isolation": False}),
            OWNER_UID)

    def repatriate(self):
        return role_identity_cutover.repatriate_role_runtime_state(self.config, config_path=self.config_path,
                                                                   runner=runner)


def repatriation(host: Host) -> None:
    legacy = Legacy(host)
    with lookups(host.homes) as seen:
        changed, problems = legacy.repatriate()
    copied = host.home / ".codex" / "sessions" / "2026" / "rollout-S1.jsonl"
    record = legacy.config.session_dir / "roles" / "ops" / legacy.record.name
    check(changed and problems == [] and seen == [], f"the whole repatriation, nothing looked up by path: {problems} {seen}")
    check(owned(copied) == (OWNER_UID, 0o640) and copied.lstat().st_mtime_ns == 1_000_000_000_000_000_000
          and copied.read_text() == "the conversation\n",
          "the role's Codex state is copied in as the owner's, with the source's mode and times")
    check(owned(record) == (OWNER_UID, 0o600) and json.loads(record.read_text())["payload"]["transcript_path"] == str(copied),
          "the session record is the owner's, private, pointing at the owner's copy of the transcript")
    check(not (host.home / ".codex" / "sessions" / "leak.jsonl").exists() and not (host.home / ".codex" / "sessions" / "away").exists()
          and legacy.target.joinpath("other.jsonl").is_symlink() and host.foreign_unchanged(),
          "links in the role's tree are never followed or copied; the owner's own link is never created through")
    check(owned(legacy.worktree)[0] == OWNER_UID and (legacy.worktree / "work.txt").lstat().st_uid == OWNER_UID
          and (legacy.worktree / "theirs").lstat().st_uid == OWNER_UID and host.foreign_unchanged(),
          "the worktree is handed back, a link in it changed as a link only")
    published = json.loads(legacy.config_path.read_text())
    check(published["role_state_isolation"] is True and "run_as_user" not in published["roles"][0]
          and owned(legacy.config_path)[0] == OWNER_UID, "the configuration is published as the owner's, the binding gone")


def repatriation_refusals(host: Host) -> None:
    legacy = Legacy(host)
    superseded = legacy.record.with_name(legacy.record.name + ".superseded")
    host.link(superseded, host.foreign / "secret.json", ROLE_UID)
    with lookups(host.homes) as seen:
        changed, problems = legacy.repatriate()
    check(not changed and problems and "cannot read session record" in problems[0] and seen == []
          and "run_as_user" in json.loads(legacy.config_path.read_text())["roles"][0] and host.foreign_unchanged(),
          f"a link in the role's records is refused, never read; nothing is published: {problems} {seen}")
    superseded.unlink()
    aside = host.plant(host.home / ".codex", host.foreign)
    with lookups(host.homes) as seen:
        changed, problems = legacy.repatriate()
    host.unplant(host.home / ".codex", aside)
    check(not changed and problems and "symbolic link" in " ".join(problems) and seen == [] and host.foreign_unchanged(),
          f"a link above the owner's copy is refused; nothing is written through it: {problems} {seen}")


def revert(host: Host) -> None:
    config = project_config(host, [role_config(host.home / "wt")])
    provision = host.home / "prov"
    config_path = host.make(provision / "p.json", json.dumps({"roles": [{"role": "ops", "run_as_user": ROLE}]}), OWNER_UID, 0o640)
    for at, target in ((config_path, host.foreign / "secret.json"), (provision, host.foreign)):
        aside = host.plant(at, target)
        with lookups(host.homes) as seen:
            changed, message = role_identity_cutover.revert_incomplete_role_account_cutover(config, config_path=config_path)
        host.unplant(at, aside)
        check(not changed and "cannot read" in message and seen == [] and host.foreign_unchanged(),
              f"a link at {at.name}: the revert reads nothing through it, writes nothing: {message!r} {seen}")
    with lookups(host.homes) as seen:
        changed, _message = role_identity_cutover.revert_incomplete_role_account_cutover(config, config_path=config_path)
    check(changed and "run_as_user" not in json.loads(config_path.read_text())["roles"][0]
          and owned(config_path) == (OWNER_UID, 0o640) and seen == [],
          f"the revert rewrites the configuration by descriptor, keeping its owner and mode: {seen}")


def cutover_trees(host: Host) -> None:
    """The cutover transaction's handoff and its rollback, and what it captures to put back."""
    worktree = host.home / "trees" / "wt"
    host.make(worktree / "work.txt", "work\n", OWNER_UID)
    host.link(worktree / "theirs", host.foreign / "file", OWNER_UID)
    config = project_config(host, [role_config(worktree)])
    with lookups(host.homes) as seen:
        ownership = role_identity_cutover._worktree_ownership(config, owner=OWNER)
        given = role_identity_cutover._give_tree(OWNER, worktree, ROLE, "", runner=runner, by_descriptor=True)
    check(ownership == {str(worktree): (OWNER_UID, OWNER_UID)} and given and owned(worktree)[0] == ROLE_UID
          and (worktree / "theirs").lstat().st_uid == ROLE_UID and host.foreign_unchanged() and seen == [],
          f"each tree's owner read by descriptor, then given to the role, a link in it as a link: {seen}")
    with lookups(host.homes) as seen:
        back = role_identity_cutover._give_tree(OWNER, worktree, str(OWNER_UID), "1800", runner=runner,
                                                by_descriptor=True)
    check(back and (worktree / "work.txt").lstat()[4:6] == (OWNER_UID, 1800) and seen == [],
          "and put back exactly, to the very uid and gid a rollback names")
    aside = host.plant(host.home / "trees", host.foreign)
    with lookups(host.homes) as seen:
        ownership = role_identity_cutover._worktree_ownership(config, owner=OWNER)
        given = role_identity_cutover._give_tree(OWNER, worktree, ROLE, "", runner=runner, by_descriptor=True)
    host.unplant(host.home / "trees", aside)
    check(ownership == {} and not given and host.foreign_unchanged() and seen == [],
          f"a link above a tree: nothing read through it, nothing given away: {seen}")
    # The board's release pointer, in the owner's home.
    board = host.home / "p-ticketboard-live"
    (board / "releases" / "old").mkdir(parents=True)
    (board / "current").symlink_to("releases/old")
    launcher._tenant_board_root_from_config_or_plan = lambda config, config_path=None: board
    with lookups(host.homes) as seen:
        pointer, target = tenant_release_report.capture_release_pointer(config, config_path=host.home / "p.json")
        PAUSED.append(True)
        (board / "current").unlink()
        (board / "current").symlink_to("releases/new")
        PAUSED.pop()
        problems = tenant_release_report.restore_release_pointer(pointer, target, runner=runner, owner_user=OWNER)
    check((pointer, target) == (board / "current", "releases/old") and problems == [] and seen == []
          and os.readlink(board / "current") == "releases/old", f"the release pointer captured and put back: {seen}")
    aside = host.plant(board, host.foreign)
    with lookups(host.homes) as seen:
        refused(lambda: tenant_release_report.capture_release_pointer(config, config_path=host.home / "p.json"), "symbolic link")
        problems = tenant_release_report.restore_release_pointer(board / "current", "releases/old", runner=runner,
                                                                 owner_user=OWNER)
    host.unplant(board, aside)
    check(problems and host.foreign_unchanged() and seen == [],
          f"a link at the board root: nothing read through it, no link made through it: {problems} {seen}")
    # The owner's listener user unit: captured and put back, or taken away.
    unit = host.home / ".config" / "systemd" / "user" / "p-listener.service"
    host.make(unit, "[Unit]\n", OWNER_UID)
    board_services.authority_unit_installs = launcher.authority_unit_installs = (
        lambda config, config_path=None: [("p-listener.service", unit, None)])
    board_services.board_is_supervised = lambda project: False
    with lookups(host.homes) as seen:
        captured = launcher.capture_installed_units(config)
    unit.write_text("changed\n")
    with lookups(host.homes) as seen_restore:
        problems = board_services.restore_installed_units(config, captured, runner=runner)
    restored = unit.read_bytes()
    with lookups(host.homes) as seen_remove:
        problems += board_services.restore_installed_units(config, {"p-listener.service": None}, runner=runner)
    check(captured == {"p-listener.service": b"[Unit]\n"} and restored == b"[Unit]\n" and not unit.exists()
          and problems == [] and seen + seen_restore + seen_remove == [],
          f"the listener unit captured, put back and taken away by descriptor: {captured} {restored} {problems} "
          f"{seen + seen_restore + seen_remove}")
    host.link(unit, host.foreign / "secret.json", OWNER_UID)
    with lookups(host.homes) as seen:
        refused(lambda: launcher.capture_installed_units(config), "symbolic link")
    unit.unlink()
    aside = host.plant(host.home / ".config", host.foreign)
    with lookups(host.homes) as seen:
        problems = board_services.restore_installed_units(config, {"p-listener.service": b"[Unit]\n"}, runner=runner)
    host.unplant(host.home / ".config", aside)
    check(problems and "symbolic link" in problems[0] and host.foreign_unchanged() and seen == [],
          f"a link at the unit or above it: nothing read or written through it: {problems} {seen}")


class Switch:
    """Root's repair of a stopped role's model, run whole: `switch_role_runtime` as the operator, from preflight to
    the journal's removal, with the configuration loaded as the command loads it."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.provision = host.home / "prov"
        self.workdir = host.home / "wt"
        roles = [{"role": "director", "cli": ["claude"], "detached": True, "workdir": str(host.home / "wd")},
                 {"role": "ops", "cli": ["claude"], "model": "m1", "detached": True, "workdir": str(self.workdir)}]
        self.config_path = host.make(self.provision / "p.json", json.dumps(
            {"project": "p", "run_as_user": OWNER, "role_state_isolation": True, "roles": roles},
            indent=2, sort_keys=True) + "\n", OWNER_UID)
        self.original = self.config_path.read_text()
        self.config = launcher.load_project_config("p", self.config_path)
        role = launcher._role_by_name(self.config, "ops")
        self.record = launcher.role_session_dir(self.config, role) / launcher.session_file_name(role.target)
        host.make(self.record, json.dumps({"target": role.target, "session_id": "S1"}), OWNER_UID, 0o600)
        host.make(self.record.with_name(self.record.name + ".superseded"), "{}", OWNER_UID, 0o600)
        self.panes = host.home / "panes"
        self.pane_state = host.make(self.panes / launcher.pane_state_file_name(role.target), "{}", OWNER_UID, 0o600)
        host.make(host.home / ".claude.json", json.dumps({"projects": {str(self.workdir): {"hasTrustDialogAccepted": True}}}),
                  OWNER_UID, 0o600)
        self.journal = launcher.default_layout_output_path(self.config, config_path=self.config_path).parent / \
            "role-runtime-ops.journal.json"
        self.said: list[str] = []
        self.fail = False
        self.after_write = None
        self.probes: list[list[str]] = []
        self.on_probe = None

    def runner(self, argv, **kwargs):
        """No role session is live; the descriptor helper runs for real; the owner's claude is logged in."""
        if "has-session" in argv:
            self.probes.append(list(argv))
            if self.on_probe is not None:
                self.on_probe(len(self.probes))
            if self.fail:
                raise RuntimeError("tmux went away")
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: p-ops")
        if "--root" in argv:
            return subprocess.run(argv, capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": True}), "")

    def run(self):
        self.probes.clear()
        real_write = role_runtime.RuntimeJournal.write

        def write(journal, path):
            real_write(journal, path)
            if self.after_write is not None:
                self.after_write(journal)

        role_runtime.RuntimeJournal.write = write
        try:
            return role_runtime.switch_role_runtime(
                self.config, config_path=self.config_path, role_name="ops", runtime="claude", model="", environ={},
                pane_state_dir=self.panes, runner=self.runner, print_func=self.said.append,
                workflow_reader=lambda board_url: ({"roles": [{"name": "director", "runtime": "claude"},
                                                              {"name": "ops", "runtime": "claude"}]}, 3))
        finally:
            role_runtime.RuntimeJournal.write = real_write

    def model(self) -> object:
        return next(r for r in json.loads(self.config_path.read_text())["roles"] if r["role"] == "ops").get("model")


def swap(at: Path, target: Path) -> Path:
    """Replace a directory with a link the owner made -- by `os` alone, so a recorder running around it sees nothing."""
    aside = at.with_name(at.name + ".aside")
    PAUSED.append(True)
    try:
        os.rename(at, aside)
        os.symlink(target, at)
        os.lchown(at, OWNER_UID, OWNER_UID)
    finally:
        PAUSED.pop()
    return aside


def decoy(host: Host, name: str, label: str) -> tuple[Path, list[tuple]]:
    """A foreign directory holding a file of `name`, as the target of a swapped-in link, and how it looks now."""
    place = host.base / f"decoy-{label}"
    place.mkdir()
    (place / name).write_text("foreign\n", encoding="utf-8")
    for item in (place, place / name):
        os.lchown(item, FOREIGN_UID, FOREIGN_UID)
    return place, snapshot(place)


def runtime_switch(host: Host) -> None:
    """The whole root repair succeeds by descriptor, and is refused, before anything changes, at a link."""
    switch = Switch(host)
    with lookups(host.homes) as seen:
        result = switch.run()
    superseded = switch.record.with_name(switch.record.name + ".superseded")
    check(result.configured_changed and not result.live_session_changed and switch.model() in (None, "")
          and owned(switch.config_path) == (OWNER_UID, 0o644) and not switch.journal.exists() and seen == [],
          f"root's repair, whole: the projection written as the owner's, the journal gone, nothing looked up by path: {seen}")
    check(not switch.record.exists() and json.loads(superseded.read_text())["session_id"] == "S1"
          and superseded.with_name(superseded.name + ".1").read_text() == "{}" and not switch.pane_state.exists()
          and owned(superseded) == (OWNER_UID, 0o600) and host.foreign_unchanged(),
          "the resume record is set aside and the pane state cleared, by descriptor")
    for at, target in ((switch.config_path, host.foreign / "secret.json"), (switch.provision, host.foreign)):
        switch.config_path.write_text(switch.original)
        aside = host.plant(at, target)
        with lookups(host.homes) as seen:
            text = refused(switch.run, "cannot switch ops", "symbolic link")
        host.unplant(at, aside)
        check(host.foreign_unchanged() and switch.config_path.read_text() == switch.original and not switch.journal.exists()
              and seen == [], f"a link at {at.name}: refused before anything changes, nothing read through it: {text} {seen}")
    # The configuration taken away once every check has read it -- at the repair's own probe of the Director's
    # session -- is refused, never journalled as an empty file that a rollback would then write back.
    away = switch.config_path.with_name("p.json.away")
    switch.on_probe = lambda count: (PAUSED.append(True), os.rename(switch.config_path, away), PAUSED.pop()) \
        if count == 2 else None
    with lookups(host.homes) as seen:
        text = refused(switch.run, "nothing is at", "to read")
    gone = not os.path.lexists(switch.config_path)
    os.rename(away, switch.config_path)
    switch.on_probe = None
    check(gone and not switch.journal.exists() and seen == [] and switch.config_path.read_text() == switch.original,
          f"a configuration gone after the checks: refused, nothing journalled or written back: {text} {seen}")
    switch.config_path.write_text(switch.original)
    aside = host.plant(switch.journal.parent, host.foreign)
    with lookups(host.homes) as seen:
        text = refused(switch.run, "symbolic link")
    host.unplant(switch.journal.parent, aside)
    check(host.foreign_unchanged() and switch.config_path.read_text() == switch.original and seen == [],
          f"a link above the journal: refused before anything changes, no journal made through it: {text} {seen}")


def runtime_pane_link(host: Host) -> None:
    """A link at the pane state directory: the repair goes on, and clears nothing through it."""
    switch = Switch(host)
    place, before = decoy(host, switch.pane_state.name, "panes")
    aside = host.plant(switch.panes, place)
    with lookups(host.homes) as seen:
        result = switch.run()
    host.unplant(switch.panes, aside)
    check(result.configured_changed and snapshot(place) == before and switch.pane_state.exists() and seen == []
          and host.foreign_unchanged(), f"the pane state's directory a link: nothing unlinked through it: {seen}")


def runtime_rollback(host: Host) -> None:
    """A failure after the projection is written rolls it back by descriptor -- through links swapped in meanwhile."""
    switch = Switch(host)

    def failing(journal) -> None:
        if "projection" in journal.steps_applied:
            switch.fail = True

    switch.after_write = failing
    with lookups(host.homes) as seen:
        text = refused(switch.run, "still runs claude; the switch was undone")
    check(switch.config_path.read_text() == switch.original and not switch.journal.exists() and seen == []
          and host.foreign_unchanged(), f"the projection put back and the journal removed, by descriptor: {text} {seen}")
    # The configuration's directory swapped for a link after the projection: the restore writes nothing through it.
    switch.fail = False
    swapped: list[Path] = []

    def swap_provision(journal) -> None:
        if "projection" in journal.steps_applied and not swapped:
            swapped.append(swap(switch.provision, host.foreign))
            switch.fail = True

    switch.after_write = swap_provision
    with lookups(host.homes) as seen:
        text = refused(switch.run, "left between runtimes and needs an operator", "symbolic link")
    left = json.loads(switch.journal.read_text())
    switch.provision.unlink()
    os.rename(swapped[0], switch.provision)
    check(host.foreign_unchanged() and seen == [] and "symbolic link" in left["observed"]["launcher_error"]
          and left["rollback_problems"], f"a link swapped in above the configuration: nothing written or read through "
          f"it, and the journal says what was seen: {text} {seen}")
    switch.journal.unlink()
    switch.config_path.write_text(switch.original)
    # A restore that fails on its own: what the rollback then reports it reads by descriptor, and reloads nothing.
    real_restore = role_runtime._restore_projection

    def full(journal) -> None:
        switch.fail = False  # tmux answers again, so what the worker shows is read too
        raise OSError(28, "No space left on device")

    switch.fail, swapped[:] = False, []
    switch.after_write = failing
    role_runtime._restore_projection = full
    try:
        with lookups(host.homes) as seen:
            text = refused(switch.run, "left between runtimes and needs an operator", "No space left")
    finally:
        role_runtime._restore_projection = real_restore
    observed = json.loads(switch.journal.read_text())["observed"]
    check(seen == [] and observed.get("launcher_runtime") == "claude" and observed.get("worker_live") is False
          and host.foreign_unchanged(), f"the rollback's report read by descriptor, nothing reloaded: {observed} {seen}")
    switch.journal.unlink()
    switch.config_path.write_text(switch.original)
    # The journal's directory swapped for a link before its removal: the same-named foreign file stays.
    for step, expect in (("worker", None), ("projection", "the switch was undone")):
        place, before = decoy(host, switch.journal.name, step)
        switch.fail = False
        swapped.clear()

        def swap_journal(journal, step=step, place=place) -> None:
            if step in journal.steps_applied and not swapped:
                swapped.append(swap(switch.journal.parent, place))
                switch.fail = step == "projection"

        switch.after_write = swap_journal
        switch.said.clear()
        with lookups(host.homes) as seen:
            if expect is None:
                switch.run()
            else:
                refused(switch.run, expect)
        switch.journal.parent.unlink()
        os.rename(swapped[0], switch.journal.parent)
        check(snapshot(place) == before and seen == [] and any("journal is left there" in line for line in switch.said),
              f"a link above the journal after its last write ({step}): nothing removed through it: {switch.said} {seen}")
        switch.journal.unlink()
        switch.config_path.write_text(switch.original)
    # A link at the resume store: setting the record aside is refused, and the switch rolls back.
    switch.after_write = None
    switch.fail = False
    store = switch.record.parent
    aside = host.plant(store, host.foreign)
    with lookups(host.homes) as seen:
        text = refused(switch.run, "the switch was undone", "symbolic link")
    host.unplant(store, aside)
    check(host.foreign_unchanged() and switch.config_path.read_text() == switch.original and not switch.journal.exists()
          and seen == [], f"a link at the resume store: nothing renamed through it, the switch undone: {text} {seen}")


def root_director(host: Host) -> None:
    """Root that says it is the Director takes no path of the Director's."""
    switch = Switch(host)
    with lookups(host.homes) as seen:
        text = refused(lambda: role_runtime.switch_role_runtime(
            switch.config, config_path=switch.config_path, role_name="ops", runtime="claude", model="",
            environ={"TICKET_BOARD_CALLER_ROLE": "director"}, runner=switch.runner), "root is not the Director")
    check(switch.config_path.read_text() == switch.original and seen == [], f"refused, nothing read: {text} {seen}")


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


class Cutover:
    """`cutover_role_identities_command` run whole as root, given a configuration loaded as the command loads it.

    What it does in the owner's tree runs for real: the configuration read and put back, the trees handed over and
    back, the listener unit and the release pointer captured and restored, the presentation gate, attachment check,
    state lock, read and write, and the artifact refresh. Stood in are only the phases that are not in the owner's
    tree or not this ticket's: the workers' start and stop (the command's own seams), the pending identities and
    accounts, the staged tooling check, the board release's deploy, and root's own upgrade journal.
    """

    def __init__(self, host: Host) -> None:
        self.host = host
        self.provision = host.home / "prov"
        self.worktree = host.home / "trees" / "wt"
        host.make(self.worktree / "work.txt", "work\n", OWNER_UID)
        self.config_path = host.make(self.provision / "p.json", json.dumps(
            {"project": "p", "run_as_user": OWNER, "presentation": {}, "roles": [
                {"role": "ops", "cli": ["claude"], "slot": 0, "workdir": str(self.worktree)}]},
            indent=2, sort_keys=True) + "\n", OWNER_UID)
        self.original = self.config_path.read_text()
        self.config = launcher.load_project_config("p", self.config_path)
        self.state = launcher.default_layout_output_path(self.config, config_path=self.config_path).parent
        self.board = host.home / "p-ticketboard-live"
        (self.board / "releases" / "old").mkdir(parents=True)
        (self.board / "current").symlink_to("releases/old")
        self.unit = host.make(host.home / ".config" / "systemd" / "user" / "p-listener.service", "[Unit]\n", OWNER_UID)
        self.proc = host.base / "proc"
        self.proc.mkdir(exist_ok=True)
        self.provision_root = host.base / "etc-provision"  # root's own staging, where the generated units are
        staged = launcher.privileged_provision_dir("p", root=self.provision_root) / "p-listener.service"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("[Unit]\n", encoding="utf-8")
        self.starts: list[int] = []
        self.on_start = None
        self.said: list[str] = []

    def runner(self, argv, **kwargs):
        """No worker session; the listener stopped, the board up; the descriptor helper runs for real; the rest succeeds."""
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "can't find session")
        if any("is-active" in str(arg) for arg in argv):
            listener = any("listener" in str(arg) for arg in argv)
            return subprocess.CompletedProcess(argv, 3 if listener else 0, "inactive\n" if listener else "active\n", "")
        if "--root" in argv:
            return subprocess.run(argv, capture_output=True, text=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def start(self, config) -> int:
        self.starts.append(1)
        return self.on_start(len(self.starts)) if self.on_start else 0

    def run(self) -> int:
        self.starts.clear()
        self.said.clear()
        unit = self.unit
        with standing_in(launcher, read_pending_identities=lambda config: {"ops": {"account": ROLE}},
                         _role_accounts_ready=lambda config: True,
                         staged_role_tooling_problems=lambda *args, **kwargs: [],
                         deploy_release_in_transaction=lambda config, **kwargs: ([], False),
                         record_upgrade_phase=lambda config, **kwargs: None,
                         switchyard_privileged_provision_root=lambda: self.provision_root,
                         _tenant_board_root_from_config_or_plan=lambda config, config_path=None: self.board,
                         authority_unit_installs=lambda config, config_path=None: [("p-listener.service", unit, None)]), \
                standing_in(board_services, board_is_supervised=lambda project: False,
                            authority_unit_installs=lambda config, config_path=None: [("p-listener.service", unit, None)]):
            return role_identity_cutover.cutover_role_identities_command(
                self.config, config_path=self.config_path, runner=self.runner, launcher=self.start,
                stopper=lambda config: 0, proc_root=self.proc, print_func=self.said.append)


def cutover_whole(host: Host) -> None:
    """The cutover transaction, whole, as root: forward, rolled back, and with links swapped in while it runs."""
    cutover = Cutover(host)
    with lookups(host.homes) as seen:
        result = cutover.run()
    check(result == 0 and owned(cutover.worktree)[0] == ROLE_UID and owned(cutover.state / "presentation.json")[0] == OWNER_UID
          and cutover.config_path.read_text() == cutover.original and seen == [] and host.foreign_unchanged(),
          f"the cutover, whole: trees handed over, presentation re-pointed, nothing looked up by path: {result} "
          f"{cutover.said} {seen}")
    # The workers do not come back: everything goes back, by descriptor.
    os.lchown(cutover.worktree, OWNER_UID, OWNER_UID)
    cutover.on_start = lambda count: 1 if count == 1 else 0
    with lookups(host.homes) as seen:
        result = cutover.run()
    check(result == 1 and owned(cutover.worktree)[0] == OWNER_UID and cutover.config_path.read_text() == cutover.original
          and os.readlink(cutover.board / "current") == "releases/old" and cutover.unit.read_text() == "[Unit]\n"
          and seen == [] and host.foreign_unchanged(),
          f"rolled back whole, by descriptor, nothing reloaded or looked up by path: {cutover.said} {seen}")
    # The configuration's and the presentation state's directories swapped for links once the workers are stopped.
    for fails in (False, True):
        swapped: list[tuple[Path, Path]] = []

        def swap_in(count, fails=fails):
            if count == 1:
                swapped.extend((at, swap(at, host.foreign)) for at in (cutover.provision, cutover.state))
            return 1 if fails and count == 1 else 0

        cutover.on_start = swap_in
        with lookups(host.homes) as seen:
            result = cutover.run()
        for at, aside in swapped:
            at.unlink()
            os.rename(aside, at)
        check(result == (1 if fails else 0) and host.foreign_unchanged() and seen == []
              and cutover.config_path.read_text() == cutover.original,
              f"links swapped in at the configuration and the presentation state ({'rolled back' if fails else 'forward'}): "
              f"nothing read or written through them: {cutover.said} {seen}")
        os.lchown(cutover.worktree, OWNER_UID, OWNER_UID)
    # The presentation state's directory alone: the gate reads the configuration, the reconnect is refused at the
    # state's lock, and that refusal is a reason to roll back -- not an exit halfway through the transaction.
    swapped.clear()

    def swap_state(count):
        if count == 1:
            swapped.append((cutover.state, swap(cutover.state, host.foreign)))
        return 0

    cutover.on_start = swap_state
    with lookups(host.homes) as seen:
        result = cutover.run()
    cutover.state.unlink()
    os.rename(swapped[0][1], cutover.state)
    said = " ".join(cutover.said)
    check(result == 1 and "the presentation did not reconnect" in said and "symbolic link" in said
          and owned(cutover.worktree)[0] == OWNER_UID and host.foreign_unchanged() and seen == [],
          f"a link swapped in at the presentation state: refused, reported and rolled back: {cutover.said} {seen}")
    # A link already there before the transaction: asking whether anyone is looking is refused too, not an exit.
    place, before = decoy(host, "presentation.json", "state")
    aside = host.plant(cutover.state, place)
    cutover.on_start = None
    with lookups(host.homes) as seen:
        result = cutover.run()
    check(result == 1 and "the presentation did not reconnect" in " ".join(cutover.said) and snapshot(place) == before
          and owned(cutover.worktree)[0] == OWNER_UID and host.foreign_unchanged() and seen == [],
          f"a link at the presentation state from the start: nothing read through it, rolled back: {cutover.said} {seen}")
    # The gate's other half: a configuration naming no presentation, and a state file there -- or a link to one.
    bare = host.make(cutover.provision / "q.json", json.dumps({"project": "p", "run_as_user": OWNER, "roles": []}), OWNER_UID)
    with lookups(host.homes) as seen:
        linked = presentation_controller.presentation_enabled(cutover.config, config_path=bare)
    host.unplant(cutover.state, aside)
    with lookups(host.homes) as seen_too:
        present = presentation_controller.presentation_enabled(cutover.config, config_path=bare)
    check(present and not linked and seen + seen_too == [],
          f"the presentation gate looks at the state by descriptor, never through a link: {present} {linked} {seen}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd612.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        saved = {name: getattr(launcher, name)
                 for name in ("load_project_config", "_tenant_board_root_from_config_or_plan", "authority_unit_installs")}
        saved_services = {name: getattr(board_services, name) for name in ("authority_unit_installs", "board_is_supervised")}
        for index, case in enumerate((repatriation, repatriation_refusals, revert, cutover_trees, runtime_switch,
                                           runtime_pane_link, runtime_rollback, root_director, cutover_whole)):
            host = Host(base / str(index))
            accounts(host)
            try:
                case(host)
            finally:
                for name, value in saved.items():
                    setattr(launcher, name, value)
                for name, value in saved_services.items():
                    setattr(board_services, name, value)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"role_cutover_writes_test: privileged child ran {CHECKS} checks")
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
