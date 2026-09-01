#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_start_auto_fast_forwards_stale_launcher_checkout_once_before_panes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        fake = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        counts = ["0 173\n", "0 0\n"]
        fast_forwarded = False
        calls: list[list[str]] = []

        def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal fast_forwarded
            calls.append(args)
            if args == git_launcher_head_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD if fast_forwarded else LOCAL_HEAD}\n")
            if args == git_launcher_ls_remote_ref_args(config, ROOT):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
            if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
                return subprocess.CompletedProcess(args, 0)
            if args == git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD):
                return subprocess.CompletedProcess(args, 0, stdout=counts.pop(0))
            if args == git_launcher_current_branch_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="main\n")
            if args == git_launcher_status_porcelain_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="")
            if args == git_launcher_head_short_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="abc1234\n" if counts else "def5678\n")
            if args == git_fast_forward_launcher_ref_args(config, ROOT):
                fast_forwarded = True
                return subprocess.CompletedProcess(args, 0)
            return fake(args, **kwargs)

        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
                konsole_process_launcher=process_launcher,
            )

        assert result == 0
        assert git_fast_forward_launcher_ref_args(config, ROOT) in calls
        assert calls.count(git_fast_forward_launcher_ref_args(config, ROOT)) == 1
        assert "auto-fast-forwarded launcher checkout" in stderr.getvalue()
        assert "was 173 commit(s) behind origin/main" in stderr.getvalue()
        layout = json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert commands
        assert all("--skip-launcher-check" in command for command in commands if " pane " in command)

def test_launcher_freshness_probe_uses_owner_runner_when_launcher_user_differs() -> None:
    original_current_user_name = team_launcher.current_user_name
    original_repo_root = team_launcher._repo_root
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-freshness.") as tmp:
            tmp_path = Path(tmp)
            config_path = _write_pgu_config_with_shared_checkout(tmp_path)
            launcher_repo = tmp_path / "repo"
            launcher_repo.mkdir()
            config = load_project_config("pgu", config_path)
            runner = SudoAwareFakeRunner()
            process_launcher = RecordingProcessLauncher()
            team_launcher.current_user_name = lambda: "root"
            team_launcher._repo_root = lambda: launcher_repo

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

        launcher_probe_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in launcher_probe_calls:
            assert ["sudo", "-u", "agent", *call] in runner.calls
            assert call not in runner.calls
    finally:
        team_launcher.current_user_name = original_current_user_name
        team_launcher._repo_root = original_repo_root

def test_launcher_freshness_probe_uses_launcher_checkout_owner_for_generated_project_shape() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-generated-freshness.") as tmp:
            tmp_path = Path(tmp)
            launcher_repo = tmp_path / "owner-home" / "otto-ticketboard-live" / "current"
            pane_launcher = launcher_repo / "scripts" / "team-launcher"
            project_repo = tmp_path / "project"
            layout = tmp_path / "layout.json"
            config_path = tmp_path / "otto.json"
            pane_launcher.parent.mkdir(parents=True)
            pane_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            pane_launcher.chmod(0o755)
            project_repo.mkdir()
            layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "project": "otto",
                        "layout": str(layout),
                        "pane_launcher": str(pane_launcher),
                        "repository": str(project_repo),
                        "run_as_user": "otto-agent",
                        "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_project_config("otto", config_path)
            checkout_owner = launcher_repo.owner()
            runner = SudoAwareFakeRunner()
            project_owner_runner = team_launcher._owner_project_git_runner(
                owner_user="otto-agent",
                project_dir=project_repo,
                owned_roots=team_launcher._control_repository_owned_roots(config),
                runner=runner,
            )
            team_launcher.current_user_name = lambda: "root"

            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                runner=project_owner_runner,
                auto_deploy=False,
            )

        launcher_probe_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in launcher_probe_calls:
            assert ["sudo", "-u", checkout_owner, *call] in runner.calls
            assert ["sudo", "-u", "otto-agent", *call] not in runner.calls
            assert call not in runner.calls
    finally:
        team_launcher.current_user_name = original_current_user_name

def test_launcher_freshness_probe_runs_direct_when_checkout_owner_is_current_user() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-current-owner-freshness.") as tmp:
            launcher_repo = Path(tmp) / "repo"
            launcher_repo.mkdir()
            config_path = _write_minimal_shared_checkout_config(Path(tmp), launcher_repo, ["ops"])
            config = load_project_config("pgu", config_path)
            runner = SudoAwareFakeRunner()
            checkout_owner = launcher_repo.owner()
            team_launcher.current_user_name = lambda: checkout_owner

            team_launcher.ensure_launcher_checkout_current(config, launcher_repo=launcher_repo, runner=runner)

        assert git_launcher_checkout_check_args(launcher_repo) in runner.calls
        assert not any(call[:3] == ["sudo", "-u", checkout_owner] for call in runner.calls)
    finally:
        team_launcher.current_user_name = original_current_user_name

def test_owner_correct_git_skips_missing_target_without_owner_rule() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-missing-git-target.") as tmp:
        missing_repo = Path(tmp) / "missing-repo"
        args = ["git", "-C", str(missing_repo), "status"]

        result = team_launcher.run_owner_correct_git(args, runner=runner)

    assert result.returncode == 125
    assert calls == []
    assert "owner-correct git skipped" in str(result.stderr)
    assert "does not exist and no owner rule matched" in str(result.stderr)

def test_owner_correct_git_skips_command_without_target_path() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    result = team_launcher.run_owner_correct_git(["git", "status"], runner=runner)

    assert result.returncode == 125
    assert calls == []
    assert "owner-correct git skipped" in str(result.stderr)
    assert "does not declare a target path" in str(result.stderr)

def test_launcher_freshness_probe_skips_when_checkout_owner_is_unknown() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    launcher_repo = Path("/tmp/pgu-launcher-owner-unknown")
    calls: list[list[str]] = []
    original_getpwuid = team_launcher.pwd.getpwuid
    try:
        team_launcher.pwd.getpwuid = lambda _uid: (_ for _ in ()).throw(KeyError(_uid))

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        stderr = StringIO()
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-unknown-owner.") as tmp:
            repo = Path(tmp)
            with redirect_stderr(stderr):
                team_launcher.ensure_launcher_checkout_current(
                    config,
                    launcher_repo=repo,
                    runner=runner,
                    auto_deploy=True,
                )
    finally:
        team_launcher.pwd.getpwuid = original_getpwuid

    assert not calls
    assert "cannot determine launcher checkout owner" in stderr.getvalue()
    assert "skipping freshness probe" in stderr.getvalue()

def test_deploy_launcher_checkout_uses_launcher_checkout_owner() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-deploy-owner.") as tmp:
            tmp_path = Path(tmp)
            launcher_repo = tmp_path / "repo"
            launcher_repo.mkdir()
            config_path = _write_minimal_shared_checkout_config(tmp_path, tmp_path / "project", ["ops"])
            config = load_project_config("pgu", config_path)
            checkout_owner = launcher_repo.owner()
            calls: list[list[str]] = []

            def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                effective_args = args[3:] if args[:3] == ["sudo", "-u", checkout_owner] else args
                if effective_args == git_launcher_head_args(launcher_repo):
                    return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\n")
                if effective_args == git_launcher_ls_remote_ref_args(config, launcher_repo):
                    return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
                return subprocess.CompletedProcess(args, 0)

            team_launcher.current_user_name = lambda: "root"

            assert team_launcher.deploy_launcher_checkout(config, launcher_repo=launcher_repo, runner=runner) == 0

        expected_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_fetch_launcher_ref_args(config, launcher_repo),
            git_checkout_launcher_branch_args(config, launcher_repo),
            git_fast_forward_launcher_ref_args(config, launcher_repo),
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in expected_calls:
            assert ["sudo", "-u", checkout_owner, *call] in calls
            assert call not in calls
    finally:
        team_launcher.current_user_name = original_current_user_name

def test_start_no_self_deploy_refuses_stale_launcher_checkout() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_head_args(ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{LOCAL_HEAD}\n")
        if args == git_launcher_ls_remote_ref_args(config, ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
        if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 173\n")
        raise AssertionError(f"unexpected call after stale launcher refusal: {args}")

    try:
        launch_project(
            config,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            runner=runner,
            no_launcher_self_deploy=True,
        )
        raise AssertionError("expected stale launcher checkout to abort")
    except SystemExit as exc:
        message = str(exc)

    assert "refusing to launch from stale checkout" in message
    assert "173 commit(s) behind origin/main" in message
    assert "deploy-launcher" in message
    assert calls == [
        git_launcher_checkout_check_args(ROOT),
        git_launcher_head_args(ROOT),
        git_launcher_ls_remote_ref_args(config, ROOT),
        git_launcher_commit_exists_args(ROOT, REMOTE_HEAD),
        git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD),
    ]

def test_allow_stale_launcher_override_warns_and_continues_without_fast_forward() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == git_launcher_checkout_check_args(ROOT):
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_head_args(ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{LOCAL_HEAD}\n")
        if args == git_launcher_ls_remote_ref_args(config, ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
        if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0, stdout="0 12\n")
        raise AssertionError(f"unexpected call after override: {args}")

    stderr = StringIO()
    with redirect_stderr(stderr):
        team_launcher.ensure_launcher_checkout_current(
            config,
            runner=runner,
            auto_deploy=False,
            allow_stale=True,
        )

    assert "OVERRIDE proceeding with stale launcher checkout" in stderr.getvalue()
    assert git_fast_forward_launcher_ref_args(config, ROOT) not in calls
    assert git_fetch_launcher_ref_args(config, ROOT) not in calls

def test_launcher_status_probe_uses_ls_remote_without_writing_git_objects() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("remote update\n", encoding="utf-8")
        _commit_all(updater, "remote update")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)

        def git_metadata_snapshot() -> dict[Path, bytes | None]:
            git_dir = launcher_repo / ".git"
            snapshot: dict[Path, bytes | None] = {}
            for path in git_dir.rglob("*"):
                relative = path.relative_to(git_dir)
                snapshot[relative] = path.read_bytes() if path.is_file() else None
            return snapshot

        git_metadata_before = git_metadata_snapshot()

        ahead, behind = team_launcher.launcher_checkout_status(config, launcher_repo=launcher_repo)

        git_metadata_after = git_metadata_snapshot()
        assert (ahead, behind) == (0, 1)
        assert git_metadata_after == git_metadata_before

def test_auto_fast_forward_launcher_checkout_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("updated\n", encoding="utf-8")
        _commit_all(updater, "update")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)
        untracked_settings = launcher_repo / ".claude" / "settings.json"
        untracked_settings.parent.mkdir()
        untracked_settings.write_text("{}\n", encoding="utf-8")

        before = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()
        stderr = StringIO()
        with redirect_stderr(stderr):
            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                auto_deploy=True,
            )
        after = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()

        assert before != after
        assert _run_git(["git", "status", "--porcelain"], cwd=launcher_repo).stdout == "?? .claude/\n"
        assert untracked_settings.read_text(encoding="utf-8") == "{}\n"
        assert "auto-fast-forwarded launcher checkout" in stderr.getvalue()

def test_auto_fast_forward_launcher_checkout_refuses_tracked_modifications() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("upstream\n", encoding="utf-8")
        _commit_all(updater, "upstream")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)
        before = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()
        (launcher_repo / "tracked.txt").write_text("local edit\n", encoding="utf-8")

        try:
            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                auto_deploy=True,
            )
            raise AssertionError("expected tracked local edit to block auto fast-forward")
        except SystemExit as exc:
            message = str(exc)
        after = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()

        assert before == after
        assert "local changes are present" in message
        assert (launcher_repo / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"

def test_undeterminable_launcher_checkout_warns_and_continues() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == git_launcher_checkout_check_args(ROOT):
            return subprocess.CompletedProcess(args, 1)
        raise AssertionError(f"unexpected call after undeterminable launcher check: {args}")

    stderr = StringIO()
    with redirect_stderr(stderr):
        team_launcher.ensure_launcher_checkout_current(config, runner=runner, auto_deploy=True)

    assert "cannot determine launcher checkout freshness" in stderr.getvalue()
    assert "continuing" in stderr.getvalue()
    assert calls == [git_launcher_checkout_check_args(ROOT)]

def test_deploy_launcher_checkout_updates_and_verifies_configured_ref() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-deploy-current-owner.") as tmp:
        launcher_repo = Path(tmp) / "pgu"
        launcher_repo.mkdir()
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == git_launcher_head_args(launcher_repo):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\n")
            if args == git_launcher_ls_remote_ref_args(config, launcher_repo):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
            if args[:3] == ["git", "-C", str(launcher_repo)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
                return subprocess.CompletedProcess(args, 0, stdout="0 0\n")
            return subprocess.CompletedProcess(args, 0)

        assert deploy_launcher_checkout(config, launcher_repo=launcher_repo, runner=runner, clean=True) == 0

        assert calls == [
            git_launcher_checkout_check_args(launcher_repo),
            git_fetch_launcher_ref_args(config, launcher_repo),
            git_checkout_launcher_branch_args(config, launcher_repo),
            git_fast_forward_launcher_ref_args(config, launcher_repo),
            git_clean_launcher_checkout_args(launcher_repo),
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_freshness_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
