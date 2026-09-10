#!/usr/bin/env python3
"""SYRD-100 review: deploying the release root has already materialized.

The trusted bootstrap exists so a commit that has NOT been published can still
be installed: the operator bundles it, root imports it into a repository of its
own, and the installer materializes `/opt/switchyard/releases/<sha>`. The
operator then names that release and its exact commit.

The release phase resolved the deploy target through the tenant's ordinary
publication cache instead, which for this case is circular -- the commit is
absent from that cache precisely because it has not been published. The live
upgrade refused with `cannot produce a safe release update` for a release root
had already materialized and activated, left the release phase `ready`, and
returned 0, so the wrapper reported the upgrade complete over a board that had
not moved.

Root's own marker is the answer to "which commit is this release". These cases
cover using it, refusing without it, and the exit status that must accompany a
refusal.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from team_launcher_test_helpers import *  # noqa: F401,F403
from scripts import team_launcher
from scripts.team_launcher import (
    installed_release_deploy_target,
    release_update_blocked,
)

SHA = "38f1096a93237adafc8c72a0326834b677778d08"
OTHER = "c3ed60f5e9492ed6f664b31fdf68f0996915d68f"


def installed_release(tmp: Path, commit: str = SHA, *, marker: bool = True) -> tuple[Path, Path]:
    """An install root with one materialized release, as the installer leaves it."""
    install_root = tmp / "opt" / "switchyard"
    release = install_root / "releases" / commit
    (release / "scripts").mkdir(parents=True)
    (release / "switchyard").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    if marker:
        (release / ".switchyard-release.json").write_text(
            json.dumps({"commit": commit, "source_repo": "/opt/switchyard/bootstrap/src", "source_ref": commit}),
            encoding="utf-8",
        )
    return install_root, release


@contextmanager
def install_root_at(install_root: Path | None):
    """The documented seam: a fixture cannot own "/", so the walk starts here.

    Restores whatever was there before rather than clearing it. The suite sets
    this variable for every case, so popping it leaks into the next one -- which
    it did, and turned an unrelated case into a failure about role state.
    """
    key = "SWITCHYARD_SHARED_INSTALL_ROOT"
    previous = os.environ.get(key)
    if install_root is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = str(install_root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def test_an_exact_sha_matching_the_marker_is_the_deploy_target() -> None:
    """The bootstrap case: the commit is not published anywhere, and need not be."""
    with tempfile.TemporaryDirectory(prefix="release-marker.") as tmp:
        install_root, release = installed_release(Path(tmp))
        with install_root_at(install_root):
            commit, refusal = installed_release_deploy_target(release, SHA)
        assert commit == SHA, (commit, refusal)
        assert refusal == "", refusal


def test_a_release_naming_another_commit_is_refused_rather_than_searched_for() -> None:
    """A mismatch must not get a second chance from a cache that happens to have it."""
    with tempfile.TemporaryDirectory(prefix="release-mismatch.") as tmp:
        install_root, release = installed_release(Path(tmp), commit=OTHER)
        with install_root_at(install_root):
            commit, refusal = installed_release_deploy_target(release, SHA)
        assert commit == "", commit
        assert OTHER in refusal and SHA in refusal, refusal
        assert "Nothing was deployed" in refusal, refusal


def test_a_release_with_no_marker_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="release-nomarker.") as tmp:
        install_root, release = installed_release(Path(tmp), marker=False)
        with install_root_at(install_root):
            commit, refusal = installed_release_deploy_target(release, SHA)
        assert commit == "", commit
        assert "carries no release marker" in refusal, refusal


def test_a_release_anybody_could_have_rewritten_is_refused() -> None:
    """Trust is the whole path, not the leaf: this is what makes the marker usable.

    A release directory anyone can write is a marker anyone can write, and the
    marker is the only thing saying which commit this is.
    """
    with tempfile.TemporaryDirectory(prefix="release-untrusted.") as tmp:
        install_root, release = installed_release(Path(tmp))
        (install_root / "releases").chmod(0o777)
        try:
            with install_root_at(install_root):
                commit, refusal = installed_release_deploy_target(release, SHA)
        finally:
            (install_root / "releases").chmod(0o755)
        assert commit == "", commit
        assert "not root-controlled" in refusal, refusal
        assert "releases" in refusal, refusal


def test_a_symbolic_ref_is_left_to_the_ordinary_cache() -> None:
    """Only an exact commit is answered here; a name is somebody asking to resolve."""
    with tempfile.TemporaryDirectory(prefix="release-symbolic.") as tmp:
        install_root, release = installed_release(Path(tmp))
        with install_root_at(install_root):
            for ref in ("origin/main", "main", "HEAD", SHA[:12]):
                commit, refusal = installed_release_deploy_target(release, ref)
                assert (commit, refusal) == ("", ""), (ref, commit, refusal)


def test_an_ordinary_checkout_is_not_treated_as_a_release() -> None:
    with tempfile.TemporaryDirectory(prefix="release-checkout.") as tmp:
        checkout = Path(tmp) / "worktree"
        checkout.mkdir()
        with install_root_at(Path(tmp) / "opt" / "switchyard"):
            commit, refusal = installed_release_deploy_target(checkout, SHA)
        assert (commit, refusal) == ("", ""), (commit, refusal)


def test_a_refusal_is_what_the_report_and_the_exit_status_both_read() -> None:
    """One reading. Saying it and exiting 0 is what reported a complete upgrade."""
    from scripts.team_launcher import TenantReleaseStatus

    unresolved = TenantReleaseStatus(
        board_root=Path("/srv/board"),
        owner_user="agent",
        owner_home=Path("/home/agent"),
        provisioned_system_unit=Path("/etc/switchyard/provision/p/p-ticket-board.service"),
        commit_git_dir="/srv/git/cache.git",
        current_release=Path("/srv/board/releases/old"),
        current_sha="a" * 40,
        target_sha="",
        deploy_ref=SHA,
        source_repo=Path("/opt/switchyard/releases/" + SHA),
        resolve_error="the cache has never heard of it",
    )
    assert release_update_blocked(unresolved) == "the cache has never heard of it"

    incomplete = TenantReleaseStatus(
        board_root=Path("/srv/board"),
        owner_user="agent",
        owner_home=Path("/home/agent"),
        provisioned_system_unit=None,
        commit_git_dir="",
        current_release=None,
        current_sha="a" * 40,
        target_sha=SHA,
        deploy_ref=SHA,
        source_repo=Path("/opt/switchyard/releases/" + SHA),
    )
    assert "units are incomplete" in release_update_blocked(incomplete)

    deployable = TenantReleaseStatus(
        board_root=Path("/srv/board"),
        owner_user="agent",
        owner_home=Path("/home/agent"),
        provisioned_system_unit=Path("/etc/switchyard/provision/p/p-ticket-board.service"),
        commit_git_dir="",
        current_release=None,
        current_sha="a" * 40,
        target_sha=SHA,
        deploy_ref=SHA,
        source_repo=Path("/opt/switchyard/releases/" + SHA),
    )
    assert release_update_blocked(deployable) == ""
    # And a board already on the target is not blocked either.
    assert release_update_blocked(
        TenantReleaseStatus(
            board_root=Path("/srv/board"),
            owner_user="agent",
            owner_home=Path("/home/agent"),
            provisioned_system_unit=None,
            commit_git_dir="",
            current_release=None,
            current_sha=SHA,
            target_sha=SHA,
            deploy_ref=SHA,
            source_repo=Path("/opt/switchyard/releases/" + SHA),
        )
    ) == ""


def _tenant_on_an_old_release(tmp: Path):
    """A tenant with a board root, so the release phase actually runs."""
    import team_launcher_upgrade_cutover_test as cutover

    board = cutover._deployed_release(tmp, "porter", "a" * 40)
    config_path, _root = cutover._declarative_tenant(tmp, board_root=board)
    staged = trusted_release_root_for(stage_trusted_releases())
    commit = json.loads((staged / ".switchyard-release.json").read_text(encoding="utf-8"))["commit"]
    return cutover, config_path, staged, commit


def test_the_upgrade_deploys_an_installed_release_without_the_publication_cache() -> None:
    """The live case, through the command an operator actually runs.

    No `--commit-git-dir`, and the tenant's ordinary cache has never heard of
    this commit, because it has deliberately not been published.
    """
    with tempfile.TemporaryDirectory(prefix="release-upgrade.") as tmp:
        cutover, config_path, staged, commit = _tenant_on_an_old_release(Path(tmp))
        result, output, _migrations = cutover._upgrade(
            config_path, as_root=True, source_repo=staged, deploy_ref=commit
        )

        assert result == 0, output
        assert "cannot produce a safe release update" not in output, output
        assert f"deployed board release new: {commit} from {commit}" in output, output
        # The deployment sequence is rendered, which is the thing the live run
        # never reached.
        assert "matching-release deployment sequence" in output, output
        # And the release tree is deployed directly: no archive out of a cache,
        # because the release is already the materialized commit.
        assert "git --git-dir=" not in output, output


def test_a_blocked_release_phase_never_reports_success() -> None:
    """`cannot produce a safe release update` and exit 0 may not coexist.

    The live upgrade printed that verdict, left the phase `ready`, and returned
    0, so the bounded wrapper reported REAL UPGRADE COMPLETE over a board that
    was still on the old release.
    """
    with tempfile.TemporaryDirectory(prefix="release-blocked.") as tmp:
        cutover, config_path, staged, commit = _tenant_on_an_old_release(Path(tmp))
        # The generated units are gone, so no safe deploy can be produced.
        privileged = team_launcher.privileged_provision_dir(
            "porter", root=team_launcher.switchyard_privileged_provision_root()
        )
        for unit in (*privileged.glob("porter-ticket-board*.service"),
                     *config_path.parent.glob("porter-ticket-board*.service")):
            unit.unlink()

        result, output, _migrations = cutover._upgrade(
            config_path, as_root=True, source_repo=staged, deploy_ref=commit
        )

        assert "cannot produce a safe release update" in output, output
        assert result != 0, output
        assert "release phase did not complete" in output, output
        assert "Nothing after it is claimed" in output, output


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("tenant_release_from_installed_release_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
