#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *
from team_launcher_test_helpers import _record_codex_trust

def test_first_run_auth_phase_is_silent_when_auth_and_trust_already_pass() -> None:
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
                    ("audit", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                ],
            ),
        )
        owner_home.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    # Claude records its own first run beside the trust map, and
                    # a tenant that has "already passed" carries both: without
                    # it the manifest correctly offers the setup step this case
                    # asserts is absent (SYRD-191).
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": {
                        str(Path(role.workdir).resolve(strict=False)): {"hasTrustDialogAccepted": True}
                        for role in config.roles
                        if role.cli == ["claude"]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agy_settings = owner_home / ".gemini" / "antigravity-cli" / "settings.json"
        agy_settings.parent.mkdir(parents=True)
        agy_settings.write_text(
            json.dumps(
                {
                    "trustedWorkspaces": [
                        str(Path(role.workdir).resolve(strict=False))
                        for role in config.roles
                        if role.cli == ["agy"]
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
    ]
    assert {kwargs.get("cwd") for kwargs in runner.call_kwargs} == {str(owner_home)}

def test_first_run_auth_phase_reports_stale_codex_hook_trust_without_writing_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-codex-hooks.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        _write_codex_first_run_hooks(owner_home)
        config_path = owner_home / ".codex" / "config.toml"
        original_config = "[hooks.state]\n"
        config_path.write_text(original_config, encoding="utf-8")
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("main", "codex"),
                    ("audit", "claude"),
                    ("app", "codex"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        # This case is about Codex hook approvals, so the Claude and agy first
        # run and their per-worktree trust are recorded as already done: they
        # are separate steps with their own cases (SYRD-191).
        _mark_first_run_setup_complete(owner_home, config)
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        original_config = config_path.read_text(encoding="utf-8")
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )
        after_config = config_path.read_text(encoding="utf-8")

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == []
    assert [(item.event, item.affected_roles) for item in report.stale_codex_hook_trust] == [
        ("session_start", ("ops", "main", "app")),
        ("stop", ("ops", "main", "app")),
    ]
    for mismatch in report.stale_codex_hook_trust:
        assert "director" not in mismatch.affected_roles
        assert "inspector" not in mismatch.affected_roles
        assert "audit" not in mismatch.affected_roles
    assert after_config == original_config
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
    ]
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "0 login step(s), 0 provider setup step(s), 0 folder trust step(s), 2 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: manual security approval: "
        "codex hook trust needs 2 approvals for owner user otto-agent; "
        "run /hooks once in any Codex pane as that owner. "
        "This trust is shared by affected Codex roles: ops, main, app. "
        "Approvals: session_start (new project, never trusted); stop (new project, never trusted)"
    ]
    assert "director" not in messages[1]
    assert "inspector" not in messages[1]
    assert "audit" not in messages[1]
    output: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
    assert output == [
        "warning: switchyard: codex hook trust needs 2 approvals for owner user otto-agent; "
        "run /hooks once in any Codex pane as that owner. "
        "This trust is shared by affected Codex roles: ops, main, app. "
        "Approvals: session_start (new project, never trusted); stop (new project, never trusted)"
    ]
    assert "director" not in output[0]
    assert "inspector" not in output[0]
    assert "audit" not in output[0]

def test_first_run_auth_phase_distinguishes_changed_codex_hook_trust_from_never_trusted() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-codex-hooks-mixed.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        _write_codex_first_run_hooks(owner_home)
        entries = team_launcher._codex_command_hook_trust_entries(owner_home)
        assert len(entries) == 2
        first_event, first_key, _first_hash = entries[0]
        config_path = owner_home / ".codex" / "config.toml"
        original_config = "\n".join(
            [
                "[hooks.state]",
                "",
                f'[hooks.state."{first_key}"]',
                'trusted_hash = "sha256:old-hook-body"',
                "",
            ]
        )
        config_path.write_text(original_config, encoding="utf-8")
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex"), ("main", "codex")]),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("codex")
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        original_config = config_path.read_text(encoding="utf-8")
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )
        after_config = config_path.read_text(encoding="utf-8")

    assert len(report.stale_codex_hook_trust) == 2
    assert report.stale_codex_hook_trust[0].event == first_event
    assert report.stale_codex_hook_trust[0].trusted_hash == "sha256:old-hook-body"
    assert "session_start (hooks changed, re-approve)" in messages[1]
    assert "stop (new project, never trusted)" in messages[1]
    assert "ops, main" in messages[1]
    assert after_config == original_config

def test_first_run_auth_phase_accepts_matching_codex_hook_trust_without_writing_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-codex-hooks-clean.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        _write_codex_first_run_hooks(owner_home)
        _write_matching_codex_hook_trust(owner_home)
        original_config = (owner_home / ".codex" / "config.toml").read_text(encoding="utf-8")
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex"), ("main", "codex"), ("app", "codex")]),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("codex")
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        original_config = (owner_home / ".codex" / "config.toml").read_text(encoding="utf-8")
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )
        after_config = (owner_home / ".codex" / "config.toml").read_text(encoding="utf-8")

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert after_config == original_config

def test_first_run_auth_phase_accepts_fresh_installer_seeded_codex_hook_trust() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-codex-hooks-seeded.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        bin_path = owner_home / ".local" / "bin" / "ticket-board-pane-idle-hook"
        subprocess.run(
            [
                str(ROOT / "scripts" / "ticket-board-install-pane-hooks"),
                "install",
                "--home",
                str(owner_home),
                "--bin-path",
                str(bin_path),
                "--seed-codex-hook-trust-if-new",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("main", "codex"),
                    ("app", "codex"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        messages: list[str] = []

        # This case is about Codex hook approvals; the provider's own first run and
        # its per-worktree trust are recorded as already done, each having
        # its own case (SYRD-191).
        _mark_first_run_setup_complete(owner_home, config)

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.stale_codex_hook_trust == []
    assert messages == []

def test_first_run_auth_phase_ignores_codex_role_with_no_installed_hooks() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-codex-no-hooks.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("codex")
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        codex_before = (owner_home / ".codex" / "config.toml").read_text(encoding="utf-8")
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )
        config_unchanged = (owner_home / ".codex" / "config.toml").read_text(encoding="utf-8") == codex_before

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert config_unchanged  # the fixture now records Codex's trust there; the product still writes nothing (SYRD-279)

def test_first_run_auth_phase_does_not_sudo_wrap_same_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                ],
            ),
        )
        owner_home.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    # Claude records its own first run beside the trust map, and
                    # a tenant that has "already passed" carries both: without
                    # it the manifest correctly offers the setup step this case
                    # asserts is absent (SYRD-191).
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": {
                        str(Path(role.workdir).resolve(strict=False)): {"hasTrustDialogAccepted": True}
                        for role in config.roles
                        if role.cli == ["claude"]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agy_settings = owner_home / ".gemini" / "antigravity-cli" / "settings.json"
        agy_settings.parent.mkdir(parents=True)
        agy_settings.write_text(
            json.dumps(
                {
                    "trustedWorkspaces": [
                        str(Path(role.workdir).resolve(strict=False))
                        for role in config.roles
                        if role.cli == ["agy"]
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        messages: list[str] = []
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "otto-agent"
            # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
            _record_codex_trust(owner_home, config)
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == [
        ["claude", "auth", "status", "--json"],
        ["codex", "login", "status"],
        ["agy", "models"],
    ]
    assert {kwargs.get("cwd") for kwargs in runner.call_kwargs} == {str(owner_home)}

def test_first_run_auth_phase_reports_missing_cli_separately_from_login() -> None:
    for status_returncode in (127, 1):
        with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-missing-cli.") as tmp:
            tmp_path = Path(tmp)
            owner_home = tmp_path / "home" / "otto-agent"
            owner_home.mkdir(parents=True)
            config = load_project_config(
                "otto",
                _write_first_run_auth_config(tmp_path, roles=[("inspector", "agy"), ("ops", "codex")]),
            )
            runner = FirstRunAuthRunner(missing_clis={"agy"}, missing_cli_status_returncode=status_returncode)
            runner.login_seen.add("codex")
            messages: list[str] = []

            # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
            _record_codex_trust(owner_home, config)
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )

        assert report.unauthenticated_roles == {}, status_returncode
        assert report.missing_cli_roles == {"agy": ["inspector"]}, status_returncode
        assert report.owner_user == "otto-agent", status_returncode
        assert messages == [
            "switchyard: first-run setup manifest for owner user otto-agent: "
            "0 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 1 missing CLI(s)",
            # SYRD-211: the remedy is host-wide and once. It used to name the
            # owner account and a vendor command that installs for whoever runs
            # it, which is the duplicate per-owner installation SYRD-210 removed.
            "switchyard: missing CLI agy (affected roles: inspector): install agy host-wide with "
            "curl -fsSL https://antigravity.google/cli/install.sh | bash, or let switchyard "
            "promote a copy you already have when it offers",
            "switchyard: panes run as owner user otto-agent, which does not inherit a CLI installed "
            "only for the user running switchyard. Install it host-wide once -- or, if you already "
            "have a private copy, let switchyard promote that executable to a root-owned host-wide "
            "copy when it offers, which every later project reuses.",
        ], status_returncode
        assert runner.calls == [
            ["sudo", "-u", "otto-agent", "agy", "models"],
            ["sudo", "-u", "otto-agent", "sh", "-c", "command -v agy"],
            ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ], status_returncode
        assert not any(call == ["sudo", "-u", "otto-agent", "agy"] for call in runner.calls), status_returncode
        output: list[str] = []
        team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
        assert output == [
            "warning: switchyard: agy is not installed for owner user otto-agent "
            "(affected roles: inspector); install agy host-wide with "
            "curl -fsSL https://antigravity.google/cli/install.sh | bash, or let switchyard "
            "promote a copy you already have when it offers"
        ], status_returncode


def test_first_run_auth_invokes_owner_home_cli_with_same_path_as_presence_check() -> None:
    class HomeOnlyCliRunner:
        def __init__(self, owner_local_bin: Path) -> None:
            self.owner_local_bin = str(owner_local_bin)
            self.calls: list[list[str]] = []
            self.returncodes: list[int] = []
            self.login_seen = False

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            command = args[3:] if args[:2] == ["sudo", "-u"] else list(args)
            path = ""
            if command and command[0] == "env":
                env_end = 1
                while env_end < len(command):
                    if command[env_end] == "-u":
                        env_end += 2
                        continue
                    if "=" not in command[env_end]:
                        break
                    key, value = command[env_end].split("=", 1)
                    if key == "PATH":
                        path = value
                    env_end += 1
                command = command[env_end:]
            has_owner_cli_path = self.owner_local_bin in path.split(":")
            if command[:2] == ["sh", "-c"] and command[2] == "command -v codex":
                returncode = 0 if has_owner_cli_path else 1
                self.returncodes.append(returncode)
                return subprocess.CompletedProcess(args, returncode, stdout=f"{self.owner_local_bin}/codex\n", stderr="")
            if command == ["codex", "login", "status"]:
                if not has_owner_cli_path:
                    self.returncodes.append(127)
                    return subprocess.CompletedProcess(args, 127, stdout="", stderr="codex: command not found\n")
                if self.login_seen:
                    self.returncodes.append(0)
                    return subprocess.CompletedProcess(args, 0, stdout="Logged in using ChatGPT\n", stderr="")
                self.returncodes.append(1)
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="Not logged in\n")
            if command == ["codex", "login"]:
                if not has_owner_cli_path:
                    self.returncodes.append(127)
                    return subprocess.CompletedProcess(args, 127, stdout="", stderr="codex: command not found\n")
                self.login_seen = True
                self.returncodes.append(0)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            self.returncodes.append(0)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-home-cli.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_local_bin = owner_home / ".local" / "bin"
        owner_local_bin.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]),
        )
        runner = HomeOnlyCliRunner(owner_local_bin)
        messages: list[str] = []

        # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
        _record_codex_trust(owner_home, config)
        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    # This run is what made codex usable, so the report says which roles were
    # configured for it: a runtime already up when that happened started before
    # the credentials existed and has to be restarted, not presented (SYRD-191).
    assert report == team_launcher.FirstRunAuthReport({}, [], authenticated_now={"codex": ["ops"]})
    assert report.roles_awaiting_restart == ("ops",)
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login codex: roles ops; interactive account setup running codex login as otto-agent",
        # SYRD-221 UAT (test9): the sign-in step says what it is as it starts.
        "switchyard: codex will now run in this terminal as otto-agent to sign in. "
        "Complete what it asks -- a browser sign-in for some providers, a choice in the "
        "terminal for others; the terminal comes back on its own once the account is set up.",
    ]
    assert 127 not in runner.returncodes
    assert all(
        any(part.startswith("PATH=") and str(owner_local_bin) in part.split("=", 1)[1].split(":") for part in call)
        for call in runner.calls
        if call[:3] == ["sudo", "-u", "otto-agent"]
    )


def test_first_run_auth_reports_profile_only_cli_missing_with_real_shell_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-profile-only.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        profile_only_bin = owner_home / ".nvm" / "bin"
        sandbox_bin = tmp_path / "sandbox-bin"
        profile_only_bin.mkdir(parents=True)
        sandbox_bin.mkdir()
        (profile_only_bin / "codex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (profile_only_bin / "codex").chmod(0o755)
        (owner_home / ".profile").write_text(
            f"PATH={profile_only_bin}:$PATH\nexport PATH\n",
            encoding="utf-8",
        )
        (sandbox_bin / "sh").symlink_to("/bin/sh")
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]),
        )
        original_base_path = team_launcher.DEFAULT_PANE_BASE_PATH
        calls: list[list[str]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            command = args[3:] if args[:2] == ["sudo", "-u"] else list(args)
            calls.append(command)
            return subprocess.run(
                command,
                cwd=str(kwargs.get("cwd") or owner_home),
                env={},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

        messages: list[str] = []
        try:
            team_launcher.DEFAULT_PANE_BASE_PATH = str(sandbox_bin)
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.DEFAULT_PANE_BASE_PATH = original_base_path

    assert report.unauthenticated_roles == {}
    assert report.missing_cli_roles == {"codex": ["ops"]}
    assert calls == [
        [
            "env",
            *team_launcher._env_unset_prefix(team_launcher.DESKTOP_ENV_KEYS),
            f"HOME={owner_home}",
            f"PATH={owner_home / 'bin'}:{owner_home / '.local' / 'bin'}:{sandbox_bin}",
            # TERM and friends are forwarded so the CLI can still draw itself;
            # taken from the product rather than spelled out, because which of
            # them exist depends on the terminal this runs in (SYRD-191).
            *team_launcher._terminal_presentation_env(),
            "codex",
            "login",
            "status",
        ],
        [
            "env",
            *team_launcher._env_unset_prefix(team_launcher.DESKTOP_ENV_KEYS),
            f"HOME={owner_home}",
            f"PATH={owner_home / 'bin'}:{owner_home / '.local' / 'bin'}:{sandbox_bin}",
            # TERM and friends are forwarded so the CLI can still draw itself;
            # taken from the product rather than spelled out, because which of
            # them exist depends on the terminal this runs in (SYRD-191).
            *team_launcher._terminal_presentation_env(),
            "sh",
            "-c",
            "command -v codex",
        ],
    ]
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "0 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 1 missing CLI(s)",
        "switchyard: missing CLI codex (affected roles: ops): install codex host-wide with "
        "curl -fsSL https://chatgpt.com/codex/install.sh | sh, or let switchyard promote a copy "
        "you already have when it offers",
        "switchyard: panes run as owner user otto-agent, which does not inherit a CLI installed "
        "only for the user running switchyard. Install it host-wide once -- or, if you already "
        "have a private copy, let switchyard promote that executable to a root-owned host-wide "
        "copy when it offers, which every later project reuses.",
    ]


def test_first_run_auth_phase_reports_broken_existing_owner_shell_without_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-broken-shell.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        broken_shell = tmp_path / "missing-fish"
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("codex")
        messages: list[str] = []
        original_getpwnam = team_launcher.pwd.getpwnam

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_shell = str(broken_shell)

        try:
            team_launcher.pwd.getpwnam = lambda user_name: FakeUserInfo() if user_name == "otto-agent" else original_getpwnam(user_name)
            # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
            _record_codex_trust(owner_home, config)
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

    assert report.owner_shell_issue == team_launcher.OwnerShellIssue(
        owner_user="otto-agent",
        shell=str(broken_shell),
        remedy="sudo usermod -s /bin/bash otto-agent",
    )
    assert report.owner_user == "otto-agent"
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "0 login step(s), 0 provider setup step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s), "
        "1 owner shell issue(s)",
        f"switchyard: owner shell for otto-agent is not executable: {broken_shell}; "
        "repair with `sudo usermod -s /bin/bash otto-agent`",
    ]
    assert not any("usermod" in call for command in runner.calls for call in command)
    output: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
    assert output == [
        f"warning: switchyard: owner shell for otto-agent is not executable: {broken_shell}; "
        "repair with `sudo usermod -s /bin/bash otto-agent`"
    ]


def test_first_run_auth_phase_silent_for_executable_owner_shell() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-good-shell.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex")]),
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("codex")
        messages: list[str] = []
        original_getpwnam = team_launcher.pwd.getpwnam

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_shell = "/bin/sh"

        try:
            team_launcher.pwd.getpwnam = lambda user_name: FakeUserInfo() if user_name == "otto-agent" else original_getpwnam(user_name)
            # Codex asks folder trust too, and a tenant past first run has answered it (SYRD-279).
            _record_codex_trust(owner_home, config)
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_first_run_auth_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
