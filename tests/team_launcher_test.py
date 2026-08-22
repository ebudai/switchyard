#!/usr/bin/env python3
"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.team_launcher as team_launcher
from scripts.ticket_board.notify_listener import PaneHookStateStore
from scripts.team_launcher import (
    cli_command_for_role,
    default_user_bin,
    deploy_launcher_checkout,
    ensure_configured_runtime_user,
    ensure_project_worktrees,
    ensure_user_linger_runtime,
    git_checkout_launcher_branch_args,
    git_clean_launcher_checkout_args,
    git_clean_shared_checkout_args,
    git_fast_forward_launcher_ref_args,
    git_fetch_launcher_ref_args,
    git_checkout_shared_ref_args,
    git_fetch_worktree_ref_args,
    git_launcher_ahead_behind_args,
    git_launcher_checkout_check_args,
    git_shared_checkout_check_args,
    konsole_launch_args,
    launch_konsole_window,
    launch_project,
    live_command_matches_role,
    load_project_config,
    pane_state_file_name,
    provision_runtime_command,
    run_detached_role,
    run_role_pane,
    session_file_name,
    worktree_ref,
    write_template,
)


class FakeRunner:
    def __init__(
        self,
        existing_sessions: set[str] | None = None,
        *,
        current_commands: dict[str, str | list[str]] | None = None,
        pane_pids: dict[str, int | list[int]] | None = None,
    ) -> None:
        self.existing_sessions = set(existing_sessions or set())
        self.current_commands = current_commands or {}
        self.pane_pids = pane_pids or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _value_for(mapping: dict[str, Any], key: str, default: Any) -> Any:
        value = mapping.get(key, default)
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            if len(value) == 1:
                return value[0]
            return default
        return value

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            session = args[-1]
            return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
        if args[:3] == ["tmux", "display-message", "-p"]:
            target = args[args.index("-t") + 1]
            if args[-1] == "#{pane_pid}":
                pane_pid = int(self._value_for(self.pane_pids, target, 0) or 0)
                return subprocess.CompletedProcess(args, 0 if pane_pid else 1, stdout=f"{pane_pid}\n" if pane_pid else "")
            command = str(self._value_for(self.current_commands, target, "") or "")
            return subprocess.CompletedProcess(args, 0 if command else 1, stdout=command + "\n")
        if args[:2] == ["tmux", "new-session"]:
            session = args[args.index("-s") + 1]
            self.existing_sessions.add(session)
        if args[:2] == ["tmux", "kill-session"]:
            session = args[-1]
            self.existing_sessions.discard(session)
        if len(args) >= 4 and args[:4] == ["git", "-C", args[2], "fetch"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "worktree", "add"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 6 and args[:6] == ["git", "-C", args[2], "checkout", "--detach", "--force"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "clean", "-fdx"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 6 and args[:6] == ["git", "-C", args[2], "rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 0\n")
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


def _layout_shape(node: object) -> object:
    if isinstance(node, dict) and isinstance(node.get("Widgets"), list):
        return {
            "Orientation": node.get("Orientation"),
            "Widgets": [_layout_shape(child) for child in node["Widgets"]],
        }
    return "leaf"


PANEL_CLOCKWISE_SLOT_ORDER = (0, 2, 3, 5, 4, 1)


def _pgu_project_root() -> Path:
    return Path("/home/agent/Projects/pgu")


def test_pgu_layout_matches_reference_six_pane_geometry() -> None:
    layout = json.loads((ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json").read_text(encoding="utf-8"))

    assert _layout_shape(layout) == {
        "Orientation": "Horizontal",
        "Widgets": [
            {
                "Orientation": "Vertical",
                "Widgets": ["leaf", "leaf"],
            },
            {
                "Orientation": "Vertical",
                "Widgets": [
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                ],
            },
        ],
    }
    assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {""}


def _write_pgu_config_with_shared_checkout(tmp: Path) -> Path:
    source = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    source["layout"] = str(ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json")
    source["repository"] = str(tmp / "repo")
    source["worktree_base"] = str(tmp / "worktrees")
    source["session_dir"] = str(tmp / "sessions")
    config_path = tmp / "pgu.json"
    config_path.write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["git", "add", "."], cwd=repo)
    _run_git(["git", "commit", "-m", message], cwd=repo)


def _make_origin_backed_repo(tmp: Path) -> tuple[Path, Path]:
    seed = tmp / "seed"
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    seed.mkdir()
    _run_git(["git", "init", "-b", "main"], cwd=seed)
    _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=seed)
    _run_git(["git", "config", "user.name", "PGU Agent"], cwd=seed)
    (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _commit_all(seed, "initial")
    _run_git(["git", "clone", "--bare", str(seed), str(origin)])
    _run_git(["git", "clone", str(origin), str(repo)])
    _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
    _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
    return origin, repo


def _write_minimal_shared_checkout_config(tmp: Path, repo: Path, roles: list[str]) -> Path:
    layout = {
        "KonsoleTabs": [
            {
                "Widgets": [
                    {"SessionRestoreId": index, "Command": "", "WorkingDirectory": ""}
                    for index, _role in enumerate(roles)
                ]
            }
        ]
    }
    layout_path = tmp / "layout.json"
    layout_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config = {
        "project": "pgu",
        "layout": str(layout_path),
        "repository": str(repo),
        "worktree_base": str(tmp / "worktrees"),
        "roles": [
            {
                "role": role,
                "slot": index,
                "target": f"pgu-{role}:0.0",
                "cli": ["codex"],
            }
            for index, role in enumerate(roles)
        ],
    }
    config_path = tmp / "pgu.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _session_payload(target: str, session_id: str, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "target": target,
            "session_id": session_id,
            "source": "codex.SessionStart",
            "payload": payload,
        }
    ) + "\n"


def _read_pane_state(state_dir: Path, target: str) -> dict[str, object]:
    return json.loads((state_dir / pane_state_file_name(target)).read_text(encoding="utf-8"))


def _patch_process_snapshot(
    parents_by_pid: dict[int, int],
    children_by_parent: dict[int, list[int]],
    process_names: dict[int, set[str]],
    argv_by_pid: dict[int, list[str]],
) -> Any:
    original = team_launcher._process_snapshot
    team_launcher._process_snapshot = lambda: (parents_by_pid, children_by_parent, process_names, argv_by_pid)
    return original


def test_dry_run_materializes_pgu_layout_with_six_visible_role_commands() -> None:
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
        assert len(commands) == 6
        active_commands = [command for command in commands if command]
        assert len(active_commands) == 6
        clockwise_commands = [commands[index] for index in PANEL_CLOCKWISE_SLOT_ORDER]
        expected_clockwise_roles = ["inspector", "director", "audit", "ops", "app", "main"]
        for role in ("director", "main", "app", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        for role, command in zip(expected_clockwise_roles, clockwise_commands):
            assert f" {role} " in f" {command} ", (role, clockwise_commands)
        assert not any(" research " in f" {command} " for command in commands)
        assert not any(" perf " in f" {command} " for command in commands)


def test_pgu_config_matches_director_supplied_live_role_assignments() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_repository = Path("/home/agent/Projects/pgu")

    assert set(roles) == {"director", "main", "app", "research", "ops", "audit", "inspector"}
    assert all(role.yolo for role in roles.values())
    assert config.repository == expected_repository
    assert config.run_as_user == "agent"
    assert config.worktree_base is None
    assert worktree_ref(config) == "origin/main"
    assert roles["research"].detached
    assert roles["research"].slot is None
    assert {role.role: role.slot for role in roles.values() if not role.detached} == {
        "director": 2,
        "main": 1,
        "app": 4,
        "ops": 5,
        "audit": 3,
        "inspector": 0,
    }
    for role_name, role in roles.items():
        assert role.workdir == str(expected_repository), role_name
    assert (roles["director"].cli, roles["director"].model, roles["director"].effort) == (
        ["claude"],
        "claude-opus-4-8",
        "high",
    )
    assert (roles["director"].resume_mode, roles["director"].resume_flag) == ("flag", "--resume")
    assert (roles["ops"].cli, roles["ops"].model, roles["ops"].effort, roles["ops"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["ops"].resume_mode, roles["ops"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["main"].cli, roles["main"].model, roles["main"].effort, roles["main"].extra_args) == (
        ["codex"],
        "gpt-5.6-terra",
        "high",
        [],
    )
    assert (roles["main"].resume_mode, roles["main"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["app"].cli, roles["app"].model, roles["app"].effort, roles["app"].extra_args) == (
        ["codex"],
        "gpt-5.4",
        "high",
        [],
    )
    assert (roles["app"].resume_mode, roles["app"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["research"].cli, roles["research"].model, roles["research"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["research"].resume_mode, roles["research"].resume_flag) == ("flag", "--resume")
    assert (roles["audit"].cli, roles["audit"].model, roles["audit"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["audit"].resume_mode, roles["audit"].resume_flag) == ("flag", "--resume")
    assert (roles["inspector"].cli, roles["inspector"].model, roles["inspector"].effort) == (
        ["agy"],
        "Gemini 3.5 Flash (Medium)",
        "",
    )
    assert (roles["inspector"].resume_mode, roles["inspector"].resume_flag) == ("flag", "--conversation")


def test_pgu_config_keeps_live_agent_repository_with_foreign_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = "/home/eric"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == "agent"
    assert config.repository == Path("/home/agent/Projects/pgu")
    assert {role.workdir for role in config.roles} == {"/home/agent/Projects/pgu"}


def test_cli_command_prepends_invoking_user_bin() -> None:
    original_home = os.environ.get("HOME")
    original_bin_dir = os.environ.pop("PGU_TEAM_LAUNCHER_BIN_DIR", None)
    try:
        os.environ["HOME"] = "/home/otto-agent"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
        role = next(role for role in config.roles if role.role == "ops")

        command = cli_command_for_role(role, session_dir=config.session_dir)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
        if original_bin_dir is not None:
            os.environ["PGU_TEAM_LAUNCHER_BIN_DIR"] = original_bin_dir

    assert command[2].startswith("PATH=/home/otto-agent/bin:")


def test_custom_config_without_run_as_user_tracks_invoking_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            tmp_path = Path(tmp)
            layout = {"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}
            layout_path = tmp_path / "layout.json"
            layout_path.write_text(json.dumps(layout), encoding="utf-8")
            config_path = tmp_path / "porter.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "porter",
                        "layout": str(layout_path),
                        "repository": "$HOME/Projects/porter",
                        "roles": [{"role": "ops", "slot": 0, "cli": ["codex"]}],
                    }
                ),
                encoding="utf-8",
            )
            os.environ["HOME"] = "/home/otto-agent"

            config = load_project_config("porter", config_path)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == ""
    assert config.repository == Path("/home/otto-agent/Projects/porter")
    assert config.roles[0].workdir == "/home/otto-agent/Projects/porter"


def test_pane_state_filename_matches_notify_listener_target_path() -> None:
    target = "pgu-ops:0.0"
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        listener_path = PaneHookStateStore(Path(tmp))._target_path(target)

    assert pane_state_file_name(target) == listener_path.name == "pgu-ops_0.0.json"


def test_ensure_user_linger_runtime_enables_linger_and_waits_for_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []
    checks: list[Path] = []
    sleeps: list[float] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def runtime_exists(path: Path) -> bool:
        checks.append(path)
        return len(checks) == 3

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        runtime_dir = ensure_user_linger_runtime(
            "otto-agent",
            runner=runner,
            runtime_exists=runtime_exists,
            sleeper=sleeps.append,
            attempts=5,
            poll_seconds=0.25,
        )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]
    assert checks == [Path("/run/user/1005"), Path("/run/user/1005"), Path("/run/user/1005")]
    assert sleeps == [0.25, 0.25]


def test_ensure_user_linger_runtime_fails_loud_when_loginctl_is_denied() -> None:
    original_uid_for_user = team_launcher.uid_for_user

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Access denied")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        try:
            ensure_user_linger_runtime(
                "otto-agent",
                runner=runner,
                runtime_exists=lambda _path: False,
                sleeper=lambda _seconds: None,
                attempts=1,
            )
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert "failed to enable linger for 'otto-agent': Access denied" in message
    assert "sudo loginctl enable-linger otto-agent" in message


def test_configured_runtime_check_skips_non_runtime_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
        config = load_project_config("pgu", config_path)

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected runtime provisioning call: {args}")

        assert ensure_configured_runtime_user(config, runner=runner) is None


def test_configured_runtime_check_uses_run_as_user_for_default_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            raw["session_dir"] = "/run/user/1005/pgu-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            runtime_dir = ensure_configured_runtime_user(
                config,
                runner=runner,
                runtime_exists=lambda path: path == Path("/run/user/1005"),
                sleeper=lambda _seconds: None,
                attempts=1,
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]


def test_provision_runtime_command_uses_configured_project_user() -> None:
    original_ensure = team_launcher.ensure_user_linger_runtime
    calls: list[str] = []

    def fake_ensure(user_name: str) -> Path:
        calls.append(user_name)
        return Path("/run/user/1005")

    try:
        team_launcher.ensure_user_linger_runtime = fake_ensure
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            assert provision_runtime_command(None, config) == 0
    finally:
        team_launcher.ensure_user_linger_runtime = original_ensure

    assert calls == ["otto-agent"]


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
        assert command[2].startswith(f"PATH={default_user_bin()}:"), (role_name, command)
        assert command[3:] == expected_tail, (role_name, command)
        assert "--reasoning-effort" not in command
        assert "--continue" not in command
        assert "--last" not in command
        assert "gemini" not in command
        assert "--yolo" not in command


def test_start_refuses_stale_launcher_checkout_before_touching_role_worktrees() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:] == ["fetch", config.worktree_remote, config.worktree_branch]:
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 173\n")
        raise AssertionError(f"unexpected call after stale launcher refusal: {args}")

    try:
        launch_project(
            config,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            runner=runner,
        )
        raise AssertionError("expected stale launcher checkout to abort")
    except SystemExit as exc:
        message = str(exc)

    assert "refusing to launch from stale checkout" in message
    assert "173 commit(s) behind origin/main" in message
    assert "deploy-launcher" in message
    assert calls == [
        git_launcher_checkout_check_args(ROOT),
        git_fetch_launcher_ref_args(config, ROOT),
        git_launcher_ahead_behind_args(config, ROOT),
    ]


def test_deploy_launcher_checkout_updates_and_verifies_configured_ref() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    launcher_repo = Path("/home/eric/Projects/pgu")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["git", "-C", str(launcher_repo)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 0\n")
        return subprocess.CompletedProcess(args, 0)

    assert deploy_launcher_checkout(config, launcher_repo=launcher_repo, runner=runner, clean=True) == 0

    assert calls == [
        git_launcher_checkout_check_args(launcher_repo),
        git_fetch_launcher_ref_args(config, launcher_repo),
        git_checkout_launcher_branch_args(config, launcher_repo),
        git_fast_forward_launcher_ref_args(config, launcher_repo),
        git_clean_launcher_checkout_args(launcher_repo),
        git_launcher_checkout_check_args(launcher_repo),
        git_fetch_launcher_ref_args(config, launcher_repo),
        git_launcher_ahead_behind_args(config, launcher_repo),
    ]


def test_konsole_launch_uses_gui_user_display_environment() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    try:
        os.environ["PGU_HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        team_launcher.uid_for_user = lambda _user_name: None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="eric") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland


def test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_wayland_name = os.environ.get("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY")
    try:
        os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "eric" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="eric") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_wayland_name is None:
            os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name


def test_konsole_launch_defaults_to_invoking_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_wayland_name = os.environ.get("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY")
    try:
        os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.current_user_name = lambda: "otto-agent"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "otto-agent" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_wayland_name is None:
            os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name


def test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    try:
        team_launcher.uid_for_user = lambda _user_name: None

        command = konsole_launch_args(Path("/tmp/layout.json"), gui_user="nobody")
        assert command[:2] == ["sh", "-lc"]
        assert "no host Wayland display" in command[2]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland


def test_launch_konsole_window_detaches_real_spawn_and_returns_status_line() -> None:
    original_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stdout = sys.stdout
    stdout = StringIO()
    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            self.pid = 424242

        def poll(self) -> int | None:
            return None

    try:
        os.environ["PGU_HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stdout = stdout

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == 0
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stdout = original_stdout
        if original_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland

    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["args"] == konsole_launch_args(Path("/tmp/layout.json"))
    kwargs = call["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert hasattr(kwargs["stdout"], "name")
    assert kwargs["stderr"] is kwargs["stdout"]
    assert "team-launcher: started pgu in background (Konsole pid 424242; log " in stdout.getvalue()


def test_start_is_attach_or_start_and_never_duplicates_existing_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=config.session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "attach", "-t", "pgu-ops"],
    ]


def test_start_creates_missing_session_once_then_attaches() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        # No recorded session id -> start falls back to a fresh launch (no resume args).
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["target"] == role.target
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        assert isinstance(state["updated_at"], float)

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert runner.calls[1][5:7] == ["-c", str(_pgu_project_root())]
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "--model gpt-5.5" in runner.calls[1][-1]
    assert "-c reasoning_effort=high" in runner.calls[1][-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[1][-1]
    assert "--dangerously-bypass-hook-trust" in runner.calls[1][-1]
    assert "--reasoning-effort" not in runner.calls[1][-1]
    assert " resume " not in f" {runner.calls[1][-1]} "
    assert runner.calls[2] == ["tmux", "attach", "-t", "pgu-ops"]


def test_start_seeds_initial_idle_state_for_codex_agy_and_claude_panes() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_cli_by_role = {
        "ops": "codex",
        "inspector": "agy",
        "director": "claude",
    }

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        for role_name, cli_name in expected_cli_by_role.items():
            role = roles[role_name]
            runner = FakeRunner()
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=tmp_path / role_name / "sessions",
                    pane_state_dir=tmp_path / role_name / "pane-state",
                    runner=runner,
                )
                == 0
            )
            state = _read_pane_state(tmp_path / role_name / "pane-state", role.target)
            assert state == {
                "source": "team_launcher.start",
                "state": "idle",
                "target": role.target,
                "updated_at": state["updated_at"],
            }
            assert isinstance(state["updated_at"], float)
            assert f" {cli_name} " in f" {runner.calls[1][-1]} "


def test_start_resumes_recorded_session_when_recreating_missing_pane() -> None:
    # start (attach-or-start) must resume a tracked session id when relaunching a
    # stopped pane -- resume is not reload-only. A cold restart uses start mode.
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(current_commands={"pgu-ops:0.0": "codex"})

        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert f"codex resume {session_id}" in runner.calls[1][-1]
    # resume verified via pane inspection -> no fresh fallback, then attach
    assert ["tmux", "kill-session", "-t", "pgu-ops"] not in runner.calls
    assert runner.calls[-1] == ["tmux", "attach", "-t", "pgu-ops"]


def test_start_runs_research_detached_before_opening_visible_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner()
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_fetch_launcher_ref_args(config, ROOT),
            git_launcher_ahead_behind_args(config, ROOT),
        ]
        assert runner.calls[3] == git_fetch_worktree_ref_args(config)
        assert runner.calls[4] == git_shared_checkout_check_args(config)
        assert runner.calls[5] == git_checkout_shared_ref_args(config)
        assert runner.calls[6] == git_clean_shared_checkout_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        research_has_session_index = runner.calls.index(["tmux", "has-session", "-t", "pgu-research"])
        research_new_session = runner.calls[research_has_session_index + 1]
        assert research_new_session[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]
        assert research_new_session[5:7] == ["-c", str(tmp_path / "repo")]
        assert "PGU_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "--model claude-sonnet-5" in research_new_session[-1]
        state = _read_pane_state(tmp_path / "pane-state", "pgu-research:0.0")
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {str(tmp_path / "repo")}
        assert runner.calls[-1] == konsole_launch_args(layout_output)
        assert ["tmux", "attach", "-t", "pgu-research"] not in runner.calls


def test_start_force_refreshes_dirty_shared_checkout_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        config = load_project_config("pgu", config_path)
        shared_checkout = Path(config.roles[0].workdir)
        assert shared_checkout == repo

        assert ensure_project_worktrees(config, refresh=True).ok

        (shared_checkout / "tracked.txt").write_text("dirty tracked edit\n", encoding="utf-8")
        (shared_checkout / "untracked.tmp").write_text("junk\n", encoding="utf-8")

        assert ensure_project_worktrees(config, refresh=True).ok

        assert (shared_checkout / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
        assert not (shared_checkout / "untracked.tmp").exists()
        status = _run_git(["git", "status", "--porcelain"], cwd=shared_checkout).stdout
        assert status == ""


def test_bad_shared_checkout_blocks_all_roles_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "not-a-git-checkout"
        repo.mkdir()
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops", "app"])
        config = load_project_config("pgu", config_path)

        result = ensure_project_worktrees(config, refresh=True)

        assert set(result.failed_roles) == {"ops", "app"}


def test_reload_fetches_without_refreshing_shared_checkout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner()
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="reload",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_fetch_launcher_ref_args(config, ROOT),
            git_launcher_ahead_behind_args(config, ROOT),
        ]
        assert runner.calls[3] == git_fetch_worktree_ref_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        assert git_checkout_shared_ref_args(config) not in runner.calls
        assert git_clean_shared_checkout_args(config) not in runner.calls


def test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1200})
        original_snapshot = _patch_process_snapshot(
            {1200: 6388, 1201: 1200},
            {1200: [1201]},
            {1200: {"fish", "claude"}, 1201: {"claude"}},
            {
                1200: ["fish", "-c", "claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
                1201: ["claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="reload",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )
        finally:
            team_launcher._process_snapshot = original_snapshot

        synced = json.loads(config_path.read_text(encoding="utf-8"))
        synced_research = next(role for role in synced["roles"] if role["role"] == "research")
        assert synced_research["cli"] == ["claude"]
        assert synced_research["model"] == "claude-sonnet-5"
        assert synced_research["detached"] is True
        assert synced_research["target"] == "pgu-research:0.0"
        assert synced_research["effort"] == "high"
        assert synced_research["yolo"] is True

        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        assert "claude --resume live-research-session --model claude-sonnet-5" in research_new_session[-1]
        assert "codex resume" not in research_new_session[-1]


def test_reload_sync_leaves_config_unchanged_when_live_matches() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1300})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1300: 6388, 1301: 1300},
            {1300: [1301]},
            {1300: {"fish", "claude"}, 1301: {"claude"}},
            {
                1300: ["fish", "-c", "claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
                1301: ["claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_reload_sync_leaves_config_unchanged_when_role_not_running() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner()
        before = config_path.read_text(encoding="utf-8")

        synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_reload_sync_leaves_config_unchanged_when_model_is_unparseable() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1400})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1400: 6388, 1401: 1400},
            {1400: [1401]},
            {1400: {"fish", "claude"}, 1401: {"claude"}},
            {
                1400: ["fish", "-c", "claude", "--dangerously-skip-permissions"],
                1401: ["claude", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_dry_run_reports_shared_checkout_without_syncing_it() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    runner = FakeRunner()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        layout_output = Path(tmp) / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                dry_run=True,
                layout_output=layout_output,
            )
            == 0
        )

        assert runner.calls == []
        plan = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(plan)} == {
            str(_pgu_project_root())
        }
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 6


def test_start_does_not_sync_live_cli_or_model() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(current_commands={"pgu-research:0.0": "codex"})
        before = config_path.read_text(encoding="utf-8")

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert config_path.read_text(encoding="utf-8") == before
        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        # start resumes the recorded id; cli/model stay configured (not synced from live)
        assert "live-research-session" in research_new_session[-1]
        assert "--model gpt-5.5" in research_new_session[-1]
        assert "claude-sonnet-5" not in research_new_session[-1]


def test_detached_research_attach_checks_session_without_attaching() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    runner = FakeRunner(existing_sessions={"pgu-research"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        assert (
            run_detached_role(
                role,
                mode="attach",
                session_dir=config.session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=runner,
            )
            == 0
        )

    assert runner.calls == [["tmux", "has-session", "-t", "pgu-research"]]


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

        assert cli_command_for_role(role, session_dir=session_dir, resume=True)[3:6] == ["codex", "resume", session_id]
        pane_state_dir = Path(tmp) / "pane-state"
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )
        state = _read_pane_state(pane_state_dir, role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.reload"

        assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
        assert runner.calls[1] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
        assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[3] == ["tmux", "kill-session", "-t", "pgu-ops"]
        assert runner.calls[4][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
        assert f"codex resume {session_id}" in runner.calls[4][-1]
        assert "--continue" not in runner.calls[4][-1]
        assert "--last" not in runner.calls[4][-1]
        assert runner.calls[5] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
        assert runner.calls[6] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[7] == ["tmux", "attach", "-t", "pgu-ops"]


def test_resume_commands_use_cli_specific_shapes_and_front_position() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    session_id = "12345678-1234-5678-9abc-def012345678"

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        for role_name in ("director", "ops", "inspector"):
            role = roles[role_name]
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id}) + "\n",
                encoding="utf-8",
            )

        director_command = cli_command_for_role(roles["director"], session_dir=session_dir, resume=True)
        ops_command = cli_command_for_role(roles["ops"], session_dir=session_dir, resume=True)
        inspector_command = cli_command_for_role(roles["inspector"], session_dir=session_dir, resume=True)

        assert director_command[2].startswith(f"PATH={default_user_bin()}:")
        assert director_command[3:6] == ["claude", "--resume", session_id]
        assert director_command[6:10] == ["--model", "claude-opus-4-8", "--effort", "high"]

        assert ops_command[2].startswith(f"PATH={default_user_bin()}:")
        assert ops_command[3:6] == ["codex", "resume", session_id]
        assert ops_command[6:10] == ["--model", "gpt-5.5", "-c", "reasoning_effort=high"]

        assert inspector_command[2].startswith(f"PATH={default_user_bin()}:")
        assert inspector_command[3:6] == ["agy", "--conversation", session_id]
        assert inspector_command[6:8] == ["--model", "Gemini 3.5 Flash (Medium)"]


def test_reload_without_recorded_resume_id_logs_fresh_start() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=Path(tmp) / "sessions",
                    pane_state_dir=Path(tmp) / "pane-state",
                    runner=runner,
                )
                == 0
            )

    assert "team-launcher: no recorded session id for ops; starting fresh session" in stderr.getvalue()
    assert runner.calls[4][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert " resume " not in f" {runner.calls[4][-1]} "
    assert "--resume" not in runner.calls[4][-1]


def test_reload_logs_resume_fallback_when_cli_never_starts() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    stderr = StringIO()
    original_timeout = team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS
    original_poll = team_launcher.RESUME_STARTUP_POLL_SECONDS
    try:
        team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.02
        team_launcher.RESUME_STARTUP_POLL_SECONDS = 0.0
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            session_dir = Path(tmp)
            session_id = "12345678-1234-5678-9abc-def012345678"
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id}) + "\n",
                encoding="utf-8",
            )
            runner = FakeRunner(
                existing_sessions={"pgu-ops"},
                current_commands={"pgu-ops:0.0": ["codex", "fish", "fish", "fish"]},
            )

            with redirect_stderr(stderr):
                pane_state_dir = Path(tmp) / "pane-state"
                assert (
                    run_role_pane(
                        role,
                        mode="reload",
                        session_dir=session_dir,
                        pane_state_dir=pane_state_dir,
                        runner=runner,
                    )
                    == 0
                )
                state = _read_pane_state(pane_state_dir, role.target)
                assert state["state"] == "idle"
                assert state["source"] == "team_launcher.reload.fallback"

        assert f"team-launcher: resume failed for ops using session {session_id}; falling back to fresh session" in stderr.getvalue()
        assert "team-launcher: started fresh session for ops after resume fallback" in stderr.getvalue()
        new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]]
        assert len(new_sessions) == 2
        assert f"codex resume {session_id}" in new_sessions[0][-1]
        assert " resume " not in f" {new_sessions[1][-1]} "
    finally:
        team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = original_timeout
        team_launcher.RESUME_STARTUP_POLL_SECONDS = original_poll


def test_reload_refuses_to_kill_session_when_live_command_mismatches_config() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=config.session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=runner,
            )
            == 1
        )

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"],
    ]


def test_reload_force_bypasses_live_command_guard() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=config.session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                force_reload=True,
                runner=runner,
            )
            == 0
        )

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1] == ["tmux", "kill-session", "-t", "pgu-ops"]


def test_reload_guard_matches_cli_child_under_shell_in_real_tmux() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-real-tmux.") as tmp:
        helper = Path(tmp) / "codex"
        marker = Path(tmp) / "running"
        helper.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "trap 'exit 0' TERM INT\n"
            "while :; do sleep 1; done\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        session = f"pgu-321-reload-guard-{os.getpid()}"
        role = next(role for role in load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json").roles if role.role == "ops")
        role = role.__class__(
            role=role.role,
            slot=role.slot,
            detached=False,
            tmux_session=session,
            target=f"{session}:0.0",
            workdir=tmp,
            cli=[str(helper)],
            model="",
            model_arg=role.model_arg,
            effort="",
            yolo=False,
            extra_args=[],
            resume_mode=role.resume_mode,
            resume_flag=role.resume_flag,
            resume_subcommand=role.resume_subcommand,
            live_commands=["codex"],
            env={},
        )
        try:
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "sh",
                    "-c",
                    f"exec {helper}",
                ],
                check=True,
            )
            for _ in range(50):
                if marker.exists():
                    break
                time.sleep(0.1)
            assert marker.exists(), "real tmux helper never started"
            pane_command = subprocess.run(
                ["tmux", "display-message", "-p", "-t", role.target, "#{pane_current_command}"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            assert pane_command in {"sh", "fish", "bash", "zsh"}, pane_command
            assert live_command_matches_role(role)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_bootstrap_template_does_not_guess_active_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        output = Path(tmp) / "porter.json"

        assert write_template("porter", output) == 0

        template = json.loads(output.read_text(encoding="utf-8"))
        assert template["project"] == "porter"
        assert template["roles"] == []


def main() -> int:
    test_pgu_layout_matches_reference_six_pane_geometry()
    test_dry_run_materializes_pgu_layout_with_six_visible_role_commands()
    test_pgu_config_matches_director_supplied_live_role_assignments()
    test_pgu_config_keeps_live_agent_repository_with_foreign_home()
    test_custom_config_without_run_as_user_tracks_invoking_home()
    test_cli_command_prepends_invoking_user_bin()
    test_pane_state_filename_matches_notify_listener_target_path()
    test_ensure_user_linger_runtime_enables_linger_and_waits_for_runtime_dir()
    test_ensure_user_linger_runtime_fails_loud_when_loginctl_is_denied()
    test_configured_runtime_check_skips_non_runtime_session_dir()
    test_configured_runtime_check_uses_run_as_user_for_default_runtime_dir()
    test_provision_runtime_command_uses_configured_project_user()
    test_yolo_config_translates_to_cli_specific_bypass_flags()
    test_effort_config_translates_to_cli_specific_args()
    test_pgu_launch_commands_include_model_and_bypass_flags()
    test_start_refuses_stale_launcher_checkout_before_touching_role_worktrees()
    test_deploy_launcher_checkout_updates_and_verifies_configured_ref()
    test_konsole_launch_uses_gui_user_display_environment()
    test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var()
    test_konsole_launch_defaults_to_invoking_user_uid_without_host_var()
    test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing()
    test_start_is_attach_or_start_and_never_duplicates_existing_session()
    test_start_creates_missing_session_once_then_attaches()
    test_start_seeds_initial_idle_state_for_codex_agy_and_claude_panes()
    test_start_resumes_recorded_session_when_recreating_missing_pane()
    test_start_runs_research_detached_before_opening_visible_layout()
    test_start_force_refreshes_dirty_shared_checkout_with_real_git()
    test_bad_shared_checkout_blocks_all_roles_with_real_git()
    test_reload_fetches_without_refreshing_shared_checkout()
    test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch()
    test_reload_sync_leaves_config_unchanged_when_live_matches()
    test_reload_sync_leaves_config_unchanged_when_role_not_running()
    test_reload_sync_leaves_config_unchanged_when_model_is_unparseable()
    test_dry_run_reports_shared_checkout_without_syncing_it()
    test_start_does_not_sync_live_cli_or_model()
    test_detached_research_attach_checks_session_without_attaching()
    test_reload_uses_recorded_resume_uuid_when_recreating_session()
    test_resume_commands_use_cli_specific_shapes_and_front_position()
    test_reload_without_recorded_resume_id_logs_fresh_start()
    test_reload_logs_resume_fallback_when_cli_never_starts()
    test_reload_refuses_to_kill_session_when_live_command_mismatches_config()
    test_reload_force_bypasses_live_command_guard()
    test_reload_guard_matches_cli_child_under_shell_in_real_tmux()
    test_bootstrap_template_does_not_guess_active_roles()
    print("team_launcher_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
