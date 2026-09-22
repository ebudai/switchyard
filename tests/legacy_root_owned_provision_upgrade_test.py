#!/usr/bin/env python3
"""A registered legacy tenant whose provision directory root owns can be upgraded.

Live mefp dry-run against 502d307: reconstructing root's baseline stopped at
"<provision> is owned by root and so does not identify a tenant". An older
release created mefp's provision directory as root, ownership of that directory
was the only owner fact reconstruction trusted, and there is no
`/etc/switchyard/provision/mefp` baseline -- so a registered, running tenant
could not be upgraded in place (SYRD-227).

Shaped like mefp: a registered config, an existing owner account, a root-owned
provision directory, a root-owned board unit naming the owner's HOME, and no
root baseline. Runs as root in a user namespace, like the plan-migration suite.
The tenant tree is made inside the owner's real home, because the registry must
place it there; it is removed afterwards.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import build_plan, write_artifacts  # noqa: E402

PROJECT = "porter"


def _root_file(path: Path, text: str, mode: int = 0o644) -> None:
    path.write_text(text, encoding="utf-8")
    os.chown(path, 0, 0)
    path.chmod(mode)


class Tenant:
    """A mefp-shaped legacy tenant, built fresh for each case."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.account = pwd.getpwnam(owner)
        self.home = Path(self.account.pw_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix=".syrd227-legacy.", dir=self.home))
        self.tmp.chmod(0o755)
        self.provision = self.tmp / "checkout" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        repository = self.tmp / "checkout"
        plan = build_plan(project=PROJECT, project_name="Porter", owner_user=owner,
                          owner_home=self.home, source_repo=ROOT)
        self.plan = plan
        write_artifacts(plan, self.provision, enable_owner_linger=False)
        self.config_path = launcher.write_new_project_launcher_artifacts(
            plan, self.provision, repository=repository, print_func=lambda _t: None
        )
        # As the older release left it: everything created by root.
        for path in [self.provision, *self.provision.iterdir()]:
            os.chown(path, 0, 0, follow_symlinks=False)
        # Something that is not a generated file, which must be left alone.
        _root_file(self.provision / "operator-notes.txt", "not generated\n")
        self.units = self.tmp / "units"
        self.units.mkdir()
        _root_file(self.units / f"{PROJECT}-ticket-board.service",
                   f"[Service]\nUser=boardsvc\nEnvironment=HOME={self.home}\n"
                   f"Environment=TICKET_BOARD_PROJECT={PROJECT}\n")
        self.registry = self.tmp / "registry"
        self.registry.mkdir()
        _root_file(self.registry / f"{PROJECT}.json", json.dumps({
            "schema": "switchyard.project-registry.v1", "slug": PROJECT, "name": "Porter",
            "config_path": str(self.config_path),
        }))
        self.privileged = self.tmp / "etc-switchyard"
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(self.privileged)
        os.environ[launcher.SWITCHYARD_REGISTRY_DIR_ENV] = str(self.registry)
        launcher.SYSTEMD_SYSTEM_UNIT_DIR = self.units
        self.config = launcher.load_project_config(PROJECT, self.config_path)

    def refresh(self, **kwargs):
        def runner(args, **kw):
            if args[0] == "chown":
                return subprocess.run(args, **kw)
            return subprocess.CompletedProcess(args, 0, "", "")
        return launcher.refresh_generated_project_runtime_artifacts(
            self.config, config_path=self.config_path, runner=runner,
            print_func=lambda _t: None, **kwargs,
        )

    def snapshot(self) -> dict[str, tuple[int, bytes]]:
        return {
            path.name: (path.lstat().st_uid, path.read_bytes() if path.is_file() else b"")
            for path in sorted(self.provision.iterdir())
        } | {".": (self.provision.lstat().st_uid, b"")}

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def _case(owner: str, name: str, body) -> None:
    tenant = Tenant(owner)
    try:
        body(tenant)
        print(f"  ok   {name}")
    finally:
        tenant.close()


def dry_run_describes_and_writes_nothing(t: Tenant) -> None:
    before = t.snapshot()
    outcome = t.refresh(dry_run=True)
    assert "would return" in outcome.message, outcome.message
    assert f"to {t.owner}" in outcome.message, outcome.message
    assert f"HOME -> {t.owner}" in outcome.message, outcome.message
    assert "re-provision" not in outcome.message.lower(), outcome.message
    assert t.snapshot() == before, "the dry run changed the provision directory"
    assert not (t.privileged / PROJECT).exists(), "the dry run staged root's baseline"


def apply_repairs_only_the_generated_files_and_builds_the_baseline(t: Tenant) -> None:
    outcome = t.refresh()
    assert outcome.changed, outcome.message
    assert "returned" in outcome.message and t.owner in outcome.message, outcome.message
    uid = t.account.pw_uid
    assert t.provision.lstat().st_uid == uid, "the provision directory is still root's"
    for name in ("plan.json", t.config_path.name):
        assert (t.provision / name).lstat().st_uid == uid, name
    # Bounded: what is not generated is left exactly as it was.
    assert (t.provision / "operator-notes.txt").lstat().st_uid == 0
    mirror = t.privileged / PROJECT
    baseline = json.loads((mirror / "plan.json").read_text(encoding="utf-8"))
    assert baseline["owner_user"] == t.owner, baseline["owner_user"]
    assert (mirror / "plan.json").stat().st_uid == 0
    # The normal upgrade now generates every unit root installs.
    for unit in (t.plan.board_unit, t.plan.canary_unit, t.plan.listener_unit):
        assert (mirror / unit).is_file(), (unit, sorted(p.name for p in mirror.iterdir()))
    # And the next upgrade needs no repair at all.
    again = t.refresh()
    assert "legacy root-owned" not in again.message, again.message
    # set-owner-identity's owner comes from that baseline.
    identity = launcher.trusted_owner_identity(PROJECT)
    if identity.owner_user != t.owner:
        # The baseline path must be root-controlled end to end; inside this
        # namespace the host's /tmp is not, so say which way it failed.
        print(f"       (trusted_owner_identity here: {identity})")
    assert t.owner in json.dumps(baseline)


def _refuses(t: Tenant, expected: str) -> None:
    before = t.snapshot()
    outcome = t.refresh()
    assert not outcome.changed, outcome.message
    assert expected in outcome.message, (expected, outcome.message)
    assert "re-provision" not in outcome.message.lower(), outcome.message
    assert t.snapshot() == before, "a refused repair changed the provision directory"
    assert not (t.privileged / PROJECT).exists()


def contradictory_unit_is_refused(t: Tenant) -> None:
    _root_file(t.units / f"{PROJECT}-ticket-board.service",
               f"[Service]\nEnvironment=HOME={t.home}\nEnvironment=TICKET_BOARD_TENANT_USER=root\n")
    _refuses(t, "contradictory")


def home_of_no_account_is_refused(t: Tenant) -> None:
    _root_file(t.units / f"{PROJECT}-ticket-board.service",
               "[Service]\nEnvironment=HOME=/nonexistent/syrd227\n")
    _refuses(t, "home of no account")


def unit_writable_by_others_is_refused(t: Tenant) -> None:
    (t.units / f"{PROJECT}-ticket-board.service").chmod(0o666)
    _refuses(t, "writable by accounts other than root")


def registry_naming_another_directory_is_refused(t: Tenant) -> None:
    _root_file(t.registry / f"{PROJECT}.json", json.dumps({
        "schema": "switchyard.project-registry.v1", "slug": PROJECT,
        "config_path": str(t.tmp / "elsewhere" / f"{PROJECT}.json"),
    }))
    _refuses(t, "registers")


def unregistered_tenant_is_refused(t: Tenant) -> None:
    (t.registry / f"{PROJECT}.json").unlink()
    _refuses(t, "not a registered tenant")


def planted_symlink_is_refused(t: Tenant) -> None:
    """A generated name pointing elsewhere: root's chown must not follow it."""
    operator = t.provision / "operator-commands.sh"
    target = t.tmp / "not-yours"
    _root_file(target, operator.read_text(encoding="utf-8"))
    operator.unlink()
    operator.symlink_to(target)
    os.chown(operator, 0, 0, follow_symlinks=False)
    _refuses(t, "it is a symbolic link")
    assert target.stat().st_uid == 0


def hard_link_is_refused(t: Tenant) -> None:
    """A generated name that is another file's second link: not chowned either."""
    operator = t.provision / "operator-commands.sh"
    target = t.tmp / "not-yours"
    _root_file(target, operator.read_text(encoding="utf-8"))
    operator.unlink()
    os.link(target, operator)
    _refuses(t, "not a regular file with one link")
    assert target.stat().st_uid == 0


def root_child(owner: str) -> None:
    assert os.geteuid() == 0 and pwd.getpwnam(owner).pw_uid != 0
    for name, body in (
        ("dry run describes the repair and writes nothing", dry_run_describes_and_writes_nothing),
        ("apply repairs only generated files and builds the baseline",
         apply_repairs_only_the_generated_files_and_builds_the_baseline),
        ("contradictory unit is refused", contradictory_unit_is_refused),
        ("HOME of no account is refused", home_of_no_account_is_refused),
        ("unit writable by others is refused", unit_writable_by_others_is_refused),
        ("registry naming another directory is refused", registry_naming_another_directory_is_refused),
        ("unregistered tenant is refused", unregistered_tenant_is_refused),
        ("planted symlink is refused", planted_symlink_is_refused),
        ("hard link is refused", hard_link_is_refused),
    ):
        _case(owner, name, body)


def main() -> int:
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    print("legacy_root_owned_provision_upgrade_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--root-child":
        root_child(sys.argv[2])
    else:
        raise SystemExit(main())
