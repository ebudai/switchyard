#!/usr/bin/env python3
"""Root reads a tenant's generated documents without following anything.

Found during SYRD-227 review: the legacy-ownership repair opened every path
with O_NOFOLLOW, and the reads that ran BEFORE it did not. A symlink planted at
the tenant's `plan.json` was followed by `Path.is_file()`, by the plan parse and
by the divergence read, so a root upgrade either parsed whatever document the
link named or died with a raw `JSONDecodeError` traceback (SYRD-228).

Shaped like the SYRD-227 suite, and deliberately so: the same mefp-shaped legacy
tenant, the same user namespace, the same real files. What differs is where the
tampering is -- at `plan.json` and at the generated configuration themselves,
rather than at the artifacts the repair chowns.
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
#: Stands in for a file root can read and the tenant cannot. Its contents must
#: never reach a diagnostic.
SECRET = "root-only-secret-syrd228\n"


def _root_file(path: Path, text: str, mode: int = 0o644) -> None:
    path.write_text(text, encoding="utf-8")
    os.chown(path, 0, 0)
    path.chmod(mode)


class Tenant:
    """A legacy tenant whose generated documents are the tenant's to write."""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.account = pwd.getpwnam(owner)
        self.home = Path(self.account.pw_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix=".syrd228-plan.", dir=self.home))
        self.tmp.chmod(0o755)
        self.provision = self.tmp / "checkout" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        repository = self.tmp / "checkout"
        plan = build_plan(project=PROJECT, project_name="Porter", owner_user=owner,
                          owner_home=self.home, source_repo=ROOT)
        write_artifacts(plan, self.provision, enable_owner_linger=False)
        self.config_path = launcher.write_new_project_launcher_artifacts(
            plan, self.provision, repository=repository, print_func=lambda _t: None
        )
        self.plan_path = self.provision / "plan.json"
        # As an older release left it: created by root, which is the legacy
        # shape SYRD-227 repairs and the one this must keep repairing.
        for path in [self.provision, *self.provision.iterdir()]:
            os.chown(path, 0, 0, follow_symlinks=False)
        self.secret = self.tmp / "root-only.txt"
        _root_file(self.secret, SECRET, mode=0o600)
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

    def snapshot(self) -> dict[str, tuple]:
        seen: dict[str, tuple] = {}
        for path in sorted([self.provision, *self.provision.iterdir(), self.secret]):
            info = path.lstat()
            body = b""
            if path.is_symlink():
                body = os.readlink(path).encode()
            elif path.is_file():
                body = path.read_bytes()
            seen[str(path)] = (info.st_uid, info.st_mode, body)
        return seen

    def replace_plan_with(self, maker) -> None:
        """Whatever the tenant put there instead of its own plan."""
        self.plan_path.unlink()
        maker(self.plan_path)

    def close(self) -> None:
        for name in ("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", launcher.SWITCHYARD_REGISTRY_DIR_ENV):
            os.environ.pop(name, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _refused(t: Tenant, expected: str) -> None:
    """The refusal names the path, changes nothing, and is not a traceback."""
    before = t.snapshot()
    outcome = t.refresh()
    assert not outcome.changed, outcome.message
    assert str(t.plan_path) in outcome.message, outcome.message
    assert expected in outcome.message, (expected, outcome.message)
    assert "Nothing was changed" in outcome.message, outcome.message
    assert "Traceback" not in outcome.message, outcome.message
    assert SECRET.strip() not in outcome.message, outcome.message
    assert t.snapshot() == before, "a refused read changed the provision directory"
    assert not (t.privileged / PROJECT).exists(), "a refused read staged root's baseline"


def a_plan_symlink_is_refused_before_anything_is_read(t: Tenant) -> None:
    """The reported defect: root followed the link and parsed what it found."""
    t.replace_plan_with(lambda path: path.symlink_to(t.secret))
    _refused(t, "is a symlink")
    # And the file it pointed at is untouched: not read into a message, not
    # rewritten, not re-owned.
    assert t.secret.read_text(encoding="utf-8") == SECRET
    assert t.secret.lstat().st_uid == 0


def a_plan_symlink_to_valid_json_is_refused_too(t: Tenant) -> None:
    """A link that parses cleanly is the dangerous one: it would be believed."""
    elsewhere = t.tmp / "somebody-elses-plan.json"
    _root_file(elsewhere, json.dumps({"project": PROJECT, "owner_user": "root"}))
    t.replace_plan_with(lambda path: path.symlink_to(elsewhere))
    _refused(t, "is a symlink")
    assert json.loads(elsewhere.read_text(encoding="utf-8"))["owner_user"] == "root"


def a_hard_linked_plan_is_refused(t: Tenant) -> None:
    """A second name for somebody else's file passes an owner check on its own."""
    t.replace_plan_with(lambda path: os.link(t.secret, path))
    _refused(t, "links")
    assert t.secret.read_text(encoding="utf-8") == SECRET


def a_plan_anybody_can_rewrite_is_refused(t: Tenant) -> None:
    t.plan_path.chmod(0o666)
    _refused(t, "which anybody in its group or beyond can write")


def a_plan_owned_by_a_third_party_is_refused(t: Tenant) -> None:
    """A repaired tenant's directory, holding a document that is nobody's here.

    The directory belongs to the owner, as it does after SYRD-227's repair, so
    what may be in it is the owner's or root's. A third account's file is
    neither, and its owner can rewrite the contents at will.
    """
    os.chown(t.provision, t.account.pw_uid, t.account.pw_gid)
    os.chown(t.plan_path, 4242, 4242)
    _refused(t, "is owned by uid 4242")


def a_plan_root_placed_for_its_owner_is_read(t: Tenant) -> None:
    """What provisioning leaves: root's directory, the owner's own document.

    Only root can write into a root-owned directory, so a file in one was put
    there by root -- and root writing the tenant's documents and giving them to
    the owner is exactly how a tenant is provisioned.
    """
    os.chown(t.plan_path, t.account.pw_uid, t.account.pw_gid)
    outcome = t.refresh()
    assert outcome.changed, outcome.message
    assert "refusing to read" not in outcome.message, outcome.message


def a_plan_that_is_not_json_is_refused_without_quoting_it(t: Tenant) -> None:
    """The raw JSONDecodeError traceback, replaced by a refusal."""
    t.plan_path.write_text("not a plan at all: " + SECRET, encoding="utf-8")
    os.chown(t.plan_path, 0, 0)
    _refused(t, "it is not JSON")
    assert "not a plan at all" not in t.refresh().message


def a_plan_that_is_not_text_is_refused_without_quoting_it(t: Tenant) -> None:
    t.plan_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    os.chown(t.plan_path, 0, 0)
    _refused(t, "it is not UTF-8 text")


def an_ancestor_symlink_is_refused(t: Tenant) -> None:
    """A link several levels up owns the whole subtree below it."""
    real = t.tmp / "moved-provision"
    t.provision.rename(real)
    t.provision.symlink_to(real)
    outcome = t.refresh()
    assert not outcome.changed, outcome.message
    assert "symlink" in outcome.message, outcome.message
    assert "Traceback" not in outcome.message, outcome.message


def a_symlinked_configuration_is_refused_by_the_root_read(t: Tenant) -> None:
    """The other generated document root opens, with the same policy."""
    target = t.tmp / "somebody-elses-config.json"
    _root_file(target, json.dumps({"project": PROJECT, "roles": []}))
    t.config_path.unlink()
    t.config_path.symlink_to(target)
    raised = ""
    try:
        launcher.load_project_config(PROJECT, t.config_path)
    except SystemExit as exc:
        raised = str(exc)
    assert str(t.config_path) in raised, raised
    assert "is a symlink" in raised, raised
    assert "Nothing was changed" in raised, raised
    assert "Traceback" not in raised, raised
    assert json.loads(target.read_text(encoding="utf-8"))["roles"] == []


def a_configuration_that_is_not_json_is_refused_without_quoting_it(t: Tenant) -> None:
    t.config_path.write_text(SECRET, encoding="utf-8")
    os.chown(t.config_path, 0, 0)
    raised = ""
    try:
        launcher.load_project_config(PROJECT, t.config_path)
    except SystemExit as exc:
        raised = str(exc)
    assert "it is not JSON" in raised, raised
    assert SECRET.strip() not in raised, raised


def the_ordinary_legacy_plan_is_still_read_and_repaired(t: Tenant) -> None:
    """SYRD-227's bounded repair, unchanged, on an untampered tenant."""
    outcome = t.refresh()
    assert outcome.changed, outcome.message
    assert "returned" in outcome.message and t.owner in outcome.message, outcome.message
    uid = t.account.pw_uid
    assert t.provision.lstat().st_uid == uid, "the provision directory is still root's"
    assert t.plan_path.lstat().st_uid == uid, "the plan was not returned to its owner"
    baseline = t.privileged / PROJECT / "plan.json"
    assert baseline.is_file(), sorted(p.name for p in (t.privileged / PROJECT).iterdir())
    assert json.loads(baseline.read_text(encoding="utf-8"))["owner_user"] == t.owner


def a_dry_run_reads_the_same_way_and_writes_nothing(t: Tenant) -> None:
    t.replace_plan_with(lambda path: path.symlink_to(t.secret))
    before = t.snapshot()
    outcome = t.refresh(dry_run=True)
    assert not outcome.changed, outcome.message
    assert "is a symlink" in outcome.message, outcome.message
    assert t.snapshot() == before


CASES = (
    a_plan_symlink_is_refused_before_anything_is_read,
    a_plan_symlink_to_valid_json_is_refused_too,
    a_hard_linked_plan_is_refused,
    a_plan_anybody_can_rewrite_is_refused,
    a_plan_owned_by_a_third_party_is_refused,
    a_plan_root_placed_for_its_owner_is_read,
    a_plan_that_is_not_json_is_refused_without_quoting_it,
    a_plan_that_is_not_text_is_refused_without_quoting_it,
    an_ancestor_symlink_is_refused,
    a_symlinked_configuration_is_refused_by_the_root_read,
    a_configuration_that_is_not_json_is_refused_without_quoting_it,
    the_ordinary_legacy_plan_is_still_read_and_repaired,
    a_dry_run_reads_the_same_way_and_writes_nothing,
)


def root_child(owner: str) -> None:
    assert os.geteuid() == 0 and pwd.getpwnam(owner).pw_uid != 0
    for case in CASES:
        tenant = Tenant(owner)
        try:
            case(tenant)
        finally:
            tenant.close()
        print(f"  ok   {case.__name__.replace('_', ' ')}")


def main() -> int:
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    print(f"privileged_plan_read_no_follow_test: {len(CASES)} cases ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--root-child":
        root_child(sys.argv[2])
    else:
        raise SystemExit(main())
