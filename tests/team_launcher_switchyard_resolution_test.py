#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_switchyard_register_cli_enables_launch_by_name_without_config_flag() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_config_dir = tmp_path / "otto" / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        project_config_dir.mkdir(parents=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = project_config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "pane_launcher": "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher",
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "otto")}],
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
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, "pane_launcher": config.pane_launcher, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["register", str(config_path)]) == 0
            assert switchyard_main(["otto"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "otto"
    assert calls[0]["config_path"] == config_path
    assert calls[0]["mode"] == "start"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] == Path("/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher")
    assert calls[0]["report_session_records"] is True

def test_switchyard_launch_runs_first_run_auth_before_panes_and_warns_after() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-first-run-launch.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "workdir": str(tmp_path / "repo")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        events: list[tuple[str, object]] = []
        report = team_launcher.FirstRunAuthReport({"codex": ["ops"]}, [])
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_run_first_run_auth_phase = team_launcher.run_first_run_auth_phase
        original_report_first_run_auth_warnings = team_launcher.report_first_run_auth_warnings
        original_geteuid = team_launcher.os.geteuid
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]

            def fake_run_first_run_auth_phase(
                config: team_launcher.ProjectConfig,
                **kwargs: object,
            ) -> team_launcher.FirstRunAuthReport:
                events.append(
                    (
                        "auth",
                        {
                            "project": config.project,
                            "owner_user": kwargs["owner_user"],
                            "owner_home": kwargs["owner_home"],
                            "validate_models": kwargs.get("validate_models"),
                        },
                    )
                )
                return report

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                events.append(("launch", {"project": config.project, **kwargs}))
                return 0

            def fake_report_first_run_auth_warnings(
                auth_report: team_launcher.FirstRunAuthReport,
                **_kwargs: object,
            ) -> None:
                events.append(("warn", auth_report))

            team_launcher.run_first_run_auth_phase = fake_run_first_run_auth_phase
            team_launcher.launch_project = fake_launch_project
            team_launcher.report_first_run_auth_warnings = fake_report_first_run_auth_warnings

            assert switchyard_main(["otto"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_first_run_auth_phase = original_run_first_run_auth_phase
            team_launcher.report_first_run_auth_warnings = original_report_first_run_auth_warnings
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert [name for name, _payload in events] == ["auth", "launch", "warn"]
    assert events[0][1] == {
        "project": "otto",
        "owner_user": "otto-agent",
        "owner_home": Path("/home/otto-agent"),
        "validate_models": False,
    }
    assert events[1][1]["config_path"] == config_path
    assert events[1][1]["mode"] == "start"
    assert events[1][1]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert events[1][1]["report_session_records"] is True
    assert events[2][1] is report

def test_switchyard_validate_models_command_runs_model_validation_on_demand() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-validate-models.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [
                        {
                            "role": "ops",
                            "slot": 0,
                            "cli": ["codex"],
                            "model": "openai/not-a-model",
                            "workdir": str(tmp_path / "repo"),
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner(
            invalid_models={("codex", "openai/not-a-model"): "model not found: openai/not-a-model\n"}
        )
        runner.login_seen.add("codex")
        output: list[str] = []

        result = team_launcher.switchyard_validate_models_command(
            "otto",
            config_dir=config_dir,
            registry_dir=registry_dir,
            runner=runner,
            print_func=output.append,
        )

    assert result == 1
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        [
            "sudo",
            "-u",
            "otto-agent",
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            "openai/not-a-model",
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
    ]
    assert output == [
        "warning: switchyard: model validation failed for role ops "
        "(cli codex, model openai/not-a-model): model not found: openai/not-a-model; "
        "no model list is available for codex; validation used a one-shot prompt"
    ]

def test_switchyard_stop_resolves_project_without_launching_or_authenticating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-stop.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "tmux_session": "otto-ops", "target": "otto-ops:0.0", "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[tuple[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_stop_project = team_launcher.stop_project
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        original_geteuid = team_launcher.os.geteuid
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]

            def fake_stop_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append(("stop", {"project": config.project, "run_as_user": config.run_as_user}))
                return 0

            def fake_launch_project(_config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append(("launch", {}))
                return 0

            def fake_first_run_auth(_config: team_launcher.ProjectConfig) -> team_launcher.FirstRunAuthReport:
                calls.append(("auth", {}))
                return team_launcher.FirstRunAuthReport({}, [])

            team_launcher.stop_project = fake_stop_project
            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = fake_first_run_auth

            assert switchyard_main(["stop", "Otto", "System"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.stop_project = original_stop_project
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [("stop", {"project": "otto", "run_as_user": "otto-agent"})]

def test_switchyard_bare_cross_owner_project_fails_before_launch_or_auth() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-cross-owner-launch.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "workdir": str(tmp_path / "repo")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[str] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        original_exec_with_root = team_launcher._switchyard_exec_with_root
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "agent"
            team_launcher.os.geteuid = lambda: 1001  # type: ignore[method-assign]
            team_launcher.launch_project = lambda *_args, **_kwargs: calls.append("launch") or 0
            team_launcher.run_switchyard_launch_first_run_auth = (
                lambda *_args, **_kwargs: calls.append("auth") or team_launcher.FirstRunAuthReport({}, [])
            )

            def fake_exec_with_root(argv: Sequence[str], **_kwargs: object) -> None:
                calls.append(f"root:{' '.join(argv)}")
                raise SystemExit("root required")

            team_launcher._switchyard_exec_with_root = fake_exec_with_root

            try:
                switchyard_main(["otto"])
                raise AssertionError("expected cross-owner launch to require root")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth
            team_launcher._switchyard_exec_with_root = original_exec_with_root

    assert calls == ["root:otto"]
    assert message == "root required"

def test_switchyard_upgrade_cross_owner_reexec_preserves_flags() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-cross-owner-upgrade.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        config_dir.mkdir()
        registry_dir.mkdir()
        (registry_dir / "mefp.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "mefp",
                    "name": "MEFP",
                    "config_path": "/home/stellaris-agent/stellaris-bugfix/.switchyard/provision/mefp.json",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []
        argv = ["upgrade", "mefp", "--dry-run", "--source-repo", "/data/git/pgu", "--deploy-ref", "v9"]
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_exec_with_root = team_launcher._switchyard_exec_with_root
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "eric"
            team_launcher.os.geteuid = lambda: 1000  # type: ignore[method-assign]

            def fake_exec_with_root(exec_argv: Sequence[str], **_kwargs: object) -> None:
                calls.append(list(exec_argv))
                raise SystemExit("root required")

            team_launcher._switchyard_exec_with_root = fake_exec_with_root

            try:
                switchyard_main(argv)
                raise AssertionError("expected foreign upgrade to re-exec with root")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher._switchyard_exec_with_root = original_exec_with_root

    assert calls == [argv]
    assert message == "root required"

def test_switchyard_add_role_cross_owner_reexecs_before_config_load() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-cross-owner-add-role.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        config_dir.mkdir()
        registry_dir.mkdir()
        (registry_dir / "mefp.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "mefp",
                    "name": "MEFP",
                    "config_path": "/home/mefp-agent/Projects/mefp/.switchyard/provision/mefp.json",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[str] = []
        argv = ["add-role", "mefp", "ops", "--cli", "codex"]
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_load_project_config = team_launcher.load_project_config
        original_exec_with_root = team_launcher._switchyard_exec_with_root
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "eric"
            team_launcher.os.geteuid = lambda: 1000  # type: ignore[method-assign]

            def fake_load_project_config(*_args: object, **_kwargs: object) -> team_launcher.ProjectConfig:
                calls.append("load")
                raise AssertionError("foreign add-role should not load the config before privilege re-exec")

            def fake_exec_with_root(exec_argv: Sequence[str], **_kwargs: object) -> None:
                calls.append(f"root:{' '.join(exec_argv)}")
                raise SystemExit("root required")

            team_launcher.load_project_config = fake_load_project_config
            team_launcher._switchyard_exec_with_root = fake_exec_with_root

            try:
                switchyard_main(argv)
                raise AssertionError("expected foreign add-role to re-exec with root")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher.load_project_config = original_load_project_config
            team_launcher._switchyard_exec_with_root = original_exec_with_root

    assert calls == ["root:add-role mefp ops --cli codex"]
    assert message == "root required"

def test_switchyard_foreign_home_config_reexecs_before_config_load() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-foreign-home.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        config_dir.mkdir()
        registry_dir.mkdir()
        (registry_dir / "mefp.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "mefp",
                    "name": "MEFP",
                    "config_path": "/home/stellaris-agent/stellaris-bugfix/.switchyard/provision/mefp.json",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[str] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_load_project_config = team_launcher.load_project_config
        original_exec_with_root = team_launcher._switchyard_exec_with_root
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "eric"
            team_launcher.os.geteuid = lambda: 1000  # type: ignore[method-assign]

            def fake_load_project_config(*_args: object, **_kwargs: object) -> team_launcher.ProjectConfig:
                calls.append("load")
                raise AssertionError("foreign config should not be loaded before privilege re-exec")

            def fake_exec_with_root(argv: Sequence[str], **_kwargs: object) -> None:
                calls.append(f"root:{' '.join(argv)}")
                raise SystemExit("root required")

            team_launcher.load_project_config = fake_load_project_config
            team_launcher._switchyard_exec_with_root = fake_exec_with_root

            try:
                switchyard_main(["mefp"])
                raise AssertionError("expected foreign-home project to re-exec with root")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher.load_project_config = original_load_project_config
            team_launcher._switchyard_exec_with_root = original_exec_with_root

    assert calls == ["root:mefp"]
    assert message == "root required"

def test_switchyard_stop_cross_owner_project_fails_before_tmux_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-cross-owner-stop.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "tmux_session": "otto-ops", "target": "otto-ops:0.0", "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[str] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_geteuid = team_launcher.os.geteuid
        original_stop_project = team_launcher.stop_project
        original_exec_with_root = team_launcher._switchyard_exec_with_root
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "agent"
            team_launcher.os.geteuid = lambda: 1001  # type: ignore[method-assign]
            team_launcher.stop_project = lambda *_args, **_kwargs: calls.append("stop") or 0

            def fake_exec_with_root(argv: Sequence[str], **_kwargs: object) -> None:
                calls.append(f"root:{' '.join(argv)}")
                raise SystemExit("root required")

            team_launcher._switchyard_exec_with_root = fake_exec_with_root

            try:
                switchyard_main(["stop", "otto"])
                raise AssertionError("expected cross-owner stop to require root")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
            team_launcher.stop_project = original_stop_project
            team_launcher._switchyard_exec_with_root = original_exec_with_root

    assert calls == ["root:stop otto"]
    assert message == "root required"

def test_switchyard_registry_does_not_change_pgu_resolution_or_launch_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-pgu-registry.") as tmp:
        registry_dir = Path(tmp) / "registry"
        registry_dir.mkdir()
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": "/home/otto-agent/Projects/otto/.switchyard/provision/otto.json",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append(
                    {
                        "project": config.project,
                        "pane_launcher": config.pane_launcher,
                        "workdirs": [role.workdir for role in config.roles],
                        **kwargs,
                    }
                )
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["pgu"]) == 0
        finally:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "pgu"
    assert calls[0]["config_path"] == ROOT / "config" / "team-launcher" / "pgu.json"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] is None
    assert set(calls[0]["workdirs"]) == {"/home/agent/Projects/pgu"}

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_switchyard_resolution_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
