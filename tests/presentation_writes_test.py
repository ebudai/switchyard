#!/usr/bin/env python3
"""SYRD-610: root's presentation writes in an owner's state directory follow no link.

As root on a tenant's behalf the launcher materialised the window layout by
path (`write_text`, after `mkdir`), read the tenant's layout template by path,
handed the layout directory over with `chown -R`, and wrote the Konsole
window-title default (`konsolerc`) beside the layout by path, then `chown`ed
it. Each followed a link the tenant left at the file or at any directory above
it. Now:

* `materialize_layout` reads the template without following a link and writes
  the layout through `owner_writes.write_owner_file` -- by descriptor from the
  owner's root, the owner's from the start, 0644 as before;
* the layout directory is handed over by `tenant_paths.py chown-tree`, rooted
  at the owner's root: never through a link, links inside changed as links;
* `konsolerc` is written, as root, for the account the launch names as the
  layout's, by descriptor from that account's root, with the layout's mode read
  from a descriptor opened on it there; a layout reached through a link gets no
  default, and nothing of it is read first.

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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import live_boundary  # noqa: E402

live_boundary.isolate()  # SYRD-627: no live home, pane store or runtime directory, here or in a child

from scripts import team_launcher as launcher  # noqa: E402,I001
from scripts import gui_window_launch  # noqa: E402

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


class Host:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home" / "tenant"
        self.state = self.home / ".local" / "state" / "switchyard" / "projects" / "p"
        self.state.mkdir(parents=True)
        self.template = self.home / "p-layout.json"
        self.template.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": [{"Command": ""}]}))
        for item in [self.home, *self.home.rglob("*")]:
            os.lchown(item, OWNER_UID, OWNER_UID)
        for item in (base, base / "home"):
            os.chmod(item, 0o755)
        self.foreign = base / "foreign"
        self.foreign.mkdir()
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "secret.json").write_text(json.dumps({"Widgets": [{"Command": "root only"}]}))
        for item in [self.foreign, *self.foreign.rglob("*")]:
            os.lchown(item, FOREIGN_UID if item.name != "secret.json" else 0, FOREIGN_UID if item.name != "secret.json" else 0)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1600:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)
        self.config = launcher.ProjectConfig(
            project="p", project_name="P", ticket_prefix="P", layout=self.template, session_dir=self.home / "s",
            board_url="http://127.0.0.1:1", board_socket="/run/p.sock", upstream_report_url="",
            upstream_report_token_file="", run_as_user=OWNER, pane_launcher=None, repository=None,
            control_repository=None, worktree_base=None, worktree_remote="origin", worktree_branch="main", roles=[])

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

    def materialize(self, output: Path, config=None):
        return launcher.materialize_layout(config or self.config, config_path=self.home / "p.json", mode="start",
                                           script_path=Path("/bin/true"), output_path=output)


def owner_lookups(home: Path) -> None:
    real_name, real_uid = pwd.getpwnam, pwd.getpwuid
    stand_in = pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(home), "/bin/sh"))
    pwd.getpwnam = lambda name: stand_in if name == OWNER else real_name(name)
    pwd.getpwuid = lambda uid: stand_in if uid == OWNER_UID else real_uid(uid)
    launcher.current_user_name = lambda: "root"


def layout(host: Host) -> None:
    output = host.state / "new" / "p-team-layout.json"
    host.materialize(output)
    status = output.lstat()
    check((status.st_uid, stat.S_IMODE(status.st_mode)) == (OWNER_UID, 0o644) and output.parent.lstat().st_uid == OWNER_UID
          and json.loads(output.read_text())["Orientation"] == "Horizontal",
          "the layout is the owner's from the start, 0644, its new directory the owner's too")
    inode = status.st_ino
    host.materialize(output)
    check(output.lstat().st_ino == inode, "an identical second layout changes nothing")
    aside = host.plant(output, host.foreign / "file")
    host.materialize(output)
    check(not output.is_symlink() and host.foreign_unchanged(), "a link at the layout is replaced, never written through")
    os.unlink(aside)
    for directory in (output.parent, host.state):
        aside = host.plant(directory, host.foreign)
        refused(lambda: host.materialize(output), "symbolic link")
        host.unplant(directory, aside)
        check(host.foreign_unchanged() and not (host.foreign / output.name).exists(),
              f"a link at {directory.name}: refused, nothing written outside")
    refused(lambda: host.materialize(host.foreign / "layout.json"), "not inside")
    check(host.foreign_unchanged(), "a layout outside the owner's roots is refused")
    # The template: read without following a link, so nothing root can read is carried into the owner's layout.
    aside = host.plant(host.template, host.foreign / "secret.json")
    refused(lambda: host.materialize(output), "symbolic link")
    host.unplant(host.template, aside)
    check("root only" not in output.read_text(), "a linked template is never read into the owner's layout")


def handover(host: Host) -> None:
    output = host.state / "p-team-layout.json"
    (host.state / "root-made").write_text("x")
    os.lchown(host.state / "root-made", 0, 0)
    (host.state / "theirs").symlink_to(host.foreign / "file")
    launcher.ensure_layout_output_owner(host.config, output, runner=captured)
    check((host.state / "root-made").lstat().st_uid == OWNER_UID and (host.state / "theirs").lstat().st_uid == OWNER_UID
          and host.foreign_unchanged(), "the layout directory is handed over, a link in it changed as a link only")
    for directory in (host.state, host.state.parent):
        aside = host.plant(directory, host.foreign)
        refused(lambda: launcher.ensure_layout_output_owner(host.config, output, runner=captured), "symbolic link")
        host.unplant(directory, aside)
        check(host.foreign_unchanged(), f"a link at {directory.name}: the handover is refused, nothing outside chowned")


def konsole_defaults(host: Host) -> None:
    model = host.state / "p-team-layout.json"
    model.write_text("{}")
    os.lchown(model, OWNER_UID, OWNER_UID)
    os.chmod(model, 0o640)
    target = host.state / gui_window_launch.KONSOLE_DEFAULTS_NAME
    write = lambda **kw: gui_window_launch.write_konsole_config_defaults(host.state, model=kw.get("at", model),  # noqa: E731
                                                                       owner=kw.get("owner", OWNER))
    refusal = write()
    status = target.lstat()
    check(refusal == "" and (status.st_uid, stat.S_IMODE(status.st_mode)) == (OWNER_UID, 0o640)
          and target.read_text() == gui_window_launch.KONSOLE_WINDOW_TITLE_DEFAULTS,
          f"the default is the named account's, with the layout's mode read from its descriptor: {refusal!r}")
    aside = host.plant(target, host.foreign / "file")
    refusal = write()
    check(refusal == "" and not target.is_symlink() and host.foreign_unchanged(),
          "a link at the default is replaced, never written through")
    os.unlink(aside)
    # The layout's directory a link to another the owner owns, holding a layout the owner owns: refused before
    # anything of the layout is read -- no pathname lstat or stat of it at all (audit of d471f228).
    decoy = host.base / "decoy"
    decoy.mkdir()
    (decoy / model.name).write_text("{}")
    for item in (decoy, decoy / model.name):
        os.lchown(item, OWNER_UID, OWNER_UID)
    looked: list[str] = []
    real_lstat, real_stat = Path.lstat, Path.stat
    Path.lstat = lambda self, *a, **k: looked.append(str(self)) or real_lstat(self, *a, **k)
    Path.stat = lambda self, *a, **k: looked.append(str(self)) or real_stat(self, *a, **k)
    aside = host.plant(host.state, decoy)
    try:
        refusal = write(at=host.state / model.name)
    finally:
        Path.lstat, Path.stat = real_lstat, real_stat
        host.unplant(host.state, aside)
    check("symbolic link" in refusal and not (decoy / target.name).exists() and model.name not in " ".join(looked),
          f"a link at the layout's directory: refused before the layout is looked at, nothing written: {refusal!r} {looked}")
    elsewhere = host.home / "elsewhere.json"
    elsewhere.write_text("{}")
    os.lchown(elsewhere, OWNER_UID, OWNER_UID)
    for odd, why in ((host.foreign / "file", "a layout that is a link"),
                     (elsewhere, "a layout that is a link, even to a file the owner owns")):
        aside = host.plant(model, odd)
        target.unlink(missing_ok=True)
        refusal = write()
        check(refusal and not target.exists() and host.foreign_unchanged(), f"{why}: no default is written: {refusal!r}")
        host.unplant(model, aside)
    target.unlink(missing_ok=True)
    check(write(owner="") == "" and not target.exists(), "with no account named by the launch, no default is written")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd610.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        for index, case in enumerate((layout, handover, konsole_defaults)):
            host = Host(base / str(index))
            owner_lookups(host.home)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"presentation_writes_test: privileged child ran {CHECKS} checks")
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
