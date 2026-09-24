#!/usr/bin/env python3
"""The director's phase reports the release the operator pinned, not origin/main.

Live MEFP, 2026-09-24: the operator installed and deployed pinned release
544402b, root handed over the reviewed workflow, and the director ran

    switchyard finish-upgrade mefp

It activated the workflow and then exited 1:

    switchyard: mefp deployed board release new: (unresolved origin/main: and the repository exists.)
    switchyard: cannot produce a safe release update for mefp: and the repository exists.

Root's record of the pin is in /etc/switchyard/provision/mefp, which is 0700
root. The director cannot read it, so the pin was silently dropped and
`origin/main` resolved in its place -- by an `ls-remote` the director has no
credential for, whose multi-line refusal was then cut to its last line
(SYRD-255).

These cases drive the real `finish_upgrade_command` against a tenant whose
privileged provision directory the caller genuinely cannot open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from scripts import team_launcher
from team_launcher_test_helpers import (
    FakeRunner,
    _stage_role_tooling,
    stage_trusted_releases,
    trusted_release_root_for,
)

import team_launcher_upgrade_cutover_test as cutover  # noqa: E402

#: Git's own words for a remote this account cannot read, all of them.
GIT_REFUSAL = (
    "fatal: Could not read from remote repository.",
    "Please make sure you have the correct access rights",
    "and the repository exists.",
)


class Tenant:
    """A tenant already on the pinned release, as MEFP was."""

    def __init__(self, tmp: Path) -> None:
        staged = trusted_release_root_for(stage_trusted_releases())
        self.commit = json.loads(
            (staged / ".switchyard-release.json").read_text(encoding="utf-8")
        )["commit"]
        self.release = staged
        board = cutover._deployed_release(tmp, "porter", self.commit)
        self.config_path, _root = cutover._declarative_tenant(tmp, board_root=board)
        self.config = team_launcher.load_project_config("porter", self.config_path)
        self.privileged = team_launcher.privileged_provision_dir(
            "porter", root=team_launcher.switchyard_privileged_provision_root()
        )
        # The units the release report needs, where an unprivileged director
        # finds them: beside its own config, not in root's directory.
        for unit in self.privileged.glob("porter-ticket-board*.service"):
            target = self.config_path.parent / unit.name
            if not target.exists():
                target.write_bytes(unit.read_bytes())
        # The role bundle root stages where every role can read it, made by the
        # real staging commands from the pinned release, marker and all.
        self.previous_control_root = os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT")
        control_root = _stage_role_tooling(tmp, "porter", release_root=staged, name="tenant-control")
        os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = str(control_root)
        self.staged_marker = control_root / "porter" / ".switchyard-release.json"
        assert self.staged_marker.is_file(), "the staging commands wrote no release marker"
        self.locked = False

    def record_pin(self) -> None:
        """Root's own record, exactly as the privileged phase writes it."""
        path = team_launcher.privileged_upgrade_source_path(self.config)
        path.write_text(json.dumps({
            "schema": team_launcher.UPGRADE_SOURCE_SCHEMA, "project": "porter",
            "source_repo": str(self.release), "commit_git_dir": "", "deploy_ref": self.commit,
        }), encoding="utf-8")
        path.chmod(0o600)

    def lock_privileged_directory(self) -> None:
        """What the director meets on a host: a directory it cannot open."""
        self.privileged.chmod(0o000)
        self.locked = True

    def finish(self, runner: FakeRunner | None = None, **kwargs) -> tuple[int, str, FakeRunner]:
        runner = runner or GitRefusingRunner()
        printed: list[str] = []
        real_migrate = team_launcher.migrate_declarative_director_onboarding
        real_state = team_launcher.director_onboarding_state
        # The director's own board writes are not what this is about, and a
        # fixture has no board to make them against.
        team_launcher.migrate_declarative_director_onboarding = lambda cfg, **kw: True
        team_launcher.director_onboarding_state = lambda cfg, **kw: ("done", "")
        try:
            result = team_launcher.finish_upgrade_command(
                self.config, config_path=self.config_path, runner=runner,
                print_func=printed.append, **kwargs,
            )
        finally:
            team_launcher.migrate_declarative_director_onboarding = real_migrate
            team_launcher.director_onboarding_state = real_state
        return result, "\n".join(printed), runner

    def close(self) -> None:
        if self.locked:
            self.privileged.chmod(0o700)
        if self.previous_control_root is None:
            os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
        else:
            os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = self.previous_control_root


class GitRefusingRunner(FakeRunner):
    """Any git the director runs is refused the way an unreadable remote is."""

    def __call__(self, args, *rest, **kwargs):  # type: ignore[override]
        argv = list(args)
        if argv[:1] == ["git"] or (argv[:1] == ["sudo"] and "git" in argv):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 128, "", "\n".join(GIT_REFUSAL) + "\n")
        return super().__call__(args, *rest, **kwargs)


def _with_tenant(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd255.") as tmp:
            tenant = Tenant(Path(tmp))
            try:
                case(tenant)
            finally:
                tenant.close()
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


@_with_tenant
def test_an_unreadable_pin_reports_the_release_already_live(tenant: Tenant) -> None:
    """The live case: pinned, deployed, and the director cannot read root's record."""
    assert os.getuid() != 0, "this case is about the unprivileged vantage"
    tenant.record_pin()
    tenant.lock_privileged_directory()
    assert not os.access(tenant.privileged, os.R_OK | os.X_OK), "the directory is still open"

    result, output, runner = tenant.finish()

    assert result == 0, output
    assert "origin/main" not in output, output
    assert f"deployed board release new: {tenant.commit} from {tenant.commit}" in output, output
    assert "unchanged; no release deploy needed" in output, output
    # It says where the answer came from, and why it did not come from root's record.
    assert f"reporting the release root staged for porter's roles instead: {tenant.commit}" in output, output
    assert "is not readable by" in output, output
    assert "cannot produce a safe release update" not in output, output
    # Nothing went looking for a branch, so nothing needed a credential.
    assert not any("ls-remote" in call for call in runner.calls), runner.calls


@_with_tenant
def test_roots_readable_record_still_wins(tenant: Tenant) -> None:
    """Where root's record can be read, it is the pin, as it always was (SYRD-61)."""
    tenant.record_pin()
    result, output, _runner = tenant.finish()
    assert result == 0, output
    assert f"reporting the release porter was pinned to: " in output, output
    assert "staged for porter's roles" not in output, output


@_with_tenant
def test_an_explicit_ref_still_wins(tenant: Tenant) -> None:
    tenant.record_pin()
    tenant.lock_privileged_directory()
    result, output, _runner = tenant.finish(source_repo=tenant.release, deploy_ref=tenant.commit)
    assert result == 0, output
    assert "staged for porter's roles" not in output, output
    assert "no pinned release is readable" not in output, output


@_with_tenant
def test_a_staged_marker_anybody_could_rewrite_is_not_believed(tenant: Tenant) -> None:
    """The marker decides what is reported, so it has to be root's to decide."""
    tenant.record_pin()
    tenant.lock_privileged_directory()
    staged_dir = tenant.staged_marker.parent
    mode = staged_dir.stat().st_mode & 0o7777
    staged_dir.chmod(0o777)
    try:
        result, output, _runner = tenant.finish()
    finally:
        staged_dir.chmod(mode)
    assert "staged for porter's roles instead" not in output, output
    assert "is not root's" in output and "writable" in output, output
    # And the fallback is said out loud, never taken silently.
    assert "no pinned release is readable for porter" in output, output
    assert result != 0, output


@_with_tenant
def test_with_no_readable_pin_the_fallback_and_the_git_reason_are_complete(tenant: Tenant) -> None:
    tenant.record_pin()
    tenant.lock_privileged_directory()
    tenant.staged_marker.unlink()
    result, output, runner = tenant.finish()

    assert result != 0, output
    assert "no pinned release is readable for porter" in output, output
    assert str(team_launcher.privileged_upgrade_source_path(tenant.config)) in output, output
    assert "Resolving origin/main instead" in output, output
    # The whole of Git's refusal, and the command it refused.
    for line in GIT_REFUSAL:
        assert line in output, (line, output)
    assert "ls-remote origin refs/heads/main" in output, output
    # And the partial result: the workflow half is not what failed.
    assert "release phase did not complete" in output, output


@_with_tenant
def test_the_partial_result_says_the_workflow_stays(tenant: Tenant) -> None:
    tenant.record_pin()
    tenant.lock_privileged_directory()
    tenant.staged_marker.unlink()
    real_install = team_launcher.install_handed_off_workflow
    team_launcher.install_handed_off_workflow = lambda cfg, **kw: True
    try:
        result, output, _runner = tenant.finish()
    finally:
        team_launcher.install_handed_off_workflow = real_install
    assert result != 0, output
    assert "handed-off workflow is installed and stays installed" in output, output
    assert "only the release phase is outstanding" in output, output


def test_a_real_git_refusal_reaches_the_operator_whole() -> None:
    """Real git, a remote that is not there, and the reason as git gave it."""
    with tempfile.TemporaryDirectory(prefix="syrd255-git.") as tmp:
        repo = Path(tmp) / "checkout"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        missing = Path(tmp) / "no-such-remote.git"
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(missing)], check=True)
        sha, reason = team_launcher._resolve_deploy_ref_readonly(repo, "origin/main")
    assert sha == "", sha
    assert "does not appear to be a git repository" in reason, reason
    for line in GIT_REFUSAL:
        assert line in reason, (line, reason)
    assert f"git -C {repo} ls-remote origin refs/heads/main" in reason, reason
    assert "\n" not in reason, reason


def test_every_line_of_a_failure_is_kept() -> None:
    proc = subprocess.CompletedProcess(["git"], 128, "", "error: first\n\nfatal: second\n  third  \n")
    assert team_launcher._proc_failure_reason(proc, "fallback") == "error: first fatal: second third"
    assert team_launcher._proc_failure_reason(
        subprocess.CompletedProcess(["git"], 1, "", "  \n"), "fallback"
    ) == "fallback"


def main() -> int:
    from standalone_test_runner import run_module_tests

    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"finish_upgrade_pinned_release_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
