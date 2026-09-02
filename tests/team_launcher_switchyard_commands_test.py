#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def _readme_switchyard_help_block() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = "<!-- switchyard-help:start -->"
    end = "<!-- switchyard-help:end -->"
    assert start in readme
    assert end in readme
    block = readme.split(start, 1)[1].split(end, 1)[0].strip()
    assert block.startswith("```text\n")
    assert block.endswith("\n```")
    return block.removeprefix("```text\n").removesuffix("\n```")


def test_readme_names_install_path_and_help_text_cannot_drift() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "[the fresh-machine install list](docs/fresh-machine-install.md)" in readme
    prereq_index = readme.index("`scripts/install-switchyard-prereqs`")
    install_index = readme.index("`scripts/install-switchyard`", prereq_index + 1)
    commands_index = readme.index("## Commands")
    assert prereq_index < install_index < commands_index
    assert "Applying that block needs root." in readme
    assert "sudo env ... --apply" in readme
    assert _readme_switchyard_help_block() == team_launcher.switchyard_help_text().strip()


def test_legacy_and_switchyard_entrypoints_render_same_plain_konsole_command() -> None:
    def run_entrypoint(
        argv: list[str],
        *,
        config_path: Path,
        config_dir: Path,
        registry_dir: Path,
        layout_output: Path,
    ) -> tuple[list[str], list[str]]:
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()
        original_launch_project = team_launcher.launch_project
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "root"
            team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])

            def launch_with_fake_runner(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                kwargs["runner"] = runner
                kwargs["layout_output"] = layout_output
                kwargs["layout_mode"] = team_launcher.LAYOUT_MODE_SEPARATE
                kwargs["report_session_records"] = False
                kwargs["no_launcher_self_deploy"] = True
                kwargs["konsole_process_launcher"] = process_launcher
                return original_launch_project(config, **kwargs)

            team_launcher.launch_project = launch_with_fake_runner
            if argv[0] == "team-launcher":
                assert team_launcher.main(["pgu", "start", "--config", str(config_path)]) == 0
            else:
                assert switchyard_main(["pgu"]) == 0
        finally:
            team_launcher.launch_project = original_launch_project
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

        assert not any("konsole" in call for call in runner.calls)
        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        pane_commands = _leaf_commands(json.loads(layout_output.read_text(encoding="utf-8")))
        return process_launcher.calls[0]["args"], pane_commands

    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-entrypoint-konsole.") as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            registry_dir = tmp_path / "registry"
            registry_dir.mkdir()
            config_path = _write_minimal_shared_checkout_config(config_dir, repo, ["ops"], run_as_user="agent")
            (config_dir / "layout.json").write_text(
                json.dumps(
                    {
                        "Widgets": [
                            {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            legacy_layout = tmp_path / "legacy-layout.json"
            switchyard_layout = tmp_path / "switchyard-layout.json"

            legacy_command, legacy_pane_commands = run_entrypoint(
                ["team-launcher"],
                config_path=config_path,
                config_dir=config_dir,
                registry_dir=registry_dir,
                layout_output=legacy_layout,
            )
            switchyard_command, switchyard_pane_commands = run_entrypoint(
                ["switchyard"],
                config_path=config_path,
                config_dir=config_dir,
                registry_dir=registry_dir,
                layout_output=switchyard_layout,
            )

        assert legacy_command == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--qwindowtitle",
            "pgu",
            "--layout",
            str(legacy_layout),
        ]
        assert switchyard_command == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--qwindowtitle",
            "pgu",
            "--layout",
            str(switchyard_layout),
        ]
        assert len(legacy_pane_commands) == 1
        assert len(switchyard_pane_commands) == 1
        assert shlex.split(legacy_pane_commands[0])[:4] == ["sudo", "-u", "agent", "-H"]
        assert shlex.split(switchyard_pane_commands[0])[:4] == ["sudo", "-u", "agent", "-H"]
        assert " pane attach-or-start ops " in f" {legacy_pane_commands[0]} "
        assert " pane attach-or-start ops " in f" {switchyard_pane_commands[0]} "
    finally:
        os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

def test_switchyard_project_name_argv_joins_and_resumes_matching_project() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-open.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "My Project Name",
                    "layout": str(layout),
                    "pane_launcher": "/home/porter-agent/porter-ticketboard-live/current/scripts/team-launcher",
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = tmp_path
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, "pane_launcher": config.pane_launcher, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["my", "project", "name"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "porter"
    assert calls[0]["config_path"] == config_path
    assert calls[0]["mode"] == "start"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] == Path("/home/porter-agent/porter-ticketboard-live/current/scripts/team-launcher")
    assert calls[0]["report_session_records"] is True

def test_switchyard_add_role_audit_flag_reaches_command() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-add-auditor-entrypoint.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout_path = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "mefp",
                    "layout": str(layout_path),
                    "roles": [{"role": "main", "slot": 0, "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        captured: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_add_project_role_command = team_launcher.add_project_role_command
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_add_project_role_command(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                captured.append({"project": config.project, **kwargs})
                return 0

            team_launcher.add_project_role_command = fake_add_project_role_command

            assert switchyard_main(["add-role", "mefp", "audit_gpt", "--audit", "--cli", "agy", "--detached", "--no-start"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.add_project_role_command = original_add_project_role_command

    assert len(captured) == 1
    assert captured[0]["project"] == "mefp"
    assert captured[0]["config_path"] == config_path
    assert captured[0]["role_name"] == "audit_gpt"
    assert captured[0]["cli"] == "agy"
    assert captured[0]["audit_role"] is True
    assert captured[0]["detached"] is True
    assert captured[0]["start"] is False

def test_team_launcher_add_role_audit_flag_reaches_command() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-auditor-entrypoint.") as tmp:
        tmp_path = Path(tmp)
        layout_path = tmp_path / "layout.json"
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "mefp.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "mefp",
                    "layout": str(layout_path),
                    "roles": [{"role": "main", "slot": 0, "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        captured: list[dict[str, object]] = []
        original_add_project_role_command = team_launcher.add_project_role_command
        try:
            def fake_add_project_role_command(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                captured.append({"project": config.project, **kwargs})
                return 0

            team_launcher.add_project_role_command = fake_add_project_role_command

            assert (
                team_launcher.main(
                    [
                        "mefp",
                        "add-role",
                        "audit_gpt",
                        "--config",
                        str(config_path),
                        "--audit",
                        "--cli",
                        "agy",
                        "--detached",
                        "--no-attach",
                    ]
                )
                == 0
            )
        finally:
            team_launcher.add_project_role_command = original_add_project_role_command

    assert len(captured) == 1
    assert captured[0]["project"] == "mefp"
    assert captured[0]["config_path"] == config_path
    assert captured[0]["role_name"] == "audit_gpt"
    assert captured[0]["cli"] == "agy"
    assert captured[0]["audit_role"] is True
    assert captured[0]["detached"] is True
    assert captured[0]["start"] is False

def test_team_launcher_main_resolves_registered_project_for_pane_operations() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-pane.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_dir = tmp_path / "otto-project"
        provision_dir = project_dir / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        config_dir.mkdir()
        registry_dir.mkdir()
        provision_dir.mkdir(parents=True)
        project_dir.mkdir(exist_ok=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = provision_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "session_dir": str(session_dir),
                    "roles": [
                        {
                            "role": "app",
                            "slot": 0,
                            "tmux_session": "otto-app",
                            "target": "otto-app:0.0",
                            "cli": ["codex"],
                            "workdir": str(project_dir),
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_run_role_pane = team_launcher.run_role_pane
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_run_role_pane(role: team_launcher.RoleConfig, **kwargs: object) -> int:
                calls.append({"role": role.role, "tmux_session": role.tmux_session, **kwargs})
                return 0

            team_launcher.run_role_pane = fake_run_role_pane

            assert team_launcher.main(["otto", "pane", "reload", "app", "--skip-launcher-check", "--pane-state-dir", str(pane_state_dir)]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.run_role_pane = original_run_role_pane

    assert calls == [
        {
            "role": "app",
            "tmux_session": "otto-app",
            "mode": "reload",
            "session_dir": session_dir,
            "pane_state_dir": pane_state_dir,
            "force_reload": False,
            "bin_user": "",
        }
    ]

def test_team_launcher_main_resolves_registered_project_for_stop() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-stop.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_dir = tmp_path / "otto-project"
        provision_dir = project_dir / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        provision_dir.mkdir(parents=True)
        project_dir.mkdir(exist_ok=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = provision_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [
                        {
                            "role": "app",
                            "slot": 0,
                            "tmux_session": "otto-app",
                            "target": "otto-app:0.0",
                            "cli": ["codex"],
                            "workdir": str(project_dir),
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_stop_project = team_launcher.stop_project
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_stop_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append({"project": config.project, "run_as_user": config.run_as_user})
                return 0

            team_launcher.stop_project = fake_stop_project

            assert team_launcher.main(["otto", "stop"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.stop_project = original_stop_project

    assert calls == [{"project": "otto", "run_as_user": "otto-agent"}]

def test_team_launcher_main_prefers_checkout_config_before_registry_for_pgu() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-precedence.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        checkout_repo = tmp_path / "checkout-repo"
        registry_repo = tmp_path / "registry-repo"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        checkout_repo.mkdir()
        registry_repo.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        checkout_config = config_dir / "pgu.json"
        registry_config = tmp_path / "registry-pgu.json"
        for path, workdir, role in [
            (checkout_config, checkout_repo, "ops"),
            (registry_config, registry_repo, "app"),
        ]:
            path.write_text(
                json.dumps(
                    {
                        "project": "pgu",
                        "layout": str(layout),
                        "roles": [{"role": role, "slot": 0, "cli": ["codex"], "workdir": str(workdir)}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        (registry_dir / "pgu.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "pgu",
                    "name": "pgu",
                    "config_path": str(registry_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"config_path": kwargs["config_path"], "workdirs": [role.workdir for role in config.roles]})
                return 0

            team_launcher.launch_project = fake_launch_project

            assert team_launcher.main(["pgu", "start", "--dry-run"]) == 0
            assert team_launcher.main(["pgu", "start", "--dry-run", "--config", str(checkout_config)]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project

    assert calls == [
        {"config_path": checkout_config, "workdirs": [str(checkout_repo)]},
        {"config_path": checkout_config, "workdirs": [str(checkout_repo)]},
    ]

def test_team_launcher_unknown_project_names_config_and_registry_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-missing.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        config_dir.mkdir()
        registry_dir.mkdir()
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            try:
                team_launcher.main(["unknown", "start", "--dry-run"])
                raise AssertionError("expected unknown project failure")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir

    assert message == (
        f"team-launcher: unknown project 'unknown'; searched config dir {config_dir} "
        f"and registry dir {registry_dir}"
    )

def test_main_start_and_reload_enable_session_record_report() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-entry-session-records.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_launch_project = team_launcher.launch_project
        try:
            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            assert team_launcher.main(["porter", "start", "--config", str(config_path)]) == 0
            assert team_launcher.main(["porter", "reload", "--config", str(config_path), "--layout", "viewer"]) == 0
        finally:
            team_launcher.launch_project = original_launch_project

    assert [call["mode"] for call in calls] == ["start", "reload"]
    assert [call["report_session_records"] for call in calls] == [True, True]
    assert [call["layout_mode"] for call in calls] == ["auto", "viewer"]

def test_switchyard_new_without_sudo_fails_before_writing() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter System",
                output_dir=output_dir,
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "requires sudo" in message
    assert not output_dir.exists()
    assert not (home_base / "otto-agent" / "Projects" / "porter_system").exists()
    assert calls == []

def test_switchyard_new_rejects_dashed_slug_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="otto-scheduler",
                agent_name="otto-agent",
                project_name="Otto Scheduler",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected dashed slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug must match" in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "otto-agent").exists()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_switchyard_commands_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
