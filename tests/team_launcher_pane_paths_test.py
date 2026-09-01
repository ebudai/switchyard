#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_launch_project_with_running_shared_checkout_session_skips_destructive_refresh_and_opens_window() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-running-project.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project="porter")
        config = load_project_config("porter", config_path)
        runner = FakeRunner(
            existing_sessions={"porter-director"},
            current_commands={"porter-director:0.0": "claude"},
        )
        process_launcher = RecordingProcessLauncher()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "materialized.json",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

    assert any(call == git_fetch_worktree_ref_args(config) for call in runner.calls)
    assert not any(call == git_checkout_shared_ref_args(config) for call in runner.calls)
    assert not any(call == team_launcher.git_clean_shared_checkout_args(config) for call in runner.calls)
    assert len(process_launcher.calls) == 1

def test_launch_project_with_running_control_role_session_refreshes_only_stopped_roles_and_opens_window() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-running-control-project.") as tmp:
        tmp_path = Path(tmp)
        layout_path = tmp_path / "porter-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "worktree_base": str(worktree_base),
                    "run_as_user": team_launcher.current_user_name(),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {
                            "role": "director",
                            "slot": 0,
                            "target": "porter-director:0.0",
                            "tmux_session": "porter-director",
                            "workdir": str(worktree_base / "director"),
                            "cli": ["claude"],
                        },
                        {
                            "role": "ops",
                            "slot": 1,
                            "target": "porter-ops:0.0",
                            "tmux_session": "porter-ops",
                            "workdir": str(worktree_base / "ops"),
                            "cli": ["codex"],
                        },
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
        finally:
            team_launcher._control_repository_owner_home = original_home
        runner = FakeRunner(
            existing_sessions={"porter-director"},
            current_commands={"porter-director:0.0": "claude"},
        )
        process_launcher = RecordingProcessLauncher()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "materialized.json",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

    director = team_launcher._role_by_name(config, "director")
    ops = team_launcher._role_by_name(config, "ops")
    assert team_launcher.git_control_worktree_add_args(config, ops) in runner.calls
    assert team_launcher.git_control_worktree_add_args(config, director) not in runner.calls
    assert team_launcher.git_role_worktree_reset_args(config, director) not in runner.calls
    assert team_launcher.git_clean_role_worktree_args(director) not in runner.calls
    assert len(process_launcher.calls) == 1

def test_kde_auto_layout_preserves_separate_konsole_plan_shape() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    runner = FakeRunner()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-kde-layout.") as tmp:
        tmp_path = Path(tmp)
        layout_output = tmp_path / "layout.json"
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    dry_run=True,
                    layout_output=layout_output,
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                )
                == 0
            )

        plan = json.loads(stdout.getvalue())
        layout = json.loads(layout_output.read_text(encoding="utf-8"))

    assert runner.calls == []
    assert "layout_mode" not in plan
    assert "viewer_session" not in plan
    assert [role["target"] for role in plan["roles"]] == [
        "pgu-director:0.0",
        "pgu-main:0.0",
        "pgu-app:0.0",
        "pgu-ops:0.0",
        "pgu-audit:0.0",
        "pgu-inspector:0.0",
    ]
    assert len(_leaf_commands(layout)) == 6

def test_pgu_config_matches_director_supplied_live_role_assignments() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    layout = json.loads(config.layout.read_text(encoding="utf-8"))
    expected_repository = Path("/home/agent/Projects/pgu")

    assert set(roles) == {"director", "main", "app", "research", "ops", "audit", "inspector"}
    assert len(roles) == 7
    assert len([role for role in roles.values() if not role.detached]) == 6
    assert len(team_launcher._layout_leaves(layout)) == 6
    assert all(role.yolo for role in roles.values())
    assert config.repository == expected_repository
    assert config.board_url == "http://127.0.0.1:8770"
    assert config.board_socket == "/run/pgu-ticket-board/ticket-board.sock"
    assert config.run_as_user == "agent"
    assert config.control_repository is None
    assert config.worktree_base is None
    assert worktree_ref(config) == "origin/main"
    assert roles["research"].detached
    assert roles["research"].slot is None
    assert {role.role: role.slot for role in roles.values() if not role.detached} == {
        "director": 2,
        "main": 1,
        "app": 4,
        "ops": 5,
        "audit": 3,
        "inspector": 0,
    }
    for role_name, role in roles.items():
        assert role.workdir == str(expected_repository), role_name
    assert (roles["director"].cli, roles["director"].model, roles["director"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["director"].resume_mode, roles["director"].resume_flag) == ("flag", "--resume")
    assert (roles["ops"].cli, roles["ops"].model, roles["ops"].effort, roles["ops"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["ops"].resume_mode, roles["ops"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["main"].cli, roles["main"].model, roles["main"].effort, roles["main"].extra_args) == (
        ["codex"],
        "gpt-5.6-sol",
        "high",
        [],
    )
    assert (roles["main"].resume_mode, roles["main"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["app"].cli, roles["app"].model, roles["app"].effort, roles["app"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["app"].resume_mode, roles["app"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["research"].cli, roles["research"].model, roles["research"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["research"].resume_mode, roles["research"].resume_flag) == ("flag", "--resume")
    assert (roles["audit"].cli, roles["audit"].model, roles["audit"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["audit"].resume_mode, roles["audit"].resume_flag) == ("flag", "--resume")
    assert (roles["inspector"].cli, roles["inspector"].model, roles["inspector"].effort) == (
        ["agy"],
        "gemini-3.7-flash-high",
        "",
    )
    assert (roles["inspector"].resume_mode, roles["inspector"].resume_flag) == ("flag", "--conversation")

def test_pgu_config_keeps_live_agent_repository_with_foreign_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = "/home/user"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == "agent"
    assert config.repository == Path("/home/agent/Projects/pgu")
    assert {role.workdir for role in config.roles} == {"/home/agent/Projects/pgu"}

def test_default_session_dir_uses_run_as_user_home_when_launcher_default_is_foreign() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-session.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_getpwnam = team_launcher.pwd.getpwnam

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.pwd.getpwnam = original_getpwnam

    expected_session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == expected_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in entries
    assert not any("/root/.local/state/pgu-ticket-board/pane-sessions" in entry for entry in entries)

def test_explicit_session_dir_env_beats_run_as_user_default() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-env-session.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        env_session_dir = tmp_path / "env" / "sessions"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        env_keys = ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_getpwnam = team_launcher.pwd.getpwnam

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = str(env_session_dir)
            os.environ.pop("PGU_TICKET_BOARD_PANE_SESSION_DIR", None)
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.pwd.getpwnam = original_getpwnam

    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == env_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={env_session_dir}" in entries
    assert not any(str(owner_home) in entry for entry in entries)
    assert not any("/root/.local/state/pgu-ticket-board/pane-sessions" in entry for entry in entries)

def test_configured_session_dir_beats_inherited_session_dir_env() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-config-session.") as tmp:
        tmp_path = Path(tmp)
        configured_session_dir = tmp_path / "config" / "sessions"
        inherited_session_dir = tmp_path / "inherited" / "sessions"
        legacy_inherited_session_dir = tmp_path / "legacy-inherited" / "sessions"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(configured_session_dir),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        env_keys = ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
        try:
            os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = str(inherited_session_dir)
            os.environ["PGU_TICKET_BOARD_PANE_SESSION_DIR"] = str(legacy_inherited_session_dir)
            config = load_project_config("porter", config_path)
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == configured_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={configured_session_dir}" in entries
    assert not any(str(inherited_session_dir) in entry for entry in entries)
    assert not any(str(legacy_inherited_session_dir) in entry for entry in entries)

def test_launch_project_default_pane_state_dir_uses_run_as_user_runtime_when_launcher_default_is_foreign() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-pane-state.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        root_pane_state = tmp_path / "run" / "user" / "0" / "pgu-ticket-board" / "pane-state"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_default_pane_state_dir = team_launcher.DEFAULT_PANE_STATE_DIR
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        class OwnerAwareRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args[:3] == ["sudo", "-u", "porter-agent"]:
                    self.calls.append(args)
                    inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return result
                return super().__call__(args, **kwargs)

        try:
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.DEFAULT_PANE_STATE_DIR = root_pane_state
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = OwnerAwareRunner()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-output.json",
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                )
                == 0
            )
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        expected_pane_state_dir = owner_runtime / "porter-ticket-board" / "pane-state"
        expected_call = [
            "sudo",
            "-u",
            "porter-agent",
            "-H",
            str(ROOT / "scripts" / "team-launcher"),
            "porter",
            "pane",
            "attach-or-start",
            "ops",
            "--config",
            str(config_path),
            "--skip-launcher-check",
            "--no-attach",
            "--pane-state-dir",
            str(expected_pane_state_dir),
        ]
        assert expected_call in runner.calls
        assert not (root_pane_state / pane_state_file_name("porter-ops:0.0")).exists()

def test_root_created_owner_state_dirs_are_owned_by_run_as_user() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-chown.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        root_session_dir = tmp_path / "root" / "sessions"
        root_pane_state = tmp_path / "run" / "user" / "0" / "pgu-ticket-board" / "pane-state"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_default_pane_state_dir = team_launcher.DEFAULT_PANE_STATE_DIR
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242
            pw_gid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        class OwnershipRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.installed_dirs: dict[Path, tuple[str, str, int]] = {}

            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["install", "-d"]:
                    self.calls.append(args)
                    mode = int(args[args.index("-m") + 1], 8)
                    owner = args[args.index("-o") + 1]
                    group = args[args.index("-g") + 1]
                    target = Path(args[-1])
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(mode)
                    self.installed_dirs[target] = (owner, group, mode)
                    return subprocess.CompletedProcess(args, 0)
                if args[:3] == ["sudo", "-u", "porter-agent"]:
                    self.calls.append(args)
                    inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return result
                return super().__call__(args, **kwargs)

        try:
            team_launcher.DEFAULT_SESSION_DIR = root_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = root_pane_state
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = OwnershipRunner()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-output.json",
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                )
                == 0
            )
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        expected_session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        expected_pane_state_dir = owner_runtime / "porter-ticket-board" / "pane-state"
        expected_session_install = [
            "install",
            "-d",
            "-m",
            "700",
            "-o",
            "porter-agent",
            "-g",
            "porter-agent",
            str(expected_session_dir),
        ]
        expected_pane_state_install = [
            "install",
            "-d",
            "-m",
            "700",
            "-o",
            "porter-agent",
            "-g",
            "porter-agent",
            str(expected_pane_state_dir),
        ]
        expected_pane_start = [
            "sudo",
            "-u",
            "porter-agent",
            "-H",
            str(ROOT / "scripts" / "team-launcher"),
            "porter",
            "pane",
            "attach-or-start",
            "ops",
            "--config",
            str(config_path),
            "--skip-launcher-check",
            "--no-attach",
            "--pane-state-dir",
            str(expected_pane_state_dir),
        ]
        assert expected_session_dir.is_dir()
        assert expected_pane_state_dir.is_dir()
        assert expected_pane_start in runner.calls
        assert not root_session_dir.exists()
        assert not root_pane_state.exists()
        assert runner.calls.count(expected_session_install) == 2
        assert runner.calls.count(expected_pane_state_install) == 2
        assert runner.installed_dirs[expected_session_dir] == ("porter-agent", "porter-agent", 0o700)
        assert runner.installed_dirs[expected_pane_state_dir] == ("porter-agent", "porter-agent", 0o700)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_pane_paths_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
