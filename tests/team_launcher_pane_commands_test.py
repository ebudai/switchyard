#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_dry_run_materializes_pgu_layout_with_six_visible_role_commands() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        layout_output = Path(tmp) / "layout.json"
        result = launch_project(
            config,
            config_path=config_path,
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            dry_run=True,
            layout_output=layout_output,
        )

        assert result == 0
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 6
        active_commands = [command for command in commands if command]
        assert len(active_commands) == 6
        clockwise_commands = [commands[index] for index in PANEL_CLOCKWISE_SLOT_ORDER]
        expected_clockwise_roles = ["inspector", "director", "audit", "ops", "app", "main"]
        assert config.pane_launcher is None
        for role in ("director", "main", "app", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        for role, command in zip(expected_clockwise_roles, clockwise_commands):
            assert f" {role} " in f" {command} ", (role, clockwise_commands)
        assert not any(" research " in f" {command} " for command in commands)
        assert not any(" perf " in f" {command} " for command in commands)

def test_pane_command_sudos_to_configured_owner_when_launcher_user_differs() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "root"
        command = team_launcher.pane_command(
            config.project,
            role,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            skip_launcher_check=True,
            run_as_user="agent",
        )
    finally:
        team_launcher.current_user_name = original_current_user_name

    tokens = shlex.split(command)
    assert tokens[:4] == ["sudo", "-u", "agent", "-H"]
    assert tokens[4:] == [
        str(ROOT / "scripts" / "team-launcher"),
        "pgu",
        "pane",
        "attach-or-start",
        "ops",
        "--config",
        str(ROOT / "config" / "team-launcher" / "pgu.json"),
        "--skip-launcher-check",
    ]

def test_pane_command_does_not_sudo_when_already_owner() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "agent"
        command = team_launcher.pane_command(
            config.project,
            role,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            skip_launcher_check=True,
            run_as_user="agent",
        )
    finally:
        team_launcher.current_user_name = original_current_user_name

    tokens = shlex.split(command)
    assert tokens[:4] != ["sudo", "-u", "agent", "-H"]
    assert tokens[0] == str(ROOT / "scripts" / "team-launcher")

def test_visible_layout_command_stays_owner_wrapped_without_forcing_pane_state_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-visible-owner-command.") as tmp:
        tmp_path = Path(tmp)
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
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        output = tmp_path / "out.json"
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "root"
            materialize_layout(
                config,
                config_path=config_path,
                mode="attach-or-start",
                script_path=ROOT / "scripts" / "team-launcher",
                output_path=output,
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

        command = json.loads(output.read_text(encoding="utf-8"))["Command"]
        assert shlex.split(command) == team_launcher.pane_command_args(
            "porter",
            role,
            config_path=config_path,
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            skip_launcher_check=True,
            run_as_user="porter-agent",
        )
        assert "--pane-state-dir" not in shlex.split(command)

def test_launch_project_default_layout_uses_project_scoped_switchyard_not_tmp() -> None:
    project = f"layoutsafe{os.getpid()}"
    stale_tmp_layout = Path(tempfile.gettempdir()) / f"{project}-team-layout.json"
    stale_tmp_layout.write_text("stale shared tmp layout\n", encoding="utf-8")
    stale_tmp_layout.chmod(0o444)
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-safe.") as tmp:
            tmp_path = Path(tmp)
            config_path = _write_six_visible_role_config(tmp_path, project=project)
            config = load_project_config(project, config_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=FakeRunner(),
                        dry_run=True,
                    )
                    == 0
                )

            expected_layout = config_path.parent / ".switchyard" / project / f"{project}-team-layout.json"
            plan = json.loads(stdout.getvalue())
            assert plan["layout"] == str(expected_layout)
            assert expected_layout.is_file()
            assert stale_tmp_layout.read_text(encoding="utf-8") == "stale shared tmp layout\n"
    finally:
        stale_tmp_layout.chmod(0o644)
        stale_tmp_layout.unlink(missing_ok=True)

def test_default_layout_paths_are_project_scoped() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-scope.") as tmp:
        tmp_path = Path(tmp)
        porter_root = tmp_path / "porter-user"
        zeta_root = tmp_path / "zeta-user"
        porter_root.mkdir()
        zeta_root.mkdir()
        porter_config_path = _write_six_visible_role_config(porter_root, project="porter")
        zeta_config_path = _write_six_visible_role_config(zeta_root, project="zeta")
        porter = load_project_config("porter", porter_config_path)
        zeta = load_project_config("zeta", zeta_config_path)

        porter_layout = team_launcher.default_layout_output_path(porter, config_path=porter_config_path)
        zeta_layout = team_launcher.default_layout_output_path(zeta, config_path=zeta_config_path)

    assert porter_layout == porter_config_path.parent / ".switchyard" / "porter" / "porter-team-layout.json"
    assert zeta_layout == zeta_config_path.parent / ".switchyard" / "zeta" / "zeta-team-layout.json"
    assert porter_layout != zeta_layout

def test_default_layout_path_for_run_as_user_lives_outside_shared_checkout() -> None:
    original_getpwnam = team_launcher.pwd.getpwnam
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-owner.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        config_path = _write_six_visible_role_config(tmp_path, project="porter")
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["run_as_user"] = "porter-agent"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_uid = os.getuid()

        try:
            def fake_getpwnam(user_name: str) -> object:
                return FakeUserInfo() if user_name == "porter-agent" else original_getpwnam(user_name)

            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
            layout = team_launcher.default_layout_output_path(config, config_path=config_path)
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

    assert layout == owner_home / ".local" / "state" / "switchyard" / "projects" / "porter" / "porter-team-layout.json"
    assert config.repository is not None
    assert not layout.is_relative_to(config.repository)

def test_launch_project_uses_configured_owner_readable_pane_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        control_repo = tmp_path / "owner-home" / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "control_repository": str(control_repo),
                    "run_as_user": "porter-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path / "owner-home"
            config = load_project_config("porter", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

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

        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls
        commands = _leaf_commands(json.loads(layout_output.read_text(encoding="utf-8")))
        assert len(commands) == 1
        assert str(owner_launcher) in commands[0]
        assert str(ROOT / "scripts" / "team-launcher") not in commands[0]
        assert "--skip-launcher-check" in commands[0]

def test_launch_project_probes_pane_launcher_as_owner_without_control_repository() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-no-control.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "run_as_user": "porter-agent",
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

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

        assert config.control_repository is None
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls

def test_launch_project_probes_pane_launcher_as_owner_without_repository() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-no-repo.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "run_as_user": "porter-agent",
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

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

        assert config.repository is None
        assert config.control_repository is None
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls

def test_launch_project_refuses_missing_configured_pane_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-missing.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        control_repo = tmp_path / "owner-home" / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "control_repository": str(control_repo),
                    "run_as_user": "porter-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path / "owner-home"
            config = load_project_config("porter", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        layout_output = tmp_path / "materialized.json"
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)]:
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)

        try:
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
            )
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)

        assert str(owner_launcher) in message
        assert "configured pane_launcher" in message
        assert "not readable/executable by porter-agent" in message
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in calls
        assert ["test", "-x", str(owner_launcher)] not in calls
        assert not layout_output.exists()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_pane_commands_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
