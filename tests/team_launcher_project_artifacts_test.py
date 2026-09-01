#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_switchyard_new_reports_project_specific_pane_state_dir_without_override() -> None:
    class NewProjectRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    captured_pane_state_dirs: list[Path | None] = []
    original_uid_for_user = team_launcher.uid_for_user
    original_report_launch_session_records = team_launcher.report_launch_session_records
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)

        def fake_report_launch_session_records(
            _config: team_launcher.ProjectConfig,
            _roles: Sequence[team_launcher.RoleConfig] | None = None,
            **kwargs: object,
        ) -> list[team_launcher.LaunchSessionRecordStatus]:
            captured_pane_state_dirs.append(kwargs.get("pane_state_dir"))  # type: ignore[arg-type]
            return []

        team_launcher.report_launch_session_records = fake_report_launch_session_records

        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-pane-state.") as tmp:
            tmp_path = Path(tmp)
            source_repo = tmp_path / "source-repo"
            source_repo.mkdir()
            process_launcher = RecordingProcessLauncher()
            assert (
                switchyard_new_command(
                    slug="porter",
                    agent_name="otto-agent",
                    project_name="Porter System",
                    source_repo=source_repo,
                    output_dir=tmp_path / "out",
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    allow_existing_owner_user=True,
                    home_base=tmp_path / "home",
                    euid_getter=lambda: 0,
                    runner=NewProjectRunner(),
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.report_launch_session_records = original_report_launch_session_records

    assert captured_pane_state_dirs == [Path("/run/user/1005/porter-ticket-board/pane-state")]

def test_switchyard_new_custom_project_path_sets_artifact_repository_and_pane_workdirs() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        custom_path = tmp_path / "external" / "otto-scheduler"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = NewProjectRunner()
        process_launcher = RecordingProcessLauncher()

        assert (
            switchyard_new_command(
                project_name="Otto Scheduler",
                slug="otto",
                agent_name="otto-agent",
                project_path=custom_path,
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                session_record_timeout=0,
                registry_dir=tmp_path / "registry",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        config = load_project_config("otto", output_dir / "otto.json")
        install_call = next(call for call in runner.calls if call[:1] == ["install"])

        assert custom_path.is_dir()
        assert json.loads((custom_path / ".switchyard" / "otto.project.json").read_text(encoding="utf-8"))["project"]["repository"] == str(custom_path)
        assert install_call[-1] == str(custom_path)
        assert config.repository == custom_path
        assert config.control_repository == Path("/home/otto-agent/.local/state/switchyard/projects/otto/control.git")
        assert {role.role: role.workdir for role in config.roles} == {
            "designer": "/home/otto-agent/otto-worktrees/designer",
            "director": "/home/otto-agent/otto-worktrees/director",
            "audit": "/home/otto-agent/otto-worktrees/audit",
            "ops": "/home/otto-agent/otto-worktrees/ops",
            "app": "/home/otto-agent/otto-worktrees/app",
            "main": "/home/otto-agent/otto-worktrees/main",
        }

def test_new_project_config_threads_upstream_report_env_to_panes() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-reporting.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        output_dir = tmp_path / "out"
        token_file = tmp_path / "mefp-upstream-report.env"
        source_repo.mkdir()
        project_repo.mkdir()

        assert (
            new_project_command(
                "mefp",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                upstream_report_url="http://127.0.0.1:8770",
                upstream_report_token_file=str(token_file),
            )
            == 0
        )

        raw_config = json.loads((output_dir / "mefp.json").read_text(encoding="utf-8"))
        loaded_config = load_project_config("mefp", output_dir / "mefp.json")

    assert raw_config["upstream_report_url"] == "http://127.0.0.1:8770"
    assert raw_config["upstream_report_token_file"] == str(token_file)
    assert "upstream_report_token" not in raw_config
    assert loaded_config.upstream_report_url == "http://127.0.0.1:8770"
    assert loaded_config.upstream_report_token_file == str(token_file)
    for role in loaded_config.roles:
        assert role.env["TICKET_BOARD_URL"].startswith("http://127.0.0.1:")
        assert role.env["TICKET_BOARD_REPORT_URL"] == "http://127.0.0.1:8770"
        assert role.env["TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"] == str(token_file)
        assert "TICKET_BOARD_TENANT_REPORT_TOKEN" not in role.env
        assert role.env["TICKET_BOARD_REPORT_ORIGIN_PROJECT"] == "mefp"
        command = cli_command_for_role(role, session_dir=loaded_config.session_dir)
        assert f"TICKET_BOARD_TENANT_REPORT_TOKEN_FILE={token_file}" in command
        assert not any("tenant-report-token" in part for part in command)

def test_project_config_rejects_inline_upstream_report_token() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-reporting-inline.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "mefp.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "mefp",
                    "layout": str(layout),
                    "repository": str(repo),
                    "upstream_report_url": "http://127.0.0.1:8770",
                    "upstream_report_token": "tenant-report-token",
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "mefp-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_config("mefp", config_path)
            raise AssertionError("expected inline upstream report token rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "upstream_report_token" in message
    assert "use upstream_report_token_file instead" in message

def test_switchyard_new_creates_absent_zeta_owner_with_linger_and_initial_artifact() -> None:
    class ZetaRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["id", "-u", "zeta-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args == ["loginctl", "enable-linger", "zeta-agent"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = ZetaRunner()
        process_launcher = RecordingProcessLauncher()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    slug="zeta",
                    agent_name="zeta-agent",
                    project_name="Zeta System",
                    source_repo=source_repo,
                    output_dir=output_dir,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    allow_existing_owner_user=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

        project_dir = home_base / "zeta-agent" / "Projects" / "zeta_system"
        expected_useradd = team_launcher._owner_project_install_args("zeta-agent", project_dir)[1]
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")
        artifact_created = (project_dir / ".switchyard" / "zeta.project.json").is_file()

    assert expected_useradd in runner.calls
    assert "-G" not in expected_useradd
    assert ["loginctl", "enable-linger", "zeta-agent"] in runner.calls
    assert artifact_created
    assert f"switchyard: created user zeta-agent with shell fish; linger enabled" in stdout.getvalue()
    assert "sudo loginctl enable-linger 'zeta-agent'" not in commands
    assert "loginctl show-user 'zeta-agent' -p Linger --value" in commands

def test_switchyard_new_reuses_existing_owner_without_account_mutation() -> None:
    class ExistingOwnerRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.owner_marker = {
                "shell": "/usr/local/bin/project-shell",
                "groups": ("project-extra",),
            }

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["id", "-u", "marked-agent"]:
                return subprocess.CompletedProcess(args, 0, stdout="4242\n")
            if args == ["loginctl", "show-user", "marked-agent", "-p", "Linger", "--value"]:
                return subprocess.CompletedProcess(args, 0, stdout="yes\n")
            if args[:1] in (["useradd"], ["usermod"], ["chsh"]):
                raise AssertionError(f"existing owner must not be modified: {args}")
            if args == ["loginctl", "enable-linger", "marked-agent"]:
                raise AssertionError(f"existing owner linger must not be changed during account bootstrap: {args}")
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-existing.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "home" / "marked-agent" / "Projects" / "marked"
        source_repo = tmp_path / "source-repo"
        output_dir = tmp_path / "out"
        source_repo.mkdir()
        artifact_path = tmp_path / "marked.project.json"
        design_document = tmp_path / "MARKED_DESIGN.md"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": str(design_document),
                    "project": {
                        "slug": "marked",
                        "name": "Marked Project",
                        "ticket_prefix": "MARK",
                        "owner_user": "marked-agent",
                        "repository": str(project_dir),
                        "capability_grants": {
                            "board_service_traversal": True,
                            "supplementary_groups": ["project-extra"],
                            "linger": True,
                            "shell": "/usr/local/bin/project-shell",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        runner = ExistingOwnerRunner()
        process_launcher = RecordingProcessLauncher()
        marker_before = dict(runner.owner_marker)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    from_artifact=artifact_path,
                    source_repo=source_repo,
                    output_dir=output_dir,
                    yes=True,
                    home_base=tmp_path / "home",
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")

    assert runner.owner_marker == marker_before
    assert not any(call[:1] == ["useradd"] for call in runner.calls)
    assert not any(call[:1] in (["usermod"], ["chsh"]) for call in runner.calls)
    assert ["loginctl", "show-user", "marked-agent", "-p", "Linger", "--value"] in runner.calls
    assert ["loginctl", "enable-linger", "marked-agent"] not in runner.calls
    assert f"switchyard: using existing user marked-agent (not modifying)" in stdout.getvalue()
    assert "sudo loginctl enable-linger 'marked-agent'" not in commands
    assert "loginctl show-user 'marked-agent' -p Linger --value" in commands

def test_switchyard_new_unwritable_existing_project_path_refuses_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        project_path = tmp_path / "unwritable"
        project_path.mkdir()
        project_path.chmod(0o500)
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []
        try:
            try:
                switchyard_new_command(
                    project_name="Otto Scheduler",
                    slug="otto",
                    agent_name="otto-agent",
                    project_path=project_path,
                    output_dir=output_dir,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    allow_existing_owner_user=True,
                    euid_getter=lambda: 0,
                    runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                )
                raise AssertionError("expected unwritable path failure")
            except SystemExit as exc:
                message = str(exc)
        finally:
            project_path.chmod(0o700)

    assert "not readable and writable by otto-agent" in message
    assert calls == []
    assert not output_dir.exists()
    assert not (project_path / ".switchyard").exists()

def test_new_project_from_handwritten_artifact_uses_same_provision_path() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        provision_dir = tmp_path / "provision"
        source_repo.mkdir()
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "remote": "origin",
                        "default_branch": "mainline",
                        "worktree_policy": "isolated",
                        "push_policy": "director-main-only",
                        "gates": {
                            "audit_signoff": True,
                            "needs_inspection": False,
                            "needs_user_signoff": True,
                        },
                        "capability_grants": {
                            "board_service_traversal": True,
                            "supplementary_groups": ["kvm"],
                            "linger": True,
                            "shell": "bash",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner()

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=provision_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((provision_dir / "atlas.json").read_text(encoding="utf-8"))
        board_unit = (provision_dir / "atlas-ticket-board.service").read_text(encoding="utf-8")

    assert plan["project"] == "atlas"
    assert plan["ticket_prefix"] == "ATL"
    assert plan["board_service_traversal"] is True
    assert config["ticket_prefix"] == "ATL"
    assert config["worktree_branch"] == "mainline"
    assert config["worktree_base"] == f"/home/{current_user}/atlas-worktrees"
    assert config["session_dir"] == f"/home/{current_user}/.local/state/atlas-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert config["control_repository"] == f"/home/{current_user}/.local/state/switchyard/projects/atlas/control.git"
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=ATL" in board_unit
    assert not any(call[:1] == ["sudo"] for call in runner.calls)

def test_new_project_artifact_board_service_traversal_false_omits_owner_home_acls() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        provision_dir = tmp_path / "provision"
        source_repo.mkdir()
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "capability_grants": {
                            "board_service_traversal": False,
                            "supplementary_groups": [],
                            "linger": True,
                            "shell": "bash",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=provision_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        commands = (provision_dir / "operator-commands.sh").read_text(encoding="utf-8")

    assert plan["board_service_traversal"] is False
    assert "sudo setfacl -m u:boardsvc:--x" not in commands
    assert "sudo setfacl -R -m u:boardsvc:rwx" not in commands
    assert "sudo setfacl -R -m u:boardsvc:rx" not in commands
    assert "board_service_traversal=false" in commands
    assert "board health check will fail" in commands

def test_project_artifact_accepts_designer_chosen_roles() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        source_repo = tmp_path / "source-repo"
        project_repo.mkdir()
        source_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["ops", "app"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

        plan = json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((tmp_path / "out" / "atlas.json").read_text(encoding="utf-8"))

    assert plan["implementer_roles"] == ["ops", "app"]
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "ops", "app"]

def test_project_artifact_can_omit_audit_role() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        source_repo = tmp_path / "source-repo"
        project_repo.mkdir()
        source_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["ops", "app"],
                        "include_audit": False,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

        plan = json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((tmp_path / "out" / "atlas.json").read_text(encoding="utf-8"))
        workflow_sql = (tmp_path / "out" / "atlas-workflow.sql").read_text(encoding="utf-8")

    assert plan["assignee_roles"] == ["unassigned", "designer", "ops", "app", "director", "user"]
    assert plan["caller_roles"] == ["director", "designer", "ops", "app", "user"]
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "ops", "app"]
    assert "('audit', 'Audit'" not in workflow_sql
    assert "('in_progress', 'director_review', 'submit_to_audit', ARRAY['ops', 'app']::text[]" in workflow_sql

def test_project_artifact_rejects_unknown_role_cli() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["backend"],
                        "role_clis": {"backend": "cluade"},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_design_artifact(artifact_path)
            raise AssertionError("expected bad CLI failure")
        except SystemExit as exc:
            message = str(exc)

    assert "CLI for backend must be one of claude, codex, agy, hermes" in message

def test_project_artifact_rejects_non_bool_include_designer() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["backend"],
                        "include_designer": "yes",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_design_artifact(artifact_path)
            raise AssertionError("expected include_designer type failure")
        except SystemExit as exc:
            message = str(exc)

    assert "field 'project.include_designer' must be a JSON boolean" in message

def test_project_artifact_rejects_non_bool_include_audit() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["backend"],
                        "include_audit": "no",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_design_artifact(artifact_path)
            raise AssertionError("expected include_audit type failure")
        except SystemExit as exc:
            message = str(exc)

    assert "field 'project.include_audit' must be a JSON boolean" in message

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_project_artifacts_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
