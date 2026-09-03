#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_first_run_auth_phase_validates_configured_models_for_all_clis() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-models.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        role_models = {
            "director": "claude-opus-5",
            "ops": "gpt-5.5",
            "inspector": "gemini-3.7-flash-high",
            "bulk": "z-ai/glm-4.6",
            "research": "deepseek/deepseek-chat",
        }
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("bulk", "hermes"),
                    ("research", "hermes"),
                ],
                role_models=role_models,
            ),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex", "hermes"})
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            validate_models=True,
            runner=runner,
            print_func=messages.append,
        )

    assert report.model_validation_failures == []
    assert messages == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "claude", "--model", "claude-opus-5", "-p", team_launcher.MODEL_VALIDATION_PROMPT],
        [
            "sudo",
            "-u",
            "otto-agent",
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            "gpt-5.5",
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
        ["sudo", "-u", "otto-agent", "agy", "--model", "gemini-3.7-flash-high", "-p", team_launcher.MODEL_VALIDATION_PROMPT],
        ["sudo", "-u", "otto-agent", "hermes", "-m", "z-ai/glm-4.6", "-z", team_launcher.MODEL_VALIDATION_PROMPT],
        ["sudo", "-u", "otto-agent", "hermes", "-m", "deepseek/deepseek-chat", "-z", team_launcher.MODEL_VALIDATION_PROMPT],
    ]
    assert {kwargs.get("cwd") for kwargs in runner.call_kwargs} == {str(owner_home)}

def test_model_validation_ignores_stderr_echoed_prompt_sentinel() -> None:
    proc = subprocess.CompletedProcess(
        ["codex", "exec", "--skip-git-repo-check", "--model", "openai/not-a-model"],
        0,
        stdout="",
        stderr=f"model not found\n{team_launcher.MODEL_VALIDATION_PROMPT}\n",
    )

    assert team_launcher._model_validation_passed(proc) is False
    assert (
        team_launcher._model_validation_passed(
            subprocess.CompletedProcess(["codex"], 0, stdout="model-ok\n", stderr="")
        )
        is True
    )

def test_first_run_auth_phase_reports_bad_models_with_agy_suggestions_and_no_catalog_for_other_clis() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-models-bad.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        role_models = {
            "director": "anthropic/claude-3.5-sonnet",
            "ops": "openai/not-a-model",
            "inspector": "gemini-retired",
            "bulk": "openrouter/missing",
        }
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("bulk", "hermes"),
                ],
                role_models=role_models,
            ),
        )
        runner = FirstRunAuthRunner(
            invalid_models={
                ("claude", "anthropic/claude-3.5-sonnet"): "HTTP 404: No endpoints found for anthropic/claude-3.5-sonnet\n",
                ("codex", "openai/not-a-model"): "model not found: openai/not-a-model\n",
                ("agy", "gemini-retired"): "model gemini-retired is unavailable\n",
            },
            empty_success_models={("hermes", "openrouter/missing")},
        )
        runner.login_seen.update({"agy", "claude", "codex", "hermes"})

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            validate_models=True,
            runner=runner,
        )

    failures = {(failure.role, failure.cli, failure.model): failure for failure in report.model_validation_failures}
    assert set(failures) == {
        ("director", "claude", "anthropic/claude-3.5-sonnet"),
        ("ops", "codex", "openai/not-a-model"),
        ("inspector", "agy", "gemini-retired"),
        ("bulk", "hermes", "openrouter/missing"),
    }
    assert "No endpoints found" in failures[("director", "claude", "anthropic/claude-3.5-sonnet")].reason
    assert "no model list is available for claude" in failures[("director", "claude", "anthropic/claude-3.5-sonnet")].suggestion
    assert "no model list is available for codex" in failures[("ops", "codex", "openai/not-a-model")].suggestion
    assert "valid agy models include: gemini-3.7-flash-high, gemini-3.7-pro" in failures[("inspector", "agy", "gemini-retired")].suggestion
    assert failures[("bulk", "hermes", "openrouter/missing")].reason == "model probe did not confirm model-ok"
    assert "no model list is available for hermes" in failures[("bulk", "hermes", "openrouter/missing")].suggestion
    warning_lines: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=warning_lines.append)
    assert any(
        "role director (cli claude, model anthropic/claude-3.5-sonnet)" in line
        and "no model list is available for claude" in line
        for line in warning_lines
    )
    assert any(
        "role inspector (cli agy, model gemini-retired)" in line
        and "valid agy models include: gemini-3.7-flash-high, gemini-3.7-pro" in line
        for line in warning_lines
    )
    validation_models = [
        runner._model_from_command(call[4:] if call[:2] == ["sudo", "-u"] else call, "-m" if "hermes" in call else "--model")
        for call in runner.calls
        if any(token in call for token in ("-p", "-z", "exec"))
    ]
    assert set(validation_models) == set(role_models.values())

def test_first_run_auth_phase_does_not_validate_models_unless_requested() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-models-disabled.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[("ops", "codex")],
                role_models={"ops": "openai/not-a-model"},
            ),
        )
        runner = FirstRunAuthRunner(
            invalid_models={("codex", "openai/not-a-model"): "model not found: openai/not-a-model\n"}
        )
        runner.login_seen.add("codex")

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
        )

    assert report.model_validation_failures == []
    assert runner.calls == [["sudo", "-u", "otto-agent", "codex", "login", "status"]]

def test_first_run_auth_phase_skips_model_validation_for_unauthenticated_or_missing_clis() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-models-skip-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[("ops", "codex"), ("inspector", "agy"), ("bulk", "hermes")],
                role_models={
                    "ops": "openai/not-a-model",
                    "inspector": "gemini-retired",
                    "bulk": "openrouter/missing",
                },
            ),
        )
        runner = FirstRunAuthRunner(
            missing_clis={"agy"},
            missing_cli_status_returncode=127,
            invalid_models={("codex", "openai/not-a-model"): "model not found: openai/not-a-model\n"},
            empty_success_models={("hermes", "openrouter/missing")},
            unauthenticated_clis={"codex"},
        )
        runner.login_seen.add("hermes")
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            validate_models=True,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {"codex": ["ops"]}
    assert report.missing_cli_roles == {"agy": ["inspector"]}
    assert [(failure.role, failure.cli, failure.model) for failure in report.model_validation_failures] == [
        ("bulk", "hermes", "openrouter/missing")
    ]
    assert not any(call[:5] == ["sudo", "-u", "otto-agent", "codex", "exec"] for call in runner.calls)
    assert not any(call[:4] == ["sudo", "-u", "otto-agent", "agy"] and "-p" in call for call in runner.calls)
    assert ["sudo", "-u", "otto-agent", "hermes", "-m", "openrouter/missing", "-z", team_launcher.MODEL_VALIDATION_PROMPT] in runner.calls
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 1 missing CLI(s)",
        "switchyard: missing CLI agy (affected roles: inspector): install agy for owner user "
        "otto-agent with: curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "switchyard: install each one for owner user otto-agent; panes run as that user, so a CLI "
        "installed only for the user running switchyard is not found.",
        "switchyard: login codex: roles ops; interactive account setup running codex login as otto-agent",
    ]

def test_first_run_auth_phase_sequences_distinct_logins_and_skips_visible_worktree_trust() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("designer", "claude"),
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("main", "codex"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v claude"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v codex"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v agy"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "login"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
    ]
    assert [kwargs.get("cwd") for kwargs in runner.call_kwargs] == [str(owner_home)] * 12
    assert not (owner_home / ".claude.json").exists()
    assert not (owner_home / ".gemini" / "antigravity-cli" / "settings.json").exists()
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "3 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login claude: roles designer, director; "
        "interactive account setup running claude auth login as otto-agent",
        "switchyard: login codex: roles ops, main; "
        "interactive account setup running codex login as otto-agent",
        "switchyard: login agy: roles inspector; interactive account setup running agy as otto-agent",
    ]

def test_first_run_auth_phase_handles_hermes_model_setup() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-hermes.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("bulk", "hermes"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v hermes"],
        ["sudo", "-u", "otto-agent", "hermes", "model"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
    ]
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login hermes: roles bulk; interactive account setup running hermes model as otto-agent",
    ]

def test_first_run_auth_phase_accepts_hermes_resolved_api_key_without_model_setup() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-hermes-env.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("bulk", "hermes"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("hermes")
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
    ]
    assert messages == []

def test_first_run_trust_skips_detached_non_folder_trust_clis() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-trust-non-folder-clis.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config_path = _write_first_run_auth_config(
            tmp_path,
            roles=[
                ("ops", "codex"),
                ("bulk", "hermes"),
            ],
        )
        _mark_first_run_roles_detached(config_path, "ops", "bulk")
        config = load_project_config("otto", config_path)
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"codex", "hermes"})
        messages: list[str] = []
        trust_probes: list[str] = []

        original_workdir_is_trusted = team_launcher._workdir_is_trusted

        def fake_workdir_is_trusted(cli: str, *, owner_home: Path, workdir: Path) -> bool:
            trust_probes.append(cli)
            return False

        try:
            team_launcher._workdir_is_trusted = fake_workdir_is_trusted
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher._workdir_is_trusted = original_workdir_is_trusted

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert trust_probes == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
    ]

def test_first_run_auth_phase_skips_cli_absent_from_status_commands() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-unknown-status.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("bulk", "hermes"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        messages: list[str] = []

        original_status_commands = team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS
        original_cli_auth_status = team_launcher._cli_auth_status

        def fake_cli_auth_status(
            cli: str,
            *,
            owner_user: str,
            owner_home: Path,
            runner: Callable[..., subprocess.CompletedProcess[Any]],
        ) -> str:
            if cli == "hermes":
                raise AssertionError("hermes should be skipped while absent from FIRST_RUN_AUTH_STATUS_COMMANDS")
            return original_cli_auth_status(cli, owner_user=owner_user, owner_home=owner_home, runner=runner)

        try:
            team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS = {
                key: value for key, value in original_status_commands.items() if key != "hermes"
            }
            team_launcher._cli_auth_status = fake_cli_auth_status
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.FIRST_RUN_AUTH_STATUS_COMMANDS = original_status_commands
            team_launcher._cli_auth_status = original_cli_auth_status

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == []

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_first_run_models_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
