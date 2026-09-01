#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_control_repository_launch_uses_role_worktrees_and_preserves_repository() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root.resolve(strict=False)

        def _is_scratch_path(self, path: Path) -> bool:
            resolved = path.resolve(strict=False)
            return resolved == self.root or resolved.is_relative_to(self.root)

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            run_kwargs = dict(kwargs)
            run_kwargs.setdefault("text", True)
            if "stdout" not in run_kwargs:
                run_kwargs["stdout"] = subprocess.PIPE
            if "stderr" not in run_kwargs:
                run_kwargs["stderr"] = subprocess.PIPE
            if args[:1] == ["mkdir"] and len(args) >= 3 and self._is_scratch_path(Path(args[2])):
                return subprocess.run(args, **run_kwargs)
            if args[:1] == ["git"]:
                git_path: Path | None = None
                if len(args) >= 3 and args[1] == "-C":
                    git_path = Path(args[2])
                elif len(args) >= 4 and args[1:3] == ["clone", "--bare"]:
                    git_path = Path(args[3])
                else:
                    for index, arg in enumerate(args):
                        if arg == "--git-dir" and index + 1 < len(args):
                            git_path = Path(args[index + 1])
                            break
                if git_path is not None and self._is_scratch_path(git_path):
                    return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "ignore bytecode"], cwd=repo)
        (repo / "tracked.txt").write_text("user's local edit\n", encoding="utf-8")
        (repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "porter.json"
        worktree_base = tmp_path / "worktrees"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "worktree_base": str(worktree_base),
                    "roles": [
                        {"role": "ops", "slot": 0, "target": "porter-ops:0.0", "cli": ["codex"]},
                        {"role": "app", "slot": 1, "target": "porter-app:0.0", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("porter", config_path)
            runner = ProjectGitRunner(tmp_path)
            process_launcher = RecordingProcessLauncher()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "launch-layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

            assert (repo / "tracked.txt").read_text(encoding="utf-8") == "user's local edit\n"
            assert (repo / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
            assert config.roles[0].workdir == str(worktree_base / "ops")
            assert config.roles[1].workdir == str(worktree_base / "app")
            assert _run_git(["git", "-C", worktree_base / "ops", "rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
            assert _run_git(["git", "-C", worktree_base / "app", "rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
            assert _run_git(["git", "-C", worktree_base / "ops", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() == "HEAD"
            assert _run_git(["git", "-C", control_repo, "config", "--get-all", "remote.origin.fetch"]).stdout.strip() == (
                "+refs/heads/*:refs/remotes/origin/*"
            )
            assert git_checkout_shared_ref_args(config) not in runner.calls
            assert git_clean_shared_checkout_args(config) not in runner.calls
            assert any(call == team_launcher.git_control_worktree_add_args(config, config.roles[0]) for call in runner.calls)
            assert any(call == team_launcher.git_control_worktree_add_args(config, config.roles[1]) for call in runner.calls)

            ops_worktree = worktree_base / "ops"
            app_worktree = worktree_base / "app"
            (ops_worktree / "tracked.txt").write_text("agent local edit\n", encoding="utf-8")
            (ops_worktree / "agent_notes.txt").write_text("agent notes\n", encoding="utf-8")
            (app_worktree / "__pycache__").mkdir()
            (app_worktree / "__pycache__" / "x.pyc").write_bytes(b"pyc\n")
            stderr = StringIO()
            second_process_launcher = RecordingProcessLauncher()
            with redirect_stderr(stderr):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=ProjectGitRunner(tmp_path),
                        layout_output=tmp_path / "launch-layout-2.json",
                        pane_state_dir=tmp_path / "pane-state-2",
                        konsole_process_launcher=second_process_launcher,
                    )
                    == 0
                )
            warning = stderr.getvalue()
            assert "will reset managed role worktree" in warning
            assert "for ops to origin/main" in warning
            assert "tracked.txt" in warning
            assert "agent_notes.txt" in warning
            assert "__pycache__" not in warning
            assert (ops_worktree / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
            assert not (ops_worktree / "agent_notes.txt").exists()
            assert not (app_worktree / "__pycache__").exists()
        finally:
            team_launcher._control_repository_owner_home = original_home

def test_control_repository_launch_runs_bootstrap_as_configured_owner() -> None:
    class OwnerRecordingRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] == ["sudo", "-u", "otto-agent", "-H"] and args[4:6] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-owner.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "otto.json"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": "otto-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [
                        {"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]},
                        {"role": "app", "slot": 1, "target": "otto-app:0.0", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        runner = OwnerRecordingRunner()
        process_launcher = RecordingProcessLauncher()
        layout_output = tmp_path / "launch-layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        owner_calls = [call for call in runner.calls if call[:3] == ["sudo", "-u", "otto-agent"]]
        assert ["sudo", "-u", "otto-agent", *team_launcher.mkdir_p_args(control_repo.parent)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_clone_control_repository_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_fetch_refspec_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_fetch_control_ref_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.mkdir_p_args(worktree_base)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_worktree_add_args(config, config.roles[0])] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_worktree_add_args(config, config.roles[1])] in owner_calls
        assert team_launcher.git_clone_control_repository_args(config) not in runner.calls
        assert team_launcher.git_control_worktree_add_args(config, config.roles[0]) not in runner.calls
        assert not any(call[:2] == ["chown", "-R"] for call in runner.calls)
        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["args"] == konsole_launch_args(layout_output)
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        assert not any(call == konsole_launch_args(layout_output) for call in runner.calls)

def test_control_repository_bootstrap_failure_aborts_without_opening_window() -> None:
    class FailingControlRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] == ["sudo", "-u", "otto-agent", "-H"] and args[4:6] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:3] == ["sudo", "-u", "otto-agent"] and args[3:6] == ["git", "clone", "--bare"]:
                return subprocess.CompletedProcess(
                    args,
                    128,
                    stderr="fatal: detected dubious ownership in repository at '/home/otto-agent/Projects/otto/.git'\n",
                )
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-fail.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "otto.json"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": "otto-agent",
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        runner = FailingControlRunner()
        layout_output = tmp_path / "launch-layout.json"
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )

        assert result == 1
        assert "failed to prepare control repository for otto" in stderr.getvalue()
        assert "detected dubious ownership" in stderr.getvalue()
        assert not layout_output.exists()
        assert not any(call == konsole_launch_args(layout_output) for call in runner.calls)

def test_control_repository_repairs_owner_mismatch_before_clone() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        current_info = team_launcher.pwd.getpwnam(current_user)
        repo = tmp_path / "repo"
        repo.mkdir()
        state_dir = tmp_path / ".local" / "state"
        switchyard_dir = state_dir / "switchyard"
        projects_dir = switchyard_dir / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        root_owned = {state_dir, switchyard_dir, projects_dir}

        class RepairRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                for path in tuple(root_owned):
                    if args == team_launcher.chown_control_repository_args(config, path, recursive=False):
                        self.calls.append(args)
                        root_owned.remove(path)
                        return subprocess.CompletedProcess(args, 0)
                if args[:2] == ["chown", "-R"] or args[:1] == ["chown"]:
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 1, stderr=f"unexpected chown: {shlex.join(args)}\n")
                return super().__call__(args, **kwargs)

        original_home = team_launcher._control_repository_owner_home
        original_owner_ids = team_launcher._path_owner_ids
        original_label = team_launcher._path_owner_label
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
            team_launcher._path_owner_ids = (
                lambda path: (0, 0)
                if path in root_owned
                else (current_info.pw_uid, current_info.pw_gid)
                if path.exists()
                else None
            )
            team_launcher._path_owner_label = lambda _path: "root:root"
            runner = RepairRunner()

            result = team_launcher.ensure_control_repository(config, runner=runner)
        finally:
            team_launcher._control_repository_owner_home = original_home
            team_launcher._path_owner_ids = original_owner_ids
            team_launcher._path_owner_label = original_label

        assert result.ok
        assert root_owned == set()
        state_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, state_dir, recursive=False))
        switchyard_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, switchyard_dir, recursive=False))
        projects_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, projects_dir, recursive=False))
        mkdir_index = runner.calls.index(team_launcher.mkdir_p_args(control_repo.parent))
        clone_index = runner.calls.index(team_launcher.git_clone_control_repository_args(config))
        assert state_chown_index < switchyard_chown_index < projects_chown_index < mkdir_index < clone_index

def test_control_repository_chown_failure_names_actual_and_expected_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair-fail.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        current_info = team_launcher.pwd.getpwnam(current_user)
        repo = tmp_path / "repo"
        repo.mkdir()
        projects_dir = tmp_path / ".local" / "state" / "switchyard" / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        class RefusingRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args == team_launcher.chown_control_repository_args(config, projects_dir, recursive=False):
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 1, stderr="chown: Operation not permitted\n")
                return super().__call__(args, **kwargs)

        original_home = team_launcher._control_repository_owner_home
        original_owner_ids = team_launcher._path_owner_ids
        original_label = team_launcher._path_owner_label
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
            team_launcher._path_owner_ids = (
                lambda path: (0, 0)
                if path == projects_dir
                else (current_info.pw_uid, current_info.pw_gid)
                if path.exists()
                else None
            )
            team_launcher._path_owner_label = lambda _path: "root:root"
            runner = RefusingRunner()

            result = team_launcher.ensure_control_repository(config, runner=runner)
        finally:
            team_launcher._control_repository_owner_home = original_home
            team_launcher._path_owner_ids = original_owner_ids
            team_launcher._path_owner_label = original_label

        assert not result.ok
        reason = result.failed_roles["ops"]
        assert str(control_repo) in reason
        assert "root:root" in reason
        assert f"expected {current_user}:{current_user}" in reason
        assert "Operation not permitted" in reason
        assert team_launcher.git_clone_control_repository_args(config) not in runner.calls

def test_control_repository_repair_requires_existing_target_user() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair-user.") as tmp:
        tmp_path = Path(tmp)
        missing_user = "pgu-nosuch-control-user-680"
        repo = tmp_path / "repo"
        repo.mkdir()
        projects_dir = tmp_path / ".local" / "state" / "switchyard" / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        control_repo.parent.mkdir()
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": missing_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home

        runner = FakeRunner()
        result = team_launcher.repair_control_repository_ownership(config, runner=runner)

        assert not result.ok
        reason = result.failed_roles["ops"]
        assert "refusing to repair control repository ownership" in reason
        assert f"target user {missing_user!r} does not exist" in reason
        assert not any(call[:1] == ["chown"] or call[:2] == ["sudo", "chown"] for call in runner.calls)

def test_control_repository_repair_refuses_unmanaged_or_slugless_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-boundary.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        managed_root = tmp_path / ".local" / "state" / "switchyard" / "projects"
        managed_root.mkdir(parents=True)
        configs: list[Path] = [
            managed_root / "control.git",
            tmp_path / "control.git",
            managed_root / "otto" / ".." / ".." / ".." / "control.git",
        ]
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            for index, control_repo in enumerate(configs):
                config_path = tmp_path / f"otto-{index}.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "project": "otto",
                            "layout": str(tmp_path / "layout.json"),
                            "repository": str(repo),
                            "control_repository": str(control_repo),
                            "run_as_user": current_user,
                            "worktree_base": str(tmp_path / "worktrees"),
                            "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                try:
                    load_project_config("otto", config_path)
                    raise AssertionError("expected SystemExit")
                except SystemExit as exc:
                    message = str(exc)

                assert "control_repository" in message
                assert "not a switchyard-managed control directory" in message
        finally:
            team_launcher._control_repository_owner_home = original_home

def test_control_repository_load_rejects_unmanaged_path_with_expected_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-load-boundary.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(tmp_path / "control.git"),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            try:
                load_project_config("otto", config_path)
                raise AssertionError("expected SystemExit")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher._control_repository_owner_home = original_home

        assert "control_repository" in message
        assert "not a switchyard-managed control directory" in message
        assert str(tmp_path / ".local" / "state" / "switchyard" / "projects") in message

def test_control_repository_load_accepts_generated_prefix_and_pgu_without_control_repo() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-load-valid.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        managed_control = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(managed_control),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        pgu_config_path = tmp_path / "pgu.json"
        pgu_config_path.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "run_as_user": current_user,
                    "roles": [{"role": "ops", "slot": 0, "target": "pgu-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            otto_config = load_project_config("otto", config_path)
            pgu_config = load_project_config("pgu", pgu_config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home

        assert otto_config.control_repository == managed_control
        assert pgu_config.control_repository is None

def test_launch_without_control_repository_does_not_owner_wrap_pgu_plan() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-no-control.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "pgu.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "run_as_user": "agent",
                    "roles": [{"role": "ops", "slot": 0, "target": "pgu-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("pgu", config_path)
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()
        layout_output = tmp_path / "launch-layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        assert not any(call[:3] == ["sudo", "-u", "agent"] for call in runner.calls)
        assert not any(call[:2] == ["chown", "-R"] for call in runner.calls)
        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["args"] == konsole_launch_args(layout_output)
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        assert not any(call == konsole_launch_args(layout_output) for call in runner.calls)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_control_repo_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
