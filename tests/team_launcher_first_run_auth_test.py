#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

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
        messages: list[str] = []

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
        "switchyard: first-run codex hook trust needs 2 approvals for owner user otto-agent; "
        "run /hooks once in any Codex pane as that owner. "
        "This trust is shared by affected Codex roles: ops, main, app. "
        "Approvals: session_start (new project, never trusted); stop (new project, never trusted)"
    ]
    assert "director" not in messages[0]
    assert "inspector" not in messages[0]
    assert "audit" not in messages[0]
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
    assert "session_start (hooks changed, re-approve)" in messages[0]
    assert "stop (new project, never trusted)" in messages[0]
    assert "ops, main" in messages[0]
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

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )
        config_written = (owner_home / ".codex" / "config.toml").exists()

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert not config_written

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
            "switchyard: first-run agy not installed for owner user otto-agent; "
            "install agy for owner user otto-agent; affected roles: inspector"
        ], status_returncode
        assert runner.calls == [
            ["sudo", "-u", "otto-agent", "agy", "models"],
            ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v agy"],
            ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ], status_returncode
        assert not any(call == ["sudo", "-u", "otto-agent", "agy"] for call in runner.calls), status_returncode
        output: list[str] = []
        team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
        assert output == [
            "warning: switchyard: agy is not installed for owner user otto-agent; "
            "install agy for owner user otto-agent; affected roles: inspector"
        ], status_returncode

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_first_run_auth_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
