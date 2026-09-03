#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_install_switchyard_onboarding_docs_warns_and_continues_when_source_missing() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-onboarding-missing.") as tmp:
        tmp_path = Path(tmp)
        missing_source = tmp_path / "missing-source"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        output: list[str] = []

        team_launcher._install_switchyard_onboarding_docs(
            source_repo=missing_source,
            project_dir=project_dir,
            owner_user=current_user,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )

        target_dir_exists = (project_dir / "docs" / "onboarding").exists()

    assert target_dir_exists is False
    assert output == [
        "warning: switchyard: onboarding docs source "
        f"{missing_source / 'docs' / 'onboarding'} is missing; skipped installing "
        f"{', '.join(team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES)} into {project_dir / 'docs' / 'onboarding'}. "
        "Copy them later from docs/onboarding/ in the Switchyard source checkout."
    ]

def test_onboarding_packet_service_doc_references_are_source_checkout_scoped() -> None:
    packet_dir = ROOT / "docs" / "onboarding"
    references: list[tuple[str, str]] = []
    for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
        for line in packet_dir.joinpath(name).read_text(encoding="utf-8").splitlines():
            if "ticket-board-service.md" in line:
                references.append((name, line.strip()))

    assert references
    for name, line in references:
        assert "Switchyard source checkout" in line, (name, line)
        assert "`ticket-board-service.md`" not in line, (name, line)
        assert "../ticket-board-service.md" not in line, (name, line)

def test_new_project_git_init_creates_main_branch_initial_commit_and_worktrees() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-git-init.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        created = team_launcher._ensure_project_git_repository(
            owner_user=current_user,
            project_dir=project_dir,
            branch="main",
            runner=subprocess.run,
        )

        role_worktree = tmp_path / "role-worktree"
        _run_git(["git", "worktree", "add", "--detach", str(role_worktree), "HEAD"], cwd=project_dir)
        git_listing = subprocess.check_output(["ls", "-ld", str(project_dir / ".git")], text=True)

        assert created is True
        assert _run_git(["git", "branch", "--show-current"], cwd=project_dir).stdout.strip() == "main"
        assert _run_git(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip() == "1"
        assert _run_git(["git", "remote", "get-url", "origin"], cwd=project_dir).stdout.strip() == str(project_dir)
        assert (role_worktree / ".git").is_file()
        assert current_user in git_listing

def test_switchyard_new_from_artifact_initializes_repo_on_configured_worktree_branch() -> None:
    current_user = team_launcher.current_user_name()
    original_precheck_project_path = team_launcher._precheck_project_path_before_mutating
    original_precheck_new_project = team_launcher.precheck_new_project
    original_ensure_owner = team_launcher._ensure_owner_user_and_project_dir
    original_chown_project_files = team_launcher._chown_switchyard_project_files
    original_prepare_auth_worktrees = team_launcher._prepare_first_run_auth_worktrees
    original_run_first_run_auth_phase = team_launcher.run_first_run_auth_phase
    original_launch_project = team_launcher.launch_project
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-artifact-branch.") as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "home" / current_user / "Projects" / "atlas"
            source_repo = tmp_path / "source-repo"
            output_dir = project_dir / ".switchyard" / "provision"
            registry_dir = tmp_path / "registry"
            design_document = project_dir / "PROJECT_DESIGN.md"
            artifact_path = tmp_path / "atlas.project.json"
            output: list[str] = []
            source_repo.mkdir()
            artifact_path.write_text(
                json.dumps(
                    {
                        "schema": "switchyard.project.v1",
                        "design_document": str(design_document),
                        "project": {
                            "slug": "atlas",
                            "name": "Atlas Planner",
                            "ticket_prefix": "ATL",
                            "owner_user": current_user,
                            "repository": str(project_dir),
                            "remote": "origin",
                            "default_branch": "trunk",
                            "worktree_policy": "isolated",
                            "roles": ["ops"],
                            "role_clis": {"director": "claude", "ops": "codex"},
                            "include_designer": False,
                            "include_audit": False,
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            team_launcher._precheck_project_path_before_mutating = lambda _owner_user, _project_dir: None
            team_launcher.precheck_new_project = lambda *_args, **_kwargs: None

            def fake_ensure_owner(
                _owner_user: str,
                requested_project_dir: Path,
                **_kwargs: object,
            ) -> team_launcher.OwnerUserProvisionResult:
                requested_project_dir.mkdir(parents=True, exist_ok=True)
                return team_launcher.OwnerUserProvisionResult(created=False, linger_enabled=False)

            def pass_real_git_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:1] == ["git"]:
                    run_kwargs = dict(kwargs)
                    run_kwargs.setdefault("text", True)
                    run_kwargs.setdefault("stdout", subprocess.PIPE)
                    run_kwargs.setdefault("stderr", subprocess.PIPE)
                    return subprocess.run(args, **run_kwargs)
                return subprocess.CompletedProcess(args, 0)

            def fake_launch_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                assert worktree_ref(config) == "origin/trunk"
                return 0

            team_launcher._ensure_owner_user_and_project_dir = fake_ensure_owner
            team_launcher._chown_switchyard_project_files = lambda *_args, **_kwargs: None
            team_launcher._prepare_first_run_auth_worktrees = lambda *_args, **_kwargs: None
            team_launcher.run_first_run_auth_phase = lambda *_args, **_kwargs: team_launcher.FirstRunAuthReport({}, [])
            team_launcher.launch_project = fake_launch_project

            with redirect_stdout(StringIO()):
                assert (
                    switchyard_new_command(
                        from_artifact=artifact_path,
                        source_repo=source_repo,
                        output_dir=output_dir,
                        yes=True,
                        allow_existing_owner_user=True,
                        home_base=tmp_path / "home",
                        euid_getter=lambda: 0,
                        runner=pass_real_git_runner,
                        pane_state_dir=tmp_path / "pane-state",
                        port_in_use=lambda _port: False,
                        socket_exists=lambda _path: False,
                        session_record_timeout=0,
                        registry_dir=registry_dir,
                        print_func=output.append,
                    )
                    == 0
                )

            config = load_project_config("atlas", output_dir / "atlas.json")
            branch = _run_git(["git", "branch", "--show-current"], cwd=project_dir).stdout.strip()
            commits = _run_git(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip()
            remote = _run_git(["git", "remote", "get-url", "origin"], cwd=project_dir).stdout.strip()
            _run_git(["git", "fetch", "origin", "trunk:refs/remotes/origin/trunk"], cwd=project_dir)
            role_worktree = tmp_path / "atlas-ops-worktree"
            _run_git(["git", "worktree", "add", "--detach", str(role_worktree), worktree_ref(config)], cwd=project_dir)

            assert config.worktree_branch == "trunk"
            assert worktree_ref(config) == "origin/trunk"
            assert branch == "trunk"
            assert commits == "2"
            assert remote == str(project_dir)
            assert (role_worktree / ".git").is_file()
            assert any(
                line == f"switchyard: initialized git repository in {project_dir} on branch trunk with an initial commit"
                for line in output
            )

    finally:
        team_launcher._precheck_project_path_before_mutating = original_precheck_project_path
        team_launcher.precheck_new_project = original_precheck_new_project
        team_launcher._ensure_owner_user_and_project_dir = original_ensure_owner
        team_launcher._chown_switchyard_project_files = original_chown_project_files
        team_launcher._prepare_first_run_auth_worktrees = original_prepare_auth_worktrees
        team_launcher.run_first_run_auth_phase = original_run_first_run_auth_phase
        team_launcher.launch_project = original_launch_project

def test_new_project_from_artifact_provisions_multiple_audit_roles() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-multi-audit.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project"
        source_repo = tmp_path / "source"
        output_dir = tmp_path / "out"
        design_document = tmp_path / "PROJECT_DESIGN.md"
        artifact_path = tmp_path / "atlas.project.json"
        project_dir.mkdir()
        source_repo.mkdir()
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": str(design_document),
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Planner",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_dir),
                        "remote": "origin",
                        "default_branch": "main",
                        "worktree_policy": "isolated",
                        "roles": ["main"],
                        "audit_roles": ["audit_gemini", "audit_gpt"],
                        "role_clis": {
                            "director": "claude",
                            "audit_gemini": "agy",
                            "audit_gpt": "codex",
                            "main": "codex",
                        },
                        "include_designer": False,
                        "include_audit": True,
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
                output_dir=output_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                require_owner_user=False,
            )
            == 0
        )

        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((output_dir / "atlas.json").read_text(encoding="utf-8"))
        board_unit = (output_dir / "atlas-ticket-board.service").read_text(encoding="utf-8")
        workflow_sql = (output_dir / "atlas-workflow.sql").read_text(encoding="utf-8")

    assert plan["audit_roles"] == ["audit_gemini", "audit_gpt"]
    assert plan["implementer_roles"] == ["main"]
    assert plan["assignee_roles"] == ["unassigned", "main", "audit_gemini", "audit_gpt", "director", "user"]
    assert plan["caller_roles"] == ["director", "main", "audit_gemini", "audit_gpt", "user"]
    assert [role["role"] for role in config["roles"]] == ["director", "audit_gemini", "audit_gpt", "main"]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "director": ["claude"],
        "audit_gemini": ["agy"],
        "audit_gpt": ["codex"],
        "main": ["codex"],
    }
    assert "TICKET_BOARD_CALLER_ROLES=director,main,audit_gemini,audit_gpt,user" in board_unit
    assert "audit_sign_off=audit_gemini,audit_gpt" in board_unit
    assert "('audit', 'Audit', 3, ARRAY['audit_gemini', 'audit_gpt']::text[]" in workflow_sql
    assert "('audit', 'director_review', 'audit_sign_off', ARRAY['audit_gemini', 'audit_gpt']::text[]" in workflow_sql

def test_new_project_git_init_leaves_existing_repo_config_branch_and_head_untouched() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-existing-git.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        _run_git(["git", "init", "-b", "trunk"], cwd=project_dir)
        (project_dir / "README.md").write_text("Existing project.\n", encoding="utf-8")
        _run_git(["git", "add", "README.md"], cwd=project_dir)
        _run_git(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Initial"], cwd=project_dir)
        before_branch = _run_git(["git", "branch", "--show-current"], cwd=project_dir).stdout.strip()
        before_head = _run_git(["git", "rev-parse", "HEAD"], cwd=project_dir).stdout.strip()
        before_config = _run_git(["git", "config", "--local", "--list"], cwd=project_dir).stdout

        created = team_launcher._ensure_project_git_repository(
            owner_user=current_user,
            project_dir=project_dir,
            branch="main",
            runner=subprocess.run,
        )

        assert created is False
        assert _run_git(["git", "branch", "--show-current"], cwd=project_dir).stdout.strip() == before_branch
        assert _run_git(["git", "rev-parse", "HEAD"], cwd=project_dir).stdout.strip() == before_head
        assert _run_git(["git", "config", "--local", "--list"], cwd=project_dir).stdout == before_config
        assert subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_dir,
            text=True,
            capture_output=True,
        ).returncode != 0
        assert _run_git(["git", "status", "--porcelain"], cwd=project_dir).stdout == ""

def test_switchyard_new_no_git_init_skips_and_refuses_absent_repo() -> None:
    current_user = team_launcher.current_user_name()
    original_precheck_new_project = team_launcher.precheck_new_project
    original_ensure_peer_auth = team_launcher._ensure_board_service_peer_auth
    original_ensure_owner = team_launcher._ensure_owner_user_and_project_dir
    original_chown_project_files = team_launcher._chown_switchyard_project_files
    original_chown_project_file = team_launcher._chown_project_file
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-no-git-init.") as tmp:
            tmp_path = Path(tmp)
            project_dir = tmp_path / "project"
            source_repo = tmp_path / "source"
            source_repo.mkdir()
            output: list[str] = []

            team_launcher.precheck_new_project = lambda *_args, **_kwargs: None
            team_launcher._ensure_board_service_peer_auth = lambda *_args, **_kwargs: None

            def fake_ensure_owner(
                _owner_user: str,
                requested_project_dir: Path,
                **_kwargs: object,
            ) -> team_launcher.OwnerUserProvisionResult:
                requested_project_dir.mkdir(parents=True, exist_ok=True)
                return team_launcher.OwnerUserProvisionResult(created=False, linger_enabled=False)

            team_launcher._ensure_owner_user_and_project_dir = fake_ensure_owner
            team_launcher._chown_switchyard_project_files = lambda *_args, **_kwargs: None
            team_launcher._chown_project_file = lambda *_args, **_kwargs: None

            try:
                switchyard_new_command(
                    slug="porter",
                    agent_name=current_user,
                    project_name="Porter System",
                    project_path=project_dir,
                    source_repo=source_repo,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    allow_existing_owner_user=True,
                    euid_getter=lambda: 0,
                    runner=subprocess.run,
                    git_init=False,
                    input_func=lambda _prompt: "",
                    print_func=output.append,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    registry_dir=tmp_path / "registry",
                )
                raise AssertionError("expected --no-git-init absent-repo failure")
            except SystemExit as exc:
                message = str(exc)
    finally:
        team_launcher.precheck_new_project = original_precheck_new_project
        team_launcher._ensure_board_service_peer_auth = original_ensure_peer_auth
        team_launcher._ensure_owner_user_and_project_dir = original_ensure_owner
        team_launcher._chown_switchyard_project_files = original_chown_project_files
        team_launcher._chown_project_file = original_chown_project_file

    assert f"switchyard: skipped project git initialization for {project_dir} (--no-git-init)" in output
    assert "is not a git repository" in message
    assert not (project_dir / ".git").exists()

def test_switchyard_new_validates_models_before_launching_panes() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_precheck_project_path = team_launcher._precheck_project_path_before_mutating
    original_precheck_new_project = team_launcher.precheck_new_project
    original_ensure_owner = team_launcher._ensure_owner_user_and_project_dir
    original_ensure_git = team_launcher._ensure_project_git_repository
    original_chown_project_files = team_launcher._chown_switchyard_project_files
    original_chown_project_file = team_launcher._chown_project_file
    original_new_project = team_launcher.new_project_command
    original_commit_project_git_changes = team_launcher._commit_project_git_changes
    original_register = team_launcher._register_switchyard_project
    original_prepare_auth_worktrees = team_launcher._prepare_first_run_auth_worktrees
    original_launch_project = team_launcher.launch_project
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-model-validation.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            project_dir = home_base / "porter-agent" / "Projects" / "porter_system"
            source_repo = tmp_path / "source-repo"
            output_dir = tmp_path / "provision"
            source_repo.mkdir()
            launch_calls: list[team_launcher.ProjectConfig] = []
            output: list[str] = []

            team_launcher.uid_for_user = lambda _user_name: None
            team_launcher._precheck_project_path_before_mutating = lambda _owner_user, _project_dir: None
            team_launcher.precheck_new_project = lambda *_args, **_kwargs: None

            def fake_ensure_owner(
                _owner_user: str,
                requested_project_dir: Path,
                **_kwargs: object,
            ) -> team_launcher.OwnerUserProvisionResult:
                requested_project_dir.mkdir(parents=True, exist_ok=True)
                return team_launcher.OwnerUserProvisionResult(created=False, linger_enabled=False)

            def fake_new_project(project: str, **kwargs: object) -> int:
                provision_dir = Path(kwargs["output_dir"])
                layout = provision_dir / f"{project}-layout.json"
                role_workdir = project_dir / "worktrees" / "director"
                provision_dir.mkdir(parents=True, exist_ok=True)
                role_workdir.mkdir(parents=True, exist_ok=True)
                layout.write_text(
                    '{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n',
                    encoding="utf-8",
                )
                (provision_dir / f"{project}.json").write_text(
                    json.dumps(
                        {
                            "project": project,
                            "project_name": "Porter System",
                            "layout": str(layout),
                            "repository": str(project_dir),
                            "run_as_user": "porter-agent",
                            "roles": [
                                {
                                    "role": "director",
                                    "slot": 0,
                                    "target": "porter-director:0.0",
                                    "tmux_session": "porter-director",
                                    "workdir": str(role_workdir),
                                    "cli": ["codex"],
                                    "model": "openai/not-a-model",
                                }
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return 0

            def fake_launch_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                launch_calls.append(config)
                return 0

            team_launcher._ensure_owner_user_and_project_dir = fake_ensure_owner
            team_launcher._ensure_project_git_repository = lambda *_args, **_kwargs: None
            team_launcher._chown_switchyard_project_files = lambda *_args, **_kwargs: None
            team_launcher._chown_project_file = lambda *_args, **_kwargs: None
            team_launcher.new_project_command = fake_new_project
            team_launcher._commit_project_git_changes = lambda *_args, **_kwargs: None
            team_launcher._register_switchyard_project = lambda config_path, **_kwargs: config_path
            team_launcher._prepare_first_run_auth_worktrees = lambda *_args, **_kwargs: None
            team_launcher.launch_project = fake_launch_project

            runner = FirstRunAuthRunner(
                invalid_models={("codex", "openai/not-a-model"): "model not found: openai/not-a-model\n"}
            )
            runner.login_seen.add("codex")

            result = switchyard_new_command(
                slug="porter",
                agent_name="porter-agent",
                project_name="Porter System",
                project_path=project_dir,
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=(("director", "codex"), ("ops", "codex")),
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                print_func=output.append,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                registry_dir=tmp_path / "registry",
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher._precheck_project_path_before_mutating = original_precheck_project_path
        team_launcher.precheck_new_project = original_precheck_new_project
        team_launcher._ensure_owner_user_and_project_dir = original_ensure_owner
        team_launcher._ensure_project_git_repository = original_ensure_git
        team_launcher._chown_switchyard_project_files = original_chown_project_files
        team_launcher._chown_project_file = original_chown_project_file
        team_launcher.new_project_command = original_new_project
        team_launcher._commit_project_git_changes = original_commit_project_git_changes
        team_launcher._register_switchyard_project = original_register
        team_launcher._prepare_first_run_auth_worktrees = original_prepare_auth_worktrees
        team_launcher.launch_project = original_launch_project

    assert result == 1
    assert launch_calls == []
    assert runner.calls == [
        ["getent", "passwd", "boardsvc"],
        [str(source_repo / "scripts" / "ticket-board-boardsvc-setup.sh"), "--apply-peer-auth"],
        ["sudo", "-u", "porter-agent", "codex", "login", "status"],
        [
            "sudo",
            "-u",
            "porter-agent",
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            "openai/not-a-model",
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
    ]
    assert output[-1] == (
        "warning: switchyard: model validation failed for role director "
        "(cli codex, model openai/not-a-model): model not found: openai/not-a-model; "
        "no model list is available for codex; validation used a one-shot prompt"
    )


def test_switchyard_new_missing_owner_clis_stops_before_launching_panes() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_precheck_project_path = team_launcher._precheck_project_path_before_mutating
    original_precheck_new_project = team_launcher.precheck_new_project
    original_ensure_owner = team_launcher._ensure_owner_user_and_project_dir
    original_ensure_git = team_launcher._ensure_project_git_repository
    original_chown_project_files = team_launcher._chown_switchyard_project_files
    original_chown_project_file = team_launcher._chown_project_file
    original_new_project = team_launcher.new_project_command
    original_commit_project_git_changes = team_launcher._commit_project_git_changes
    original_register = team_launcher._register_switchyard_project
    original_prepare_auth_worktrees = team_launcher._prepare_first_run_auth_worktrees
    original_launch_project = team_launcher.launch_project
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-missing-cli.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            project_dir = home_base / "test-agent" / "Projects" / "test"
            source_repo = tmp_path / "source-repo"
            output_dir = tmp_path / "provision"
            source_repo.mkdir()
            launch_calls: list[team_launcher.ProjectConfig] = []
            output: list[str] = []

            team_launcher.uid_for_user = lambda _user_name: None
            team_launcher._precheck_project_path_before_mutating = lambda _owner_user, _project_dir: None
            team_launcher.precheck_new_project = lambda *_args, **_kwargs: None

            def fake_ensure_owner(
                _owner_user: str,
                requested_project_dir: Path,
                **_kwargs: object,
            ) -> team_launcher.OwnerUserProvisionResult:
                requested_project_dir.mkdir(parents=True, exist_ok=True)
                return team_launcher.OwnerUserProvisionResult(created=False, linger_enabled=False)

            def fake_new_project(project: str, **kwargs: object) -> int:
                provision_dir = Path(kwargs["output_dir"])
                layout = provision_dir / f"{project}-layout.json"
                provision_dir.mkdir(parents=True, exist_ok=True)
                layout.write_text(
                    '{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n',
                    encoding="utf-8",
                )
                roles = [
                    ("designer", "claude"),
                    ("director", "claude"),
                    ("audit", "claude"),
                    ("main", "codex"),
                    ("ops", "codex"),
                    ("app", "codex"),
                ]
                (provision_dir / f"{project}.json").write_text(
                    json.dumps(
                        {
                            "project": project,
                            "project_name": "Test",
                            "layout": str(layout),
                            "repository": str(project_dir),
                            "run_as_user": "test-agent",
                            "roles": [
                                {
                                    "role": role,
                                    "slot": index,
                                    "target": f"test-{role}:0.0",
                                    "tmux_session": f"test-{role}",
                                    "workdir": str(project_dir / "worktrees" / role),
                                    "cli": [cli],
                                }
                                for index, (role, cli) in enumerate(roles)
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return 0

            def fake_launch_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                launch_calls.append(config)
                output.append("team-launcher: role designer did not leave a live test-designer session")
                return 1

            team_launcher._ensure_owner_user_and_project_dir = fake_ensure_owner
            team_launcher._ensure_project_git_repository = lambda *_args, **_kwargs: None
            team_launcher._chown_switchyard_project_files = lambda *_args, **_kwargs: None
            team_launcher._chown_project_file = lambda *_args, **_kwargs: None
            team_launcher.new_project_command = fake_new_project
            team_launcher._commit_project_git_changes = lambda *_args, **_kwargs: None
            team_launcher._register_switchyard_project = lambda config_path, **_kwargs: config_path
            team_launcher._prepare_first_run_auth_worktrees = lambda *_args, **_kwargs: None
            team_launcher.launch_project = fake_launch_project

            runner = FirstRunAuthRunner(missing_clis={"claude", "codex"})

            result = switchyard_new_command(
                slug="test",
                agent_name="test-agent",
                project_name="Test",
                project_path=project_dir,
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=(
                    ("designer", "claude"),
                    ("director", "claude"),
                    ("audit", "claude"),
                    ("main", "codex"),
                    ("ops", "codex"),
                    ("app", "codex"),
                ),
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                print_func=output.append,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                registry_dir=tmp_path / "registry",
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher._precheck_project_path_before_mutating = original_precheck_project_path
        team_launcher.precheck_new_project = original_precheck_new_project
        team_launcher._ensure_owner_user_and_project_dir = original_ensure_owner
        team_launcher._ensure_project_git_repository = original_ensure_git
        team_launcher._chown_switchyard_project_files = original_chown_project_files
        team_launcher._chown_project_file = original_chown_project_file
        team_launcher.new_project_command = original_new_project
        team_launcher._commit_project_git_changes = original_commit_project_git_changes
        team_launcher._register_switchyard_project = original_register
        team_launcher._prepare_first_run_auth_worktrees = original_prepare_auth_worktrees
        team_launcher.launch_project = original_launch_project

    assert result == 1
    assert launch_calls == []
    assert any(
        line == (
            "switchyard: first-run setup manifest for owner user test-agent: "
            "0 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 2 missing CLI(s)"
        )
        for line in output
    )
    assert any("claude (roles: designer, director, audit)" in line for line in output)
    assert any("codex (roles: main, ops, app)" in line for line in output)
    assert any("Install the missing CLI(s) for owner user test-agent and rerun switchyard." in line for line in output)
    assert not any("did not leave a live" in line for line in output)


def test_switchyard_new_reports_existing_owner_with_broken_shell_without_repairing() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_getpwnam = team_launcher.pwd.getpwnam
    original_precheck_project_path = team_launcher._precheck_project_path_before_mutating
    original_precheck_new_project = team_launcher.precheck_new_project
    original_ensure_owner = team_launcher._ensure_owner_user_and_project_dir
    original_ensure_git = team_launcher._ensure_project_git_repository
    original_chown_project_files = team_launcher._chown_switchyard_project_files
    original_chown_project_file = team_launcher._chown_project_file
    original_new_project = team_launcher.new_project_command
    original_commit_project_git_changes = team_launcher._commit_project_git_changes
    original_register = team_launcher._register_switchyard_project
    original_prepare_auth_worktrees = team_launcher._prepare_first_run_auth_worktrees
    original_launch_project = team_launcher.launch_project
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-broken-owner-shell.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            owner_home = home_base / "test-agent"
            project_dir = owner_home / "Projects" / "test"
            source_repo = tmp_path / "source-repo"
            output_dir = tmp_path / "provision"
            broken_shell = tmp_path / "missing-fish"
            source_repo.mkdir()
            output: list[str] = []
            launch_calls: list[team_launcher.ProjectConfig] = []

            class FakeUserInfo:
                pw_dir = str(owner_home)
                pw_shell = str(broken_shell)

            team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "test-agent" else None
            team_launcher.pwd.getpwnam = lambda user_name: FakeUserInfo() if user_name == "test-agent" else original_getpwnam(user_name)
            team_launcher._precheck_project_path_before_mutating = lambda _owner_user, _project_dir: None
            team_launcher.precheck_new_project = lambda *_args, **_kwargs: None

            def fake_ensure_owner(
                _owner_user: str,
                requested_project_dir: Path,
                **_kwargs: object,
            ) -> team_launcher.OwnerUserProvisionResult:
                requested_project_dir.mkdir(parents=True, exist_ok=True)
                return team_launcher.OwnerUserProvisionResult(created=False, linger_enabled=False)

            def fake_new_project(project: str, **kwargs: object) -> int:
                provision_dir = Path(kwargs["output_dir"])
                layout = provision_dir / f"{project}-layout.json"
                provision_dir.mkdir(parents=True, exist_ok=True)
                layout.write_text(
                    '{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n',
                    encoding="utf-8",
                )
                (provision_dir / f"{project}.json").write_text(
                    json.dumps(
                        {
                            "project": project,
                            "project_name": "Test",
                            "layout": str(layout),
                            "repository": str(project_dir),
                            "run_as_user": "test-agent",
                            "roles": [
                                {
                                    "role": "director",
                                    "slot": 0,
                                    "target": "test-director:0.0",
                                    "tmux_session": "test-director",
                                    "workdir": str(project_dir / "worktrees" / "director"),
                                    "cli": ["codex"],
                                },
                                {
                                    "role": "ops",
                                    "slot": 1,
                                    "target": "test-ops:0.0",
                                    "tmux_session": "test-ops",
                                    "workdir": str(project_dir / "worktrees" / "ops"),
                                    "cli": ["codex"],
                                },
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return 0

            def fake_launch_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                launch_calls.append(config)
                return 0

            team_launcher._ensure_owner_user_and_project_dir = fake_ensure_owner
            team_launcher._ensure_project_git_repository = lambda *_args, **_kwargs: None
            team_launcher._chown_switchyard_project_files = lambda *_args, **_kwargs: None
            team_launcher._chown_project_file = lambda *_args, **_kwargs: None
            team_launcher.new_project_command = fake_new_project
            team_launcher._commit_project_git_changes = lambda *_args, **_kwargs: None
            team_launcher._register_switchyard_project = lambda config_path, **_kwargs: config_path
            team_launcher._prepare_first_run_auth_worktrees = lambda *_args, **_kwargs: None
            team_launcher.launch_project = fake_launch_project

            runner = FirstRunAuthRunner()
            runner.login_seen.add("codex")

            result = switchyard_new_command(
                slug="test",
                agent_name="test-agent",
                project_name="Test",
                project_path=project_dir,
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=(("director", "codex"), ("ops", "codex")),
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                print_func=output.append,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                registry_dir=tmp_path / "registry",
                session_record_timeout=0,
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.pwd.getpwnam = original_getpwnam
        team_launcher._precheck_project_path_before_mutating = original_precheck_project_path
        team_launcher.precheck_new_project = original_precheck_new_project
        team_launcher._ensure_owner_user_and_project_dir = original_ensure_owner
        team_launcher._ensure_project_git_repository = original_ensure_git
        team_launcher._chown_switchyard_project_files = original_chown_project_files
        team_launcher._chown_project_file = original_chown_project_file
        team_launcher.new_project_command = original_new_project
        team_launcher._commit_project_git_changes = original_commit_project_git_changes
        team_launcher._register_switchyard_project = original_register
        team_launcher._prepare_first_run_auth_worktrees = original_prepare_auth_worktrees
        team_launcher.launch_project = original_launch_project

    assert result == 0
    assert len(launch_calls) == 1
    assert any(line == "switchyard: using existing user test-agent (not modifying)" for line in output)
    assert any(
        line == (
            "switchyard: first-run setup manifest for owner user test-agent: "
            "0 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s), "
            "1 owner shell issue(s)"
        )
        for line in output
    )
    assert any(
        line == (
            f"switchyard: owner shell for test-agent is not executable: {broken_shell}; "
            "repair with `sudo usermod -s /bin/bash test-agent`"
        )
        for line in output
    )
    assert any(
        line == (
            f"warning: switchyard: owner shell for test-agent is not executable: {broken_shell}; "
            "repair with `sudo usermod -s /bin/bash test-agent`"
        )
        for line in output
    )
    assert not any("usermod" in token for command in runner.calls for token in command)


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_onboarding_git_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
