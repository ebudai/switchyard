#!/usr/bin/env python3
"""SYRD-60: the staged board-skill bundle must run without the source tree.

The SYRD-19 rollout installed `/opt/switchyard/current/scripts/switchyard-board-skill`
into `/usr/local/lib/switchyard/syrd/`, ran it as `syrd-director`, and got
`ModuleNotFoundError: No module named 'board_skill_cli'`. The wrapper puts its
own directory on `sys.path` and imports that module, and the module was never
staged beside it.

The existing suites could not catch it: they assert command strings, or they
exercise the wrapper from a full checkout where the sibling module is importable
anyway. This one assembles the exact files the renderer says to install, in a
directory with nothing else in it, and runs the wrapper there as an account that
cannot see any checkout at all.

It runs in a user namespace with a tmpfs over `/usr/local/lib`, so the staging
path is the real pinned one and the host's is untouched.
"""

from __future__ import annotations

import json
import os
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

from scripts import team_launcher as launcher
from scripts.ticket_board.board_skill import (
    CANONICAL_SKILLS,
    RELEASE_MARKER_NAME,
    SKILLS_DIR_NAME,
)
from scripts.ticket_board.project_provision import (
    ROLE_STAGED_EXECUTABLES,
    build_plan,
    entry_point_module_dependencies,
    role_runtime_command,
    role_tooling_staging_commands,
)

PROJECT = "porter"
STAGING = Path("/usr/local/lib/switchyard") / PROJECT
#: The uid the staged bundle is exercised as: not this process, and with no
#: readable checkout anywhere in its world.
ROLE_UID = 65534


def test_the_renderer_stages_every_module_its_entry_points_import() -> None:
    """Discovered from the sources, so a new sibling import is staged with it."""
    dependencies = entry_point_module_dependencies()
    assert "board_skill_cli" in dependencies, dependencies
    for module in dependencies:
        assert (ROOT / "scripts" / f"{module}.py").is_file(), module
    commands = role_tooling_staging_commands(PROJECT, "/opt/switchyard/current")
    for module in dependencies:
        assert any(
            f"/{module}.py'" in command and "install -m 0644 -o root -g root" in command
            for command in commands
        ), module
    # Modules are readable, not executable; the entry points are executable.
    for name in ROLE_STAGED_EXECUTABLES:
        assert any(
            command.endswith(f"'{STAGING}/{name}'") and "install -m 0755" in command
            for command in commands
        ), name


def test_both_generated_paths_stage_the_bundle() -> None:
    """Fresh provisioning and the resumable migration render the same staging."""
    plan = build_plan(
        project=PROJECT,
        project_name="Porter",
        owner_user=f"{PROJECT}-agent",
        owner_home=Path("/srv") / f"{PROJECT}-agent",
        source_repo=ROOT,
    )
    fresh = role_runtime_command(plan)
    assert f"{STAGING}/board_skill_cli.py" in fresh, fresh
    assert f"{STAGING}/{SKILLS_DIR_NAME}" in fresh, fresh

    with tempfile.TemporaryDirectory(prefix="staged-bundle-migration.") as tmp:
        provision = Path(tmp) / "provision"
        provision.mkdir()
        repository = Path(tmp) / "repository"
        repository.mkdir()
        from scripts.ticket_board.project_provision import write_artifacts

        write_artifacts(plan, provision, enable_owner_linger=False)
        config_path = launcher.write_new_project_launcher_artifacts(
            plan, provision, repository=repository, print_func=lambda _text: None
        )
        config = launcher.load_project_config(PROJECT, config_path)
        migration = launcher.render_role_account_migration(config, config_path=config_path)
    assert f"{STAGING}/board_skill_cli.py" in migration, migration
    assert f"{STAGING}/{SKILLS_DIR_NAME}" in migration, migration


def _release_tree(root: Path) -> Path:
    """A release with the files the staging commands copy out of it."""
    release = root / "release"
    (release).mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "scripts", release / "scripts")
    shutil.copytree(ROOT / SKILLS_DIR_NAME, release / SKILLS_DIR_NAME)
    # A real release is an export of one commit and carries its marker; the
    # deployed tree on this host does. Provenance is what `verify` checks.
    (release / RELEASE_MARKER_NAME).write_text(
        json.dumps({"commit": "0" * 40}) + "\n", encoding="utf-8"
    )
    return release


def _sudo_shim(root: Path) -> Path:
    """`sudo` inside the namespace, where this process is already root.

    It execs its arguments unchanged, so the rendered lines run exactly as
    written -- including the shell guard around the release marker, which is
    why the block is run as one script rather than line by line.
    """
    binaries = root / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    shim = binaries / "sudo"
    shim.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return binaries


def _apply(commands: list[str], *, binaries: Path) -> None:
    script = "\n".join(["set -euo pipefail", *commands])
    environment = dict(os.environ)
    environment["PATH"] = f"{binaries}:{environment.get('PATH', '')}"
    subprocess.run(["bash", "-c", script], check=True, env=environment)


def _run_staged(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The wrapper, as the role account, with no checkout in reach."""
    return subprocess.run(
        [
            "setpriv",
            "--reuid",
            str(ROLE_UID),
            "--regid",
            str(ROLE_UID),
            "--clear-groups",
            "env",
            "-i",
            f"HOME={home}",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
            str(STAGING / "switchyard-board-skill"),
            *args,
        ],
        cwd="/",
        capture_output=True,
        text=True,
    )


def staged_bundle_runs_for_a_fresh_role_home() -> None:
    with tempfile.TemporaryDirectory(prefix="staged-bundle.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        release = _release_tree(root)
        _apply(role_tooling_staging_commands(PROJECT, str(release)), binaries=_sudo_shim(root))

        # Re-runnable: the interrupted rollout has to be resumable, so applying
        # the same block again must change nothing and fail nothing.
        _apply(role_tooling_staging_commands(PROJECT, str(release)), binaries=root / "bin")

        # Exactly what the renderer said to install, and nothing else.
        staged = {path.name for path in STAGING.iterdir()}
        expected = {
            *ROLE_STAGED_EXECUTABLES,
            *(f"{module}.py" for module in entry_point_module_dependencies()),
            "ticket_board",
            SKILLS_DIR_NAME,
            RELEASE_MARKER_NAME,
        }
        assert staged == expected, staged.symmetric_difference(expected)
        for name in ROLE_STAGED_EXECUTABLES:
            info = (STAGING / name).stat()
            assert info.st_uid == 0 and info.st_mode & 0o111, name
        for module in entry_point_module_dependencies():
            info = (STAGING / f"{module}.py").stat()
            assert info.st_uid == 0, module
            assert info.st_mode & 0o444 and not info.st_mode & 0o111, module

        home = root / "role-home"
        home.mkdir()
        os.chown(home, ROLE_UID, ROLE_UID)

        installed = _run_staged(home, "install", "--home", str(home))
        assert installed.returncode == 0, (installed.returncode, installed.stdout, installed.stderr)
        assert "ModuleNotFoundError" not in installed.stderr, installed.stderr
        for skill in CANONICAL_SKILLS:
            projected = list(home.rglob(f"{skill.name}/SKILL.md"))
            assert projected, (skill.name, sorted(p.name for p in home.rglob("*")))

        verified = _run_staged(home, "verify", "--home", str(home))
        assert verified.returncode == 0, (verified.returncode, verified.stdout, verified.stderr)

        # Provenance cannot outlive the release it came from. Stage again from
        # a source that names no commit, and the previous marker must go rather
        # than stamp the next install with a release that does not contain it.
        assert (STAGING / RELEASE_MARKER_NAME).is_file()
        markerless = root / "markerless"
        shutil.copytree(release, markerless)
        (markerless / RELEASE_MARKER_NAME).unlink()
        _apply(
            role_tooling_staging_commands(PROJECT, str(markerless)),
            binaries=root / "bin",
        )
        assert not (STAGING / RELEASE_MARKER_NAME).exists(), "a stale release marker survived"
        stale_home = root / "stale-home"
        stale_home.mkdir()
        os.chown(stale_home, ROLE_UID, ROLE_UID)
        restaged = _run_staged(stale_home, "install", "--home", str(stale_home))
        assert restaged.returncode == 0, (restaged.returncode, restaged.stderr)
        for projected in stale_home.rglob("SKILL.md"):
            assert "0" * 40 not in projected.read_text(encoding="utf-8"), projected

        # And the marked source restores it, so the branch is not one-way.
        _apply(role_tooling_staging_commands(PROJECT, str(release)), binaries=root / "bin")
        assert (STAGING / RELEASE_MARKER_NAME).is_file()

        # The defect: remove the companion module and the same run must fail.
        (STAGING / "board_skill_cli.py").unlink()
        broken = _run_staged(home, "install", "--home", str(home))
        assert broken.returncode != 0, broken.stdout
        assert "board_skill_cli" in broken.stderr, broken.stderr


def _private_staging_root() -> None:
    """A writable /usr/local/lib for this namespace, and for nothing else."""
    Path("/usr/local/lib").mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", "/usr/local/lib"], check=True)


def namespace_command(child: list[str], *, euid: int) -> list[str]:
    """The child, always inside a mount namespace of its own.

    Root gets a private mount namespace too. Without one, mounting a tmpfs over
    /usr/local/lib happens in the runner's own namespace and is never undone,
    which hides this host's staging tree from every process that follows
    (SYRD-60).
    """
    if euid == 0:
        return ["unshare", "--mount", *child]
    return ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", *child]


def test_the_namespace_is_entered_whoever_runs_this() -> None:
    child = ["python3", "x", "--namespace-child"]
    as_root = namespace_command(child, euid=0)
    assert as_root[:2] == ["unshare", "--mount"], as_root
    assert as_root[2:] == child, as_root
    as_user = namespace_command(child, euid=1000)
    assert as_user[0] == "unshare" and "--mount" in as_user, as_user
    assert "--user" in as_user and "--map-root-user" in as_user, as_user
    assert as_user[-len(child) :] == child, as_user
    # Neither path can reach the mount without the namespace in front of it.
    for command in (as_root, as_user):
        assert command.index("--mount") < command.index("--namespace-child"), command


def host_staging_listing() -> list[str]:
    """What this host has under the staging root, as the parent process sees it."""
    root = Path("/usr/local/lib")
    if not root.is_dir():
        return []
    return sorted(str(path) for path in root.iterdir())


def main() -> int:
    test_the_renderer_stages_every_module_its_entry_points_import()
    test_both_generated_paths_stage_the_bundle()
    test_the_namespace_is_entered_whoever_runs_this()
    before = host_staging_listing()
    command = namespace_command(
        [sys.executable, str(Path(__file__).resolve()), "--namespace-child"],
        euid=os.geteuid(),
    )
    subprocess.run(command, check=True)
    # The boundary, proved from outside it: nothing the child mounted or wrote
    # is visible here, and this host's staging root is exactly as it was.
    assert host_staging_listing() == before, host_staging_listing()
    assert not (STAGING).exists(), f"{STAGING} escaped the namespace"
    print("board_skill_staged_bundle_test: ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--namespace-child":
        _private_staging_root()
        staged_bundle_runs_for_a_fresh_role_home()
    else:
        raise SystemExit(main())
