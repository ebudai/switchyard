#!/usr/bin/env python3
"""Split team-launcher regression tests.

Guard-only (SYRD-808): `host_state_guard` refuses any read or exec of this host's Switchyard state and privileged
tools, and the last check fails on any reach. Run it as an ordinary account, never as root, and never in a sweep
that runs as root.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import host_state_guard  # noqa: E402  - before any production module: no host state (SYRD-808)
host_state_guard.install()

from team_launcher_test_helpers import *
import contextlib as _contextlib
import pwd
from unittest import mock

#: The owner a root command acts for here: the account running the suite, which owns every fixture file. Never a
#: real tenant account of this host -- `otto-agent` is one here, and acting for it ran the real sudo (SYRD-808).
OWNER = pwd.getpwuid(os.getuid()).pw_name
OWNER_HOME = pwd.getpwuid(os.getuid()).pw_dir


@_contextlib.contextmanager
def roots_record_naming(project: str, owner: str):
    """Root's provisioning record of `project`, naming `owner` as whose it is, in a stand-in root of the case's own.

    A command run as root acts for the owner root's records name, never for the account a configuration says it runs
    as (SYRD-663, SYRD-757). The privileged provision root's override is the documented seam for that record: under
    it the caller's own uid stands in for root's (no_follow_records). Never this host's /etc/switchyard (SYRD-808).
    """
    with tempfile.TemporaryDirectory(prefix="syrd808-root.") as root:
        record = Path(root) / project / "plan.json"
        record.parent.mkdir()
        record.write_text(json.dumps({"project": project, "owner_user": owner}) + "\n", encoding="utf-8")
        record.chmod(0o600)
        saved = os.environ.get("SWITCHYARD_PRIVILEGED_PROVISION_ROOT")
        os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = root
        try:
            yield record
        finally:
            if saved is None:
                os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)
            else:
                os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = saved


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
                    "desktop_access": {"mode": "headless"},
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
                    "desktop_access": {"mode": "headless"},
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": OWNER,
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "workdir": str(tmp_path / "repo")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        events: list[tuple[str, object]] = []
        # A warning that does not stop the launch: an unfinished sign-in now refuses it before any pane (SYRD-221).
        report = team_launcher.FirstRunAuthReport({}, [("ops", "codex", str(tmp_path / "repo"))])
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

            with roots_record_naming("otto", OWNER):
                assert switchyard_main(["otto"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_first_run_auth_phase = original_run_first_run_auth_phase
            team_launcher.report_first_run_auth_warnings = original_report_first_run_auth_warnings
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert [name for name, _payload in events] == ["auth", "launch", "warn"], events
    assert events[0][1] == {
        "project": "otto",
        "owner_user": OWNER,
        "owner_home": Path(OWNER_HOME),
        "validate_models": False,
    }
    assert events[1][1]["config_path"] == config_path
    assert events[1][1]["mode"] == "start"
    assert events[1][1]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert events[1][1]["report_session_records"] is True
    assert events[2][1] is report


def test_switchyard_launch_missing_owner_clis_stops_before_panes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-missing-cli-launch.") as tmp:
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
                    "desktop_access": {"mode": "headless"},
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": OWNER,
                    "roles": [
                        {"role": "designer", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "designer")},
                        {"role": "director", "slot": 1, "cli": ["claude"], "workdir": str(tmp_path / "director")},
                        {"role": "ops", "slot": 2, "cli": ["codex"], "workdir": str(tmp_path / "ops")},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        events: list[tuple[str, object]] = []
        output = StringIO()
        report = team_launcher.FirstRunAuthReport(
            {},
            [],
            missing_cli_roles={"claude": ["designer", "director"], "codex": ["ops"]},
            owner_user=OWNER,
        )
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
                **_kwargs: object,
            ) -> team_launcher.FirstRunAuthReport:
                events.append(("auth", config.project))
                return report

            def fake_launch_project(_config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                events.append(("launch", {}))
                print("team-launcher: role designer did not leave a live otto-designer session")
                return 1

            def fake_report_first_run_auth_warnings(
                auth_report: team_launcher.FirstRunAuthReport,
                **_kwargs: object,
            ) -> None:
                events.append(("warn", auth_report))

            team_launcher.run_first_run_auth_phase = fake_run_first_run_auth_phase
            team_launcher.launch_project = fake_launch_project
            team_launcher.report_first_run_auth_warnings = fake_report_first_run_auth_warnings

            with redirect_stdout(output), roots_record_naming("otto", OWNER):
                result = switchyard_main(["otto"])
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_first_run_auth_phase = original_run_first_run_auth_phase
            team_launcher.report_first_run_auth_warnings = original_report_first_run_auth_warnings
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    rendered = output.getvalue()
    assert result == 1
    assert [name for name, _payload in events] == ["auth"]
    assert f"cannot launch panes because required CLI(s) are missing for owner user {OWNER}" in rendered
    assert "claude (roles: designer, director)" in rendered
    assert "codex (roles: ops)" in rendered
    assert "did not leave a live" not in rendered

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
                    "desktop_access": {"mode": "headless"},
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": OWNER,
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

        # The probes go through `runner`; the steps a person answers would open the owner's own CLI on a pty. The
        # owner's home is the case's own, its folder trust stands recorded, and no interactive session may open: one
        # did, as the real sudo -u otto-agent, a real account of this host (SYRD-808).
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()

        def no_session(**kwargs: object) -> bool:
            raise AssertionError(f"validate-models opened an interactive provider session: {kwargs.get('args')}")

        with mock.patch.object(team_launcher, "_owner_home_for_auth", lambda _owner: owner_home), \
                mock.patch.object(team_launcher, "_workdir_is_trusted", lambda *_a, **_k: True), \
                mock.patch.object(team_launcher, "run_provider_first_run_session", no_session):
            result = team_launcher.switchyard_validate_models_command(
                "otto",
                config_dir=config_dir,
                registry_dir=registry_dir,
                runner=runner,
                print_func=output.append,
            )

    assert result == 1, output
    assert runner.calls == [
        # The owner is this account, so its CLI runs directly; the sudo wrapping for another owner is
        # team_launcher_first_run_auth_test's.
        ["codex", "login", "status"],
        [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            "openai/not-a-model",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "-C",
            runner.model_probe_cwds[0],
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
    ], runner.calls
    assert output == [
        # Each probe is announced with its bound before it waits (041e3282), and its failed attempts follow the
        # verdict.
        "switchyard: checking ops's model (openai/not-a-model) with codex; this asks it one question and waits up to "
        "180s for the answer",
        "warning: switchyard: model validation failed for role ops "
        "(cli codex, model openai/not-a-model): model not found: openai/not-a-model; "
        "no model list is available for codex; validation used a one-shot prompt",
        "warning: switchyard:   probe attempt 1 (exit 1) did not answer: stderr: model not found: openai/not-a-model",
    ], output

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
                    "desktop_access": {"mode": "headless"},
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": OWNER,
                    "roles": [{"role": "ops", "slot": 0, "tmux_session": "otto-ops", "target": "otto-ops:0.0", "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[tuple[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_suspend_tenant = team_launcher.suspend_tenant
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        original_geteuid = team_launcher.os.geteuid
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]

            # `switchyard stop` suspends the whole tenant (SYRD-193): window, sessions, escaped processes, listener
            # and board. Its own suite drives that; here it is stood in, so the case never scans this host's
            # processes nor asks its systemd (SYRD-808).
            def fake_suspend_tenant(config: team_launcher.ProjectConfig, **_kwargs: object) -> list[str]:
                calls.append(("stop", {"project": config.project, "run_as_user": config.run_as_user}))
                return []

            def fake_launch_project(_config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append(("launch", {}))
                return 0

            def fake_first_run_auth(_config: team_launcher.ProjectConfig) -> team_launcher.FirstRunAuthReport:
                calls.append(("auth", {}))
                return team_launcher.FirstRunAuthReport({}, [])

            team_launcher.suspend_tenant = fake_suspend_tenant
            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = fake_first_run_auth

            with roots_record_naming("otto", OWNER):
                assert switchyard_main(["stop", "Otto", "System"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.suspend_tenant = original_suspend_tenant
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth
            team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [("stop", {"project": "otto", "run_as_user": OWNER})]

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
                    "desktop_access": {"mode": "headless"},
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
                    "desktop_access": {"mode": "headless"},
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
            # The shipped pgu.json runs as `agent`, a real account of this host, so any other caller crosses to it
            # through root -- the real sudo (SYRD-808). This case is about which configuration the name resolves to;
            # who may run it is the crossing's own suite's, so the caller stands as pgu's owner. The shipped file names
            # no desktop policy, and a launch asks for one first; desktop setup is project_desktop's to test.
            with mock.patch.object(team_launcher, "_require_switchyard_project_owner_or_root", lambda *_a, **_k: None), \
                    mock.patch.object(team_launcher, "prepare_project_desktop", lambda config, **_k: config):
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
    # These cases are about how a name resolves to a project and whose it is. A crossing and a launch's resume first
    # stage a tenant's missing role bundle (32654e78), which here would be root's privileged staging for fixture
    # tenants; that repair is single_owner_staged_tooling_test's, so it is stood in as already done (SYRD-808).
    staged = {"ensure_staged_role_bundle_before_crossing": lambda *_args, **_kwargs: "",
              "ensure_staged_role_tooling": lambda *_args, **_kwargs: [],
              # and a launch's resume of a suspended tenant asks systemd itself: tenant_suspension_test's.
              "resume_tenant": lambda *_args, **_kwargs: []}
    original = {name: getattr(team_launcher, name) for name in staged}
    for name, stand_in in staged.items():
        setattr(team_launcher, name, stand_in)
    # Each case names its own registry through DEFAULT_SWITCHYARD_REGISTRY_DIR; the helpers' registry variable would
    # outrank it, so one case's registration -- of a directory it has since removed -- answered the next (SYRD-808).
    # Between cases the default is the helpers' empty registry, never this host's.
    registry_variable = os.environ.pop(team_launcher.SWITCHYARD_REGISTRY_DIR_ENV, None)
    original_registry = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
    team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = TEST_SWITCHYARD_REGISTRY_DIR
    try:
        run_team_launcher_tests(globals(), first=())
    finally:
        for name, value in original.items():
            setattr(team_launcher, name, value)
        team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry
        if registry_variable is not None:
            os.environ[team_launcher.SWITCHYARD_REGISTRY_DIR_ENV] = registry_variable
        host_state_guard.assert_untouched("team_launcher_switchyard_resolution_test")  # a reach that also broke a case is reported as the reach
    print("team_launcher_switchyard_resolution_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
