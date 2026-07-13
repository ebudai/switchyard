#!/usr/bin/env python3
"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.team_launcher import (
    cli_command_for_role,
    launch_project,
    load_project_config,
    run_role_pane,
    session_file_name,
    write_template,
)


class FakeRunner:
    def __init__(
        self,
        existing_sessions: set[str] | None = None,
        *,
        current_commands: dict[str, str] | None = None,
    ) -> None:
        self.existing_sessions = set(existing_sessions or set())
        self.current_commands = current_commands or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            session = args[-1]
            return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
        if args[:3] == ["tmux", "display-message", "-p"]:
            target = args[args.index("-t") + 1]
            command = self.current_commands.get(target, "")
            return subprocess.CompletedProcess(args, 0 if command else 1, stdout=command + "\n")
        if args[:2] == ["tmux", "new-session"]:
            session = args[args.index("-s") + 1]
            self.existing_sessions.add(session)
        if args[:2] == ["tmux", "kill-session"]:
            session = args[-1]
            self.existing_sessions.discard(session)
        return subprocess.CompletedProcess(args, 0)


def _leaf_commands(node: object) -> list[str]:
    commands: list[str] = []
    if isinstance(node, dict):
        widgets = node.get("Widgets")
        if isinstance(widgets, list):
            for child in widgets:
                commands.extend(_leaf_commands(child))
        elif "Command" in node:
            commands.append(str(node["Command"]))
    return commands


def test_dry_run_materializes_pgu_layout_with_all_role_commands() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        layout_output = Path(tmp) / "layout.json"
        result = launch_project(
            config,
            config_path=config_path,
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            dry_run=True,
            layout_output=layout_output,
        )

        assert result == 0
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 8
        active_commands = [command for command in commands if command]
        assert len(active_commands) == 7
        for role in ("director", "main", "app", "research", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        assert not any(" perf " in f" {command} " for command in commands)


def test_pgu_config_matches_director_supplied_live_role_assignments() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    assert set(roles) == {"director", "main", "app", "research", "ops", "audit", "inspector"}
    assert all(role.yolo for role in roles.values())
    assert (roles["director"].cli, roles["director"].model, roles["director"].effort) == (
        ["claude"],
        "claude-opus-4-8",
        "high",
    )
    assert (roles["ops"].cli, roles["ops"].model, roles["ops"].effort, roles["ops"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["main"].cli, roles["main"].model, roles["main"].effort, roles["main"].extra_args) == (
        ["codex"],
        "gpt-5.6-terra",
        "high",
        [],
    )
    assert (roles["app"].cli, roles["app"].model, roles["app"].effort, roles["app"].extra_args) == (
        ["codex"],
        "gpt-5.4",
        "high",
        [],
    )
    assert (roles["research"].cli, roles["research"].model, roles["research"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["audit"].cli, roles["audit"].model, roles["audit"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["inspector"].cli, roles["inspector"].model, roles["inspector"].effort) == (
        ["agy"],
        "Gemini 3.5 Flash (Medium)",
        "",
    )


def test_yolo_config_translates_to_cli_specific_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    expected_flags = {
        "director": ["--dangerously-skip-permissions"],
        "main": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "app": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "research": ["--dangerously-skip-permissions"],
        "ops": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "audit": ["--dangerously-skip-permissions"],
        "inspector": ["--dangerously-skip-permissions"],
    }

    for role_name, flags in expected_flags.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        for expected_flag in flags:
            assert expected_flag in command, (role_name, command)


def test_effort_config_translates_to_cli_specific_args() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    for role_name in ("director", "research", "audit"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "--effort" in command, (role_name, command)
        assert command[command.index("--effort") + 1] == "high"
        assert "reasoning_effort=high" not in command

    for role_name in ("main", "app", "ops"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "-c" in command, (role_name, command)
        assert command[command.index("-c") + 1] == "reasoning_effort=high"
        assert "--effort" not in command

    inspector_command = cli_command_for_role(roles["inspector"], session_dir=config.session_dir)
    assert "--effort" not in inspector_command
    assert "-c" not in inspector_command
    assert "reasoning_effort=high" not in inspector_command


def test_pgu_launch_commands_include_model_and_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_by_role = {
        "director": ["claude", "--model", "claude-opus-4-8", "--effort", "high", "--dangerously-skip-permissions"],
        "main": [
            "codex",
            "--model",
            "gpt-5.6-terra",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "app": [
            "codex",
            "--model",
            "gpt-5.4",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "research": ["claude", "--model", "claude-sonnet-5", "--effort", "high", "--dangerously-skip-permissions"],
        "ops": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "audit": ["claude", "--model", "claude-sonnet-5", "--effort", "high", "--dangerously-skip-permissions"],
        "inspector": ["agy", "--model", "Gemini 3.5 Flash (Medium)", "--dangerously-skip-permissions"],
    }

    for role_name, expected_tail in expected_by_role.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert command[:2] == ["env", f"PGU_PANE_TARGET=pgu-{role_name}:0.0"]
        assert command[2:] == expected_tail, (role_name, command)
        assert "--reasoning-effort" not in command
        assert "gemini" not in command
        assert "--yolo" not in command


def test_start_is_attach_or_start_and_never_duplicates_existing_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"})

    assert run_role_pane(role, mode="start", session_dir=config.session_dir, runner=runner) == 0

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "attach", "-t", "pgu-ops"],
    ]


def test_start_creates_missing_session_once_then_attaches() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()

    assert run_role_pane(role, mode="start", session_dir=config.session_dir, runner=runner) == 0

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "--model gpt-5.5" in runner.calls[1][-1]
    assert "-c reasoning_effort=high" in runner.calls[1][-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[1][-1]
    assert "--dangerously-bypass-hook-trust" in runner.calls[1][-1]
    assert "--reasoning-effort" not in runner.calls[1][-1]
    assert runner.calls[2] == ["tmux", "attach", "-t", "pgu-ops"]


def test_reload_uses_recorded_resume_uuid_when_recreating_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})

        assert cli_command_for_role(role, session_dir=session_dir, resume=True)[-2:] == ["--resume", session_id]
        assert run_role_pane(role, mode="reload", session_dir=session_dir, runner=runner) == 0

        assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
        assert runner.calls[1] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[2] == ["tmux", "kill-session", "-t", "pgu-ops"]
        assert runner.calls[3][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
        assert f"--resume {session_id}" in runner.calls[3][-1]
        assert runner.calls[4] == ["tmux", "attach", "-t", "pgu-ops"]


def test_reload_refuses_to_kill_session_when_live_command_mismatches_config() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    assert run_role_pane(role, mode="reload", session_dir=config.session_dir, runner=runner) == 1

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"],
    ]


def test_reload_force_bypasses_live_command_guard() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    assert run_role_pane(
        role,
        mode="reload",
        session_dir=config.session_dir,
        force_reload=True,
        runner=runner,
    ) == 0

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1] == ["tmux", "kill-session", "-t", "pgu-ops"]


def test_bootstrap_template_does_not_guess_active_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        output = Path(tmp) / "porter.json"

        assert write_template("porter", output) == 0

        template = json.loads(output.read_text(encoding="utf-8"))
        assert template["project"] == "porter"
        assert template["roles"] == []


def main() -> int:
    test_dry_run_materializes_pgu_layout_with_all_role_commands()
    test_pgu_config_matches_director_supplied_live_role_assignments()
    test_yolo_config_translates_to_cli_specific_bypass_flags()
    test_effort_config_translates_to_cli_specific_args()
    test_pgu_launch_commands_include_model_and_bypass_flags()
    test_start_is_attach_or_start_and_never_duplicates_existing_session()
    test_start_creates_missing_session_once_then_attaches()
    test_reload_uses_recorded_resume_uuid_when_recreating_session()
    test_reload_refuses_to_kill_session_when_live_command_mismatches_config()
    test_reload_force_bypasses_live_command_guard()
    test_bootstrap_template_does_not_guess_active_roles()
    print("team_launcher_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
