#!/usr/bin/env python3
"""SYRD-611: root's `switchyard new` and onboarding writes in a tenant's project directory follow no link.

As root on a tenant's behalf, `switchyard new` and `upgrade` wrote the
tenant's project files by path: the onboarding files and docs (`mkdir`,
`install -d -o`, `write_text`, then a chown), `PROJECT_DESIGN.md` only if
`exists()` said it was missing -- a dangling link is "missing" -- the design
artifact, `chown -R` of `.switchyard`, root's records in the provision
directory (`desktop-policy.json`, the packet record, the launcher config and
layout), Router's rewrites (`lstat`, write, `lchown`, `chmod`), and the
fallback `mkdir` after the owner's project directories were made. Each
followed a link the tenant left at the file or at any directory above it.
Now each goes by descriptor from the owner's root (`owner_writes`,
`tenant_paths`): the owner's files are the owner's before they are in place,
root's records are root's, a link at a name is replaced as an entry or (for a
write-if-missing) counts as present, and a link above is refused.

Each case proves a foreign tree unchanged byte for byte (contents, owner,
mode, ACL, xattrs). The privileged half runs in a user namespace with the
invoking account's subuid range mapped; the owner account is a stand-in for
the lookups only.
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
from scripts import owner_writes, root_packet, router_onboarding, tenant_paths  # noqa: E402

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


def runner(argv, **kwargs):
    """Runs what root runs -- the descriptor helper -- and answers the source's own `git rev-parse`."""
    if "rev-parse" in argv:
        return subprocess.CompletedProcess(argv, 0, "c0ffee\n", "")
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


def owned(path: Path) -> tuple[int, int]:
    status = path.lstat()
    return status.st_uid, stat.S_IMODE(status.st_mode)


class Host:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.home = base / "home" / "tenant"
        self.project = self.home / "proj"
        self.switchyard = self.project / ".switchyard"
        self.provision = self.switchyard / "provision"
        self.provision.mkdir(parents=True)
        for item in [self.home, *self.home.rglob("*")]:
            os.lchown(item, OWNER_UID, OWNER_UID)
        for item in (base, base / "home"):
            os.chmod(item, 0o755)
        self.source = base / "src"
        (self.source / "docs" / "onboarding").mkdir(parents=True)
        for name in launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            (self.source / "docs" / "onboarding" / name).write_text(f"# {name}\n", encoding="utf-8")
        self.foreign = base / "foreign"
        self.foreign.mkdir()
        (self.foreign / "file").write_text("not the tenant's\n", encoding="utf-8")
        (self.foreign / "secret.json").write_text(json.dumps({"files": {"stolen": {"sha256": "root only"}}}))
        for item in [self.foreign, *self.foreign.rglob("*")]:
            ids = 0 if item.name == "secret.json" else FOREIGN_UID
            os.lchown(item, ids, ids)
            os.chmod(item, 0o700 if item.is_dir() else 0o600)
        run(["setfacl", "-m", "u:1600:r--", str(self.foreign / "file")])
        os.setxattr(self.foreign / "file", "user.origin", b"foreign")
        self.untouched = snapshot(self.foreign)

    def foreign_unchanged(self) -> bool:
        return snapshot(self.foreign) == self.untouched and sorted(p.name for p in self.foreign.iterdir()) == ["file", "secret.json"]

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


def owner_lookups(home: Path) -> None:
    real_name, real_uid = pwd.getpwnam, pwd.getpwuid
    stand_in = pwd.struct_passwd((OWNER, "x", OWNER_UID, OWNER_UID, "", str(home), "/bin/sh"))
    pwd.getpwnam = lambda name: stand_in if name == OWNER else real_name(name)
    pwd.getpwuid = lambda uid: stand_in if uid == OWNER_UID else real_uid(uid)
    launcher.current_user_name = lambda: "root"


def onboarding_files(host: Host) -> None:
    director = host.switchyard / launcher.SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME
    designer = host.switchyard / launcher.SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME

    def write() -> None:
        launcher._write_switchyard_onboarding_files(
            project_name="P", slug="p", owner_user=OWNER, project_dir=host.project,
            artifact_path=host.switchyard / "p.project.json", design_document=host.project / "PROJECT_DESIGN.md",
            director_onboarding=director, include_designer=True)

    aside = host.plant(director, host.foreign / "file")
    write()
    check(owned(director) == (OWNER_UID, 0o644) and owned(designer) == (OWNER_UID, 0o644) and not director.is_symlink()
          and host.foreign_unchanged(), "the onboarding files are the owner's from the start; a link at one is replaced")
    aside.unlink(missing_ok=True)
    aside = host.plant(host.switchyard, host.foreign)
    refused(write, "symbolic link")
    host.unplant(host.switchyard, aside)
    check(host.foreign_unchanged(), "a link at .switchyard: refused, nothing written through it")


def initial_artifact(host: Host) -> None:
    design = host.project / launcher.SWITCHYARD_DESIGN_FILE_NAME
    artifact = host.switchyard / "p.project.json"

    def write() -> None:
        launcher._write_initial_switchyard_project_artifact(
            project_name="P", slug="p", owner_user=OWNER, project_dir=host.project, artifact_path=artifact,
            design_document=design, owner_shell="/bin/sh")

    design.symlink_to(host.base / "dangling")
    os.lchown(design, OWNER_UID, OWNER_UID)
    write()
    check(design.is_symlink() and not (host.base / "dangling").exists() and owned(artifact) == (OWNER_UID, 0o600),
          "a dangling link at the design document counts as present: nothing is created through it; the artifact "
          "is the owner's")
    design.unlink()
    write()
    check(owned(design) == (OWNER_UID, 0o644) and "Design in progress." in design.read_text(), "a missing design "
          "document is started, the owner's")
    design.write_text("kept")
    write()
    check(design.read_text() == "kept", "an existing design document is never rewritten")
    # One that appears after root looked -- while its own copy is being staged -- is left as it is, not replaced.
    design.unlink()
    shipped = tenant_paths.Root._staging

    @contextlib.contextmanager
    def racing(root, parent, display, verb):
        with shipped(root, parent, display, verb) as stage:
            if display == str(design):
                design.write_text("raced")
            yield stage

    tenant_paths.Root._staging = racing
    try:
        write()
    finally:
        tenant_paths.Root._staging = shipped
    check(design.read_text() == "raced", "a design document that arrives after the look is never replaced")
    aside = host.plant(host.switchyard, host.foreign)
    refused(write, "symbolic link")
    host.unplant(host.switchyard, aside)
    check(host.foreign_unchanged(), "a link at .switchyard: the artifact is refused, nothing written through it")


def handover(host: Host) -> None:
    (host.switchyard / "root-made").write_text("x")
    os.lchown(host.switchyard / "root-made", 0, 0)
    (host.switchyard / "theirs").symlink_to(host.foreign / "file")
    launcher._chown_switchyard_project_files(owner_user=OWNER, project_dir=host.project, runner=runner)
    check((host.switchyard / "root-made").lstat().st_uid == OWNER_UID and (host.switchyard / "theirs").lstat().st_uid == OWNER_UID
          and host.foreign_unchanged(), ".switchyard is handed over, a link in it changed as a link only")
    aside = host.plant(host.switchyard, host.foreign)
    refused(lambda: launcher._chown_switchyard_project_files(owner_user=OWNER, project_dir=host.project, runner=runner),
            "failed to assign")
    host.unplant(host.switchyard, aside)
    check(host.foreign_unchanged(), "a link at .switchyard: the handover is refused, nothing outside chowned")


def onboarding_docs(host: Host) -> None:
    target = host.project / "docs" / "onboarding"
    names = launcher.SWITCHYARD_ONBOARDING_DOC_NAMES
    said: list[str] = []
    aside = host.plant(host.project / "docs", host.foreign)
    refused(lambda: launcher._install_switchyard_onboarding_docs(source_repo=host.source, project_dir=host.project,
                                                                owner_user=OWNER, runner=runner, print_func=said.append),
            "failed to create onboarding docs directory")
    host.unplant(host.project / "docs", aside)
    check(host.foreign_unchanged(), "a link at docs/: the docs directory is refused, nothing made or written outside")
    target.mkdir(parents=True)
    for item in (target.parent, target):
        os.lchown(item, OWNER_UID, OWNER_UID)
    (target / names[0]).symlink_to(host.base / "dangling")
    launcher._install_switchyard_onboarding_docs(source_repo=host.source, project_dir=host.project, owner_user=OWNER,
                                                 runner=runner, print_func=said.append)
    check(not (host.base / "dangling").exists() and (target / names[0]).is_symlink()
          and all(owned(target / name) == (OWNER_UID, 0o644) for name in names[1:])
          and any(names[0] in line and "skipped existing" in line for line in said),
          f"the docs are the owner's; a dangling link at one is present, never written through: {said}")
    # Upgrade: a doc missing is installed, the owner's; an installed copy that is a link is never read.
    (target / names[1]).unlink()
    aside = host.plant(target / names[2], host.foreign / "secret.json")
    said.clear()
    launcher.upgrade_switchyard_onboarding_docs(source_repo=host.source, project_dir=host.project, owner_user=OWNER,
                                                dry_run=False, runner=runner, print_func=said.append)
    check(owned(target / names[1]) == (OWNER_UID, 0o644) and (target / names[2]).is_symlink() and host.foreign_unchanged()
          and any(names[2] in line and "cannot read installed copy" in line for line in said),
          f"upgrade installs a missing doc as the owner's and reads no linked copy: {said}")
    host.unplant(target / names[2], aside)


def provision_records(host: Host) -> None:
    rendered = {"operator-commands.sh": b"#!/bin/bash\n", "plan.json": b"{}\n"}
    root_copy = host.base / "root-copy"
    aside = host.plant(host.provision / "plan.json", host.foreign / "file")
    (host.provision / root_packet.RECORD_NAME).symlink_to(host.foreign / "secret.json")
    problem = root_packet.publish_packet_record(host.provision, rendered, root_copy=root_copy, owner_user=OWNER)
    record = json.loads((host.provision / root_packet.RECORD_NAME).read_text())
    check(problem == "" and owned(host.provision / "operator-commands.sh") == (0, 0o755)
          and owned(host.provision / "plan.json") == (0, 0o644) and owned(host.provision / root_packet.RECORD_NAME)[0] == 0
          and "stolen" not in record["files"] and host.foreign_unchanged(),
          f"the packet record is root's own; links at its names are replaced, the old record never read: {problem!r}")
    aside.unlink(missing_ok=True)
    aside = host.plant(host.provision, host.foreign)
    problem = root_packet.publish_packet_record(host.provision, rendered, root_copy=root_copy, owner_user=OWNER)
    host.unplant(host.provision, aside)
    check("symbolic link" in problem and host.foreign_unchanged(), f"a link at provision/: reported, nothing written: {problem!r}")
    # Root's desktop policy record, made with the provision directory if it is missing (as `new` makes it).
    shutil.rmtree(host.provision)
    policy = host.provision / "desktop-policy.json"
    aside = host.plant(host.switchyard, host.foreign)
    refused(lambda: owner_writes.write_root_record(OWNER, policy, b"{}\n", mode=0o600, parents_mode=0o755), "symbolic link")
    host.unplant(host.switchyard, aside)
    owner_writes.write_root_record(OWNER, policy, b"{}\n", mode=0o600, parents_mode=0o755)
    check(owned(host.provision) == (0, 0o755) and owned(policy) == (0, 0o600) and host.foreign_unchanged(),
          "the desktop policy is root's record, its new directory root's, as the by-path writes made them")


def launcher_artifacts(host: Host) -> None:
    owner_writes.trust_project_root(OWNER, host.project)
    plan = launcher.build_plan(project="p", project_name="P", owner_user=OWNER, owner_home=host.home, source_repo=ROOT)
    write = lambda: launcher.write_new_project_launcher_artifacts(  # noqa: E731
        plan, host.provision, repository=host.project, print_func=lambda _line: None)
    layout = host.provision / "p-konsole-layout.json"
    aside = host.plant(layout, host.foreign / "file")
    config = write()
    check(owned(config) == (0, 0o644) and owned(layout) == (0, 0o644) and not layout.is_symlink() and host.foreign_unchanged()
          and json.loads(config.read_text())["project"] == "p",
          "the launcher config and layout are root's, as before; a link at one is replaced")
    aside.unlink(missing_ok=True)
    aside = host.plant(host.provision, host.foreign)
    refused(write, "symbolic link")
    host.unplant(host.provision, aside)
    check(host.foreign_unchanged(), "a link at provision/: the launcher artifacts are refused, nothing written through it")


def router_rewrites(host: Host) -> None:
    mine = host.switchyard / "p.project.json"
    mine.write_text(json.dumps({"project": {}}))
    os.lchown(mine, OWNER_UID, OWNER_UID)
    os.chmod(mine, 0o640)
    roots = host.provision / "p.json"
    roots.write_text("{}")
    os.chmod(roots, 0o600)
    for path, ids in ((mine, (OWNER_UID, 0o640)), (roots, (0, 0o600))):
        router_onboarding._rewrite(path, {"rewritten": True}, owner_user=OWNER)
        check(owned(path) == ids and json.loads(path.read_text()) == {"rewritten": True},
              f"{path.name}: rewritten keeping the owner and mode it had")
    for odd, why in ((host.foreign / "file", "a link at the file"), (None, "a link above it")):
        at = mine if odd is not None else host.switchyard
        aside = host.plant(at, odd if odd is not None else host.foreign)
        refused(lambda: router_onboarding._rewrite(host.switchyard / mine.name, {"x": 1}, owner_user=OWNER), "refusing")
        host.unplant(at, aside)
        check(host.foreign_unchanged(), f"{why}: refused, nothing read, written or chowned through it")


def project_directory_fallback(host: Host) -> None:
    project = host.home / "Projects" / "new"
    (host.home / "Projects").symlink_to(host.foreign)
    os.lchown(host.home / "Projects", OWNER_UID, OWNER_UID)

    def nothing(argv, **kwargs):
        stdout = "yes\n" if "show-user" in argv else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    refused(lambda: launcher._ensure_owner_user_and_project_dir(OWNER, project, runner=nothing, owner_home=host.home),
            "failed to create project directory")
    check(host.foreign_unchanged(), "the fallback after the helper refuses a link swapped in: nothing made through it")


def privileged_cases() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd611.") as raw:
        base = Path(raw)
        base.chmod(0o755)
        cases = (onboarding_files, initial_artifact, handover, onboarding_docs, provision_records, launcher_artifacts,
                 router_rewrites, project_directory_fallback)
        for index, case in enumerate(cases):
            host = Host(base / str(index))
            owner_lookups(host.home)
            case(host)
    return CHECKS


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_cases()
        print(f"new_project_writes_test: privileged child ran {CHECKS} checks")
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
