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
        # This case is about model validation; the provider's own first run and
        # its per-worktree trust are recorded as already done, each having
        # its own case (SYRD-191).
        _mark_first_run_setup_complete(owner_home, config)
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
    # One probe directory for the whole phase, and every model probe runs in it
    # rather than in the owner's home: the file the prompt names has to be in
    # the working directory the CLI is given (SYRD-111).
    workspace = set(runner.model_probe_cwds)
    assert len(workspace) == 1, runner.model_probe_cwds
    probe_dir = workspace.pop()
    assert probe_dir != str(owner_home), probe_dir
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        [
            "sudo", "-u", "otto-agent", "claude", "--model", "claude-opus-5",
            "--dangerously-skip-permissions", "-p", team_launcher.MODEL_VALIDATION_PROMPT,
        ],
        [
            "sudo",
            "-u",
            "otto-agent",
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--model",
            "gpt-5.5",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "-C",
            probe_dir,
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
        [
            "sudo", "-u", "otto-agent", "agy", "--model", "gemini-3.7-flash-high",
            "--dangerously-skip-permissions", "-p", team_launcher.MODEL_VALIDATION_PROMPT,
        ],
        [
            "sudo", "-u", "otto-agent", "hermes", "-m", "z-ai/glm-4.6", "--yolo", "-z",
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
        [
            "sudo", "-u", "otto-agent", "hermes", "-m", "deepseek/deepseek-chat", "--yolo", "-z",
            team_launcher.MODEL_VALIDATION_PROMPT,
        ],
    ]
    # Every model answered with a token it could only have read, and every
    # token was the one this run wrote.
    assert len(set(runner.model_probe_tokens)) == 1, runner.model_probe_tokens
    assert all(token for token in runner.model_probe_tokens), runner.model_probe_tokens
    # The auth probes still run in the owner's home; only the model probes move.
    assert str(owner_home) in {str(kwargs.get("cwd")) for kwargs in runner.call_kwargs}

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
        # Past its providers' first runs, which is the only tenant whose models
        # are probed: an unfinished first run is what the CLI shows a probe
        # instead of answering it (SYRD-221).
        _mark_first_run_setup_complete(owner_home, config)

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
    assert [
        "sudo", "-u", "otto-agent", "hermes", "-m", "openrouter/missing", "--yolo", "-z",
        team_launcher.MODEL_VALIDATION_PROMPT,
    ] in runner.calls
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 1 missing CLI(s)",
        # SYRD-211: host-wide once, not once per owner account.
        "switchyard: missing CLI agy (affected roles: inspector): install agy host-wide with "
        "curl -fsSL https://antigravity.google/cli/install.sh | bash, or let switchyard promote "
        "a copy you already have when it offers",
        "switchyard: panes run as owner user otto-agent, which does not inherit a CLI installed "
        "only for the user running switchyard. Install it host-wide once -- or, if you already "
        "have a private copy, let switchyard promote that executable to a root-owned host-wide "
        "copy when it offers, which every later project reuses.",
        "switchyard: login codex: roles ops; interactive account setup running codex login as otto-agent",
        # SYRD-221 UAT (test9): the sign-in step says what it is as it starts.
        "switchyard: codex will now run in this terminal as otto-agent to sign in. "
        "Complete what it asks -- a browser sign-in for some providers, a choice in the "
        "terminal for others; the terminal comes back on its own once the account is set up.",
    ]

def test_first_run_auth_phase_sequences_setup_then_logins_then_trust_for_every_role() -> None:
    """Setup FIRST, then a login only if the account still needs one.

    Logins used to run first, and live Zorin UAT showed what that costs: the
    operator signed into Claude, signed into Codex, and was then asked to sign
    into Claude a second time inside its own welcome flow, which does its own
    sign-in. Running the account-wide first run before the logins, and
    re-reading the account before each one, is what makes the second question
    unnecessary -- and the login is still there for an account the welcome flow
    does not settle (SYRD-211 live UAT).
    """
    """One login and one first run per provider, one trust action per worktree.

    This used to assert that a VISIBLE role's worktree trust was skipped, on
    the reasoning that its pane is somewhere the dialog can be answered. The
    User answered it five times instead, after two successful logins, and the
    Director's decision on SYRD-191 reversed it: the provider's required setup
    and every distinct worktree's trust are collected in the foreground, before
    any role is launched or presented.
    """
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
    # The fake CLI records nothing, so setup and trust are still outstanding
    # afterwards -- and that is said rather than left for the panes to show.
    assert report.incomplete_provider_setup == [("claude", ["designer", "director"])]
    assert sorted(report.untrusted_roles) == [
        ("agy", "inspector", str(tmp_path / "worktrees" / "inspector")),
        ("claude", "designer", str(tmp_path / "worktrees" / "designer")),
        ("claude", "director", str(tmp_path / "worktrees" / "director")),
    ]
    # The manifest probes, then Claude's own first run once for both of its
    # roles, then -- for each provider still unauthenticated -- one login,
    # each preceded by the re-read that would have skipped it, then one trust
    # action per distinct worktree: three, for two Claude roles and one agy
    # role, and none for Codex, which takes no directory trust.
    #
    # `claude auth login` survives here only because this fake records nothing:
    # its welcome flow does not mark the account authenticated, so the re-read
    # still says no. On a real host that flow signs in and the login is skipped,
    # which is the duplicate live UAT was asked to sit through.
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v claude"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v codex"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v agy"],
        ["sudo", "-u", "otto-agent", "claude"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v claude"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "login"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v codex"],
        ["sudo", "-u", "otto-agent", "codex", "login"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v agy"],
        ["sudo", "-u", "otto-agent", "agy"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "claude"],
        ["sudo", "-u", "otto-agent", "claude"],
        ["sudo", "-u", "otto-agent", "agy"],
    ]
    # Each trust action runs in the worktree it is about; everything before it
    # runs in the owner's home.
    assert [kwargs.get("cwd") for kwargs in runner.call_kwargs][-3:] == [
        str(tmp_path / "worktrees" / "designer"),
        str(tmp_path / "worktrees" / "director"),
        str(tmp_path / "worktrees" / "inspector"),
    ]
    # Nothing was written on the account's behalf: Switchyard asks the CLI to
    # run its own setup and looks again, and never manufactures the answer.
    assert not (owner_home / ".claude.json").exists()
    assert not (owner_home / ".gemini" / "antigravity-cli" / "settings.json").exists()
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "3 login step(s), 1 provider setup step(s), 3 folder trust step(s), "
        "0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login claude: roles designer, director; "
        "interactive account setup running claude auth login as otto-agent",
        "switchyard: login codex: roles ops, main; "
        "interactive account setup running codex login as otto-agent",
        "switchyard: login agy: roles inspector; interactive account setup running agy as otto-agent",
        "switchyard: provider setup claude: roles designer, director; this account has not "
        "completed Claude's own first run (theme, then sign-in); measured on this host, that "
        "flow asks to sign in again even when the account already holds valid credentials, and "
        "it is what every pane opens until it is done; interactive first run of claude as "
        "otto-agent, once for every role that uses it",
        "switchyard: folder trust claude: role designer at "
        f"{tmp_path / 'worktrees' / 'designer'}; recurs per project/workdir even when the owner "
        "user is reused; interactive repository trust today, not account login",
        "switchyard: folder trust claude: role director at "
        f"{tmp_path / 'worktrees' / 'director'}; recurs per project/workdir even when the owner "
        "user is reused; interactive repository trust today, not account login",
        "switchyard: folder trust agy: role inspector at "
        f"{tmp_path / 'worktrees' / 'inspector'}; recurs per project/workdir even when the owner "
        "user is reused; interactive repository trust today, not account login",
        # Each foreground step says what it is about to do with the terminal,
        # and how to hand it back, before it takes it (SYRD-191).
        "switchyard: claude will now run in this terminal as otto-agent. Answer its own "
        "prompts to the end -- a theme, the sign-in it asks for even though credentials "
        "exist, because that flow does not consult them, and whether to trust this folder. "
        "The terminal comes back on its own once nothing is left to answer; you do not have "
        "to exit anything. It is asked once for the account, not once per role, and no pane "
        "will ask again.",
        # And each sign-in says what it is as it starts, as the steps either side
        # of it do (SYRD-221 UAT, test9).
        *(
            f"switchyard: {cli} will now run in this terminal as otto-agent to sign in. "
            "Complete what it asks -- a browser sign-in for some providers, a choice in the "
            "terminal for others; the terminal comes back on its own once the account is set up."
            for cli in ("claude", "codex", "agy")
        ),
        f"switchyard: claude will now run in {tmp_path / 'worktrees' / 'designer'} as this "
        "project's owner so it can be trusted once for designer. Answer the trust "
        "prompt; the terminal comes back on its own once the answer is recorded.",
        f"switchyard: claude will now run in {tmp_path / 'worktrees' / 'director'} as this "
        "project's owner so it can be trusted once for director. Answer the trust "
        "prompt; the terminal comes back on its own once the answer is recorded.",
        f"switchyard: agy will now run in {tmp_path / 'worktrees' / 'inspector'} as this "
        "project's owner so it can be trusted once for inspector. Answer the trust "
        "prompt; the terminal comes back on its own once the answer is recorded.",
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
    # The middle pair is the account being re-read immediately before the login
    # runs. It is what lets a provider whose own first run already signed in be
    # skipped instead of asked again, and it costs one probe per login step
    # (SYRD-211 live UAT).
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v hermes"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "sh", "-c", "command -v hermes"],
        ["sudo", "-u", "otto-agent", "hermes", "model"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
    ]
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login hermes: roles bulk; interactive account setup running hermes model as otto-agent",
        "switchyard: hermes will now run in this terminal as otto-agent to sign in. "
        "Complete what it asks -- a browser sign-in for some providers, a choice in the "
        "terminal for others; the terminal comes back on its own once the account is set up.",
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
