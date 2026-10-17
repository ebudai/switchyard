#!/usr/bin/env python3
"""SYRD-608: root's writes, handoffs and reads of a tenant's generated files follow no link.

As root on a tenant's behalf, `owner_files.write_owner_file`, the atomic JSON
writers, `ensure_owner_file`, `_chown_project_file`, `read_owner_json` and the
presentation state lock all reach the file by descriptor from the owner's home
(`scripts/tenant_paths.py`). The SYRD-606 acceptance, case by case:

* a link planted at the file, or at any directory between the home and it,
  never steers root: a foreign file keeps its contents, owner, mode, ACL and
  xattrs byte for byte; the call refuses (nonzero, the path and the reason)
  where following was the only way through; and nothing is made in the
  foreign tree;
* a directory swapped for a link between root looking and root opening is
  refused the same way;
* a path outside the owner's home -- an absolute `layout`, a `..` -- is
  refused rather than normalised in;
* ordinary files are made the owner's, with the mode they should have, and a
  second identical write changes nothing.

The unprivileged half pins that a caller writing its own files is unchanged.
The privileged half runs in a user namespace with the invoking account's
subuid range mapped, so root, ownership, ACLs and xattrs are the kernel's; the
owner account and its home are stand-ins for the lookups only.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402,I001
from scripts import presentation_state_document as presentation  # noqa: E402
from scripts import tenant_paths  # noqa: E402

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


def snapshot(top: Path) -> list[tuple]:
    """Contents, owner, mode, link target, ACL and xattrs of everything under `top`, links not followed."""
    seen = []
    for item in [top, *sorted(top.rglob("*"))]:
        status = item.lstat()
        if stat.S_ISLNK(status.st_mode):
            seen.append((str(item), status.st_uid, status.st_gid, "link", os.readlink(item)))
            continue
        xattrs = sorted((name, os.getxattr(item, name)) for name in os.listxattr(item) if name.startswith("user."))
        acl = run(["getfacl", "-cpn", str(item)]).stdout
        body = item.read_bytes() if stat.S_ISREG(status.st_mode) else b""
        seen.append((str(item), status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode), acl, xattrs, body))
    return seen


def config_for(home: Path, *, owner: str = OWNER) -> launcher.ProjectConfig:
    provision = home / "Projects" / "p" / ".switchyard" / "provision"
    return launcher.ProjectConfig(
        project="p", project_name="P", ticket_prefix="P", layout=provision / "layout.json",
        session_dir=home / ".local" / "state" / "switchyard" / "sessions" / "p",
        board_url="http://127.0.0.1:1", board_socket="/run/p.sock", upstream_report_url="",
        upstream_report_token_file="", run_as_user=owner, pane_launcher=None, repository=None,
        control_repository=None, worktree_base=None, worktree_remote="origin", worktree_branch="main", roles=[])


def refused(call, *needles: str) -> str:
    try:
        call()
    except SystemExit as stop:
        text = str(stop)
        assert all(needle in text for needle in needles), (text, needles)
        return text
    raise AssertionError(f"not refused; expected {needles}")


# --------------------------------------------------------------------------
# the unprivileged half: a caller's own files are written as before
# --------------------------------------------------------------------------


def test_the_callers_own_files_are_written_by_path_as_before() -> None:
    me = launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="syrd608-own.") as raw:
        target = Path(raw) / "own.json"
        launcher.write_owner_file(me, target, "mine\n")
        check(target.read_text() == "mine\n", "the caller's own file is written where it is named")
        check(not launcher.root_writes_for(me) and not launcher.root_writes_for(""),
              "only root, writing for somebody else, writes by descriptor")
        target.write_text('{"a": 1}\n')
        check(launcher.read_owner_json(me, target) == {"a": 1}, "and read as before")
        check(launcher.read_owner_bytes(me, Path(raw) / "missing") is None, "a missing file reads as None")
        ran = []
        refused(lambda: launcher._chown_project_file(owner_user="", path=target, runner=ran.append), "there is no owner")
        check(ran == [], "a project file with no owner is refused before anything runs")


# --------------------------------------------------------------------------
# the privileged half
# --------------------------------------------------------------------------


class Host:
    """A tenant home, a foreign tree it must never reach, and root's view of the owner."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home" / "tenant"
        self.provision = self.home / "Projects" / "p" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        for item in [self.home, *self.home.rglob("*")]:
            os.lchown(item, OWNER_UID, OWNER_UID)
        for item in (base, base / "home"):
            os.chmod(item, 0o755)
        os.chmod(self.home, 0o750)
        self.foreign = base / "foreign"
        (self.foreign / "inner").mkdir(parents=True)
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "secret.json").write_text('{"secret": "root only"}\n', encoding="utf-8")
        (self.foreign / "inner" / "file").write_text("deeper\n", encoding="utf-8")
        for item in [self.foreign, *self.foreign.rglob("*")]:
            os.lchown(item, FOREIGN_UID if item.name != "secret.json" else 0, FOREIGN_UID if item.name != "secret.json" else 0)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1600:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)
        self.config = config_for(self.home)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched

    def plant(self, at: Path, target: Path) -> Path:
        """Put a tenant-owned link to `target` at `at`, setting aside whatever was there; returns the aside."""
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


def owner_lookups(home: Path):
    """The owner account is a stand-in for the lookups; everything else is real."""
    real = pwd.getpwnam

    def getpwnam(name: str):
        if name == OWNER:
            return pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(home), "/bin/sh"))
        return real(name)

    pwd.getpwnam = getpwnam
    launcher.current_user_name = lambda: "root"


def ancestors(host: Host, leaf: Path) -> list[Path]:
    """Every directory between the home and `leaf`, the home excluded."""
    parts = leaf.relative_to(host.home).parts[:-1]
    return [host.home.joinpath(*parts[: index + 1]) for index in range(len(parts))]


def writes(host: Host) -> None:
    home, target = host.home, host.provision / "p-board.service"
    launcher.write_owner_file(OWNER, target, "[Unit]\n")
    status = target.lstat()
    check((status.st_uid, status.st_gid, stat.S_IMODE(status.st_mode)) == (OWNER_UID, OWNER_UID, 0o600)
          and target.read_text() == "[Unit]\n", f"a new file is the owner's, 0600: {status}")
    os.chmod(target, 0o640)
    inode = target.lstat().st_ino
    launcher.write_owner_file(OWNER, target, "[Unit]\n")
    check(target.lstat().st_ino == inode, "an identical second write changes nothing")
    launcher.write_owner_file(OWNER, target, "[Unit]\nDescription=b\n")
    status = target.lstat()
    check(status.st_ino != inode and stat.S_IMODE(status.st_mode) == 0o640 and status.st_uid == OWNER_UID,
          "a changed file is replaced whole, keeping its mode and owner")
    fresh = home / "Projects" / "p" / "made" / "deeper" / "file"
    launcher.write_owner_file(OWNER, fresh, "x")
    check(all(item.lstat().st_uid == OWNER_UID and stat.S_IMODE(item.lstat().st_mode) == 0o755
              for item in (fresh.parent, fresh.parent.parent)), "a missing directory on the way is made the owner's, 0755")
    shutil.rmtree(home / "Projects" / "p" / "made")

    # A link at the file itself is replaced as a directory entry; its target is never opened.
    aside = host.plant(target, host.foreign / "file")
    launcher.write_owner_file(OWNER, target, "[Unit]\n")
    check(not target.is_symlink() and target.lstat().st_uid == OWNER_UID and host.foreign_unchanged(),
          "a link at the file is replaced, never written through")
    target.unlink()
    os.rename(aside, target)
    # A link at every directory on the way is refused, and nothing lands in the foreign tree.
    for directory in ancestors(host, target):
        aside = host.plant(directory, host.foreign)
        text = refused(lambda: launcher.write_owner_file(OWNER, target, "[Unit]\n"), "symbolic link", str(directory))
        check(host.foreign_unchanged() and not (host.foreign / target.name).exists(),
              f"a link at {directory.relative_to(home)}: refused, the foreign tree untouched: {text}")
        host.unplant(directory, aside)
    # An owner that is not an account on this host gets nothing written for it.
    refused(lambda: launcher.write_owner_file("nobody608", target, "x"), "not an account")
    # Outside the home, however it is spelled, is refused rather than normalised in.
    refused(lambda: launcher.write_owner_file(OWNER, host.foreign / "file", "x"), "not inside")
    refused(lambda: launcher.write_owner_file(OWNER, Path(f"{home}/../../foreign/file"), "x"), "normalised")
    check(host.foreign_unchanged(), "a path outside the home, or with `..`, changes nothing outside it")

    # A directory swapped for a link after root looked and before it opened.
    real_open = tenant_paths._openat

    def swapped(name, flags, dir_fd):
        if name == "provision" and not host.provision.is_symlink():
            host.plant(host.provision, host.foreign)
        return real_open(name, flags, dir_fd)

    tenant_paths._openat = swapped
    try:
        text = refused(lambda: launcher.write_owner_file(OWNER, target, "[Unit]\nswapped\n"), "provision")
    finally:
        tenant_paths._openat = real_open
    check(host.foreign_unchanged(), f"a directory swapped for a link mid-walk: refused, untouched: {text}")
    host.unplant(host.provision, host.provision.with_name("provision.aside"))


def json_writers(host: Host) -> None:
    config_path = host.provision / "p.json"
    launcher._write_json_atomic(config_path, {"project": "p"}, owner_user=OWNER)
    status = config_path.lstat()
    check(status.st_uid == OWNER_UID and json.loads(config_path.read_text()) == {"project": "p"},
          "the atomic writer publishes the owner's document")
    for directory in ancestors(host, config_path):
        aside = host.plant(directory, host.foreign)
        refused(lambda: launcher._write_json_atomic(config_path, {"project": "x"}, owner_user=OWNER), "symbolic link")
        check(host.foreign_unchanged(), f"the atomic writer refuses a link at {directory.relative_to(host.home)}")
        host.unplant(directory, aside)
    state_dir = host.home / ".local" / "state" / "p"
    state_dir.mkdir(parents=True)
    for item in (host.home / ".local", host.home / ".local" / "state", state_dir):
        os.lchown(item, OWNER_UID, OWNER_UID)
    os.chmod(state_dir, 0o755)
    launcher._write_private_json_atomic(state_dir / "state.json", {"a": 1}, owner_user=OWNER)
    status, parent = (state_dir / "state.json").lstat(), state_dir.lstat()
    check(stat.S_IMODE(status.st_mode) == 0o600 and status.st_uid == OWNER_UID and stat.S_IMODE(parent.st_mode) == 0o700,
          "the private writer: the owner's 0600 file in a 0700 directory, set on descriptors")
    aside = host.plant(state_dir, host.foreign)
    refused(lambda: launcher._write_private_json_atomic(state_dir / "state.json", {"a": 2}, owner_user=OWNER),
            "symbolic link")
    check(host.foreign_unchanged() and stat.S_IMODE(host.foreign.lstat().st_mode) == 0o700,
          "the private writer never chmods or writes through a linked directory")
    host.unplant(state_dir, aside)


def captured(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True)


def handoffs(host: Host) -> None:
    target = host.provision / "root-made.sql"
    target.write_text("select 1;\n")
    launcher.ensure_owner_file(host.config, target, runner=captured)
    check(target.lstat().st_uid == OWNER_UID, "an ordinary file is handed to the owner")
    aside = host.plant(target, host.foreign / "file")
    refused(lambda: launcher.ensure_owner_file(host.config, target, runner=captured), "symbolic link")
    check(host.foreign_unchanged(), "a link at the file is refused, its target never chowned")
    host.unplant(target, aside)
    for directory in ancestors(host, target):
        aside = host.plant(directory, host.foreign)
        refused(lambda: launcher.ensure_owner_file(host.config, target, runner=captured), "symbolic link")
        check(host.foreign_unchanged(), f"the handoff refuses a link at {directory.relative_to(host.home)}")
        host.unplant(directory, aside)
    # An absolute `layout` outside the home: the tenant's config naming a host file.
    layout = replace(host.config, layout=host.foreign / "file")
    refused(lambda: launcher.ensure_owner_file(layout, layout.layout, runner=captured), "not inside")
    refused(lambda: launcher._chown_project_file(owner_user=OWNER, path=host.foreign / "file", runner=captured),
            "failed to assign")
    check(host.foreign_unchanged(), "a handoff of a path outside the home changes nothing")


def reads(host: Host) -> None:
    document = host.provision / "p.project.json"
    launcher.write_owner_file(OWNER, document, '{"project": {"roles": []}}\n')
    check(launcher.read_owner_json(OWNER, document) == {"project": {"roles": []}}, "an ordinary document is read")
    aside = host.plant(document, host.foreign / "secret.json")
    refused(lambda: launcher.read_owner_json(OWNER, document), "symbolic link")
    host.unplant(document, aside)
    aside = host.plant(host.provision, host.foreign)
    refused(lambda: launcher.read_owner_json(OWNER, host.provision / "secret.json"), "symbolic link")
    host.unplant(host.provision, aside)
    check(host.foreign_unchanged(), "root never reads a root-only file through the tenant's link")
    refused(lambda: launcher.read_owner_json(OWNER, host.provision), "not a regular file")


def presentation_lock(host: Host) -> None:
    state_path = host.home / ".local" / "state" / "switchyard" / "projects" / "p" / "presentation.json"
    runner = captured  # every handoff really runs, the layout directory's `chown -R` (SYRD-610) included

    with presentation._locked_state(state_path, config=host.config, runner=runner):
        lock = state_path.with_name("presentation.json.lock").lstat()
        check((lock.st_uid, stat.S_IMODE(lock.st_mode)) == (OWNER_UID, 0o600)
              and all(item.lstat().st_uid == OWNER_UID for item in (state_path.parent, state_path.parent.parent)),
              "the lock and the directories made for it are the owner's")
    lock_path = state_path.with_name("presentation.json.lock")
    lock_path.unlink()
    descriptor = tenant_paths.open_file(str(host.home), str(lock_path), uid=OWNER_UID, gid=OWNER_UID, mode=0o600)
    os.close(descriptor)
    made = lock_path.lstat()
    check((made.st_uid, made.st_gid, stat.S_IMODE(made.st_mode)) == (OWNER_UID, OWNER_UID, 0o600),
          "open_file makes the file it creates the owner's, 0600, on its descriptor")
    lock_path.unlink()
    lock_path.symlink_to(host.foreign / "file")
    os.lchown(lock_path, OWNER_UID, OWNER_UID)

    def lock_it():
        with presentation._locked_state(state_path, config=host.config, runner=runner):
            pass

    refused(lock_it, "symbolic link")
    check(host.foreign_unchanged(), "a planted lock link: refused, its target neither created, opened nor chowned")
    lock_path.unlink()
    missing = host.foreign / "made-by-root"
    lock_path.symlink_to(missing)
    refused(lock_it, "symbolic link")
    check(not missing.exists(), "a dangling lock link never makes its target")
    lock_path.unlink()
    # The state document itself: never read through a link, never written through a linked directory.
    state_path.symlink_to(host.foreign / "secret.json")
    os.lchown(state_path, OWNER_UID, OWNER_UID)
    refused(lambda: presentation._read_state(state_path, config=host.config, config_path=host.provision / "p.json"),
            "symbolic link")
    state_path.unlink()
    aside = host.plant(state_path.parent, host.foreign)
    refused(lambda: presentation._write_state(host.config, state_path, {"slots": {}}, runner=captured), "symbolic link")
    host.unplant(state_path.parent, aside)
    check(host.foreign_unchanged() and not (host.foreign / "presentation.json").exists(),
          "the presentation state is neither read nor written through a link")


def through_a_linked_provision(host: Host, label: str, call, *needles: str, seed: dict[str, str] | None = None) -> str:
    """Run one real caller with the tenant's provision directory a link into the foreign tree."""
    for name, text in (seed or {}).items():
        (host.foreign / name).write_text(text, encoding="utf-8")
    host.untouched = snapshot(host.foreign)
    aside = host.plant(host.provision, host.foreign)
    try:
        text = refused(call, *needles)
    finally:
        host.unplant(host.provision, aside)
    check(host.foreign_unchanged(), f"{label}: refused through a linked provision directory, the foreign tree untouched")
    return text


def callers(host: Host) -> None:
    """The writers' real callers, each with the tenant's provision directory a link to a foreign tree."""
    from types import SimpleNamespace

    from scripts import (generated_layout_upgrade, model_validation, project_role_add, project_vcs_close_role,
                         upstream_report, workflow_launcher)

    config, provision = host.config, host.provision
    config_path = provision / "p.json"
    document = {"project": "p", "layout": "layout.json", "roles": [{"role": "ops"}]}
    launcher.write_owner_file(OWNER, config_path, json.dumps(document))
    launcher.load_project_config = lambda project, path: config
    record = lambda: model_validation.record_role_model(  # noqa: E731
        config, config_path=config_path, role_name="ops", model="m", runner=captured)
    record()
    check(json.loads(config_path.read_text())["roles"][0]["model"] == "m" and config_path.lstat().st_uid == OWNER_UID,
          "record_role_model writes the owner's config")
    through_a_linked_provision(host, "record_role_model", record, "symbolic link", seed={"p.json": json.dumps(document)})
    aside = host.plant(config_path, host.foreign / "secret.json")
    refused(record, "symbolic link")
    check(config_path.is_symlink() and host.foreign_unchanged(), "a linked config is never read into a new one")
    host.unplant(config_path, aside)
    real_read = launcher.read_owner_json

    def read_then_swap(owner, path):
        document = real_read(owner, path)
        host.plant(host.provision, host.foreign)  # between root's read and its write
        return document

    launcher.read_owner_json = read_then_swap
    try:
        refused(record, "symbolic link")
    finally:
        launcher.read_owner_json = real_read
        host.unplant(host.provision, host.provision.with_name("provision.aside"))
    check(host.foreign_unchanged(), "record_role_model: a directory swapped after the read refuses the write")

    plan = SimpleNamespace(project="p", board_unit="p-board.service")
    launcher.render_board_unit = lambda plan: "[Unit]\n"
    launcher.render_add_role_sql = lambda plan, role: "select 1;\n"
    launcher.render_vcs_close_role_sql = lambda plan: "select 2;\n"
    for module, function in ((project_role_add, "_write_updated_project_plan_artifacts"),
                             (project_vcs_close_role, "_write_vcs_close_role_artifacts")):
        write = lambda: getattr(module, function)(  # noqa: E731
            plan, role_name="ops", provision_dir=provision, runner=captured, config=config)
        made = write()
        check(all(path.lstat().st_uid == OWNER_UID and not path.is_symlink() for path in made),
              f"{function} writes the owner's plan, unit and SQL")
        through_a_linked_provision(host, function, write, "symbolic link")
        for path in made:
            aside = host.plant(path, host.foreign / "file")
            write()
            check(not path.is_symlink() and path.lstat().st_uid == OWNER_UID and host.foreign_unchanged(),
                  f"{function}: a link at {path.name} is replaced, its target never written")
            aside.unlink()

    artifact = provision.parent / "p.project.json"
    launcher.write_owner_file(OWNER, artifact, json.dumps({"project": {"roles": []}}))
    design = lambda: project_role_add._update_project_design_artifact_for_role(  # noqa: E731
        config, config_path, role_name="ops", cli="claude")
    design()
    check(json.loads(artifact.read_text())["project"]["roles"] == ["ops"], "the design artifact gains the role")
    aside = host.plant(artifact, host.foreign / "secret.json")
    refused(design, "symbolic link")
    host.unplant(artifact, aside)
    check(host.foreign_unchanged(), "a linked design artifact is neither read nor rewritten")

    upstream = lambda: upstream_report.record_upstream_report_link(  # noqa: E731
        config, config_path=config_path, upstream_report_url="http://127.0.0.1:2", upstream_report_token_file="/t",
        print_func=lambda line: None)
    aside = host.plant(config_path, host.foreign / "secret.json")
    _config, problems = upstream()
    check(problems and "symbolic link" in problems[0] and config_path.is_symlink() and host.foreign_unchanged(),
          f"the upstream link record refuses to read a linked config, and writes nothing: {problems}")
    host.unplant(config_path, aside)
    upstream()
    check(json.loads(config_path.read_text())["upstream_report_url"] == "http://127.0.0.1:2", "and records an ordinary one")

    aside = host.plant(config_path, host.foreign / "secret.json")
    refused(lambda: workflow_launcher.assign_projection_owner(config, config_path, runner=captured), "symbolic link")
    refused(lambda: project_role_add._write_added_role_config(config, config_path=config_path, role_name="perf", cli="codex",
                                                              detached=True, slot=None, runner=captured), "symbolic link")
    host.unplant(config_path, aside)
    check(host.foreign_unchanged(), "the projection handoff and add-role never read a linked config")

    outside = dict(document, layout=str(host.foreign / "file"))
    launcher.write_owner_file(OWNER, config_path, json.dumps(outside))
    refused(lambda: workflow_launcher.assign_projection_owner(config, config_path, runner=captured), "not inside")
    check(host.foreign_unchanged(), "an absolute layout outside the home is never handed to the tenant")
    launcher.write_owner_file(OWNER, config_path, json.dumps(document))

    for name, value in (("_is_generated_project_layout_template", lambda config, config_path: True),
                        ("_generated_project_durable_session_dir", lambda config: None),
                        ("_tenant_board_root_from_config_or_plan", lambda config, config_path: None),
                        ("_new_project_layout_payload", lambda count: {"layout": "current"}),
                        ("_known_generated_project_layout_payloads", lambda count: [{"layout": "legacy"}])):
        setattr(launcher, name, value)
    layout = replace(config, layout=provision / "layout.json")
    upgrade = lambda: generated_layout_upgrade.upgrade_generated_project_config(  # noqa: E731
        layout, config_path=config_path, runner=captured)
    launcher.write_owner_file(OWNER, layout.layout, json.dumps({"layout": "legacy"}))
    upgrade()
    check(json.loads(layout.layout.read_text()) == {"layout": "current"} and layout.layout.lstat().st_uid == OWNER_UID,
          "the generated layout upgrade writes the owner's layout")
    launcher.write_owner_file(OWNER, layout.layout, json.dumps({"layout": "legacy"}))
    through_a_linked_provision(host, "upgrade_generated_project_config", upgrade, "symbolic link",
                               seed={"layout.json": json.dumps({"layout": "legacy"})})


def more_callers(host: Host) -> None:
    """The upgrade's and reload's config rewriters: a linked config is never read into a rewrite."""
    from types import SimpleNamespace

    from scripts import legacy_presentation, live_role_runtime, project_desktop

    role = launcher.RoleConfig(
        role="ops", slot=0, detached=False, tmux_session="s", target="t", workdir="/", cli=["claude"], model="a",
        model_arg="--model", effort="", yolo=False, extra_args=[], resume_mode="", resume_flag="", resume_subcommand="",
        fresh_session_per_ticket=False, live_commands=["claude"], env={})
    config = replace(host.config, roles=[role])
    config_path = host.provision / "p.json"
    document = json.dumps({"project": "p", "roles": [{"role": "ops", "model": "a"}]})
    launcher.role_process_runner_for = lambda config, role, runner: (lambda argv, **kwargs: SimpleNamespace(returncode=0))
    launcher.live_model_for_role = lambda role, session_dir, runner: "b"
    launcher.live_cli_for_role = lambda role, runner: ["claude"]
    launcher.legacy_presentation_migration = lambda config, config_path: SimpleNamespace(
        refusal="", needed=True, section={"slots": 1}, describe=lambda: "a presentation section", gui_user="")
    launcher.prepare_project_desktop = lambda config, runner: config
    rewriters = {
        "sync_reload_config_to_live_sessions": (
            lambda: live_role_runtime.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=captured),
            lambda: json.loads(config_path.read_text())["roles"][0]["model"] == "b"),
        "migrate_legacy_presentation": (
            lambda: legacy_presentation.migrate_legacy_presentation(config, config_path=config_path, print_func=lambda line: None),
            lambda: json.loads(config_path.read_text())["presentation"] == {"slots": 1}),
        "configure_project_desktop": (
            lambda: project_desktop.configure_project_desktop(config, config_path=config_path, policy_path=Path("headless"),
                                                              runner=captured),
            lambda: json.loads(config_path.read_text())["desktop_access"]["mode"] == "headless"),
    }
    for name, (call, done) in rewriters.items():
        launcher.write_owner_file(OWNER, config_path, document)
        launcher.load_project_config = lambda project, path: config
        call()
        check(done() and config_path.lstat().st_uid == OWNER_UID, f"{name} rewrites the owner's config")
        aside = host.plant(config_path, host.foreign / "secret.json")
        refused(call, "symbolic link")
        check(config_path.is_symlink() and host.foreign_unchanged(), f"{name}: a linked config is never read into a rewrite")
        host.unplant(config_path, aside)


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd608.") as raw:
        base = Path(raw)
        for index, case in enumerate((writes, json_writers, handoffs, reads, presentation_lock, callers, more_callers)):
            host = Host(base / str(index))
            owner_lookups(host.home)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"owner_file_writes_test: privileged child ran {CHECKS} checks")
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
    print(f"owner_file_writes_test: {unprivileged} unprivileged checks ok")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
