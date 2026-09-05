#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_switchyard_new_prompts_roles_and_skips_designer_when_absent() -> None:
    class PromptedRoleRunner(FakeRunner):
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

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-roles.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        prompts: list[str] = []
        answers = iter(["n", "n", "claude", "code-review, runtime", "agy", "codex"])
        runner = PromptedRoleRunner()
        process_launcher = RecordingProcessLauncher()
        stdout = StringIO()

        def input_func(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    desktop_policy=Path("headless"),
                    slug="mycoolthing",
                    agent_name="otto-agent",
                    project_name="My Cool Thing",
                    project_path=home_base / "otto-agent" / "Projects" / "mycoolthing",
                    source_repo=source_repo,
                    output_dir=output_dir,
                    yes=True,
                    allow_existing_owner_user=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    input_func=input_func,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

        project_dir = home_base / "otto-agent" / "Projects" / "mycoolthing"
        artifact = json.loads((project_dir / ".switchyard" / "mycoolthing.project.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((output_dir / "mycoolthing.json").read_text(encoding="utf-8"))
        workflow_sql = (output_dir / "mycoolthing-workflow.sql").read_text(encoding="utf-8")
        rendered = stdout.getvalue()

    assert prompts == [
        "Include designer role [Y/n]: ",
        "Include audit role [Y/n]: ",
        "director CLI (claude/codex/agy/hermes) [claude]: ",
        "Implementer roles (comma-separated) [main, ops]: ",
        "code-review CLI (claude/codex/agy/hermes) [codex]: ",
        "runtime CLI (claude/codex/agy/hermes) [codex]: ",
    ]
    assert artifact["project"]["include_designer"] is False
    assert artifact["project"]["include_audit"] is False
    assert artifact["project"]["audit_roles"] == []
    assert artifact["project"]["roles"] == ["code-review", "runtime"]
    assert artifact["project"]["role_clis"] == {
        "director": "claude",
        "code-review": "agy",
        "runtime": "codex",
    }
    assert plan["draft_roles"] == []
    assert plan["implementer_roles"] == ["code-review", "runtime"]
    assert plan["audit_roles"] == []
    assert plan["assignee_roles"] == ["unassigned", "code-review", "runtime", "director", "user"]
    assert plan["caller_roles"] == ["director", "code-review", "runtime", "user"]
    assert [role["role"] for role in config["roles"]] == ["director", "code-review", "runtime"]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "director": ["claude"],
        "code-review": ["agy"],
        "runtime": ["codex"],
    }
    assert [role["target"] for role in config["roles"]] == [
        "mycoolthing-director:0.0",
        "mycoolthing-code-review:0.0",
        "mycoolthing-runtime:0.0",
    ]
    assert "('draft', 'Draft', 0, ARRAY[]::text[]" in workflow_sql
    assert "('draft', 'analysis', 'release_draft', ARRAY['director', 'user']::text[]" in workflow_sql
    assert "('audit', 'Audit'" not in workflow_sql
    assert "ARRAY['audit']::text[]" not in workflow_sql
    assert "('in_progress', 'director_review', 'submit_to_audit', ARRAY['code-review', 'runtime']::text[]" in workflow_sql
    assert not (project_dir / "PROJECT_DESIGN.md").exists()
    assert not (project_dir / ".switchyard" / "DESIGNER_ONBOARDING.md").exists()
    assert "switchyard: design phase skipped; no designer pane configured" in rendered
    assert "maximize the designer pane" not in rendered

def test_switchyard_new_accepts_role_defaults_and_includes_ops() -> None:
    class PromptedRoleRunner(FakeRunner):
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

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-default-roles.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        prompts: list[str] = []
        output: list[str] = []
        answers = iter(["", "", "", "", "", "", "", ""])
        runner = PromptedRoleRunner()
        process_launcher = RecordingProcessLauncher()

        def input_func(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        assert (
            switchyard_new_command(
                desktop_policy=Path("headless"),
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter System",
                project_path=home_base / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo,
                output_dir=output_dir,
                yes=True,
                allow_existing_owner_user=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                input_func=input_func,
                print_func=output.append,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                session_record_timeout=0,
                registry_dir=tmp_path / "registry",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        project_dir = home_base / "otto-agent" / "Projects" / "porter"
        artifact = json.loads((project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((output_dir / "porter.json").read_text(encoding="utf-8"))

    assert prompts == [
        "Include designer role [Y/n]: ",
        "Include audit role [Y/n]: ",
        "designer CLI (claude/codex/agy/hermes) [claude]: ",
        "director CLI (claude/codex/agy/hermes) [claude]: ",
        "audit CLI (claude/codex/agy/hermes) [claude]: ",
        "Implementer roles (comma-separated) [main, ops]: ",
        "main CLI (claude/codex/agy/hermes) [codex]: ",
        "ops CLI (claude/codex/agy/hermes) [codex]: ",
    ]
    assert "switchyard: project name: Porter System" in output
    assert "switchyard: slug: porter" in output
    assert "switchyard: owner user: otto-agent" in output
    assert f"switchyard: project path: {project_dir}" in output
    assert "Conventional implementer roles:" in output
    assert "  main: core/domain implementation and integration" in output
    assert "  ops: environment, services, tooling, and infrastructure" in output
    assert "  app: application/UI work" in output
    assert "  research: investigation, design support, and unknowns" in output
    assert "  perf: measurement and performance work" in output
    assert artifact["project"]["roles"] == ["main", "ops"]
    assert artifact["project"]["role_clis"] == {
        "designer": "claude",
        "director": "claude",
        "audit": "claude",
        "main": "codex",
        "ops": "codex",
    }
    assert plan["implementer_roles"] == ["main", "ops"]
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "main", "ops"]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "designer": ["claude"],
        "director": ["claude"],
        "audit": ["claude"],
        "main": ["codex"],
        "ops": ["codex"],
    }

def test_switchyard_new_stdin_eof_exits_without_traceback() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "switchyard"), "new"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert proc.stdout == "Project name: "
    assert proc.stderr.strip() == "switchyard: no input available"
    assert "Traceback" not in combined

def test_switchyard_new_confirmation_eof_exits_without_traceback() -> None:
    try:
        team_launcher._confirm_switchyard_new(
            slug="porter",
            owner_user="porter-agent",
            project_name="Porter System",
            project_dir=Path("/home/porter-agent/Projects/porter"),
            yes=False,
            input_func=lambda _prompt: (_ for _ in ()).throw(EOFError()),
            print_func=lambda _line: None,
        )
        raise AssertionError("expected confirmation EOF failure")
    except SystemExit as exc:
        message = str(exc)

    assert message == "switchyard: no input available"

def test_prompt_bool_caps_invalid_retries() -> None:
    stdout = StringIO()
    calls = 0

    def input_func(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 20:
            raise AssertionError("prompt retry cap did not terminate")
        return "maybe"

    with redirect_stdout(stdout):
        try:
            team_launcher._prompt_bool("Include designer role", default=True, input_func=input_func)
            raise AssertionError("expected prompt retry cap")
        except SystemExit as exc:
            message = str(exc)

    assert message == "switchyard: too many invalid answers for Include designer role"
    assert stdout.getvalue().count("answer yes or no") == team_launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS

def test_prompt_cli_caps_invalid_retries() -> None:
    stdout = StringIO()
    calls = 0

    def input_func(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 20:
            raise AssertionError("prompt retry cap did not terminate")
        return "vim"

    with redirect_stdout(stdout):
        try:
            team_launcher._prompt_cli("director", default="claude", input_func=input_func)
            raise AssertionError("expected prompt retry cap")
        except SystemExit as exc:
            message = str(exc)

    assert message == "switchyard: too many invalid answers for director CLI"
    assert stdout.getvalue().count("CLI for director must be one of claude, codex, agy, hermes") == (
        team_launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS
    )

def test_switchyard_role_choices_caps_comma_only_implementer_retries() -> None:
    stdout = StringIO()
    calls = 0

    def input_func(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls > 20:
            raise AssertionError("prompt retry cap did not terminate")
        return "" if calls <= 5 else ","

    with redirect_stdout(stdout):
        try:
            team_launcher._prompt_switchyard_role_choices(input_func=input_func)
            raise AssertionError("expected prompt retry cap")
        except SystemExit as exc:
            message = str(exc)

    assert message == "switchyard: too many invalid answers for implementer roles"
    assert stdout.getvalue().count("at least one implementer role is required") == (
        team_launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS
    )

def test_switchyard_new_rejects_role_choices_without_required_director() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-required-roles.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        try:
            switchyard_new_command(
                desktop_policy=Path("headless"),
                slug="atlas",
                agent_name="atlas",
                project_name="Atlas Project",
                project_path=home_base / "atlas-agent" / "Projects" / "atlas-project",
                output_dir=output_dir,
                role_clis=(("audit", "claude"), ("app", "codex")),
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=FakeRunner(),
            )
            raise AssertionError("expected missing required role failure")
        except SystemExit as exc:
            message = str(exc)

    assert "required roles missing: director" in message
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent" / "Projects" / "atlas-project").exists()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_project_role_prompts_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
