#!/usr/bin/env python3
"""SYRD-161: an empty commit-store directory is not a Git repository.

SYRD-146 resumed the partial testing tenant at release 1198f80. The regenerated
packet completed -- confinement, authority, database replay, all of it -- and
the supported continuation then stopped in rollout journal 0027:

    fatal: not in a git directory
    team-launcher: failed to prepare control repository for testing:
      fetch refspec config failed with exit 128

Nothing was wrong with the packet. Confining the tenant's tree means creating
the directories between the home and each managed path before anything is
granted on them, so the commit store now exists -- owner-owned, 0750 and empty
-- before the first clone. `ensure_control_repository` asked only whether the
path existed, read that placeholder as an initialised bare repository, skipped
the clone, and ran `git config` against a directory git does not recognise.

Three things can be at that path and they need three different answers: a
repository is used, the placeholder provisioning left is cloned into, and
anything else is somebody's data that this will not delete or write over. This
suite is those three, against real git repositories on disk, plus the launch
that journal 0027 never reached.
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

from team_launcher_test_helpers import *  # noqa: F403,E402
from team_launcher_test_helpers import (  # noqa: E402
    RecordingProcessLauncher,
    _make_origin_backed_repo,
    _run_git,
)

PROJECT = "testing"
ROLES = ("main", "ops")


def real_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run it for real. The question here is what git does with the path."""
    call = dict(kwargs)
    call.setdefault("text", True)
    call.setdefault("capture_output", True)
    return subprocess.run(args, **call)


class RecordingRunner:
    """A real runner that remembers what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return real_runner(args, **kwargs)


def tenant(tmp: Path, *, roles: tuple[str, ...] = ROLES):
    """A provisioned tenant's configuration, with its repository on disk."""
    _origin, repo = _make_origin_backed_repo(tmp)
    control = tmp / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
    layout = tmp / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "Orientation": "Horizontal",
                "Widgets": [
                    {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
                    for index, _role in enumerate(roles)
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp / f"{PROJECT}.json"
    config_path.write_text(
        json.dumps(
            {
                "desktop_access": {"mode": "headless"},
                "project": PROJECT,
                "layout": str(layout),
                "repository": str(repo),
                "control_repository": str(control),
                "worktree_base": str(tmp / "worktrees"),
                "roles": [
                    {
                        "role": role,
                        "slot": index,
                        "target": f"{PROJECT}-{role}:0.0",
                        "cli": ["codex"],
                    }
                    for index, role in enumerate(roles)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # The owner home this project's commit store must live under, answered from
    # the fixture: the boundary check is the product's, the home is this tree.
    team_launcher._control_repository_owner_home = lambda _config: tmp
    return load_project_config(PROJECT, config_path), config_path, repo, control


def placeholder(control: Path) -> None:
    """What the operator packet leaves: owner-owned, 0750, and empty."""
    control.mkdir(parents=True, mode=0o750)
    assert not any(control.iterdir())


def is_repository(path: Path) -> bool:
    done = _run_git(["git", "-C", str(path), "rev-parse", "--is-bare-repository"])
    return done.returncode == 0 and done.stdout.strip() == "true"


# --------------------------------------------------------------------------


def test_the_state_of_the_commit_store_path_is_told_apart() -> None:
    """Acceptance 2, as the question it is: which of the three is this?"""
    with tempfile.TemporaryDirectory(prefix="syrd161-state.") as tmp:
        tmp_path = Path(tmp)
        missing = tmp_path / "not-there"
        assert team_launcher.control_repository_state(missing) == "missing"

        empty = tmp_path / "empty"
        empty.mkdir()
        assert team_launcher.control_repository_state(empty) == "empty"

        repo = tmp_path / "real.git"
        _run_git(["git", "init", "--bare", str(repo)])
        assert team_launcher.control_repository_state(repo) == "repository"

        occupied = tmp_path / "occupied"
        occupied.mkdir()
        (occupied / "somebody-elses-notes.txt").write_text("keep me\n", encoding="utf-8")
        assert team_launcher.control_repository_state(occupied) == "occupied"

        a_file = tmp_path / "a-file"
        a_file.write_text("not a directory\n", encoding="utf-8")
        assert team_launcher.control_repository_state(a_file) == "occupied"

        # A directory holding only part of a repository is not a repository.
        partial = tmp_path / "partial"
        (partial / "objects").mkdir(parents=True)
        assert team_launcher.control_repository_state(partial) == "occupied"


def test_a_path_this_caller_cannot_see_into_is_not_evidence_of_anything() -> None:
    """"I cannot look" must not become "somebody's data is here".

    A caller who is neither root nor the owner cannot stat inside the owner's
    home at all, and the first version of this check answered that with the
    refusal meant for real data -- which stopped a fresh project whose commit
    store it simply could not see. Absence of a reading is not a reading: the
    path is treated as one that is not there yet, which is what the existence
    check it replaced did with an unreadable path, and git says what is wrong.
    """
    with tempfile.TemporaryDirectory(prefix="syrd161-unreadable.") as tmp:
        closed = Path(tmp) / "closed"
        (closed / "control.git").mkdir(parents=True)
        closed.chmod(0o000)
        try:
            state = team_launcher.control_repository_state(closed / "control.git")
            if state == "repository":
                return  # running with privilege that ignores the mode; nothing to ask
            assert state == "unreadable", state
        finally:
            closed.chmod(0o755)


def test_the_placeholder_provisioning_leaves_is_initialised() -> None:
    """The live failure: journal 0027, from the shape the packet leaves."""
    with tempfile.TemporaryDirectory(prefix="syrd161-placeholder.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path, _repo, control = tenant(tmp_path)
        placeholder(control)

        result = team_launcher.ensure_control_repository(config, runner=real_runner)

        assert result.ok, result.failed_roles
        assert is_repository(control), sorted(p.name for p in control.iterdir())
        # And the thing that failed: the refspec the launcher configures next.
        assert _run_git(
            ["git", "-C", str(control), "config", "--get-all", "remote.origin.fetch"]
        ).stdout.strip() == "+refs/heads/*:refs/remotes/origin/*"
        # The placeholder's own directory was used, not replaced beside it.
        assert control.is_dir()


def test_a_commit_store_that_is_not_there_is_still_created() -> None:
    """The path that always worked still works, and is the same code."""
    with tempfile.TemporaryDirectory(prefix="syrd161-missing.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path, _repo, control = tenant(tmp_path)
        assert not control.exists()

        result = team_launcher.ensure_control_repository(config, runner=real_runner)

        assert result.ok, result.failed_roles
        assert is_repository(control)


def test_a_second_resume_uses_the_repository_it_already_made() -> None:
    """Acceptance 4: repeated resume is idempotent, and does not re-clone.

    Not merely "it returns ok again": a second clone into the same path would
    either fail or discard whatever the first one fetched, so what is checked
    is that no clone was attempted and the object store is the one from before.
    """
    with tempfile.TemporaryDirectory(prefix="syrd161-repeat.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path, _repo, control = tenant(tmp_path)
        placeholder(control)
        assert team_launcher.ensure_control_repository(config, runner=real_runner).ok

        before = (control / "HEAD").stat().st_ino
        marker = control / "objects" / "info" / "syrd161-marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("the first run's store\n", encoding="utf-8")

        recording = RecordingRunner()
        result = team_launcher.ensure_control_repository(config, runner=recording)

        assert result.ok, result.failed_roles
        assert not any(
            call[:3] == ["git", "clone", "--bare"] or call[1:4] == ["git", "clone", "--bare"]
            for call in recording.calls
        ), recording.calls
        assert (control / "HEAD").stat().st_ino == before
        assert marker.read_text(encoding="utf-8") == "the first run's store\n"


def test_data_at_that_path_is_refused_and_left_exactly_as_it_was() -> None:
    """Acceptance 2's third case, and the one that must never be tidied away."""
    with tempfile.TemporaryDirectory(prefix="syrd161-occupied.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path, _repo, control = tenant(tmp_path)
        control.mkdir(parents=True)
        kept = control / "somebody-elses-notes.txt"
        kept.write_text("keep me\n", encoding="utf-8")
        before = (kept.read_bytes(), kept.stat().st_ino, sorted(p.name for p in control.iterdir()))

        result = team_launcher.ensure_control_repository(config, runner=real_runner)

        assert not result.ok
        for role in ROLES:
            assert str(control) in result.failed_roles[role], result.failed_roles
            assert "not a Git repository" in result.failed_roles[role], result.failed_roles
            assert "will delete it or write over it" in result.failed_roles[role]
        assert (kept.read_bytes(), kept.stat().st_ino, sorted(p.name for p in control.iterdir())) == before
        assert not (control / "HEAD").exists(), "nothing may initialise over somebody's data"


def test_no_git_state_is_created_as_anybody_but_the_owner() -> None:
    """Acceptance 3: what the clone leaves belongs to the account it runs as.

    Asked of the product's own owner-correct runner rather than of a uid this
    process cannot become: every git command it issues for this path is either
    run as the owner or run unchanged because the caller already is the owner.
    """
    with tempfile.TemporaryDirectory(prefix="syrd161-owner.") as tmp:
        tmp_path = Path(tmp)
        config, _config_path, _repo, control = tenant(tmp_path)
        placeholder(control)
        recording = RecordingRunner()

        assert team_launcher.ensure_control_repository(config, runner=recording).ok

        owner = config.run_as_user or team_launcher.current_user_name()
        for call in recording.calls:
            if call[:1] != ["git"]:
                continue
            if owner != team_launcher.current_user_name():
                assert call[:3] == ["sudo", "-u", owner], call
        # Every file the clone produced belongs to this process, which is the
        # owner in this fixture; nothing was made by anybody else.
        for path in control.rglob("*"):
            assert path.stat().st_uid == os.getuid(), path


def test_a_launch_starts_from_the_placeholder_and_prepares_every_role() -> None:
    """What journal 0027 was trying to do, driven through launch_project."""
    with tempfile.TemporaryDirectory(prefix="syrd161-launch.") as tmp:
        tmp_path = Path(tmp)
        config, config_path, repo, control = tenant(tmp_path)
        placeholder(control)
        worktrees = tmp_path / "worktrees"

        class ProjectGitRunner(FakeRunner):
            """Real git and mkdir inside the fixture; everything else recorded."""

            def __call__(self, args: list[str], **kwargs: object):
                self.calls.append(list(args))
                inside = any(str(tmp_path) in str(value) for value in args)
                if inside and args[:1] in (["git"], ["mkdir"], ["install"]):
                    return real_runner(args, **kwargs)
                return super().__call__(args, **kwargs)

        runner = ProjectGitRunner()
        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "launch-layout.json",
                pane_state_dir=tmp_path / "pane-state",
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            == 0
        )

        assert is_repository(control)
        for role in ROLES:
            tree = worktrees / role
            assert tree.is_dir(), sorted(p.name for p in worktrees.iterdir())
            assert _run_git(
                ["git", "-C", str(tree), "rev-parse", "--is-inside-work-tree"]
            ).stdout.strip() == "true"
        # The tenant's own checkout is untouched by any of it.
        assert (repo / "tracked.txt").read_text(encoding="utf-8") == "initial\n"


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"control_repository_placeholder_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
