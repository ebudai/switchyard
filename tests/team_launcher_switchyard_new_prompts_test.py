#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_switchyard_new_unit_without_database_precheck_runs_before_user_and_project_creation() -> None:
    class UnitWithoutDatabaseRunner(FakeRunner):
        def __init__(self, *, home_base: Path) -> None:
            super().__init__()
            self.home_base = home_base

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service disabled\n")
            if args[:2] == ["psql", "-XAt"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if args == ["id", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                user_home = self.home_base / "otto-agent"
                user_home.mkdir(parents=True, exist_ok=True)
                (user_home / ".created-by-useradd").write_text("created\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
        created_user_marker = home_base / "otto-agent" / ".created-by-useradd"
        runner = UnitWithoutDatabaseRunner(home_base=home_base)

        try:
            switchyard_new_command(
                desktop_policy=Path("headless"),
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter System",
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected unit-without-database precheck failure")
        except SystemExit as exc:
            message = str(exc)

        assert not created_user_marker.exists()
        assert not project_dir.exists()
        assert not output_dir.exists()

    assert "porter-ticket-board.service is installed" in message
    assert "database 'porter_ticket_board' does not exist" in message
    assert not any(call[:1] == ["useradd"] for call in runner.calls)
    assert not any(call == ["loginctl", "enable-linger", "otto-agent"] for call in runner.calls)
    assert not any(call[:1] == ["install"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

def test_switchyard_new_prompts_name_first_and_derives_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        prompts: list[str] = []
        output: list[str] = []
        answers = iter(["Otto Scheduler", "", "", ""])

        def input_func(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        try:
            switchyard_new_command(
                desktop_policy=Path("headless"),
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=input_func,
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto_scheduler-agent" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert prompts == [
        "Project name: ",
        "Slug [otto_scheduler]: ",
        "Agent user [otto_scheduler-agent]: ",
        f"Project path [{expected_path}]: ",
    ]
    agent_prompt = prompts[2]
    agent_default = agent_prompt.removeprefix("Agent user [").removesuffix("]: ")
    owner_summary = next(line.removeprefix("switchyard: owner user: ") for line in output if line.startswith("switchyard: owner user: "))
    assert agent_default == owner_summary == "otto_scheduler-agent"
    assert f"switchyard: project path: {expected_path}" in output

def test_switchyard_new_typed_agent_user_is_verbatim_not_suffixed() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda _user_name: None
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            prompts: list[str] = []
            output: list[str] = []
            answers = iter(["Otto Scheduler", "", "eric", ""])

            def input_func(prompt: str) -> str:
                prompts.append(prompt)
                return next(answers)

            try:
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 1000,
                    runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                    input_func=input_func,
                    print_func=output.append,
                )
                raise AssertionError("expected sudo precheck failure")
            except SystemExit as exc:
                message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    expected_path = home_base / "eric" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert prompts == [
        "Project name: ",
        "Slug [otto_scheduler]: ",
        "Agent user [otto_scheduler-agent]: ",
        f"Project path [{expected_path}]: ",
    ]
    assert "switchyard: owner user: eric" in output
    assert f"switchyard: project path: {expected_path}" in output

def test_switchyard_new_agent_name_argument_is_verbatim_not_suffixed() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda _user_name: None
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            output: list[str] = []

            try:
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="otto",
                    agent_name="eric",
                    project_name="Otto Scheduler",
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 1000,
                    runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                    input_func=lambda _prompt: "",
                    print_func=output.append,
                )
                raise AssertionError("expected sudo precheck failure")
            except SystemExit as exc:
                message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    expected_path = home_base / "eric" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert "switchyard: owner user: eric" in output
    assert f"switchyard: project path: {expected_path}" in output

def test_new_project_owner_user_argument_is_verbatim_not_suffixed() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()

        assert (
            new_project_command(
                "otto",
                owner_user="eric",
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

        config = json.loads((output_dir / "otto.json").read_text(encoding="utf-8"))

    assert config["run_as_user"] == "eric"

def test_switchyard_new_existing_owner_decline_aborts_before_mutating() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_home_dir_for_user = team_launcher.home_dir_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: 1000 if user_name == "eric" else None
        team_launcher.home_dir_for_user = lambda user_name: Path("/home/eric") if user_name == "eric" else None
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-existing-owner.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            output_dir = tmp_path / "out"
            calls: list[list[str]] = []
            prompts: list[str] = []
            output: list[str] = []

            def input_func(prompt: str) -> str:
                prompts.append(prompt)
                return "n"

            try:
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="otto",
                    agent_name="eric",
                    project_name="Otto Scheduler",
                    project_path=home_base / "eric" / "Projects" / "otto_scheduler",
                    output_dir=output_dir,
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                    input_func=input_func,
                    print_func=output.append,
                )
                raise AssertionError("expected existing owner cancellation")
            except SystemExit as exc:
                message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.home_dir_for_user = original_home_dir_for_user

    assert message == "switchyard: cancelled"
    assert prompts == ["Use existing owner user eric (uid 1000, home /home/eric) [y/N]: "]
    assert output == ["warning: switchyard: owner user eric (uid 1000, home /home/eric) already exists"]
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "eric").exists()

def test_switchyard_new_existing_owner_noninteractive_requires_explicit_opt_in() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_home_dir_for_user = team_launcher.home_dir_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: 1000 if user_name == "eric" else None
        team_launcher.home_dir_for_user = lambda user_name: Path("/home/eric") if user_name == "eric" else None
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-existing-owner.") as tmp:
            tmp_path = Path(tmp)
            home_base = tmp_path / "home"
            output: list[str] = []

            try:
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="otto",
                    agent_name="eric",
                    project_name="Otto Scheduler",
                    project_path=home_base / "eric" / "Projects" / "otto_scheduler",
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                    input_func=lambda _prompt: (_ for _ in ()).throw(EOFError()),
                    print_func=output.append,
                )
                raise AssertionError("expected noninteractive existing-owner failure")
            except SystemExit as exc:
                refused = str(exc)

            try:
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="atlas",
                    agent_name="eric",
                    project_name="Atlas",
                    project_path=home_base / "eric" / "Projects" / "atlas",
                    yes=True,
                    allow_existing_owner_user=True,
                    home_base=home_base,
                    euid_getter=lambda: 1000,
                    runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                    input_func=lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected prompt")),
                    print_func=output.append,
                )
                raise AssertionError("expected sudo precheck failure")
            except SystemExit as exc:
                allowed = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.home_dir_for_user = original_home_dir_for_user

    assert "already exists; pass --allow-existing-owner-user to reuse it" in refused
    assert "requires sudo" in allowed
    assert output.count("warning: switchyard: owner user eric (uid 1000, home /home/eric) already exists") == 2

def test_switchyard_new_edited_slug_does_not_change_name_derived_default_path() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output: list[str] = []

        try:
            switchyard_new_command(
                desktop_policy=Path("headless"),
                project_name="Otto Scheduler",
                slug="otto",
                agent_name="otto-agent",
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=lambda _prompt: "",
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto-agent" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert "switchyard: slug: otto" in output
    assert f"switchyard: project path: {expected_path}" in output

def test_switchyard_new_writes_initial_artifact_and_starts_full_pane_window() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self, *, owner_user: str, project_dir: Path) -> None:
            super().__init__()
            self.call_kwargs: list[dict[str, object]] = []
            self.owner_user = owner_user
            self.project_dir = project_dir
            self.git_output = ""

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.call_kwargs.append(dict(kwargs))
            self.calls.append(args)
            if args[:4] == ["sudo", "-u", self.owner_user, "git"]:
                git_args = args[3:]
                if len(git_args) >= 3 and git_args[:2] == ["git", "-C"] and Path(git_args[2]) == self.project_dir:
                    return self._run_project_git(git_args, **kwargs)
                return subprocess.CompletedProcess(args, 0)
            if args[:4] == ["sudo", "-u", self.owner_user, "mkdir"]:
                return subprocess.CompletedProcess(args, 0)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.project_dir:
                return self._run_project_git(args, **kwargs)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

        def _run_project_git(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            run_kwargs = dict(kwargs)
            run_kwargs.setdefault("text", True)
            if "stdout" not in run_kwargs:
                run_kwargs["stdout"] = subprocess.PIPE
            if "stderr" not in run_kwargs:
                run_kwargs["stderr"] = subprocess.PIPE
            proc = subprocess.run(args, **run_kwargs)
            stdout = str(proc.stdout or "")
            stderr = str(proc.stderr or "")
            self.git_output += stdout + stderr
            return proc

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        pane_state_dir = tmp_path / "pane-state"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        _write_source_onboarding_docs(source_repo)
        project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
        provision_dir = project_dir / ".switchyard" / "provision"
        registry_dir = tmp_path / "registry"
        runner = NewProjectRunner(owner_user="otto-agent", project_dir=project_dir)
        process_launcher = RecordingProcessLauncher()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="porter",
                    agent_name="otto-agent",
                    project_name="Porter System",
                    source_repo=source_repo,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    allow_existing_owner_user=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=pane_state_dir,
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=registry_dir,
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                    konsole_process_launcher=process_launcher,
                )
                == 0
        )

        chown_call_index = next(index for index, call in enumerate(runner.calls) if call[:2] == ["chown", "-R"])
        config = json.loads((provision_dir / "porter.json").read_text(encoding="utf-8"))
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        artifact = json.loads((project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8"))
        generated_layout_path = (
            home_base
            / "otto-agent"
            / ".local"
            / "state"
            / "switchyard"
            / "projects"
            / "porter"
            / "porter-team-layout.json"
        )
        generated_layout = json.loads(generated_layout_path.read_text(encoding="utf-8"))
        generated_commands = _leaf_commands(generated_layout)
        designer_env = next(role["env"] for role in config["roles"] if role["role"] == "designer")
        director_env = next(role["env"] for role in config["roles"] if role["role"] == "director")
        designer_onboarding = (project_dir / ".switchyard" / "DESIGNER_ONBOARDING.md").read_text(encoding="utf-8")
        project_onboarding_dir = project_dir / "docs" / "onboarding"
        registry_pointer = json.loads((registry_dir / "porter.json").read_text(encoding="utf-8"))
        expected_pane_launcher = str(team_launcher.switchyard_shared_pane_launcher())

        assert chown_call_index < next(index for index, call in enumerate(runner.calls) if call == ["sudo", "-v"])
        assert artifact["project"]["repository"] == str(project_dir)
        assert registry_pointer["config_path"] == str((provision_dir / "porter.json").resolve(strict=False))
        assert registry_pointer["name"] == "Porter System"
        assert registry_pointer["slug"] == "porter"
        assert artifact["project"]["roles"] == ["ops", "app", "main"]
        assert plan["implementer_roles"] == ["ops", "app", "main"]
        assert config["pane_launcher"] == expected_pane_launcher
        assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "ops", "app", "main"]
        assert designer_env["SWITCHYARD_PROJECT_ARTIFACT"] == str(project_dir / ".switchyard" / "porter.project.json")
        assert designer_env["SWITCHYARD_DESIGNER_ONBOARDING"].endswith("DESIGNER_ONBOARDING.md")
        assert director_env["SWITCHYARD_PROJECT_DESIGN"] == str(project_dir / "PROJECT_DESIGN.md")
        assert director_env["SWITCHYARD_DIRECTOR_ONBOARDING"].endswith("DIRECTOR_ONBOARDING.md")
        assert len(generated_commands) == 6
        assert all(f"{expected_pane_launcher} porter pane attach-or-start" in command for command in generated_commands)
        assert not any(f"{ROOT / 'scripts' / 'team-launcher'} porter pane attach-or-start" in command for command in generated_commands)
        assert not any("switchyard porter pane attach-or-start" in command for command in generated_commands)
        assert _run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=project_dir).stdout.strip() == "true"
        assert _run_git(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip() == "2"
        assert _run_git(["git", "remote", "get-url", "origin"], cwd=project_dir).stdout.strip() == str(project_dir)
        assert (provision_dir / "porter.json").is_file()
        assert (provision_dir / "operator-commands.sh").is_file()
        assert "local repository as a bootstrap placeholder" in designer_onboarding
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            target_doc = project_onboarding_dir / name
            assert target_doc.is_file()
            assert f"source docs/onboarding/{name}" in target_doc.read_text(encoding="utf-8")
            assert ["chown", "otto-agent:otto-agent", str(target_doc)] in runner.calls
        role_worktree = tmp_path / "porter-ops-worktree"
        _run_git(["git", "worktree", "add", "--detach", str(role_worktree), "HEAD"], cwd=project_dir)
        assert (role_worktree / ".git").is_file()
        assert any(call == team_launcher._owner_git_args("otto-agent", project_dir, "init", "-b", "main") for call in runner.calls)
        assert any(
            call[:6] == ["sudo", "-u", "otto-agent", "git", "-C", str(project_dir)]
            and "commit" in call
            and call[-2:] == ["-m", "Record Switchyard provisioning artifacts"]
            for call in runner.calls
        )
        assert any(call[:6] == ["sudo", "-u", "otto-agent", "git", "-C", str(project_dir)] for call in runner.calls)
        assert "not a git repository" not in runner.git_output
        assert not any(call[:1] in (["env"], ["sh"]) and "konsole" in call for call in runner.calls)
        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        output = stdout.getvalue()
        assert f"switchyard: installed onboarding docs into {project_onboarding_dir}: " in output
        assert "switchyard: full pane window started for porter" in output
        assert output.count("warning: switchyard: session record missing for ") == 6
        assert "warning: switchyard: session record missing for designer (porter-designer:0.0)" in output
        assert "warning: switchyard: session record missing for main (porter-main:0.0)" in output
        assert "Konsole Ctrl+Shift+E" in output
        assert f"switchyard: initialized git repository in {project_dir} on branch main with an initial commit" in output

def test_install_switchyard_onboarding_docs_stamps_provenance_and_skips_existing_files() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-onboarding-docs.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        source_repo.mkdir()
        source_commit = _write_source_onboarding_docs(source_repo, initialize_git=True)
        target_dir = project_dir / "docs" / "onboarding"
        target_dir.mkdir(parents=True)
        existing_readme = target_dir / "README.md"
        existing_readme.write_text("# Edited README\n\nKeep me.\n", encoding="utf-8")
        output: list[str] = []

        team_launcher._install_switchyard_onboarding_docs(
            source_repo=source_repo,
            project_dir=project_dir,
            owner_user=current_user,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )

        listing = subprocess.check_output(["ls", "-l", str(target_dir)], text=True)

        assert existing_readme.read_text(encoding="utf-8") == "# Edited README\n\nKeep me.\n"
        assert f"switchyard: skipped existing onboarding docs in {target_dir}: README.md" in output
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES[1:]:
            text = target_dir.joinpath(name).read_text(encoding="utf-8")
            assert f"source commit {source_commit}" in text
            assert f"source docs/onboarding/{name}" in text
            assert f"Source copy for {name}." in text
        assert current_user in listing

def test_switchyard_upgrade_refreshes_only_unedited_onboarding_docs() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-onboarding-upgrade.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_dir = tmp_path / "project"
        provision_dir = project_dir / ".switchyard" / "provision"
        source_repo.mkdir()
        provision_dir.mkdir(parents=True)
        old_commit = _write_source_onboarding_docs(source_repo, initialize_git=True)
        target_dir = project_dir / "docs" / "onboarding"
        output: list[str] = []
        team_launcher._install_switchyard_onboarding_docs(
            source_repo=source_repo,
            project_dir=project_dir,
            owner_user=current_user,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )
        docs_dir = source_repo / "docs" / "onboarding"
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            docs_dir.joinpath(name).write_text(f"# {name}\n\nCurrent copy for {name}.\n", encoding="utf-8")
        _run_git(["git", "add", "docs/onboarding"], cwd=source_repo)
        _run_git(
            [
                "git",
                "-c",
                "user.name=Switchyard Test",
                "-c",
                "user.email=switchyard-test@example.invalid",
                "commit",
                "-m",
                "Refresh onboarding docs",
            ],
            cwd=source_repo,
        )
        new_commit = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

        refreshed_name = "README.md"
        edited_name = "adversarial-collaborative-methodology.md"
        no_header_name = "switchyard-board-guide.md"
        missing_name = "switchyard-director-guide.md"
        edited_path = target_dir / edited_name
        no_header_path = target_dir / no_header_name
        missing_path = target_dir / missing_name
        edited_bytes = edited_path.read_bytes() + b"\nTenant note.\n"
        no_header_bytes = b"# Tenant-maintained board guide\n\nNo provenance.\n"
        edited_path.write_bytes(edited_bytes)
        no_header_path.write_bytes(no_header_bytes)
        missing_path.unlink()
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        output.clear()
        assert (
            team_launcher.upgrade_project_command(
                config,
                config_path=config_path,
                source_repo=source_repo,
                runner=_owner_file_install_runner,
                print_func=output.append,
            )
            == 0
        )

        refreshed_text = (target_dir / refreshed_name).read_text(encoding="utf-8")
        missing_text = missing_path.read_text(encoding="utf-8")
        assert old_commit not in refreshed_text
        assert f"source commit {new_commit}" in refreshed_text
        assert f"Current copy for {refreshed_name}." in refreshed_text
        assert edited_path.read_bytes() == edited_bytes
        assert no_header_path.read_bytes() == no_header_bytes
        assert f"source commit {new_commit}" in missing_text
        assert f"Current copy for {missing_name}." in missing_text
        assert any(f"onboarding doc {refreshed_name}: refreshed" in line for line in output)
        assert any(f"onboarding doc {edited_name}: skipped, edited since source commit {old_commit}" in line for line in output)
        assert any(f"onboarding doc {no_header_name}: skipped, no provenance header" in line for line in output)
        assert any(f"onboarding doc {missing_name}: installed" in line for line in output)
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            assert sum(f"onboarding doc {name}:" in line for line in output) == 1

def test_switchyard_upgrade_refuses_to_downgrade_onboarding_docs_from_stale_checkout() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-onboarding-stale.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_dir = tmp_path / "project"
        source_repo.mkdir()
        project_dir.mkdir()
        old_commit = _write_source_onboarding_docs(source_repo, initialize_git=True)
        docs_dir = source_repo / "docs" / "onboarding"
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            docs_dir.joinpath(name).write_text(f"# {name}\n\nNewer copy for {name}.\n", encoding="utf-8")
        _run_git(["git", "add", "docs/onboarding"], cwd=source_repo)
        _run_git(
            [
                "git",
                "-c",
                "user.name=Switchyard Test",
                "-c",
                "user.email=switchyard-test@example.invalid",
                "commit",
                "-m",
                "Update onboarding docs",
            ],
            cwd=source_repo,
        )
        new_commit = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()
        output: list[str] = []

        team_launcher._install_switchyard_onboarding_docs(
            source_repo=source_repo,
            project_dir=project_dir,
            owner_user=current_user,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )
        target_path = project_dir / "docs" / "onboarding" / "README.md"
        installed_text = target_path.read_text(encoding="utf-8")
        _run_git(["git", "checkout", "--detach", old_commit], cwd=source_repo)

        output.clear()
        team_launcher.upgrade_switchyard_onboarding_docs(
            source_repo=source_repo,
            project_dir=project_dir,
            owner_user=current_user,
            dry_run=False,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )

        assert target_path.read_text(encoding="utf-8") == installed_text
        assert f"source commit {new_commit}" in installed_text
        assert f"Newer copy for README.md." in installed_text
        assert old_commit not in target_path.read_text(encoding="utf-8")
        assert any("onboarding doc README.md: skipped, installed copy is newer than this checkout" in line for line in output)
        assert not any("onboarding doc README.md: refreshed" in line for line in output)
        for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
            assert sum(f"onboarding doc {name}:" in line for line in output) == 1

def test_switchyard_upgrade_still_skips_onboarding_doc_when_stamp_commit_is_missing() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-onboarding-missing-stamp.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_dir = tmp_path / "project"
        source_repo.mkdir()
        project_dir.mkdir()
        _write_source_onboarding_docs(source_repo, initialize_git=True)
        target_dir = project_dir / "docs" / "onboarding"
        target_dir.mkdir(parents=True)
        target_path = target_dir / "README.md"
        missing_commit = "f" * 40
        installed_text = team_launcher._stamp_onboarding_doc(
            source_name="README.md",
            source_commit=missing_commit,
            body="# README.md\n\nCopy from a missing source commit.\n",
        )
        target_path.write_text(installed_text, encoding="utf-8")
        output: list[str] = []

        team_launcher.upgrade_switchyard_onboarding_docs(
            source_repo=source_repo,
            project_dir=project_dir,
            owner_user=current_user,
            dry_run=False,
            runner=_owner_file_install_runner,
            print_func=output.append,
        )

        assert target_path.read_text(encoding="utf-8") == installed_text
        assert any(f"onboarding doc README.md: skipped, cannot verify source commit {missing_commit}" in line for line in output)
        assert not any("onboarding doc README.md: refreshed" in line for line in output)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_switchyard_new_prompts_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
