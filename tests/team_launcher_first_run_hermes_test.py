#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_hermes_config_check_requires_resolved_api_key() -> None:
    unauthenticated = subprocess.CompletedProcess(
        ["hermes", "config", "check"],
        0,
        stdout="📋 Configuration Status\n\n  Optional:\n    ○ OPENROUTER_API_KEY → vision_analyze, mixture_of_agents\n",
        stderr="",
    )
    pooled_false_positive = subprocess.CompletedProcess(
        ["hermes", "auth", "list"],
        0,
        stdout="openrouter (1 credentials):\n  #1  pgu634-probe         api_key manual <-\n",
        stderr="",
    )
    env_resolved = subprocess.CompletedProcess(
        ["hermes", "config", "check"],
        0,
        stdout="📋 Configuration Status\n\n  Optional:\n    ✓ OPENROUTER_API_KEY\n",
        stderr="",
    )

    assert not team_launcher._cli_auth_probe_passed("hermes", unauthenticated)
    assert not team_launcher._cli_auth_probe_passed("hermes", pooled_false_positive)
    assert team_launcher._cli_auth_probe_passed("hermes", env_resolved)

def test_first_run_auth_phase_reports_hermes_without_resolved_api_key_as_unauthenticated() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth-hermes-empty.") as tmp:
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
        runner = FirstRunAuthRunner(authenticated_after_login=False)
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {"hermes": ["bulk"]}
    assert report.missing_cli_roles == {}
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v hermes"],
        ["sudo", "-u", "otto-agent", "hermes", "model"],
        ["sudo", "-u", "otto-agent", "hermes", "config", "check"],
        ["sudo", "-u", "otto-agent", "sh", "-lc", "command -v hermes"],
    ]
    assert messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "1 login step(s), 0 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login hermes: roles bulk; interactive account setup running hermes model as otto-agent",
    ]

def test_first_run_trust_handles_detached_roles_and_persists_for_later_launches() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-trust-repeat.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config_path = _write_first_run_auth_config(tmp_path, roles=[("research", "claude")])
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][0]["detached"] = True
        raw["roles"][0].pop("slot", None)
        config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)

        calls: list[list[str]] = []
        call_kwargs: list[dict[str, object]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            call_kwargs.append(dict(kwargs))
            command = args[3:] if args[:2] == ["sudo", "-u"] else args
            if command == ["claude", "auth", "status", "--json"]:
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"loggedIn": True}) + "\n")
            if command == ["claude"]:
                cwd = str(kwargs["cwd"])
                claude_config = owner_home / ".claude.json"
                try:
                    payload = json.loads(claude_config.read_text(encoding="utf-8"))
                except OSError:
                    payload = {}
                projects = payload.setdefault("projects", {})
                projects[cwd] = {"hasTrustDialogAccepted": True}
                claude_config.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 1, stderr="unexpected command\n")

        first_messages: list[str] = []
        first_report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=first_messages.append,
        )
        calls_after_first = list(calls)
        second_messages: list[str] = []
        second_report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=second_messages.append,
        )

    assert first_report == team_launcher.FirstRunAuthReport({}, [])
    assert second_report == team_launcher.FirstRunAuthReport({}, [])
    assert calls_after_first == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "claude"],
    ]
    assert calls == [
        *calls_after_first,
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
    ]
    assert call_kwargs[1].get("cwd") == str(tmp_path / "worktrees" / "research")
    assert first_messages == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "0 login step(s), 1 folder trust step(s), 0 codex hook approval(s), 0 missing CLI(s)",
        f"switchyard: folder trust claude: role research at {tmp_path / 'worktrees' / 'research'}; "
        "recurs per project/workdir even when the owner user is reused; "
        "interactive repository trust today, not account login",
    ]
    assert second_messages == []

def test_first_run_setup_manifest_prints_every_step_before_first_interactive_command() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-manifest-order.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        _write_codex_first_run_hooks(owner_home)
        config_path = _write_first_run_auth_config(
            tmp_path,
            roles=[
                ("director", "claude"),
                ("research", "claude"),
                ("ops", "codex"),
                ("inspector", "agy"),
            ],
        )
        _mark_first_run_roles_detached(config_path, "research")
        config = load_project_config("otto", config_path)
        events: list[tuple[str, list[str] | str]] = []

        class EventRunner(FirstRunAuthRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                events.append(("run", args))
                return super().__call__(args, **kwargs)

        runner = EventRunner()

        def print_func(message: str) -> None:
            events.append(("print", message))

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=print_func,
        )

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == [("claude", "research", str(tmp_path / "worktrees" / "research"))]
    printed = [value for kind, value in events if kind == "print"]
    assert printed == [
        "switchyard: first-run setup manifest for owner user otto-agent: "
        "3 login step(s), 1 folder trust step(s), 2 codex hook approval(s), 0 missing CLI(s)",
        "switchyard: login claude: roles director, research; "
        "interactive account setup running claude auth login as otto-agent",
        "switchyard: login codex: roles ops; interactive account setup running codex login as otto-agent",
        "switchyard: login agy: roles inspector; interactive account setup running agy as otto-agent",
        f"switchyard: folder trust claude: role research at {tmp_path / 'worktrees' / 'research'}; "
        "recurs per project/workdir even when the owner user is reused; "
        "interactive repository trust today, not account login",
        "switchyard: manual security approval: "
        "codex hook trust needs 2 approvals for owner user otto-agent; run /hooks once in any Codex pane as that owner. "
        "This trust is shared by affected Codex roles: ops. "
        "Approvals: session_start (new project, never trusted); stop (new project, never trusted)",
    ]
    first_interactive_index = next(
        index
        for index, (_kind, value) in enumerate(events)
        if value == ["sudo", "-u", "otto-agent", "claude", "auth", "login"]
    )
    last_manifest_print_index = max(index for index, (kind, _value) in enumerate(events) if kind == "print")
    assert last_manifest_print_index < first_interactive_index

def test_first_run_trust_matches_configured_symlink_path_without_reprompting() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-trust-path.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        target = tmp_path / "real-worktree"
        target.mkdir()
        link = tmp_path / "linked-worktree"
        link.symlink_to(target, target_is_directory=True)
        config_path = _write_first_run_auth_config(tmp_path, roles=[("director", "claude")])
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][0]["detached"] = True
        raw["roles"][0].pop("slot", None)
        raw["roles"][0]["workdir"] = str(link)
        config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)
        owner_home.joinpath(".claude.json").write_text(
            json.dumps({"projects": {str(link): {"hasTrustDialogAccepted": True}}}) + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("claude")
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
    assert runner.calls == [["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"]]

def test_first_run_auth_phase_declined_login_reports_affected_roles_without_aborting() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex"), ("main", "codex")]),
        )
        messages: list[str] = []
        runner = FirstRunAuthRunner(authenticated_after_login=False)

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {"codex": ["ops", "main"]}
    assert report.missing_cli_roles == {}
    assert report.untrusted_roles == []
    output: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
    assert output == ["warning: switchyard: codex is still unauthenticated; affected roles: ops, main"]

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_first_run_hermes_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
