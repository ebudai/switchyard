#!/usr/bin/env python3
"""SYRD-39: generated artifacts root consumes must not become tenant-writable.

`switchyard upgrade` regenerates the board unit, the role-control sudoers grant,
the tmpfiles and polkit fragments and the SQL root runs as postgres. Handing any
of those to the tenant -- or leaving root to install them out of a directory the
tenant owns -- means the tenant chooses what root runs. This exercises the real
root branch in a user namespace with a genuinely distinct tenant uid.
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
            plan,
            provision,
            repository=repository,
            print_func=lambda _text: None,
        )
        # Provisioning hands the project's .switchyard tree to the tenant with a
        # recursive chown. That is the shape refresh actually runs against: a
        # tenant-owned directory holding artifacts root installs.
        subprocess.run(["chown", "-R", f"{owner}:{owner}", str(provision)], check=True)

        # A role added declaratively: the tenant's own plan write, which is what
        # the refresh has to pick up.
        plan_path = provision / "plan.json"
        stored = json.loads(plan_path.read_text())
        stored["role_accounts"] = [*stored["role_accounts"], ["inspector", "porter-inspector"]]
        plan_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n")

        def runner(args, **kwargs):
            if args[0] == "chown":
                return subprocess.run(args, **kwargs)
            return subprocess.CompletedProcess(args, 0, "", "")

        config = launcher.load_project_config("porter", config_path)
        result = launcher.refresh_generated_project_runtime_artifacts(
            config, config_path=config_path, runner=runner
        )
        assert result.changed, result.message

        board_unit = provision / plan.board_unit
        mirror = privileged_root / "porter"
        mirrored_unit = mirror / plan.board_unit

        # The table the tenant wrote reached the unit root installs.
        assert "inspector=porter-inspector" in mirrored_unit.read_text()
        assert "inspector=porter-inspector" in board_unit.read_text()

        # Everything root consumes stayed root's, including the sudoers grant.
        for name in privileged_artifact_names(plan):
            path = provision / name
            assert path.stat().st_uid == 0 and path.stat().st_gid == 0, (name, path.stat())
            assert (mirror / name).stat().st_uid == 0, name
        assert (provision / "plan.json").stat().st_uid == identity.pw_uid

        # Root's own copy is unreachable: the mirror and every parent of it are
        # root-owned, so the tenant cannot replace a file by replacing its
        # directory either.
        for directory in (mirror, *[p for p in mirror.parents if p.is_relative_to(privileged_root)]):
            assert directory.stat().st_uid == 0, directory
            assert directory.stat().st_mode & 0o022 == 0, (directory, oct(directory.stat().st_mode))

        # The printed install command must name root's copy, not the tenant's.
        status = launcher.tenant_release_status(config, config_path=config_path, runner=runner)
        assert status is not None
        assert status.provisioned_system_unit == mirrored_unit, status.provisioned_system_unit

        # And the tenant genuinely cannot rewrite it.
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
                # The tenant does still own its own plan.
                (provision / "plan.json").read_text()
            except BaseException:
                import traceback

                traceback.print_exc()
                os._exit(1)
            os._exit(0)
        assert os.waitpid(child, 0)[1] == 0
        assert "inspector=porter-inspector" in mirrored_unit.read_text()


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
