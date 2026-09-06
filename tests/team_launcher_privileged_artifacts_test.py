#!/usr/bin/env python3
"""SYRD-39: what root installs must come from data and paths root controls.

`switchyard upgrade` regenerates the board unit, the role-control sudoers grant,
the tmpfiles and polkit fragments and the SQL root runs as postgres. The plan
those are rendered from used to be the tenant's, and they were written through
the tenant's own directory. Either one lets the tenant choose what root runs.
These cases exercise the real root branch in a user namespace against a
genuinely distinct tenant uid.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as launcher
from scripts.ticket_board.project_provision import (
    TENANT_ARTIFACT_NAMES,
    build_plan,
    privileged_artifact_names,
    write_artifacts,
)


def test_classification_covers_every_generated_artifact() -> None:
    """A new generated artifact must be classified, not default to tenant ownership."""
    plan = build_plan(
        project="porter",
        project_name="Porter",
        owner_user="porter-agent",
        source_repo=ROOT,
    )
    with tempfile.TemporaryDirectory(prefix="artifact-classes.") as tmp:
        write_artifacts(plan, Path(tmp))
        produced = {path.name for path in Path(tmp).iterdir()}
    classified = set(privileged_artifact_names(plan)) | set(TENANT_ARTIFACT_NAMES)
    assert produced == classified, produced.symmetric_difference(classified)


def edit_tenant_plan(plan_path: Path, **fields: object) -> None:
    stored = json.loads(plan_path.read_text())
    stored.update(fields)
    plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")


def privileged_refresh(owner: str) -> None:
    """Run the root upgrade branch against a tenant-owned provision directory."""
    identity = pwd.getpwnam(owner)
    assert os.geteuid() == 0 and identity.pw_uid != 0
    with tempfile.TemporaryDirectory(prefix="privileged-artifacts-") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        provision = root / "provision"
        provision.mkdir()
        repository = root / "repository"
        repository.mkdir()
        privileged_root = root / "etc-switchyard"
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(privileged_root)

        plan = build_plan(
            project="porter",
            project_name="Porter",
            owner_user=owner,
            owner_home=root / "owner-home",
            source_repo=ROOT,
        )
        write_artifacts(plan, provision, enable_owner_linger=False)
        config_path = launcher.write_new_project_launcher_artifacts(
            plan, provision, repository=repository, print_func=lambda _text: None
        )
        # Provisioning hands the project's .switchyard tree to the tenant with a
        # recursive chown. That is the shape refresh actually runs against.
        subprocess.run(["chown", "-R", f"{owner}:{owner}", str(provision)], check=True)
        config = launcher.load_project_config("porter", config_path)
        plan_path = provision / "plan.json"
        mirror = privileged_root / "porter"
        mirrored_unit = mirror / plan.board_unit

        def runner(args, **kwargs):
            if args[0] == "chown":
                return subprocess.run(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        def refresh():
            said: list[str] = []
            outcome = launcher.refresh_generated_project_runtime_artifacts(
                config, config_path=config_path, runner=runner, print_func=said.append
            )
            return outcome, said

        # A tenant plan that would run the board as root is not adopted as
        # root's baseline at all: there is nothing to sanitise afterwards,
        # because a plan root did not write is not evidence of anything.
        edit_tenant_plan(plan_path, service_user="root", board_current="/tmp/tenant-controlled-release")
        outcome, _said = refresh()
        assert "refusing to adopt" in outcome.message and "service_user" in outcome.message, outcome.message
        # The tenant's own copies may be regenerated from the tenant's own plan
        # -- they are its view of its project -- but nothing root installs was
        # produced from it.
        assert not mirror.exists(), sorted(f.name for f in mirror.iterdir()) if mirror.exists() else ()

        # Restored to what provisioning produced, root adopts it once and keeps
        # its own copy of it from then on.
        edit_tenant_plan(plan_path, service_user=plan.service_user, board_current=plan.board_current)
        outcome, _said = refresh()
        assert outcome.changed, outcome.message
        assert (mirror / "plan.json").stat().st_uid == 0
        baseline_unit = mirrored_unit.read_text()
        assert f"User={plan.service_user}" in baseline_unit
        assert "/tmp/tenant-controlled-release" not in baseline_unit

        # The one thing the unprivileged projection may say: a role it added,
        # under that role's own canonical account.
        stored = json.loads(plan_path.read_text())
        edit_tenant_plan(
            plan_path, role_accounts=[*stored["role_accounts"], ["inspector", "porter-inspector"]]
        )
        outcome, _said = refresh()
        assert outcome.changed, outcome.message
        assert "inspector=porter-inspector" in mirrored_unit.read_text()

        # Everything else it might say is refused and reported: another role's
        # account, a non-canonical account, and the fields that decide what root
        # runs. The unit keeps the baseline's answers.
        stored = json.loads(plan_path.read_text())
        edit_tenant_plan(
            plan_path,
            service_user="root",
            board_current="/tmp/tenant-controlled-release",
            role_accounts=[
                *stored["role_accounts"],
                ["smuggled", "porter-director"],
                ["main", "porter-smuggled"],
            ],
        )
        outcome, said = refresh()
        unit = mirrored_unit.read_text()
        assert f"User={plan.service_user}" in unit and "User=root" not in unit
        assert "/tmp/tenant-controlled-release" not in unit
        assert "smuggled=porter-director" not in unit and "porter-smuggled" not in unit
        assert any("smuggled=porter-director" in line for line in said), said
        assert any("main=porter-smuggled" in line for line in said), said

        # A symlink at a generated target cannot reach its referent, because
        # root does not write into the tenant's directory at all.
        victim = root / "victim"
        victim.write_text("do not overwrite\n")
        for name in privileged_artifact_names(plan):
            target = provision / name
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(victim)
        stored = json.loads(plan_path.read_text())
        edit_tenant_plan(
            plan_path,
            service_user=plan.service_user,
            board_current=plan.board_current,
            role_accounts=[*stored["role_accounts"], ["scribe", "porter-scribe"]],
        )
        outcome, _said = refresh()
        assert outcome.changed, outcome.message
        assert victim.read_text() == "do not overwrite\n"
        for name in privileged_artifact_names(plan):
            entry = provision / name
            # The tenant's copy is republished by replacing the entry, so the
            # symlink is gone rather than followed: the referent is untouched.
            assert not entry.is_symlink(), name
            assert entry.is_file(), name
        assert "scribe=porter-scribe" in mirrored_unit.read_text()

        # Replacing the tenant's copy cannot reach root's: root's bytes never
        # come from there, so there is no window to race.
        for name in privileged_artifact_names(plan):
            entry = provision / name
            entry.unlink()
            entry.write_text("[Service]\nExecStart=/tmp/evil\n")
        stored = json.loads(plan_path.read_text())
        edit_tenant_plan(
            plan_path, role_accounts=[*stored["role_accounts"], ["porter", "porter-porter"]]
        )
        outcome, _said = refresh()
        assert outcome.changed, outcome.message
        assert "/tmp/evil" not in mirrored_unit.read_text()
        assert "porter=porter-porter" in mirrored_unit.read_text()

        # The install command names root's copy.
        status = launcher.tenant_release_status(config, config_path=config_path, runner=runner)
        assert status is not None
        assert status.provisioned_system_unit == mirrored_unit, status.provisioned_system_unit

        # Root's copy and every parent of it are root's, and stay that way.
        for directory in (mirror, *[p for p in mirror.parents if p.is_relative_to(privileged_root)]):
            assert directory.stat().st_uid == 0, directory
            assert directory.stat().st_mode & 0o022 == 0, (directory, oct(directory.stat().st_mode))
        for name in (*privileged_artifact_names(plan), "plan.json"):
            assert (mirror / name).stat().st_uid == 0, name

        child = os.fork()
        if child == 0:
            try:
                os.setgid(identity.pw_gid)
                os.setuid(identity.pw_uid)
                assert not os.access(mirrored_unit, os.W_OK), mirrored_unit
                for attempt in (
                    lambda: mirrored_unit.write_text("[Service]\nExecStart=/tmp/evil\n"),
                    lambda: os.unlink(mirrored_unit),
                    lambda: (mirror / "smuggled").write_text("x"),
                    lambda: os.rename(mirror, mirror.with_name("moved")),
                ):
                    try:
                        attempt()
                    except (PermissionError, OSError):
                        continue
                    raise AssertionError(f"tenant could modify root's install source: {attempt}")
                (provision / "plan.json").read_text()
            except BaseException:
                import traceback

                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        assert os.waitpid(child, 0)[1] == 0
        assert "/tmp/evil" not in mirrored_unit.read_text()


def main() -> int:
    test_classification_covers_every_generated_artifact()
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [sys.executable, str(Path(__file__).resolve()), "--ownership-child", owner]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    print("team_launcher_privileged_artifacts_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--ownership-child":
        privileged_refresh(sys.argv[2])
    else:
        raise SystemExit(main())
