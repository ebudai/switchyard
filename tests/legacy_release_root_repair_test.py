#!/usr/bin/env python3
"""A legacy tenant's release root is repaired, or refused, before a deploy is offered.

Live on mefp at 1309ec8: the upgrade reported the release phase `ready` and
printed its deploy sequence; the operator stopped the listener and ran it; and
`ticket-board-service.sh deploy-restart`, running as the owner, stopped at
`mkdir: cannot create directory '.../releases/.tmp-...': Permission denied`,
because an older release had deployed as root and left `releases/` root's
(SYRD-231).

Runs as root in a user and mount namespace, like the other privileged suites.
The owner is a real account with a home outside /home; a scratch directory is
bound over that home for the run, so nothing on the host is written except the
scratch tree itself. The deploy boundary is the real one: the shipped
`ticket-board-service.sh`, sourced and run as the owner's uid.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import build_plan, write_artifacts  # noqa: E402

PROJECT = "relroot"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40


def _candidate_owner() -> pwd.struct_passwd:
    """An account whose home can be bound over without hiding this checkout."""
    for entry in sorted(pwd.getpwall(), key=lambda e: e.pw_uid):
        home = Path(entry.pw_dir)
        if not (0 < entry.pw_uid < 65536) or home == Path("/") or not home.is_dir():
            continue
        if home.is_relative_to("/home") or ROOT.is_relative_to(home):
            continue
        if Path(tempfile.gettempdir()).is_relative_to(home):
            continue
        return entry
    raise SystemExit("legacy_release_root_repair_test: no account with a bindable home")


class Tenant:
    """mefp's shape: a live board root whose release parent root made."""

    def __init__(self, owner: pwd.struct_passwd) -> None:
        self.owner = owner
        self.home = Path(owner.pw_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd231."))
        self.tmp.chmod(0o755)
        bound = self.tmp / "home"
        bound.mkdir()
        subprocess.run(["mount", "--bind", str(bound), str(self.home)], check=True)
        os.chown(self.home, owner.pw_uid, owner.pw_gid)
        self.home.chmod(0o750)
        self.privileged = self.tmp / "etc-switchyard"
        os.environ[launcher.PRIVILEGED_PROVISION_ROOT_ENV] = str(self.privileged)
        self.plan = build_plan(project=PROJECT, project_name="Relroot", owner_user=owner.pw_name,
                               owner_home=self.home, source_repo=ROOT)
        self.board_root = Path(self.plan.board_root)
        assert self.board_root == self.home / f"{PROJECT}-ticketboard-live", self.board_root
        # Root's baseline, as the upgrade stages it.
        rendered = self.tmp / "rendered"
        write_artifacts(self.plan, rendered, enable_owner_linger=False)
        launcher.ensure_privileged_provision_dir(self.baseline.parent)
        shutil.copyfile(rendered / "plan.json", self.baseline)
        self.baseline.chmod(0o600)
        # What older root-run deploys left: the board root, its release parent
        # and the unit hash all root's; a release, its link and the live state
        # beside them, which a repair must not touch.
        release = self.board_root / "releases" / OLD_SHA
        release.mkdir(parents=True)
        (release / ".pgu-deploy-sha").write_text(OLD_SHA + "\n", encoding="utf-8")
        release.chmod(0o755)
        (self.board_root / "current").symlink_to(release)
        (self.board_root / "system-unit.sha256").write_text("abc  unit\n", encoding="utf-8")
        (self.board_root / "canary.env").write_text("BOARD_PORT=1\n", encoding="utf-8")
        for name in ("assets", "tickets", "journal"):
            (self.board_root / name).mkdir()
            (self.board_root / name / "kept").write_text(name, encoding="utf-8")
        (self.board_root / "release-rollback.json").write_text("{}", encoding="utf-8")
        self.board_root.chmod(0o755)
        (self.board_root / "releases").chmod(0o755)
        self.config = types.SimpleNamespace(project=PROJECT, run_as_user=owner.pw_name)

    @property
    def baseline(self) -> Path:
        return launcher.privileged_baseline_plan_path(PROJECT)

    @property
    def record(self) -> Path:
        return launcher.release_root_repair_record_path(PROJECT)

    def prepare(self, **kwargs) -> tuple[list[str], str]:
        printed: list[str] = []
        problems = launcher.prepare_tenant_release_root(
            self.config, print_func=printed.append, **kwargs
        )
        return problems, "\n".join(printed)

    def snapshot(self) -> dict[str, tuple]:
        """Every entry under the board root, with who owns it and what it holds."""
        seen: dict[str, tuple] = {}
        for path in sorted([self.board_root, *self.board_root.rglob("*")]):
            info = path.lstat()
            body = path.read_bytes() if path.is_file() and not path.is_symlink() else (
                os.readlink(path) if path.is_symlink() else b"")
            seen[str(path.relative_to(self.home))] = (info.st_uid, info.st_mode, body)
        return seen

    def deploy_as_owner(self) -> subprocess.CompletedProcess[str]:
        """The owner-run export `deploy-restart` makes, through the shipped script."""
        source = self.tmp / "source"
        if not source.exists():
            source.mkdir()
            (source / ".switchyard-release.json").write_text(
                json.dumps({"commit": NEW_SHA}), encoding="utf-8")
            (source / "README").write_text("release\n", encoding="utf-8")
            for path in (source, *source.iterdir()):
                path.chmod(0o755 if path.is_dir() else 0o644)
        # The shipped script, where the owner can read it: this checkout sits
        # in a home the owner cannot enter.
        script = self.tmp / "scripts" / "ticket-board-service.sh"
        if not script.exists():
            script.parent.mkdir(mode=0o755)
            shutil.copyfile(ROOT / "scripts" / "ticket-board-service.sh", script)
            script.chmod(0o755)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "TICKET_BOARD_PROJECT": PROJECT,
            "TICKET_BOARD_OWNER_HOME": str(self.home),
            "TICKET_BOARD_COMMIT_GIT_DIR": str(self.tmp / "no-commit-store"),
            "SOURCE_REPO": str(source),
            "BOARD_CANARY_USER": self.owner.pw_name,
        }
        body = (
            f"source {script}; deploy_export || exit 1; "
            f"printf 'def  unit\\n' >\"$SYSTEM_UNIT_HASH_RECORD\""
        )
        return subprocess.run(
            ["setpriv", f"--reuid={self.owner.pw_uid}", f"--regid={self.owner.pw_gid}",
             "--clear-groups", "bash", "-c", body],
            env=env, capture_output=True, text=True, timeout=120,
        )

    def close(self) -> None:
        os.environ.pop(launcher.PRIVILEGED_PROVISION_ROOT_ENV, None)
        subprocess.run(["umount", str(self.home)], check=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _owned(t: Tenant, path: Path) -> bool:
    return path.lstat().st_uid == t.owner.pw_uid


def the_live_failure_reproduces_without_the_repair(t: Tenant) -> None:
    """Probe the fixture: unrepaired, the owner's deploy fails exactly as mefp's did."""
    result = t.deploy_as_owner()
    assert result.returncode != 0, result
    # The probe has to have reached the step mefp died at, not stopped early.
    assert "cannot create directory" in result.stderr, result.stderr
    assert f"releases/.tmp-{NEW_SHA}" in result.stderr, result.stderr
    assert "Permission denied" in result.stderr, result.stderr
    assert (t.board_root / "current").resolve().name == OLD_SHA


def dry_run_describes_the_repair_and_writes_nothing(t: Tenant) -> None:
    before = t.snapshot()
    problems, said = t.prepare(dry_run=True)
    assert problems == [], problems
    for name in ("", "/releases", "/system-unit.sha256"):
        assert f"would give {t.board_root}{name} to {t.owner.pw_name}" in said, said
    assert "gave " not in said.replace("would give", ""), said
    assert t.snapshot() == before, "the dry run changed the board root"
    assert not t.record.exists(), "the dry run journaled a repair it did not make"


def the_repair_lets_the_owner_publish_and_keeps_live_state(t: Tenant) -> None:
    before = t.snapshot()
    problems, said = t.prepare()
    assert problems == [], problems
    assert f"recorded that repair in {t.record}" in said, said
    for path in (t.board_root, t.board_root / "releases", t.board_root / "system-unit.sha256"):
        assert _owned(t, path), path
        assert path.lstat().st_mode & 0o200, path
    after = t.snapshot()
    repaired = {"relroot-ticketboard-live", "relroot-ticketboard-live/releases",
                "relroot-ticketboard-live/system-unit.sha256"}
    # Bounded: nothing but those three changed, and they changed only in owner and mode.
    for name, (uid, mode, body) in before.items():
        if name in repaired:
            assert after[name][2] == body, name
        else:
            assert after[name] == (uid, mode, body), (name, after[name], (uid, mode, body))
    assert (t.board_root / "releases" / OLD_SHA).lstat().st_uid == 0, "a release was re-owned"
    # Journaled, root-only, before-and-after for each entry.
    record = json.loads(t.record.read_text(encoding="utf-8"))
    assert record["schema"] == "switchyard.release-root-repair.v1", record
    assert record["owner"] == t.owner.pw_name, record
    assert sorted(e["path"] for e in record["entries"]) == sorted(
        str(t.home / name) for name in repaired), record
    assert all(e["uid_before"] == 0 for e in record["entries"]), record
    assert t.record.stat().st_uid == 0 and t.record.stat().st_mode & 0o077 == 0

    # The real boundary: the owner's own deploy now publishes and activates.
    result = t.deploy_as_owner()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (t.board_root / "current").resolve().name == NEW_SHA
    assert _owned(t, t.board_root / "releases" / NEW_SHA)
    assert (t.board_root / "system-unit.sha256").read_text() == "def  unit\n"
    # The rollback target and the live state are still there.
    assert (t.board_root / "releases" / OLD_SHA / ".pgu-deploy-sha").read_text() == OLD_SHA + "\n"
    for name in ("assets", "tickets", "journal"):
        assert (t.board_root / name / "kept").read_text() == name


def a_rerun_is_idempotent(t: Tenant) -> None:
    assert t.prepare()[0] == []
    journal = t.record.read_bytes()
    before = t.snapshot()
    problems, said = t.prepare()
    assert problems == [] and said == "", (problems, said)
    assert t.snapshot() == before
    assert t.record.read_bytes() == journal, "a rerun with nothing to do rewrote the journal"
    problems, said = t.prepare(dry_run=True)
    assert problems == [] and "would give" not in said, (problems, said)


def _refused(t: Tenant, expected: str) -> None:
    before = t.snapshot()
    problems, _said = t.prepare()
    assert problems and expected in problems[0], (expected, problems)
    assert t.snapshot() == before, "a refused repair changed the board root"
    assert not t.record.exists()


def a_symlinked_release_parent_is_refused(t: Tenant) -> None:
    elsewhere = t.tmp / "elsewhere"
    elsewhere.mkdir()
    releases = t.board_root / "releases"
    shutil.rmtree(releases)
    releases.symlink_to(elsewhere)
    _refused(t, "a symbolic link")
    assert elsewhere.stat().st_uid == 0, "root followed the link"


def a_symlinked_unit_hash_is_refused(t: Tenant) -> None:
    target = t.tmp / "not-the-tenants"
    target.write_text("x", encoding="utf-8")
    hashed = t.board_root / "system-unit.sha256"
    hashed.unlink()
    hashed.symlink_to(target)
    _refused(t, "a symbolic link")
    assert target.stat().st_uid == 0


def a_hard_linked_unit_hash_is_refused(t: Tenant) -> None:
    # Beside the board root: a link cannot cross the bind mount.
    target = t.home / "not-the-tenants"
    target.write_text("x", encoding="utf-8")
    hashed = t.board_root / "system-unit.sha256"
    hashed.unlink()
    os.link(target, hashed)
    _refused(t, "not a regular file with one link")
    assert target.stat().st_uid == 0


def a_substituted_board_root_is_refused(t: Tenant) -> None:
    real = t.home / "substituted-root"
    t.board_root.rename(real)
    t.board_root.symlink_to(real)
    before = {p: p.lstat() for p in (real, real / "releases")}
    problems, _ = t.prepare()
    assert problems and "symlink" in problems[0], problems
    assert {p: p.lstat() for p in before} == before, "root changed what the link pointed at"


def another_accounts_entry_is_refused(t: Tenant) -> None:
    os.chown(t.board_root / "releases", 4242, 4242)
    _refused(t, "neither")


def a_baseline_naming_another_root_is_refused(t: Tenant) -> None:
    plan = json.loads(t.baseline.read_text(encoding="utf-8"))
    plan["board_root"] = str(t.tmp / "elsewhere-live")
    t.baseline.write_text(json.dumps(plan), encoding="utf-8")
    _refused(t, "not one this repair will touch")


def without_roots_baseline_a_repair_is_refused_and_a_sound_root_passes(t: Tenant) -> None:
    t.baseline.unlink()
    _refused(t, "root will not attribute them")
    # The dry run of the upgrade that stages the baseline says what it will do.
    problems, said = t.prepare(dry_run=True)
    assert problems == [] and "once root's baseline is staged" in said, (problems, said)
    # Nothing to repair: no attribution needed, and nothing is written.
    for path in (t.board_root, t.board_root / "releases", t.board_root / "system-unit.sha256"):
        os.chown(path, t.owner.pw_uid, t.owner.pw_gid, follow_symlinks=False)
    assert t.prepare() == ([], "")


def a_fresh_tenant_needs_nothing(t: Tenant) -> None:
    shutil.rmtree(t.board_root)
    assert t.prepare() == ([], "")


CASES = (
    the_live_failure_reproduces_without_the_repair,
    dry_run_describes_the_repair_and_writes_nothing,
    the_repair_lets_the_owner_publish_and_keeps_live_state,
    a_rerun_is_idempotent,
    a_symlinked_release_parent_is_refused,
    a_symlinked_unit_hash_is_refused,
    a_hard_linked_unit_hash_is_refused,
    a_substituted_board_root_is_refused,
    another_accounts_entry_is_refused,
    a_baseline_naming_another_root_is_refused,
    without_roots_baseline_a_repair_is_refused_and_a_sound_root_passes,
    a_fresh_tenant_needs_nothing,
)


def root_child() -> None:
    assert os.geteuid() == 0
    owner = _candidate_owner()
    for case in CASES:
        tenant = Tenant(owner)
        try:
            case(tenant)
        finally:
            tenant.close()
        print(f"  ok   {case.__name__.replace('_', ' ')}")


def main() -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "--root-child"]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", *command]
    else:
        command = ["unshare", "--mount", *command]
    subprocess.run(command, check=True)
    print(f"legacy_release_root_repair_test: {len(CASES)} cases ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--root-child"]:
        root_child()
    else:
        raise SystemExit(main())
