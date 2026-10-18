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
root, under a recorder of every pathname lookup in the tenant's homes (which
must see none), and proves a foreign tree unchanged byte for byte -- contents,
owner, mode, ACL and xattrs. The privileged half runs in a user namespace with
the invoking account's subuid range mapped; the owner and role accounts are
stand-ins for the lookups, named by number so the descriptor helper, in its own
process, resolves them too.
"""

from __future__ import annotations

import contextlib
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
from scripts import board_services, role_identity_cutover, role_runtime, runtime_projection  # noqa: E402
from scripts import stopped_project_model_repair, tenant_release_report  # noqa: E402

OWNER, OWNER_UID, ROLE, ROLE_UID, FOREIGN_UID = "1500", 1500, "1600", 1600, 1700
CHECKS = 0


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
    """Every pathname lookup of something under `top` -- by `pathlib` or `os` -- while this holds.

    Descriptor-relative lookups (a name with `dir_fd`) are what root is meant to use, and are not recorded. A child
    root forks to act as an account records into its own copy, which this never sees.
    """
    seen: list[str] = []
    prefix = str(top).rstrip("/") + "/"

    def note(target, **kwargs):
        text = os.fsdecode(target) if isinstance(target, (str, bytes, os.PathLike)) else ""
        if kwargs.get("dir_fd") is None and text.startswith(prefix):
            seen.append(text)

    patches = []
    for holder, name in ((Path, "exists"), (Path, "is_dir"), (Path, "is_file"), (Path, "is_symlink"), (Path, "stat"),
                         (Path, "lstat"), (Path, "resolve"), (Path, "read_text"), (Path, "read_bytes"), (os, "stat"),
                         (os, "lstat"), (os, "access"), (os, "readlink"), (os.path, "exists"), (os.path, "lexists"),
                         (os.path, "isdir"), (os.path, "isfile"), (os.path, "islink"), (os.path, "realpath")):
        real = getattr(holder, name)

        def spy(*args, __real=real, **kwargs):
            note(args[0] if args else kwargs.get("path", ""), **kwargs)
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
        (board / "current").unlink()
        (board / "current").symlink_to("releases/new")
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


def runtime_switch(host: Host) -> None:
    """Root's repair of a stopped role's runtime: the projection, its journal, and putting it back."""
    provision = host.home / "prov"
    config_path = host.make(provision / "p.json", json.dumps({"project": "p", "roles": [{"role": "ops", "cli": ["claude"]}]},
                                                             indent=2, sort_keys=True) + "\n", OWNER_UID)
    original = config_path.read_text()
    config = project_config(host, [role_config(host.home / "wt", cli="claude", run_as_user="")])
    launcher.load_project_config = lambda project, path: "reloaded"
    journal_path = host.home / ".local" / "state" / "switchyard" / "projects" / "p" / "role-runtime-ops.journal.json"
    journal = role_runtime.RuntimeJournal(project="p", role="ops", config_path=str(config_path), owner_user=OWNER,
                                          previous_config_bytes=runtime_projection.projection_text(config_path, OWNER) or "")

    def switch():
        stopped_project_model_repair.make_owner_dirs(journal_path.parent, config_path, owner_user=OWNER)
        journal.write(journal_path)
        return runtime_projection.write_runtime_projection(config, config_path=config_path, role_name="ops",
                                                           runtime="codex", runner=runner, journal=journal,
                                                           journal_path=journal_path)

    with lookups(host.homes) as seen:
        reloaded = switch()
    check(reloaded == "reloaded" and json.loads(config_path.read_text())["roles"][0]["cli"] == ["codex"]
          and owned(config_path) == (OWNER_UID, 0o644) and owned(journal_path) == (OWNER_UID, 0o600)
          and owned(journal_path.parent) == (OWNER_UID, 0o700) and seen == []
          and json.loads(journal_path.read_text())["previous_projection"] == {str(config_path): original},
          f"the projection written and journaled as the owner's, nothing looked up by path: {seen}")
    with lookups(host.homes) as seen:
        runtime_projection.restore_projection(journal)
    check(config_path.read_text() == original and owned(config_path)[0] == OWNER_UID and seen == [],
          f"and put back from the journal by descriptor: {seen}")
    for at, target in ((config_path, host.foreign / "secret.json"), (provision, host.foreign)):
        aside = host.plant(at, target)
        with lookups(host.homes) as seen:
            refused(switch, "symbolic link")
        host.unplant(at, aside)
        check(host.foreign_unchanged() and config_path.read_text() == original and seen == [],
              f"a link at {at.name}: nothing read into the journal or projection through it: {seen}")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd612.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        saved = {name: getattr(launcher, name)
                 for name in ("load_project_config", "_tenant_board_root_from_config_or_plan", "authority_unit_installs")}
        saved_services = {name: getattr(board_services, name) for name in ("authority_unit_installs", "board_is_supervised")}
        for index, case in enumerate((repatriation, repatriation_refusals, revert, cutover_trees, runtime_switch)):
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
