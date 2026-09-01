#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_owner_state_dir_install_args_take_effect_on_real_filesystem_for_current_user() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-real.") as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "porter.json"
        layout_path = tmp_path / "layout.json"
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": current_user,
                    "layout": str(layout_path),
                    "repository": str(repo_path),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        target = tmp_path / "owner-home" / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"

        result = subprocess.run(team_launcher.install_owner_state_dir_args(config, target), text=True, capture_output=True)

        assert result.returncode == 0, result.stderr
        info = target.stat()
        assert target.owner() == current_user
        assert target.group() == current_user
        assert info.st_mode & 0o777 == 0o700

def test_owner_state_dir_install_is_skipped_when_already_running_as_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-same-user.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        config_path = tmp_path / "porter.json"
        layout_path = tmp_path / "layout.json"
        repo_path = tmp_path / "repo"
        session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        pane_state_dir = owner_runtime / "pgu-ticket-board" / "pane-state"
        repo_path.mkdir()
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout_path),
                    "repository": str(repo_path),
                    "session_dir": str(session_dir),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_current_user_name = team_launcher.current_user_name
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242
            pw_gid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            team_launcher.current_user_name = lambda: "porter-agent"
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            config = load_project_config("porter", config_path)
            runner = FakeRunner()

            team_launcher.ensure_owner_state_dirs(config, pane_state_dir=pane_state_dir, runner=runner)
        finally:
            team_launcher.current_user_name = original_current_user_name
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid

        assert not any(call[:2] == ["install", "-d"] for call in runner.calls)

def test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-env-pane-state.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        env_pane_state = tmp_path / "env" / "pane-state"
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
        env_keys = ("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
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
            os.environ["TICKET_BOARD_PANE_STATE_DIR"] = str(env_pane_state)
            os.environ.pop("PGU_TICKET_BOARD_PANE_STATE_DIR", None)
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
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

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
            str(env_pane_state),
        ]
        owner_state = owner_runtime / "pgu-ticket-board" / "pane-state" / pane_state_file_name("porter-ops:0.0")
        assert expected_call in runner.calls
        assert not owner_state.exists()
        assert not (root_pane_state / pane_state_file_name("porter-ops:0.0")).exists()

def test_cli_command_prepends_invoking_user_bin() -> None:
    original_home = os.environ.get("HOME")
    original_generic_bin_dir = os.environ.pop("TEAM_LAUNCHER_BIN_DIR", None)
    original_bin_dir = os.environ.pop("PGU_TEAM_LAUNCHER_BIN_DIR", None)
    try:
        os.environ["HOME"] = "/home/otto-agent"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
        role = next(role for role in config.roles if role.role == "ops")

        command = cli_command_for_role(role, session_dir=config.session_dir)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
        if original_generic_bin_dir is not None:
            os.environ["TEAM_LAUNCHER_BIN_DIR"] = original_generic_bin_dir
        if original_bin_dir is not None:
            os.environ["PGU_TEAM_LAUNCHER_BIN_DIR"] = original_bin_dir

    assert any(entry.startswith("PATH=/home/otto-agent/bin:") for entry in _env_entries(command))

def test_cli_command_with_explicit_owner_uses_owner_bin_and_drops_invoker_private_path() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    original_path = os.environ.get("PATH")
    original_generic_bin_dir = os.environ.pop("TEAM_LAUNCHER_BIN_DIR", None)
    original_bin_dir = os.environ.pop("PGU_TEAM_LAUNCHER_BIN_DIR", None)
    original_getpwnam = team_launcher.pwd.getpwnam
    original_current_user_name = team_launcher.current_user_name

    class OwnerPw:
        pw_dir = "/home/porter-agent"

    def fake_getpwnam(user_name: str) -> Any:
        if user_name == "porter-agent":
            return OwnerPw()
        return original_getpwnam(user_name)

    try:
        os.environ["PATH"] = "/root/bin:/usr/bin"
        team_launcher.pwd.getpwnam = fake_getpwnam
        team_launcher.current_user_name = lambda: "root"
        command = cli_command_for_role(
            role,
            session_dir=Path("/home/porter-agent/.local/state/pgu-ticket-board/pane-sessions"),
            pane_state_dir=Path("/run/user/4242/pgu-ticket-board/pane-state"),
            bin_user="porter-agent",
        )
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path
        if original_generic_bin_dir is not None:
            os.environ["TEAM_LAUNCHER_BIN_DIR"] = original_generic_bin_dir
        if original_bin_dir is not None:
            os.environ["PGU_TEAM_LAUNCHER_BIN_DIR"] = original_bin_dir
        team_launcher.pwd.getpwnam = original_getpwnam
        team_launcher.current_user_name = original_current_user_name

    path = next(entry.split("=", 1)[1] for entry in _env_entries(command) if entry.startswith("PATH="))
    assert path.startswith("/home/porter-agent/bin:")
    assert "/root/bin" not in path.split(":")

def test_modern_env_names_win_over_legacy_aliases_with_legacy_fallback() -> None:
    def wayland_from_command(command: list[str]) -> str:
        return next(entry.split("=", 1)[1] for entry in command if entry.startswith("WAYLAND_DISPLAY="))

    def konsole_wayland() -> str:
        return wayland_from_command(konsole_launch_args(Path("/tmp/layout.json"), gui_user="user"))

    cases = [
        {
            "name": "user bin",
            "modern": team_launcher.USER_BIN_ENV,
            "legacy": team_launcher.LEGACY_USER_BIN_ENV,
            "modern_value": "/modern/bin",
            "legacy_value": "/legacy/bin",
            "resolve": default_user_bin,
            "expected_modern": "/modern/bin",
            "expected_legacy": "/legacy/bin",
        },
        {
            "name": "gui user",
            "modern": team_launcher.GUI_USER_ENV,
            "legacy": team_launcher.LEGACY_GUI_USER_ENV,
            "modern_value": "modern-user",
            "legacy_value": "legacy-user",
            "resolve": team_launcher.default_gui_user,
            "expected_modern": "modern-user",
            "expected_legacy": "legacy-user",
        },
        {
            "name": "configured wayland display",
            "modern": team_launcher.GUI_WAYLAND_ENV,
            "legacy": team_launcher.LEGACY_GUI_WAYLAND_ENV,
            "modern_value": "wayland-modern",
            "legacy_value": "wayland-legacy",
            "resolve": konsole_wayland,
            "expected_modern": "/run/user/4242/wayland-modern",
            "expected_legacy": "/run/user/4242/wayland-legacy",
        },
        {
            "name": "host wayland display",
            "modern": team_launcher.HOST_WAYLAND_ENV,
            "legacy": team_launcher.LEGACY_HOST_WAYLAND_ENV,
            "modern_value": "/run/user/1000/wayland-modern-host",
            "legacy_value": "/run/user/1001/wayland-legacy-host",
            "resolve": konsole_wayland,
            "expected_modern": "/run/user/1000/wayland-modern-host",
            "expected_legacy": "/run/user/1001/wayland-legacy-host",
        },
        {
            "name": "pane session dir",
            "modern": "TICKET_BOARD_PANE_SESSION_DIR",
            "legacy": "PGU_TICKET_BOARD_PANE_SESSION_DIR",
            "modern_value": "/tmp/modern-pane-sessions",
            "legacy_value": "/tmp/legacy-pane-sessions",
            "resolve": lambda: str(team_launcher.default_session_dir_for_user("owner")),
            "expected_modern": "/tmp/modern-pane-sessions",
            "expected_legacy": "/tmp/legacy-pane-sessions",
        },
        {
            "name": "pane state dir",
            "modern": "TICKET_BOARD_PANE_STATE_DIR",
            "legacy": "PGU_TICKET_BOARD_PANE_STATE_DIR",
            "modern_value": "/tmp/modern-pane-state",
            "legacy_value": "/tmp/legacy-pane-state",
            "resolve": lambda: str(team_launcher.default_pane_state_dir_for_user("owner", project="pgu")),
            "expected_modern": "/tmp/modern-pane-state",
            "expected_legacy": "/tmp/legacy-pane-state",
        },
    ]
    env_keys = {
        key
        for case in cases
        for key in (str(case["modern"]), str(case["legacy"]))
    }
    original_env = {key: os.environ.get(key) for key in env_keys}
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None
        for case in cases:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ[str(case["modern"])] = str(case["modern_value"])
            os.environ[str(case["legacy"])] = str(case["legacy_value"])
            assert case["resolve"]() == case["expected_modern"], case["name"]

            os.environ.pop(str(case["modern"]))
            assert case["resolve"]() == case["expected_legacy"], case["name"]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def test_cli_command_exports_durable_session_dir_for_pane_hooks() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    command = cli_command_for_role(role, session_dir=config.session_dir)

    entries = _env_entries(command)
    assert entries[0] == "TICKET_BOARD_PANE_TARGET=pgu-ops:0.0"
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in entries
    assert f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
    assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
    assert str(config.session_dir) == str(Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions")
    assert not any(entry.startswith("TICKET_BOARD_PANE_SESSION_ID=") for entry in entries)

def test_cli_command_exports_known_resume_session_id_for_hooks() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = Path(tmp) / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )

        command = cli_command_for_role(role, session_dir=session_dir, resume=True)

    entries = _env_entries(command)
    assert f"TICKET_BOARD_PANE_SESSION_ID={session_id}" in entries
    assert f"PGU_PANE_SESSION_ID={session_id}" in entries
    assert _command_tail(command)[:3] == ["codex", "resume", session_id]

def test_cli_command_clears_ambient_pane_identity_before_target_identity() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    with tempfile.TemporaryDirectory(prefix="pgu-cli-command-pane-identity.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        session_dir.mkdir()
        session_id = "research-session-id"
        transcript = tmp_path / "research.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )

        command = cli_command_for_role(role, session_dir=session_dir, pane_state_dir=pane_state_dir, resume=True)

    unsets = _env_unsets(command)
    assert set(team_launcher.PANE_TARGET_ENV_KEYS).issubset(unsets)
    assert "TMUX" not in unsets
    assert "TMUX_PANE" not in unsets
    entries = _env_entries(command)
    assert f"TICKET_BOARD_PANE_TARGET={role.target}" in entries
    assert f"PGU_PANE_TARGET={role.target}" in entries
    assert f"TICKET_BOARD_PANE_SESSION_ID={session_id}" in entries
    assert f"PGU_PANE_SESSION_ID={session_id}" in entries
    assert _command_tail(command)[:3] == ["claude", "--resume", session_id]

def test_owner_cli_probe_and_interactive_strip_caller_pane_identity() -> None:
    calls: list[list[str]] = []
    call_kwargs: list[dict[str, Any]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        call_kwargs.append(dict(kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    owner = team_launcher.current_user_name()
    original_env = {key: os.environ.get(key) for key in team_launcher.PROBE_IDENTITY_ENV_KEYS}
    with tempfile.TemporaryDirectory(prefix="pgu-owner-cli-probe-env.") as tmp:
        try:
            for key in team_launcher.PROBE_IDENTITY_ENV_KEYS:
                os.environ[key] = f"ambient-{key}"
            owner_home = Path(tmp)
            probe = team_launcher._run_owner_cli_probe(
                owner_user=owner,
                owner_home=owner_home,
                command=["agy", "--model", "gemini-3.7-flash-high", "-p", "model-ok"],
                runner=runner,
            )
            interactive = team_launcher._run_owner_cli_interactive(
                owner_user=owner,
                cwd=owner_home,
                command=["claude", "login"],
                runner=runner,
            )
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    assert probe.returncode == 0
    assert interactive.returncode == 0
    assert calls == [
        ["agy", "--model", "gemini-3.7-flash-high", "-p", "model-ok"],
        ["claude", "login"],
    ]
    assert len(call_kwargs) == 2
    for kwargs in call_kwargs:
        env = kwargs["env"]
        assert isinstance(env, dict)
        for key in team_launcher.PROBE_IDENTITY_ENV_KEYS:
            assert key not in env

def test_custom_config_without_run_as_user_tracks_invoking_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            tmp_path = Path(tmp)
            layout = {"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}
            layout_path = tmp_path / "layout.json"
            layout_path.write_text(json.dumps(layout), encoding="utf-8")
            config_path = tmp_path / "porter.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "porter",
                        "layout": str(layout_path),
                        "repository": "$HOME/Projects/porter",
                        "roles": [{"role": "ops", "slot": 0, "cli": ["codex"]}],
                    }
                ),
                encoding="utf-8",
            )
            os.environ["HOME"] = "/home/otto-agent"

            config = load_project_config("porter", config_path)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == ""
    assert config.board_url == "http://127.0.0.1:23682"
    assert config.board_socket == "/run/porter-ticket-board/ticket-board.sock"
    assert config.repository == Path("/home/otto-agent/Projects/porter")
    assert config.roles[0].workdir == "/home/otto-agent/Projects/porter"

def test_custom_project_pane_env_is_wired_to_project_board_and_role_map() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        layout = {"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": "/home/otto-agent/Projects/otto",
                    "roles": [
                        {"role": "director", "slot": 0, "tmux_session": "otto-director", "cli": ["claude"]},
                        {"role": "implementer", "slot": 1, "tmux_session": "otto-implementer", "cli": ["codex"]},
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = load_project_config("otto", config_path)
        role = next(role for role in config.roles if role.role == "implementer")
        command = cli_command_for_role(role, session_dir=config.session_dir)

    entries = _env_entries(command)
    role_map = json.loads(next(entry.split("=", 1)[1] for entry in entries if entry.startswith("TICKET_BOARD_CALLER_ROLE_MAP=")))
    assert config.board_url == "http://127.0.0.1:20740"
    assert config.board_socket == "/run/otto-ticket-board/ticket-board.sock"
    assert f"TICKET_BOARD_PANE_TARGET={role.target}" in entries
    assert "TICKET_BOARD_PROJECT=otto" in entries
    assert "TICKET_BOARD_URL=http://127.0.0.1:20740" in entries
    assert "TICKET_BOARD_SOCKET=/run/otto-ticket-board/ticket-board.sock" in entries
    assert "TICKET_BOARD_CALLER_ROLE=implementer" in entries
    assert role_map == {"otto-director": "director", "otto-implementer": "implementer"}
    assert not any(entry.startswith("PGU_TICKET_BOARD_SOCKET=") for entry in entries)
    assert not any(entry.startswith("PGU_TICKET_BOARD_URL=") for entry in entries)

def test_pane_state_filename_matches_notify_listener_target_path() -> None:
    target = "pgu-ops:0.0"
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        listener_path = PaneHookStateStore(Path(tmp))._target_path(target)

    assert pane_state_file_name(target) == listener_path.name == "pgu-ops_0.0.json"

def test_ensure_user_linger_runtime_enables_linger_and_waits_for_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []
    checks: list[Path] = []
    sleeps: list[float] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def runtime_exists(path: Path) -> bool:
        checks.append(path)
        return len(checks) == 3

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        runtime_dir = ensure_user_linger_runtime(
            "otto-agent",
            runner=runner,
            runtime_exists=runtime_exists,
            sleeper=sleeps.append,
            attempts=5,
            poll_seconds=0.25,
        )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]
    assert checks == [Path("/run/user/1005"), Path("/run/user/1005"), Path("/run/user/1005")]
    assert sleeps == [0.25, 0.25]

def test_ensure_user_linger_runtime_fails_loud_when_loginctl_is_denied() -> None:
    original_uid_for_user = team_launcher.uid_for_user

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Access denied")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        try:
            ensure_user_linger_runtime(
                "otto-agent",
                runner=runner,
                runtime_exists=lambda _path: False,
                sleeper=lambda _seconds: None,
                attempts=1,
            )
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert "failed to enable linger for 'otto-agent': Access denied" in message
    assert "sudo loginctl enable-linger otto-agent" in message

def test_configured_runtime_check_skips_non_runtime_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
        config = load_project_config("pgu", config_path)

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected runtime provisioning call: {args}")

        assert ensure_configured_runtime_user(config, runner=runner) is None

def test_configured_runtime_check_uses_run_as_user_for_default_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            raw["session_dir"] = "/run/user/1005/pgu-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            runtime_dir = ensure_configured_runtime_user(
                config,
                runner=runner,
                runtime_exists=lambda path: path == Path("/run/user/1005"),
                sleeper=lambda _seconds: None,
                attempts=1,
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]

def test_provision_runtime_command_uses_configured_project_user() -> None:
    original_ensure = team_launcher.ensure_user_linger_runtime
    calls: list[str] = []

    def fake_ensure(user_name: str) -> Path:
        calls.append(user_name)
        return Path("/run/user/1005")

    try:
        team_launcher.ensure_user_linger_runtime = fake_ensure
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            assert provision_runtime_command(None, config) == 0
    finally:
        team_launcher.ensure_user_linger_runtime = original_ensure

    assert calls == ["otto-agent"]

def test_yolo_config_translates_to_cli_specific_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    expected_flags = {
        "director": ["--dangerously-skip-permissions"],
        "main": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "app": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "research": ["--dangerously-skip-permissions"],
        "ops": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "audit": ["--dangerously-skip-permissions"],
        "inspector": ["--dangerously-skip-permissions"],
    }

    for role_name, flags in expected_flags.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        for expected_flag in flags:
            assert expected_flag in command, (role_name, command)

def test_project_config_rejects_unsupported_cli_names() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-dead-cli.") as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "porter.json"

        for cli_name in ("gemini", "gemeni", "totally-made-up"):
            config_path.write_text(
                json.dumps(
                    {
                        "project": "porter",
                        "layout": str(tmp_path / "layout.json"),
                        "roles": [
                            {
                                "role": "inspector",
                                "slot": 0,
                                "target": "porter-inspector:0.0",
                                "cli": [cli_name],
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            try:
                load_project_config("porter", config_path)
                raise AssertionError(f"expected unsupported CLI {cli_name!r} to be rejected")
            except SystemExit as exc:
                message = str(exc)

            assert f"role inspector cli {cli_name!r} is not supported" in message
            assert "supported clis: agy, claude, codex, hermes" in message

def test_project_config_accepts_hermes_runtime_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-hermes.") as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(tmp_path / "layout.json"),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {
                            "role": "bulk",
                            "slot": 0,
                            "target": "porter-bulk:0.0",
                            "cli": ["/home/eric/.local/bin/hermes"],
                            "model": "zai/glm-5.3",
                            "effort": "xhigh",
                            "yolo": True,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        session_id = "20260823_140512_b49a1a"
        session_dir = tmp_path / "sessions"

        config = load_project_config("porter", config_path)
        role = config.roles[0]
        session_dir.mkdir()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )

        command = cli_command_for_role(role, session_dir=session_dir, resume=True)

    entries = _env_entries(command)
    tail = _command_tail(command)
    assert role.model_arg == "-m"
    assert (role.resume_mode, role.resume_flag) == ("flag", "--resume")
    assert not any(entry.startswith("TICKET_BOARD_PANE_SESSION_ID=") for entry in entries)
    assert not any(entry.startswith("PGU_PANE_SESSION_ID=") for entry in entries)
    assert tail == [
        "/home/eric/.local/bin/hermes",
        "-m",
        "zai/glm-5.3",
        "--reasoning",
        "xhigh",
        "--yolo",
        "--accept-hooks",
        "--pass-session-id",
    ]

def test_effort_config_translates_to_cli_specific_args() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    for role_name in ("director", "research", "audit"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "--effort" in command, (role_name, command)
        assert command[command.index("--effort") + 1] == "high"
        assert "reasoning_effort=high" not in command

    for role_name in ("main", "app", "ops"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "-c" in command, (role_name, command)
        assert command[command.index("-c") + 1] == "reasoning_effort=high"
        assert "--effort" not in command

    inspector_command = cli_command_for_role(roles["inspector"], session_dir=config.session_dir)
    assert "--effort" not in inspector_command
    assert "-c" not in inspector_command
    assert "reasoning_effort=high" not in inspector_command

def test_inspector_agy_cli_stays_in_first_run_trust_phase() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    inspector = next(role for role in config.roles if role.role == "inspector")

    assert team_launcher._role_cli_name(inspector) == "agy"
    assert team_launcher._role_cli_name(inspector) in team_launcher.FIRST_RUN_TRUST_CLIS
    assert "gemini" not in team_launcher.FIRST_RUN_TRUST_CLIS

def test_pgu_launch_commands_include_model_and_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_by_role = {
        "director": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "main": [
            "codex",
            "--model",
            "gpt-5.6-sol",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "app": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "research": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "ops": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "audit": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "inspector": ["agy", "--model", "gemini-3.7-flash-high", "--dangerously-skip-permissions"],
    }

    for role_name, expected_tail in expected_by_role.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        entries = _env_entries(command)
        assert entries[0] == f"TICKET_BOARD_PANE_TARGET=pgu-{role_name}:0.0"
        assert f"PGU_PANE_TARGET=pgu-{role_name}:0.0" in entries
        assert f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
        assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
        assert any(entry.startswith(f"PATH={default_user_bin()}:") for entry in entries), (role_name, command)
        assert _command_tail(command) == expected_tail, (role_name, command)
        assert "--reasoning-effort" not in command
        assert "--continue" not in command
        assert "--last" not in command
        assert "gemini" not in command
        assert "--yolo" not in command

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_env_config_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
